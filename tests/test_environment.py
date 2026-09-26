"""
室内环境风险（AC-20）。

═══════════════════════════════════════════════════════════════════
本文件守的核心：**不许出现浓度数字。**
═══════════════════════════════════════════════════════════════════
AC-20 要"标注甲醛/TVOC 风险"。风险等级可以推，浓度不能推 ——
让模型看着一张俯视平面图给出「甲醛 0.06 mg/m³」是一个**看起来很专业的
编造**，而用户会拿它当依据决定要不要住进去。

所以有一条用例专门**扫描输出里有没有 mg/m³ 这类单位**。它看起来笨，
但它守的是"这条功能永远不会变成编造浓度"这件事 —— 靠人自觉是守不住的，
尤其是将来有人觉得"加个数值更直观"的时候。
"""

from __future__ import annotations

import json
import re

import pytest

from backend.app.services.environment import assess_environment

#: 通风良好的户型：两扇窗、两个朝向（能形成对流）
AIRY = {
    "rooms": [{"name": "客厅"}, {"name": "主卧"}],
    "windows": [
        {"position": [600, 50], "width": 1.8, "orientation": "south"},
        {"position": [50, 300], "width": 1.2, "orientation": "east"},
    ],
}
#: 通风差的户型：只有一扇窗
STUFFY = {
    "rooms": [{"name": "客厅"}],
    "windows": [{"position": [600, 50], "width": 1.8, "orientation": "south"}],
}


def mat(*items: tuple[str, str, str]) -> dict:
    return {"items": [
        {"name": n, "category": c, "eco_level": lv} for n, c, lv in items
    ]}


class TestNeverFabricatesConcentration:
    def test_输出里不出现任何浓度单位(self):
        """
        ⚠️ **这条是本文件存在的主要理由。**

        扫描整个返回体的 JSON：不许出现 mg/m³、mg/m3、ppm、μg/m³ 之类。
        判据必须来自**材料等级与通风条件**，而不是一个数字。
        """
        payload = assess_environment(
            AIRY, mat(("E1 密度板门", "door", "E1"), ("E0 乳胶漆", "paint", "E0")),
        )
        blob = json.dumps(payload, ensure_ascii=False)
        for unit in ("mg/m", "mg/m3", "µg", "μg", "ppm", "毫克", "微克"):
            assert unit not in blob, (
                f"输出里出现了浓度单位 {unit!r} —— "
                f"平面图推不出浓度，这是编造：{blob}"
            )

    def test_必须带上免责声明(self):
        """缺了这句话，上面的风险档就会被当成检测结论。"""
        payload = assess_environment(AIRY, mat(("E0 地板", "floor", "E0")))
        assert "不是浓度检测值" in payload["disclaimer"]
        assert "检测机构" in payload["disclaimer"], "要说清该由谁核定"


class TestRiskByMaterialGrade:
    def test_全E0且通风好是低风险(self):
        r = assess_environment(AIRY, mat(
            ("E0 实木复合地板", "floor", "E0"),
            ("E0 室内门", "door", "E0"),
            ("E0 乳胶漆", "paint", "E0"),
        ))
        assert r["formaldehyde"]["risk"] == "low"
        assert r["formaldehyde"]["insufficient_data"] is False
        assert r["tvoc"]["risk"] == "low"

    def test_混进E1就是中风险(self):
        """最差的那一项决定风险档 —— 集合运算，不是平均。"""
        r = assess_environment(AIRY, mat(
            ("E0 实木复合地板", "floor", "E0"),
            ("E1 密度板门", "door", "E1"),
        ))
        assert r["formaldehyde"]["risk"] == "medium"
        # 依据里要能看出是哪一项拉高了风险，否则用户无从改善
        assert any("E1" in b for b in r["formaldehyde"]["basis"])

    def test_通风差会上调一档并写明原因(self):
        ok = assess_environment(AIRY, mat(("E0 地板", "floor", "E0")))
        bad = assess_environment(STUFFY, mat(("E0 地板", "floor", "E0")))
        assert ok["formaldehyde"]["risk"] == "low"
        assert bad["formaldehyde"]["risk"] == "medium"
        assert any("通风" in b for b in bad["formaldehyde"]["basis"])

    def test_上调不会超过高档(self):
        """E1 + 通风差 = 高；再差也只是高，不该出现"更高"。"""
        r = assess_environment(STUFFY, mat(("E1 密度板门", "door", "E1")))
        assert r["formaldehyde"]["risk"] == "high"

    def test_马桶和灯的等级不影响甲醛判断(self):
        """
        判据是"人造板材与胶粘剂"，不是"凡是材料都算"。
        洁具/灯具在目录里根本没有释放等级（`—`），把它们拉进来只会
        稀释结论。
        """
        r = assess_environment(AIRY, mat(
            ("E0 地板", "floor", "E0"),
            ("某洁具", "sanitary", "—"),
            ("某灯具", "lighting", "—"),
        ))
        assert r["formaldehyde"]["risk"] == "low"
        assert len(r["formaldehyde"]["basis"]) == 1, (
            f"不该把无关品类写进依据：{r['formaldehyde']['basis']}"
        )


