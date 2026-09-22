"""
文件路径: backend/app/agents/material_agent.py
模块职责: A-05 材料选型 —— 从材料目录中挑选主材辅材，优先全友自有产品
依赖关系: A-01（房间）+ fan-out 注入的 branch_spec -> 本 Agent -> fan-in
对应 Skill 文档: skills/05_material_agent.md
上游: A-00 MasterOrchestrator（经 fan-out 注入 branch_spec）
下游: fan_in_aggregate（与 A-03 / A-04 并行写入同一 plan_bundles[plan_id]）

对应需求文档:
  - 2.2.2 多 Agent 并行方案生成模块（材料选型 Agent，可行性 B）
  - 5.3 材料与知识库（全友产品价格库无真实数据源，须构造演示种子集）
  - AC-18：方案中全友产品覆盖率 ≥ 60%
  - AC-21：悬停显示全友产品价格和官网链接
  - R-09：全友产品数据不可得 —— 构造演示种子数据并**显著标注**

═══════════════════════════════════════════════════════════════════
本 Agent 的三条纪律（每一条都对应一次"否则会怎样"）
═══════════════════════════════════════════════════════════════════

【1】模型只能**指认**，不能**创造**
    它从候选清单里挑 product_id，代码按 id 回填名称/品牌/价格/链接。
    写出的 id 不在候选集里就剔除并记入 invented_products
    （与 A-03 剔除幻觉房间同一手法）。
    否则用户会拿到一个"型号看起来很专业、官网搜不到"的推荐。

【2】价格与链接由代码回填，模型碰不到
    MaterialPlan 里没有任何金额字段。这与 A-04 用类型系统封死 ADR-07
    是同一条纪律 —— 本项目的模式是：**凡是会产出"看起来合理的错误数字"
    的地方，都从类型层面断掉路径**。

【3】AC-18 的覆盖率由代码算，且**代码兜底**
    让模型自报"我推荐了 80% 全友"毫无意义。覆盖率由代码统计；
    若低于 60%，由代码在同品类里换成全友的替代品，并记录每一次替换。
    实测：目录里全友占比恰好 50%，**随机选只有 50%，过不了 60% 的线** ——
    所以这条 AC 是一道真门槛，不是自我感觉良好的声明。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..core.capabilities import OperationNotAllowedError, check_operation
from ..graph.state import HomeDecoState
from ..schemas.material import MaterialPlan
from ..services.material import catalog
from .base import BaseAgent

#: 模型挑选的独立超时。比节点总超时（30s）短 —— 让"兜底"先于"熔断"发生。
#: 与 A-04 的 NARRATIVE_TIMEOUT 同一个思路。
SELECT_TIMEOUT = 22.0

SYSTEM_PROMPT = """你是一名装修材料选配顾问，为业主在一份**给定的候选清单**里挑选材料。

【最重要的一条：你只能从候选清单里挑】
每个品类都会给你一份候选商品列表，每项带 id、名称、品牌、规格、价格区间，
以及"匹配理由"。你只能从这些候选里选，**不得自行编造型号或品牌**。

候选里确实没有合适的，就把那个品类**留空不选** —— 少选一项，
比编一个搜不到的型号强得多。系统会用代码核对你的每一个 id，
对不上的会被剔除。

【优先全友自有产品】
这是全友的平台，优先推荐自有产品是明确的业务要求。
在**同样合适**的前提下选全友；如果某品类的全友产品明显不匹配
（规格不符、档位不符），可以选竞品，但要在 reason 里说清为什么。

【理由要引用给定的匹配信息】
候选清单里已经给出了每件商品的匹配理由（如「适配需求（children）」
「风格匹配（nordic）」「环保等级 E0」）。你的 reason 应当**基于这些已有信息**，
不要自己另编依据，也不要声称候选清单里没有的参数。

【不要谈钱】
价格由系统从目录里直接取用，**你不需要也不允许计算、估算或提及任何金额**。
你的价值在于"选得对、说得清"，不在于报价。

