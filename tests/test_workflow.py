"""
LangGraph 工作流测试（解析 → 诊断 → 3 路 fan-out → fan-in）。

重点覆盖**条件路由**：什么时候该继续，什么时候该短路。
短路的两种情形都不能让下游节点抛错——错误要由上游写进 errors，
由前端从那里读，而不是让图崩在半路。

fan-out / fan-in 的语义细节（分支隔离、汇总阈值、对比表门槛）
单独放在 tests/test_fanout.py。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend.app.agents.layout_diagnoser import LayoutDiagnoserAgent
from backend.app.agents.layout_parser import LayoutParserAgent
from backend.app.agents.space_planner import SpacePlannerAgent
from backend.app.core.llm_client import LLMResult
from backend.app.graph import workflow
from backend.app.graph.state import initial_state
from backend.app.schemas.layout import LayoutDiagnosis, LayoutSchema
from backend.app.schemas.plan import SpacePlan

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

SPACE_PLAN_PAYLOAD = {
    "summary": "以家具摆放实现分区。",
    "zones": [
        {"room_name": "客厅", "function": "起居", "rationale": "面积充裕", "furniture": ["沙发"]},
        {"room_name": "主卧", "function": "睡眠", "rationale": "朝南", "furniture": ["床"]},
    ],
    "storage_plans": [{"location": "主卧", "kind": "ready_made", "note": "成品衣柜"}],
    "circulation_fixes": [],
    "key_moves": ["客餐厅一体"],
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
                 space_plan: dict | None = None, fail_layout: bool = False):
        self.layout = layout if layout is not None else LAYOUT_PAYLOAD
        self.diagnosis = diagnosis if diagnosis is not None else DIAGNOSIS_PAYLOAD
        self.space_plan = space_plan if space_plan is not None else SPACE_PLAN_PAYLOAD
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
        if schema is SpacePlan:
            # 三分支并发调用，这里会被调用 3 次
            self.seen.append("A-03")
            return schema.model_validate(self.space_plan), self._res()
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
    """
    把**所有**节点换成假 LLM，返回 (llm, graph)。

    ⚠️ 必须覆盖 `workflow._AGENTS` 里的每一个 Agent。
    漏掉任何一个，它就会拿着 import 时构造的真实 LLMClient 去打真实 API——
    而测试**照样会绿**，只是变慢、花钱、且结果依赖网络。
    conftest.py 里的绊线专门用来把这种漏网当场炸出来。
    """
    def _build(**llm_kw):
        llm = _DispatchLLM(**llm_kw)
        for node, cls in (
            ("parse_layout", LayoutParserAgent),
            ("diagnose_layout", LayoutDiagnoserAgent),
            ("generate_plan", SpacePlannerAgent),
        ):
            monkeypatch.setitem(workflow._AGENTS, node, cls(llm=llm))
        return llm, workflow.build_graph(with_checkpointer=False)

    return _build


# ══════════════════════════════════════════════════════════════════
# 图结构
# ══════════════════════════════════════════════════════════════════


class TestGraphStructure:
    def test_all_agents_registered(self):
        assert set(workflow._AGENTS) == {
            "parse_layout", "diagnose_layout", "generate_plan",
        }

    def test_compiles_without_checkpointer(self):
        assert workflow.build_graph(with_checkpointer=False) is not None

    def test_compiles_with_memory_checkpointer(self):
        from langgraph.checkpoint.memory import InMemorySaver

        g = workflow.build_graph(checkpointer=InMemorySaver())
        assert g is not None


# ══════════════════════════════════════════════════════════════════
# 正常路径
# ══════════════════════════════════════════════════════════════════


class TestFullFlow:
    def test_parse_diagnose_then_three_plans(self, patched):
        """全链路跑通：解析一次、诊断一次、方案三次。"""
        llm, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))

        assert llm.seen[0] == "A-01", f"调用顺序异常: {llm.seen}"
        assert llm.seen[1] == "A-02", f"调用顺序异常: {llm.seen}"
        # 三个分支并发，完成顺序不确定，因此比较次数而不是序列
        assert llm.seen.count("A-03") == 3, f"A-03 应被调用 3 次，实际 {llm.seen}"

        assert out["layout"]["rooms"][0]["name"] == "客厅"
        assert out["diagnosis"]["overall_score"] > 0
        assert len(out["plans"]) == 3
        assert out["phase"] == "finalizing"

    def test_trace_has_five_entries(self, patched):
        """A-01 + A-02 + A-03×3 = 5 条 trace。"""
        _, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))

        agents = [t["agent"] for t in out["trace"]]
        assert len(agents) == 5
        assert sorted(agents) == ["A-01", "A-02", "A-03", "A-03", "A-03"]
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
        必须是拼接而不是覆盖——否则并发分支的记录会互相顶掉，
        最后只剩一份，排查问题时完全看不出发生过什么。
        """
        _, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))
        assert len(out["trace"]) == 5, "三个分支的 trace 与上游两条都应保留"

    def test_degrade_reasons_accumulate(self, monkeypatch):
        """五个节点全部降级时，五条原因一条都不能丢。"""
        llm = _DispatchLLM()

        async def degraded_json(schema, **kw):
            parsed, _ = await _DispatchLLM.complete_json(llm, schema, **kw)
            return parsed, LLMResult(
                text="{}", provider="ollama", model_used="local",
                degraded=True, degrade_reason=f"{schema.__name__} 降级",
                prompt_tokens=1, completion_tokens=1, elapsed_ms=1)

        llm.complete_json = degraded_json  # type: ignore[method-assign]
        for node, cls in (
            ("parse_layout", LayoutParserAgent),
            ("diagnose_layout", LayoutDiagnoserAgent),
            ("generate_plan", SpacePlannerAgent),
        ):
            monkeypatch.setitem(workflow._AGENTS, node, cls(llm=llm))

        out = asyncio.run(workflow.build_graph(with_checkpointer=False).ainvoke(_state()))
        # A-01 + A-02 + A-03×3
        assert len(out["degrade_reasons"]) == 5
        assert all("降级" in r for r in out["degrade_reasons"])
        # 任一分支降级 => 整体降级（OrBool reducer）
        assert out["degraded"] is True

    def test_checkpointer_resumes_by_thread_id(self, patched):
        """挂了 checkpointer 时应能按 thread_id 取回状态（AC-12 基础）。"""
        from langgraph.checkpoint.memory import InMemorySaver

        llm, _ = patched()
        g = workflow.build_graph(checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": "thread-1"}}
        out = asyncio.run(g.ainvoke(_state(), cfg))

        snap = g.get_state(cfg)
        assert snap.values.get("layout_id") == out["layout_id"]
        assert len(snap.values.get("plans") or []) == 3
