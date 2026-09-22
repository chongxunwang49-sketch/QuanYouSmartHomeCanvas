"""
文件路径: backend/app/agents/space_planner.py
模块职责: A-03 空间规划 —— 功能分区 / 动线优化 / 收纳设计
依赖关系: A-01（layout）+ A-02（diagnosis）-> 本 Agent -> A-04 预算（规则引擎）
对应 Skill 文档: skills/03_space_planner.md
上游: A-00 MasterOrchestrator（经 fan-out 注入 branch_spec）
下游: A-04 BudgetAgent（同分支内并行）、fan_in_aggregate

对应需求文档:
  - 2.2.2 多 Agent 并行方案生成模块（空间规划 Agent）
  - 3.3 LangGraph 状态图（generate_plan_* 分支内）
  - AC-04：输出 3 套可选方案
  - AC-05：方案含功能分区、动线、收纳三部分

═══════════════════════════════════════════════════════════════════
本 Agent 的两个关键设计
═══════════════════════════════════════════════════════════════════

【设计 1】幻觉房间必须被代码拦下，不能只靠提示词
    模型的训练语料里堆满了"标准三室两厅"的样板方案。给它一个两居室，
    它仍然会写出「儿童房」「书房」——而且写得很像那么回事。

    这与 A-02 的"编造评分"同源：**模型在被要求产出结构化内容时，
    倾向于先满足格式、再考虑事实**。提示词里写"不要编造"能减少但不能消除。

    因此 zones 里凡是对不上 layout.rooms 的房间，一律**剔除**并记入
    `invented_rooms`。剔除而不是保留，是因为一个不存在的房间无法落地施工；
    记录而不是静默丢弃，是因为这是要暴露给开发者的质量问题信号。

【设计 2】本 Agent 不碰任何价格
    预算由 A-04 的规则引擎算（ADR-07）。SpacePlan 里没有金额字段，
    提示词里也明确禁止给价——**用类型系统固化职责边界**，
    比在文档里写一句"注意不要报价"可靠得多。
"""

from __future__ import annotations

import re
from typing import Any

from ..core.capabilities import OperationNotAllowedError, check_operation
from ..graph.state import HomeDecoState
from ..schemas.plan import SpacePlan
from .base import BaseAgent

# ══════════════════════════════════════════════════════════════════
# 风格与预算档的做法提示
# ══════════════════════════════════════════════════════════════════
#
# 这三套方案如果只是换个形容词，那"多方案对比"就是假的。
# 下面两个表让风格与预算档**真正改变空间做法**，而不只是改变措辞。

_STYLE_HINTS: dict[str, str] = {
    "modern": "现代简约：线条干净，减少装饰性造型；家具低矮、留白多，视觉上放大空间；"
              "色彩以白/灰/木色为主。",
    "nordic": "北欧/奶油风：浅色木质为主，注重自然采光；布艺与圆角家具，氛围柔和；"
              "可用浅色乳胶漆配合开放式布局。",
    "chinese": "现代中式/侘寂：木作与留白并重，讲究对称与秩序；家具数量少而精，"
               "重视材质的原始质感；可设置茶区或阅读角。",
    "cream": "奶油风：低饱和暖色系，弧形造型与软质材料；收纳尽量隐蔽，表面少物品。",
    "japandi": "日式侘寂+北欧：极简、自然材质，强调收纳的隐藏性与功能的克制。",
    "industrial": "工业风：裸露材质与金属元素，开放式布局，收纳偏重展示而非隐藏。",
}

_GRADE_HINTS: dict[str, str] = {
    "economy": "经济档（¥800-1200/㎡）：**优先不动结构、不砌墙、不吊顶**，"
               "以家具摆放和软装实现分区；收纳以**成品柜**为主，避免全屋定制；"
               "能用现成柜体解决的，不做嵌入式。",
    "medium": "中档（¥1500-2000/㎡）：可做局部拆改与定制柜，重点空间（玄关、主卧）"
              "用定制，次要空间用成品；可考虑局部吊顶与灯光设计。",
    "high": "高端（¥2500-3500/㎡）：可做全屋定制与系统性收纳规划，"
            "支持结构改造（**涉及墙体时必须先经专业人员复核**）、"
            "全屋灯光与智能家居预埋。",
}


