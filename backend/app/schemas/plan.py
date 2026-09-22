"""
方案相关 Pydantic Schema（A-03 空间规划，以及后续 A-04~A-07 的公共契约）。

注意：Schema 中每个字段的 description 会被 `llm_client.build_schema_instruction()`
**原样拼进 Prompt**，所以描述是写给模型看的，不是写给开发者看的。

═══════════════════════════════════════════════════════════════════
本文件的核心约束：**方案只能安排户型里真实存在的房间**
═══════════════════════════════════════════════════════════════════

装修方案生成是最容易"编"的环节。模型的训练数据里有海量"三室两厅"的样板方案，
它会非常自然地写出一份漂亮但**与本户型无关**的规划：给一个只有两间房的户型
安排"儿童房"，或者把根本不存在的"玄关"纳入收纳设计。

这与 A-02 的"编造评分"是同一类错误，但危害更直接——用户会照着这份方案去
和装修公司谈，谈一个不存在的房间。

因此本模块采取与 A-02 相同的策略：

  Schema 层：字段描述里明确要求"原样照抄户型数据中的房间名"
  代码层：  A-03 的 _postprocess 会**逐一比对** zones 与 layout.rooms，
           把对不上的房间**剔除**并记入 `invented_rooms`

**关键判定不依赖模型的自觉**——它照做是好事，不照做也拦得住。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 方案风格。与 API 契约 4.3 的 styles 取值对应。
PlanStyle = Literal["modern", "nordic", "chinese", "cream", "japandi", "industrial"]
#: 预算档。数字区间定义在各档的规则表里（M3 的预算 Agent），此处只做标识。
BudgetGrade = Literal["economy", "medium", "high"]
#: 收纳实现方式。经济档应偏向成品柜，高端档才用全屋定制。
StorageKind = Literal["custom_cabinet", "ready_made", "built_in", "none"]


class ZonePlan(BaseModel):
    """一个房间的功能安排。"""

    room_name: str = Field(
        description=(
            "**必须原样照抄户型数据中已存在的房间名**，不得新造房间。"
            "例如户型数据里叫「次卧」，就写「次卧」，不要写成「儿童房」——"
            "功能要写在 function 字段里，不是改房间名"
        )
    )
    function: str = Field(
        description="该房间在本方案中承担的功能，如「主卧」「儿童房」「开放式书房」"
    )
    rationale: str = Field(
        description="为什么这样安排，须结合该房间的面积与朝向说明，一句话即可"
    )
    furniture: list[str] = Field(
        default_factory=list,
        description="该房间建议的主要家具，3-6 件，只写品类不写品牌与价格",
    )


class StoragePlan(BaseModel):
    """一处收纳设计。"""

    location: str = Field(
        description="收纳位置，优先用户型中的房间名；走廊、玄关等非独立房间的空间也可直接描述"
    )
    kind: StorageKind = Field(
        description=(
            "实现方式。custom_cabinet=全屋定制，ready_made=成品柜，"
            "built_in=嵌入式（需改造墙体，有承重风险时不得使用）"
        )
    )
    note: str = Field(default="", description="补充说明，如容量估算或注意事项")


class CirculationFix(BaseModel):
    """动线优化措施。"""

    problem: str = Field(
        description="要解决的动线问题，**应来自户型诊断中已指出的问题**，不要自行发明新问题"
    )
    solution: str = Field(description="具体的优化做法")
    target_rooms: list[str] = Field(
        default_factory=list,
        description="涉及哪些房间，须使用户型数据中的房间名",
    )


class SpacePlan(BaseModel):
    """
    A-03 空间规划的结构化输出。

    ⚠️ **本 Schema 不含任何价格、金额字段**，这是刻意的。

    项目最隐蔽的可行性地雷是"让 LLM 算钱"——它会产生看起来合理但算术错误的
    数字，而用户恰恰是拿预算去和装修公司砍价的（见需求文档 2.2.2 与 ADR-07）。
    预算由 A-04 的**规则引擎**计算，A-03 只负责"空间上怎么安排"。
    两个 Agent 的职责边界在这里用类型系统固化下来。
    """

    summary: str = Field(
        description="本方案的整体思路，2-3 句话，面向业主，说人话"
    )
    zones: list[ZonePlan] = Field(
        default_factory=list,
        description="逐房间的功能安排，**须覆盖户型数据中的每一个房间**，不要遗漏",
    )
    storage_plans: list[StoragePlan] = Field(
        default_factory=list,
        description="收纳设计，3-6 处；经济档以成品柜为主，高端档才考虑全屋定制",
    )
    circulation_fixes: list[CirculationFix] = Field(
        default_factory=list,
        description="动线优化，针对户型诊断中已指出的问题；没有问题则留空数组",
    )
    key_moves: list[str] = Field(
        default_factory=list,
        description=(
            "本方案区别于其他方案的 2-4 条核心改动。"
            "**必须体现风格与预算档的差异**，不要写成放之四海皆准的空话"
        ),
    )
    data_gaps: list[str] = Field(
        default_factory=list,
        description="规划中因数据不足而只能粗略处理的地方，如实列出",
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="本方案的置信度。户型数据越完整、需要推测的地方越少，分数越高",
    )


__all__ = [
    "ZonePlan", "StoragePlan", "CirculationFix", "SpacePlan",
    "PlanStyle", "BudgetGrade", "StorageKind",
]
