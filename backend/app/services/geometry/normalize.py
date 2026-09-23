"""
把 A-01 的像素级户型 JSON 归一化成**米制场景**。

═══════════════════════════════════════════════════════════════════
这件事为什么不能"顺手做掉"
═══════════════════════════════════════════════════════════════════
A-01 的输出是**图片像素坐标**（左上角原点、Y 轴向下）。要用它渲染，
必须先回答四个问题，而每个问题都有坑：

【1】一像素等于多少米？
    A-01 **不输出这个**。它输出的是 `total_area`（平方米）和一堆像素坐标。
    唯一可行的推导是：`px_per_m = sqrt(Σ房间bbox面积_px / total_area_m²)`。

    ⚠️ 这是个**近似**，而且误差来源明确：
      · bbox 是轴对齐矩形，L 形房间会被高估
      · 相邻房间的 bbox 会重叠（隔墙两侧各算一次）
      · 阳台/飘窗可能不计入 total_area 但有 bbox
    实测一次真实解析：由面积推 86.8 px/m，由尺寸标注推 83.5 / 90.2，
    三者彼此差 8% —— **图本身就不是严格按比例的**。
    所以这里不只算一个数，还**把分歧如实报出来**。

【2】Y 轴朝哪？
    图片 Y 向下，数学/3D 场景 Y 向上。不翻转的话，3D 里的户型是**镜像**的
    —— 而镜像的户型看上去完全正常，只是左右反了。这种错误没人会发现，
    除非拿原图逐间比对。

【3】原点在哪？
    实测那张家用户型图从 [40,40] 开始，不是 [0,0]。
    不归一化原点，两个渲染器的"居中"会各自得到不同的结果。

【4】墙是闭合的吗？
    这决定 3D 漫步会不会穿墙。外墙如果不闭合（模型漏识别了一段），
    挤出的墙体就有缺口 —— 必须**检测出来并降级**，而不是硬渲。

═══════════════════════════════════════════════════════════════════
输出的是"场景"，不是"渲染指令"
═══════════════════════════════════════════════════════════════════
`Scene` 只描述**几何事实与假设**，不含任何渲染参数（颜色、相机、材质）。
2D 与 3D 渲染器各自决定怎么画。这样加第三个渲染器（比如导出 DXF）
时不需要动这里。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# ── 默认假设 ────────────────────────────────────────────────────────

#: 层高。**户型图是二维的，没有这个信息** —— 这是假设，不是测量。
#: 中国城镇住宅常见净高 2.7–2.9m，取中。必须随场景一起返回，
#: 由渲染器标注给用户看。
DEFAULT_CEILING_HEIGHT_M = 2.8

#: 门洞默认宽度。实测 A-01 输出的 `width` 常常是 0（三扇门全是 0），
#: 所以需要一个兜底值。0.9m 是国内住宅内门的常见规格。
DEFAULT_DOOR_WIDTH_M = 0.9

#: 墙体厚度。用于 2D 描边与 3D 挤出。
DEFAULT_WALL_THICKNESS_M = 0.2

#: 墙端点"重合"的容差（米）。视觉模型识别的坐标有抖动，
#: 要求严格相等会把本该闭合的墙判成断开。
WELD_TOLERANCE_M = 0.12

#: 比例尺由两个来源推导，分歧超过这个比例就告警。
SCALE_DISAGREEMENT_WARN = 0.12


@dataclass(frozen=True)
class Vec2:
    """米制平面坐标。**原点在户型包围盒左下角，X 向右、Y 向上。**"""

    x: float
    y: float

    def as_list(self) -> list[float]:
        return [round(self.x, 4), round(self.y, 4)]


@dataclass
class WallSeg:
    """一段墙。"""

    points: list[Vec2]
    kind: str = "unknown"            # load_bearing / non_load_bearing / unknown
    thickness_m: float = DEFAULT_WALL_THICKNESS_M

    @property
    def is_loop(self) -> bool:
        """是不是一个闭合环（首尾点重合）。"""
        return len(self.points) >= 3 and _nearly(self.points[0], self.points[-1])

    @property
    def length_m(self) -> float:
        return sum(
            math.dist((a.x, a.y), (b.x, b.y))
            for a, b in zip(self.points, self.points[1:])
        )

    def segments(self) -> list[tuple[Vec2, Vec2]]:
        """拆成直线段。闭合环最后会回到起点。"""
        return list(zip(self.points, self.points[1:]))


@dataclass
class Opening:
    """门或窗。位置是米制中心点。"""

    kind: str                        # door / window
    center: Vec2
    width_m: float
    #: 落在哪段墙上（WallSeg 在 scene.walls 里的下标）。找不到时为 -1
    wall_index: int = -1
    #: 沿该墙从起点算起的距离（米）。用于在墙上开洞
    offset_along_wall_m: float = 0.0
    swing: str = "unknown"
    #: 宽度是不是用了兜底值（而不是识别出来的）
    width_is_assumed: bool = False


@dataclass
class RoomShape:
    """一个房间。"""

    name: str
    kind: str
    #: 米制多边形。v1 直接由 bbox 得到（矩形），**不是真实轮廓**
    polygon: list[Vec2]
    area_m2: float
    #: 面积是不是由多边形算出来的（与 A-01 给的 area 可能不一致）
    area_from_polygon_m2: float = 0.0
    polygon_is_bbox: bool = True

    @property
    def center(self) -> Vec2:
        """多边形包围盒的中心。**不是质心** —— v1 的多边形就是矩形，两者相同。"""
        if not self.polygon:
            return Vec2(0.0, 0.0)
        xs = [p.x for p in self.polygon]
        ys = [p.y for p in self.polygon]
        return Vec2((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


@dataclass
class Quality:
    """
    几何质量。**决定 3D 漫步能不能用真实墙体。**

    这不是"要不要警告用户"的问题，而是"渲染器该走哪条路"的问题：
    墙不闭合时挤出真实墙体必然漏光、能走出去，必须降级成房间盒体。
    """

    walls_closed: bool = False
    wall_count: int = 0
    room_count: int = 0
    #: 没被任何房间 bbox 覆盖的墙体比例（0–1）。偏高说明墙与房间对不上
    unmatched_wall_ratio: float = 0.0
    issues: list[str] = field(default_factory=list)

    @property
    def can_build_walls(self) -> bool:
        """能不能用真实墙体建模（而不是退化成房间盒体）。"""
        return self.walls_closed and self.wall_count > 0 and self.unmatched_wall_ratio < 0.5


@dataclass
class Scene:
    """归一化后的米制场景。**2D 与 3D 渲染器的唯一输入。**"""

    #: 像素 → 米。A-01 不输出它，是这里推出来的
    px_per_m: float
    #: 比例尺怎么来的，以及分歧有多大
    scale_source: str
    scale_notes: list[str] = field(default_factory=list)

    walls: list[WallSeg] = field(default_factory=list)
    openings: list[Opening] = field(default_factory=list)
    rooms: list[RoomShape] = field(default_factory=list)

    #: 户型米制包围盒（宽 × 深）
    width_m: float = 0.0
    depth_m: float = 0.0

    ceiling_height_m: float = DEFAULT_CEILING_HEIGHT_M
    #: 所有"这是我们假设的、不是测出来的"条目。渲染器必须展示。
    assumptions: list[str] = field(default_factory=list)

    quality: Quality = field(default_factory=Quality)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "units": "m",
            "px_per_m": round(self.px_per_m, 3),
            "scale_source": self.scale_source,
            "scale_notes": self.scale_notes,
            "width_m": round(self.width_m, 3),
            "depth_m": round(self.depth_m, 3),
            "ceiling_height_m": self.ceiling_height_m,
            "assumptions": self.assumptions,
            "walls": [
                {
                    "kind": w.kind,
                    "thickness_m": w.thickness_m,
                    "is_loop": w.is_loop,
                    "length_m": round(w.length_m, 3),
                    "points": [p.as_list() for p in w.points],
                }
                for w in self.walls
            ],
            "openings": [
                {
                    "kind": o.kind,
                    "center": o.center.as_list(),
                    "width_m": round(o.width_m, 3),
                    "wall_index": o.wall_index,
                    "offset_along_wall_m": round(o.offset_along_wall_m, 3),
                    "swing": o.swing,
                    "width_is_assumed": o.width_is_assumed,
                }
                for o in self.openings
            ],
            "rooms": [
                {
                    "name": r.name,
                    "kind": r.kind,
                    "area_m2": round(r.area_m2, 2),
                    "polygon": [p.as_list() for p in r.polygon],
                    "polygon_is_bbox": r.polygon_is_bbox,
                }
                for r in self.rooms
            ],
            "quality": {
                "walls_closed": self.quality.walls_closed,
                "wall_count": self.quality.wall_count,
                "room_count": self.quality.room_count,
                "unmatched_wall_ratio": round(self.quality.unmatched_wall_ratio, 3),
                "can_build_walls": self.quality.can_build_walls,
                "issues": self.quality.issues,
            },
            "confidence": self.confidence,
        }


# ── 小工具 ──────────────────────────────────────────────────────────


def _nearly(a: Vec2, b: Vec2, tol: float = 1e-6) -> bool:
    return abs(a.x - b.x) < tol and abs(a.y - b.y) < tol


def _polygon_area(pts: Sequence[Vec2]) -> float:
    """鞋带公式。返回绝对值，不关心顶点方向。"""
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for a, b in zip(pts, pts[1:] + pts[:1]):
        s += a.x * b.y - b.x * a.y
    return abs(s) / 2


def _point_seg_distance(p: Vec2, a: Vec2, b: Vec2) -> tuple[float, float]:
    """
    点到线段的距离，以及投影参数 t（0–1）。

    返回 t 是为了算"门窗在墙上的位置" —— 只知道最近还不够，
    还要知道它落在墙的哪一段上。
    """
    dx, dy = b.x - a.x, b.y - a.y
    if dx == 0 and dy == 0:
        return math.dist((p.x, p.y), (a.x, a.y)), 0.0
    t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    proj = Vec2(a.x + t * dx, a.y + t * dy)
    return math.dist((p.x, p.y), (proj.x, proj.y)), t


# ── 比例尺 ──────────────────────────────────────────────────────────


@dataclass
class ScaleEstimate:
    px_per_m: float
    source: str
    notes: list[str] = field(default_factory=list)


def derive_scale(layout: dict[str, Any]) -> ScaleEstimate:
    """
    推导 像素 → 米 的比例尺。

    ══════════════════════════════════════════════════════════════════
    为什么是 sqrt(面积比) 而不是别的
    ══════════════════════════════════════════════════════════════════
    像素面积 与 平方米 之间差一个**平方**的因子：

        area_m2 = area_px / px_per_m²
        ⇒ px_per_m = sqrt(area_px / area_m2)

    两个来源互相校验：

      · **主推导**：所有房间 bbox 的像素面积之和 ÷ `total_area`
      · **副校验**：尺寸标注（`dimensions` 里 value 的单位是米）。
        但标注**没有和像素位置绑定** —— A-01 只给了文字的数值，没说它量的是
        哪一段。所以只能用它做量级校验（比如"标注里的最大值不该超过户型
        像素宽度的换算值太多"），不能做精确标定。

    两者分歧超过 `SCALE_DISAGREEMENT_WARN` 时**如实报出来**，不假装一致。
    实测一次真实解析：由面积推 86.8，由标注的两个方向分别推 83.5 / 90.2
    —— 差 8%。那不是代码错，是**图本身不严格按比例**。
    """
    notes: list[str] = []

    boxes = [
        b for b in ((r.get("bbox") or []) for r in layout.get("rooms") or [])
        if len(b) == 4
    ]
    total_area = float(layout.get("total_area") or 0)

    if not boxes:
        notes.append("没有房间 bbox，无法由面积推导比例尺，退化为 1 px = 1 cm")
        return ScaleEstimate(px_per_m=100.0, source="fallback_default", notes=notes)

    sum_px = 0.0
    for x1, y1, x2, y2 in boxes:
        sum_px += abs(x2 - x1) * abs(y2 - y1)

    if sum_px <= 0:
        notes.append("房间 bbox 面积为 0，退化为 1 px = 1 cm")
        return ScaleEstimate(px_per_m=100.0, source="fallback_default", notes=notes)

    if total_area <= 0:
        # 没有总面积 —— 用尺寸标注兜底，再不行就用一个经验值
        dims = [float(d.get("value") or 0) for d in layout.get("dimensions") or []]
        dims = [d for d in dims if d > 0]
        if dims and len(boxes) >= 1:
            span_px = max(
                max(b[2] for b in boxes) - min(b[0] for b in boxes),
                max(b[3] for b in boxes) - min(b[1] for b in boxes),
            )
            px_per_m = span_px / max(dims)
            notes.append(
                f"缺少 total_area，改用尺寸标注中的最大值 {max(dims)}m 推导 —— "
                f"该标注未必对应户型最长边，误差可能很大"
            )
            return ScaleEstimate(px_per_m=px_per_m, source="from_dimension", notes=notes)

        notes.append("既无 total_area 也无尺寸标注，退化为 1 px = 1 cm")
        return ScaleEstimate(px_per_m=100.0, source="fallback_default", notes=notes)

    px_per_m = math.sqrt(sum_px / total_area)
    source = "derived_from_area"

    # ── 与尺寸标注交叉校验（只报分歧，不覆盖主推导）──
    dims = [float(d.get("value") or 0) for d in layout.get("dimensions") or []]
    dims = sorted((d for d in dims if d > 0), reverse=True)
    if dims:
        span_px = max(
            max(b[2] for b in boxes) - min(b[0] for b in boxes),
            max(b[3] for b in boxes) - min(b[1] for b in boxes),
        )
        implied = span_px / dims[0]
        gap = abs(implied - px_per_m) / px_per_m
        notes.append(
            f"尺寸标注交叉校验：标注最大值 {dims[0]:.2f}m 对应 {implied:.1f} px/m，"
            f"与面积推导的 {px_per_m:.1f} px/m 相差 {gap:.0%}"
        )
        if gap > SCALE_DISAGREEMENT_WARN:
            notes.append(
                "⚠️ 两个来源分歧偏大，说明**原图不严格按比例绘制**或 bbox 有偏差。"
                "3D 漫游里的尺寸与实际会有出入"
            )

    notes.append(
        "该比例尺由 bbox 面积推导，是**近似值**：bbox 是轴对齐矩形，"
        "L 形房间会被高估、相邻房间的 bbox 会重叠"
    )
    return ScaleEstimate(px_per_m=px_per_m, source=source, notes=notes)


# ── 主流程 ──────────────────────────────────────────────────────────


def normalize_layout(
    layout: dict[str, Any],
    *,
    ceiling_height_m: float = DEFAULT_CEILING_HEIGHT_M,
) -> Scene:
    """
    像素级户型 JSON → 米制场景。

    坐标约定（**Y 轴向上**，原点在包围盒左下角）：

        图片坐标系（Y 向下）          场景坐标系（Y 向上）
        (0,0) ┌──────────► x        (0,H) ┌──────────► x
              │                            │
              │                            │
              ▼ y                     (0,0)└──────────►
    """
    scale = derive_scale(layout)
    k = 1.0 / scale.px_per_m

    boxes = [
        b for b in ((r.get("bbox") or []) for r in layout.get("rooms") or [])
        if len(b) == 4
    ]

    # ── 包围盒：决定平移量（原点归位）与翻转基准 ──
    if boxes:
        min_x = min(b[0] for b in boxes)
        min_y = min(b[1] for b in boxes)
        max_x = max(b[2] for b in boxes)
        max_y = max(b[3] for b in boxes)
    else:
        # 没有房间 bbox 时，用墙的坐标兜底
        pts = [
            p for w in layout.get("walls") or []
            for p in (w.get("coords") or []) if len(p) == 2
        ]
        if pts:
            min_x = min(p[0] for p in pts)
            min_y = min(p[1] for p in pts)
            max_x = max(p[0] for p in pts)
            max_y = max(p[1] for p in pts)
        else:
            min_x = min_y = 0.0
            max_x = max_y = 1.0

    def to_scene(px: float, py: float) -> Vec2:
        """像素 → 米。先平移使原点归位，再翻转 Y。"""
        return Vec2((px - min_x) * k, (max_y - py) * k)

    issues: list[str] = []

    # ── 房间 ──
    rooms: list[RoomShape] = []
    for r in layout.get("rooms") or []:
        b = r.get("bbox") or []
        if len(b) != 4:
            continue
        p1, p2 = to_scene(b[0], b[1]), to_scene(b[2], b[3])
        poly = [
            Vec2(p1.x, p2.y), Vec2(p2.x, p2.y), Vec2(p2.x, p1.y), Vec2(p1.x, p1.y),
        ]
        poly_area = _polygon_area(poly)
        declared = float(r.get("area") or 0)
        rooms.append(
            RoomShape(
                name=str(r.get("name") or "未命名"),
                kind=str(r.get("type") or "other"),
                polygon=poly,
                # 优先用 bbox 算出来的面积 —— 它和 polygon 是**同一份数据**，
                # 而 A-01 的 area 是模型读图估的。两者不一致时以几何为准，
                # 但在下面记一条 issue。
                area_m2=poly_area,
                area_from_polygon_m2=poly_area,
                polygon_is_bbox=True,
            )
        )
        if declared and poly_area and abs(declared - poly_area) / max(poly_area, 1e-6) > 0.35:
            issues.append(
                f"房间「{r.get('name')}」：模型给的面积 {declared:.1f}㎡ 与 "
                f"bbox 算出的 {poly_area:.1f}㎡ 相差过大，已以几何为准"
            )

    # ── 墙 ──
    walls: list[WallSeg] = []
    for w in layout.get("walls") or []:
        pts = [p for p in (w.get("coords") or []) if len(p) == 2]
        if len(pts) < 2:
            continue
        scene_pts = [to_scene(p[0], p[1]) for p in pts]

        # 焊接：把抖动造成的"几乎重合"的相邻点合并
        welded: list[Vec2] = []
        for p in scene_pts:
            if welded and math.dist((p.x, p.y), (welded[-1].x, welded[-1].y)) < WELD_TOLERANCE_M:
                continue
            welded.append(p)

        # ⚠️ **首尾也要焊一次。**
        #
        # 上面的循环只比较**相邻**点，闭合环的末点与首点不是相邻的，
        # 于是差几厘米也不会被合并 —— 而 `is_loop` 要求首尾几乎相等，
        # 结果一个肉眼看上去闭合的环被判成开放折线（实测差 1.6cm 就中招）。
        #
        # 这里直接把末点**吸附**到首点，让环真正闭上。这也修了几何本身：
        # 3D 挤出时首尾不重合的环会有个缺口。
        if len(welded) >= 3 and math.dist(
            (welded[0].x, welded[0].y), (welded[-1].x, welded[-1].y)
        ) < WELD_TOLERANCE_M:
            welded[-1] = welded[0]

        if len(welded) >= 2:
            walls.append(
                WallSeg(points=welded, kind=str(w.get("type") or "unknown"))
            )

    # ── 墙体闭合性检查（决定 3D 能不能用真实墙体）──
    walls_closed = _check_walls_closed(walls, rooms)
    if not walls_closed:
        # ⚠️ 没有墙时**也要**上报。
        # 第一版写成 `if walls and not walls_closed`，于是"一堵墙都没识别出来"
        # 这种最需要降级的情况反而没有任何提示 —— 渲染器拿不到信号，
        # 会以为自己可以按真实墙体建模。
        if not walls:
            issues.append(
                "未识别出任何墙体。3D 只能按房间 bbox 建盒体，"
                "渲染器请走简化模式"
            )
        else:
            issues.append(
                "外墙未闭合：端点之间存在缺口。3D 漫游若按真实墙体建模会漏光、"
                "可以走出去，应降级为房间盒体模式"
            )

    # ── 门窗 ──
    openings: list[Opening] = []
    for kind, key in (("door", "doors"), ("window", "windows")):
        for o in layout.get(key) or []:
            pos = o.get("position") or []
            if len(pos) != 2:
                continue
            center = to_scene(pos[0], pos[1])
            raw_w = float(o.get("width") or 0)
            assumed = raw_w <= 0
            width = raw_w if raw_w > 0 else (
                DEFAULT_DOOR_WIDTH_M if kind == "door" else 1.2
            )
            idx, offset = _locate_on_wall(center, walls)
            openings.append(
                Opening(
                    kind=kind,
                    center=center,
                    width_m=width,
                    wall_index=idx,
                    offset_along_wall_m=offset,
                    swing=str(o.get("swing") or "unknown"),
                    width_is_assumed=assumed,
                )
            )

    assumed_doors = [o for o in openings if o.kind == "door" and o.width_is_assumed]
    if assumed_doors:
        issues.append(
            f"{len(assumed_doors)} 扇门未识别出宽度，已按 "
            f"{DEFAULT_DOOR_WIDTH_M}m 兜底"
        )

    # ── 墙与房间的吻合度 ──
    unmatched = _unmatched_wall_ratio(walls, rooms)

    # ── 组装 ──
    assumptions: list[str] = []
    if not boxes:
        assumptions.append("没有房间 bbox，尺寸完全来自墙体坐标，精度较低")
    assumptions.append(
        f"层高 {ceiling_height_m}m 是**假设值** —— 户型图是二维的，"
        f"不含层高信息"
    )
    if any(o.width_is_assumed for o in openings):
        assumptions.append(f"部分门窗宽度按标准值兜底（门 {DEFAULT_DOOR_WIDTH_M}m）")
    assumptions.append("房间轮廓目前用 bbox 矩形近似，不是真实墙面轮廓")

    scene = Scene(
        px_per_m=scale.px_per_m,
        scale_source=scale.source,
        scale_notes=scale.notes,
        walls=walls,
        openings=openings,
        rooms=rooms,
        width_m=(max_x - min_x) * k,
        depth_m=(max_y - min_y) * k,
        ceiling_height_m=ceiling_height_m,
        assumptions=assumptions,
        quality=Quality(
            walls_closed=walls_closed,
            wall_count=len(walls),
            room_count=len(rooms),
            unmatched_wall_ratio=unmatched,
            issues=issues,
        ),
        confidence=float(layout.get("confidence") or 0.0),
    )
    return scene


#: 判定"墙围不围得住"的网格步长（米）。0.15m 下 12×9m 的户型约 80×60 格，
#: 对十几段墙做距离判定约五万次，纯 Python 几十毫秒。
ENCLOSURE_STEP_M = 0.15
#: 网格比户型外扩多少（米）—— 洪水填充要从"确定在外面"的地方起步。
ENCLOSURE_MARGIN_M = 1.5


def _check_walls_closed(
    walls: Sequence[WallSeg],
    rooms: Sequence[RoomShape] | None = None,
) -> bool:
    """
    这些墙**围不围得住**？

    ══════════════════════════════════════════════════════════════════
    ⚠️ 这个函数曾经判错了整整一类输入（2026-09-23 实测发现）
    ══════════════════════════════════════════════════════════════════
    初版的判据是「**存在一个闭合环**」—— 要求某一段墙**自己**是个首尾重合的折线。
    它在一份真实解析结果上是对的（那次模型确实给了一个 5 点闭环），
    于是被当成了通用规则。

    后来用自己生成的户型图实测，模型给出的是这样四段：

        [0] (95,140)   -> (1520,140)     顶
        [1] (95,1190)  -> (1520,1190)    底
        [2] (95,140)   -> (95,1190)      左
        [3] (1520,140) -> (1520,1190)    右

    **四条边严丝合缝地围成了一个矩形** —— 但它们是**四段独立的墙**，
    没有任何一段自己是环。于是判据给出 False：可建墙 = False。

    后果不是报错，而是**静默降级**：3D 漫游退回"自由视角（可穿墙飞）"，
    第一人称行走这个功能**永远起不来** —— 而界面上一切正常。

    ══════════════════════════════════════════════════════════════════
    改成问对的问题：**从这里出得去吗**
    ══════════════════════════════════════════════════════════════════
    真正要保证的事情只有一件：**人不能走出户型**。
    那就直接测这件事 —— 从户型外面做洪水填充，看能不能渗进任何一间房。

    这个判据与"模型怎么表示墙"无关：环也好、碎段也好，只要围得住就通过。
    它也不会被"远处有一截不相干的墙"干扰（那个问题归 `unmatched_wall_ratio` 管）。
    """
    if not walls:
        return False

    rooms = list(rooms or ())
    if not rooms:
        # 没有房间就无从判断"里面"在哪 —— 保守判 False，让调用方降级
        return False

    # ── 快速路径：有环，且环包住了所有房间中心 ──
    # 模型有时确实给闭环；这种情况不必跑网格。
    for w in walls:
        if w.is_loop and len(w.points) >= 4:
            if all(_in_polygon(r.center, w.points) for r in rooms):
                return True

    return not _can_escape(rooms, walls)


def _can_escape(rooms: Sequence[RoomShape], walls: Sequence[WallSeg]) -> bool:
    """
    从户型外面洪水填充，能不能渗到任何一间房的中心。

    墙按**带宽度的实体**处理：离墙中心线不超过半个墙厚的格子算实心。
    所以比墙厚还窄的缝不算"通" —— 视觉模型给的点本来就有抖动，
    零容差会把正常的墙判成漏的。
    """
    import collections
    import math as _math

    half = 0.0
    for w in walls:
        half = max(half, w.thickness_m)
    solid = max(half / 2, 0.06)

    xs = [p.x for r in rooms for p in r.polygon]
    ys = [p.y for r in rooms for p in r.polygon]
    if not xs or not ys:
        return True
    x1, x2 = min(xs) - ENCLOSURE_MARGIN_M, max(xs) + ENCLOSURE_MARGIN_M
    y1, y2 = min(ys) - ENCLOSURE_MARGIN_M, max(ys) + ENCLOSURE_MARGIN_M

    segs = [(a.x, a.y, b.x, b.y) for w in walls for a, b in w.segments()]

    step = ENCLOSURE_STEP_M
    nx = int((x2 - x1) / step) + 1
    ny = int((y2 - y1) / step) + 1
    if nx * ny > 400_000:                      # 护栏：超大户型自动降精度
        step = _math.sqrt((x2 - x1) * (y2 - y1) / 400_000)
        nx = int((x2 - x1) / step) + 1
        ny = int((y2 - y1) / step) + 1

    def free(i: int, j: int) -> bool:
        px = x1 + i * step
        py = y1 + j * step
        for ax, ay, bx, by in segs:
            dx, dy = bx - ax, by - ay
            if dx == 0 and dy == 0:
                d = _math.hypot(px - ax, py - ay)
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                d = _math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d <= solid:
                return False
        return True

    # 从四周边界上所有"可通行"的格子起步 —— 那里一定在户型之外
    seen = [[False] * ny for _ in range(nx)]
    q: collections.deque[tuple[int, int]] = collections.deque()
    for i in range(nx):
        for j in (0, ny - 1):
            if free(i, j) and not seen[i][j]:
                seen[i][j] = True
                q.append((i, j))
    for j in range(ny):
        for i in (0, nx - 1):
            if free(i, j) and not seen[i][j]:
                seen[i][j] = True
                q.append((i, j))

    targets = {
        (int((r.center.x - x1) / step), int((r.center.y - y1) / step))
        for r in rooms
    }

    while q:
        i, j = q.popleft()
        if (i, j) in targets:
            return True                        # 渗进来了 → 围不住
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            a, b = i + di, j + dj
            if 0 <= a < nx and 0 <= b < ny and not seen[a][b] and free(a, b):
                seen[a][b] = True
                q.append((a, b))
    return False


def _in_polygon(p: Vec2, poly: Sequence[Vec2]) -> bool:
    """射线法。多边形的**真实**内外判定（不只是包围盒）。"""
    inside = False
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        if (a.y > p.y) != (b.y > p.y):
            xin = (b.x - a.x) * (p.y - a.y) / (b.y - a.y) + a.x
            if p.x < xin:
                inside = not inside
    return inside


def wall_point_at(wall: WallSeg, offset_m: float) -> tuple[Vec2, Vec2] | None:
    """
    墙上的第 `offset_m` 米处：返回 `(点, 单位方向向量)`。

    ⚠️ 方向由**这一段**决定，不是整段墙的首尾 —— 折线墙拐弯后方向会变，
    用整段的首尾方向会把门画歪（2D）或把碰撞体切歪（3D）。

    越界（offset 超过墙长）时钳到末端；墙退化（长度全为 0）时返回 `None`。

    放在 geometry 而不是 render：2D 画门要用它、3D 切门洞也要用它。
    留在渲染层会逼着几何层反向依赖渲染层，那是错的层次关系。
    """
    travelled = 0.0
    for a, b in wall.segments():
        seg_len = math.dist((a.x, a.y), (b.x, b.y))
        if seg_len <= 1e-9:
            continue
        if travelled + seg_len >= offset_m:
            t = (offset_m - travelled) / seg_len
            return (
                Vec2(a.x + t * (b.x - a.x), a.y + t * (b.y - a.y)),
                Vec2((b.x - a.x) / seg_len, (b.y - a.y) / seg_len),
            )
        travelled += seg_len
    return None


def _locate_on_wall(center: Vec2, walls: Sequence[WallSeg]) -> tuple[int, float]:
    """
    找出门窗落在哪段墙的哪个位置。

    返回 `(墙下标, 沿墙距离)`；找不到（离所有墙都远）时返回 `(-1, 0)`。

    这个映射是**热区的几何依据** —— 3D 里要在墙上开洞，2D 里要把热点
    画在门的位置上，两处用的是同一个结果。
    """
    best: tuple[int, float, float] = (-1, 0.0, float("inf"))
    for wi, w in enumerate(walls):
        travelled = 0.0
        for a, b in w.segments():
            dist, t = _point_seg_distance(center, a, b)
            if dist < best[2]:
                seg_len = math.dist((a.x, a.y), (b.x, b.y))
                best = (wi, travelled + t * seg_len, dist)
            travelled += math.dist((a.x, a.y), (b.x, b.y))
    # 离墙太远（超过 1m）就不认这个映射 —— 那多半是识别偏了
    if best[2] > 1.0:
        return -1, 0.0
    return best[0], best[1]


def _unmatched_wall_ratio(
    walls: Sequence[WallSeg], rooms: Sequence[RoomShape]
) -> float:
    """
    有多少比例的墙**不在任何房间的 bbox 内**。

    这个数字偏高说明墙与房间对不上 —— 通常是模型把墙识别到了户型外面，
    或者房间 bbox 严重偏小。两种情况下按墙建模都会得到奇怪的结果。
    """
    if not walls or not rooms:
        return 0.0

    total = 0.0
    outside = 0.0
    for w in walls:
        for a, b in w.segments():
            seg_len = math.dist((a.x, a.y), (b.x, b.y))
            total += seg_len
            mid = Vec2((a.x + b.x) / 2, (a.y + b.y) / 2)
            if not any(_inside_room_extent(mid, r, margin=0.3) for r in rooms):
                outside += seg_len
    return outside / total if total else 0.0


def _inside_room_extent(p: Vec2, room: RoomShape, margin: float = 0.0) -> bool:
    """
    点是否落在房间的**范围**内。

    ⚠️ 名字刻意不叫 `_in_polygon` —— v1 的房间 `polygon` 是 bbox 矩形，
    所以这里做的是包围盒判定，**不是真正的多边形内判定**。
    叫 `_in_polygon` 会让人以为凹多边形也支持，而它不支持。
    等房间轮廓改成真实墙面轮廓（M5 后半），这里要换成射线法。
    """
    if not room.polygon:
        return False
    xs = [v.x for v in room.polygon]
    ys = [v.y for v in room.polygon]
    return (
        min(xs) - margin <= p.x <= max(xs) + margin
        and min(ys) - margin <= p.y <= max(ys) + margin
    )
