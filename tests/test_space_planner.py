"""
A-03 SpacePlannerAgent 测试。

本文件守的核心命题：**方案只能安排户型里真实存在的房间。**

模型的训练语料里堆满了"标准三室两厅"的样板方案。给它一个两居室，它照样会
写出「儿童房」「书房」——而且写得很像那么回事。这与 A-02 的"编造评分"同源：
**模型被要求产出结构化内容时，倾向于先满足格式、再考虑事实。**

因此本文件最重要的两组测试是：
- `TestRoomAlignment`：幻觉房间被剔除、遗漏房间被记录
- `TestRoomNameMatching`：表述差异（"主卧室" vs "主卧"）**不被误判**为幻觉

第二条同样重要：如果容错做得太松，每份方案都会误报幻觉，
那个信号就废了；做得太紧，则把模型的正常表述当成编造。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.agents.space_planner import (
    SpacePlannerAgent,
    _match_room,
    _norm,
)
from backend.app.core.capabilities import OperationNotAllowedError
from backend.app.core.llm_client import LLMResult
from backend.app.graph.state import initial_state
from backend.app.schemas.plan import SpacePlan

# ══════════════════════════════════════════════════════════════════
# 样本
# ══════════════════════════════════════════════════════════════════

#: 两居室。刻意**没有**书房/儿童房——用来验证模型编房间时会被剔除。
LAYOUT = {
    "layout_id": "layout_test_001",
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
    "ventilation": {"score": 0, "insufficient_data": True,
                    "issues": ["数据中未识别到南北对流"], "suggestions": []},
    "circulation": {"score": 6.0, "insufficient_data": False,
                    "issues": ["入户到厨房动线穿过客厅"], "suggestions": ["调整家具布局"]},
    "space_utilization": {"score": 7.0, "insufficient_data": False,
                          "issues": [], "suggestions": []},
    "green_score": {"score": 7.0, "insufficient_data": False, "issues": [], "suggestions": []},
    "overall_score": 6.8,
    "summary": "格局方正。",
    "data_gaps": [],
    "confidence": 0.8,
}


def _response(**overrides) -> dict:
    """一份模型响应的骨架，可局部覆盖。"""
    base = {
        "summary": "以家具摆放实现分区，不动结构。",
        "zones": [
            {"room_name": "客厅", "function": "起居+用餐", "rationale": "面积充裕",
             "furniture": ["沙发", "餐桌"]},
            {"room_name": "主卧", "function": "睡眠+收纳", "rationale": "朝南采光好",
             "furniture": ["床", "衣柜"]},
        ],
        "storage_plans": [
            {"location": "主卧", "kind": "custom_cabinet", "note": "到顶衣柜"},
        ],
        "circulation_fixes": [
            {"problem": "入户到厨房动线穿过客厅",
             "solution": "餐桌靠墙，留出通行宽度",
             "target_rooms": ["客厅"]},
        ],
        "key_moves": ["客餐厅一体", "主卧到顶衣柜"],
        "data_gaps": [],
        "confidence": 0.8,
    }
    base.update(overrides)
    return base


class _FakeLLM:
    """返回预置 SpacePlan 的假 LLM。"""

    def __init__(self, payload: dict | None = None, *, degraded: bool = False):
        self.payload = payload if payload is not None else _response()
        self.degraded = degraded

    async def complete_json(self, schema, **kwargs):
        assert schema is SpacePlan, f"A-03 应请求 SpacePlan，实际 {schema}"
        return schema.model_validate(self.payload), LLMResult(
            text="{}",
            provider="ollama" if self.degraded else "deepseek",
            model_used="qwen2.5:3b" if self.degraded else "deepseek-flash",
            degraded=self.degraded,
            degrade_reason="deepseek 不可用" if self.degraded else None,
            prompt_tokens=2000, completion_tokens=600, elapsed_ms=9000,
        )

    async def complete(self, **kwargs):  # pragma: no cover
        raise AssertionError("A-03 不应走纯文本路径")


def _state(**over) -> dict:
    base = {
        "task_id": "t-a03",
        "layout": LAYOUT,
        "diagnosis": DIAGNOSIS,
        "branch_spec": {"plan_id": "plan_modern_economy", "style": "modern",
                        "budget_grade": "economy", "index": 0},
    }
    base.update(over)
    return initial_state(**base)


def _run(llm: _FakeLLM, **over) -> dict:
    return asyncio.run(SpacePlannerAgent(llm=llm).execute(_state(**over)))


def _plan(out: dict) -> dict:
    """从返回值里取出本分支的方案。"""
    bundles = out["plan_bundles"]
    assert len(bundles) == 1, f"应恰好写入一个 plan_id，实际 {list(bundles)}"
    return next(iter(bundles.values()))["space_plan"]


# ══════════════════════════════════════════════════════════════════
# 房间名校验（纯函数，先行验证）
# ══════════════════════════════════════════════════════════════════


class TestRoomNameMatching:
    """容错的松紧决定了 invented_rooms 这个信号有没有价值。"""

    @pytest.mark.parametrize("raw", ["客厅", "客 厅", "客厅(Living Room)", "客厅（含餐厅）"])
    def test_表述差异不算幻觉(self, raw):
        assert _match_room(raw, ["客厅", "主卧"]) == "客厅"

    def test_短名包含长名(self):
        assert _match_room("主卧室", ["主卧"]) == "主卧"

    def test_单字不参与包含匹配(self):
        """
        「房」这种单字若参与包含匹配，会把一堆房间误配到一起。
        短边必须 ≥2 字。
        """
        assert _match_room("房", ["主卧", "次卧"]) is None

    def test_完全无关的房间返回None(self):
        assert _match_room("儿童房", ["客厅", "主卧"]) is None

    def test_空名返回None(self):
        assert _match_room("", ["客厅"]) is None
        assert _match_room("   ", ["客厅"]) is None

    def test_norm_去掉标点空白(self):
        assert _norm("主 卧-A(1)") == _norm("主卧a1")


# ══════════════════════════════════════════════════════════════════
# 房间对齐 —— 本文件的核心
# ══════════════════════════════════════════════════════════════════


class TestRoomAlignment:
    def test_全部房间被安排(self):
        out = _run(_FakeLLM())
        plan = _plan(out)
        assert plan["unassigned_rooms"] == []
        assert plan["invented_rooms"] == []

    def test_幻觉房间被剔除并记录(self):
        """
        模型给两居室安排了「儿童房」和「书房」——两个户型里都不存在。
        必须**剔除**（不存在的房间无法落地），但**记录下来**（这是质量信号）。
        """
        llm = _FakeLLM(_response(zones=[
            {"room_name": "客厅", "function": "起居", "rationale": "面积充裕", "furniture": []},
            {"room_name": "主卧", "function": "睡眠", "rationale": "朝南", "furniture": []},
            {"room_name": "儿童房", "function": "儿童居住", "rationale": "预留", "furniture": []},
            {"room_name": "书房", "function": "办公", "rationale": "靠窗", "furniture": []},
        ]))
        plan = _plan(_run(llm))

        kept = [z["room_name"] for z in plan["zones"]]
        assert kept == ["客厅", "主卧"], f"幻觉房间应被剔除，实际保留 {kept}"
        assert set(plan["invented_rooms"]) == {"儿童房", "书房"}
        # 剔除这件事必须对用户可见，不能静默
        assert any("不存在的房间" in g for g in plan["data_gaps"])

    def test_遗漏房间被记录(self):
        """模型只安排了客厅，漏掉主卧——代码必须补记，而不是当没发生。"""
        llm = _FakeLLM(_response(zones=[
            {"room_name": "客厅", "function": "起居", "rationale": "面积充裕", "furniture": []},
        ]))
        plan = _plan(_run(llm))

        assert plan["unassigned_rooms"] == ["主卧"]
        assert any("未获功能安排" in g for g in plan["data_gaps"])

    def test_重复安排同一房间只保留一次(self):
        llm = _FakeLLM(_response(zones=[
            {"room_name": "客厅", "function": "起居", "rationale": "a", "furniture": []},
            {"room_name": "客厅", "function": "书房", "rationale": "b", "furniture": []},
            {"room_name": "主卧", "function": "睡眠", "rationale": "c", "furniture": []},
        ]))
        plan = _plan(_run(llm))

        assert [z["room_name"] for z in plan["zones"]] == ["客厅", "主卧"]
        assert any("重复安排" in r for r in plan["invented_rooms"])

    def test_房间名被规范成户型里的原名(self):
        """
        模型写「主卧室」，户型里叫「主卧」——保留时统一成户型原名，
        否则下游按键名索引（热区、图像标注）会对不上。
        """
        llm = _FakeLLM(_response(zones=[
            {"room_name": "客 厅", "function": "起居", "rationale": "a", "furniture": []},
            {"room_name": "主卧室", "function": "睡眠", "rationale": "b", "furniture": []},
        ]))
        plan = _plan(_run(llm))
        assert [z["room_name"] for z in plan["zones"]] == ["客厅", "主卧"]
        assert plan["invented_rooms"] == [], "表述差异不应被记成幻觉"

    def test_动线目标房间同样过滤(self):
        llm = _FakeLLM(_response(circulation_fixes=[
            {"problem": "动线穿客厅", "solution": "调整家具",
             "target_rooms": ["客厅", "儿童房", "主卧室"]},
        ]))
        plan = _plan(_run(llm))
        assert plan["circulation_fixes"][0]["target_rooms"] == ["客厅", "主卧"]


# ══════════════════════════════════════════════════════════════════
# 身份字段由代码写入
# ══════════════════════════════════════════════════════════════════


class TestIdentityFromSpec:
    def test_plan_id由代码写入(self):
        """
        不采信模型自报的身份——否则三个分支可能给自己起同一个名字，
        fan-in 汇总时会在 plan_bundles 里互相覆盖，最终只剩一套方案。
        """
        out = _run(_FakeLLM(), branch_spec={"plan_id": "plan_nordic_high",
                                            "style": "nordic", "budget_grade": "high",
                                            "index": 2})
        assert list(out["plan_bundles"]) == ["plan_nordic_high"]
        plan = _plan(out)
        assert plan["plan_id"] == "plan_nordic_high"
        assert plan["style"] == "nordic"
        assert plan["budget_grade"] == "high"
        assert plan["plan_index"] == 2

    def test_缺少branch_spec时回退到首个风格(self):
        """Agent 必须能脱离 fan-out 单独跑（调试、单测）。"""
        state = _state(styles=["chinese"], budget_grades=["high"])
        state.pop("branch_spec")
        out = asyncio.run(SpacePlannerAgent(llm=_FakeLLM()).execute(state))
        assert list(out["plan_bundles"]) == ["plan_chinese_high"]


# ══════════════════════════════════════════════════════════════════
# 业务连续性守卫
# ══════════════════════════════════════════════════════════════════


class TestGuard:
    def test_降级解析被拒且不调用LLM(self):
        """AC-33 的延伸：降级结果连诊断都撑不住，更撑不住规划。"""
        calls: list[str] = []

        class _Spy(_FakeLLM):
            async def complete_json(self, schema, **kwargs):
                calls.append("called")
                return await super().complete_json(schema, **kwargs)

        layout = {"mode": "degraded_basic",
                  "rooms": [{"name": "Living", "type": "other", "area": 0.0, "bbox": []}],
                  "walls": [], "doors": [], "windows": [], "total_area": 0.0}

        out = _run(_Spy(), layout=layout)
        assert calls == [], "守卫应在调用 LLM 之前拦下"
        assert out["trace"][0]["ok"] is False
        assert "generate_plan" in out["errors"][0]["message"]

    def test_无墙体被拒(self):
        """
        规划的判据比诊断严一档：还要有墙体信息（承重墙决定哪些改造能提）。
        缺墙体时是 3 个分支各撞一次墙，还是有干净的拒绝，由上层路由决定——
        Agent 这里负责如实抛错。
        """
        layout = {**LAYOUT, "walls": []}
        out = _run(_FakeLLM(), layout=layout)
        assert out["trace"][0]["ok"] is False
        assert "墙体" in out["errors"][0]["message"]

    def test_无layout直接失败(self):
        out = _run(_FakeLLM(), layout=None)
        assert out["trace"][0]["ok"] is False
        assert "layout" in out["errors"][0]["message"]

    def test_守卫异常带missing与suggestion(self):
        """拒绝必须说清缺什么、怎么办，不能只说"不允许"。"""
        agent = SpacePlannerAgent(llm=_FakeLLM())
        layout = {**LAYOUT, "walls": []}
        with pytest.raises(OperationNotAllowedError) as ei:
            asyncio.run(agent.run(_state(layout=layout)))
        payload = ei.value.to_payload()
        assert payload["missing"] == ["墙体信息"]
        assert payload["suggestion"]
        assert payload["operation"] == "generate_plan"


# ══════════════════════════════════════════════════════════════════
# 诊断缺失的处理
# ══════════════════════════════════════════════════════════════════


class TestDiagnosisHandling:
    def test_诊断失败时提示词区分于无问题(self):
        """
        「诊断跑了但没问题」与「诊断压根没跑成」必须分开说。
        混为一谈，模型会把"没有诊断"读成"户型没缺陷"，给出比实际更乐观的方案。
        """
        prompt = SpacePlannerAgent._build_prompt(
            LAYOUT, None, {"plan_id": "p", "style": "modern", "budget_grade": "economy"})
        assert "上游 A-02 执行失败" in prompt
        assert "未发现明显问题" not in prompt

    def test_诊断正常时展示问题清单(self):
        prompt = SpacePlannerAgent._build_prompt(
            LAYOUT, DIAGNOSIS, {"plan_id": "p", "style": "modern", "budget_grade": "economy"})
        assert "入户到厨房动线穿过客厅" in prompt
        assert "执行失败" not in prompt

    def test_诊断不可用时标记based_on(self):
        plan = _plan(_run(_FakeLLM(), diagnosis=None))
        assert plan["based_on"]["diagnosis_available"] is False


# ══════════════════════════════════════════════════════════════════
# 降级与跨切面
# ══════════════════════════════════════════════════════════════════


class TestDegradeAndTrace:
    def test_降级可见且带分支标识(self):
        out = _run(_FakeLLM(degraded=True))
        assert out["degraded"] is True
        assert any("plan_modern_economy" in r for r in out["degrade_reasons"])

    def test_trace记录模型与耗时(self):
        out = _run(_FakeLLM())
        t = out["trace"][0]
        assert t["agent"] == "A-03"
        assert t["ok"] is True
        assert t["llm"]["model"] == "deepseek-flash"
        assert t["elapsed_ms"] >= 0

    def test_phase进入planning(self):
        assert _run(_FakeLLM())["phase"] == "planning"

    def test_缺窗户写入data_gaps(self):
        plan = _plan(_run(_FakeLLM(), layout={**LAYOUT, "windows": []}))
        assert any("窗户" in g for g in plan["data_gaps"])

    def test_缺口多时压低置信度(self):
        """幻觉 + 遗漏 + 缺窗 => 缺口 ≥3，置信度应主动压到 0.5 以下。"""
        llm = _FakeLLM(_response(
            zones=[{"room_name": "客厅", "function": "起居", "rationale": "a", "furniture": []},
                   {"room_name": "书房", "function": "办公", "rationale": "b", "furniture": []}],
            confidence=0.95,
        ))
        plan = _plan(_run(llm, layout={**LAYOUT, "windows": []}))
        assert len(plan["data_gaps"]) >= 3
        assert plan["confidence"] <= 0.5
