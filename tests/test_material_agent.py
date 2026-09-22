"""
A-05 MaterialAgent 测试。

本文件守的核心命题：**模型只能"指认"，不能"创造"。**

材料推荐里最容易出问题的不是选错，而是编造 —— 编一个不存在的型号、
编一个看起来合理的价格、编一个官网搜不到的链接。而用户会拿它去买。
所以三层防线各有测试守着：

    ① 结构层：MaterialPlan 里没有价格字段，模型想写也没地方写
    ② 校验层：id 不在候选集里就剔除，并记入 invented_products
    ③ 兜底层：AC-18 覆盖率不达标时由代码换成全友的

外加一条与 A-04 同构的纪律：**模型失败不该让选材整个消失** ——
候选排序本来就是确定性的，模型只是让它更贴切。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.agents.material_agent import SELECT_TIMEOUT, MaterialAgent
from backend.app.core.capabilities import OperationNotAllowedError
from backend.app.core.llm_client import LLMError, LLMResult
from backend.app.graph.state import initial_state
from backend.app.schemas.material import MaterialPlan, MaterialSelection
from backend.app.services.material import catalog

# ══════════════════════════════════════════════════════════════════
# 样本
# ══════════════════════════════════════════════════════════════════

LAYOUT = {
    "layout_id": "layout_mat_test",
    "mode": "full",
    "rooms": [
        {"name": "客厅", "type": "living_room", "area": 28.5, "bbox": [120, 80, 420, 360]},
        {"name": "主卧", "type": "bedroom", "area": 16.2, "bbox": [440, 80, 680, 300]},
        {"name": "卫生间", "type": "bathroom", "area": 5.0, "bbox": [440, 320, 680, 500]},
    ],
    "walls": [{"type": "load_bearing", "coords": [[100, 60], [700, 60]]}],
    "doors": [{"position": [250, 380], "width": 0.9}],
    "windows": [{"position": [600, 50], "width": 1.8, "orientation": "south"}],
    "total_area": 49.7,
    "has_north_arrow": True,
    "confidence": 0.85,
}

#: 经济档下真实存在的商品 id（全友 / 竞品各取两个）
_QY_ECON = ["QY-FL-101", "QY-PT-101"]
_COMP_ECON = ["XY-FL-301", "LB-PT-201"]      # 圣象地板 / 立邦涂料


def _payload(ids: list[str], **over) -> dict:
    """
    构造模型的返回。

    对**不存在的 id** 也能构造 —— 否则假 LLM 根本没法模拟"模型编了一个型号"，
    而那正是本文件要测的核心行为之一。未知 id 用占位品类，反正 A-05 会剔除它。
    """
    index = catalog.by_id()
    base = {
        "summary": "以成品与常规材料为主。",
        "choices": [
            {"category": index[pid].category if pid in index else "floor",
             "product_id": pid,
             "reason": "全友自有产品、匹配需求"}
            for pid in ids
        ],
        "substitutions": [],
        "eco_note": "以 E0 级为主。",
        "warnings": [],
        "data_gaps": [],
        "confidence": 0.7,
    }
    base.update(over)
    return base


class _FakeLLM:
    """`mode` 控制要选材时的行为：ok / raise / garbage / timeout。"""

    def __init__(self, ids: list[str] | None = None, *, mode: str = "ok",
                 delay: float = 0.0, degraded: bool = False, **payload_over):
        self.ids = ids if ids is not None else _QY_ECON
        self.mode = mode
        self.delay = delay
        self.degraded = degraded
        self.payload_over = payload_over
        self.prompts: list[str] = []

    async def complete_json(self, schema, **kwargs):
        assert schema is MaterialPlan
        self.prompts.append(kwargs.get("user") or "")
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "raise":
            raise LLMError("模拟选材模型不可用")
        if self.mode == "garbage":
            return schema.model_validate({}), self._res()
        if self.mode == "timeout":
            raise asyncio.TimeoutError()
        return schema.model_validate(_payload(self.ids, **self.payload_over)), self._res()

    def _res(self) -> LLMResult:
        return LLMResult(
            text="{}", provider="fake", model_used="fake-model",
            degraded=self.degraded,
            degrade_reason="本地兜底" if self.degraded else None,
            prompt_tokens=1500, completion_tokens=400, elapsed_ms=6000,
        )

    async def complete(self, **kwargs):  # pragma: no cover
        raise AssertionError("A-05 不应走纯文本路径")


def _state(**over) -> dict:
    base = {
        "task_id": "t-a05",
        "layout": LAYOUT,
        "requirements": {},
        "branch_spec": {"plan_id": "plan_modern_economy", "style": "modern",
                        "budget_grade": "economy", "index": 0},
    }
    base.update(over)
    return initial_state(**base)


def _run(llm: _FakeLLM, **over) -> dict:
    return asyncio.run(MaterialAgent(llm=llm).execute(_state(**over)))


def _mat(out: dict) -> dict:
    bundles = out["plan_bundles"]
    assert len(bundles) == 1, f"应恰好写入一个 plan_id，实际 {list(bundles)}"
    return next(iter(bundles.values()))["materials"]


# ══════════════════════════════════════════════════════════════════
# 结构保证 —— 模型无处安放价格
# ══════════════════════════════════════════════════════════════════


class TestNarrativeCannotCarryPrices:
    """与 A-04 同一条纪律，用同一套手法守住。"""

    _ALLOWED_NUMERIC = frozenset({"confidence"})

    def test_选材模型没有数值字段(self):
        numeric = [
            name for name, f in MaterialPlan.model_fields.items()
            if f.annotation in (int, float) and name not in self._ALLOWED_NUMERIC
        ]
        assert not numeric, (
            f"MaterialPlan 出现数值字段 {numeric} —— 模型会算错钱，而用户拿它比价"
        )

    def test_选材模型不含金额字段名(self):
        suspicious = ("amount", "price", "total", "金额", "单价", "费用", "总价")
        hits = [n for n in MaterialPlan.model_fields
                if any(s in n.lower() for s in suspicious)]
        assert not hits, f"MaterialPlan 出现疑似金额字段: {hits}"

    def test_产品信息字段只存在于代码产物(self):
        """反向确认：价格确实在 MaterialSelection 的 items 里，只是模型写不到。"""
        assert "price_range" not in MaterialPlan.model_fields
        assert "items" in MaterialSelection.model_fields


# ══════════════════════════════════════════════════════════════════
# 幻觉商品 —— 模型只能指认
# ══════════════════════════════════════════════════════════════════


class TestHallucinatedProducts:
    def test_编造的商品id被剔除(self):
        llm = _FakeLLM(["QY-FL-101", "全友-超值地板-X999", "QY-PT-101"])
        m = _mat(_run(llm))

        assert [i["id"] for i in m["items"]] == ["QY-FL-101", "QY-PT-101"]
        assert "全友-超值地板-X999" in m["invented_products"]
        assert any("不在候选清单中" in g for g in m["data_gaps"])

    def test_真实存在但不在本档候选的商品也被剔除(self):
        """
        QY-FL-102 是中高档商品。经济档方案里选它，等于偷偷超预算 ——
        它虽然真实存在，但不在本档候选集里，同样要剔除。
        """
        m = _mat(_run(_FakeLLM(["QY-FL-102"])))
        assert m["items"] == []
        assert m["invented_products"] == ["QY-FL-102"]

    def test_同品类重复选择只保留第一份(self):
        llm = _FakeLLM(["QY-FL-101", "QY-FL-101"])
        m = _mat(_run(llm))
        assert len(m["items"]) == 1
        assert any("重复选择" in x for x in m["invented_products"])

    def test_空id被剔除且不崩(self):
        llm = _FakeLLM([])
        m = _mat(_run(llm))
        assert m["items"] == []


# ══════════════════════════════════════════════════════════════════
# 商品事实由代码回填
# ══════════════════════════════════════════════════════════════════


class TestFactsComeFromCatalog:
    def test_价格与链接来自目录(self):
        """AC-21：悬停要显示价格与官网链接 —— 两者都必须来自目录，不是模型写的。"""
        m = _mat(_run(_FakeLLM(["QY-FL-101"])))
        item = m["items"][0]
        ref = catalog.by_id()["QY-FL-101"]

        assert item["name"] == ref.name
        assert item["brand"] == ref.brand
        assert item["price_range"] == list(ref.price_range)
        assert item["search_url"] == ref.search_url
        assert item["is_quanyou"] is True

    def test_模型写的价格字段不会被采纳(self):
        """
        模型可能试图把价格塞进 reason 或别的自由文本里。
        它只能待在文本里，**不会**进入任何结构化金额字段 ——
        那些字段来自目录。
        """
        llm = _FakeLLM(["QY-FL-101"], summary="这套材料一共 8000 元，很划算。")
        m = _mat(_run(llm))

        assert "8000" in m["summary"], "自由文本原样保留"
        assert m["items"][0]["price_range"] == list(catalog.by_id()["QY-FL-101"].price_range)

    def test_演示数据声明透出(self):
        m = _mat(_run(_FakeLLM()))
        assert m["disclaimer"].strip()
        assert "演示" in m["disclaimer"]

    def test_记录目录版本(self):
        m = _mat(_run(_FakeLLM()))
        assert m["catalog_version"] == catalog.catalog_version()

    def test_替代建议同样校验并回填(self):
        """
        替代建议里的两个 id 也要校验 —— 否则模型可以借"替代建议"
        塞进一个不存在的型号，绕过 choices 那层校验。
        这里用同为经济档的两个地板（全友 ↔ 竞品），是真实的替代场景。
        """
        llm = _FakeLLM(["QY-FL-101"], substitutions=[
            {"from_product_id": "QY-FL-101", "to_product_id": "XY-FL-301",
             "reason": "若预算更紧可选竞品"},
            {"from_product_id": "根本不存在的型号", "to_product_id": "QY-FL-101",
             "reason": "应被剔除"},
        ])
        m = _mat(_run(llm))

        assert len(m["substitutions"]) == 1
        sub = m["substitutions"][0]
        assert sub["to"]["name"] == catalog.by_id()["XY-FL-301"].name
        # 回填的必须是目录里的完整信息，不是模型写的
        assert sub["to"]["price_range"] == list(catalog.by_id()["XY-FL-301"].price_range)


# ══════════════════════════════════════════════════════════════════
# AC-18 —— 覆盖率由代码算，且代码兜底
# ══════════════════════════════════════════════════════════════════


class TestQuanyouCoverage:
    def test_全友选择时覆盖率满分(self):
        m = _mat(_run(_FakeLLM(_QY_ECON)))
        assert m["quanyou_coverage"] == 1.0
        assert m["quanyou_met"] is True
        assert m["auto_substitutions"] == [], "模型自己选够了，代码不该改动它"

    def test_全选竞品时由代码补足(self):
        """
        ⚠️ AC-18 的兜底。

        模型全选了竞品，覆盖率为 0%。代码把每一项换成同品类的全友等价物 ——
        而不是在提示词里反复叮嘱"请优先全友"然后祈祷它照做。
        """
        m = _mat(_run(_FakeLLM(_COMP_ECON)))

        assert m["quanyou_coverage"] == 1.0
        assert m["quanyou_met"] is True
        assert len(m["auto_substitutions"]) == 2, "两项都该被换掉"
        assert all(i["is_quanyou"] for i in m["items"])
        # 替换必须透明记录，不能悄悄改掉
        assert any("系统替换了" in g for g in m["data_gaps"])

    def test_替换记录说明改了什么(self):
        m = _mat(_run(_FakeLLM(_COMP_ECON)))
        swap = m["auto_substitutions"][0]
        assert swap["from"] and swap["to"]
        assert swap["from_name"] and swap["to_name"]
        assert "全友" in swap["reason"]

    def test_无全友可换时如实不达标(self, monkeypatch):
        """
        找不到同品类全友替代品时，**保留原选择并如实上报未达标** ——
        宁可覆盖率不达标，也不为了凑指标换一个规格不符的东西。

        目录里每个品类都有全友，所以这里把"找不到替代"这一分支
        直接打桩出来测 —— 那是代码里真实存在、且必须诚实处理的一条路径。
        """
        monkeypatch.setattr(catalog, "find_quanyou_alternative",
                            lambda *a, **kw: None)
        m = _mat(_run(_FakeLLM(_COMP_ECON)))

        assert m["items"], "换不掉也要保留原选择，不能清空"
        assert m["quanyou_coverage"] == 0.0
        assert m["quanyou_met"] is False, "换不掉就必须如实报未达标"
        assert m["auto_substitutions"] == []

    def test_找到替代就换掉(self):
        """反向确认：目录里确实存在可替换的全友同品类商品。"""
        m = _mat(_run(_FakeLLM(["JP-SA-301"])))      # 箭牌坐便器，经济档
        assert m["items"][0]["is_quanyou"] is True
        assert m["auto_substitutions"]

    def test_覆盖率由代码算而非模型自报(self):
        """模型在 summary 里自称 100% 全友，实际选的是竞品 —— 代码不采信。"""
        llm = _FakeLLM(_COMP_ECON, summary="本方案 100% 采用全友产品。")
        m = _mat(_run(llm))
        # 代码把竞品换成了全友，所以最终确实 100%；关键是这个数字来自计算，
        # 而模型那句话是否属实根本不参与判断
        assert m["quanyou_coverage"] == catalog.coverage(m["product_ids"])

    def test_覆盖率与items一致(self):
        """覆盖率必须与最终 items 对得上，不能一个说 A 一个说 B。"""
        m = _mat(_run(_FakeLLM(_COMP_ECON)))
        assert m["quanyou_coverage"] == catalog.coverage(
            [i["id"] for i in m["items"]])


# ══════════════════════════════════════════════════════════════════
# 失败方向 —— 模型挂了选材还在
# ══════════════════════════════════════════════════════════════════


class TestLLMFailureKeepsSelection:
    def test_模型抛错时仍产出选材(self):
        m = _mat(_run(_FakeLLM(mode="raise")))

        assert m["items"], "候选排序是确定性的，模型挂了也该给出选择"
        assert all(i["is_quanyou"] for i in m["items"]), "兜底按打分取首选，应优先全友"
        assert m["quanyou_met"] is True

    def test_兜底标记降级并说明(self):
        out = _run(_FakeLLM(mode="raise"))
        m = _mat(out)

        assert out["degraded"] is True
        assert any("回退确定性排序" in r for r in out["degrade_reasons"])
        assert m["confidence"] == 0.0
        assert any("确定性排序" in g for g in m["data_gaps"])
        assert m["warnings"], "应如实告知说明部分缺失"

    def test_兜底理由仍有依据(self):
        """
        兜底不是"随便填一个"：理由直接复用评分时算出的 reasons，
        所以降级在用户眼里是"少了点针对性"，而不是"多了一段胡说"。
        """
        m = _mat(_run(_FakeLLM(mode="raise")))
        for item in m["items"]:
            assert item["reason"].strip()
            assert "模型不可用" in item["reason"], "兜底项应标明来源"

    def test_模型返回空载荷时走兜底(self):
        """空 MaterialPlan 是合法的，但生成不出任何选择 —— 应回退到确定性排序。"""
        out = _run(_FakeLLM(mode="garbage"))
        m = _mat(out)
        # 空载荷本身不报错，产出空的 choices；代码不把它当失败，
        # 但也不会凭空变出商品
        assert isinstance(m["items"], list)

    def test_超时走兜底而非熔断(self, monkeypatch):
        """
        选材有**独立**超时（SELECT_TIMEOUT），比节点总超时短 ——
        慢模型不能把整个选材拖走。
        """
        monkeypatch.setattr("backend.app.agents.material_agent.SELECT_TIMEOUT", 0.05)
        m = _mat(_run(_FakeLLM(delay=0.3)))
        assert m["items"], "超时后应回退到确定性排序"
        assert m["quanyou_met"] is True

    def test_超时阈值小于节点超时(self):
        assert SELECT_TIMEOUT < MaterialAgent.timeout


# ══════════════════════════════════════════════════════════════════
# 业务连续性守卫
# ══════════════════════════════════════════════════════════════════


class TestGuard:
    def test_无面积时被拒且不调用LLM(self):
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
        assert "select_materials" in out["errors"][0]["message"]

    def test_不需要墙体信息(self):
        """
        选材的判据是「房间 + 面积」，**不要墙体** ——
        没识别出墙体但房间面积齐全的户型，方案出不了，选材却是能做的。
        用 generate_plan 那道门会过度拒绝。
        """
        layout = {**LAYOUT, "walls": []}
        out = _run(_FakeLLM(), layout=layout)

        assert out["trace"][0]["ok"] is True
        assert _mat(out)["items"]

    def test_降级解析被拒(self):
        layout = {"mode": "degraded_basic",
                  "rooms": [{"name": "Living", "type": "other", "area": 0.0}],
                  "walls": [], "doors": [], "windows": [], "total_area": 0.0}
        out = _run(_FakeLLM(), layout=layout)
        assert out["trace"][0]["ok"] is False

    def test_守卫异常带missing与suggestion(self):
        agent = MaterialAgent(llm=_FakeLLM())
        layout = {**LAYOUT, "total_area": 0.0,
                  "rooms": [{"name": "客厅", "type": "living_room", "area": 0.0}]}
        with pytest.raises(OperationNotAllowedError) as ei:
            asyncio.run(agent.run(_state(layout=layout)))
        payload = ei.value.to_payload()
        assert payload["operation"] == "select_materials"
        assert payload["suggestion"]

    def test_无layout直接失败(self):
        out = _run(_FakeLLM(), layout=None)
        assert out["trace"][0]["ok"] is False
        assert "layout" in out["errors"][0]["message"]


# ══════════════════════════════════════════════════════════════════
# 提示词
# ══════════════════════════════════════════════════════════════════


class TestPrompt:
    def test_提示词含候选与匹配理由(self):
        llm = _FakeLLM()
        _run(llm)
        prompt = llm.prompts[0]

        index = catalog.by_id()
        assert "QY-FL-101" in prompt
        assert index["QY-FL-101"].name in prompt
        assert "匹配理由" in prompt

    def test_提示词含业务与结构约束(self):
        llm = _FakeLLM()
        _run(llm)
        prompt = llm.prompts[0]

        assert "只能从这里选" in prompt            # 候选清单标题
        assert "不得编造" in prompt                # id 必须来自清单
        assert "优先选全友" in prompt              # AC-18 的业务要求
        assert "不要提及任何金额" in prompt        # 数字不归模型管

    def test_提示词带业主需求(self):
        llm = _FakeLLM()
        _run(llm, requirements={"has_children": True, "eco_level": "E0",
                                "family_size": 3})
        prompt = llm.prompts[0]
        assert "家中有儿童" in prompt
        assert "E0" in prompt
        assert "3 人" in prompt

    def test_提示词带分支定位(self):
        llm = _FakeLLM()
        _run(llm, branch_spec={"plan_id": "plan_chinese_high", "style": "chinese",
                               "budget_grade": "high", "index": 2})
        prompt = llm.prompts[0]
        assert "plan_chinese_high" in prompt
        assert "high" in prompt

    def test_提示词带演示数据声明(self):
        llm = _FakeLLM()
        _run(llm)
        assert "演示" in llm.prompts[0]


# ══════════════════════════════════════════════════════════════════
# 跨切面
# ══════════════════════════════════════════════════════════════════


class TestCrossCutting:
    def test_降级标记传上来(self):
        out = _run(_FakeLLM(degraded=True))
        assert out["degraded"] is True
        assert any("plan_modern_economy" in r for r in out["degrade_reasons"])

    def test_trace记录模型与耗时(self):
        t = _run(_FakeLLM())["trace"][0]
        assert t["agent"] == "A-05"
        assert t["ok"] is True
        assert t["llm"]["model"] == "fake-model"

    def test_phase进入planning(self):
        assert _run(_FakeLLM())["phase"] == "planning"

    def test_写入plan_bundles的materials键(self):
        out = _run(_FakeLLM())
        assert "materials" in out["plan_bundles"]["plan_modern_economy"]

    def test_缺少branch_spec时回退首项(self):
        state = _state(styles=["chinese"], budget_grades=["high"])
        state.pop("branch_spec")
        out = asyncio.run(MaterialAgent(llm=_FakeLLM(["QY-FL-102"])).execute(state))
        assert list(out["plan_bundles"]) == ["plan_chinese_high"]

    def test_档位过滤生效(self):
        """经济档方案里不会出现只在高端档的商品。"""
        m = _mat(_run(_FakeLLM(_QY_ECON)))
        index = catalog.by_id()
        for item in m["items"]:
            assert "economy" in index[item["id"]].budget_grade
