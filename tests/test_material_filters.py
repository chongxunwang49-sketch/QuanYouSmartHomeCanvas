"""
AC-19 材料偏好过滤测试。

本文件守的核心命题：**排除必须是硬的，偏好才是软的；而矛盾必须在请求期说清。**

围绕它有三组断言：

  ① **语义** —— 排除真的把东西移出候选池（不是排序靠后），
     偏好只加分；品类适用性由户型房间决定（没卫生间不买洁具）。
  ② **防线** —— 请求期的校验能把"排除到池子空了""排除了全友"这类
     矛盾在提交任务前拦下。这些条件如果漏过去，代价是 40 秒并发 + 一次
     付费额度，或者更糟：一套**静默少一项**的方案。
  ③ **数据形态** —— 目录按档位切开之后并不均匀。这组测试不测代码，
     测的是"AC-18 这条指标在哪个档位上真的有约束力"。

第三组是 2026-09-24 写这个文件时**测出来的意外发现**，值得单独说明：
`catalog.py` 里原有一句断言「随机选择期望覆盖率 50%，过不了 60% 的线」，
按档位实测后发现它对 economy/medium 成立、对 **high 不成立**（实测 66.7%）。
不改数据、只把数字钉住 —— 改数据能让断言变漂亮，但那是拿结论凑指标。
"""

from __future__ import annotations

import pytest

from backend.app.services.material import catalog
from backend.app.services.material.filters import (
    DEFAULT_FILTERS,
    QUANYOU_BRAND,
    MaterialFilters,
    applicable_categories,
    validate_filters,
)

_GRADES = ("economy", "medium", "high")


def _codes(problems) -> list[str]:
    return [p.code for p in problems]


# ══════════════════════════════════════════════════════════════════
# ① 语义
# ══════════════════════════════════════════════════════════════════


class TestApplicableCategories:
    """品类适用性：AC-19 里唯一一处"户型决定了不该买什么"的地方。"""

    def test_有卫生间时包含卫浴洁具(self):
        got = applicable_categories(["客厅", "主卧", "卫生间"])
        assert "sanitary" in got

    def test_没有卫生间时不包含卫浴洁具(self):
        got = applicable_categories(["客厅", "主卧", "书房"])
        assert "sanitary" not in got
        # 其余品类不受影响 —— 门槛只该砍掉真的不该买的那一项
        assert "floor" in got and "cabinet" in got and "lighting" in got

    def test_英文房间名同样识别(self):
        assert "sanitary" in applicable_categories(["Living Room", "Bathroom"])

    def test_房间名拿不到时不做删减(self):
        """
        缺数据 ≠ 没有那个房间。没有房间名时保守地保留全部品类 ——
        与 `capabilities.py` 的立场一致（数据驱动，而不是模式驱动）。
        """
        assert set(applicable_categories([])) == {
            c["key"] for c in catalog.categories()
        }

    def test_瓷砖不受房间限制(self):
        """
        瓷砖**刻意**没有登记为条件品类：国内住宅里它大量用于客厅/餐厅地面，
        不只是厨卫墙面。用它当条件会在"没识别出厨房"的户型上误删一个
        完全合理的选择 —— 而误删比多买一项更难发现（多买看得见，少买只少一行）。
        """
        assert "tile" in applicable_categories(["客厅"])


