"""
预算规则引擎测试。

ADR-07 的立论是：「让 LLM 算钱，错误是**静默的**——看起来合理的错误数字
比明显崩溃更危险」。它给的回报则是：**预算部分变得可单元测试**。

本文件就是那份回报的兑现。它守四件事：

1. **数字必须落在文档定义的档位区间内**（¥800-1200 / 1500-2000 / 2500-3500）
   —— 这条把价格表和需求文档 2.2.2 钉在一起，改单价改歪了会立刻报红。
2. **面积无效时必须拒绝**，绝不返回 ¥0 —— 那个假数字正是 capabilities.py
   整个模块存在的理由，引擎内部再兜一道。
3. **结果是确定性的** —— 同样入参永远同样输出，不随机、不依赖时间与网络。
4. **引擎不依赖任何 LLM** —— 直接读源码断言，用不变量代替自觉。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.app.schemas.budget import BudgetBreakdown
from backend.app.services.budget import engine

_ENGINE_SRC = Path(engine.__file__).read_text(encoding="utf-8")
_ENGINE_TREE = ast.parse(_ENGINE_SRC)

_GRADES = ("economy", "medium", "high")


def _imported_modules(tree: ast.AST) -> set[str]:
    """
    取出源码里**实际导入**的模块路径。

    刻意用 AST 而不是全文正则：本文件的说明文字里就写着「不导入 llm_client」，
    全文匹配会把这句说明本身判成违规 —— 判据必须是导入语句，不是出现过这个词。
    """
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module)
                mods.update(f"{node.module}.{alias.name}" for alias in node.names)
    return mods


# ══════════════════════════════════════════════════════════════════
# 结构约束 —— ADR-07 的执行方式
# ══════════════════════════════════════════════════════════════════


class TestEngineIsLLMFree:
    def test_引擎不导入任何LLM相关模块(self):
        """
        如果这个模块连 `llm_client` 都导不进来，它就不可能在某次"顺手优化"里
        悄悄调用模型。这比在文档里写一句"注意不要用 LLM 算钱"可靠得多。

        这是**架构测试**，不是业务测试 —— 但它守的是本项目最重要的一条 ADR。
        """
        forbidden = ("llm_client", "openai", "anthropic", "httpx", "requests",
                     "ollama", "mcp")
        imported = _imported_modules(_ENGINE_TREE)
        hits = [
            mod for mod in imported
            if any(f in mod.lower() for f in forbidden)
        ]
        assert not hits, f"预算引擎导入了不该有的模块: {hits}"

    def test_辅助函数本身是可靠的(self):
        """
        上面的断言依赖这个解析器 —— 先验证它认得出真实导入。

        注意 AST 会把相对导入的层级（`..`）单独放在 `level` 字段里，
        `module` 只留 `core.llm_client`。所以这里断言的是规范化后的名字，
        而黑名单匹配用的是子串 —— 两者配合才能既准确又不误伤。
        """
        sample = ast.parse(
            "import httpx\n"
            "from ..core.llm_client import LLMClient\n"
            "from ..schemas.budget import BudgetLine\n"
        )
        mods = _imported_modules(sample)

        assert "httpx" in mods
        assert "core.llm_client" in mods
        assert "core.llm_client.LLMClient" in mods
        # 真正要断言的是：黑名单能抓住它
        assert any("llm_client" in m.lower() for m in mods)
        # 而正常依赖不会被误伤
        assert not any("llm_client" in m.lower()
                       for m in ("schemas.budget", "core.config"))

    def test_引擎没有异步函数(self):
        """
        纯计算不需要 async。出现 async 通常意味着这里混进了 IO ——
        而 IO 就意味着"算个预算要等网络"，那正是要避免的。
        """
        async_defs = [
            node.name for node in ast.walk(_ENGINE_TREE)
            if isinstance(node, ast.AsyncFunctionDef)
        ]
        assert not async_defs, f"预算引擎不该有异步函数: {async_defs}"


# ══════════════════════════════════════════════════════════════════
# 与需求文档的区间一致性 —— 价格表的护栏
# ══════════════════════════════════════════════════════════════════


class TestGradeRangeConformance:
    """
    需求文档 2.2.2 给三档定了区间。价格表必须算得出落在这个区间里的总价。

    这组测试的价值在于：**改单价时若跑挂了，说明价格表已偏离文档口径** ——
    而不是等做完演示才被人发现"经济档怎么要 1500 一平"。
    """

    @pytest.mark.parametrize("grade", _GRADES)
    def test_折合单价落在文档区间(self, grade):
        lo, hi = engine.grade_ranges()[grade]
        result = engine.calculate(area=89.0, grade=grade)

        assert lo <= result.price_per_sqm_min, (
            f"{grade} 折合单价下限 {result.price_per_sqm_min} 低于文档区间 {lo}-{hi}"
        )
        assert result.price_per_sqm_max <= hi, (
            f"{grade} 折合单价上限 {result.price_per_sqm_max} 高于文档区间 {lo}-{hi}"
        )

    def test_折合单价与面积无关(self):
        """
        档位区间是「元/㎡」定义的，所以不同面积下折合单价必须一致 ——
        如果小户型单价更高（或更低），说明公式里混进了非线性项，
        那与文档「面积 × 单价」的口径不符。
        """
        for area in (45.0, 89.0, 140.0):
            r = engine.calculate(area=area, grade="medium")
            lo, hi = engine.grade_ranges()["medium"]
            assert lo <= r.price_per_sqm_min and r.price_per_sqm_max <= hi

    def test_区间定义可读且完整(self):
        ranges = engine.grade_ranges()
        assert set(ranges) == set(_GRADES)
        for grade, (lo, hi) in ranges.items():
            assert 0 < lo < hi, f"{grade} 的区间定义不合理: {lo}-{hi}"


# ══════════════════════════════════════════════════════════════════
# 面积无效必须拒绝 —— 不给 ¥0
# ══════════════════════════════════════════════════════════════════


class TestRefusesInvalidArea:
    """
    **本文件最重要的一组。**

    `calc_budget(area=0)` 返回一个 ¥0 的预算是本项目最危险的失败形态：
    不报错、HTTP 200、看起来正常，而用户会拿它去和装修公司谈。
    引擎必须拒绝，而不是"算出一个 0"。
    """

    @pytest.mark.parametrize("area", [0, 0.0, -1, -89.5])
    def test_面积非正数时拒绝计算(self, area):
        with pytest.raises(engine.InvalidAreaError):
            engine.calculate(area=area, grade="economy")

    @pytest.mark.parametrize("area", [None, "89", [], {}])
    def test_面积类型不对时拒绝计算(self, area):
        with pytest.raises(engine.InvalidAreaError):
            engine.calculate(area=area, grade="economy")  # type: ignore[arg-type]

    def test_拒绝时不给任何数字(self):
        """
        异常必须抛在返回之前 —— 不能返回一个对象再附带个 warning。
        调用方拿不到半成品，就不会有人误用。
        """
        with pytest.raises(engine.InvalidAreaError) as ei:
            engine.calculate(area=0, grade="economy")
        assert "0" in str(ei.value) or "无效" in str(ei.value)

    def test_未知档位被拒(self):
        with pytest.raises(ValueError, match="档位"):
            engine.calculate(area=89.0, grade="luxury")


# ══════════════════════════════════════════════════════════════════
# 计算结果的结构与算术
# ══════════════════════════════════════════════════════════════════


class TestCalculation:
    def test_分项数满足AC05(self):
        """AC-05：输出 ≥7 个分项。"""
        for grade in _GRADES:
            r = engine.calculate(area=89.0, grade=grade)
            assert len(r.lines) >= engine.MIN_BUDGET_LINES, (
                f"{grade} 只有 {len(r.lines)} 个分项，AC-05 要求 ≥{engine.MIN_BUDGET_LINES}"
            )

    @pytest.mark.parametrize("grade", _GRADES)
    def test_上下限顺序正确(self, grade):
        r = engine.calculate(area=89.0, grade=grade)
        assert 0 < r.subtotal_min <= r.subtotal_max
        assert 0 < r.total_min <= r.total_max
        assert r.total_min >= r.subtotal_min, "总价不可能小于施工费小计"

    @pytest.mark.parametrize("grade", _GRADES)
    def test_每个分项自身上下限有序(self, grade):
        r = engine.calculate(area=89.0, grade=grade)
        for ln in r.lines:
            assert ln.unit_price_min <= ln.unit_price_max, f"{ln.key} 单价上下限反了"
            assert ln.amount_min <= ln.amount_max, f"{ln.key} 金额上下限反了"

    @pytest.mark.parametrize("grade", _GRADES)
    def test_总价等于小计加比例项(self, grade):
        """
        算术自洽：总价 = 施工费小计 + 各比例项金额。
        这条防的是将来有人改动求和逻辑时漏掉或重复计算某一类分项。
        """
        r = engine.calculate(area=89.0, grade=grade)

        pct_min = sum(ln.amount_min for ln in r.lines if ln.basis == "subtotal_pct")
        pct_max = sum(ln.amount_max for ln in r.lines if ln.basis == "subtotal_pct")

        assert r.total_min == pytest.approx(r.subtotal_min + pct_min, abs=0.02)
        assert r.total_max == pytest.approx(r.subtotal_max + pct_max, abs=0.02)

    @pytest.mark.parametrize("grade", _GRADES)
    def test_小计等于工程量分项之和(self, grade):
        r = engine.calculate(area=89.0, grade=grade)
        area_lines = [ln for ln in r.lines if ln.basis != "subtotal_pct"]
        assert r.subtotal_min == pytest.approx(sum(ln.amount_min for ln in area_lines), abs=0.02)
        assert r.subtotal_max == pytest.approx(sum(ln.amount_max for ln in area_lines), abs=0.02)

    def test_比例项不以含比例项的金额为基数(self):
        """
        管理费/设计费都以**施工费小计**为基数，彼此不叠加。

        若按"含设计费"的金额算管理费，会出现复利效应，与装修公司实际计法不符 ——
        这种错误不会报错，只会让总价悄悄偏高。
        """
        r = engine.calculate(area=89.0, grade="high")
        pct_lines = [ln for ln in r.lines if ln.basis == "subtotal_pct"]
        assert len(pct_lines) >= 2, "高端档应同时有管理费与设计费"

        for ln in pct_lines:
            expected_min = r.subtotal_min * ln.unit_price_min / 100
            assert ln.amount_min == pytest.approx(expected_min, abs=0.02), (
                f"{ln.key} 的基数不是施工费小计"
            )

    def test_档位越高总价越高(self):
        totals = [engine.calculate(area=89.0, grade=g).total_min for g in _GRADES]
        assert totals[0] < totals[1] < totals[2], f"档位与总价未同向递增: {totals}"

    def test_面积越大总价越高(self):
        small = engine.calculate(area=45.0, grade="medium").total_min
        large = engine.calculate(area=140.0, grade="medium").total_min
        assert large > small

    def test_面积线性放大(self):
        """面积翻倍，总价应精确翻倍——公式里不该有非线性的隐藏项。"""
        a = engine.calculate(area=60.0, grade="medium")
        b = engine.calculate(area=120.0, grade="medium")
        assert b.total_min == pytest.approx(a.total_min * 2, rel=1e-6)
        assert b.total_max == pytest.approx(a.total_max * 2, rel=1e-6)


# ══════════════════════════════════════════════════════════════════
# 计价基准
# ══════════════════════════════════════════════════════════════════


class TestPricingBasis:
    def test_墙面项按折算系数计算(self):
        """
        油漆按「墙面面积 = 套内面积 × 2.6」计。若误按套内面积算，
        油漆这一项会偏小一大截，而且不报错。
        """
        r = engine.calculate(area=100.0, grade="economy")
        painting = next(ln for ln in r.lines if ln.basis == "wall_area")

        factor = engine.load_pricing()["_meta"]["wall_area_factor"]
        assert painting.quantity == pytest.approx(100.0 * factor)
        assert painting.quantity > r.area, "墙面面积应大于套内面积"

    def test_面积项按套内面积计算(self):
        r = engine.calculate(area=100.0, grade="economy")
        for ln in r.lines:
            if ln.basis == "area":
                assert ln.quantity == pytest.approx(100.0), f"{ln.key} 工程量不是套内面积"

    def test_比例项的quantity是百分比(self):
        r = engine.calculate(area=89.0, grade="medium")
        for ln in r.lines:
            if ln.basis == "subtotal_pct":
                assert 0 < ln.quantity < 100, f"{ln.key} 的 quantity 应是百分数"

    def test_墙面系数可覆盖(self):
        default = engine.calculate(area=100.0, grade="economy")
        custom = engine.calculate(area=100.0, grade="economy", wall_area_factor=3.0)

        d = next(ln for ln in default.lines if ln.basis == "wall_area")
        c = next(ln for ln in custom.lines if ln.basis == "wall_area")
        assert c.quantity > d.quantity

    def test_地区系数可覆盖(self):
        base = engine.calculate(area=89.0, grade="medium")
        up = engine.calculate(area=89.0, grade="medium", region_coefficient=1.5)
        assert up.total_min > base.total_min
        assert up.region_coefficient == 1.5


# ══════════════════════════════════════════════════════════════════
# 确定性 —— 可测试性的前提
# ══════════════════════════════════════════════════════════════════


class TestDeterminism:
    def test_同样入参得到完全相同的结果(self):
        """
        不随机、不读时间、不依赖网络。这条不成立，上面所有断言都没有意义。
        """
        a = engine.calculate(area=89.0, grade="medium")
        b = engine.calculate(area=89.0, grade="medium")
        assert a.model_dump() == b.model_dump()

    def test_连续多次调用结果稳定(self):
        results = {engine.calculate(area=89.0, grade="high").total_min for _ in range(5)}
        assert len(results) == 1


# ══════════════════════════════════════════════════════════════════
# 可追溯性与演示数据声明
# ══════════════════════════════════════════════════════════════════


class TestTraceability:
    def test_标注计算来源(self):
        """ADR-07 要求 DB 记录 budget_computed_by，规则引擎版本可追溯。"""
        r = engine.calculate(area=89.0, grade="economy")
        assert r.computed_by.startswith("rule_engine")
        assert r.data_source

    def test_演示数据声明必须非空(self):
        """
        价格全是演示数据，用户有权知道。声明为空等于隐瞒 ——
        所以它不是"最好有"，而是断言。
        """
        for grade in _GRADES:
            r = engine.calculate(area=89.0, grade=grade)
            assert r.disclaimer.strip(), f"{grade} 缺少演示数据声明"
            assert "演示" in r.disclaimer

    def test_结果可序列化(self):
        """要进 LangGraph state 与 Redis checkpointer，必须能 dump 成纯 dict。"""
        r = engine.calculate(area=89.0, grade="medium")
        assert isinstance(r, BudgetBreakdown)
        dumped = r.model_dump()
        assert isinstance(dumped, dict)
        import json
        json.dumps(dumped, ensure_ascii=False)   # 不抛异常即可


# ══════════════════════════════════════════════════════════════════
# 价格表健壮性
# ══════════════════════════════════════════════════════════════════


class TestPricingData:
    def test_每个分项三档单价齐全(self):
        """缺任何一档，那个档位的计算就会 KeyError —— 提前在数据层拦住。"""
        items = engine.load_pricing()["items"]
        for item in items:
            for grade in _GRADES:
                assert grade in item["unit_price"], f"{item['key']} 缺 {grade} 档单价"
                lo, hi = item["unit_price"][grade]
                assert 0 <= lo <= hi, f"{item['key']} 的 {grade} 单价区间不合理"

    def test_分项key不重复(self):
        keys = [it["key"] for it in engine.load_pricing()["items"]]
        assert len(keys) == len(set(keys)), "分项 key 重复会让对比表对不上"

    def test_价格表缺失时明确报错(self, tmp_path):
        missing = tmp_path / "nope.json"
        with pytest.raises(engine.PricingDataError):
            engine.load_pricing(str(missing))

    def test_价格表不是合法JSON时明确报错(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ 这不是 json", encoding="utf-8")
        with pytest.raises(engine.PricingDataError):
            engine.load_pricing(str(bad))

    def test_缺少items时明确报错(self, tmp_path):
        empty = tmp_path / "empty.json"
        empty.write_text('{"_meta": {}}', encoding="utf-8")
        with pytest.raises(engine.PricingDataError):
            engine.load_pricing(str(empty))
