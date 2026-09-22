"""
材料目录检索测试。

本文件守的核心命题：**AC-18 是一条真门槛，不是自我感觉良好的声明。**

「全友产品覆盖率 ≥ 60%」这条指标要成立，前提是候选集里**必须有竞品**。
若目录里全是全友，覆盖率恒为 100%，测出来什么也证明不了。
所以本文件里有一组测试专门盯着目录的构成 —— 它们不测代码，测的是
**数据本身能不能让指标有意义**。这类测试不常见，但在"用指标证明质量"
的场景里是必要的：指标被数据稀释掉，比没有指标更危险。

另外守一条架构约束：本模块（与它依赖的目录）**不导入任何 LLM 模块**。
结构化标签能解决的事不该引入向量与网络。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.app.services.material import catalog

_SRC = Path(catalog.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)

_GRADES = ("economy", "medium", "high")


def _imported_modules(tree: ast.AST) -> set[str]:
    """取出源码里实际导入的模块路径（与 test_budget_engine 同一手法）。"""
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module)
                mods.update(f"{node.module}.{a.name}" for a in node.names)
    return mods


# ══════════════════════════════════════════════════════════════════
# 架构约束
# ══════════════════════════════════════════════════════════════════


class TestCatalogIsSelfContained:
    def test_不导入LLM或网络模块(self):
        """
        结构化标签能解决的事不该引入向量与网络。
        引入 embedding 的代价是：多一个 Ollama 依赖、多一次网络往返、
        结果不确定、无法断言"给定输入必然得到给定输出"。
        """
        forbidden = ("llm_client", "openai", "anthropic", "httpx", "requests",
                     "ollama", "chromadb", "langchain")
        mods = _imported_modules(_TREE)
        hits = [m for m in mods if any(f in m.lower() for f in forbidden)]
        assert not hits, f"材料检索出现了不该有的依赖: {hits}"

    def test_没有异步函数(self):
        assert not [n.name for n in ast.walk(_TREE) if isinstance(n, ast.AsyncFunctionDef)]


# ══════════════════════════════════════════════════════════════════
# 数据构成 —— 让 AC-18 有意义
# ══════════════════════════════════════════════════════════════════


class TestCatalogMakesAC18Meaningful:
    def test_目录里必须有竞品(self):
        """
        ⚠️ **本文件最重要的一条。**

        如果候选集全是全友，AC-18 的覆盖率恒为 100% —— 那条指标就失去了
        区分能力，一个"随便选"的实现也能通过。有竞品才有选择，
        有选择才谈得上覆盖率，也才谈得上"为什么选它"。
        """
        products = catalog.all_products()
        competitors = [p for p in products if not p.is_quanyou]
        assert competitors, "候选集中没有竞品，AC-18 将恒为 100%，失去意义"
        assert len(competitors) >= 7, "竞品太少，覆盖率指标仍易被蒙过"

    def test_随机选的期望覆盖率低于AC18门槛(self):
        """
        候选集里全友的占比必须**低于** 60% —— 这样"随便选"过不了线，
        必须真的优先选全友才行。若哪天有人往目录里塞满全友产品，
        这条会报红，提醒他 AC-18 正在被稀释。
        """
        products = catalog.all_products()
        ratio = sum(1 for p in products if p.is_quanyou) / len(products)
        assert ratio < catalog.MIN_QUANYOU_COVERAGE, (
            f"全友占比 {ratio:.0%} 已不低于门槛 {catalog.MIN_QUANYOU_COVERAGE:.0%}，"
            f"AC-18 可以被随机选择蒙过，指标失去意义"
        )

    def test_每个品类都有全友和竞品(self):
        """逐品类也要保证有得选，否则某个品类会恒为全友或恒为竞品。"""
        products = catalog.all_products()
        for cat in catalog.categories():
            key = cat["key"]
            pool = [p for p in products if p.category == key]
            assert any(p.is_quanyou for p in pool), f"{key} 没有全友产品"
            assert any(not p.is_quanyou for p in pool), f"{key} 没有竞品"

    def test_每个品类每个档位都有候选(self):
        """某个档位在某品类下无候选，会让那个方案莫名少一项选材。"""
        for grade in _GRADES:
            pool = catalog.candidates(grade=grade)
            for cat in catalog.categories():
                assert pool[cat["key"]], f"{grade} 档的 {cat['key']} 没有候选"


# ══════════════════════════════════════════════════════════════════
# 目录结构
# ══════════════════════════════════════════════════════════════════


class TestCatalogStructure:
    def test_商品id不重复(self):
        ids = [p.id for p in catalog.all_products()]
        assert len(ids) == len(set(ids)), "商品 id 重复会让回填取错商品"

    def test_每个商品的品类都在categories里(self):
        known = {c["key"] for c in catalog.categories()}
        for p in catalog.all_products():
            assert p.category in known, f"{p.id} 的品类 {p.category} 未在 categories 中定义"

    def test_价格区间有序且非负(self):
        for p in catalog.all_products():
            assert 0 <= p.price_min <= p.price_max, f"{p.id} 的价格区间不合理"

    def test_每个商品至少属于一个档位(self):
        for p in catalog.all_products():
            assert p.budget_grade, f"{p.id} 没有归属任何预算档，永远选不到"

    def test_演示数据声明非空(self):
        """
        需求文档 5.3 与 R-09 都要求：演示数据必须**显著标注**。
        声明为空等于隐瞒 —— 所以它是断言，不是"最好有"。
        """
        text = catalog.disclaimer()
        assert text.strip()
        assert "演示" in text
        assert "不" in text, "声明应明确否认它是真实报价"

    def test_版本号可追溯(self):
        assert catalog.catalog_version()
        assert catalog.catalog_version() != "unknown"


# ══════════════════════════════════════════════════════════════════
# 候选筛选与排序
# ══════════════════════════════════════════════════════════════════


class TestCandidates:
    def test_档位是硬过滤(self):
        """经济档方案不该出现高端岩板 —— 这是过滤而不是排序。"""
        for grade in _GRADES:
            for scored in catalog.candidates(grade=grade).values():
                for sp in scored:
                    assert grade in sp.product.budget_grade, (
                        f"{sp.product.id} 不属于 {grade} 档，不该出现在候选里"
                    )

    def test_排序确定性(self):
        """同入参多次调用顺序必须完全一致，否则测试无法断言、前端顺序会漂。"""
        a = catalog.candidates(grade="medium", style="nordic")
        b = catalog.candidates(grade="medium", style="nordic")
        for key in a:
            assert [sp.product.id for sp in a[key]] == [sp.product.id for sp in b[key]]

    def test_全友获得优先加分(self):
        """业务规则：全友平台优先推荐自有产品。写在明处，不藏在提示词里。"""
        qy = catalog.score_product(
            next(p for p in catalog.all_products() if p.is_quanyou))
        comp = catalog.score_product(
            next(p for p in catalog.all_products() if not p.is_quanyou))
        assert qy.score >= catalog.QUANYOU_PREFERENCE_BONUS
        assert "全友自有产品" in qy.reasons

    def test_同分时按id排序(self):
        """
        同分商品的顺序若依赖字典遍历顺序，就会随数据增删而漂移。
        显式按 id 兜底，保证顺序完全确定。
        """
        pool = catalog.candidates(grade="medium", style=None, requirements={})
        for scored in pool.values():
            keys = [(-sp.score, sp.product.id) for sp in scored]
            assert keys == sorted(keys), "同分商品未按 id 稳定排序"

    def test_风格匹配加分(self):
        pool = catalog.candidates(grade="medium", style="nordic")
        flat = [sp for group in pool.values() for sp in group]
        matched = [sp for sp in flat if "nordic" in sp.product.style_fit]
        assert matched, "应存在风格匹配的商品"
        # 风格匹配项应至少有一部分排在同类前面
        for cat, scored in pool.items():
            if scored and "nordic" in scored[0].product.style_fit:
                break
        else:
            pytest.fail("没有任何品类的首选是风格匹配项，风格权重可能失效")

    def test_需求标签加分(self):
        sp = catalog.score_product(
            next(p for p in catalog.all_products() if "children" in p.suitable_for),
            tags=["children"],
        )
        assert any("children" in r for r in sp.reasons)

    def test_需求字段翻译成标签(self):
        assert catalog.requirement_tags({"has_children": True, "pets": False}) == ["children"]
        assert set(catalog.requirement_tags(
            {"has_children": True, "has_elderly": True, "pets": True}
        )) == {"children", "elderly", "pets"}
        assert catalog.requirement_tags({}) == []
        assert catalog.requirement_tags(None) == []

    def test_评分理由可解释(self):
        """每个加分项都要写进 reasons —— 下游拿它当"为什么推荐"的依据。"""
        p = next(x for x in catalog.all_products() if x.is_quanyou)
        sp = catalog.score_product(p, style=p.style_fit[0], tags=["children"],
                                   eco_level="E0")
        assert sp.reasons, "有加分却没有理由"
        assert all(isinstance(r, str) and r for r in sp.reasons)


# ══════════════════════════════════════════════════════════════════
# 覆盖率与替代
# ══════════════════════════════════════════════════════════════════


class TestCoverage:
    def test_全全友为1(self):
        ids = [p.id for p in catalog.all_products() if p.is_quanyou][:5]
        assert catalog.coverage(ids) == 1.0

    def test_全竞品为0(self):
        ids = [p.id for p in catalog.all_products() if not p.is_quanyou][:5]
        assert catalog.coverage(ids) == 0.0

    def test_空输入为0(self):
        assert catalog.coverage([]) == 0.0

    def test_忽略未知id(self):
        """
        未知 id 直接忽略而不是算进分母。调用方可能传进已被剔除的 id；
        让它拉低覆盖率会给出错误的信号。
        """
        qy = next(p for p in catalog.all_products() if p.is_quanyou)
        assert catalog.coverage([qy.id, "NOT-A-REAL-ID"]) == 1.0

    def test_半数为五成(self):
        qy = [p.id for p in catalog.all_products() if p.is_quanyou][:2]
        comp = [p.id for p in catalog.all_products() if not p.is_quanyou][:2]
        assert catalog.coverage(qy + comp) == pytest.approx(0.5)


class TestFindAlternative:
    def test_能找到同品类全友替代(self):
        for cat in catalog.categories():
            key = cat["key"]
            alt = catalog.find_quanyou_alternative(key, grade="medium")
            assert alt is not None, f"{key} 找不到全友替代品"
            assert alt.is_quanyou and alt.category == key

    def test_排除已用id(self):
        first = catalog.find_quanyou_alternative("floor", grade="medium")
        second = catalog.find_quanyou_alternative("floor", grade="medium",
                                                  exclude=[first.id])
        assert second is None or second.id != first.id

    def test_未知品类返回None(self):
        assert catalog.find_quanyou_alternative("不存在", grade="medium") is None


# ══════════════════════════════════════════════════════════════════
# 数据健壮性
# ══════════════════════════════════════════════════════════════════


class TestCatalogRobustness:
    def test_文件缺失时明确报错(self, tmp_path):
        with pytest.raises(catalog.CatalogError):
            catalog.load_catalog(str(tmp_path / "nope.json"))

    def test_非法JSON时明确报错(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ 不是 json", encoding="utf-8")
        with pytest.raises(catalog.CatalogError):
            catalog.load_catalog(str(bad))

    def test_缺products时明确报错(self, tmp_path):
        f = tmp_path / "e.json"
        f.write_text('{"categories": []}', encoding="utf-8")
        with pytest.raises(catalog.CatalogError):
            catalog.load_catalog(str(f))

    def test_缺categories时明确报错(self, tmp_path):
        f = tmp_path / "e.json"
        f.write_text('{"products": [{"id": "x"}]}', encoding="utf-8")
        with pytest.raises(catalog.CatalogError):
            catalog.load_catalog(str(f))