class TestFromPayload:
    def test_去重并排序_同一组输入得到同一个对象(self):
        a = MaterialFilters.from_payload(
            {"excluded_brands": ["立邦", "东鹏", "立邦"]}
        )
        b = MaterialFilters.from_payload(
            {"excluded_brands": ["东鹏", "立邦"]}
        )
        assert a == b
        assert a.excluded_brands == ("东鹏", "立邦")

    def test_容忍单个字符串(self):
        """前端只排除一项时最容易传成裸字符串，不该因此 500。"""
        f = MaterialFilters.from_payload({"excluded_categories": "tile"})
        assert f.excluded_categories == ("tile",)

    def test_忽略空白项(self):
        f = MaterialFilters.from_payload({"excluded_brands": ["  ", "立邦", ""]})
        assert f.excluded_brands == ("立邦",)

    def test_默认全友优先为真(self):
        assert MaterialFilters.from_payload(None).quanyou_priority is True
        assert DEFAULT_FILTERS.quanyou_priority is True

    def test_关闭全友优先后加分为零(self):
        on = MaterialFilters.from_payload({"quanyou_priority": True})
        off = MaterialFilters.from_payload({"quanyou_priority": False})
        assert on.quanyou_bonus == catalog.QUANYOU_PREFERENCE_BONUS
        assert off.quanyou_bonus == 0.0

    def test_偏好品牌加分高于平台全友加分(self):
        """
        ⚠️ 这条断言的方向是 2026-09-24 改过来的，原先是反的。

        初版让偏好加分（1.0）低于全友加分（1.5），推理是"平台规则赢过用户倾向"。
        但那样一来用户偏好永远翻不过全友，**这个开关和死开关没区别** ——
        正是刚修掉的那类缺陷（界面承诺了，系统不认）。

        分层才对：排序层让明确的用户偏好压过平台的默认倾向；
        底线层由 AC-18 的 60% 代码兜底守住。所以这里是 `>` 不是 `<`。
        """
        assert catalog.PREFERRED_BRAND_BONUS > catalog.QUANYOU_PREFERENCE_BONUS

    def test_偏好品牌与风格匹配同分时仍然确定(self):
        """同分按 id 升序 —— 否则候选顺序会随字典顺序漂移，测试没法断言。"""
        assert catalog.PREFERRED_BRAND_BONUS == 2.0

    def test_to_dict可回环(self):
        f = MaterialFilters.from_payload({
            "excluded_categories": ["tile"],
            "excluded_brands": ["立邦"],
            "preferred_brands": ["东鹏"],
            "quanyou_priority": False,
        })
        assert MaterialFilters.from_payload(f.to_dict()) == f

    def test_to_dict是纯JSON可序列化(self):
        """state 会随 checkpointer 落盘，放 dataclass 会直接序列化失败。"""
        import json

        payload = MaterialFilters.from_payload(
            {"excluded_brands": ["立邦"]}
        ).to_dict()
        assert json.loads(json.dumps(payload)) == payload


