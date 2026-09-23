"""
文件路径: backend/app/agents/risk_reviewer.py
模块职责: A-06 避坑审查 —— 站在业主立场质疑报价单/预算，引用知识库来源
依赖关系: 知识库（Chroma）-> 本 Agent -> 下游（fan-in 或 API 直出）
对应 Skill 文档: skills/06_risk_reviewer.md

对应需求文档:
  - 2.2.2 多 Agent 并行方案生成模块（避坑审查 Agent，"对抗式 Agent + 避坑知识库"）
  - 4.6 POST /api/v1/avoid-pit/review（报价单/合同审查）
  - PO-05：识别报价单中的增项、漏项、单价异常
  - AC-06：识别 ≥ 5 类风险，引用知识库来源
  - M4：RAG（嵌入式 Chroma）+ 避坑 Agent + 规范校验 + 环保评估

═══════════════════════════════════════════════════════════════════
本 Agent 与其他五个的根本区别：**它是审查者，不是生产者**
═══════════════════════════════════════════════════════════════════

A-01~A-05 都在"产出"（解析、诊断、规划、预算、选材），彼此可以并行 ——
它们互不依赖。A-06 不一样：**你无法审查一份还不存在的预算**。

所以 A-06 在图上是个**汇聚节点**，不是并行分支：

    generate_plan ─┐
    estimate_budget ├─→ review_risks (A-06) ─→ aggregate_plans
    select_materials┘

需求文档 3.3 的状态图画的是"4 个 Agent 并行"。那是架构草图，
真实的依赖关系是"审查在产出之后"。这一点与 ADR-08（图像节点移出并行分支）
是同一类修正：**并行的前提是互不依赖，而不是"看起来可以并行"**。

（已实测确认 LangGraph 的语义：9 个分派任务全部汇聚到一个节点时，
 该节点只执行一次，且能看到全部合并后的写入。）

═══════════════════════════════════════════════════════════════════
两种模式
═══════════════════════════════════════════════════════════════════
  quote 模式：state 里有 `quote_text` → 审查这份报价单（AC-06 主战场，
              对应 4.6 的 API 入口，用 seed_data/sample_quotes 验收）
  plan  模式：没有 quote → 逐方案审查 A-04 算出的预算分项
              （分支内跑的就是这个，产出"这套预算在实际签合同时
                哪几项最容易加钱"）

═══════════════════════════════════════════════════════════════════
引用必须可验证
═══════════════════════════════════════════════════════════════════
检索到的 chunk 被编号成 [S1][S2]…交给模型，模型只能回 `source_ids`。
代码再映射回真实出处（文件 + 章节），编造的编号被剔除并记入
`invented_citations`。这样每条风险要么能追溯到具体语料，要么明确没有来源 ——
**不会有"看起来像引用但查不到"的中间态**。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger

from ..graph.state import HomeDecoState
from ..schemas.risk import RiskReview
from ..services.knowledge import retriever
from .base import BaseAgent

#: 审查的独立超时。仍是"比节点总超时短"，让失败早于熔断发生。
#:
#: ⚠️ 实测从 24s 调上来的。原因不是模型慢，是**提示词太大** ——
#: 当时检索没封顶，一次塞了 26 条依据（top_k 的 2.6 倍），
#: 模型 24 秒内写不完十几条风险，直接超时、0 条产出。
#: 封顶依据（`RETRIEVAL_TOP_K`）是主要修复，这里同步放宽以留出余量。
#:
#: ⚠️ 2026-09-23：45s → 75s。同一个成因再犯一次 —— `LLM_MAX_TOKENS`
#:    从 8000 提到 32000 之后调用变慢，45s 又不够了。实测跑一次
#:    `/design/generate`，降级原因里写着「[A-06] 审查超时（>45.0s）」。
#:    审查是全链路里**输出最长**的一个（十几条风险，每条要带可溯源引用），
#:    所以给到三个内层超时里最大的 75s，仍低于节点层 90s。
REVIEW_TIMEOUT = 75.0

#: 一次审查检索多少条依据。太少覆盖不到多类风险，太多会稀释模型的注意力。
RETRIEVAL_TOP_K = 10

#: 兜底的主题查询。**这组词是按语料的实际结构选的** ——
#: 语料就是按"增项/漏项/单价/合同/环保"分的，用这些词召回率最高。
_TOPIC_QUERIES: tuple[str, ...] = (
    "报价单里只给单价不给总价、后期按实结算的套路",
    "施工环节偷项漏项，该报没报的项目",
    "合同定金订金、付款方式与违约条款风险",
    "管理费与各项费用的计费基数陷阱",
    "材料环保等级不足、甲醛超标的板材风险",
)


SYSTEM_PROMPT = """你是一名**站在业主立场**的装修报价审查顾问。你的工作不是把报价单讲清楚，
而是**挑出里面会坑业主的地方**——增项、漏项、单价虚高、条款陷阱。