【边界】
你只负责**选材**。报价单审查、增项漏项、合同风险都不属于你 ——
那是避坑审查 Agent 的职责，不要写进 warnings。"""


class MaterialAgent(BaseAgent):
    """A-05：房间 + 档位 + 需求 → 逐品类的材料选择（优先全友）。"""

    code = "A-05"
    name = "MaterialAgent"
    requires_vision = False
    timeout = 30.0

    async def run(self, state: HomeDecoState) -> dict[str, Any]:
        layout = state.get("layout")
        if not layout:
            raise ValueError(
                "状态中缺少 layout，A-05 无法执行。"
                "上游 A-01 可能已失败——请检查 errors 字段"
            )

        # ══ 业务连续性守卫 ══════════════════════════════════════
        # 用 select_materials 而非 generate_plan —— 选材不需要墙体信息，
        # 用后者会把「没识别出墙体但房间面积齐全」的户型过度拒绝掉。
        cap = check_operation(layout, "select_materials")
        if not cap.allowed:
            raise OperationNotAllowedError("select_materials", cap)

        spec = self._branch_spec(state)
        requirements = state.get("requirements") or {}

        # ══ 第 1 层：确定性检索（纯函数，无网络）══════════════
        pool = catalog.candidates(
            grade=spec["budget_grade"],
            style=spec["style"],
            requirements=requirements,
        )

        if not any(pool.values()):
            raise ValueError(
                f"材料目录中没有任何适配 {spec['budget_grade']} 档的商品，无法选材"
            )

        # ══ 第 2 层：模型挑选（只给理由，不给数字）══════════════
        parsed, degraded, reasons = await self._select(pool, spec, requirements)

        # ══ 第 3 层：代码回填与校验 ════════════════════════════
        payload = self._postprocess(parsed, pool, spec)

        self.log.info(
            f"选材 {spec['plan_id']} 完成 "
            f"{len(payload['items'])} 个品类，"
            f"全友覆盖 {payload['quanyou_coverage']:.0%}"
            f"{'（代码补足）' if payload['auto_substitutions'] else ''}，"
            f"幻觉商品 {len(payload['invented_products'])} 个"
            f"{'，已降级' if degraded else ''}"
        )

        return {
            "plan_bundles": {spec["plan_id"]: {"materials": payload}},
            "degraded": degraded,
            "degrade_reasons": (
                [f"[A-05/{spec['plan_id']}] {r}" for r in reasons] if reasons else []
            ),
            "phase": "planning",
            "_llm_meta": getattr(self, "_last_llm_meta", None),
        }

    # ── 挑选：模型优先，确定性兜底 ────────────────────────

    async def _select(
        self,
        pool: dict[str, list[catalog.ScoredProduct]],
        spec: dict[str, Any],
        requirements: dict[str, Any],
    ) -> tuple[MaterialPlan, bool, list[str]]:
        """
        让模型在候选里挑。**任何失败都不向上抛** —— 回退到确定性排序。

        为什么 A-05 也该有兜底：候选排序本身（`catalog.candidates`）已经
        编码了档位过滤、风格匹配、需求适配与全友优先，**不需要模型参与**。
        模型的价值在于"挑得更有判断力、理由说得更贴切"，
        而不是"提供候选" —— 后者本来就是确定性的。

        这与 A-04「先算数、再写字」是同一条纪律：
        **把不依赖模型的那部分产物保护起来，不让它被模型的失败拖走。**
        区别只是 A-04 保住的是数字，A-05 保住的是选择。
        """
        try:
            parsed, result = await asyncio.wait_for(
                self._call_llm(
                    system=SYSTEM_PROMPT,
                    user=self._build_prompt(pool, spec, requirements),
                    schema=MaterialPlan,
                ),
                timeout=SELECT_TIMEOUT,
            )
            assert isinstance(parsed, MaterialPlan)
            return parsed, result.degraded, (
                [f"选材由本地文本模型完成：{result.degrade_reason}"]
                if result.degrade_reason else []
            )

        except asyncio.TimeoutError:
            reason = f"选材超时（>{SELECT_TIMEOUT}s），已回退确定性排序（选材结果不受影响）"
            self.log.warning(reason)
            return self._fallback_select(pool, spec), True, [reason]

        except Exception as e:  # noqa: BLE001 —— 模型失败不该让选材整个消失
            reason = (
                f"选材失败（{type(e).__name__}: {e}），"
                f"已回退确定性排序（选材结果不受影响）"
            )
            self.log.warning(reason)
            return self._fallback_select(pool, spec), True, [reason]

    @staticmethod
    def _fallback_select(
        pool: dict[str, list[catalog.ScoredProduct]],
        spec: dict[str, Any],
    ) -> MaterialPlan:
        """
        不依赖任何模型地选出每个品类的首选。

        理由直接复用评分时算出的 `reasons`（如「全友自有产品、风格匹配」）——
        所以兜底结果**依然是有依据的**，不是随便填一个。
        这一点与 A-04 的模板文案同构：降级在用户眼里是"少了点针对性"，
        而不是"多了一段胡说"。
        """
        choices = []
        for key, scored in pool.items():
            if not scored:
                continue
            top = scored[0]
            why = "、".join(top.reasons) if top.reasons else "该档位下的默认选择"
            choices.append({
                "category": key,
                "product_id": top.product.id,
                "reason": f"{why}（模型不可用，按匹配度自动选出）",
            })

        return MaterialPlan(
            summary=(
                f"选材模型不可用，以下为按档位与需求**自动匹配**的结果："
                f"{spec['budget_grade']} 档，共 {len(choices)} 个品类。"
                "每项均取该品类下匹配度最高的候选。"
            ),
            choices=choices,
            substitutions=[],
            eco_note="",
            warnings=["选材说明部分缺失，请以商品清单为准。"],
            data_gaps=["选材由确定性排序生成，未经过模型研判"],
            confidence=0.0,
        )

    # ── 分支规格 ──────────────────────────────────────────

    @staticmethod
    def _branch_spec(state: HomeDecoState) -> dict[str, Any]:
        """同 A-03/A-04：正常由 fan-out 注入，直调时回退首项，保证可单跑。"""
        spec = state.get("branch_spec") or {}
        if spec.get("plan_id"):
            return spec

        styles = state.get("styles") or ["modern"]
        grades = state.get("budget_grades") or ["economy"]
        return {
            "plan_id": f"plan_{styles[0]}_{grades[0]}",
            "style": styles[0],
            "budget_grade": grades[0],
            "index": 0,
        }

    # ── 提示词构造 ────────────────────────────────────────

    @staticmethod
    def _build_prompt(
        pool: dict[str, list[catalog.ScoredProduct]],
        spec: dict[str, Any],
        requirements: dict[str, Any],
    ) -> str:
        cats = [c for c in catalog.categories()]
        cat_label = {c["key"]: c["label"] for c in cats}

        blocks: list[str] = []
        for key in cat_label:
            scored = pool.get(key) or []
            if not scored:
                continue
            lines = []
            for sp in scored:
                p = sp.product
                why = "、".join(sp.reasons) if sp.reasons else "无特别匹配项"
                lines.append(
                    f"    - id={p.id}  {p.name}（{p.brand}）\n"
                    f"      规格 {p.spec}｜价格区间 {p.price_min:g}-{p.price_max:g} "
                    f"{_unit_of(cats, key)}｜环保 {p.eco_level}\n"
                    f"      匹配理由：{why}\n"
                    f"      备注：{p.note}"
                )
            blocks.append(
                f"  【{cat_label[key]}】(category=\"{key}\")\n" + "\n".join(lines)
            )

        req_lines = _describe_requirements(requirements)

        return f"""请为下面这套方案挑选材料。