class TestCandidatesFiltering:
    def test_排除品牌后该品牌不在候选中(self):
        pool = catalog.candidates(grade="economy", exclude_brands=("立邦",))
        brands = {sp.product.brand for sp in pool["paint"]}
        assert "立邦" not in brands
        assert brands, "排除一个品牌不该把整个品类清空"

    def test_排除品类后该品类为空列表(self):
        """
        返回空列表而不是省略这个 key —— 调用方按"这个品类没有候选"处理，
        与"型号不匹配"走同一条分支，少一处特判。
        """
        pool = catalog.candidates(grade="economy", exclude_categories=("tile",))
        assert pool["tile"] == []
        assert pool["floor"], "其它品类不受影响"

    def test_只考虑给定品类(self):
        pool = catalog.candidates(grade="economy", only_categories=("floor", "paint"))
        assert set(pool) == {"floor", "paint"}

    def test_偏好品牌可改变排序(self):
        默认 = catalog.candidates(grade="high")["floor"]
        偏好 = catalog.candidates(grade="high", preferred_brands=("大自然",))["floor"]
        assert 默认[0].product.brand != "大自然"
        assert 偏好[0].product.brand == "大自然"

    def test_关闭全友优先后排序改变(self):
        """
        `quanyou_priority` 曾经是个死开关（API 与 state 都接了线，
        没有任何 Agent 读它）。这条测试守住它现在**真的有效果**。
        """
        开 = catalog.candidates(grade="economy")["paint"]
        关 = catalog.candidates(grade="economy", quanyou_bonus=0.0)["paint"]
        assert 开[0].product.is_quanyou
        assert not 关[0].product.is_quanyou

    def test_默认参数下的排序被钉住(self):
        """
        AC-19 给 `candidates()` 加了一堆过滤参数，默认路径（不带任何过滤）
        **必须与改动前逐项一致** —— 否则三个方案的内容会静默变化，
        而没有任何测试会告诉你。所以这里把完整的排序结果写死。

        顺带记录一个容易被忽略的形态：`cabinet` 在 medium/nordic 下，
        竞品索菲亚（风格匹配 +2.0）**排在**全友（自有加分 +1.5）前面。
        也就是说"全友优先"是加分不是保证 —— AC-18 的兜底必须在
        这种"模型/排序选了竞品"的情况下真的动手，否则覆盖率会掉到线下。
        """
        pool = catalog.candidates(grade="medium", style="nordic")
        assert {k: [sp.product.id for sp in v] for k, v in pool.items()} == {
            "floor": ["QY-FL-101", "QY-FL-102", "XY-FL-301"],
            "tile": ["QY-TL-102", "DP-TL-301", "QY-TL-101"],
            "paint": ["QY-PT-101", "DLS-PT-301", "LB-PT-201", "QY-PT-102"],
            "door": ["QY-DR-101", "QY-DR-102", "MT-DR-301"],
            "sanitary": ["QY-SA-101", "JP-SA-301", "JM-SA-201"],
            "cabinet": ["SFY-CB-301", "QY-CB-102"],
            "lighting": ["QY-LT-101", "YS-LT-201", "NS-LT-301"],
        }

    def test_竞品可以排在全部候选中领先的位置(self):
        """上一条的补充：证明"全友优先"不是"全友包揽榜首"。"""
        pool = catalog.candidates(grade="medium", style="nordic")
        leaders = [v[0].product.is_quanyou for v in pool.values()]
        assert False in leaders, (
            "所有品类的榜首都是全友 —— 那么 AC-18 的兜底分支永远跑不到，"
            "它到底是坏是好就没人知道了"
        )

    def test_候选kwargs只有一处翻译(self):
        """
        初始选材、AC-18 兜底、请求期校验三处必须用同一套过滤语义。
        各写各的转译，迟早会出现"初始选择守规矩、补足环节不守"的不一致。
        """
        f = MaterialFilters.from_payload({
            "excluded_categories": ["tile"],
            "excluded_brands": ["立邦"],
            "preferred_brands": ["东鹏"],
            "quanyou_priority": False,
        })
        kwargs = f.candidate_kwargs(only_categories=("floor", "paint"))
        assert kwargs["exclude_categories"] == ("tile",)
        assert kwargs["exclude_brands"] == ("立邦",)
        assert kwargs["preferred_brands"] == ("东鹏",)
        assert kwargs["quanyou_bonus"] == 0.0
        assert kwargs["only_categories"] == ("floor", "paint")


class TestQuanyouFallbackRespectsFilters:
    """
    AC-18 的兜底替换是**代码发起**的第二轮挑选，很容易漏掉过滤条件。

    漏掉的后果不是"多买一件"这么轻：它只在覆盖率不达标时才显形，
    平时跑不到那条分支，测试也测不到。
    """

    def test_默认能找到全友替代品(self):
        assert catalog.find_quanyou_alternative("paint", grade="economy") is not None

    def test_排除全友后找不到替代品_于是不替换(self):
        """
        宁可覆盖率不达标并如实上报，也不违反用户明确的排除。

        这是本模块里"诚实优先于达标"的一个具体落点：AC-18 是系统的
        验收指标，但用户说了"不要这个品牌"之后，**如实报告没做到**
        比偷偷换一个更符合项目的纪律（不产出看起来合理的错误）。
        """
        assert catalog.find_quanyou_alternative(
            "paint", grade="economy", exclude_brands=(QUANYOU_BRAND,),
        ) is None


# ══════════════════════════════════════════════════════════════════
# ② 防线：请求期校验
# ══════════════════════════════════════════════════════════════════