【最重要的一条：只能引用给定的来源编号】
系统会给你一份【知识库依据】，每条前面有编号，如 [S1]、[S2]。
你判断某条风险时，把用到的编号填进 source_ids，形如 ["S1", "S3"]。

- **不要自己编造编号**，只能从给出的编号里选
- 系统会核对你的每一个编号，对不上的会被剔除
- 某条风险如果属于常识判断、没有对应依据，**source_ids 就留空数组** ——
  留空是允许的；编一个查不到的来源，比没有来源糟糕得多

【审查什么】
重点看这几类问题：
1. **增项风险**：后期会以各种名目加钱的地方
2. **漏项**：该报没报，而实际施工中必然发生的项目
3. **单价异常**：单价明显偏离市场区间
4. **计价陷阱**：计费基数或方式有问题（例如管理费按"含主材的总价"计取）
5. **模糊计量**：遍数、高度、规格约定不清，给后期增项留了口子
6. **环保风险**：材料环保等级不足
7. **合同条款风险**：定金、付款、违约等条款对业主不利
8. **营销话术**：折扣、限时优惠等制造决策压力的手段

【关于"单价异常"——必须对着参考价比，不要凭感觉】
系统如果给了【市场参考价】，判断单价异常时**必须拿报价单的单价与它比对**：
高于区间上限就是异常，并在 detail 里写明"报价 X 元，参考区间 A-B 元"。
**没有参考价可比的项，就不要报单价异常** ——
凭印象说"这个价偏高"是不可靠的，宁可不报。

【不要无中生有】
一份报价单如果确实干净，就如实说"未发现明显问题"。
**为了凑数编造风险，比漏判更伤信任** —— 业主会因为一条假警报
而不再相信其它真实的提醒。

【面向谁说话】
业主不是装修行业的人。用"倒角这20块一米，实际结账可能按两块砖算成40块"，
而不是"磨边工程量计量口径存在歧义"。**说人话，别堆术语。**

