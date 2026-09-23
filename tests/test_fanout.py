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

from backend.app.agents.budget_agent import BudgetAgent
from backend.app.agents.layout_diagnoser import LayoutDiagnoserAgent
from backend.app.agents.layout_parser import LayoutParserAgent
from backend.app.agents.material_agent import MaterialAgent
from backend.app.agents.risk_reviewer import RiskReviewAgent
from backend.app.agents.space_planner import SpacePlannerAgent
from backend.app.core.llm_client import LLMError, LLMResult
from backend.app.graph import workflow
from backend.app.graph.state import initial_state
from tests.helpers import floorplan_data_uri
from backend.app.schemas.budget import BudgetNarrative
from backend.app.schemas.layout import LayoutDiagnosis, LayoutSchema
from backend.app.schemas.material import MaterialPlan
from backend.app.schemas.plan import SpacePlan
from backend.app.schemas.risk import RiskReview
from backend.app.services.material import catalog

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


NARRATIVE = {
    "summary": "经济档预算。",
    "grade_rationale": "省在定制柜上。",
    "cost_drivers": ["主材"],
    "negotiation_tips": ["水电按实测结算"],
    "saving_tips": ["成品柜替代定制"],
    "warnings": [],
    "confidence": 0.7,
}

RISK = {
    "summary": "预算存在 5 类风险。",
    "findings": [
        {"risk_type": "计价陷阱", "severity": "high", "title": "管理费基数不对",
         "where": "管理费", "detail": "按含主材总价计取。",
         "suggestion": "改按施工费。", "source_ids": ["S1"]},
        {"risk_type": "合同条款风险", "severity": "high", "title": "水电无上限",
         "where": "水电", "detail": "只给单价。", "suggestion": "写明上限。",
         "source_ids": ["S2"]},
        {"risk_type": "漏项", "severity": "medium", "title": "垃圾清运未计",
         "where": "拆除", "detail": "推给业主。", "suggestion": "要求列入。",
         "source_ids": ["S9"]},
        {"risk_type": "模糊计量", "severity": "medium", "title": "防水规格不清",
         "where": "防水", "detail": "未写遍数。", "suggestion": "写清。",
         "source_ids": []},
        {"risk_type": "环保风险", "severity": "low", "title": "板材等级偏低",
         "where": "木工", "detail": "E1 而非 E0。", "suggestion": "升级 E0。",
         "source_ids": []},
    ],
    "overall_risk": "high",
    "negotiation_points": ["管理费改按施工费", "水电写明上限"],
    "data_gaps": [],
    "confidence": 0.7,
}


_BRANCH_TOKENS = ("plan_modern_economy", "plan_nordic_medium", "plan_chinese_high")

#: 每个档位能选中的商品 id。**必须是材料目录里对应档位真实存在的**，
#: 否则会被 A-05 判成幻觉商品剔除 —— 那正好是它该做的事，但会让这里的
#: 测试失去焦点。写成常量也顺便把「哪些 id 属于哪档」这件事固定下来。
_MATERIAL_BY_GRADE: dict[str, list[str]] = {
    "economy": ["QY-FL-101", "QY-PT-101"],
    "medium": ["QY-FL-101", "QY-DR-102"],
    "high": ["QY-FL-102", "QY-DR-102"],
}


def _material_payload(prompt: str) -> dict:
    """
    按提示词里的档位，返回该档位下合法的商品选择。

    假 LLM 也得"守规矩" —— 它挑的 id 必须真的在该档候选里。
    这正是 A-05 对真实模型的要求。
    """
    grade = "economy"
    for g in ("medium", "high", "economy"):
        if f"预算档位：{g}" in prompt:
            grade = g
            break

    index = catalog.by_id()
    choices = [
        {"category": index[pid].category, "product_id": pid,
         "reason": "全友自有产品、匹配需求"}
        for pid in _MATERIAL_BY_GRADE[grade]
    ]
    return {
        "summary": f"{grade} 档选材。",
        "choices": choices,
        "substitutions": [],
        "eco_note": "以 E0 级为主。",
        "warnings": [],
        "data_gaps": [],
        "confidence": 0.7,
    }


