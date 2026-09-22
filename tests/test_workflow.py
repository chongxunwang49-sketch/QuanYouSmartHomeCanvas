"""
LangGraph 工作流测试（当前为「解析 → 诊断」两节点线性链路）。

重点覆盖**条件路由**：什么时候该继续诊断，什么时候该短路。
短路的两种情形都不能让下游节点抛错——错误要由上游写进 errors，
由前端从那里读，而不是让图崩在半路。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend.app.agents.layout_diagnoser import LayoutDiagnoserAgent
from backend.app.agents.layout_parser import LayoutParserAgent
from backend.app.core.llm_client import LLMResult
from backend.app.graph import workflow
from backend.app.graph.state import initial_state
from backend.app.schemas.layout import LayoutDiagnosis, LayoutSchema

# ══════════════════════════════════════════════════════════════════
# 假 LLM：按请求的 Schema 分派不同响应
# ══════════════════════════════════════════════════════════════════


LAYOUT_PAYLOAD = {
    "rooms": [
        {"name": "客厅", "type": "living_room", "area": 28.5, "bbox": [120, 80, 420, 360],
         "orientation": "south"},
        {"name": "主卧", "type": "bedroom", "area": 16.2, "bbox": [440, 80, 680, 300],
         "orientation": "south"},
    ],
    "walls": [{"type": "load_bearing", "coords": [[100, 60], [700, 60]]}],
    "windows": [{"position": [600, 50], "width": 1.8, "orientation": "south"}],
    "total_area": 89.0,
    "has_north_arrow": True,
    "confidence": 0.87,
}

DIAGNOSIS_PAYLOAD = {
    "lighting": {"score": 7.5, "issues": [], "suggestions": []},
    "ventilation": {"score": 8.0, "issues": [], "suggestions": []},
    "circulation": {"score": 6.5, "issues": [], "suggestions": []},
    "space_utilization": {"score": 7.0, "issues": [], "suggestions": []},
    "green_score": {"score": 7.8, "issues": [], "suggestions": []},
    "overall_score": 7.4,
    "summary": "格局方正，采光良好。",
    "load_bearing_warning": [],
    "data_gaps": [],
    "confidence": 0.8,
}


class _DispatchLLM:
    """
    按 schema 参数分派响应。

    一个 Agent 一个 Fake 的做法在这里不够用——工作流会把多个 Agent
    接到同一张图上，需要按 Schema 类型区分。
    """

    def __init__(self, layout: dict | None = None, diagnosis: dict | None = None,
                 fail_layout: bool = False):
        self.layout = layout if layout is not None else LAYOUT_PAYLOAD
        self.diagnosis = diagnosis if diagnosis is not None else DIAGNOSIS_PAYLOAD
        self.fail_layout = fail_layout
        self.seen: list[str] = []

    async def complete_json(self, schema, **kwargs):
        if schema is LayoutSchema:
            self.seen.append("A-01")
            if self.fail_layout:
                from backend.app.core.llm_client import LLMError

                raise LLMError("模拟主模型失败")
            return schema.model_validate(self.layout), self._res()
        if schema is LayoutDiagnosis:
            self.seen.append("A-02")
            return schema.model_validate(self.diagnosis), self._res()
        raise AssertionError(f"未预期的 Schema: {schema}")

    def _res(self) -> LLMResult:
        return LLMResult(text="{}", provider="fake", model_used="fake-model",
                         prompt_tokens=100, completion_tokens=50, elapsed_ms=10)

    async def complete(self, **kwargs):  # pragma: no cover
        raise AssertionError("工作流不应走纯文本路径")


def _state(**over) -> dict[str, Any]:
    return initial_state(
        task_id="t-wf",
        image_ref="data:image/png;base64,AAAA",
        **over,
    )


@pytest.fixture
def patched(monkeypatch):
    """把两个节点都换成假 LLM，返回 (llm, graph)。"""
    def _build(**llm_kw):
        llm = _DispatchLLM(**llm_kw)
        monkeypatch.setitem(workflow._AGENTS, "parse_layout", LayoutParserAgent(llm=llm))
        monkeypatch.setitem(workflow._AGENTS, "diagnose_layout",
                            LayoutDiagnoserAgent(llm=llm))
        return llm, workflow.build_graph(with_checkpointer=False)

    return _build


# ══════════════════════════════════════════════════════════════════
# 图结构
# ══════════════════════════════════════════════════════════════════


class TestGraphStructure:
    def test_two_agents_registered(self):
        assert set(workflow._AGENTS) == {"parse_layout", "diagnose_layout"}

    def test_compiles_without_checkpointer(self):
        assert workflow.build_graph(with_checkpointer=False) is not None

    def test_compiles_with_memory_checkpointer(self):
        from langgraph.checkpoint.memory import InMemorySaver

        g = workflow.build_graph(checkpointer=InMemorySaver())
        assert g is not None


# ══════════════════════════════════════════════════════════════════
# 正常路径
# ══════════════════════════════════════════════════════════════════


class TestTwoNodeFlow:
    def test_parse_then_diagnose(self, patched):
        """两个节点都跑，两个 Agent 都被调用。"""
        llm, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))

        assert llm.seen == ["A-01", "A-02"], f"调用顺序异常: {llm.seen}"
        assert out["layout"]["rooms"][0]["name"] == "客厅"
        assert out["diagnosis"]["overall_score"] > 0
        assert out["phase"] == "diagnosed"

    def test_trace_has_both_nodes(self, patched):
        _, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))

        assert len(out["trace"]) == 2
        assert [t["agent"] for t in out["trace"]] == ["A-01", "A-02"]
        assert all(t["ok"] for t in out["trace"])

    def test_layout_id_generated(self, patched):
        _, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))
        assert out["layout_id"].startswith("layout_")

    def test_no_errors_on_success(self, patched):
        _, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))
        assert out["errors"] == []
        assert out["degraded"] is False


# ══════════════════════════════════════════════════════════════════
# 条件路由 —— 本文件的核心
# ══════════════════════════════════════════════════════════════════


class TestRouting:
    def test_skips_diagnosis_when_parse_fails(self, patched):
        """
        解析彻底失败时短路，不让 A-02 抛错。
        A-02 需要的 layout 都不存在，跑它只会得到一个必然的异常。
        """
        llm, graph = patched(fail_layout=True)
        out = asyncio.run(graph.ainvoke(_state()))

        assert llm.seen == ["A-01"], "A-02 不应被调用"
        assert out.get("diagnosis") is None
        assert out["degraded"] is True
        assert out["errors"][0]["agent"] == "A-01"

    def test_skips_diagnosis_on_degraded_layout(self, monkeypatch):
        """
        降级解析（只有房间名）无法支撑诊断——短路，省掉一次注定失败的调用。
        对应 AC-33：degraded_basic 下诊断必须被拒。

        注意：降级结果**无法通过 Fake LLM 伪造**——`LayoutSchema` 里没有
        `mode` 字段，喂进去的 mode 会被 Pydantic 丢掉，A-01 照常标注 "full"。
        降级路径只能由 `_degraded_parse` 真实产生，所以这里直接替换节点。
        """
        from backend.app.agents.base import BaseAgent

        class _DegradedParser(BaseAgent):
            code, name = "A-01", "DegradedStub"

            async def run(self, state):
                return {
                    "layout": {
                        "mode": "degraded_basic",
                        "rooms": [{"name": "Living", "type": "other",
                                   "area": 0.0, "bbox": []}],
                        "walls": [], "doors": [], "windows": [], "total_area": 0.0,
                        "confidence": 0.4,
                    },
                    "layout_id": "layout_stub",
                    "degraded": True,
                    "degrade_reasons": ["[A-01] 主模型不可用"],
                    "phase": "degraded",
                }

        called: list[str] = []

        class _SpyDiagnoser(LayoutDiagnoserAgent):
            async def run(self, state):
                called.append("A-02")
                return await super().run(state)

        monkeypatch.setitem(workflow._AGENTS, "parse_layout", _DegradedParser())
        monkeypatch.setitem(workflow._AGENTS, "diagnose_layout", _SpyDiagnoser())

        out = asyncio.run(workflow.build_graph(with_checkpointer=False).ainvoke(_state()))

        assert called == [], "降级结果不得触发诊断"
        assert out.get("diagnosis") is None
        # 关键：不抛错，图正常结束，房间名仍然拿得到
        assert len(out["trace"]) == 1
        assert out["layout"]["rooms"][0]["name"] == "Living"
        assert out["degraded"] is True

    def test_degraded_short_circuit_produces_no_errors(self, monkeypatch):
        """短路是正常路径，不是错误路径——errors 必须干净。"""
        from backend.app.agents.base import BaseAgent

        class _DegradedParser(BaseAgent):
            code, name = "A-01", "DegradedStub"

            async def run(self, state):
                return {"layout": {"mode": "degraded_basic", "rooms": []},
                        "layout_id": "x", "degraded": True, "phase": "degraded"}

        monkeypatch.setitem(workflow._AGENTS, "parse_layout", _DegradedParser())
        out = asyncio.run(workflow.build_graph(with_checkpointer=False).ainvoke(_state()))
        assert out["errors"] == []

    def test_router_is_pure(self):
        """路由函数只做判断，不抛错、不产生副作用。"""
        assert workflow._route_after_parse({"layout": None}) == "end"
        assert workflow._route_after_parse({}) == "end"
        assert workflow._route_after_parse(
            {"layout": {"mode": "degraded_basic"}}) == "end"
        assert workflow._route_after_parse({"layout": {"mode": "full"}}) == "diagnose"


# ══════════════════════════════════════════════════════════════════
# 状态合并
# ══════════════════════════════════════════════════════════════════


class TestStateMerge:
    def test_reducers_append_not_overwrite(self, patched):
        """
        trace / errors / degrade_reasons 用 operator.add 归约，
        必须是拼接而不是覆盖——否则两个节点的记录会互相顶掉。
        """
        _, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))
        assert len(out["trace"]) == 2, "两个节点的 trace 应都保留"

    def test_degrade_reasons_accumulate(self, monkeypatch):
        """两个节点都降级时，两条原因都要留下。"""
        llm = _DispatchLLM()

        async def degraded_json(schema, **kw):
            parsed, _ = await _DispatchLLM.complete_json(llm, schema, **kw)
            return parsed, LLMResult(
                text="{}", provider="ollama", model_used="local",
                degraded=True, degrade_reason=f"{schema.__name__} 降级",
                prompt_tokens=1, completion_tokens=1, elapsed_ms=1)

        llm.complete_json = degraded_json  # type: ignore[method-assign]
        monkeypatch.setitem(workflow._AGENTS, "parse_layout", LayoutParserAgent(llm=llm))
        monkeypatch.setitem(workflow._AGENTS, "diagnose_layout",
                            LayoutDiagnoserAgent(llm=llm))

        out = asyncio.run(workflow.build_graph(with_checkpointer=False).ainvoke(_state()))
        assert len(out["degrade_reasons"]) == 2
        assert all("降级" in r for r in out["degrade_reasons"])

    def test_checkpointer_resumes_by_thread_id(self, patched):
        """挂了 checkpointer 时应能按 thread_id 取回状态（AC-12 基础）。"""
        from langgraph.checkpoint.memory import InMemorySaver

        llm = _DispatchLLM()
        workflow._AGENTS["parse_layout"] = LayoutParserAgent(llm=llm)
        workflow._AGENTS["diagnose_layout"] = LayoutDiagnoserAgent(llm=llm)

        g = workflow.build_graph(checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": "thread-1"}}
        out = asyncio.run(g.ainvoke(_state(), cfg))

        snap = g.get_state(cfg)
        assert snap.values.get("layout_id") == out["layout_id"]
