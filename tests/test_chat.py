"""
智友问答（对话式 RAG）—— 服务层测试。

本文件守的是**"它看了什么、就说什么"**这条纪律：

1. 四处依据（知识库 / 户型 / 屋主详情 / 方案）**逐一判断可用性**，
   缺哪处就在提示词里明说缺，而不是静默变成空段落 ——
   静默的话模型会以为"没有相关资料"从而自由发挥，那正是最危险的失败方式。
2. 引用清单**由代码从检索结果里取**，不由模型写 ——
   模型编一个不存在的文件名也进不了这个清单。
3. 对话历史按**轮**截断，不把一问一答拆开。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.app.core.llm_client import LLMError, LLMResult
from backend.app.services.chat import answer as A


# ══════════════════════════════════════════════════════════════════
# 样本
# ══════════════════════════════════════════════════════════════════

LAYOUT = {
    "layout_id": "layout_test_chat",
    "rooms": [{"name": "客厅", "area": 28.5}, {"name": "厨房", "area": 7.2}],
    "windows": [{"width": 1.8}],
    "doors": [{"width": 0.9}, {"width": 0.8}],
    "total_area": 89.0,
    "diagnosis": {"overall_score": 7.4, "summary": "南北通透，厨房偏小。"},
    "house_detail": {"text": "层高 2.9 m，主朝向正南，南向采光面 9.6 m。", "title": "屋主自述"},
}

PLAN = {
    "plan_id": "plan_modern_economy",
    "style": "modern", "style_label": "现代简约",
    "budget_grade": "economy", "budget_grade_label": "经济",
    "total_budget_min": 80000, "total_budget_max": 120000,
    "space_plan": {"zones": [{"room": "客厅", "summary": "沙发靠东墙"}]},
    "materials": [{"name": "木纹砖"}],
}


class _FakeChunk:
    def __init__(self, text: str, citation: str) -> None:
        self._d = {"text": text, "citation": citation, "source": "zhuangxiu-skills/a.md",
                   "headings": "二、单价陷阱", "doc_type": "avoid_pit"}

    def to_dict(self) -> dict:
        return dict(self._d)


class _FakeRetrieval:
    def __init__(self, chunks, available=True, reason=""):
        self.chunks = chunks
        self.available = available
        self.reason = reason


def _patch_retriever(monkeypatch, chunks=None, available=True, reason=""):
    def fake_search_many(queries, *, top_k_each=3, doc_type=None, max_total=None):
        return _FakeRetrieval(chunks or [], available, reason)
    monkeypatch.setattr(A.retriever, "search_many", fake_search_many)


# ══════════════════════════════════════════════════════════════════
# 上下文
# ══════════════════════════════════════════════════════════════════


class TestContext:
    def test_四处依据都进提示词(self, monkeypatch):
        _patch_retriever(monkeypatch, [_FakeChunk("单价陷阱：拆项报价", "装修报价审核 / 单价陷阱 § 二、11项隐形消费")])
        ctx = A.build_context("厨房能改开放式吗", layout=LAYOUT, plan=PLAN)
        prompt = A.build_messages("厨房能改开放式吗", ctx)

        assert "单价陷阱：拆项报价" in prompt
        assert "客厅（28.5㎡）" in prompt          # 户型摘要
        assert "主朝向正南" in prompt              # 屋主详情
        assert "现代简约" in prompt and "80000" in prompt   # 方案
        assert ctx.has_layout and ctx.has_detail and ctx.has_plan

    def test_缺的资料要明说缺(self, monkeypatch):
        """⚠️ 这一条是**本文件的核心**：缺资料必须显式说出来。"""
        _patch_retriever(monkeypatch, [], available=False, reason="embedding 不可用")
        ctx = A.build_context("随便问问", layout=None, plan=None)
        prompt = A.build_messages("随便问问", ctx)

        assert "本次没有可用片段" in prompt and "embedding 不可用" in prompt
        assert "本次没有户型信息" in prompt
        assert "本次没有屋主详情" in prompt
        assert "本次没有选定方案" in prompt

    def test_知识库不可用不抛异常(self, monkeypatch):
        """问答必须能降级回答（只是说清"这次没看到知识库"）。"""
        def boom(*a, **k):
            raise RuntimeError("chroma 挂了")
        monkeypatch.setattr(A.retriever, "search_many", boom)
        ctx = A.build_context("问点什么", layout=LAYOUT, plan=None)
        assert ctx.knowledge_available is False
        # ⚠️ 2026-09-27 改：`knowledge_reason` 会**原样显示在对话页上**
        #    （「（知识库不可用：…）」），所以界面拿到的是人话；异常原文进日志。
        assert "知识库暂时不可用" in ctx.knowledge_reason
        assert "chroma 挂了" not in ctx.knowledge_reason


# ══════════════════════════════════════════════════════════════════
# 引用来源
# ══════════════════════════════════════════════════════════════════


class TestCitations:
    def test_引用来自检索结果而不是模型(self, monkeypatch):
        _patch_retriever(monkeypatch, [
            _FakeChunk("a", "装修报价审核 / 单价陷阱 § 一"),
            _FakeChunk("b", "装修环保避坑 / 板材与选择 § 三"),
        ])
        ctx = A.build_context("q", layout=LAYOUT, plan=PLAN)
        cites = ctx.citations()

        kinds = [c["kind"] for c in cites]
        assert kinds[:2] == ["knowledge", "knowledge"]
        assert "layout" in kinds and "house_detail" in kinds and "plan" in kinds
        assert cites[0]["citation"] == "装修报价审核 / 单价陷阱 § 一"

    def test_没参与的资料不进清单(self, monkeypatch):
        """只带了户型时，清单里不该出现"方案"这条 —— 清单要能回答"看了什么"。"""
        _patch_retriever(monkeypatch, [])
        ctx = A.build_context("q", layout=LAYOUT, plan=None)
        kinds = [c["kind"] for c in ctx.citations()]
        assert "layout" in kinds and "plan" not in kinds


# ══════════════════════════════════════════════════════════════════
# 历史
# ══════════════════════════════════════════════════════════════════


class TestHistory:
    def test_按轮截断不拆开一问一答(self):
        msgs = []
        for i in range(10):
            msgs.append({"role": "user", "content": f"问 {i}"})
            msgs.append({"role": "assistant", "content": f"答 {i}"})
        h = A.build_history(msgs, turns=2)
        assert len(h) == 4
        assert h[0]["role"] == "user" and h[-1]["role"] == "assistant"
        assert h[-2]["content"] == "问 9"

    def test_丢掉非对话角色(self):
        h = A.build_history([
            {"role": "system", "content": "不该出现"},
            {"role": "user", "content": "问"},
            {"role": "tool", "content": "也不该出现"},
            {"role": "assistant", "content": "答"},
        ])
        assert [m["role"] for m in h] == ["user", "assistant"]


# ══════════════════════════════════════════════════════════════════
# 流式
# ══════════════════════════════════════════════════════════════════


class _FakeStreamClient:
    """假客户端：按脚本吐字，并可指定"吐到第几块时炸"。"""

    def __init__(self, pieces: list[str], fail_after: int | None = None,
                 raise_first: Exception | None = None):
        self.pieces = pieces
        self.fail_after = fail_after
        self.raise_first = raise_first
        self.saw: dict | None = None

    async def stream(self, **kwargs):
        self.saw = kwargs
        if self.raise_first:
            raise self.raise_first
        for i, p in enumerate(self.pieces):
            yield p
            # ⚠️ 判据在 **yield 之后**：要模拟的是"吐了几块之后断掉"，
            #    放在前面的话，最后一块永远不会触发（第一版就踩了这个坑，
            #    测试报 DID NOT RAISE）。
            if self.fail_after is not None and i == self.fail_after:
                raise LLMError("半路断了")


def _collect(agen) -> str:
    async def run():
        out = []
        async for x in agen:
            out.append(x)
        return "".join(out)
    return asyncio.run(run())


class TestStreaming:
    def test_逐块吐出来(self, monkeypatch):
        _patch_retriever(monkeypatch, [])
        ctx = A.build_context("q", layout=LAYOUT, plan=None)
        client = _FakeStreamClient(["厨房", "改开放式", "要看承重墙。"])
        got = _collect(A.stream_answer("厨房能改开放式吗", ctx, client=client))
        assert got == "厨房改开放式要看承重墙。"
        # 提示词里确实带了依据
        assert "层高 2.9" in client.saw["user"]
        assert client.saw["system"] == A.SYSTEM_PROMPT

    def test_半路断了要抛出去(self, monkeypatch):
        """调用方需要知道"说到一半断了"，好把已有半截留在界面上。"""
        _patch_retriever(monkeypatch, [])
        ctx = A.build_context("q", layout=LAYOUT, plan=None)
        client = _FakeStreamClient(["前半段", "后半段"], fail_after=0)
        with pytest.raises(LLMError):
            _collect(A.stream_answer("q", ctx, client=client))

    def test_一个字都没吐就失败也抛(self, monkeypatch):
        _patch_retriever(monkeypatch, [])
        ctx = A.build_context("q", layout=None, plan=None)
        client = _FakeStreamClient([], raise_first=LLMError("所有提供方均失败"))
        with pytest.raises(LLMError):
            _collect(A.stream_answer("q", ctx, client=client))


class TestSystemPromptDiscipline:
    """提示词里的三条纪律不能被人顺手删掉（它们直接决定回答可不可信）。"""

    def test_三条纪律都在(self):
        p = A.SYSTEM_PROMPT
        assert "只依据这些资料回答" in p
        assert "不知道" in p and "不要" in p
        assert "别编" in p or "编一个具体数字" in p
