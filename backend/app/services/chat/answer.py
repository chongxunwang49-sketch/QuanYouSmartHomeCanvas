"""
智友问答 —— 对话式 RAG。

═══════════════════════════════════════════════════════════════════
它凭什么能回答（依据来自四处，缺哪处就说哪处没有）
═══════════════════════════════════════════════════════════════════
需求方 2026-09-27：

> 用户可以对其进行智能体的问答服务，rag 框架下，以这一个模块所有上传的和
> 已经存在的文档为专业技能知识依据，结合前一个模块用户选择的装修方案、
> 用户的平面图信息和上传的屋子详细信息，为用户排忧解惑。

   ① **知识库**（`services/knowledge`）—— 装修避坑 / 报价审核 / 规范，走检索
   ② **当前户型**（`layout_store` 里的 rooms/walls/doors/windows + 诊断）
   ③ **屋主户型详情**（`layout["house_detail"]`，4.3′ 新增的那份文字资料）
   ④ **选定的装修方案**（plan_id 对应的方案摘要）

⚠️ **四处的可用性是逐项判断的，不是"有就用、没有就算了"。**
   哪一处没有，就在提示词里明说"这次没有该资料"，并要求模型**不要编**。
   这与项目一贯的立场一致：缺数据就说缺数据，别产出"看起来合理的错误"。

═══════════════════════════════════════════════════════════════════
为什么引用要在"会话最后"列出来，而不是正文里塞角标
═══════════════════════════════════════════════════════════════════
需求方原话：「生成的对话使用了什么数据库的文件要在会话的最后引用来源」。

做法也顺便更诚实：**正文由模型写，来源清单由代码从检索结果里取** ——
模型想引用哪条都可以，但"用了哪几份文档"这件事由我们自己的检索结果说了算，
模型编一个不存在的文件名也进不了那个清单（`citation` 是从 chunk 派生的）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from ...core.llm_client import LLMClient, LLMError
from ..knowledge import retriever

#: 一次对话往提示词里塞多少条知识。多了费 token 且会把注意力摊薄。
TOP_K_KNOWLEDGE = 6

#: 对话历史带多少轮（一轮 = 一问一答）。太长会让模型复述旧结论。
HISTORY_TURNS = 6

#: 回答的 temperature。**不是 0**：0 会让长回答反复用同一批句式，
#: 读起来像模板；但也不能高 —— 这是要给用户当依据的技术内容。
ANSWER_TEMPERATURE = 0.3

SYSTEM_PROMPT = """你是「全友·智绘家」的装修顾问助手，回答业主关于户型、装修、
预算、材料与避坑的问题。

【第一纪律：有依据才说】
下面会给你几类资料。**只依据这些资料回答**：
  · 【知识库片段】—— 装修与报价方面的专业知识，回答时优先引用它，
    并自然地提到出处（例如"按知识库里《单价陷阱与隐形消费》的说法…"）
  · 【当前户型】—— 这个户型的客观数据
  · 【屋主户型详情】—— 屋主自己填的实际信息（层高、朝向、采光、收纳、设备）
  · 【当前装修方案】—— 用户选定的那套方案的内容
⚠️ 资料里没有的，**直说"这个我不知道/资料里没有"**，不要按常识推测。
   编一个具体数字（金额、尺寸、型号）比说"不知道"糟得多 —— 业主会拿它去下单。

【第二纪律：说人话】
面向业主，不是面向设计师。用"北侧卧室采光不足，建议…"这种说法，
不要堆术语。分点作答，每点一两句，不要长篇大论。
回答长度控制在 300 字以内，除非用户明确要求详细展开。

【格式：纯文本，别写 Markdown】
界面是**按纯文本渲染**的（不会把 `**加粗**` 变成粗体，会把星号原样显示出来）。
所以：不要用 `**`、`__`、`#` 这些标记；分点就用「- 」开头，标题就用一行短句。

