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

from backend.app.agents.budget_agent import BudgetAgent
from backend.app.agents.layout_diagnoser import LayoutDiagnoserAgent
from backend.app.agents.layout_parser import LayoutParserAgent
from backend.app.agents.material_agent import MaterialAgent
from backend.app.agents.space_planner import SpacePlannerAgent
from backend.app.core.llm_client import LLMResult
from backend.app.graph import workflow
from backend.app.graph.state import initial_state
from backend.app.schemas.budget import BudgetNarrative
from backend.app.schemas.layout import LayoutDiagnosis, LayoutSchema
from backend.app.schemas.material import MaterialPlan
from backend.app.schemas.plan import SpacePlan
from backend.app.schemas.risk import RiskReview
from backend.app.agents.risk_reviewer import RiskReviewAgent

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

NARRATIVE_PAYLOAD = {
    "summary": "经济档预算，主要花在主材与水电上。",
    "grade_rationale": "把钱省在定制柜上，改用成品柜。",
    "cost_drivers": ["主材", "水电改造"],
    "negotiation_tips": ["水电按实测结算，合同写明单价上限"],
    "saving_tips": ["成品柜替代全屋定制"],
    "warnings": ["水电易增项"],
    "confidence": 0.7,
}

#: 每个档位下**真实存在于候选池**的全友商品 id。
#: A-05 是档位敏感的：给 high 档喂 economy 的 id 会被正确地判成
#: 「不在候选清单中」而剔除 —— 那正是它该做的事，但会让这里的测试跑偏。
_MATERIAL_BY_GRADE: dict[str, list[str]] = {
    "economy": ["QY-FL-101", "QY-PT-101"],
    "medium": ["QY-FL-101", "QY-DR-102"],
    "high": ["QY-FL-102", "QY-DR-102"],
}


RISK_PAYLOAD = {
    "summary": "这份预算存在 5 类风险，最该盯住的是管理费的计费基数。",
    "findings": [
        {"risk_type": "计价陷阱", "severity": "high", "title": "管理费计费基数不对",
         "where": "管理费", "detail": "按含主材的总价计取，主材部分被重复计费。",
         "suggestion": "要求改为按施工费计取。", "source_ids": ["S1"]},
        {"risk_type": "合同条款风险", "severity": "high", "title": "水电按实结算无上限",
         "where": "水电改造", "detail": "只给单价不给总价，实际做完容易大幅超支。",
         "suggestion": "合同写明单价上限。", "source_ids": ["S1", "S9"]},
        {"risk_type": "漏项", "severity": "medium", "title": "垃圾清运推给业主",
         "where": "拆除", "detail": "垃圾外运未计费，实际是拆除的大头。",
         "suggestion": "要求列入报价。", "source_ids": ["S2"]},
        {"risk_type": "模糊计量", "severity": "medium", "title": "防水未写遍数与高度",
         "where": "防水施工", "detail": "规格不清会给后期增项留口子。",
         "suggestion": "写清遍数与淋浴区高度。", "source_ids": []},
        {"risk_type": "环保风险", "severity": "low", "title": "定制柜板材等级偏低",
         "where": "木工", "detail": "标注 E1，而需求是 E0。",
         "suggestion": "要求升级到 E0。", "source_ids": []},
    ],
    "overall_risk": "high",
    "negotiation_points": ["管理费改按施工费计取", "水电写明单价上限"],
    "data_gaps": [],
    "confidence": 0.7,
}


def _material_payload(prompt: str) -> dict:
    """按提示词里的档位，返回该档位下合法的商品选择。"""
    grade = "economy"
    for g in ("medium", "high", "economy"):
        if f"预算档位：{g}" in prompt:
            grade = g
            break

    from backend.app.services.material import catalog

    index = catalog.by_id()
    return {
        "summary": "优先选全友自有产品，环保等级以 E0 为主。",
        "choices": [
            {"category": index[pid].category, "product_id": pid,
             "reason": "全友自有产品、匹配需求"}
            for pid in _MATERIAL_BY_GRADE[grade]
        ],
        "substitutions": [],
        "eco_note": "所选材料以 E0 级为主。",
        "warnings": [],
        "data_gaps": [],
        "confidence": 0.7,
    }