【不要算钱】
报价单上的价格是业主给的输入，你直接引用原文即可，
不要自己重新计算或估算任何金额。"""


class RiskReviewAgent(BaseAgent):
    """A-06：避坑审查。汇合节点，两种模式。"""

    code = "A-06"
    name = "RiskReviewAgent"
    requires_vision = False
    # ⚠️ 2026-09-23：55s → 90s。**不是随手放宽，是被内层超时逼上来的。**
    #    这个 Agent 有三层超时，不变量是「内层 < 节点层 < LLM 层(120s)」：
    #      REVIEW_TIMEOUT (单次调用)  <  timeout (本行)  <  LLM_TIMEOUT_SECONDS
    #    内层因为 `LLM_MAX_TOKENS` 提到 32000 已放宽到 75s
    #    （见 REVIEW_TIMEOUT 的注释），55s 会变成「内层还没到点、
    #    节点先被熔断」—— 兜底机制失效，整个审查归零。
    #    一并提到 90s，与生成侧其它 Agent 对齐。
    timeout = 90.0

    async def run(self, state: HomeDecoState) -> dict[str, Any]:
        quote = (state.get("quote_text") or "").strip()
        if quote:
            return await self._review_quote(state, quote)

        plans = state.get("plan_bundles") or {}
        if not plans:
            raise ValueError(
                "状态中既没有 quote_text 也没有 plan_bundles，A-06 无内容可审。"
                "上游 Agent 可能已失败——请检查 errors 字段"
            )
        return await self._review_plans(state, plans)

    # ══════════════════════════════════════════════════════════
    # 模式一：审查一份报价单（AC-06 主战场）
    # ══════════════════════════════════════════════════════════

    async def _review_quote(self, state: HomeDecoState, quote: str) -> dict[str, Any]:
        queries = list(_TOPIC_QUERIES) + _queries_from_quote(quote)
        result = retriever.search_many(queries, top_k_each=3,
                                       max_total=RETRIEVAL_TOP_K)

        review, degraded, reasons = await self._review(
            subject_title="报价单",
            subject_body=quote,
            sources=result,
            # ⚠️ 基准价必须给，否则"单价异常"这一类审不出来 —— 详见 _price_benchmark
            benchmark=_price_benchmark(),
        )
        payload = self._postprocess(review, result, subject_kind="quote")

        self.log.info(
            f"报价单审查完成：{len(payload['findings'])} 条风险 / "
            f"{payload['distinct_risk_types']} 类 / 整体 {payload['overall_risk']} / "
            f"依据 {payload['source_count']} 条"
            f"{'（知识库不可用）' if not result.available else ''}"
        )

        return {
            "risk_review": payload,
            "degraded": degraded,
            "degrade_reasons": (
                [f"[A-06] {r}" for r in reasons] if reasons else []
            ),
            "phase": "planning",
            "_llm_meta": getattr(self, "_last_llm_meta", None),
        }

    # ══════════════════════════════════════════════════════════
    # 模式二：逐方案审查预算（分支内）
    # ══════════════════════════════════════════════════════════

    async def _review_plans(self, state: HomeDecoState, plans: dict) -> dict[str, Any]:
        """
        对每一套方案各审一次。三套并发，总耗时 ≈ max 而非 sum。

        只审有 `budget` 的方案 —— 没有预算就没有可审的对象，
        硬审会变成让模型对着一份空表编风险。
        """
        targets = {
            pid: bundle for pid, bundle in plans.items()
            if isinstance(bundle, dict) and bundle.get("budget")
        }
        if not targets:
            raise ValueError(
                "所有方案都没有预算产出，A-06 无内容可审。"
                "A-04 可能已失败——请检查 errors 字段"
            )

        async def one(pid: str, bundle: dict):
            subject = self._budget_as_text(bundle)
            queries = [_TOPIC_QUERIES[0], _TOPIC_QUERIES[1], _TOPIC_QUERIES[3]] + \
                      _queries_from_budget(bundle.get("budget") or {})
            result = retriever.search_many(queries, top_k_each=3,
                                           max_total=RETRIEVAL_TOP_K)
            review, degraded, reasons = await self._review(
                subject_title=f"方案 {pid} 的预算",
                subject_body=subject,
                sources=result,
            )
            return pid, self._postprocess(review, result, subject_kind="plan"), degraded, reasons

        outcomes = await asyncio.gather(
            *(one(pid, b) for pid, b in targets.items()),
            return_exceptions=True,
        )

        bundles: dict[str, Any] = {}
        all_reasons: list[str] = []
        any_degraded = False
        errors: list[dict[str, Any]] = []

        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                # 单套审查失败不拖垮其余 —— 与分支内的隔离原则一致
                errors.append({
                    "agent": self.code, "type": "error",
                    "message": f"某套方案的审查失败：{type(outcome).__name__}: {outcome}",
                })
                continue
            pid, payload, degraded, reasons = outcome
            bundles[pid] = {"risks": payload}
            all_reasons.extend(reasons)
            any_degraded = any_degraded or degraded

        if not bundles:
            # ⚠️ 把逐条原因带出来。只抛一句"全部失败"会让排查变成猜谜 ——
            # 而这三条子错误恰恰是唯一能说明"为什么失败"的信息。
            detail = "；".join(
                f"{e.get('message', '')}" for e in errors
            ) or "（未捕获到具体异常）"
            raise ValueError(f"所有方案的避坑审查均失败，无可交付内容 —— {detail}")

        self.log.info(
            f"分支审查完成：{len(bundles)} 套方案，"
            f"风险数 {[len(b['risks']['findings']) for b in bundles.values()]}"
        )

        out: dict[str, Any] = {
            "plan_bundles": bundles,
            "degraded": any_degraded,
            "phase": "planning",
            "_llm_meta": getattr(self, "_last_llm_meta", None),
        }
        if all_reasons:
            out["degrade_reasons"] = [f"[A-06] {r}" for r in dict.fromkeys(all_reasons)]
        if errors:
            out["errors"] = errors
        return out

    # ══════════════════════════════════════════════════════════
    # 调用模型（两模式共用）
    # ══════════════════════════════════════════════════════════

    async def _review(
        self,
        *,
        subject_title: str,
        subject_body: str,
        sources: retriever.RetrievalResult,
        benchmark: str = "",
    ) -> tuple[RiskReview, bool, list[str]]:
        """
        交给模型审查。**知识库不可用不抛异常** —— 只是没有依据而已。

        但要把"没有依据"这件事**明确告诉模型**，否则它会以为
        【知识库依据】那一节为空是因为没有问题，转而凭常识编。
        """
        if not sources.available:
            notes = [
                f"知识库不可用（{sources.reason}），本次审查没有可引用的依据。"
                f"请只做常识范围内的判断，并在 data_gaps 中说明缺依据。"
            ]
            degraded = True
        else:
            notes = []
            degraded = False

        try:
            parsed, result = await asyncio.wait_for(
                self._call_llm(
                    system=SYSTEM_PROMPT,
                    user=self._build_prompt(subject_title, subject_body, sources,
                                            notes, benchmark),
                    schema=RiskReview,
                ),
                timeout=REVIEW_TIMEOUT,
            )
            assert isinstance(parsed, RiskReview)
            reasons = list(notes)
            if result.degrade_reason:
                reasons.append(f"审查由本地文本模型完成：{result.degrade_reason}")
            return parsed, degraded or result.degraded, reasons

        except asyncio.TimeoutError:
            reason = f"审查超时（>{REVIEW_TIMEOUT}s）"
            self.log.warning(reason)
            return _empty_review(reason), True, [reason]

        except Exception as e:  # noqa: BLE001
            reason = f"审查失败（{type(e).__name__}: {e}）"
            self.log.warning(reason)
            return _empty_review(reason), True, [reason]

    @staticmethod
    def _build_prompt(
        subject_title: str,
        subject_body: str,
        sources: retriever.RetrievalResult,
        notes: list[str],
        benchmark: str = "",
    ) -> str:
        if sources.chunks:
            blocks = []
            for i, c in enumerate(sources.chunks, 1):
                blocks.append(f"[S{i}] 出处：{c.citation}\n     {c.text}")
            source_text = "\n\n".join(blocks)
        else:
            source_text = "（本次未检索到相关知识库内容）"

        note_text = ("\n【特别说明】\n" + "\n".join(f"- {n}" for n in notes)) if notes else ""
        bench_text = f"\n【市场参考价】\n{benchmark}\n" if benchmark else ""

        return f"""请审查下面这份{subject_title}。

