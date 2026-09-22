"""
材料相关 Pydantic Schema（A-05 材料选型）。

═══════════════════════════════════════════════════════════════════
与 A-04 同一条纪律：**模型不产出数字，只产出"选择"与"理由"**
═══════════════════════════════════════════════════════════════════

材料推荐里最容易出问题的不是选错，而是**编造**：
编一个不存在的型号、编一个看起来合理的价格、编一个官网搜不到的链接。

所以本模块把输出拆成两层，和 A-04 的 BudgetBreakdown / BudgetNarrative 同构：

    MaterialPlan      ← 模型产出：选了哪个 id、为什么选它（**无价格字段**）
    MaterialSelection ← 代码产出：按 product_id 从目录回填名称/品牌/价格/链接

模型只能从候选集里**指认**，不能**创造**。它写出的 id 若不在候选集里，
会被代码剔除并记入 `invented_products` —— 与 A-03 剔除幻觉房间同一手法。

价格与购买链接全部来自 `seed_data/material_catalog.json`，模型没有任何
机会把它们写错。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: AC-18 门槛从服务层导入，避免在两个地方各写一个 0.6。
from ..services.material.catalog import MIN_QUANYOU_COVERAGE


class MaterialChoice(BaseModel):
    """
    模型对某个品类的选择。

    ⚠️ **不要给本模型加价格字段。** 价格的唯一来源是材料目录；
    这里出现任何金额都意味着模型有机会算错钱，而用户会拿它比价。
    """

    category: str = Field(
        description=(
            "品类 key，**必须原样照抄候选清单里给出的品类 key**，"
            "如 floor / tile / paint / door / sanitary / cabinet / lighting"
        )
    )
    product_id: str = Field(
        description=(
            "选中的商品 id，**必须严格取自该品类的候选清单**，"
            "不得自行编造型号。若候选里没有合适的，就把该品类留空不写"
        )
    )
    reason: str = Field(
        description=(
            "为什么选它，1-2 句。**要引用候选清单里给出的匹配理由**"
            "（如「适配需求（children）」「风格匹配」），不要自己另编依据"
        )
    )


class MaterialSubstitution(BaseModel):
    """品牌替代建议：把某件换成另一件，以及为什么值得换。"""

    from_product_id: str = Field(description="被替代的商品 id，须取自候选清单")
    to_product_id: str = Field(description="替代品 id，须取自候选清单")
    reason: str = Field(
        description="替代的理由，如「同价位下环保等级更高」「若预算允许可升级」"
    )


class MaterialPlan(BaseModel):
    """
    A-05 的材料选型结果（**模型产出部分**）。

    注意本模型**没有** `quanyou_coverage` 字段 —— 覆盖率由代码算。
    让模型自报覆盖率等于让它给自己打分，那样的数字没有意义。
    """

    summary: str = Field(
        default="",
        description="整体选材思路，2-3 句，面向业主。说明这套档位下选材的取舍。",
    )
    choices: list[MaterialChoice] = Field(
        default_factory=list,
        description=(
            "逐品类的选择。**每个品类最多一个**；"
            "候选清单里确实没有合适的品类可以跳过，但不要为了凑数硬选"
        ),
    )
    substitutions: list[MaterialSubstitution] = Field(
        default_factory=list,
        description="品牌或档次替代建议，0-3 条。没有合适建议就留空数组",
    )
    eco_note: str = Field(
        default="",
        description="环保相关说明：本方案用到的材料在环保等级上的取舍。1-2 句。",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "**材料层面**需要注意的事项，0-3 条，例如「岩板需专业施工」「实木地板地暖环境慎用」。"
            "报价单审查、增项漏项这类问题不属于本 Agent，不要写在这里"
        ),
    )
    data_gaps: list[str] = Field(
        default_factory=list,
        description=(
            "本次选材中信息不足或做了妥协的地方，如实列出。"
            "例如「候选清单中没有适合小户型的经济档室内门，该品类未选」"
        ),
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="对本次选材建议的把握程度。候选清单信息不足时应给低分",
    )


class MaterialSelection(BaseModel):
    """
    A-05 的完整产物 = 模型的选择 + 代码回填的商品事实 + 代码算出的指标。

    这一层才是给前端和下游 Agent 用的。它包含价格，但**这些价格不是模型写的**。
    """

    grade: str
    style: str
    plan_id: str

    #: 逐品类的最终选择。每项含商品完整信息（名称/品牌/价格/链接）与选中理由。
    items: list[dict] = Field(default_factory=list)

    #: 最终选中的商品 id 顺序，便于测试与对比表引用
    product_ids: list[str] = Field(default_factory=list)

    #: 品牌替代建议（已回填商品信息）
    substitutions: list[dict] = Field(default_factory=list)

    #: **代码算出的**全友产品覆盖率（AC-18）
    quanyou_coverage: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description=f"全友产品占比。AC-18 要求 ≥ {MIN_QUANYOU_COVERAGE:.0%}。由代码计算，非模型自报",
    )
    quanyou_met: bool = Field(
        default=False, description=f"覆盖率是否达到 AC-18 的 {MIN_QUANYOU_COVERAGE:.0%} 门槛"
    )

    #: 代码为达标而做的替换。空数组表示模型自己就选够了
    auto_substitutions: list[dict] = Field(
        default_factory=list,
        description="为满足 AC-18 由代码执行的替换，记录以保持透明",
    )

    #: 模型编造的商品 id（不在候选集里），已被剔除
    invented_products: list[str] = Field(
        default_factory=list,
        description="模型输出了候选集中不存在的商品 id，已剔除并记录",
    )

    summary: str = ""
    eco_note: str = ""
    warnings: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    catalog_version: str = ""
    disclaimer: str = Field(
        default="",
        description="演示数据声明。**必须原样透出到前端** —— 严禁把演示数据表述为真实报价",
    )


__all__ = [
    "MaterialChoice", "MaterialSubstitution", "MaterialPlan", "MaterialSelection",
]