def _norm(name: str) -> str:
    """
    房间名归一化，用于比对。

    模型不会严格照抄："客厅(Living Room)" / "客 厅" / "主卧室" 都可能出现。
    这些是**表述差异**而非凭空编造，不该被当成幻觉剔除——
    否则每份方案都会误报，`invented_rooms` 就失去信号价值了。
    """
    return re.sub(r"[\s\-_·、,，.。()（）\[\]【】/]+", "", name or "").lower()


def _match_room(candidate: str, known: list[str]) -> str | None:
    """
    在已知房间名里找 candidate 的对应项，返回**户型数据里的原名**。

    匹配顺序：完全相等 -> 去噪后相等 -> 一方包含另一方。
    最后一条兜住「主卧」vs「主卧室」这类情况，但要求较短一方 ≥2 字，
    避免「房」这种单字把一堆房间误配到一起。
    """
    if not candidate:
        return None
    cn = _norm(candidate)
    if not cn:
        return None
    for name in known:
        if name == candidate:
            return name
    for name in known:
        if _norm(name) == cn:
            return name
    for name in known:
        nn = _norm(name)
        short, long_ = (nn, cn) if len(nn) <= len(cn) else (cn, nn)
        if len(short) >= 2 and short in long_:
            return name
    return None


SYSTEM_PROMPT = """你是一名住宅空间规划师，负责把一个已知户型安排成一个具体的居住方案。
用户会给你**结构化的户型数据**和**户型诊断结论**，你要基于它们做空间规划。

【最重要的纪律：只能安排真实存在的房间】
你的 zones 里出现的每一个 room_name，**必须是户型数据 rooms 列表里已有的名字，原样照抄**。
- 户型里只有「次卧」，就不要写「儿童房」——功能写在 function 字段，不是改房间名。
- 户型里没有的房间（玄关、书房、储物间），**一律不要出现在 zones 里**。
- 户型数据里的每一个房间都必须被安排到，不要遗漏。

系统会用代码逐一比对你的输出与户型数据，对不上的会被剔除。
**编造房间不会让方案更好，只会让它不可用。**

【你的职责边界】
你负责：功能分区、动线优化、收纳设计。
你**不负责**：报价、算钱、推荐品牌。方案里不要出现任何金额、单价、预算数字——
那是预算规则的职责，你给了也会被覆盖。

【诊断结论怎么用】
用户会给你 A-02 的诊断结果。其中的 issues 是已经发现的户型缺陷，你的
circulation_fixes **应该是针对这些已知问题的回应**，不要自行发明新的问题。
若诊断说某项数据不足（insufficient_data），你只能做保守安排，并写进 data_gaps。

【风格与预算档必须真正影响做法】
不同的风格和预算档要产出**实质不同**的方案，而不是换形容词：
- 经济档应避免结构改造与全屋定制，多用成品柜与软装分区；
- 高端档才考虑系统性收纳、结构改造与全屋预埋；
- 风格影响家具形态、材质与色彩，也影响空间是"开放"还是"分隔"。

若三套方案读起来只是措辞不同，那这套多方案对比就没有意义。"""