class _FanoutLLM:
    """
    可控的假 LLM。

    `delay` 用来验证并发性；`fail_on` / `fail_budget_on` 用来模拟单个分支失败——
    两个 Agent 都会把 plan_id 写进用户提示词，因此靠提示词内容区分分支。

    ⚠️ A-04 的**数字不经过这里**。它只找 LLM 要一段文字（BudgetNarrative），
    金额全部由规则引擎算好。所以下面 `fail_budget_on` 模拟的是"解说词写不出来"，
    此时预算数字仍然必须完好无损 —— 这正是要测的行为。
    """

    def __init__(self, *, delay: float = 0.0, fail_on: str | None = None,
                 fail_budget_on: str | None = None, fail_material_on: str | None = None,
                 fail_all_plans: bool = False,
                 fail_all_budgets: bool = False,
                 fail_all_materials: bool = False, layout: dict | None = None):
        self.delay = delay
        self.fail_on = fail_on
        self.fail_budget_on = fail_budget_on
        self.fail_material_on = fail_material_on
        self.fail_all_plans = fail_all_plans
        self.fail_all_budgets = fail_all_budgets
        self.fail_all_materials = fail_all_materials
        #: 覆盖 A-01 的产出。要让布局"缺东西"，必须改这里而不是 state——
        #: A-01 跑完会用它的产出整体覆盖 state 里的 layout。
        self.layout = layout if layout is not None else LAYOUT
        self.plan_calls: list[str] = []
        self.budget_calls: list[str] = []
        self.material_calls: list[str] = []
        self.review_calls: list[str] = []

    @staticmethod
    def _branch_of(user: str) -> str | None:
        for token in _BRANCH_TOKENS:
            if token in user:
                return token
        return None

    async def complete_json(self, schema, **kwargs):
        user = kwargs.get("user") or ""

        if schema is LayoutSchema:
            return schema.model_validate(self.layout), self._res()
        if schema is LayoutDiagnosis:
            return schema.model_validate(DIAGNOSIS), self._res()

        if schema is SpacePlan:
            if self.delay:
                await asyncio.sleep(self.delay)
            token = self._branch_of(user)
            if token:
                self.plan_calls.append(token)
                if self.fail_all_plans or token == self.fail_on:
                    raise LLMError(f"模拟分支 {token} 的空间规划失败")
            return schema.model_validate(SPACE_PLAN), self._res()

        if schema is BudgetNarrative:
            token = self._branch_of(user)
            if token:
                self.budget_calls.append(token)
                if self.fail_all_budgets or token == self.fail_budget_on:
                    raise LLMError(f"模拟分支 {token} 的预算解说失败")
            return schema.model_validate(NARRATIVE), self._res()

        if schema is MaterialPlan:
            token = self._branch_of(user)
            if token:
                self.material_calls.append(token)
                if self.fail_all_materials or token == self.fail_material_on:
                    raise LLMError(f"模拟分支 {token} 的选材失败")
            return schema.model_validate(_material_payload(user)), self._res()

        if schema is RiskReview:
            # A-06 是汇聚节点：一次审三套方案，每套一次调用。
            # 提示词里带的是方案正文而非 plan_id，所以这里不去识别分支。
            self.review_calls.append(user[:0] or "review")
            return schema.model_validate(RISK), self._res()

        raise AssertionError(f"未预期的 Schema: {schema}")

    def _res(self) -> LLMResult:
        return LLMResult(text="{}", provider="fake", model_used="fake-model",
                         prompt_tokens=100, completion_tokens=50, elapsed_ms=1)

    async def complete(self, **kwargs):  # pragma: no cover
        raise AssertionError("不应走纯文本路径")


