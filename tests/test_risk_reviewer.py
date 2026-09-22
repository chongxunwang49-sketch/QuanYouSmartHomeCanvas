"""
A-06 RiskReviewAgent 测试。

本文件守的核心命题：**每一条风险要么能追溯到具体语料，要么明确没有来源 ——
不存在"看起来像引用但查不到"的中间态。**

AC-06 要求「识别 ≥5 类风险，**引用知识库来源**」。如果引用是模型自由写的，
那条引用不可验证 —— 面试官问"这规则出自哪里"就答不上来，更糟的是模型
可能编一个像模像样的来源，而用户以为它查证过。

所以引用走**指认**而非**生成**：提示词把 chunk 编号成 [S1][S2]…，
模型只能回 `source_ids`，代码再映射回真实出处。这组测试就是验证
"指认错了会被抓住"（`TestCitationMapping`）。

另外守两条与 A-02 同源的纪律：
  - 知识库不可用时**如实说没有依据**，而不是凭常识编规则
  - 没发现问题就如实说没有，**不为凑数编造风险**
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.app.agents.risk_reviewer import REVIEW_TIMEOUT, RiskReviewAgent
from backend.app.core.llm_client import LLMError, LLMResult
from backend.app.graph.state import initial_state
from backend.app.schemas.risk import RiskReview
from backend.app.services.knowledge import retriever

ROOT = Path(__file__).resolve().parent.parent

# ══════════════════════════════════════════════════════════════════
# 样本
# ══════════════════════════════════════════════════════════════════

QUOTE = """# 报价单
| 序号 | 项目 | 单位 | 数量 | 单价(元) | 合计(元) |
|---|---|---|---|---|---|
| 2-1 | 电路改造 | 米 | —— | 15 | 按实结算 |
| 3-2 | 瓷砖倒角 | 米 | —— | 20 | 按实结算 |
合同签订时需支付定金 20000 元，定金不予退还。
"""

BUDGET = {
    "grade": "economy",
    "area": 89.0,
    "total_min": 73648.0,
    "total_max": 104093.0,
    "price_per_sqm_min": 828.0,
    "price_per_sqm_max": 1170.0,
    "lines": [
        {"key": "plumbing_electrical", "label": "水电改造", "unit": "元/㎡",
         "basis": "area", "quantity": 89.0, "unit_price_min": 105,
         "unit_price_max": 145, "amount_min": 9345.0, "amount_max": 12905.0,
         "note": "隐蔽工程，报价单上最容易被做增项的一项。"},
        {"key": "management", "label": "管理费", "unit": "占施工费比例 %",
         "basis": "subtotal_pct", "quantity": 5.0, "unit_price_min": 5.0,
         "unit_price_max": 5.0, "amount_min": 3458.0, "amount_max": 4887.0,
         "note": "按施工费计取。"},
    ],
}


def _chunk(i: int, source: str = "", headings: str = "") -> retriever.KnowledgeChunk:
    return retriever.KnowledgeChunk(
        text=f"知识片段 {i}：只有单价没有总价的项目，都是为了让合同价看起来低。",
        source=source or f"zhuangxiu-skills/装修报价审核/references/ref-{i}.md",
        headings=headings or f"第 {i} 节",
        similarity=0.8 - i * 0.01,
    )


class _FakeRetrieval:
    """可控的检索结果。"""

    def __init__(self, n_chunks: int = 3, available: bool = True, reason: str = ""):
        self.n = n_chunks
        self.available = available
        self.reason = reason
        self.queries: list[list[str]] = []

    def install(self, monkeypatch):
        # 接受 top_k_each / doc_type / max_total 等任何参数 ——
        # 桩的签名若与真实函数不一致，加参数时会在**被测代码里**炸 TypeError，
        # 而错误现场离"桩没跟上"这个根因很远。
        def _stub(queries, **kwargs):
            self.queries.append(list(queries))
            chunks = [_chunk(i) for i in range(1, self.n + 1)]
            return retriever.RetrievalResult(
                query=" | ".join(queries), chunks=chunks,
                available=self.available, reason=self.reason,
            )
        monkeypatch.setattr(retriever, "search_many", _stub)
        return self


def _finding(**over) -> dict:
    base = {
        "risk_type": "计价陷阱", "severity": "high", "title": "只有单价没有总价",
        "where": "2-1 电路改造", "detail": "后期按实结算容易超支。",
        "suggestion": "要求写明单价上限。", "source_ids": ["S1"],
    }
    base.update(over)
    return base


def _review(findings: list[dict] | None = None, **over) -> dict:
    base = {
        "summary": "整体风险较高。",
        "findings": findings if findings is not None else [_finding()],
        "overall_risk": "high",
        "negotiation_points": ["要求写明单价上限"],
        "data_gaps": [],
        "confidence": 0.7,
    }
    base.update(over)
    return base


class _FakeLLM:
    def __init__(self, payload: dict | None = None, *, mode: str = "ok",
                 delay: float = 0.0, degraded: bool = False):
        self.payload = payload if payload is not None else _review()
        self.mode = mode
        self.delay = delay
        self.degraded = degraded
        self.prompts: list[str] = []

    async def complete_json(self, schema, **kwargs):
        assert schema is RiskReview
        self.prompts.append(kwargs.get("user") or "")
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "raise":
            raise LLMError("模拟审查模型不可用")
        return schema.model_validate(self.payload), LLMResult(
            text="{}", provider="fake", model_used="fake-model",
            degraded=self.degraded,
            degrade_reason="本地兜底" if self.degraded else None,
            prompt_tokens=2000, completion_tokens=600, elapsed_ms=7000,
        )

    async def complete(self, **kwargs):  # pragma: no cover
        raise AssertionError("A-06 不应走纯文本路径")


def _state(**over) -> dict:
    base = {"task_id": "t-a06"}
    base.update(over)
    return initial_state(**base)


def _quote_out(llm: _FakeLLM, **over) -> dict:
    return asyncio.run(RiskReviewAgent(llm=llm).execute(_state(quote_text=QUOTE, **over)))


def _plan_state() -> dict:
    return _state(plan_bundles={"plan_x": {"budget": BUDGET}})


# ══════════════════════════════════════════════════════════════════
# 引用映射 —— 本文件最重要的一组
# ══════════════════════════════════════════════════════════════════


class TestCitationMapping:
    def test_有效编号映射成真实出处(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        out = _quote_out(_FakeLLM(_review([_finding(source_ids=["S1", "S2"])])))
        f = out["risk_review"]["findings"][0]

        assert len(f["citations"]) == 2
        assert f["has_source"] is True
        # 出处必须来自**真实 chunk 的路径与章节**，不是模型写的字符串。
        # 注意 citation 会剥掉 `zhuangxiu-skills/` 这个共同前缀（出处要短好读），
        # 所以这里断言的是文件名与章节，而不是完整路径。
        assert any("ref-1" in c for c in f["citations"])
        assert any("第 1 节" in c for c in f["citations"])
        assert out["risk_review"]["invented_citations"] == []

    def test_编造的编号被剔除并记录(self, monkeypatch):
        """模型写了 S9，而检索只返回了 3 条 —— 必须抓出来。"""
        _FakeRetrieval(3).install(monkeypatch)
        out = _quote_out(_FakeLLM(_review([_finding(source_ids=["S1", "S9", "S99"])])))
        r = out["risk_review"]

        assert r["findings"][0]["citations"] == [r["findings"][0]["citations"][0]]
        assert set(r["invented_citations"]) == {"S9", "S99"}
        assert any("不存在的来源编号" in g for g in r["data_gaps"])

    def test_留空编号是允许的(self, monkeypatch):
        """
        常识性判断可以没有依据 —— **留空不算错**。
        这条很重要：若把"没有引用"也判成违规，模型会倾向于编一个来源来交差。
        """
        _FakeRetrieval(3).install(monkeypatch)
        out = _quote_out(_FakeLLM(_review([_finding(source_ids=[])])))
        r = out["risk_review"]

        assert r["findings"][0]["has_source"] is False
        assert r["invented_citations"] == []
        # 但要如实告知有多少条没有依据
        assert any("没有知识库依据" in g for g in r["data_gaps"])

    def test_小写与空格编号也能识别(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        out = _quote_out(_FakeLLM(_review([_finding(source_ids=[" s1 ", "S2"])])))
        assert len(out["risk_review"]["findings"][0]["citations"]) == 2

    def test_依据原文被带上便于核对(self, monkeypatch):
        """前端要能展开看原文，人工也要能核对"这条判断是不是真有出处"。"""
        _FakeRetrieval(3).install(monkeypatch)
        out = _quote_out(_FakeLLM(_review([_finding(source_ids=["S1"])])))
        assert out["risk_review"]["findings"][0]["source_excerpt"]

    def test_重复引用同一来源不产生重复出处(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        out = _quote_out(_FakeLLM(_review([_finding(source_ids=["S1", "S1"])])))
        assert len(out["risk_review"]["findings"][0]["citations"]) == 1


# ══════════════════════════════════════════════════════════════════
# AC-06 —— 类型数可数
# ══════════════════════════════════════════════════════════════════


class TestAC06:
    def _five_types(self) -> list[dict]:
        types = ["计价陷阱", "合同条款风险", "漏项", "模糊计量", "环保风险"]
        return [_finding(risk_type=t, title=f"问题{i}", source_ids=["S1"])
                for i, t in enumerate(types)]

    def test_五类达标(self, monkeypatch):
        """AC-06 的门槛是「≥5 **类**」，不是「≥5 条」。"""
        _FakeRetrieval(3).install(monkeypatch)
        out = _quote_out(_FakeLLM(_review(self._five_types())))
        r = out["risk_review"]

        assert r["finding_count"] == 5
        assert r["distinct_type_count"] == 5
        assert r["ac06_met"] is True

    def test_同类型重复不算多类(self, monkeypatch):
        """
        这是本组测试存在的理由：10 条同类型的风险**不能**算作识别了 10 类。
        没有这条断言，AC-06 可以被"把一个问题拆成十条"刷过去。
        """
        _FakeRetrieval(3).install(monkeypatch)
        findings = [_finding(risk_type="增项风险", title=f"问题{i}") for i in range(10)]
        out = _quote_out(_FakeLLM(_review(findings)))
        r = out["risk_review"]

        assert r["finding_count"] == 10
        assert r["distinct_type_count"] == 1
        assert r["ac06_met"] is False, "10 条同类型不能算 5 类"

    def test_未达标不抛错只如实标记(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        out = _quote_out(_FakeLLM(_review([_finding()])))
        assert out["risk_review"]["ac06_met"] is False
        assert out["trace"][0]["ok"] is True, "未达标不是运行失败"

    def test_类型清单可读(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        out = _quote_out(_FakeLLM(_review(self._five_types())))
        assert set(out["risk_review"]["distinct_risk_types"]) == {
            "计价陷阱", "合同条款风险", "漏项", "模糊计量", "环保风险"}


# ══════════════════════════════════════════════════════════════════
# 知识库不可用 —— 如实说，不编
# ══════════════════════════════════════════════════════════════════


class TestKnowledgeUnavailable:
    def test_不可用时明确告知模型(self, monkeypatch):
        """
        ⚠️ 关键：必须**明确告诉模型**没有依据。
        否则它会以为【知识库依据】那节为空是"没有问题"，转而凭常识编。
        """
        _FakeRetrieval(0, available=False, reason="Ollama 未启动").install(monkeypatch)
        llm = _FakeLLM()
        out = _quote_out(llm)

        assert "知识库不可用" in llm.prompts[0]
        assert "Ollama 未启动" in llm.prompts[0]
        assert out["degraded"] is True

    def test_不可用时产出仍完整但标记降级(self, monkeypatch):
        _FakeRetrieval(0, available=False, reason="连接失败").install(monkeypatch)
        out = _quote_out(_FakeLLM())
        r = out["risk_review"]

        assert r["findings"], "库挂了也照常审查，只是没有引用"
        assert r["knowledge_available"] is False
        assert any("未取得知识库依据" in g for g in r["data_gaps"])
        assert r["confidence"] <= 0.4, "缺依据时要主动压低置信度"
        assert r["disclaimer"]

    def test_检索为空但可用时也如实说(self, monkeypatch):
        """库是好的，只是没查到相关内容 —— 与"库挂了"要区分开。"""
        _FakeRetrieval(0, available=True).install(monkeypatch)
        out = _quote_out(_FakeLLM())
        assert any("没有检索到相关内容" in g for g in out["risk_review"]["data_gaps"])

    def test_检索异常不向上抛(self, monkeypatch):
        """retriever.search_many 抛异常时，A-06 不该跟着崩。"""
        def _boom(*a, **kw):
            raise RuntimeError("向量库炸了")
        monkeypatch.setattr(retriever, "search_many", _boom)

        out = _quote_out(_FakeLLM())
        assert out["trace"][0]["ok"] is False
        assert "向量库炸了" in out["errors"][0]["message"]


# ══════════════════════════════════════════════════════════════════
# 模型失败
# ══════════════════════════════════════════════════════════════════


class TestLLMFailure:
    def test_模型抛错时如实报空(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        out = _quote_out(_FakeLLM(mode="raise"))
        r = out["risk_review"]

        assert r["findings"] == []
        assert r["confidence"] == 0.0
        assert any("审查未完成" in g for g in r["data_gaps"])

    def test_模型超时走独立阈值(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        monkeypatch.setattr("backend.app.agents.risk_reviewer.REVIEW_TIMEOUT", 0.05)
        out = _quote_out(_FakeLLM(delay=0.3))
        assert any("超时" in g for g in out["risk_review"]["data_gaps"])

    def test_审查超时阈值小于节点超时(self):
        assert REVIEW_TIMEOUT < RiskReviewAgent.timeout

    def test_降级标记透传(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        out = _quote_out(_FakeLLM(degraded=True))
        assert out["degraded"] is True
        assert any("本地文本模型" in r for r in out["degrade_reasons"])


# ══════════════════════════════════════════════════════════════════
# 两种模式
# ══════════════════════════════════════════════════════════════════


class TestModes:
    def test_有报价单走quote模式(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        llm = _FakeLLM()
        out = _quote_out(llm)
        assert out["risk_review"]["subject_kind"] == "quote"
        assert "报价单" in llm.prompts[0]

    def test_无报价单走plan模式(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        llm = _FakeLLM()
        out = asyncio.run(RiskReviewAgent(llm=llm).execute(_plan_state()))
        assert out["plan_bundles"]["plan_x"]["risks"]["subject_kind"] == "plan"
        assert "预算" in llm.prompts[0]

    def test_plan模式只审有预算的方案(self, monkeypatch):
        """
        没有预算的方案不该被审 —— 硬审会变成让模型对着一份空表编风险。
        """
        _FakeRetrieval(3).install(monkeypatch)
        state = _state(plan_bundles={
            "plan_a": {"budget": BUDGET},
            "plan_b": {"space_plan": {"summary": "只有规划"}},   # 无预算
        })
        out = asyncio.run(RiskReviewAgent(llm=_FakeLLM()).execute(state))
        assert set(out["plan_bundles"]) == {"plan_a"}

    def test_plan模式审查文本含分项与选材(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        state = _state(plan_bundles={"plan_x": {
            "budget": BUDGET,
            "materials": {"items": [{"category": "floor", "name": "全友地板",
                                     "brand": "全友", "price_range": [89, 159],
                                     "unit": "元/㎡", "eco_level": "E0"}]},
        }})
        llm = _FakeLLM()
        asyncio.run(RiskReviewAgent(llm=llm).execute(state))
        prompt = llm.prompts[0]

        assert "水电改造" in prompt
        assert "管理费" in prompt
        assert "全友地板" in prompt, "选材信息也该纳入审查视野"

    def test_多套方案各审一次并发(self, monkeypatch):
        _FakeRetrieval(2).install(monkeypatch)
        state = _state(plan_bundles={
            "p1": {"budget": BUDGET}, "p2": {"budget": BUDGET},
            "p3": {"budget": BUDGET},
        })
        llm = _FakeLLM()
        out = asyncio.run(RiskReviewAgent(llm=llm).execute(state))
        assert len(llm.prompts) == 3
        assert set(out["plan_bundles"]) == {"p1", "p2", "p3"}
        assert all(b["risks"]["finding_count"] == 1 for b in out["plan_bundles"].values())

    def test_两者都无时报错(self):
        out = asyncio.run(RiskReviewAgent(llm=_FakeLLM()).execute(_state()))
        assert out["trace"][0]["ok"] is False
        assert "无内容可审" in out["errors"][0]["message"]

    def test_有方案但都无预算时报错(self):
        state = _state(plan_bundles={"p1": {"space_plan": {}}})
        out = asyncio.run(RiskReviewAgent(llm=_FakeLLM()).execute(state))
        assert out["trace"][0]["ok"] is False
        assert "没有预算" in out["errors"][0]["message"]


# ══════════════════════════════════════════════════════════════════
# 排序与提示词
# ══════════════════════════════════════════════════════════════════


class TestOrderingAndPrompt:
    def test_按严重度排序(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        findings = [
            _finding(severity="low", title="低"),
            _finding(severity="high", title="高"),
            _finding(severity="medium", title="中"),
        ]
        out = _quote_out(_FakeLLM(_review(findings)))
        assert [f["severity"] for f in out["risk_review"]["findings"]] == [
            "high", "medium", "low"
        ]

    def test_提示词带编号来源(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        llm = _FakeLLM()
        _quote_out(llm)
        prompt = llm.prompts[0]

        for i in (1, 2, 3):
            assert f"[S{i}]" in prompt
        assert "只能取自" in prompt or "只能从给出的编号里选" in prompt

    def test_提示词含报价单原文(self, monkeypatch):
        _FakeRetrieval(3).install(monkeypatch)
        llm = _FakeLLM()
        _quote_out(llm)
        assert "电路改造" in llm.prompts[0]
        assert "定金" in llm.prompts[0]

    def test_抽分项名做补充查询(self, monkeypatch):
        """
        主题查询覆盖通用套路，但抽不出"这份报价单特有的项目"。
        两者合起来召回更全 —— 多几次检索的成本远低于一次漏判。
        """
        fr = _FakeRetrieval(3).install(monkeypatch)
        _quote_out(_FakeLLM())
        queries = fr.queries[0]
        assert any("电路改造" in q for q in queries), "应从报价单里抽出分项名"
        assert any("倒角" in q for q in queries)

    def test_plan模式从预算分项派生查询(self, monkeypatch):
        fr = _FakeRetrieval(3).install(monkeypatch)
        asyncio.run(RiskReviewAgent(llm=_FakeLLM()).execute(_plan_state()))
        assert any("水电改造" in q for q in fr.queries[0])
        assert any("管理费" in q for q in fr.queries[0])


# ══════════════════════════════════════════════════════════════════
# 样本报价单的答案文件 —— 保证 AC-06 可量化
# ══════════════════════════════════════════════════════════════════


class TestSampleQuoteGroundTruth:
    """
    这组不调模型，只校验**答案文件本身是自洽的**。

    有了带标注的样本，AC-06 才能从"模型说它找到了 5 类"变成
    "模型找到了我们埋进去的哪几条" —— 那是可量化的。
    """

    @pytest.fixture(scope="class")
    def expected(self) -> dict:
        p = ROOT / "seed_data" / "sample_quotes" / "quote_01_economy.expected.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def test_样本报价单存在(self):
        p = ROOT / "seed_data" / "sample_quotes" / "quote_01_economy.md"
        assert p.exists()
        assert "演示数据" in p.read_text(encoding="utf-8")

    def test_埋点数量与类型满足AC06(self, expected):
        planted = expected["planted"]
        assert len(planted) >= 10, "埋点太少，测不出召回率"
        types = {p["risk_type"] for p in planted}
        assert len(types) >= 5, f"埋点类型 {len(types)} 类，不足 AC-06 的 5 类"

    def test_埋点id不重复(self, expected):
        ids = [p["id"] for p in expected["planted"]]
        assert len(ids) == len(set(ids))

    def test_可核验埋点确实成立(self, expected):
        """
        ⚠️ 价格异常必须**客观可核验** —— 锚定在 material_catalog.json 上，
        否则"单价偏高"就只是主观判断，答案文件也就没有权威性。

        判据用机器可读的 `evidence`，不去猜文本 —— 从描述里正则找 product_id
        既脆又容易在改文案时静默失效。
        """
        from backend.app.services.material import catalog

        index = catalog.by_id()
        verifiable = [p for p in expected["planted"] if p.get("verifiable")]
        assert verifiable, "至少要有一条可客观核验的埋点"

        for p in verifiable:
            ev = p.get("evidence") or {}
            pid, quoted = ev.get("product_id"), ev.get("quoted_price")
            assert pid in index, f"{p['id']} 的 evidence.product_id 不存在: {pid}"
            assert isinstance(quoted, (int, float)), f"{p['id']} 缺 evidence.quoted_price"

            _lo, hi = index[pid].price_range
            assert quoted > hi, (
                f"{p['id']} 声称单价异常，但 {quoted} 并未高于 {pid} 的目录上限 {hi} —— "
                f"答案文件与材料目录不一致"
            )

    def test_报告的价格区间与目录一致(self, expected):
        """desc 里写的区间必须真的是目录里的区间，否则答案会误导人工核对。"""
        from backend.app.services.material import catalog

        index = catalog.by_id()
        for p in expected["planted"]:
            ev = p.get("evidence") or {}
            pid = ev.get("product_id")
            if not pid:
                continue
            lo, hi = index[pid].price_range
            # 去掉千分位再比 —— desc 是给人读的，写成 1,080 比 1080 自然，
            # 但那样数字就断成两截，所以判据要归一化而不是要求文案迁就测试
            flat = p["desc"].replace(",", "")
            for bound in (lo, hi):
                assert f"{bound:g}" in flat, (
                    f"{p['id']} 的 desc 未提及 {pid} 的区间端点 {bound:g}"
                )

    def test_汇总口径一致(self, expected):
        s = expected["expected_summary"]
        assert s["total_planted"] == len(expected["planted"])
        counted = sum(s["by_risk_type"].values())
        assert counted == s["total_planted"], "分类计数与总数不符"
        assert s["distinct_risk_types"] == len(s["by_risk_type"])

    def test_通过阈值不低于AC06(self, expected):
        assert expected["expected_summary"]["pass_threshold"]["distinct_types"] >= 5