【第三纪律：金额与尺寸的出处】
谈到价格时，说清"这是知识库里的区间"还是"按你这套方案算出来的"。
两者口径不同，混着说会让业主以为是一个数。
"""


@dataclass
class ChatContext:
    """这一轮回答用到的全部依据。**同时用于提示词与最终的引用清单。**"""

    knowledge: list[dict[str, Any]] = field(default_factory=list)
    knowledge_available: bool = True
    knowledge_reason: str = ""
    layout_summary: str = ""
    house_detail: str = ""
    plan_summary: str = ""
    has_layout: bool = False
    has_detail: bool = False
    has_plan: bool = False

    def citations(self) -> list[dict[str, Any]]:
        """
        给界面看的来源清单。

        ⚠️ 只列**真的检索到的知识库片段**，以及那三类结构化资料是否参与了。
        没参与的不列 —— 清单要能回答"这份回答到底看了什么"。
        """
        out: list[dict[str, Any]] = []
        for c in self.knowledge:
            out.append({
                "kind": "knowledge",
                "citation": c.get("citation") or "",
                "source": c.get("source") or "",
                "headings": c.get("headings") or "",
                "doc_type": c.get("doc_type") or "",
            })
        if self.has_layout:
            out.append({"kind": "layout", "citation": "本户型的解析结果（房间/墙体/门窗/面积）"})
        if self.has_detail:
            out.append({"kind": "house_detail", "citation": "屋主提供的户型详情"})
        if self.has_plan:
            out.append({"kind": "plan", "citation": self.plan_summary.splitlines()[0]
                        if self.plan_summary else "当前选定的装修方案"})
        return out


def _layout_summary(layout: dict[str, Any]) -> str:
    rooms = layout.get("rooms") or []
    parts = [
        f"套内总面积约 {layout.get('total_area') or '未知'} ㎡",
        f"共 {len(rooms)} 个房间：" + "、".join(
            f"{r.get('name')}（{r.get('area') or '?'}㎡）" for r in rooms[:8]),
        f"门窗：{len(layout.get('windows') or [])} 扇窗 / {len(layout.get('doors') or [])} 扇门",
    ]
    diag = layout.get("diagnosis") or {}
    if diag.get("overall_score") is not None:
        parts.append(f"五维诊断综合分 {diag['overall_score']}")
    if diag.get("summary"):
        parts.append(f"诊断综述：{diag['summary']}")
    return "\n".join(f"- {p}" for p in parts)


def _plan_summary(plan: dict[str, Any] | None) -> str:
    if not plan:
        return ""
    style = plan.get("style_label") or plan.get("style") or ""
    grade = plan.get("budget_grade_label") or plan.get("budget_grade") or ""
    lines = [f"{style} · {grade}（plan_id={plan.get('plan_id')}）"]
    lo, hi = plan.get("total_budget_min"), plan.get("total_budget_max")
    if lo or hi:
        lines.append(f"- 预算区间：{lo or '?'}–{hi or '?'} 元")
    sp = plan.get("space_plan") or {}
    zones = sp.get("zones") or []
    if zones:
        lines.append("- 空间规划：" + "；".join(
            f"{z.get('room') or z.get('name') or '?'}：{z.get('summary') or z.get('usage') or ''}"
            for z in zones[:6]))
    mats = plan.get("materials") or []
    if isinstance(mats, list) and mats:
        lines.append("- 材料：" + "、".join(
            str(m.get("name") if isinstance(m, dict) else m) for m in mats[:8]))
    return "\n".join(lines)


def build_context(
    question: str,
    *,
    layout: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> ChatContext:
    """
    把四处依据凑齐。**任何一处取不到都不抛异常** —— 问答必须能降级回答，
    只是要说清"这次没看到哪份资料"。
    """
    ctx = ChatContext()

    # ① 知识库检索。**这是主依据**，失败要说出来而不是静默变空。
    try:
        res = retriever.search_many(
            [question],
            top_k_each=TOP_K_KNOWLEDGE,
            max_total=TOP_K_KNOWLEDGE,
        )
        ctx.knowledge_available = bool(res.available)
        ctx.knowledge_reason = res.reason or ""
        ctx.knowledge = [c.to_dict() for c in (res.chunks or [])]
    except Exception as e:  # noqa: BLE001
        ctx.knowledge_available = False
        ctx.knowledge_reason = f"{type(e).__name__}: {e}"
        logger.warning(f"[chat] 知识库检索异常：{e}")

    # ② 户型 / ③ 屋主详情 / ④ 方案
    if layout:
        ctx.has_layout = True
        ctx.layout_summary = _layout_summary(layout)
        detail = layout.get("house_detail") or {}
        if isinstance(detail, dict) and (detail.get("text") or "").strip():
            ctx.has_detail = True
            ctx.house_detail = (detail.get("text") or "").strip()[:6000]
    if plan:
        ctx.has_plan = True
        ctx.plan_summary = _plan_summary(plan)

    return ctx


def build_messages(question: str, ctx: ChatContext) -> str:
    """把这轮的依据拼成给模型的用户消息。**结构固定**，模型才稳定。"""
    blocks: list[str] = []

    if ctx.knowledge:
        chunks = "\n\n".join(
            f"[{i + 1}] 出处：{c.get('citation')}\n{c.get('text')}"
            for i, c in enumerate(ctx.knowledge)
        )
        blocks.append(f"【知识库片段】\n{chunks}")
    else:
        why = ctx.knowledge_reason or "没有检索到相关内容"
        blocks.append(
            f"【知识库片段】\n（本次没有可用片段：{why}）\n"
            "→ 请只依据下面其它资料回答，并说明这一部分没有知识库依据。"
        )

    blocks.append(
        f"【当前户型】\n{ctx.layout_summary or '（本次没有户型信息 —— 用户还没上传或已过期）'}"
    )
    blocks.append(
        f"【屋主户型详情】\n{ctx.house_detail or '（本次没有屋主详情 —— 层高/朝向/采光等以平面图推断为准，缺的就说缺）'}"
    )
    blocks.append(f"【当前装修方案】\n{ctx.plan_summary or '（本次没有选定方案）'}")
    blocks.append(f"【业主的问题】\n{question}")
    return "\n\n".join(blocks)


def build_history(messages: list[dict[str, Any]], *, turns: int = HISTORY_TURNS) -> list[dict[str, str]]:
    """
    取最近若干轮作为对话历史。

    ⚠️ `turns` 是**轮**不是条：一问一答两条算一轮。只按条数截会把一问一答拆开，
    模型看到"回答了但没问"的历史，容易接着自说自话。
    """
    if not messages:
        return []
    keep = max(1, turns) * 2
    tail = messages[-keep:]
    return [
        {"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")}
        for m in tail
        if m.get("role") in ("user", "assistant")
    ]


async def stream_answer(
    question: str,
    ctx: ChatContext,
    *,
    history: list[dict[str, str]] | None = None,
    client: LLMClient | None = None,
    prefer_local: bool = False,
) -> AsyncIterator[str]:
    """
    流式产出回答正文。**只吐正文** —— 引用清单由调用方从 `ctx` 取，
    两条信息分开走，免得混在一条流里解析。

    ⚠️ 模型一个字都没吐就失败时，抛 `LLMError` 让调用方如实告诉用户；
    吐了一半断掉时也抛，但调用方应当把**已经吐出来的那半截**留在界面上
    （用户看到的是"说到一半断了"，比"整段消失"更好排查）。
    """
    llm = client or LLMClient()
    user = build_messages(question, ctx)
    async for piece in llm.stream(
        user=user,
        system=SYSTEM_PROMPT,
        history=history or [],
        temperature=ANSWER_TEMPERATURE,
        agent="chat",
        force_local=prefer_local,
    ):
        yield piece


__all__ = [
    "ChatContext", "build_context", "build_messages", "build_history",
    "stream_answer", "SYSTEM_PROMPT", "TOP_K_KNOWLEDGE", "HISTORY_TURNS",
]