def _stub_knowledge(monkeypatch):
    """
    给知识库检索打桩。

    A-06 是唯一走网络的 Agent（要调 Ollama 算查询向量），
    而 conftest 的 httpx 绊线会拦住它 —— 不打桩的话，本文件里所有
    工作流测试都会走"知识库不可用"分支，正常路径反而测不到。
    """
    from backend.app.services.knowledge import retriever

    chunks = [
        retriever.KnowledgeChunk(
            text="只有单价没有总价的项目，都是为了让合同价看起来低。",
            source="zhuangxiu-skills/装修报价审核/references/预算陷阱避坑.md",
            headings="四、装修预算只有单价坑你没商量",
            doc_type="avoid_pit", tags=["报价审核"], similarity=0.81,
        ),
        retriever.KnowledgeChunk(
            text="定金是付款的担保，具有法律约束力；订金一般视为预付款，可以退。",
            source="zhuangxiu-skills/装修合同审核/references/定金订金类.md",
            headings="一、定金和订金的法律区别",
            doc_type="avoid_pit", tags=["合同"], similarity=0.78,
        ),
    ]

    def _stub(queries, **kwargs):   # 接受 top_k_each / doc_type / max_total 等任何参数
        return retriever.RetrievalResult(
            query=" | ".join(queries) if isinstance(queries, list) else str(queries),
            chunks=list(chunks),
        )

    monkeypatch.setattr(retriever, "search_many", _stub)


def _patch_all(monkeypatch, llm):
    for node, cls in (
        ("parse_layout", LayoutParserAgent),
        ("diagnose_layout", LayoutDiagnoserAgent),
        ("generate_plan", SpacePlannerAgent),
        ("estimate_budget", BudgetAgent),
        ("select_materials", MaterialAgent),
        ("review_risks", RiskReviewAgent),
    ):
        monkeypatch.setitem(workflow._AGENTS, node, cls(llm=llm))
    _stub_knowledge(monkeypatch)


def _state(**over) -> dict:
    return initial_state(task_id="t-fanout", image_ref=floorplan_data_uri(), **over)


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

        # 1 套方案 × 3 个分支 Agent
        assert len(sends) == 3
        assert {s.node for s in sends} == set(workflow._BRANCH_NODES)
        for send in sends:
            assert send.arg["layout"] == LAYOUT, f"{send.node} 拿不到 layout"
            assert send.arg["diagnosis"] == DIAGNOSIS
            assert send.arg["branch_spec"]["plan_id"] == "plan_modern_economy"

    def test_每个Send是独立对象(self):
        """
        不能让多个 Send 共用同一个 payload dict —— LangGraph 内部处理时
        可能产生别名问题，而复制一份的成本可以忽略。
        """
        sends = workflow._fan_out_plans(_state(
            layout=LAYOUT, styles=["modern"], budget_grades=["economy"]))
        args = [id(s.arg) for s in sends]
        assert len(set(args)) == len(args), "存在共用 payload 的 Send"

    def test_payload不带无关字段(self):
        """
        payload 会随 checkpointer 序列化落盘，塞整份 state 会白白放大 N×M 倍。
        这里反向确认：图像 base64 这类大字段不该出现在分支 payload 里。
        """
        sends = workflow._fan_out_plans(_state(
            layout=LAYOUT, diagnosis=DIAGNOSIS,
            styles=["modern"], budget_grades=["economy"]))

        assert all("image_ref" not in s.arg for s in sends)


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

        assert len(llm.plan_calls) == 3, f"三个方案分支都应执行，实际 {llm.plan_calls}"
        assert len(out["plans"]) == 3
        # 9 个任务（3 方案 × 3 Agent）并发；A-03 每个睡 0.25s，串行要 0.75s
        assert elapsed < 0.6, f"分支疑似串行执行，耗时 {elapsed:.2f}s"