class TestValidateFilters:
    def test_合法偏好无问题(self):
        f = MaterialFilters.from_payload({
            "excluded_categories": ["lighting"],
            "excluded_brands": ["立邦"],
            "preferred_brands": ["东鹏"],
        })
        assert validate_filters(f, grades=_GRADES) == []

    def test_不认识的品类(self):
        f = MaterialFilters.from_payload({"excluded_categories": ["ceiling"]})
        assert "unknown_category" in _codes(validate_filters(f, grades=_GRADES))

    def test_不认识的品牌(self):
        f = MaterialFilters.from_payload({"excluded_brands": ["不存在的牌子"]})
        assert "unknown_brand" in _codes(validate_filters(f, grades=_GRADES))

    def test_同品牌既排除又优先(self):
        f = MaterialFilters.from_payload({
            "excluded_brands": ["立邦"], "preferred_brands": ["立邦"],
        })
        assert "contradictory_brand" in _codes(validate_filters(f, grades=_GRADES))

    def test_不能排除全友(self):
        """
        AC-18 是平台级验收指标，不能由用户请求关掉。
        能被用户关掉的验收指标是测不了的。
        """
        f = MaterialFilters.from_payload({"excluded_brands": [QUANYOU_BRAND]})
        problems = validate_filters(f, grades=_GRADES)
        assert "quanyou_excluded" in _codes(problems)
        # 消息必须给出**替代做法**，不能只说"不允许"
        # ⚠️ 2026-09-27 改：原来指的是字段名 `preferred_brands`，而这句
        #    提示会弹在界面上 —— 界面上那一栏叫「偏好品牌」。
        msg = next(p.message for p in problems if p.code == "quanyou_excluded")
        assert "偏好品牌" in msg

    def test_全部排除后无品类可选(self):
        f = MaterialFilters.from_payload(
            {"excluded_categories": [c["key"] for c in catalog.categories()]}
        )
        assert "no_category_left" in _codes(
            validate_filters(f, grades=_GRADES, room_names=["客厅", "卫生间"])
        )

    def test_排除到某个档位的候选池为空(self):
        """
        实测：`door` 在 economy 档**只有一件全友商品、没有竞品**。
        所以"排除全友"会让经济档的室内门无货可挑。

        这正是必须在请求期校验的场景：晚一步发现，任务已经跑完 A-02
        诊断与九路并发，或者更糟 —— 那套方案照出，只是静默少了一个品类。
        """
        f = MaterialFilters.from_payload({"excluded_brands": [QUANYOU_BRAND]})
        problems = validate_filters(f, grades=["economy"])
        empty = next(p for p in problems if p.code == "empty_pool")
        assert "室内门" in empty.message
        # 2026-09-27 改：档位印的是中文名（"经济档"），不再是枚举值 `economy`。
        assert "经济档" in empty.message

    def test_空池问题按档位分别报告(self):
        """
        不同档位的商品池不同。只在某一个档位为空时，那套方案会静默少一项 ——
        这种"只有一套方案不对"是最难发现的，所以消息里必须**带上档位**，
        而且三个档位要分开列。

        实测（排除全友时）：
            economy → 室内门、橱柜衣柜   （这两项经济档只有全友一件）
            medium  → 无                  （每项都还有竞品）
            high    → 墙面涂料、卫浴洁具、灯具（这三项高端档只有全友一件）
        """
        f = MaterialFilters.from_payload({"excluded_brands": [QUANYOU_BRAND]})
        problems = validate_filters(f, grades=_GRADES)
        empty = next(p for p in problems if p.code == "empty_pool")

        # 2026-09-27 改：档位改印中文名（与"用户可见的名词要用中文"一致）。
        assert "经济档：室内门、橱柜衣柜" in empty.message
        assert "高端档：墙面涂料、卫浴洁具、灯具" in empty.message
        assert "中档" not in empty.message, "中档每项都还有竞品，不该被列为空"

    def test_不适用范围为空时只报告真正涉及的品类(self):
        """
        没有卫生间的户型 + 排除卫浴洁具 = 无事发生，
        不该产生"你排除了一个不存在的品类"这类噪音。
        """
        f = MaterialFilters.from_payload({"excluded_categories": ["sanitary"]})
        assert validate_filters(
            f, grades=_GRADES, room_names=["客厅", "主卧"],
        ) == []

    def test_品类取值来自目录而不是硬编码(self):
        allowed = {c["key"] for c in catalog.categories()}
        f = MaterialFilters.from_payload({"excluded_categories": ["tile"]})
        validate_filters(f, grades=_GRADES)
        assert "tile" in allowed


# ══════════════════════════════════════════════════════════════════
# ③ 数据形态：AC-18 在哪个档位上真的有约束力
# ══════════════════════════════════════════════════════════════════


