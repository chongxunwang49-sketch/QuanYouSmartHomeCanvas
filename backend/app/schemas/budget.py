"""
预算相关 Pydantic Schema（A-04 预算造价）。

本文件把 ADR-07（**预算数字由规则引擎计算，LLM 不得生成金额**）
从一条"约定"变成**类型系统层面的保证**：

    BudgetLine / BudgetBreakdown  ← 有金额字段，**只由 engine.py 产出**
    BudgetNarrative               ← 无金额字段，**唯一交给 LLM 的部分**

这两组模型从来不互相赋值。LLM 拿到的是 `BudgetBreakdown` 的文本化结果，
被要求返回 `BudgetNarrative`；由于叙事模型里**根本没有存放金额的字段**，
模型即使想编一个数字出来也无处安放 —— 它只可能出现在自由文本 summary 里，
而那是叙述性文字，不会被任何计算或展示逻辑当作数值使用。

这比在提示词里写一句"不要给金额"可靠得多：提示词是**降低概率**，
类型是**消除路径**。二者的差别在 [[面试亮点]] 里也记过，同 A-03 的处理思路。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 计价基准。决定 quantity 怎么算。
PriceBasis = Literal[
    "area",           # 按套内面积
    "wall_area",      # 按墙面面积（套内面积 × 系数）
    "subtotal_pct",   # 按施工费小计的百分比
]
BudgetGrade = Literal["economy", "medium", "high"]


class BudgetLine(BaseModel):
    """预算表的一个分项。字段口径与 DB 的 `design_plans.breakdown` 一致。"""

    key: str = Field(description="分项标识，如 plumbing_electrical")
    label: str = Field(description="分项中文名，如「水电改造」")
    unit: str = Field(description="计价单位，如「元/㎡」")
    basis: PriceBasis = Field(description="计价基准")

    quantity: float = Field(
        description="工程量。basis=area/wall_area 时是面积；basis=subtotal_pct 时是百分比数值"
    )
    unit_price_min: float = Field(description="单价下限")
    unit_price_max: float = Field(description="单价上限")
    amount_min: float = Field(description="本分项金额下限 = quantity × unit_price_min")
    amount_max: float = Field(description="本分项金额上限 = quantity × unit_price_max")
    note: str = Field(default="", description="该分项的计价说明，用于向用户解释")


class BudgetBreakdown(BaseModel):
    """
    一次完整的预算计算。**只由 `services/budget/engine.py` 产出。**

    ⚠️ 这个模型里的每一个数字都是确定性的乘加结果。任何试图"让模型估一下"
    的做法都会破坏它的可信度 —— 而这份预算的用途是**拿去找装修公司砍价**，
    算错一个数比崩溃更糟（ADR-07 的立论就在于此）。
    """

    grade: BudgetGrade
    area: float = Field(description="参与计算的套内面积（㎡）")
    region_coefficient: float = Field(description="地区调整系数，本表按内江标定为 1.0")

    lines: list[BudgetLine] = Field(description="各分项明细，AC-05 要求 ≥7 项")
    subtotal_min: float = Field(description="施工费小计下限（不含按比例计取的管理费/设计费）")
    subtotal_max: float = Field(description="施工费小计上限")

    total_min: float = Field(description="总价下限")
    total_max: float = Field(description="总价上限")
    price_per_sqm_min: float = Field(description="折合单价下限（元/㎡），用于与档位区间比对")
    price_per_sqm_max: float = Field(description="折合单价上限")

    computed_by: str = Field(
        description="计算来源标识，写入 DB 的 budget_computed_by 字段以便追溯（ADR-07）"
    )
    data_source: str = Field(description="价格表来源标识")
    disclaimer: str = Field(
        description="演示数据声明。**必须原样透出到前端**——用户有权知道这不是真实报价"
    )


class BudgetNarrative(BaseModel):
    """
    预算的**文字部分** —— 这是本模块唯一交给 LLM 的东西。

    ⚠️ **本模型刻意不含任何金额字段**（没有 int/float/Decimal）。

    契约是：数字由引擎算好、以文本形式喂给模型；模型只负责把数字组织成人话、
    补充砍价话术与省钱建议。因为返回类型里没有放数字的位置，
    模型即便想在结构化字段里"顺手改个数"也无处可改。

    新增字段时请守住这条：**要加的是"说法"，不是"算法"。**
    """

    summary: str = Field(
        default="",
        description="给业主看的整体说明：这笔钱大概花在哪、为什么这个档位是这个价位。2-4 句。",
    )
    grade_rationale: str = Field(
        default="",
        description="解释本档位的定位与取舍，例如经济档为什么把钱省在了定制柜上",
    )
    cost_drivers: list[str] = Field(
        default_factory=list,
        description="本档位的主要成本驱动项，2-4 条。**须引用给定的分项名称**，不要自己发明分项",
    )
    negotiation_tips: list[str] = Field(
        default_factory=list,
        description=(
            "砍价话术，3-5 条。要具体、能直接用，"
            "例如「水电按实测结算，要求合同写明单价上限」而不是「要努力砍价」"
        ),
    )
    saving_tips: list[str] = Field(
        default_factory=list,
        description="省钱建议，2-4 条。**只提做法，不要承诺能省多少钱**",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="本档位需要留意的风险，如增项高发项、容易被漏报的项目",
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="对本次文字解读的把握程度。**不要因为数字精确就给高分**——数字不是你的功劳",
    )


__all__ = [
    "BudgetLine", "BudgetBreakdown", "BudgetNarrative",
    "PriceBasis", "BudgetGrade",
]
