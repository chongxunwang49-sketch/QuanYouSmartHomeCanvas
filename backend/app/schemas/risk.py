"""
避坑审查相关 Schema（A-06）。

═══════════════════════════════════════════════════════════════════
本 Schema 最核心的一条：**引用必须能对上知识库里的真实 chunk**
═══════════════════════════════════════════════════════════════════

AC-06 要求「识别 ≥ 5 类风险，**引用知识库来源**」。

如果让模型自由写来源（"参考行业规范"），那条引用是**不可验证的** ——
面试官问"这个规则出自哪里"就答不上来，更糟的是模型可能编一个像模像样的
来源，而用户会以为它查证过。

所以引用走**指认**而非**生成**（与 A-05 选材、A-03 房间对齐同一手法）：

    提示词把检索到的 chunk 编号成 [S1] [S2] … [Sn] 交给模型，
    模型只能回 `source_ids: ["S1", "S3"]`，
    代码再映射回真实的 `citation`（含文件路径与章节）。

模型编一个不存在的编号 → 被剔除并记入 `invented_citations`。
这样每一条风险都能追溯到具体的语料文件与章节，**或者明确地没有来源**。

═══════════════════════════════════════════════════════════════════
为什么 risk_type 用枚举
═══════════════════════════════════════════════════════════════════
AC-06 的门槛是「≥5 **类**风险」——"类"必须可数，否则"识别了 10 条风险"
可能全是同一个类型的重复。用 Literal 把类型固定下来，
验收时才能直接统计 `len({f.risk_type for f in findings})`。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 风险类型。AC-06 要求 ≥5 类 —— 用 Literal 固定后这个数字才可验证。
RiskType = Literal[
    "增项风险",       # 后期以各种名目加钱
    "漏项",           # 该报没报，后期必然发生
    "单价异常",       # 单价明显偏离市场区间
    "计价陷阱",       # 计费基数/方式有问题（如管理费按含主材总价计）
    "模糊计量",       # 规格/遍数/高度等约定不清，留出增项空间
    "环保风险",       # 材料环保等级不足
    "合同条款风险",   # 定金/付款/违约等条款问题
    "营销话术",       # 折扣、限时等制造决策压力的手段
]

Severity = Literal["high", "medium", "low"]


class RiskFinding(BaseModel):
    """
    一条风险发现。

    ⚠️ **不要给本模型加"金额"字段。** 报价单里的价格是用户给的输入，
    模型要引用就直接引原文，不需要也不应该自己算钱（ADR-07 的同一条纪律）。
    """

    risk_type: RiskType = Field(
        description="风险类型，**必须从给定枚举中选一个**，不要自创类型"
    )
    severity: Severity = Field(
        description="严重程度。high=可能显著超支或涉及法律风险；medium=常见但可控；low=提示性"
    )
    title: str = Field(description="一句话说清问题，20 字以内，例如「倒角只给单价未给总价」")
    where: str = Field(
        description="问题出在报价单的哪一处，**引用原文的序号或条款名**，如「3-2 瓷砖倒角」"
    )
    detail: str = Field(
        description="具体说明问题是什么、会造成什么后果。2-3 句，说人话，面向业主"
    )
    suggestion: str = Field(
        default="",
        description="给业主的应对建议，要具体到能直接用于谈判。1-2 句"
    )
    source_ids: list[str] = Field(
        default_factory=list,
        description=(
            "本条风险所依据的知识库来源编号，取值形如 [\"S1\", \"S3\"]。"
            "**只能从给出的编号里选**，不得自己编造。"
            "若该条属于常识性判断、没有对应来源，就留空数组 —— "
            "留空是允许的，编造编号会被系统剔除"
        ),
    )


class RiskReview(BaseModel):
    """
    一次避坑审查的结果（**模型产出部分**）。

    注意本模型没有 `citation` 字段 —— 真实出处由代码按 `source_ids` 回填。
    """

    summary: str = Field(
        default="",
        description=(
            "整体判断，2-3 句，面向业主。说明这份报价单/预算的整体风险水平，"
            "以及最该盯住的是哪一两项"
        ),
    )
    findings: list[RiskFinding] = Field(
        default_factory=list,
        description=(
            "逐条风险。**按 severity 从高到低排**。"
            "没有发现问题就留空数组，不要为了凑数编造风险 —— "
            "一份干净的报价单被判出问题，比漏判更伤信任"
        ),
    )
    overall_risk: Severity = Field(
        default="low",
        description="整体风险等级。high=不建议直接签；medium=需逐条核实后再签；low=可签但注意细节",
    )
    negotiation_points: list[str] = Field(
        default_factory=list,
        description=(
            "谈判要点，2-4 条。**应当是 findings 的提炼**，"
            "是业主可以直接拿去和装修公司谈的话，不要引入新的问题"
        ),
    )
    data_gaps: list[str] = Field(
        default_factory=list,
        description="本次审查中信息不足的地方，如实列出（例如「报价单未附施工图纸，无法核对工程量」）",
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="对本次审查的把握程度。报价单信息越少、需要推测的地方越多，分数应越低",
    )


__all__ = ["RiskType", "Severity", "RiskFinding", "RiskReview"]