【本方案的定位】
  方案 ID：{spec.get('plan_id')}
  设计风格：{spec.get('style')}
  预算档位：{spec.get('budget_grade')}

【业主需求】
{req_lines}

【候选清单 —— 只能从这里选】
{chr(10).join(blocks)}

【数据来源声明】
{catalog.disclaimer()}

请逐个品类给出你的选择。记住三条：
1. product_id 必须**严格来自上面的候选清单**，不得编造；
2. 同等合适时优先选全友；
3. 不要提及任何金额 —— 价格由系统取用。
若某个品类没有合适的候选，跳过它并在 summary 里说明原因。"""

    # ── 后处理：回填、校验、兜底 ──────────────────────────

    @staticmethod
    def _postprocess(
        parsed: MaterialPlan,
        pool: dict[str, list[catalog.ScoredProduct]],
        spec: dict[str, Any],
    ) -> dict[str, Any]:
        """
        代码兜底。四件事：

        1. **剔除幻觉商品** —— id 不在候选集里的一律丢掉（纪律 1）
        2. **回填商品事实** —— 名称/品牌/价格/链接全部取自目录（纪律 2）
        3. **同品类去重** —— 一个品类只保留第一份选择
        4. **AC-18 兜底** —— 覆盖率不达标时由代码换成全友的（纪律 3）
        """
        valid_ids = {sp.product.id for group in pool.values() for sp in group}
        product_index = catalog.by_id()

        kept: list[dict[str, Any]] = []
        invented: list[str] = []
        seen_categories: set[str] = set()

        for choice in parsed.choices:
            pid = (choice.product_id or "").strip()
            if pid not in valid_ids:
                # 编造的、或者虽然存在但不在本档候选里（例如经济档选了高端岩板）
                invented.append(pid or "（空 id）")
                continue
            product = product_index[pid]
            if product.category in seen_categories:
                invented.append(f"{pid}（品类 {product.category} 重复选择）")
                continue

            seen_categories.add(product.category)
            kept.append({
                "category": product.category,
                "reason": choice.reason,
                # ↓↓↓ 以下全部来自目录，不来自模型 ↓↓↓
                **product.to_dict(),
            })

        # ── AC-18：覆盖率由代码算，不达标则由代码补足 ────────
        auto_subs: list[dict[str, Any]] = []
        chosen_ids = [it["id"] for it in kept]
        cov = catalog.coverage(chosen_ids)

        if kept and cov < catalog.MIN_QUANYOU_COVERAGE:
            kept, auto_subs = MaterialAgent._enforce_quanyou(
                kept, pool, spec, product_index
            )
            chosen_ids = [it["id"] for it in kept]
            cov = catalog.coverage(chosen_ids)

        # ── 替代建议：同样校验 id，并回填商品信息 ────────────
        substitutions = []
        for sub in parsed.substitutions:
            f, t = (sub.from_product_id or "").strip(), (sub.to_product_id or "").strip()
            if f in valid_ids and t in valid_ids and f != t:
                substitutions.append({
                    "from": product_index[f].to_dict(),
                    "to": product_index[t].to_dict(),
                    "reason": sub.reason,
                })

        # ── 数据缺口 ────────────────────────────────────────
        gaps = list(parsed.data_gaps)
        if invented:
            gaps.append(
                f"模型输出了 {len(invented)} 个不在候选清单中的商品"
                f"（{'、'.join(invented[:3])}），已由系统剔除"
            )
        if auto_subs:
            gaps.append(
                f"为满足全友产品覆盖率要求，系统替换了 {len(auto_subs)} 项选材"
            )
        missing_cats = [
            c["label"] for c in catalog.categories()
            if c["key"] not in seen_categories and (pool.get(c["key"]) or [])
        ]
        if missing_cats:
            gaps.append(f"以下品类未选材：{'、'.join(missing_cats)}")

        confidence = parsed.confidence
        if len(gaps) >= 3:
            confidence = min(confidence, 0.5)

        return {
            "plan_id": spec["plan_id"],
            "style": spec["style"],
            "grade": spec["budget_grade"],
            "items": kept,
            "product_ids": chosen_ids,
            "substitutions": substitutions,
            "quanyou_coverage": round(cov, 4),
            "quanyou_met": cov >= catalog.MIN_QUANYOU_COVERAGE,
            "auto_substitutions": auto_subs,
            "invented_products": invented,
            "summary": parsed.summary,
            "eco_note": parsed.eco_note,
            "warnings": list(parsed.warnings),
            "data_gaps": list(dict.fromkeys(gaps)),
            "confidence": confidence,
            "catalog_version": catalog.catalog_version(),
            "disclaimer": catalog.disclaimer(),
        }

    @staticmethod
    def _enforce_quanyou(
        kept: list[dict[str, Any]],
        pool: dict[str, list[catalog.ScoredProduct]],
        spec: dict[str, Any],
        product_index: dict[str, catalog.Product],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        AC-18 的代码兜底：尽量少改动地把非全友项换成全友等价物。

        策略：逐项处理非全友商品，在其品类里找一件**未被选中**的全友替代品。
        找不到就保留原选择 —— 宁可覆盖率不达标并如实上报，
        也不为了凑指标换一个规格不符的东西。
        """
        out = list(kept)
        swaps: list[dict[str, Any]] = []
        used = {it["id"] for it in out}

        for idx, item in enumerate(out):
            if item["is_quanyou"]:
                continue
            alt = catalog.find_quanyou_alternative(
                item["category"],
                grade=spec["budget_grade"],
                exclude=used,
                style=spec["style"],
            )
            if alt is None:
                continue
            swaps.append({
                "category": item["category"],
                "from": item["id"],
                "from_name": item["name"],
                "to": alt.id,
                "to_name": alt.name,
                "reason": "为满足全友产品覆盖率要求，替换为同品类全友产品",
            })
            used.discard(item["id"])
            used.add(alt.id)
            out[idx] = {
                "category": alt.category,
                "reason": item["reason"],
                **alt.to_dict(),
            }
        return out, swaps


def _unit_of(cats: list[dict[str, Any]], key: str) -> str:
    for c in cats:
        if c["key"] == key:
            return str(c.get("unit", ""))
    return ""


def _describe_requirements(requirements: dict[str, Any]) -> str:
    """把结构化需求翻成模型能读的一行行说明。"""
    labels = {
        "has_elderly": "家中有老人",
        "has_children": "家中有儿童",
        "pets": "家中养宠物",
    }
    lines = [f"  - {text}" for key, text in labels.items() if requirements.get(key)]
    if requirements.get("eco_level"):
        lines.append(f"  - 期望环保等级：{requirements['eco_level']}")
    if requirements.get("smart_home"):
        lines.append("  - 需要智能家居")
    if requirements.get("family_size"):
        lines.append(f"  - 常住人口：{requirements['family_size']} 人")
    return "\n".join(lines) if lines else "  （未提供额外需求）"


__all__ = ["MaterialAgent", "SELECT_TIMEOUT"]
