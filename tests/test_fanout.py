"""
fan-out / fan-in 语义测试。

本文件守的是「多分支编排」本身，而不是某个 Agent 的业务逻辑。
三组命题：

1. **分支是并发的，不是串行的** —— 总耗时 = max(分支耗时)，不是 sum。
   这是 fan-out 存在的全部理由，所以必须有测试真的去量时间。
2. **单个分支失败不拖垮整体** —— 3 套里出 2 套是可用结果，0 套才是失败。
3. **Send 的 payload 必须自带分支所需的一切** —— 因为 payload 会**替换**
   节点看到的状态，不是合并（实测结论，见 workflow.py 文件头）。
   这条最容易被误改，且改坏了要等运行到 Agent 里才报 KeyError。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.app.agents.layout_diagnoser import LayoutDiagnoserAgent
from backend.app.agents.layout_parser import LayoutParserAgent
from backend.app.agents.space_planner import SpacePlannerAgent
from backend.app.core.llm_client import LLMError, LLMResult
from backend.app.graph import workflow
from backend.app.graph.state import initial_state
from backend.app.schemas.layout import LayoutDiagnosis, LayoutSchema
from backend.app.schemas.plan import SpacePlan

# ══════════════════════════════════════════════════════════════════
# 样本
# ══════════════════════════════════════════════════════════════════

LAYOUT = {
    "layout_id": "layout_fanout",
    "mode": "full",
    "rooms": [
        {"name": "客厅", "type": "living_room", "area": 28.5, "bbox": [120, 80, 420, 360],
         "orientation": "south"},
        {"name": "主卧", "type": "bedroom", "area": 16.2, "bbox": [440, 80, 680, 300],
         "orientation": "south"},
    ],
    "walls": [{"type": "load_bearing", "coords": [[100, 60], [700, 60]]}],
    "doors": [{"position": [250, 380], "width": 0.9}],
    "windows": [{"position": [600, 50], "width": 1.8, "orientation": "south"}],
    "total_area": 44.7,
    "has_north_arrow": True,
    "confidence": 0.85,
}

DIAGNOSIS = {
    "lighting": {"score": 7.0, "insufficient_data": False, "issues": [], "suggestions": []},
    "ventilation": {"score": 7.0, "insufficient_data": False, "issues": [], "suggestions": []},
    "circulation": {"score": 6.0, "insufficient_data": False, "issues": [], "suggestions": []},
    "space_utilization": {"score": 7.0, "insufficient_data": False, "issues": [], "suggestions": []},
    "green_score": {"score": 7.0, "insufficient_data": False, "issues": [], "suggestions": []},
    "overall_score": 6.8,
    "summary": "格局方正。",
    "data_gaps": [],
    "confidence": 0.8,
}

SPACE_PLAN = {
    "summary": "方案思路。",
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


class _FanoutLLM:
    """
    可控的假 LLM。

    `delay` 用来验证并发性；`fail_on` 用来模拟单个分支失败——
    A-03 会把 plan_id 写进用户提示词，因此这里靠提示词内容区分是哪个分支。
    """

    def __init__(self, *, delay: float = 0.0, fail_on: str | None = None,
                 fail_all_plans: bool = False, layout: dict | None = None):
        self.delay = delay
        self.fail_on = fail_on
        self.fail_all_plans = fail_all_plans
        #: 覆盖 A-01 的产出。要让布局"缺东西"，必须改这里而不是 state——
        #: A-01 跑完会用它的产出整体覆盖 state 里的 layout。
        self.layout = layout if layout is not None else LAYOUT
        self.plan_calls: list[str] = []

    async def complete_json(self, schema, **kwargs):
        user = kwargs.get("user") or ""

        if schema is LayoutSchema:
            return schema.model_validate(self.layout), self._res()
        if schema is LayoutDiagnosis:
            return schema.model_validate(DIAGNOSIS), self._res()
        if schema is SpacePlan:
            if self.delay:
                await asyncio.sleep(self.delay)
            # 提示词里带着「方案 ID：plan_xxx」，据此识别分支
            for token in ("plan_modern_economy", "plan_nordic_medium", "plan_chinese_high"):
                if token in user:
                    self.plan_calls.append(token)
                    if self.fail_all_plans or token == self.fail_on:
                        raise LLMError(f"模拟分支 {token} 失败")
                    break
            return schema.model_validate(SPACE_PLAN), self._res()

        raise AssertionError(f"未预期的 Schema: {schema}")

    def _res(self) -> LLMResult:
        return LLMResult(text="{}", provider="fake", model_used="fake-model",
                         prompt_tokens=100, completion_tokens=50, elapsed_ms=1)

    async def complete(self, **kwargs):  # pragma: no cover
        raise AssertionError("不应走纯文本路径")


def _patch_all(monkeypatch, llm):
    for node, cls in (
        ("parse_layout", LayoutParserAgent),
        ("diagnose_layout", LayoutDiagnoserAgent),
        ("generate_plan", SpacePlannerAgent),
    ):
        monkeypatch.setitem(workflow._AGENTS, node, cls(llm=llm))


def _state(**over) -> dict:
    return initial_state(task_id="t-fanout", image_ref="data:image/png;base64,AAAA", **over)


def _run(monkeypatch, llm, **state_kw) -> dict:
    _patch_all(monkeypatch, llm)
    graph = workflow.build_graph(with_checkpointer=False)
    return asyncio.run(graph.ainvoke(_state(**state_kw)))


# ══════════════════════════════════════════════════════════════════
# 分支规格
# ══════════════════════════════════════════════════════════════════


class TestBranchSpecs:
    def test_默认三套方案(self):
        specs = workflow.build_branch_specs({})
        assert [s["plan_id"] for s in specs] == [
            "plan_modern_economy", "plan_nordic_medium", "plan_chinese_high",
        ]
        assert [s["index"] for s in specs] == [0, 1, 2]

    def test_按位置配对(self):
        specs = workflow.build_branch_specs(
            {"styles": ["cream", "japandi"], "budget_grades": ["high", "economy"]})
        assert [(s["style"], s["budget_grade"]) for s in specs] == [
            ("cream", "high"), ("japandi", "economy"),
        ]

    def test_plan_id确定性(self):
        """同样的输入必须得到同样的 plan_id——它是 plan_bundles 的键。"""
        a = workflow.build_branch_specs({"styles": ["modern"], "budget_grades": ["economy"]})
        b = workflow.build_branch_specs({"styles": ["modern"], "budget_grades": ["economy"]})
        assert a == b

    def test_超过上限被截断(self):
        """请求 20 套会并发 20 路，每路后面还要接 4 个 Agent——必须设上限。"""
        specs = workflow.build_branch_specs({
            "styles": [f"s{i}" for i in range(20)],
            "budget_grades": ["economy"] * 20,
        })
        assert len(specs) == workflow.MAX_PLAN_BRANCHES

    def test_长度不一致按短的截断(self):
        specs = workflow.build_branch_specs(
            {"styles": ["a", "b", "c"], "budget_grades": ["high"]})
        assert len(specs) == 1

    def test_空输入回退默认(self):
        assert len(workflow.build_branch_specs({"styles": [], "budget_grades": []})) == 3


# ══════════════════════════════════════════════════════════════════
# Send payload —— 防回归
# ══════════════════════════════════════════════════════════════════


class TestSendPayload:
    def test_payload自带layout与diagnosis(self):
        """
        ⚠️ 回归防线。

        Send 的 payload 会**替换**节点看到的状态，不是合并。
        因此 layout / diagnosis 不塞进 payload，A-03 里 `state.get("layout")`
        就是 None，三个分支会一起抛"状态中缺少 layout"。

        这个坑很隐蔽：官方 map-reduce 示例里节点只用 payload 里的字段，
        所以看不出差别，照抄示例就会中招。
        """
        sends = workflow._fan_out_plans(_state(
            layout=LAYOUT, diagnosis=DIAGNOSIS,
            styles=["modern"], budget_grades=["economy"]))

        assert len(sends) == 1
        send = sends[0]
        assert send.node == "generate_plan"
        assert send.arg["layout"] == LAYOUT
        assert send.arg["diagnosis"] == DIAGNOSIS
        assert send.arg["branch_spec"]["plan_id"] == "plan_modern_economy"

    def test_payload不带无关字段(self):
        """
        payload 会随 checkpointer 序列化落盘，塞整份 state 会白白放大 N 倍。
        这里反向确认：图像 base64 这类大字段不该出现在分支 payload 里。
        """
        sends = workflow._fan_out_plans(_state(
            layout=LAYOUT, diagnosis=DIAGNOSIS,
            styles=["modern"], budget_grades=["economy"]))

        assert "image_ref" not in sends[0].arg


# ══════════════════════════════════════════════════════════════════
# 并发性 —— fan-out 存在的理由
# ══════════════════════════════════════════════════════════════════


class TestConcurrency:
    def test_三分支并发而非串行(self, monkeypatch):
        """
        总耗时 = max(分支耗时)，不是 sum。

        每个分支的 LLM 调用睡 0.25s。串行需要 0.75s，并发只要 ~0.25s。
        阈值取 0.5s：足以区分两者，又不会在慢机器上误报。
        """
        llm = _FanoutLLM(delay=0.25)

        started = time.perf_counter()
        out = _run(monkeypatch, llm)
        elapsed = time.perf_counter() - started

        assert len(llm.plan_calls) == 3, f"三个分支都应执行，实际 {llm.plan_calls}"
        assert len(out["plans"]) == 3
        assert elapsed < 0.5, f"三分支疑似串行执行，耗时 {elapsed:.2f}s"


# ══════════════════════════════════════════════════════════════════
# 分支隔离
# ══════════════════════════════════════════════════════════════════


class TestBranchIsolation:
    def test_单分支失败不影响其余(self, monkeypatch):
        """3 套里出 2 套是可用结果——失败的那个如实记录，其余照常交付。"""
        out = _run(monkeypatch, _FanoutLLM(fail_on="plan_nordic_medium"))

        assert len(out["plans"]) == 2
        assert {p["plan_id"] for p in out["plans"]} == {
            "plan_modern_economy", "plan_chinese_high",
        }

        failed = [e for e in out["errors"] if e["agent"] == "A-03"]
        assert len(failed) == 1
        assert "plan_nordic_medium" in failed[0]["message"]

    def test_单分支失败仍产出可用对比表(self, monkeypatch):
        out = _run(monkeypatch, _FanoutLLM(fail_on="plan_nordic_medium"))
        comp = out["comparison"]

        assert comp["available"] is True, "2 套仍可对比"
        assert comp["plan_count"] == 2
        assert comp["requested_count"] == 3
        assert any("实际产出 2 套" in n for n in comp["notes"])

    def test_全部失败时降级且带原因(self, monkeypatch):
        out = _run(monkeypatch, _FanoutLLM(fail_all_plans=True))

        assert out["plans"] == []
        assert out["comparison"]["available"] is False
        assert out["degraded"] is True
        assert any("fan-in" in r for r in out["degrade_reasons"])
        # 三条分支各自的失败都要留下，不能只剩一条
        assert len([e for e in out["errors"] if e["agent"] == "A-03"]) == 3

    def test_全部失败时图不崩(self, monkeypatch):
        """这是最要紧的一条：0 套方案是业务失败，不是系统崩溃。"""
        out = _run(monkeypatch, _FanoutLLM(fail_all_plans=True))
        assert out["phase"] == "finalizing"
        assert out["layout"] is not None, "上游成果不应因下游失败而丢失"


# ══════════════════════════════════════════════════════════════════
# fan-in 汇总
# ══════════════════════════════════════════════════════════════════


class TestAggregation:
    def test_按分支顺序而非plan_id字典序(self, monkeypatch):
        """
        字典序会得到「中式、现代、北欧」——与请求顺序无关的排列，
        前端展示时用户会以为顺序是乱的。
        """
        out = _run(monkeypatch, _FanoutLLM())
        assert [p["plan_id"] for p in out["plans"]] == [
            "plan_modern_economy", "plan_nordic_medium", "plan_chinese_high",
        ]

    def test_对比表含全部方案行(self, monkeypatch):
        out = _run(monkeypatch, _FanoutLLM())
        comp = out["comparison"]

        assert comp["available"] is True
        assert comp["plan_count"] == 3
        assert len(comp["rows"]) == 3
        assert [r["style"] for r in comp["rows"]] == ["modern", "nordic", "chinese"]

    def test_不代替用户排优劣(self, monkeypatch):
        """
        没有业主的优先级信息（更看重预算还是环保？），
        任何"推荐方案"都是无依据的——这与「不编造评分」是同一条纪律。
        """
        out = _run(monkeypatch, _FanoutLLM())
        assert out["comparison"]["recommendation"] is None
        assert out["comparison"]["recommendation_note"]

    def test_仅一套时对比表不可用(self, monkeypatch):
        """单列不成表。方案照常返回，只是不声称可对比。"""
        out = _run(monkeypatch, _FanoutLLM(),
                   styles=["modern"], budget_grades=["economy"])

        assert len(out["plans"]) == 1
        assert out["comparison"]["available"] is False
        assert "仅产出 1 套" in out["comparison"]["unavailable_reason"]

    def test_对比表字段含数据质量信号(self, monkeypatch):
        out = _run(monkeypatch, _FanoutLLM())
        row = out["comparison"]["rows"][0]
        assert "unassigned_rooms" in row
        assert "invented_rooms" in row

    def test_自定义分支数(self, monkeypatch):
        out = _run(monkeypatch, _FanoutLLM(),
                   styles=["cream", "japandi"], budget_grades=["high", "medium"])
        assert [p["plan_id"] for p in out["plans"]] == [
            "plan_cream_high", "plan_japandi_medium",
        ]


# ══════════════════════════════════════════════════════════════════
# fan-out 前的路由
# ══════════════════════════════════════════════════════════════════


class TestRouteAfterDiagnosis:
    def test_能出方案时返回Send列表(self):
        result = workflow._route_after_diagnosis(_state(layout=LAYOUT, diagnosis=DIAGNOSIS))
        assert isinstance(result, list)
        assert len(result) == 3

    def test_缺墙体时短路到END(self):
        """
        规划的判据比诊断严一档（还要墙体信息）。存在「诊断能跑但方案不能出」
        的中间态——此时在入口拦一次，好过让 3 个分支各撞一次墙、
        最后得到 0 套方案加 3 条重复错误。
        """
        assert workflow._route_after_diagnosis(_state(layout={**LAYOUT, "walls": []})) == "end"

    def test_无layout时短路(self):
        assert workflow._route_after_diagnosis(_state(layout=None)) == "end"

    def test_短路时不出方案也不报错(self, monkeypatch):
        """
        路由短路是**正常路径**，不是错误路径——errors 必须干净。

        注意：这里改的是「A-01 的产出」而不是初始 state。
        A-01 跑完会用它自己的产出整体覆盖 state 里的 layout，
        往 state 里塞一个缺墙的 layout 是没用的。
        """
        llm = _FanoutLLM(layout={**LAYOUT, "walls": []})
        out = _run(monkeypatch, llm)

        assert out["plans"] == []
        assert llm.plan_calls == [], "短路时不应调用 A-03"
        assert out["errors"] == []
        assert out["layout"] is not None, "解析成果仍应保留"


# ══════════════════════════════════════════════════════════════════
# 端到端：跨切面字段在并发下不互相覆盖
# ══════════════════════════════════════════════════════════════════


class TestCrossCuttingUnderConcurrency:
    def test_plan_bundles三个键都在(self, monkeypatch):
        """
        plan_bundles 用 MergeDict 归约。若少了这个 reducer，
        三个分支并发写入会抛 InvalidUpdateError —— 这是接入 fan-out
        时第一个会撞上的坑。
        """
        out = _run(monkeypatch, _FanoutLLM())
        assert set(out["plan_bundles"]) == {
            "plan_modern_economy", "plan_nordic_medium", "plan_chinese_high",
        }

    def test_phase与degraded并发写入不报错(self, monkeypatch):
        """
        degraded / phase 是标量，没有 reducer 时三个分支同时写会直接报错。
        前者用 OrBool，后者用 LastWrite（见 state.py）。
        """
        out = _run(monkeypatch, _FanoutLLM())
        assert out["phase"] == "finalizing"   # fan-in 在分支之后写，应当胜出
        assert out["degraded"] is False