class TestHonestUnknown:
    """不知道就说不知道 —— 不拿"没数据"当"没问题"。"""

    def test_没有相关材料时是unknown而不是low(self):
        r = assess_environment(AIRY, mat(("某灯具", "lighting", "—")))
        assert r["formaldehyde"]["risk"] == "unknown"
        assert r["formaldehyde"]["insufficient_data"] is True
        assert "没有涉及" in r["formaldehyde"]["basis"][0]

    def test_没有任何材料时也是unknown(self):
        r = assess_environment(AIRY, {})
        assert r["formaldehyde"]["risk"] == "unknown"
        assert r["tvoc"]["risk"] == "unknown"

    def test_等级都没有时要说明是哪几项(self):
        r = assess_environment(AIRY, mat(("某洁具", "floor", "—")))
        assert r["formaldehyde"]["risk"] == "unknown"
        assert any("没有可用于判定" in b for b in r["formaldehyde"]["basis"])

    def test_瓷砖的A类不算甲醛等级(self):
        """
        ⚠️ 易错点：「A 类」是 GB 6566 对**建筑材料放射性核素**的分级
        （A 类可用于任何场所），与甲醛释放量是两套东西。

        瓷砖本来就不在甲醛品类里（判据是 floor/door/cabinet），所以它
        不会进甲醛结论；这里额外钉住"就算有人把瓷砖放进判据，
        A 类也不会被当成释放等级"。
        """
        r = assess_environment(AIRY, mat(("A 类瓷砖", "tile", "A 类")))
        assert r["formaldehyde"]["risk"] == "unknown"
        # 把 A 类塞进 floor 品类也不该被映射成某个等级
        r2 = assess_environment(AIRY, mat(("A 类瓷砖", "floor", "A 类")))
        assert r2["formaldehyde"]["risk"] == "unknown", (
            "A 类被当成了甲醛释放等级 —— 那是两套国标"
        )


class TestVentilationJudge:
    def test_无窗判为通风差(self):
        r = assess_environment({"windows": []}, mat(("E0 地板", "floor", "E0")))
        assert r["ventilation"]["poor"] is True
        assert "未识别到窗户" in r["ventilation"]["basis"]

    def test_两扇窗同朝向不算对流(self):
        r = assess_environment({"windows": [
            {"orientation": "south"}, {"orientation": "south"},
        ]}, mat(("E0 地板", "floor", "E0")))
        assert r["ventilation"]["poor"] is True
        assert "同一朝向" in r["ventilation"]["basis"]

    def test_两扇窗不同朝向算有对流(self):
        r = assess_environment(AIRY, mat(("E0 地板", "floor", "E0")))
        assert r["ventilation"]["poor"] is False

    def test_布局缺失时不报错(self):
        r = assess_environment(None, mat(("E0 地板", "floor", "E0")))
        assert r["ventilation"]["poor"] is True
        assert r["computed_by"] == "rule_engine"


class TestWiredIntoPlans:
    """接线：三套方案的产出里真的带上了这一块。"""

    def test_每套方案各算各的(self):
        """
        ⚠️ 三套方案档位不同、选材不同，风险**本来就不该一样**。
        这也正是把它放在 fan-in 而不是诊断里的原因 ——
        放在诊断里只能给整个户型一个笼统的结论。
        """
        from backend.app.graph.workflow import assemble_plans

        state = {
            "layout": AIRY,
            "styles": ["modern", "chinese"],
            "budget_grades": ["economy", "high"],
            "plan_bundles": {
                "plan_modern_economy": {
                    "space_plan": {"summary": "x"},
                    "materials": mat(("E1 密度板门", "door", "E1")),
                },
                "plan_chinese_high": {
                    "space_plan": {"summary": "y"},
                    "materials": mat(("E0 室内门", "door", "E0")),
                },
            },
        }
        plans = assemble_plans(state)["plans"]
        by_id = {p["plan_id"]: p for p in plans}

        assert by_id["plan_modern_economy"]["environment"]["formaldehyde"]["risk"] == "medium"
        assert by_id["plan_chinese_high"]["environment"]["formaldehyde"]["risk"] == "low"

    def test_没选材的方案也能产出(self):
        """材料缺失不该让整条 fan-in 挂掉 —— 那一块如实返回 unknown。"""
        from backend.app.graph.workflow import assemble_plans

        state = {
            "layout": AIRY, "styles": ["modern"], "budget_grades": ["economy"],
            "plan_bundles": {"plan_modern_economy": {"space_plan": {"summary": "x"}}},
        }
        plan = assemble_plans(state)["plans"][0]
        assert plan["environment"]["formaldehyde"]["risk"] == "unknown"
