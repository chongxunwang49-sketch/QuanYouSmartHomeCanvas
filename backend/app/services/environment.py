"""
室内环境风险（甲醛 / TVOC）—— AC-20。

═══════════════════════════════════════════════════════════════════
本模块绝不输出浓度数字。这是设计的第一条，不是风格问题。
═══════════════════════════════════════════════════════════════════
AC-20 的原文是「输出环保评分，标注甲醛/TVOC 风险」。**风险等级可以推，
浓度不能推**：室内甲醛浓度是（材料释放量 × 装载度 × 换气次数 × 时间 × 温度）
的函数，还要靠现场检测。让模型看着一张俯视平面图给出「甲醛 0.06 mg/m³」，
是一个**看起来很专业的编造** —— 而用户会拿它当依据决定要不要住进去。

所以这里只做两件**可断言**的事：

  ① **按材料环保等级定风险档**。材料目录里每一项都带 `eco_level`
     （实测分布：地板 4 项全 E0、涂料 4 项全 E0、室内门 3×E0 + 1×E1、
     橱柜 3×E0 + 1×E1；瓷砖的「A 类」是 GB 6566 的**放射性**分级，
     与甲醛无关，不能混进这条判据）。
     判定规则就是"这批材料里最差的那一项决定风险档" —— 集合运算，
     确定、可复现、能写断言。

  ② **用户型本身的通风条件做修正**。对流差（窗户少 / 全在同一朝向）
     意味着同样的材料释放量更难散出去，风险上调一档。

两者的依据都是**已有数据**，没有一处需要模型发挥。所以本模块是纯函数、
无 LLM，与 ADR-07（预算必须由规则引擎算）同一条纪律。

═══════════════════════════════════════════════════════════════════
为什么在 fan-in 而不是诊断里做
═══════════════════════════════════════════════════════════════════
需求文档把它列在诊断那一段，但 A-02 诊断时**还没有材料**——
选材（A-05）在并行分支里，与诊断同时跑。所以"按材料定风险"只能在
**材料齐了之后**做，也就是 fan-in。

而且放在 fan-in 反而更有用：三套方案的档位不同、选材不同，
风险等级本来就应该**不一样**（经济档混了 E1 板材，高端档全 E0）。
在诊断里做，只能给整个户型一个笼统的数字。
"""

from __future__ import annotations

from typing import Any, Literal

__all__ = [
    "RiskLevel", "HazardItem", "assess_environment",
    "FORMALDEHYDE_CATEGORIES", "TVOC_CATEGORIES", "DISCLAIMER",
]

RiskLevel = Literal["low", "medium", "high", "unknown"]

#: 会释放甲醛的品类。**判据是"人造板材与胶粘剂"**，不是"看起来像木头的"。
FORMALDEHYDE_CATEGORIES: tuple[str, ...] = ("floor", "door", "cabinet")

#: TVOC 的主要来源：涂料与胶粘剂。目录里目前只有 `paint` 一个品类。
TVOC_CATEGORIES: tuple[str, ...] = ("paint",)

#: 环保等级 → 风险档。**没登记的等级一律 unknown**，不往下猜。
#:
#: 口径依据（需求文档附录 C 的术语表）：
#:   ENF（无醛添加）< E0 < E1 —— 数值越小释放量越低。
#:   实测目录里目前最高到 E0，ENF 留在这里是为了将来加品时不至于漏判。
#: ⚠️ 「A 类」不在表里 —— 它是 GB 6566 对**建筑材料放射性核素**的分级
#:    （A 类可用于任何场所），与甲醛释放量是两套东西。把瓷砖的 A 类
#:    当成"环保等级好"写进甲醛结论，就是拿一个正确的标签说一件错的事。
_LEVEL_TO_RISK: dict[str, RiskLevel] = {
    "ENF": "low",
    "E0": "low",
    "E1": "medium",
    "E2": "high",
}

#: 风险档的序，用于"通风差上调一档"。
_ORDER: tuple[RiskLevel, ...] = ("low", "medium", "high")