class SpacePlannerAgent(BaseAgent):
    """A-03：户型 + 诊断 + 分支规格 → 一套空间规划方案。"""

    code = "A-03"
    name = "SpacePlannerAgent"
    requires_vision = False
    timeout = 30.0

    async def run(self, state: HomeDecoState) -> dict[str, Any]:
        layout = state.get("layout")
        if not layout:
            raise ValueError(
                "状态中缺少 layout，A-03 无法执行。"
                "上游 A-01 可能已失败——请检查 errors 字段"
            )

        # ══ 业务连续性守卫 ══════════════════════════════════════
        # 用 generate_plan 而非 diagnose：规划的判据更严
        # （还要求墙体信息，因为承重墙直接决定哪些改造能提）。
        # 这两级门槛在 capabilities.py 里早就分好了，此处直接引用，
        # 不在 Agent 里另立一套标准。
        cap = check_operation(layout, "generate_plan")
        if not cap.allowed:
            raise OperationNotAllowedError("generate_plan", cap)

        spec = self._branch_spec(state)
        diagnosis = state.get("diagnosis")
        user_prompt = self._build_prompt(layout, diagnosis, spec)

        parsed, llm_result = await self._call_llm(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            schema=SpacePlan,
        )
        assert isinstance(parsed, SpacePlan)

        payload = self._postprocess(parsed, layout, spec, diagnosis)

        self.log.info(
            f"方案 {spec['plan_id']} 完成 "
            f"zones={len(payload['zones'])}/{len(layout.get('rooms') or [])} "
            f"未安排={len(payload['unassigned_rooms'])} "
            f"幻觉剔除={len(payload['invented_rooms'])}"
        )

        return {
            # 分支产出挂到 plan_bundles[plan_id]，由 fan-in 汇总。
            # 用 MergeDict 归约：三个分支各写自己的键，互不覆盖。
            "plan_bundles": {spec["plan_id"]: {"space_plan": payload}},
            "degraded": llm_result.degraded,
            "degrade_reasons": (
                [f"[A-03/{spec['plan_id']}] 方案由本地文本模型完成：{llm_result.degrade_reason}"]
                if llm_result.degrade_reason else []
            ),
            "phase": "planning",
            "_llm_meta": self._last_llm_meta,
        }

    # ── 分支规格 ──────────────────────────────────────────

    @staticmethod
    def _branch_spec(state: HomeDecoState) -> dict[str, Any]:
        """
        取本分支的规格（风格 + 预算档 + plan_id）。

        正常情况下由 fan-out 的 Send 注入。直接调用（测试、单跑）时
        回退到 styles/budget_grades 的第一项，保证 Agent 可独立执行——
        这与 A-01/A-02 能被单独 invoke 的设计保持一致。
        """
        spec = state.get("branch_spec") or {}
        if spec.get("plan_id"):
            return spec

        styles = state.get("styles") or ["modern"]
        grades = state.get("budget_grades") or ["economy"]
        style, grade = styles[0], grades[0]
        return {
            "plan_id": f"plan_{style}_{grade}",
            "style": style,
            "budget_grade": grade,
            "index": 0,
        }

    # ── 提示词构造 ────────────────────────────────────────

    @staticmethod
    def _build_prompt(
        layout: dict[str, Any],
        diagnosis: dict[str, Any] | None,
        spec: dict[str, Any],
    ) -> str:
        rooms = layout.get("rooms") or []
        room_lines = []
        for r in rooms:
            area = r.get("area") or 0
            area_txt = f"{area}㎡" if area > 0 else "面积未知"
            room_lines.append(
                f"  - 「{r.get('name', '未命名')}」"
                f"（{r.get('type', 'other')}）{area_txt}，朝向：{r.get('orientation', 'unknown')}"
            )

        # 诊断里的问题清单——动线优化要针对这些，而不是凭空发明
        issues: list[str] = []
        if diagnosis:
            for dim in ("lighting", "ventilation", "circulation",
                        "space_utilization", "green_score"):
                item = diagnosis.get(dim) or {}
                if item.get("insufficient_data"):
                    issues.append(f"  - [{dim}] 数据不足，无法评估")
                    continue
                for iss in item.get("issues") or []:
                    issues.append(f"  - [{dim}] {iss}")

        gaps = (diagnosis or {}).get("data_gaps") or []

        # 「诊断跑了但没问题」与「诊断压根没跑成」必须区分开。
        # 若混为一谈，模型会把"没有诊断"理解成"户型没缺陷"，
        # 从而给出比实际更乐观的方案——这是 A-02 insufficient_data 的同一个原则。
        if diagnosis is None:
            diagnosis_block = "  （诊断未能生成——上游 A-02 执行失败。请勿据此认为户型没有缺陷，\n" \
                              "    动线优化部分请保守处理，并在 data_gaps 中说明缺少诊断依据）"
        elif issues:
            diagnosis_block = chr(10).join(issues)
        else:
            diagnosis_block = "  （诊断已完成，未发现明显问题）"

        style = spec.get("style", "modern")
        grade = spec.get("budget_grade", "economy")

        return f"""请为以下户型生成一套空间规划方案。

【本方案的定位】
  方案 ID：{spec.get('plan_id')}
  设计风格：{style} —— {_STYLE_HINTS.get(style, '按该风格的通行做法处理')}
  预算档位：{grade} —— {_GRADE_HINTS.get(grade, '按中档做法处理')}

【户型数据】
套内总面积：{layout.get('total_area') or 0}㎡
房间列表（**zones 只能使用下列房间名**）：
{chr(10).join(room_lines) if room_lines else '  （无房间数据）'}

窗户数量：{len(layout.get('windows') or [])}
门数量：{len(layout.get('doors') or [])}
入户朝向：{layout.get('entrance_orientation', 'unknown')}

【户型诊断结论】
{diagnosis_block}

诊断指出的数据缺口：
{(chr(10).join('  - ' + g for g in gaps) if gaps else '  （无）')}

请基于以上信息，给出这套方案的功能分区、动线优化与收纳设计。
**再次强调**：zones 的 room_name 只能用上面列出的房间名，一个都不要新造；
每个房间都要安排到；方案里不要出现任何金额。"""

    # ── 后处理 ────────────────────────────────────────────

    @staticmethod
    def _postprocess(
        parsed: SpacePlan,
        layout: dict[str, Any],
        spec: dict[str, Any],
        diagnosis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        代码兜底。三件事：

        1. **剔除幻觉房间**（见模块头「设计 1」）—— 只保留户型里真实存在的房间
        2. **补记遗漏房间** —— 模型漏安排哪个房间，代码补进 unassigned_rooms
        3. **plan_id/style/grade 由代码写入** —— 不采信模型自报的身份，
           否则三个分支可能给自己起同一个名字，fan-in 汇总时会互相覆盖
        """
        payload = parsed.model_dump()

        known_rooms = [r.get("name", "") for r in (layout.get("rooms") or [])]
        known_rooms = [n for n in known_rooms if n]

        # ── 1. 房间对齐 ────────────────────────────────────
        kept_zones: list[dict[str, Any]] = []
        invented: list[str] = []
        covered: set[str] = set()

        for zone in payload["zones"]:
            canonical = _match_room(zone.get("room_name", ""), known_rooms)
            if canonical is None:
                # 对不上 = 模型编的房间（或写了个完全无关的名字）。
                # 剔除而不是留下：不存在的房间无法落地。
                invented.append(zone.get("room_name", ""))
                continue
            if canonical in covered:
                # 同一个房间被安排了两次 —— 保留第一份，后者视为重复
                invented.append(f"{zone.get('room_name')}（重复安排）")
                continue
            zone["room_name"] = canonical      # 统一成户型数据里的原名
            covered.add(canonical)
            kept_zones.append(zone)

        # ── 2. 遗漏房间 ────────────────────────────────────
        unassigned = [n for n in known_rooms if n not in covered]

        payload["zones"] = kept_zones

        # ── 3. 动线优化的 target_rooms 同样过滤 ────────────
        for fix in payload["circulation_fixes"]:
            fix["target_rooms"] = [
                m for m in (_match_room(t, known_rooms) for t in fix.get("target_rooms") or [])
                if m
            ]

        # ── 4. 身份字段由代码写入 ──────────────────────────
        payload["plan_id"] = spec["plan_id"]
        payload["style"] = spec["style"]
        payload["budget_grade"] = spec["budget_grade"]
        payload["plan_index"] = spec.get("index", 0)

        payload["invented_rooms"] = invented
        payload["unassigned_rooms"] = unassigned

        # ── 5. 数据缺口兜底 + 置信度联动 ───────────────────
        gaps = list(payload["data_gaps"])
        if invented:
            gaps.append(
                f"模型输出了 {len(invented)} 个户型中不存在的房间"
                f"（{'、'.join(invented[:3])}），已由系统剔除"
            )
        if unassigned:
            gaps.append(f"有 {len(unassigned)} 个房间未获功能安排：{'、'.join(unassigned)}")
        if not (layout.get("windows") or []):
            gaps.append("数据中未识别到窗户，采光相关安排（如开放式布局）缺乏依据")
        payload["data_gaps"] = list(dict.fromkeys(gaps))

        if len(gaps) >= 3:
            payload["confidence"] = min(payload["confidence"], 0.5)

        payload["based_on"] = {
            "rooms_total": len(known_rooms),
            "rooms_planned": len(kept_zones),
            "diagnosis_available": diagnosis is not None,
        }
        return payload


__all__ = ["SpacePlannerAgent"]