class _DispatchLLM:
    """
    按 schema 参数分派响应。

    一个 Agent 一个 Fake 的做法在这里不够用——工作流会把多个 Agent
    接到同一张图上，需要按 Schema 类型区分。
    """

    def __init__(self, layout: dict | None = None, diagnosis: dict | None = None,
                 space_plan: dict | None = None, narrative: dict | None = None,
                 risk: dict | None = None, fail_layout: bool = False):
        self.layout = layout if layout is not None else LAYOUT_PAYLOAD
        self.diagnosis = diagnosis if diagnosis is not None else DIAGNOSIS_PAYLOAD
        self.space_plan = space_plan if space_plan is not None else SPACE_PLAN_PAYLOAD
        self.narrative = narrative if narrative is not None else NARRATIVE_PAYLOAD
        self.risk = risk if risk is not None else RISK_PAYLOAD
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
        if schema is BudgetNarrative:
            # 同样三分支并发。注意 A-04 的**数字**不经过这里——
            # 它只调 LLM 要这段文字，见 budget_agent.py
            self.seen.append("A-04")
            return schema.model_validate(self.narrative), self._res()
        if schema is MaterialPlan:
            # A-05 同样只让模型**指认**候选 id，价格由代码回填。
            # 这里必须按提示词里的档位给 id —— 见 _material_payload 的说明。
            self.seen.append("A-05")
            return schema.model_validate(_material_payload(kwargs.get("user") or "")), self._res()
        if schema is RiskReview:
            self.seen.append("A-06")
            return schema.model_validate(self.risk), self._res()
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
def patched(stub_knowledge, monkeypatch):
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
            ("estimate_budget", BudgetAgent),
            ("select_materials", MaterialAgent),
            ("review_risks", RiskReviewAgent),
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
            "parse_layout", "diagnose_layout",
            "generate_plan", "estimate_budget", "select_materials",
            "review_risks",
        }

    def test_产出者与审查者分开登记(self):
        """
        分支里有两类节点：**产出者**（可并行）与**审查者**（必须排在产出之后）。
        这条断言防的是有人把审查者混进 _BRANCH_PRODUCERS ——
        那样它会被 Send 并发分派，于是在预算还不存在时就去审查它。
        """
        assert workflow._REVIEW_NODE not in workflow._BRANCH_NODES, (
            "审查节点不能在并行产出者列表里 —— 它必须等产出完成后执行"
        )
        assert workflow._REVIEW_NODE in workflow._AGENTS
        # 产物列表 = 产出者的产物 + 审查者自己的产物
        assert set(workflow.BRANCH_ARTIFACTS) == {
            *workflow._BRANCH_PRODUCERS.values(), workflow._REVIEW_ARTIFACT,
        }

    def test_branch_nodes_registered(self):
        for node in workflow._BRANCH_NODES:
            assert node in workflow._AGENTS, f"分支节点 {node} 未注册"

    def test_compiles_without_checkpointer(self):
        assert workflow.build_graph(with_checkpointer=False) is not None

    def test_所有Agent写的phase都在枚举内(self):
        """
        ⚠️ AST 静态扫描，不跑任何节点。

        实测踩过：A-01 写 `parsed`、A-02 写 `diagnosed`、A-06 写 `reviewed` ——
        **三个都不在 `Phase` 枚举里**。

        而 `PHASE_TEXT.get(phase, phase)` 对枚举外的值是**原样返回**的，
        于是前端会看到英文的 "diagnosed"。更麻烦的是需求文档明确告诉前端
        「不必自己维护映射表，直接展示 phase_text」—— 也就是说，
        前端**没有任何机会**发现这个值是错的。

        这个 bug 存在了很久都没暴露，因为**直到接口层出现之前，
        没有任何东西真的消费 `phase`**。写状态而没人读，错了也不会有症状。
        """
        import ast
        from pathlib import Path

        from backend.app.core.redis_client import Phase

        allowed = set(Phase.__args__)  # type: ignore[attr-defined]
        agents_dir = Path(workflow.__file__).resolve().parents[1] / "agents"

        offenders: list[str] = []
        for py in sorted(agents_dir.glob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for k, v in zip(node.keys, node.values):
                    # 只找 {"phase": "字面量"} 这种写法
                    if not (isinstance(k, ast.Constant) and k.value == "phase"):
                        continue
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        if v.value not in allowed:
                            offenders.append(f"{py.name}: {v.value!r}")

        assert not offenders, (
            f"以下 Agent 写了 Phase 枚举外的值，前端会看到英文原文：{offenders}。"
            f"合法取值：{sorted(allowed)}"
        )

    def test_compiles_with_memory_checkpointer(self):
        from langgraph.checkpoint.memory import InMemorySaver

        g = workflow.build_graph(checkpointer=InMemorySaver())
        assert g is not None


# ══════════════════════════════════════════════════════════════════
# 正常路径
# ══════════════════════════════════════════════════════════════════


class TestFullFlow:
    def test_parse_diagnose_then_three_plans(self, patched):
        """全链路跑通：解析 1 次、诊断 1 次、每套方案 2 个 Agent × 3 套。"""
        llm, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))

        assert llm.seen[0] == "A-01", f"调用顺序异常: {llm.seen}"
        assert llm.seen[1] == "A-02", f"调用顺序异常: {llm.seen}"
        # 分支内并发，完成顺序不确定，因此比较次数而不是序列
        for code in ("A-03", "A-04", "A-05"):
            assert llm.seen.count(code) == 3, f"{code} 应被调用 3 次，实际 {llm.seen}"
        # A-06 是汇聚节点，只跑一次，一次审三套方案
        assert llm.seen.count("A-06") == 3, f"A-06 每套方案各审一次，实际 {llm.seen}"

        assert out["layout"]["rooms"][0]["name"] == "客厅"
        assert out["diagnosis"]["overall_score"] > 0
        assert len(out["plans"]) == 3
        assert out["phase"] == "finalizing"

    def test_每套方案同时含四种产物(self, patched):
        """
        三个分支 Agent 并行写同一个 plan_id，三份产物都必须留下来。
        浅合并 reducer 会把先写的那份整个顶掉 —— 详见 state.py 的 _merge_dict。
        """
        _, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))

        for plan in out["plans"]:
            assert plan["space_plan"] is not None, f"{plan['plan_id']} 缺空间规划"
            assert plan["budget"] is not None, f"{plan['plan_id']} 缺预算"
            assert plan["materials"] is not None, f"{plan['plan_id']} 缺材料"
            assert plan["risks"] is not None, f"{plan['plan_id']} 缺避坑审查"
            assert plan["missing_artifacts"] == []

    def test_全友覆盖率达标(self, patched):
        """AC-18：方案中全友产品覆盖率 ≥ 60%，由代码统计而非模型自报。"""
        _, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))

        for plan in out["plans"]:
            mt = plan["materials"]
            assert mt["quanyou_met"] is True, (
                f"{plan['plan_id']} 全友覆盖率 {mt['quanyou_coverage']:.0%} 未达 AC-18"
            )

    def test_预算由规则引擎算而非模型(self, patched):
        """AC-05 / ADR-07：预算数字必须来自 rule_engine，且分项 ≥7。"""
        _, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))

        for plan in out["plans"]:
            bg = plan["budget"]
            assert bg["computed_by"].startswith("rule_engine")
            assert len(bg["lines"]) >= 7
            assert 0 < bg["total_min"] < bg["total_max"]
            assert bg["disclaimer"], "演示数据声明必须透出"

    def test_trace_covers_all_nodes(self, patched):
        """A-01 + A-02 + (A-03+A-04+A-05) × 3 + A-06 × 1 = 12 条 trace。"""
        _, graph = patched()
        out = asyncio.run(graph.ainvoke(_state()))

        agents = [t["agent"] for t in out["trace"]]
        assert len(agents) == 12
        for code in ("A-03", "A-04", "A-05"):
            assert agents.count(code) == 3
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
        assert len(out["trace"]) == 12, "九个分支任务 + 一次审查 + 上游两条都应保留"

    def test_degrade_reasons_accumulate(self, stub_knowledge, monkeypatch):
        """
        十二个节点全部降级时，十二条原因一条都不能丢。

        ⚠️ 必须打桩知识库。否则 A-06 的检索会失败，多产出**一条"无依据"的原因**
        （那本身是正确行为），于是这里数出 13 条 —— 而这个测试要测的是
        "LLM 降级原因会不会丢"，不是"知识库挂了会怎样"。两者混在一起，
        断言就变得没法解释。
        """
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
            ("estimate_budget", BudgetAgent),
            ("select_materials", MaterialAgent),
            ("review_risks", RiskReviewAgent),
        ):
            monkeypatch.setitem(workflow._AGENTS, node, cls(llm=llm))

        out = asyncio.run(workflow.build_graph(with_checkpointer=False).ainvoke(_state()))
        # A-01 + A-02 + (A-03 + A-04 + A-05) × 3 + A-06
        assert len(out["degrade_reasons"]) == 12
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