【知识库依据】（引用时只能使用这些编号）
{source_text}
{note_text}
{bench_text}
【待审查的{subject_title}】
{subject_body}

请逐条挑出会坑业主的地方。记住：
1. source_ids 只能取自上面的 [S1]…编号，编造的会被系统剔除；
2. 没有依据支撑的判断可以留空 source_ids，但要如实说；
3. 没发现问题就如实说没发现，**不要为凑数编造风险**。"""

    # ══════════════════════════════════════════════════════════
    # 后处理：映射引用、统计类型
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _postprocess(
        review: RiskReview,
        sources: retriever.RetrievalResult,
        *,
        subject_kind: str,
    ) -> dict[str, Any]:
        """
        三件事：

        1. **把 source_ids 映射回真实出处**，编造的编号剔除并记录
        2. 统计**风险类型数**（AC-06 的门槛是"类"，不是"条"）
        3. 按 severity 排序，让最该看的排在最前
        """
        catalog = {f"S{i}": c for i, c in enumerate(sources.chunks, 1)}
        invented: list[str] = []
        findings: list[dict[str, Any]] = []

        for f in review.findings:
            citations: list[str] = []
            # 依据原文：同一条风险可能引多个来源，把命中的片段拼起来，
            # 前端可展开查看，也便于人工核对"这条判断是不是真有出处"
            excerpts: list[str] = []

            for sid in f.source_ids:
                sid = (sid or "").strip().upper()
                chunk = catalog.get(sid)
                if chunk is None:
                    invented.append(sid)
                    continue
                if chunk.citation not in citations:
                    citations.append(chunk.citation)
                    excerpts.append(chunk.text[:300])

            findings.append({
                "risk_type": f.risk_type,
                "severity": f.severity,
                "title": f.title,
                "where": f.where,
                "detail": f.detail,
                "suggestion": f.suggestion,
                "citations": citations,
                "has_source": bool(citations),
                "source_excerpt": " … ".join(excerpts),
            })

        order = {"high": 0, "medium": 1, "low": 2}
        findings.sort(key=lambda x: order.get(x["severity"], 9))

        gaps = list(review.data_gaps)
        if invented:
            gaps.append(
                f"模型引用了 {len(invented)} 个不存在的来源编号"
                f"（{'、'.join(invented[:3])}），已由系统剔除"
            )
        if not sources.available:
            gaps.append(
                f"本次未取得知识库依据（{sources.reason}），"
                f"风险判断主要基于常识，可靠性下降"
            )
        elif not sources.chunks:
            gaps.append("知识库中没有检索到相关内容，本次审查缺少引用依据")

        no_citation = [f["title"] for f in findings if not f["has_source"]]
        if no_citation:
            gaps.append(
                f"有 {len(no_citation)} 条风险没有知识库依据"
                f"（{'、'.join(no_citation[:3])}），属于常识性提醒"
            )

        confidence = review.confidence
        if not sources.available:
            confidence = min(confidence, 0.4)
        elif len(gaps) >= 3:
            confidence = min(confidence, 0.5)

        distinct = sorted({f["risk_type"] for f in findings})

        return {
            "subject_kind": subject_kind,
            "summary": review.summary,
            "overall_risk": review.overall_risk,
            "findings": findings,
            "finding_count": len(findings),
            "distinct_risk_types": distinct,
            "distinct_type_count": len(distinct),
            # AC-06：≥5 类
            "ac06_met": len(distinct) >= 5,
            "negotiation_points": list(review.negotiation_points),
            "data_gaps": list(dict.fromkeys(gaps)),
            "confidence": confidence,
            "invented_citations": invented,
            "source_count": len(sources.chunks),
            "sources": [c.to_dict() for c in sources.chunks],
            "knowledge_available": sources.available,
            "knowledge_reason": sources.reason,
            "reranked": sources.reranked,
            "disclaimer": _DISCLAIMER,
        }

    # ══════════════════════════════════════════════════════════
    # 待审对象的文本化
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _budget_as_text(bundle: dict[str, Any]) -> str:
        """把一套方案的预算与选材摊成文本，供审查。"""
        bg = bundle.get("budget") or {}
        lines = []
        lines.append(f"预算档位：{bg.get('grade')}")
        lines.append(f"套内面积：{bg.get('area')} ㎡")
        lines.append(
            f"总价区间：{bg.get('total_min', 0):,.0f} - {bg.get('total_max', 0):,.0f} 元"
            f"（折合 {bg.get('price_per_sqm_min', 0):.0f}-{bg.get('price_per_sqm_max', 0):.0f} 元/㎡）"
        )
        lines.append("")
        lines.append("分项明细：")
        for ln in bg.get("lines") or []:
            if ln.get("basis") == "subtotal_pct":
                lines.append(
                    f"  - {ln['label']}：按施工费的 {ln.get('quantity')}%，"
                    f"{ln.get('amount_min', 0):,.0f}-{ln.get('amount_max', 0):,.0f} 元"
                    f"｜{ln.get('note', '')}"
                )
            else:
                lines.append(
                    f"  - {ln['label']}：{ln.get('quantity')} {ln.get('unit')} × "
                    f"{ln.get('unit_price_min')}-{ln.get('unit_price_max')} 元 "
                    f"= {ln.get('amount_min', 0):,.0f}-{ln.get('amount_max', 0):,.0f} 元"
                    f"｜{ln.get('note', '')}"
                )

        mt = bundle.get("materials") or {}
        if mt.get("items"):
            lines.append("")
            lines.append("选材：")
            for it in mt["items"]:
                lo, hi = (it.get("price_range") or [0, 0])[:2]
                lines.append(
                    f"  - {it.get('category')}：{it.get('name')}（{it.get('brand')}）"
                    f"{lo:g}-{hi:g} {it.get('unit', '')}｜环保 {it.get('eco_level')}"
                )
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════════════

_DISCLAIMER = (
    "本审查基于公开装修经验语料与系统内置规则，**不是法律意见，也不是施工规范审查**。"
    "涉及合同条款与结构安全的问题，请以专业人士的现场意见为准。"
)

#: 从报价单里抽分项名（markdown 表格里 `| 3-2 | 瓷砖倒角 | ... |` 那种行）
_QUOTE_ROW = re.compile(r"^\|\s*[\d\-\.]+\s*\|\s*([^|]{2,20}?)\s*\|", re.M)


def _queries_from_quote(quote: str) -> list[str]:
    """
    从报价单里抽出分项名作为补充查询。

    主题查询能覆盖通用套路，但抽不出"这份报价单特有的项目"。
    两者合起来召回更全 —— 而多几次检索的成本远低于一次漏判。
    """
    names: list[str] = []
    for m in _QUOTE_ROW.finditer(quote):
        name = m.group(1).strip()
        if name and name not in names:
            names.append(name)
    # 每个分项名配一个"会有什么坑"的后缀，让查询更贴近风险语义
    return [f"{n} 常见的报价陷阱与增项风险" for n in names[:8]]


def _queries_from_budget(budget: dict[str, Any]) -> list[str]:
    """从预算分项名派生查询。分项名（水电改造/瓦工/管理费）正是语料的话题。"""
    out = []
    for ln in budget.get("lines") or []:
        label = str(ln.get("label") or "").strip()
        if label:
            out.append(f"{label} 常见的报价陷阱与增项风险")
    return out[:8]


def _price_benchmark() -> str:
    """
    市场参考价 —— **判"单价异常"必须有基准，否则这一类审不出来。**

    ═══════════════════════════════════════════════════════════════
    这是被验收数据暴露出来的
    ═══════════════════════════════════════════════════════════════
    第一次跑 `scripts/review_sample_quote.py` 时召回 12/14，
    **漏掉的恰好是两条『单价异常』** —— 而那两条恰恰是答案里最客观的
    （锚定在 material_catalog.json 的演示区间上，高于上限即为异常）。

    原因很简单：报价单里写"圣象地板 218 元/㎡"，A-06 看到了这个数，
    但**不知道这个型号的合理区间是多少**。没有基准价，就没法判断
    218 是贵还是便宜 —— 这一类风险它再努力也审不出来。

    这不是模型能力问题，是**输入缺了**。所以修法是补输入，不是改提示词。

    价格全部来自 `material_catalog.json`，那里已声明为演示数据；
    本函数产出的文本会原样进入提示词，因此带着"演示数据"的口径。
    """
    try:
        from ..services.material import catalog

        cats = {c["key"]: c["label"] for c in catalog.categories()}
        lines = [
            "（以下为演示数据，仅用于判断报价单价是否明显偏离，非真实市场行情）"
        ]
        for p in catalog.all_products():
            lo, hi = p.price_range
            lines.append(
                f"  - {cats.get(p.category, p.category)}｜{p.brand} {p.name}"
                f"：{lo:g}-{hi:g} 元/㎡"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 —— 拿不到基准价不该让审查挂掉
        logger.warning(f"取市场参考价失败（{type(e).__name__}: {e}），本次不提供基准价")
        return ""


def _empty_review(reason: str) -> RiskReview:
    """模型不可用时的空结果。**不编内容，只如实说。**"""
    return RiskReview(
        summary="本次避坑审查未能完成，请以预算分项与合同原文为准。",
        findings=[],
        overall_risk="low",
        negotiation_points=[],
        data_gaps=[f"审查未完成：{reason}"],
        confidence=0.0,
    )


__all__ = ["RiskReviewAgent", "REVIEW_TIMEOUT", "RETRIEVAL_TOP_K"]