def _worse(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    """取更差的一档；unknown 不参与比较（不知道不等于好，也不等于坏）。"""
    if a == "unknown":
        return b
    if b == "unknown":
        return a
    return a if _ORDER.index(a) >= _ORDER.index(b) else b


class HazardItem(dict):
    """
    一条风险结论。用 dict 而不是 dataclass，因为它直接进 API 响应，
    而 pydantic 那边是按 dict 校验的 —— 中间少一次转换就少一处漂移。

    字段与 `LayoutDiagnosis` 的 `DiagnosisItem` 对齐（`insufficient_data`
    是同一个语义）：**宁可显式说"评不了"，也不给一个假分数。**
    """


def _assess_one(
    *,
    label: str,
    categories: tuple[str, ...],
    items: list[dict[str, Any]],
    ventilation_poor: bool,
    ventilation_basis: str,
) -> dict[str, Any]:
    """算一个危害项。抽出来是因为甲醛与 TVOC 的算法完全同构。"""
    relevant = [
        it for it in items
        if str(it.get("category") or "") in categories
    ]
    if not relevant:
        return {
            "risk": "unknown",
            "insufficient_data": True,
            "basis": [
                f"这套方案没有涉及{label}相关品类"
                f"（{'、'.join(categories)}），无法据此评估"
            ],
            "note": "没有可判定的材料项，因此不给风险档 —— 不拿'没数据'当'没问题'。",
        }

    basis: list[str] = []
    risk: RiskLevel = "unknown"
    unrated: list[str] = []

    for it in relevant:
        level = str(it.get("eco_level") or "—")
        mapped = _LEVEL_TO_RISK.get(level)
        if mapped is None:
            # 「—」= 目录里就没标等级；「A 类」= 放射性分级，不是释放等级。
            # 两者都进 unrated，不参与定级。
            unrated.append(f"{it.get('name')}（{level}）")
            continue
        basis.append(f"{it.get('name')} 为 {level} 级")
        risk = _worse(risk, mapped)

    if risk == "unknown":
        return {
            "risk": "unknown",
            "insufficient_data": True,
            "basis": basis + [
                f"涉及的 {len(relevant)} 项材料都没有可用于判定的环保等级："
                f"{'、'.join(unrated)}"
            ],
            "note": "目录里这些品类没有登记释放等级，因此不推断风险。",
        }

    if unrated:
        basis.append(
            f"另有 {len(unrated)} 项无等级数据、未参与定级：{'、'.join(unrated)}"
        )

    if ventilation_poor:
        # ⚠️ 上调一档的理由必须写出来 —— 否则用户看到"中"却不知道
        #    是因为材料还是因为户型，也就无从改善。
        before = risk
        idx = min(_ORDER.index(risk) + 1, len(_ORDER) - 1)
        risk = _ORDER[idx]
        if risk != before:
            basis.append(f"户型通风条件偏弱（{ventilation_basis}），风险上调一档")

    return {
        "risk": risk,
        "insufficient_data": False,
        "basis": basis,
        "note": "",
    }


def _ventilation_of(layout: dict[str, Any]) -> tuple[bool, str]:
    """
    户型通风是否偏弱。返回 (是否偏弱, 一句依据)。

    判据只用**可数的事实**：窗户数量、以及能否形成对流。
    "对流"的定义收敛为"至少两扇窗且朝向不止一种" —— 这是保守判据
    （真实对流还看开窗位置与户型通透性），所以宁可用它说"不弱"，
    也不去编一个更精细的判断。
    """
    windows = layout.get("windows") or []
    n = len(windows)
    orientations = {
        str((w or {}).get("orientation") or "unknown").lower() for w in windows
    }
    orientations.discard("")
    orientations.discard("unknown")

    if n == 0:
        return True, "未识别到窗户"
    if n == 1:
        return True, "只有 1 扇窗，难以形成对流通风"
    if len(orientations) < 2:
        return True, f"{n} 扇窗都在同一朝向，无法形成对流通风"
    return False, f"{n} 扇窗、{len(orientations)} 个朝向，具备对流通风条件"


#: **每一份环境评估都必须带上这句话。** 它不是免责声明式的客套 ——
#: 它划清了"我们推的是什么"和"我们没测什么"，缺了它，
#: 上面那些风险档就会被当成检测结论。
DISCLAIMER = (
    "以上是按**材料环保等级与户型通风条件**推出的风险等级，"
    "**不是浓度检测值**。国标限量（如 GB 18580 对人造板的释放限量）"
    "须由具备资质的检测机构在装修完工后现场采样出具报告核定。"
)


def assess_environment(
    layout: dict[str, Any] | None, materials: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    给一套方案算室内环境风险。

    Args:
        layout: 户型 JSON（用它的窗户/朝向判通风）。
        materials: A-05 的产出（用 `items[].eco_level` 定风险档）。

    Returns:
        可直接进 API 响应的 dict；`items[]` 为空或布局缺失时如实返回
        `insufficient_data`，**不给分数、不给浓度**。
    """
    layout = layout or {}
    items = list((materials or {}).get("items") or [])
    poor, why = _ventilation_of(layout)

    return {
        "formaldehyde": _assess_one(
            label="甲醛", categories=FORMALDEHYDE_CATEGORIES,
            items=items, ventilation_poor=poor, ventilation_basis=why,
        ),
        "tvoc": _assess_one(
            label="TVOC", categories=TVOC_CATEGORIES,
            items=items, ventilation_poor=poor, ventilation_basis=why,
        ),
        "ventilation": {"poor": poor, "basis": why},
        "computed_by": "rule_engine",
        "disclaimer": DISCLAIMER,
    }