# ══════════════════════════════════════════════════════════════════
# 分支隔离
# ══════════════════════════════════════════════════════════════════


class TestBranchIsolation:
    def test_单分支空间规划失败不影响其余(self, monkeypatch):
        """A-03 挂在某套方案上，其余两套照常交付。"""
        out = _run(monkeypatch, _FanoutLLM(fail_on="plan_nordic_medium"))

        assert len(out["plans"]) == 3, "失败的方案**仍应列出**，只是缺 space_plan"
        by_id = {p["plan_id"]: p for p in out["plans"]}
        assert by_id["plan_nordic_medium"]["space_plan"] is None
        assert by_id["plan_modern_economy"]["space_plan"] is not None

        failed = [e for e in out["errors"] if e["agent"] == "A-03"]
        assert len(failed) == 1
        assert "plan_nordic_medium" in failed[0]["message"]

    def test_空间规划失败时其余产物仍在(self, monkeypatch):
        """
        这是本文件最重要的一条 —— 分支内各 Agent **互相独立**。

        A-03 失败不该连累 A-04/A-05：预算只需要面积和档位，选材只需要房间与需求，
        都不依赖空间规划。旧实现按 space_plan 是否存在来收集方案，
        这种分支会被整个丢掉，连带把已经算好的预算一起扔了。
        """
        out = _run(monkeypatch, _FanoutLLM(fail_on="plan_nordic_medium"))
        plan = next(p for p in out["plans"] if p["plan_id"] == "plan_nordic_medium")

        assert plan["budget"] is not None, "A-03 失败不该连带丢掉预算"
        assert len(plan["budget"]["lines"]) >= 7
        assert plan["materials"] is not None, "A-03 失败不该连带丢掉选材"
        assert plan["missing_artifacts"] == ["space_plan"]

    def test_预算解说失败时数字仍在(self, monkeypatch):
        """
        A-04 的 LLM 只负责写解说词。它挂了，**数字必须完好无损** ——
        数字来自规则引擎，压根不经过模型。

        这条守住的是本 Agent 最核心的设计：失败方向是反的。
        """
        out = _run(monkeypatch, _FanoutLLM(fail_budget_on="plan_modern_economy"))
        plan = next(p for p in out["plans"] if p["plan_id"] == "plan_modern_economy")

        assert plan["budget"] is not None
        assert plan["budget"]["total_min"] > 0, "解说失败不该影响金额"
        assert plan["budget"]["computed_by"].startswith("rule_engine")
        assert plan["budget"]["narrative_degraded"] is True, "应标记为降级"
        # 回退文案要如实说明缺了什么，而不是假装正常
        assert plan["budget"]["narrative"]["warnings"]

    def test_部分产出时对比表仍可用(self, monkeypatch):
        out = _run(monkeypatch, _FanoutLLM(fail_on="plan_nordic_medium"))
        comp = out["comparison"]

        assert comp["available"] is True
        assert comp["plan_count"] == 3
        assert len(comp["rows"]) == 3
        # 缺产物要显式标出，不能让字段静默为空
        row = next(r for r in comp["rows"] if r["plan_id"] == "plan_nordic_medium")
        assert row["missing_artifacts"] == ["space_plan"]
        assert any("只产出了部分内容" in n for n in comp["notes"])

    def test_部分Agent全失败时方案仍保留(self, monkeypatch):
        """
        只要还有一个 Agent 有产出，这套方案就留在列表里。

        注意 fail_all_budgets / fail_all_materials 只让**文字部分**失败，
        A-04 的数字与 A-05 的候选 id 照常产出 —— 所以方案**仍应保留**，
        这是设计如此，不是缺陷。真正让它们整个失败的只有守卫不通过或引擎异常。
        """
        out = _run(monkeypatch, _FanoutLLM(
            fail_all_plans=True, fail_all_budgets=True, fail_all_materials=True))

        # A-03 全挂，但 A-04 的预算还在
        assert len(out["plans"]) == 3
        for plan in out["plans"]:
            assert plan["space_plan"] is None
            assert plan["budget"]["total_min"] > 0, "解说失败不该影响金额"
            assert plan["missing_artifacts"] == ["space_plan"]

    def test_分支完全无产出时方案不列出(self):
        """
        直接测 fan-in 的契约：plan_bundles 为空时，不编造方案行。

        这是"方案消失"的唯一条件，用纯函数测最精确 ——
        走整图反而要靠桩节点才构造得出这个状态。
        """
        out = workflow.aggregate_plans(_state(plan_bundles={}))

        assert out["plans"] == []
        assert out["comparison"]["available"] is False
        assert out["comparison"]["plan_count"] == 0
        assert out["degraded"] is True
        assert any("fan-in" in r for r in out["degrade_reasons"])
        assert out["errors"], "无产出必须记错误，不能静默返回空"

    def test_全部失败时图不崩(self, monkeypatch):
        """这是最要紧的一条：0 套方案是业务失败，不是系统崩溃。"""
        out = _run(monkeypatch, _FanoutLLM(fail_all_plans=True, fail_all_budgets=True))
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

    def test_对比表含预算列(self, monkeypatch):
        """对比表要能一眼看出三套方案的预算差距——这是用户最关心的对比维度。"""
        out = _run(monkeypatch, _FanoutLLM())
        rows = out["comparison"]["rows"]

        totals = [r["budget_total_min"] for r in rows]
        assert all(t and t > 0 for t in totals), f"每行都应有预算下限，实际 {totals}"
        # 经济 < 中档 < 高端
        assert totals[0] < totals[1] < totals[2], f"预算未随档位递增: {totals}"
        assert all(r["budget_computed_by"].startswith("rule_engine") for r in rows)

    def test_对比表字段含数据质量信号(self, monkeypatch):
        out = _run(monkeypatch, _FanoutLLM())
        row = out["comparison"]["rows"][0]
        assert "unassigned_rooms" in row
        assert "invented_rooms" in row
        assert "missing_artifacts" in row

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
        """3 套方案 × 3 个分支 Agent = 9 个 Send。"""
        result = workflow._route_after_diagnosis(_state(layout=LAYOUT, diagnosis=DIAGNOSIS))
        assert isinstance(result, list)
        assert len(result) == 3 * len(workflow._BRANCH_NODES)
        assert {s.node for s in result} == set(workflow._BRANCH_NODES)

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

    def test_同一plan_id下四个Agent的产物共存(self, monkeypatch):
        """
        ⚠️ 回归防线 —— 一个曾经静默丢数据的 bug。

        分支内三个 Agent 会**并发写同一个 plan_id**：
            A-03 → {"plan_x": {"space_plan": …}}
            A-04 → {"plan_x": {"budget":     …}}
            A-05 → {"plan_x": {"materials":  …}}

        MergeDict 原先是**浅合并**，实测结果是只剩最后写的那一个 ——
        另外两份被整个顶掉，**不报错、不告警**，用户拿到一份残缺的"方案"。
        分支内 Agent 越多，丢得越多。

        这条测试与 `test_每套方案同时含三种产物` 一起守住这个行为。
        """
        out = _run(monkeypatch, _FanoutLLM())

        for plan_id, bundle in out["plan_bundles"].items():
            assert set(bundle) == set(workflow.BRANCH_ARTIFACTS), (
                f"{plan_id} 的产物被覆盖了，只剩 {set(bundle)}"
            )

    def test_phase与degraded并发写入不报错(self, monkeypatch):
        """
        degraded / phase 是标量，没有 reducer 时多个分支同时写会直接报错。
        前者用 OrBool，后者用 LastWrite（见 state.py）。
        """
        out = _run(monkeypatch, _FanoutLLM())
        assert out["phase"] == "finalizing"   # fan-in 在分支之后写，应当胜出
        assert out["degraded"] is False
