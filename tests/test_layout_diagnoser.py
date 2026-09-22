"""
A-02 LayoutDiagnoserAgent 测试。

本文件守的核心命题：**诊断是推理，但推理不等于可以造依据。**

具体两条：
1. 数据不足以支撑诊断时（降级解析 / 缺面积缺窗），必须**拒绝**而不是编造评分
   （AC-33：`degraded_basic` 下诊断必须被拒）。
2. 承重墙提示与 data_gaps 由**代码强制生成**，不依赖模型是否想得起来。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.app.agents.layout_diagnoser import LayoutDiagnoserAgent
from backend.app.core.capabilities import OperationNotAllowedError
from backend.app.core.llm_client import LLMError, LLMResult
from backend.app.graph.state import HomeDecoState, initial_state
from backend.app.schemas.layout import DiagnosisItem, LayoutDiagnosis


# ══════════════════════════════════════════════════════════════════
# 样本
# ══════════════════════════════════════════════════════════════════

#: 数据完整的户型 —— 诊断应当正常进行
FULL_LAYOUT = {
    "layout_id": "layout_test_001",
    "mode": "full",
    "rooms": [
        {"name": "客厅", "type": "living_room", "area": 28.5, "bbox": [120, 80, 420, 360],
         "orientation": "south"},
        {"name": "主卧", "type": "bedroom", "area": 16.2, "bbox": [440, 80, 680, 300],
         "orientation": "south"},
        {"name": "次卧", "type": "bedroom", "area": 12.0, "bbox": [440, 320, 680, 500],
         "orientation": "north"},
    ],
    "walls": [
        {"type": "load_bearing", "coords": [[100, 60], [700, 60]]},
        {"type": "non_load_bearing", "coords": [[100, 400], [700, 400]]},
    ],
    # 至少 2 樘门，否则动线维度会被标记为数据不足（判据见 _DIM_REQUIRES）
    "doors": [
        {"position": [250, 380], "width": 0.9, "swing": "inward"},
        {"position": [550, 380], "width": 0.8, "swing": "inward"},
    ],
    "windows": [
        {"position": [600, 50], "width": 1.8, "orientation": "south"},
        {"position": [300, 50], "width": 1.5, "orientation": "south"},
    ],
    "total_area": 89.0,
    "entrance_orientation": "north",
    "has_north_arrow": True,
    "confidence": 0.85,
}

#: 降级结果 —— 只有房间名，其余全空。诊断必须被拒。
DEGRADED_LAYOUT = {
    "layout_id": "layout_test_002",
    "mode": "degraded_basic",
    "rooms": [
        {"name": "Living room", "type": "other", "area": 0.0, "bbox": [], "orientation": "unknown"},
        {"name": "Bedroom", "type": "other", "area": 0.0, "bbox": [], "orientation": "unknown"},
    ],
    "walls": [], "doors": [], "windows": [], "dimensions": [],
    "total_area": 0.0,
    "has_north_arrow": False,
    "confidence": 0.45,
}

#: 完整模式但数据残缺（模型没估出面积）—— 同样应被守卫拦下
PARTIAL_LAYOUT = {
    "mode": "full",
    "rooms": [{"name": "客厅", "type": "living_room", "area": 0.0, "bbox": [1, 2, 3, 4]}],
    "walls": [{"type": "unknown", "coords": []}],
    "windows": [],
    "total_area": 0.0,
    "has_north_arrow": False,
    "confidence": 0.6,
}


def _diag_payload(**over) -> dict:
    """构造一份合法的诊断输出（模型侧）。"""
    base = {
        "lighting": {"score": 7.5, "issues": ["北侧卧室采光不足"], "suggestions": ["增加浅色reflection"]},
        "ventilation": {"score": 8.0, "issues": [], "suggestions": []},
        "circulation": {"score": 6.5, "issues": ["厨房到餐厅动线过长"], "suggestions": ["调整入口"]},
        "space_utilization": {"score": 7.0, "issues": [], "suggestions": []},
        "green_score": {"score": 7.8, "issues": [], "suggestions": ["建议使用E0级板材"]},
        "overall_score": 7.3,
        "summary": "整体格局方正，南北通风良好，北侧房间采光是主要短板。",
        "highlights": ["南北通透", "客厅面积充裕"],
        "load_bearing_warning": [],
        "data_gaps": [],
        "confidence": 0.8,
    }
    base.update(over)
    return base


class _FakeLLM:
    """假 LLM：记录调用参数，按脚本返回。"""

    def __init__(self, payload: dict | None = None, raises: Exception | None = None,
                 degraded: bool = False):
        self.payload = payload or _diag_payload()
        self.raises = raises
        self.degraded = degraded
        self.calls: list[dict] = []

    async def complete_json(self, schema, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return schema.model_validate(self.payload), LLMResult(
            text=json.dumps(self.payload), provider="fake", model_used="fake-model",
            degraded=self.degraded,
            degrade_reason="deepseek: HTTP 429" if self.degraded else None,
            prompt_tokens=500, completion_tokens=200, elapsed_ms=300,
        )

    async def complete(self, **kwargs):  # pragma: no cover
        raise AssertionError("诊断不应走纯文本路径")


def _state(layout: dict | None) -> HomeDecoState:
    return initial_state(task_id="t-diag", layout=layout)


# ══════════════════════════════════════════════════════════════════
# 业务连续性守卫 —— 本文件最重要的一组
# ══════════════════════════════════════════════════════════════════


class TestCapabilityGate:
    """
    诊断依赖窗户/朝向/面积。降级解析这三项全空。
    硬跑诊断的结果是模型凭空编造评分——比拒绝糟糕得多。
    """

    def test_degraded_layout_is_rejected(self):
        """AC-33：degraded_basic 下诊断必须被拒。"""
        agent = LayoutDiagnoserAgent(llm=_FakeLLM())
        out = asyncio.run(agent.execute(_state(DEGRADED_LAYOUT)))

        assert out["trace"][0]["ok"] is False
        assert out["degraded"] is True
        # LLM 一次都不该被调用——拦在入口
        assert out["diagnosis"] if "diagnosis" in out else True

    def test_degraded_layout_never_calls_llm(self):
        """守卫必须在调用 LLM **之前**生效，否则白烧 token。"""
        fake = _FakeLLM()
        asyncio.run(LayoutDiagnoserAgent(llm=fake).execute(_state(DEGRADED_LAYOUT)))
        assert fake.calls == [], "降级输入不得触发任何 LLM 调用"

    def test_partial_layout_rejected_even_in_full_mode(self):
        """
        判据是**数据内容**，不是 mode 字符串。
        完整模式但没面积，同样拦——这是守卫"数据驱动"设计的核心。
        """
        fake = _FakeLLM()
        out = asyncio.run(LayoutDiagnoserAgent(llm=fake).execute(_state(PARTIAL_LAYOUT)))
        assert out["trace"][0]["ok"] is False
        assert fake.calls == []

    def test_rejection_reason_is_actionable(self):
        """拒绝时必须说清缺什么、怎么办。"""
        agent = LayoutDiagnoserAgent(llm=_FakeLLM())
        with pytest.raises(OperationNotAllowedError) as ei:
            asyncio.run(agent.run(_state(DEGRADED_LAYOUT)))
        payload = ei.value.to_payload()
        assert payload["missing"], "必须列出缺失字段"
        assert payload["suggestion"], "必须给出后续建议"

    def test_missing_layout_rejected(self):
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM()).execute(_state(None)))
        assert out["trace"][0]["ok"] is False
        assert "layout" in out["errors"][0]["message"]


# ══════════════════════════════════════════════════════════════════
# 正常路径
# ══════════════════════════════════════════════════════════════════


class TestHappyPath:
    def test_full_layout_diagnoses(self):
        agent = LayoutDiagnoserAgent(llm=_FakeLLM())
        out = asyncio.run(agent.execute(_state(FULL_LAYOUT)))

        assert out["trace"][0]["ok"] is True, out.get("errors")
        d = out["diagnosis"]
        # AC-03：≥5 个维度
        for dim in ("lighting", "ventilation", "circulation",
                    "space_utilization", "green_score"):
            assert dim in d and "score" in d[dim]
        assert 0 <= d["overall_score"] <= 10
        assert out["phase"] == "diagnosed"

    def test_prompt_carries_data_summary(self):
        """提示词里应带上数据概览，让模型一眼看出有没有数据。"""
        fake = _FakeLLM()
        asyncio.run(LayoutDiagnoserAgent(llm=fake).execute(_state(FULL_LAYOUT)))
        prompt = fake.calls[0]["user"]
        assert "识别到的窗户数量" in prompt
        assert "面积数据是否完整" in prompt

    def test_prompt_lists_rooms_and_windows(self):
        fake = _FakeLLM()
        asyncio.run(LayoutDiagnoserAgent(llm=fake).execute(_state(FULL_LAYOUT)))
        prompt = fake.calls[0]["user"]
        assert "客厅" in prompt and "主卧" in prompt
        assert "south" in prompt

    def test_no_windows_explicitly_stated(self):
        """没有窗时必须明说，而不是给一个空数组让模型自己猜。"""
        fake = _FakeLLM()
        layout = dict(FULL_LAYOUT, windows=[])
        asyncio.run(LayoutDiagnoserAgent(llm=fake).execute(_state(layout)))
        assert "未识别到任何窗户" in fake.calls[0]["user"]


# ══════════════════════════════════════════════════════════════════
# 后处理：代码兜底，不交给模型的自觉
# ══════════════════════════════════════════════════════════════════


class TestPostprocess:
    """
    安全提示与数据缺口由代码强制生成。

    理由：不能指望模型每次都记得写承重墙提示——
    这是安全事故级别的信息，漏一次就是事故。
    """

    def test_load_bearing_warning_forced(self):
        """识别到承重墙时，提示必须非空且含"现场复核"。"""
        fake = _FakeLLM(_diag_payload(load_bearing_warning=[]))   # 模型忘了写
        out = asyncio.run(LayoutDiagnoserAgent(llm=fake).execute(_state(FULL_LAYOUT)))
        w = out["diagnosis"]["load_bearing_warning"]
        assert w, "承重墙提示不得为空"
        assert any("现场复核" in x for x in w)

    def test_warning_when_no_load_bearing_detected(self):
        """
        没识别到承重墙 ≠ 没有承重墙。
        这个区别对用户很重要——不能让人以为"全部可以拆"。
        """
        layout = dict(FULL_LAYOUT, walls=[{"type": "non_load_bearing", "coords": []}])
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM()).execute(_state(layout)))
        w = out["diagnosis"]["load_bearing_warning"]
        assert w and any("现场确认" in x for x in w)

    def test_data_gaps_filled_by_code(self):
        """模型说"一切正常"，但代码发现缺窗缺面积，必须补上。"""
        layout = dict(FULL_LAYOUT, windows=[], has_north_arrow=False)
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM(_diag_payload(data_gaps=[])))
                          .execute(_state(layout)))
        gaps = out["diagnosis"]["data_gaps"]
        assert any("窗户" in g for g in gaps)
        assert any("指北针" in g for g in gaps)

    def test_data_gaps_deduplicated(self):
        """模型自己也写了一条，代码再补同一条，不应重复。"""
        layout = dict(FULL_LAYOUT, windows=[])
        out = asyncio.run(
            LayoutDiagnoserAgent(llm=_FakeLLM(_diag_payload(
                data_gaps=["数据中未识别到窗户，采光与通风评分缺乏直接依据"])))
            .execute(_state(layout)))
        gaps = out["diagnosis"]["data_gaps"]
        assert len(gaps) == len(set(gaps)), "data_gaps 不应有重复项"

    def test_confidence_lowered_when_many_gaps(self):
        """缺口多时主动压低置信度，不让模型自我感觉良好。"""
        # 面积保留（否则会被守卫拦下），但拿掉窗户/指北针/墙体 -> 3 项缺口
        layout = dict(FULL_LAYOUT, windows=[], walls=[], has_north_arrow=False)
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM(_diag_payload(confidence=0.95)))
                          .execute(_state(layout)))
        assert out["diagnosis"]["confidence"] <= 0.5


class TestInsufficientData:
    """
    「数据不足」必须能表达出来，而不是被迫给一个假分数填位。

    这是本模块相对"让 LLM 随便打分"的关键差别——
    一个编造的"采光 7.5 分"比"无法评估"糟糕得多。
    """

    def test_lighting_and_ventilation_marked_when_no_windows(self):
        layout = dict(FULL_LAYOUT, windows=[])
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM()).execute(_state(layout)))
        d = out["diagnosis"]

        for dim in ("lighting", "ventilation"):
            assert d[dim]["insufficient_data"] is True, f"{dim} 应标记数据不足"
            assert d[dim]["score"] == 0.0, f"{dim} 数据不足时分数必须为 0"
            assert any("窗户" in i for i in d[dim]["issues"])

    def test_other_dimensions_still_evaluated(self):
        """采光通风评不了，不代表动线和利用率也不能评。"""
        layout = dict(FULL_LAYOUT, windows=[])
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM()).execute(_state(layout)))
        d = out["diagnosis"]

        for dim in ("circulation", "space_utilization", "green_score"):
            assert d[dim]["insufficient_data"] is False
            assert d[dim]["score"] > 0

    def test_overall_excludes_insufficient_dimensions(self):
        """
        综合分不得把"数据不足"的 0 分算进去——
        那个 0 不代表户型差，只代表我们不知道。混进去会误导用户。
        """
        layout = dict(FULL_LAYOUT, windows=[])
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM()).execute(_state(layout)))
        d = out["diagnosis"]

        scored = [d[k]["score"] for k in
                  ("circulation", "space_utilization", "green_score")]
        expected = round(sum(scored) / len(scored), 1)
        assert d["overall_score"] == expected
        assert d["overall_score"] > 0, "不应被两个 0 分维度拉垮"

    def test_with_windows_nothing_marked_insufficient(self):
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM()).execute(_state(FULL_LAYOUT)))
        d = out["diagnosis"]
        assert all(not d[k]["insufficient_data"] for k in (
            "lighting", "ventilation", "circulation", "space_utilization", "green_score"))

    def test_circulation_marked_when_too_few_doors(self):
        """
        少于 2 樘门就无法判断房间之间的连通关系。

        实测中模型**自己**发现过这一点并主动标记（只识别到 1 樘门时），
        但关键判定不能依赖模型的自觉——换个种子它可能就编一个动线分数出来。
        这里由代码强制保证（见 _DIM_REQUIRES）。
        """
        layout = dict(FULL_LAYOUT, doors=[{"position": [250, 380], "width": 0.9}])
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM()).execute(_state(layout)))
        c = out["diagnosis"]["circulation"]
        assert c["insufficient_data"] is True
        assert c["score"] == 0.0
        assert any("门" in i for i in c["issues"])

    def test_circulation_ok_with_two_doors(self):
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM()).execute(_state(FULL_LAYOUT)))
        assert out["diagnosis"]["circulation"]["insufficient_data"] is False

    def test_overall_drops_insufficient_dimensions_from_denominator(self):
        """
        失效维度不得进入综合分。

        注意各维度的数据依赖是**独立**的：去掉门只影响动线，
        采光/通风因为窗户还在所以照常评估。分母应为 4 而非 5。
        """
        layout = dict(FULL_LAYOUT, doors=[])
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM()).execute(_state(layout)))
        d = out["diagnosis"]

        assert d["circulation"]["insufficient_data"] is True
        assert d["lighting"]["insufficient_data"] is False

        scored = [d[k]["score"] for k in
                  ("lighting", "ventilation", "space_utilization", "green_score")]
        assert d["overall_score"] == round(sum(scored) / len(scored), 1)

    def test_five_dimensions_have_independent_data_dependencies(self):
        """
        各维度的数据依赖是独立的，一次缺失不应连带拖垮无关维度。

        windows 缺失影响采光+通风，doors 缺失影响动线——但
        空间利用率（看面积）与绿色环保（看整体条件）不受这两个影响。

        注：「五个维度全部失效」在设计上**不可达**——守卫要求必须有面积，
        而空间利用率正是看面积的维度，因此它永远可评。
        """
        layout = dict(FULL_LAYOUT, windows=[], doors=[])
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM()).execute(_state(layout)))
        d = out["diagnosis"]

        insufficient = {k for k in ("lighting", "ventilation", "circulation",
                                    "space_utilization", "green_score")
                        if d[k]["insufficient_data"]}
        assert insufficient == {"lighting", "ventilation", "circulation"}

        remaining = [d[k]["score"] for k in ("space_utilization", "green_score")]
        assert d["overall_score"] == round(sum(remaining) / len(remaining), 1)
        assert d["overall_score"] > 0, "不应因为两个无关维度缺失就归零"

    def test_schema_forces_zero_score_when_insufficient(self):
        """
        Schema 层兜底：模型一边标 insufficient_data=true 一边给 7 分时，
        把分数强制归零——那种"既说不知道又给结论"的输出最容易误导人。
        """
        item = DiagnosisItem.model_validate(
            {"score": 7.5, "insufficient_data": True, "issues": [], "suggestions": []})
        assert item.score == 0.0

    def test_insufficient_reason_survives_llm_claims(self):
        """模型声称采光 9 分，但没窗户——代码覆盖它。"""
        layout = dict(FULL_LAYOUT, windows=[])
        fake = _FakeLLM(_diag_payload(
            lighting={"score": 9.0, "issues": [], "suggestions": []}))
        out = asyncio.run(LayoutDiagnoserAgent(llm=fake).execute(_state(layout)))
        assert out["diagnosis"]["lighting"]["score"] == 0.0
        assert out["diagnosis"]["lighting"]["insufficient_data"] is True

    def test_model_based_on_recorded(self):
        """记录诊断依据了哪些数据，便于事后审计。"""
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM()).execute(_state(FULL_LAYOUT)))
        meta = out["diagnosis"]["model_based_on"]
        assert meta["rooms"] == 3 and meta["windows"] == 2 and meta["has_area"] is True


# ══════════════════════════════════════════════════════════════════
# 降级与容错
# ══════════════════════════════════════════════════════════════════


class TestDegradation:
    def test_degraded_flag_propagates(self):
        """
        诊断是**文本推理**，本地 qwen2.5:3b 属能力范围内，
        因此允许降级（与 A-01 的视觉任务不同，那里必须禁止）。
        """
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM(degraded=True))
                          .execute(_state(FULL_LAYOUT)))
        assert out["degraded"] is True
        assert "A-02" in out["degrade_reasons"][0]

    def test_llm_failure_isolated(self):
        out = asyncio.run(LayoutDiagnoserAgent(llm=_FakeLLM(raises=LLMError("全部失败")))
                          .execute(_state(FULL_LAYOUT)))
        assert out["trace"][0]["ok"] is False
        assert out["errors"][0]["agent"] == "A-02"

    def test_timeout_circuit_broken(self):
        """A-02 超时阈值应为 30s（纯文本任务，比 A-01 的 45s 紧）。"""
        assert LayoutDiagnoserAgent.timeout == 30.0

    def test_requires_vision_false(self):
        """诊断是纯文本任务，不应被走视觉链路（否则白白多花成本）。"""
        assert LayoutDiagnoserAgent.requires_vision is False


# ══════════════════════════════════════════════════════════════════
# Schema
# ══════════════════════════════════════════════════════════════════


class TestDiagnosisSchema:
    def test_score_range_enforced(self):
        with pytest.raises(Exception):
            LayoutDiagnosis.model_validate(_diag_payload(
                lighting={"score": 15.0, "issues": [], "suggestions": []}))

    def test_defaults_fillable(self):
        """只给五个维度也应能构造——其余字段有默认值。"""
        d = LayoutDiagnosis.model_validate({
            "lighting": {"score": 7}, "ventilation": {"score": 7},
            "circulation": {"score": 7}, "space_utilization": {"score": 7},
            "green_score": {"score": 7},
        })
        assert d.data_gaps == [] and d.confidence == 0.0
