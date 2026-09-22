"""
A-04 BudgetAgent 测试。

本文件守的核心命题：**LLM 挂了，预算仍然完整。**

这与 A-01~A-03 的失败方向**正好相反**。那几个 Agent 是生成型 ——
LLM 失败就是没产物，合理。A-04 是计算型，产物分两层：

    数字（规则引擎，零延迟零依赖）  ← 必须永远拿得到
    文字（LLM，装饰）              ← 失败就回退模板，不影响数字

而「数字不被模型污染」这件事，靠的不是提示词，是**类型系统**：
`BudgetNarrative` 里一个数值字段都没有，模型即便想改个数也无处可放。
本文件里有两条测试专门守住这个结构事实。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.app.agents.budget_agent import NARRATIVE_TIMEOUT, BudgetAgent
from backend.app.core.capabilities import OperationNotAllowedError
from backend.app.core.llm_client import LLMError, LLMResult
from backend.app.graph.state import initial_state
from backend.app.schemas.budget import BudgetBreakdown, BudgetNarrative
from backend.app.services.budget import engine

# ══════════════════════════════════════════════════════════════════
# 样本
# ══════════════════════════════════════════════════════════════════

LAYOUT = {
    "layout_id": "layout_budget_test",
    "mode": "full",
    "rooms": [
        {"name": "客厅", "type": "living_room", "area": 28.5, "bbox": [120, 80, 420, 360]},
        {"name": "主卧", "type": "bedroom", "area": 16.2, "bbox": [440, 80, 680, 300]},
        {"name": "次卧", "type": "bedroom", "area": 12.0, "bbox": [440, 320, 680, 500]},
    ],
    "walls": [{"type": "load_bearing", "coords": [[100, 60], [700, 60]]}],
    "doors": [{"position": [250, 380], "width": 0.9}],
    "windows": [{"position": [600, 50], "width": 1.8, "orientation": "south"}],
    "total_area": 89.0,
    "has_north_arrow": True,
    "confidence": 0.85,
}

NARRATIVE = {
    "summary": "经济档预算，大头在主材和水电。",
    "grade_rationale": "省在定制柜上。",
    "cost_drivers": ["主材", "水电改造"],
    "negotiation_tips": ["水电按实测结算，合同写明单价上限"],
    "saving_tips": ["成品柜替代全屋定制"],
    "warnings": ["水电是增项高发项"],
    "confidence": 0.7,
}


class _FakeLLM:
    """可控的假 LLM。`mode` 决定它在要文字时的行为。"""

    def __init__(self, payload: dict | None = None, *,
                 mode: str = "ok", delay: float = 0.0, degraded: bool = False):
        self.payload = payload if payload is not None else NARRATIVE
        self.mode = mode          # ok | raise | garbage
        self.delay = delay
        self.degraded = degraded
        self.prompts: list[str] = []

    async def complete_json(self, schema, **kwargs):
        assert schema is BudgetNarrative, f"A-04 只应向 LLM 要 BudgetNarrative，实际 {schema}"
        self.prompts.append(kwargs.get("user") or "")

        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "raise":
            raise LLMError("模拟解说模型不可用")
        if self.mode == "garbage":
            # 模型返回完全不合法的载荷
            return schema.model_validate({}), self._res()

        return schema.model_validate(self.payload), self._res()

    def _res(self) -> LLMResult:
        return LLMResult(
            text="{}", provider="fake", model_used="fake-model",
            degraded=self.degraded,
            degrade_reason="本地兜底" if self.degraded else None,
            prompt_tokens=900, completion_tokens=300, elapsed_ms=8000,
        )

    async def complete(self, **kwargs):  # pragma: no cover
        raise AssertionError("A-04 不应走纯文本路径")


def _state(**over) -> dict:
    base = {
        "task_id": "t-a04",
        "layout": LAYOUT,
        "branch_spec": {"plan_id": "plan_modern_economy", "style": "modern",
                        "budget_grade": "economy", "index": 0},
    }
    base.update(over)
    return initial_state(**base)


def _run(llm: _FakeLLM, **over) -> dict:
    return asyncio.run(BudgetAgent(llm=llm).execute(_state(**over)))


def _budget(out: dict) -> dict:
    bundles = out["plan_bundles"]
    assert len(bundles) == 1, f"应恰好写入一个 plan_id，实际 {list(bundles)}"
    return next(iter(bundles.values()))["budget"]


# ══════════════════════════════════════════════════════════════════
# 类型系统层面的保证 —— 本文件最重要的两条
# ══════════════════════════════════════════════════════════════════


class TestNarrativeCannotCarryAmounts:
    """
    这两条不是业务测试，是**结构事实**的断言。

    它们保证「LLM 不得生成金额」不依赖于模型的自觉，也不依赖于提示词写得好 ——
    而是因为类型里根本没有放数字的地方。新建字段时若不小心加了个 float，
    这里会立刻报红。
    """

    #: 唯一允许的数值字段。它是「把握程度」，不是钱。
    _ALLOWED_NUMERIC = frozenset({"confidence"})

    def test_叙事模型没有任何数值字段(self):
        """
        允许清单之外的数值字段一律不允许出现。

        用**允许清单**而不是黑名单：黑名单会随字段命名变化而失效，
        允许清单则要求每个新增的数值字段都被显式地审过一次。
        """
        numeric = [
            name for name, field in BudgetNarrative.model_fields.items()
            if field.annotation in (int, float)
            and name not in self._ALLOWED_NUMERIC
        ]
        assert not numeric, (
            f"BudgetNarrative 出现了数值字段 {numeric} —— "
            f"这会让 LLM 有机会产出金额，违反 ADR-07。"
            f"若确属非金额字段，请显式加入 _ALLOWED_NUMERIC 并说明理由"
        )

    def test_叙事模型不含金额相关字段名(self):
        """
        防的是"用嵌套模型或字符串绕过上一条"。

        判据只取**明确指向钱**的词。刻意不包含 sum / cost / budget 这类：
        `summary`（含 sum）与 `cost_drivers`（描述成本**构成**的文字）
        都是合法的叙述字段，把它们误判成违规会让这条测试失去意义。
        """
        suspicious = ("amount", "price", "total", "金额", "单价", "费用", "总价")
        hits = [
            name for name in BudgetNarrative.model_fields
            if any(s in name.lower() for s in suspicious)
        ]
        assert not hits, f"BudgetNarrative 出现了疑似金额字段: {hits}"

    def test_允许清单本身是审慎的(self):
        """允许清单不能悄悄膨胀 —— 加字段的人必须在这里显式改一行。"""
        assert self._ALLOWED_NUMERIC == {"confidence"}, (
            "允许清单被改动了。新增数值字段前请先确认它真的与金额无关"
        )

    def test_金额字段只存在于引擎产物中(self):
        """反向确认：金额确实在 BudgetBreakdown 里，只是不在叙事模型里。"""
        assert "total_min" in BudgetBreakdown.model_fields
        assert "total_min" not in BudgetNarrative.model_fields


# ══════════════════════════════════════════════════════════════════
# 数字由引擎算
# ══════════════════════════════════════════════════════════════════


class TestNumbersComeFromEngine:
    def test_标注计算来源(self):
        b = _budget(_run(_FakeLLM()))
        assert b["computed_by"].startswith("rule_engine")

    def test_与直接调用引擎结果一致(self):
        """
        同面积同档位下，Agent 产出的数字必须与直接调引擎**完全一致** ——
        不允许 Agent 层做任何"微调"。差一分钱都说明中间被改过。
        """
        b = _budget(_run(_FakeLLM()))
        expected = engine.calculate(area=89.0, grade="economy")

        assert b["total_min"] == expected.total_min
        assert b["total_max"] == expected.total_max
        assert len(b["lines"]) == len(expected.lines)
        assert [ln["key"] for ln in b["lines"]] == [ln.key for ln in expected.lines]

    def test_分项数满足AC05(self):
        b = _budget(_run(_FakeLLM()))
        assert len(b["lines"]) >= engine.MIN_BUDGET_LINES

    def test_档位取自分支规格(self):
        """三套方案的预算必须各按自己的档位算，而不是都用默认档。"""
        out = _run(_FakeLLM(), branch_spec={
            "plan_id": "plan_chinese_high", "style": "chinese",
            "budget_grade": "high", "index": 2,
        })
        b = _budget(out)
        assert b["grade"] == "high"
        assert b["total_min"] == engine.calculate(area=89.0, grade="high").total_min

    def test_演示数据声明透出(self):
        b = _budget(_run(_FakeLLM()))
        assert b["disclaimer"].strip()
        assert "演示" in b["disclaimer"]

    def test_模型写在summary里的数字不污染金额(self):
        """
        模型可能在自由文本里编一个"大概 15 万"。
        它只能待在 summary 里，**不会**进入任何金额字段 ——
        因为那些字段来自引擎，而不是模型的返回。
        """
        llm = _FakeLLM({**NARRATIVE,
                        "summary": "这套方案总共大约 15 万元，比市场价便宜 3 万。"})
        b = _budget(_run(llm))

        assert b["total_min"] == engine.calculate(area=89.0, grade="economy").total_min
        assert "15 万" in b["narrative"]["summary"], "自由文本原样保留"
        assert b["narrative"]["confidence"] == NARRATIVE["confidence"]


# ══════════════════════════════════════════════════════════════════
# 失败方向 —— LLM 挂了数字还在
# ══════════════════════════════════════════════════════════════════


class TestLLMFailureKeepsNumbers:
    def test_解说模型抛错时数字完好(self):
        b = _budget(_run(_FakeLLM(mode="raise")))

        assert b["total_min"] > 0
        assert len(b["lines"]) >= engine.MIN_BUDGET_LINES
        assert b["computed_by"].startswith("rule_engine")

    def test_解说失败时标记降级并回退模板(self):
        out = _run(_FakeLLM(mode="raise"))
        b = _budget(out)

        assert b["narrative_degraded"] is True
        assert out["degraded"] is True
        assert any("回退模板" in r for r in out["degrade_reasons"])
        # 回退文案不假装有观点，且如实告知缺失
        assert b["narrative"]["confidence"] == 0.0
        assert b["narrative"]["warnings"]
        assert "未能生成" in b["narrative"]["grade_rationale"]

    def test_解说超时时数字完好(self, monkeypatch):
        """
        「写字」有**独立**的超时（NARRATIVE_TIMEOUT），比节点总超时短 ——
        慢模型不能把已经算好的数字一起拖走。

        这里把阈值压到 0.05s，用 0.3s 的假模型触发超时。
        """
        monkeypatch.setattr("backend.app.agents.budget_agent.NARRATIVE_TIMEOUT", 0.05)
        latched: list[str] = []

        class _Slow(_FakeLLM):
            async def complete_json(self, schema, **kw):
                try:
                    return await super().complete_json(schema, **kw)
                finally:
                    latched.append("llm")

        llm = _Slow(delay=0.3)
        out = _run(llm)
        b = _budget(out)

        assert latched, "假模型应确实被调用过"
        assert b["total_min"] > 0, "超时不该影响金额"
        assert b["narrative_degraded"] is True
        assert any("超时" in r for r in out["degrade_reasons"])

    def test_模型返回空载荷时数字完好(self):
        """模型返回一个合法但全空的 BudgetNarrative —— 数字照样不受影响。"""
        b = _budget(_run(_FakeLLM(mode="garbage")))
        assert b["total_min"] > 0

    def test_断网也能算出完整预算(self):
        """
        这是 A-04 最值得讲的特性：**整条链路上唯一"断网可用"的 Agent**。

        数字不经过任何模型，所以把 LLM 彻底拿掉，预算表依然完整。
        """
        b = _budget(_run(_FakeLLM(mode="raise", delay=0.0)))

        assert b["subtotal_min"] > 0
        assert b["price_per_sqm_min"] > 0
        assert all(ln["amount_min"] >= 0 for ln in b["lines"])
        assert b["region_coefficient"] > 0


# ══════════════════════════════════════════════════════════════════
# 业务连续性守卫
# ══════════════════════════════════════════════════════════════════


class TestGuard:
    def test_无面积时拒且不调用LLM(self):
        """
        预算只需要一个数：面积。缺了就什么都算不了。
        **绝不能返回 ¥0** —— 那正是 capabilities 模块要拦的失败形态。
        """
        calls: list[str] = []

        class _Spy(_FakeLLM):
            async def complete_json(self, schema, **kw):
                calls.append("called")
                return await super().complete_json(schema, **kw)

        layout = {**LAYOUT, "total_area": 0.0,
                  "rooms": [{"name": "客厅", "type": "living_room", "area": 0.0}]}
        out = _run(_Spy(), layout=layout)

        assert calls == [], "守卫应在调用 LLM 之前拦下"
        assert out["trace"][0]["ok"] is False
        assert "estimate_budget" in out["errors"][0]["message"]
        # 关键：没有产出任何预算数字
        assert out.get("plan_bundles") in (None, {})

    def test_面积回退到房间面积之和(self):
        """
        total_area 为 0 但房间面积齐全时，可以**推导**出总面积 ——
        这是合理推导不是编造（见 capabilities.effective_total_area）。
        """
        layout = {**LAYOUT, "total_area": 0.0}   # 房间面积 28.5+16.2+12.0
        out = _run(_FakeLLM(), layout=layout)

        assert out["trace"][0]["ok"] is True
        b = _budget(out)
        assert b["area"] == pytest.approx(56.7)
        assert b["total_min"] == engine.calculate(area=56.7, grade="economy").total_min

    def test_守卫与引擎用同一个面积(self):
        """
        守卫判 `has_total_area`、引擎拿去做乘法 —— 两处必须是**同一个数**。
        若各自实现一遍回退逻辑，就会出现最坏情况：守卫放行、引擎拿到 0。
        """
        layout = {**LAYOUT, "total_area": 0.0}
        from backend.app.core.capabilities import effective_total_area

        out = _run(_FakeLLM(), layout=layout)
        b = _budget(out)
        assert b["area"] == pytest.approx(effective_total_area(layout))

    def test_降级解析被拒(self):
        layout = {"mode": "degraded_basic",
                  "rooms": [{"name": "Living", "type": "other", "area": 0.0}],
                  "walls": [], "doors": [], "windows": [], "total_area": 0.0}
        out = _run(_FakeLLM(), layout=layout)
        assert out["trace"][0]["ok"] is False

    def test_守卫异常带missing与suggestion(self):
        agent = BudgetAgent(llm=_FakeLLM())
        layout = {**LAYOUT, "total_area": 0.0,
                  "rooms": [{"name": "客厅", "type": "living_room", "area": 0.0}]}
        with pytest.raises(OperationNotAllowedError) as ei:
            asyncio.run(agent.run(_state(layout=layout)))
        payload = ei.value.to_payload()
        assert payload["operation"] == "estimate_budget"
        assert payload["missing"] == ["套内总面积"]
        assert payload["suggestion"]

    def test_无layout直接失败(self):
        out = _run(_FakeLLM(), layout=None)
        assert out["trace"][0]["ok"] is False
        assert "layout" in out["errors"][0]["message"]


# ══════════════════════════════════════════════════════════════════
# 提示词
# ══════════════════════════════════════════════════════════════════


class TestPrompt:
    def test_提示词含算好的数字不含计算要求(self):
        llm = _FakeLLM()
        _run(llm)
        prompt = llm.prompts[0]

        expected = engine.calculate(area=89.0, grade="economy")
        # 数字以便于阅读的形式给到模型
        assert f"{expected.total_min:,.0f}" in prompt
        assert "不要做任何计算" in prompt
        assert "不要修改任何金额" in prompt

    def test_提示词带分支定位(self):
        """
        带上 plan_id 与风格，三份解说词才会各自贴合自己的方案；
        否则同户型三个分支只差一个档位，解说词会高度雷同。
        """
        llm = _FakeLLM()
        _run(llm, branch_spec={"plan_id": "plan_nordic_medium", "style": "nordic",
                               "budget_grade": "medium", "index": 1})
        prompt = llm.prompts[0]
        assert "plan_nordic_medium" in prompt
        assert "北欧" in prompt

    def test_提示词带演示数据声明(self):
        llm = _FakeLLM()
        _run(llm)
        assert "演示" in llm.prompts[0]

    def test_提示词列出全部分项(self):
        llm = _FakeLLM()
        _run(llm)
        prompt = llm.prompts[0]
        expected = engine.calculate(area=89.0, grade="economy")
        for ln in expected.lines:
            assert ln.label in prompt, f"分项 {ln.label} 未出现在提示词中"


# ══════════════════════════════════════════════════════════════════
# 跨切面
# ══════════════════════════════════════════════════════════════════


class TestCrossCutting:
    def test_降级标记传上来(self):
        out = _run(_FakeLLM(degraded=True))
        assert out["degraded"] is True
        assert any("plan_modern_economy" in r for r in out["degrade_reasons"])

    def test_trace记录模型与耗时(self):
        out = _run(_FakeLLM())
        t = out["trace"][0]
        assert t["agent"] == "A-04"
        assert t["ok"] is True
        assert t["llm"]["model"] == "fake-model"

    def test_phase进入planning(self):
        assert _run(_FakeLLM())["phase"] == "planning"

    def test_写入plan_bundles的budget键(self):
        out = _run(_FakeLLM())
        assert "plan_modern_economy" in out["plan_bundles"]
        assert "budget" in out["plan_bundles"]["plan_modern_economy"]

    def test_缺少branch_spec时回退首项(self):
        state = _state(styles=["chinese"], budget_grades=["high"])
        state.pop("branch_spec")
        out = asyncio.run(BudgetAgent(llm=_FakeLLM()).execute(state))
        assert list(out["plan_bundles"]) == ["plan_chinese_high"]

    def test_超时阈值小于总超时(self):
        """
        写字超时必须**短于**节点总超时，否则慢模型会把数字一起拖走，
        独立超时就失去意义了。
        """
        assert NARRATIVE_TIMEOUT < BudgetAgent.timeout

    def test_纯计算耗时极短(self):
        """
        算数部分不涉及 IO，应当在毫秒级完成 —— 与动辄 10s 的 LLM 节点
        形成鲜明对比。这条断言防的是将来有人往引擎里加了 IO。
        """
        started = time.perf_counter()
        engine.calculate(area=89.0, grade="high")
        assert time.perf_counter() - started < 0.1
