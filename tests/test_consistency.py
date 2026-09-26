"""
AC-32 的结果一致性判据。

本文件守的核心命题：**分母只装确定性的东西，而结构不变量不能被分数盖住。**

三个阶段各有一条容易被做错的地方，都有用例盯着：

  ① **A 类的成分** —— 哪些字段进分母是**一个决定**，不是"能比就比"。
     进多了（把 LLM 文本算进去）会让分数长期在门槛附近抖；进少了
     （只比 plan_id 集合）会让分数恒为 100% 而什么都没证明。
     所以这里把 A 类的键集合**逐个钉住**。
  ② **B 类不被分数掩盖** —— A 类 100% 而 B 类有一条不成立时，
     整体必须是"不一致"。只报分数的话，一个"所有金额都对、但材料里
     混进了目录外的商品"的结果会显示成完美。
  ③ **C 类真的只记录** —— 它漂了不该让判定失败，否则又回到了
     "拿不确定性凑指标"。
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import get_args

import pytest

from backend.app.core import consistency
from backend.app.schemas.risk import RiskType
from backend.app.services.material import catalog


def _result() -> dict:
    """一份结构完整的合成结果。数值刻意用规则引擎真正会产出的形状。"""
    line = {"name": "水电改造", "min": 8000.0, "max": 11000.0}
    return {
        "errors": [],
        "plans": [
            {
                "plan_id": "plan_modern_economy",
                "style": "modern",
                "budget_grade": "economy",
                "missing_artifacts": [],
                "space_plan": {"summary": "以成品家具为主。", "zones": []},
                "budget": {
                    "total_min": 81178.24, "total_max": 104000.0,
                    "subtotal_min": 70000.0, "subtotal_max": 90000.0,
                    "computed_by": "rule_engine_v1",
                    "lines": [dict(line)],
                },
                "materials": {
                    "items": [{"id": "QY-FL-101", "category": "floor",
                               "is_quanyou": True}],
                    "quanyou_met": True,
                    "quanyou_coverage": 1.0,
                    "catalog_version": catalog.catalog_version(),
                },
                "risks": {
                    "items": [{"type": get_args(RiskType)[0], "title": "增项"}],
                    "distinct_type_count": 1,
                },
                "environment": {
                    "formaldehyde": {"risk": "low"},
                    "tvoc": {"risk": "low"},
                },
            }
        ],
        "comparison": {
            "available": False,
            "rows": [{"plan_id": "plan_modern_economy"}],
        },
    }


# ══════════════════════════════════════════════════════════════════
# ① A 类的成分
# ══════════════════════════════════════════════════════════════════


class TestAClassIsDeliberate:
    def test_键集合被钉住(self):
        """
        加一个字段进 A 类 = 把它纳入"必须逐次相同"的承诺。
        所以这份键表要显式维护，改它得是一个有意的动作。
        """
        got = set(consistency.fingerprint(_result())["a"])
        assert got == {
            "plan_ids", "plan_count",
            "plan_modern_economy.style",
            "plan_modern_economy.budget_grade",
            "plan_modern_economy.budget.total_min",
            "plan_modern_economy.budget.total_max",
            "plan_modern_economy.budget.subtotal_min",
            "plan_modern_economy.budget.subtotal_max",
            "plan_modern_economy.budget.computed_by",
            "plan_modern_economy.budget.lines",
            "plan_modern_economy.catalog_version",
            "comparison.available",
            "comparison.rows_order",
        }

    def test_不含任何LLM产出的文本(self):
        """
        ⚠️ 这是**整个判据成立的前提**。

        方案解说、风险标题、空间规划文本都是模型写的、每次都不同。
        把它们放进 A 类，分数就会长期停在 90% 上下，而那个数字既不
        上升也不下降 —— 它只是在度量模型的随机性，不是系统的正确性。
        """
        a = consistency.fingerprint(_result())["a"]
        blob = repr(a)
        assert "以成品家具为主" not in blob, "空间规划文本进了 A 类"
        assert "增项" not in blob, "风险标题进了 A 类"

    def test_不含材料的最终选品(self):
        """
        `items` 是模型在候选里挑的，逐次可能不同 —— 属于 B 类（形状）
        与 C 类（条数），不属于 A 类。
        """
        assert "QY-FL-101" not in repr(consistency.fingerprint(_result())["a"])

    def test_金额用浮点归一化(self):
        """
        引擎算出来的是 `Decimal`，JSON 回来的是 `float`。
        不归一化的话 `Decimal('81178.24') != 81178.24`，
        会把每一次比对都判成不一致 —— 一个**恒假的检查**等于没有检查。
        """
        assert consistency._num(Decimal("81178.24")) == consistency._num(81178.24)
        assert consistency._num(Decimal("81178.240000")) == 81178.24

    def test_布尔不被当成数字(self):
        """`True == 1` 在 Python 里成立，但那会把两种语义混起来。"""
        assert consistency._num(True) is True
        assert consistency._num(False) is False


# ══════════════════════════════════════════════════════════════════
# ② 比对
# ══════════════════════════════════════════════════════════════════


class TestCompare:
    def test_完全相同得满分(self):
        base = consistency.fingerprint(_result())
        rep = consistency.compare(base, copy.deepcopy(base))
        assert rep["score"] == 1.0
        assert rep["identical"] is True
        assert rep["a"]["diffs"] == {}

    def test_金额变一分钱就报出来(self):
        base = consistency.fingerprint(_result())
        changed = _result()
        changed["plans"][0]["budget"]["total_min"] = 81178.25
        rep = consistency.compare(base, consistency.fingerprint(changed))
        assert rep["score"] < 1.0
        assert "plan_modern_economy.budget.total_min" in rep["a"]["diffs"]
        # 报错要能直接指出是哪个字段、两边各是多少
        diff = rep["a"]["diffs"]["plan_modern_economy.budget.total_min"]
        assert diff["baseline"] == 81178.24 and diff["current"] == 81178.25

    def test_缺一套方案会被发现(self):
        base = consistency.fingerprint(_result())
        fewer = _result()
        fewer["plans"] = []
        rep = consistency.compare(base, consistency.fingerprint(fewer))
        assert rep["score"] < 1.0
        assert "plan_ids" in rep["a"]["diffs"]

    def test_分辨率是逐字段而不是整体(self):
        """
        报出来的必须是**字段级**的差异。整块 dict 比对只能告诉你
        "不一样"，而查一个人要的是"哪个字段不一样"。
        """
        base = consistency.fingerprint(_result())
        changed = _result()
        changed["plans"][0]["budget"]["lines"][0]["max"] = 12000.0
        rep = consistency.compare(base, consistency.fingerprint(changed))
        assert list(rep["a"]["diffs"]) == ["plan_modern_economy.budget.lines"]


class TestBClassCannotBeMasked:
    def test_A类满分但B类不成立时整体不一致(self):
        """
        ⚠️ **这条是本文件里最重要的一条。**

        只报分数的话，一个"所有金额都对、但材料里混进了目录外的商品"
        的结果会显示成 100% —— 而那种结果正是 A-05 纪律 1 要防的东西。
        分数是**必要不充分**的判据，判定必须另有 B 类这一票。
        """
        base = consistency.fingerprint(_result())
        bad = _result()
        bad["plans"][0]["materials"]["items"] = [
            {"id": "编造的型号-9999", "category": "floor", "is_quanyou": True}
        ]
        rep = consistency.compare(base, consistency.fingerprint(bad))

        assert rep["score"] == 1.0, "A 类本来就该全等 —— 这条测的是 B 类"
        assert rep["identical"] is False, "B 类不成立必须让整体判为不一致"
        assert "plan_modern_economy.没有目录外的商品" in rep["b"]["failed"]

    def test_风险类型用真枚举(self):
        """
        ⚠️ 第一版在 `consistency.py` 里**手写了一份英文枚举**，而真实的
        `RiskType` 是中文的。后果是 B 类那条检查对**每一条真实风险**
        都判不成立 —— 一个只会说"不合格"的检查，看着在工作，其实什么都没检查。
        """
        assert consistency.RISK_TYPES == get_args(RiskType)
        assert "增项风险" in consistency.RISK_TYPES

        base = consistency.fingerprint(_result())
        good = _result()
        good["plans"][0]["risks"]["items"] = [{"type": "增项风险"}]
        rep = consistency.compare(base, consistency.fingerprint(good))
        assert "plan_modern_economy.风险类型都在枚举内" not in rep["b"]["failed"]

    def test_编造的风险类型被抓住(self):
        base = consistency.fingerprint(_result())
        bad = _result()
        bad["plans"][0]["risks"]["items"] = [{"type": "看起来很专业的风险"}]
        rep = consistency.compare(base, consistency.fingerprint(bad))
        assert "plan_modern_economy.风险类型都在枚举内" in rep["b"]["failed"]

    def test_环境结论混进浓度会被抓住(self):
        """
        `services/environment.py` 的纪律是"只给风险档、绝不输出浓度"。
        那边不产生浓度，这条负责确认它没从别处漏进来。
        """
        base = consistency.fingerprint(_result())
        bad = _result()
        bad["plans"][0]["space_plan"] = {"summary": "预估甲醛 0.06 mg/m³"}
        rep = consistency.compare(base, consistency.fingerprint(bad))
        assert "plan_modern_economy.环境结论没混进浓度" in rep["b"]["failed"]

    def test_全友覆盖率不达标会被抓住(self):
        base = consistency.fingerprint(_result())
        bad = _result()
        bad["plans"][0]["materials"]["quanyou_met"] = False
        rep = consistency.compare(base, consistency.fingerprint(bad))
        assert "plan_modern_economy.全友覆盖率达标" in rep["b"]["failed"]

    def test_缺产物会被抓住(self):
        base = consistency.fingerprint(_result())
        bad = _result()
        bad["plans"][0]["missing_artifacts"] = ["risks"]
        rep = consistency.compare(base, consistency.fingerprint(bad))
        assert "plan_modern_economy.每项产物都在" in rep["b"]["failed"]

    def test_新增的检查项算不成立(self):
        """
        后端加了新的不变量检查时，拿旧基线比会多出键。
        **算不成立**而不是忽略 —— 忽略的话，加检查这个动作本身
        会让门禁悄悄失效。
        """
        base = consistency.fingerprint(_result())
        rep = consistency.compare(base, {"a": base["a"],
                                        "b": {**base["b"], "新增的检查": True},
                                        "c": base["c"]})
        assert "新增的检查" in rep["b"]["new_keys"]
        assert rep["identical"] is False


# ══════════════════════════════════════════════════════════════════
# ③ C 类只记录
# ══════════════════════════════════════════════════════════════════


class TestCClassOnlyRecords:
    def test_风险条数漂了不影响判定(self):
        base = consistency.fingerprint(_result())
        other = _result()
        other["plans"][0]["risks"]["items"].append({"type": "漏项"})
        other["plans"][0]["risks"]["distinct_type_count"] = 2
        rep = consistency.compare(base, consistency.fingerprint(other))

        assert rep["score"] == 1.0
        assert rep["identical"] is True, "C 类漂移不该让判定失败"
        assert "plan_modern_economy.风险条数" in rep["c"]["drift"]

    def test_文本长度进C类而不是A类(self):
        base = consistency.fingerprint(_result())
        other = _result()
        other["plans"][0]["space_plan"] = {"summary": "换了一种说法，长一点。" * 3}
        rep = consistency.compare(base, consistency.fingerprint(other))
        assert rep["identical"] is True
        assert "plan_modern_economy.规划文本长度" in rep["c"]["drift"]

    def test_只记长度不记内容(self):
        """内容每次都不同，记下来只会淹没真正的变化。"""
        c = consistency.fingerprint(_result())["c"]
        assert isinstance(c["plan_modern_economy.规划文本长度"], int)
        assert "以成品家具为主" not in repr(c)


# ══════════════════════════════════════════════════════════════════
# 折算成一次重放的分数
# ══════════════════════════════════════════════════════════════════


class TestConsistencyScore:
    def test_三次全同得满分(self):
        r = consistency.fingerprint(_result())
        rep = consistency.consistency_score([r, copy.deepcopy(r), copy.deepcopy(r)])
        assert rep["runs"] == 3
        assert rep["score"] == 1.0
        assert rep["identical"] is True

    def test_基线是第一次而不是预设答案(self):
        """
        取第一次当基线有个演示上的好处：**不需要预先维护一份"正确答案"
        文件**，也就不会过期。改一次模型/提示词，基线自动跟着走。
        """
        a, b = _result(), _result()
        b["plans"][0]["budget"]["total_min"] = 1.0
        rep = consistency.consistency_score([
            consistency.fingerprint(a),
            consistency.fingerprint(b),
            consistency.fingerprint(a),
        ])
        # 第 2 次与基线不同、第 3 次与基线相同
        assert rep["reports"][0]["score"] < 1.0
        assert rep["reports"][1]["score"] == 1.0
        assert rep["identical"] is False

    def test_空输入不给假满分(self):
        """没有可比的结果时给 0 而不是 1.0 —— 与 `/system/metrics` 的
        "没采到 ≠ 0" 是同一条纪律的镜像：这里不能把"没测"说成"满分"。"""
        rep = consistency.consistency_score([])
        assert rep["score"] == 0.0
        assert rep["identical"] is False
        assert rep["reason"]

    def test_单次运行算通过(self):
        """`-n 1` 时没有可比的第二份 —— 如实说"只跑了 1 次"，不假装验证过。"""
        rep = consistency.consistency_score([consistency.fingerprint(_result())])
        assert rep["runs"] == 1
        assert rep["reports"] == []