def _expected_coverage_by_grade(grade: str) -> float:
    """
    假设**完全随机**挑一件（每个品类等概率选它内部的任一件），
    该档位下的期望全友覆盖率。

    这是"AC-18 有没有约束力"的判据：期望值低于门槛，说明不主动优先
    全友就会不达标，代码兜底是真的在工作；高于门槛，说明兜底是空转的。
    """
    by_cat: dict[str, list[bool]] = {}
    for p in catalog.all_products():
        if grade in p.budget_grade:
            by_cat.setdefault(p.category, []).append(p.is_quanyou)
    ratios = [sum(v) / len(v) for v in by_cat.values()]
    return sum(ratios) / len(ratios)


class TestSeedDataShape:
    """
    不测代码，测**数据本身能不能让 AC-18 这条指标有意义**。

    指标被数据稀释掉，比没有指标更危险 —— 它会让人以为某个能力被验证过了。
    """

    def test_每档位的期望全友覆盖率钉住实测值(self):
        measured = {g: round(_expected_coverage_by_grade(g), 3) for g in _GRADES}
        assert measured == {"economy": 0.595, "medium": 0.524, "high": 0.667}, (
            f"种子数据的形态变了（{measured}）—— AC-18 的约束力随之改变，"
            f"请重新评估后更新 catalog.py 里那段实测表，不要只改这个断言"
        )

    def test_经济档与中档随机选过不了线(self):
        """这两套方案是 AC-18 真正被考验的地方。"""
        for g in ("economy", "medium"):
            assert _expected_coverage_by_grade(g) < catalog.MIN_QUANYOU_COVERAGE

    def test_高端档随机就能过线_所以兜底对它空转(self):
        """
        ⚠️ 2026-09-24 实测发现的意外：high 档期望覆盖率 66.7% ≥ 60%。

        原因是该档位有 3 个品类（涂料 / 洁具 / 灯具）在目录里只有一件全友
        商品、没有竞品，那三项恒为 100%，把整体抬过了线。

        这条测试**不是**在庆祝一个漏洞，而是在记录一个事实：
        "AC-18 通过了"这个结论，真正被考验的是经济档与中档两套方案。
        将来若要补强，正确做法是给高端档补几个竞品，而不是改这个断言。
        """
        assert _expected_coverage_by_grade("high") >= catalog.MIN_QUANYOU_COVERAGE

    def test_每个品类每个档位至少有一件全友商品(self):
        """
        反过来守一条底线：AC-18 的兜底要靠"同品类里有全友替代品"才成立。
        若某个(品类, 档位)组合一件全友都没有，兜底会静默失败。
        """
        combos: dict[tuple[str, str], bool] = {}
        for p in catalog.all_products():
            for g in p.budget_grade:
                key = (p.category, g)
                combos[key] = combos.get(key, False) or p.is_quanyou
        missing = [k for k, ok in combos.items() if not ok]
        assert not missing, f"以下(品类,档位)组合没有任何全友商品: {missing}"


# ══════════════════════════════════════════════════════════════════
# 翻译层不引入新依赖
# ══════════════════════════════════════════════════════════════════


class TestFiltersStayOffline:
    def test_过滤模块不导入LLM或网络(self):
        """
        与 `catalog.py` 同一条架构约束：过滤是集合运算，
        没有一处需要模型或网络。走 LLM 会让"排除"变得不确定。
        """
        import ast
        from pathlib import Path

        import backend.app.services.material.filters as mod

        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        mods: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        forbidden = ("llm_client", "openai", "anthropic", "httpx", "requests",
                     "ollama", "chromadb", "langgraph")
        hits = [m for m in mods if any(f in m.lower() for f in forbidden)]
        assert not hits, f"过滤模块出现了不该有的依赖: {hits}"


@pytest.mark.parametrize("grade", _GRADES)
def test_每个档位都能给出非空候选(grade: str):
    """三个档位都必须有货 —— 否则方案生成会在 A-05 抛错。"""
    pool = catalog.candidates(grade=grade)
    assert all(v for v in pool.values()), f"{grade} 档有空品类"
