"""
户型相关 Pydantic Schema（A-01 解析 / A-02 诊断）。

注意：Schema 中每个字段的 description 不只是给开发者看的文档——
llm_client.build_schema_instruction() 会把它们**原样拼进 Prompt**，
因此描述要写得让模型一看就懂，这会直接决定解析准确率。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

RoomType = Literal[
    "living_room", "bedroom", "kitchen", "bathroom", "balcony",
    "study", "dining_room", "entrance", "storage", "other",
]
WallType = Literal["load_bearing", "non_load_bearing", "unknown"]
Orientation = Literal["north", "south", "east", "west", "northeast",
                      "northwest", "southeast", "southwest", "unknown"]


class Room(BaseModel):
    """一个房间。"""

    name: str = Field(description="房间中文名，如图中标注则用图中原文，否则按功能命名")
    type: RoomType = Field(description="房间功能类型")
    area: float = Field(default=0.0, description="面积，单位平方米；无法确定时填 0")
    bbox: list[int] = Field(
        default_factory=list,
        description="在图片中的像素边界框 [x1, y1, x2, y2]，左上角为原点；无法确定时填空数组",
    )
    orientation: Orientation = Field(default="unknown", description="该房间主要采光朝向")
    notes: str = Field(default="", description="该房间的特殊说明，如无则留空字符串")


class Wall(BaseModel):
    """一段墙体。"""

    type: WallType = Field(description="承重墙为 load_bearing，非承重为 non_load_bearing，无法判断为 unknown")
    coords: list[list[int]] = Field(
        default_factory=list,
        description="墙体折线坐标点列表，每点为 [x, y] 像素坐标",
    )
    note: str = Field(default="", description="补充说明，如疑似承重墙需人工复核")


class Door(BaseModel):
    """一扇门。"""

    position: list[int] = Field(default_factory=list, description="门中心点像素坐标 [x, y]")
    width: float = Field(default=0.0, description="门洞宽度，单位米；无法确定填 0")
    swing: Literal["inward", "outward", "sliding", "unknown"] = Field(
        default="unknown", description="开启方式：内开/外开/推拉/未知"
    )


class Window(BaseModel):
    """一扇窗。"""

    position: list[int] = Field(default_factory=list, description="窗中心点像素坐标 [x, y]")
    width: float = Field(default=0.0, description="窗洞宽度，单位米；无法确定填 0")
    orientation: Orientation = Field(default="unknown", description="窗户朝向")


class Dimension(BaseModel):
    """图中标注的一处尺寸。"""

    label: str = Field(description="图中标注的原始文字，如 '4200'")
    value: float = Field(description="换算后的数值，单位米")
    unit: str = Field(default="m", description="单位")


class DiagnosisItem(BaseModel):
    """单个诊断维度的结果。"""

    score: float = Field(ge=0.0, le=10.0, description="该维度评分，0 到 10 分")
    issues: list[str] = Field(default_factory=list, description="发现的问题列表，无问题则为空数组")
    suggestions: list[str] = Field(default_factory=list, description="改进建议列表")


class LayoutSchema(BaseModel):
    """A-01 户型图解析:结构化输出。这是全流程最关键的 Schema。"""

    rooms: list[Room] = Field(default_factory=list, description="识别出的所有房间")
    walls: list[Wall] = Field(default_factory=list, description="识别出的墙体")
    doors: list[Door] = Field(default_factory=list, description="识别出的门")
    windows: list[Window] = Field(default_factory=list, description="识别出的窗")
    dimensions: list[Dimension] = Field(default_factory=list, description="图中标注的尺寸")
    total_area: float = Field(default=0.0, description="套内总面积估算，单位平方米；无法确定填 0")
    entrance_orientation: Orientation = Field(
        default="unknown", description="入户门所在方位（依据指北针判断）"
    )
    has_north_arrow: bool = Field(default=False, description="图中是否存在指北针")
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="你对本次识别结果的置信度，0 到 1；模糊或凭猜测的内容越多，分数应越低",
    )
    uncertain_points: list[str] = Field(
        default_factory=list,
        description="你不确定的地方，必须如实列出，例如「左上角墙体粗细不清，无法判断是否承重」",
    )
    image_quality_note: str = Field(default="", description="对图像质量的说明，如模糊、倾斜、有水印等")

    @field_validator("rooms")
    @classmethod
    def _at_least_one_room(cls, v: list[Room]) -> list[Room]:
        # 不做强制校验（避免模型偶尔漏识别时直接抛错），改由 Agent 层根据
        # confidence 和 uncertain_points 决定是否告警。此处仅保留钩子。
        return v


class DegradedRoom(BaseModel):
    """
    降级模式下的房间 —— 只有名字。

    为什么单独定义一个这么贫瘠的模型：实测本地兜底模型 minicpm-v4.6
    能读对房间名，但**产不出可信的结构信息**（会编造越界 bbox、编造不存在的墙体，
    且 JSON 语法本身常是坏的）。与其让它拿着完整 Schema 去编，
    不如把任务缩小到它能力范围内，剩下的字段一律留空。
    """

    name: str = Field(description="房间中文名或图中原文标注的文字")


class DegradedLayout(BaseModel):
    """
    本地兜底模型的最小输出契约。

    ⚠️ 该结果**只可用于"大致有哪些房间"的参考**，绝不可作为拆改或预算依据。
    所有结构字段（墙体/门窗/尺寸/面积）必须留空，不得由模型推断。
    """

    rooms: list[DegradedRoom] = Field(default_factory=list, description="从图中读到的房间名称列表")
    confidence: float = Field(
        default=0.3, ge=0.0, le=1.0,
        description="把握程度。本地模型识别户型图时通常偏低，如实填写，不要给高分",
    )
    uncertainty: str = Field(
        default="", description="说明哪些信息没能识别，例如「无法判断墙体与门窗」"
    )

    @field_validator("rooms", mode="before")
    @classmethod
    def _accept_flat_string_list(cls, v):
        """
        容错：小模型经常把对象数组扁平化成字符串数组。

        实测 minicpm-v4.6 会返回 rooms: ["Living room", "Bed"] 而不是
        [{"name": "Living room"}, ...]。这是**结构表达差异**而非内容错误，
        与其多花一轮重试把它们纠正过来，不如直接接受——反正降级模式下的
        房间本来就只有一个 name 字段。

        注意：这只做形状适配，不做内容编造。字符串里写了什么就是什么。
        """
        if isinstance(v, list):
            return [{"name": item} if isinstance(item, str) else item for item in v]
        return v


class LayoutDiagnosis(BaseModel):
    """A-02 户型诊断结构化输出。五个维度与 API 契约 4.2 一致。"""

    lighting: DiagnosisItem = Field(description="采光诊断")
    ventilation: DiagnosisItem = Field(description="通风诊断")
    circulation: DiagnosisItem = Field(description="动线诊断")
    space_utilization: DiagnosisItem = Field(description="空间利用率诊断")
    green_score: DiagnosisItem = Field(description="绿色环保诊断")

    overall_score: float = Field(default=0.0, ge=0.0, le=10.0, description="综合评分")
    summary: str = Field(default="", description="整体评价，2-4 句话")
    load_bearing_warning: list[str] = Field(
        default_factory=list,
        description="涉及承重墙的风险提示，必须提醒用户由专业人员现场复核",
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="诊断置信度")


__all__ = [
    "Room", "Wall", "Door", "Window", "Dimension", "DiagnosisItem",
    "LayoutSchema", "LayoutDiagnosis",
    "DegradedRoom", "DegradedLayout",
    "RoomType", "WallType", "Orientation",
]
