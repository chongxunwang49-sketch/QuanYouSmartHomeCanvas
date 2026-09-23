"""
物品热区 —— 从**几何**推导，不用目标检测。

═══════════════════════════════════════════════════════════════════
为什么不用 YOLO（需求文档 2.2.6 记录的那次方案重做）
═══════════════════════════════════════════════════════════════════
V1.0 的设计是"用 YOLOv8 检测生成图里的地板、墙纸、空调"。它不可行，
理由不是"效果差一点"，而是**类别根本不存在**：

    COCO 80 类里没有"地板"，没有"墙纸"，甚至没有"空调"。

（COCO 有 `couch`、`bed`，但没有地面材料。）要做开放词汇检测得换
GroundingDINO / OWL-ViT 一类模型 —— 又是 1–2GB 显存，而本机可用显存是 3.4GB。

V2.0 改成**坐标映射**：户型解析结果里本来就有每个房间的 bbox，
地板就是那个 bbox 往里缩一个墙厚，墙面就是沿 bbox 的一圈带，
门的中心点已经落在墙上（`Opening.wall_index` / `offset_along_wall_m`）。
**零检测模型、零显存、且比检测更准** —— 检测是在猜边界，这是量出来的。

═══════════════════════════════════════════════════════════════════
precision 字段：不许假装精确（AC-09 / AC-28）
═══════════════════════════════════════════════════════════════════
    exact        坐标来自几何，热区边界就是房间边界
    room_level   只有房间级近似（AI 图路径）
    none         不提供热区

本模块产出的**全部是 `exact`** —— 因为它量的是几何本身，不是生成图。
`room_level` / `none` 出现在 `geo_check.py` 处理 AI 图的路径上。

> 诚实性原则（需求文档 2.2.6）：`room_level` 的热区悬停时必须显示
> "本区域整体参考价"，而不是假装精确到某一件家具。
> **用户可以接受近似，不能接受被骗。**

═══════════════════════════════════════════════════════════════════
天花板热区为什么不在 2D 图上
═══════════════════════════════════════════════════════════════════
俯视图里，天花板投影下来的像素**和地板完全重合**。两个热区抢同一块像素，
悬停命中谁取决于绘制顺序 —— 用户会看到"同一个位置有时报地板价、
有时报吊顶价"，而没有任何办法区分。**这种不确定性比少一个热区更糟。**
天花板的材料量改在 3D 视角里呈现（那里地板和天花板在屏幕上不重合）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..geometry import Scene
from ..geometry.normalize import DEFAULT_WALL_THICKNESS_M, Opening, RoomShape, Vec2
from .projection import Projection

#: 品类映射。房间的"地面 / 墙面"落到材料目录的哪个 `category` 上。
FLOOR_CATEGORY = "floor"
WALL_CATEGORY = "paint"
DOOR_CATEGORY = "door"

#: 一个热区最多带几件商品。悬停卡片放不下更多，多了也没人看。
ITEMS_PER_HOTSPOT = 3

#: 墙面面积用的层高来源见 Scene.ceiling_height_m（**是假设值**）。
#: 未扣除门窗洞口 —— 这里如实标注，不假装算过。


@dataclass
class HotspotGeom:
    """
    热区的**几何**部分（不含价格）。

    与价格分开是为了让 `svg.py` 能只依赖几何 —— 渲染器不该知道材料目录的存在。
    """

    index: int
    label: str
    category: str
    #: 子路径列表。地面是 1 个外环；墙面是 [外环, 内环]（挖空成一个"回"字）
    rings: list[list[list[float]]]
    #: 外环的轴对齐包围盒 `[left, top, right, bottom]`（像素）
    bbox: list[float]
    precision: str
    source: str
    #: 计价面积/数量（㎡ 或 个）。None 表示这项不按面积算
    quantity: float | None = None
    unit: str = ""
    note: str = ""

    def svg_path(self) -> str:
        """
        热区的 SVG path。

        多环时用**一条 path 的两个子路径 + `fill-rule="evenodd"`**，
        而不是"一个外框 + 一个内框两个元素"。理由是命中测试：
        浏览器对 evenodd 路径的挖空区域**不算命中**，所以墙面的"回"字
        中间那块会让鼠标穿透到下面的地板热区。
        换成两个矩形叠着放，中间那块就会被上层截走，地板永远悬停不到。
        """
        parts = []
        for ring in self.rings:
            if len(ring) < 3:
                continue
            d = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in ring) + " Z"
            parts.append(d)
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "category": self.category,
            "bbox": [round(v, 2) for v in self.bbox],
            "polygon": [[round(x, 2), round(y, 2)] for x, y in self.rings[0]]
            if self.rings else [],
            "rings": [[[round(x, 2), round(y, 2)] for x, y in r] for r in self.rings],
            "precision": self.precision,
            "source": self.source,
            "quantity": round(self.quantity, 2) if self.quantity is not None else None,
            "unit": self.unit,
            "note": self.note,
        }


# ══════════════════════════════════════════════════════════════════
# 几何
# ══════════════════════════════════════════════════════════════════


def hotspot_geometry(scene: Scene, proj: Projection) -> list[HotspotGeom]:
    """
    场景 → 一堆**位置精确**的热区。

    绘制/命中优先级 = 列表顺序（后面的盖前面的）。刻意排成
    **地面 → 墙面 → 门**：面积越小、越"具体"的目标越靠后，
    这样贴着墙走鼠标时，最后命中门的概率高于命中整面墙。
    """
    half = _wall_half_thickness(scene)
    geoms: list[HotspotGeom] = []

    # ── 一、地面：每个房间一块 ──
    for room in scene.rooms:
        g = _floor_hotspot(len(geoms), room, proj, half)
        if g is not None:
            geoms.append(g)

    # ── 二、墙面：每个房间一圈（挖空中间，见 HotspotGeom.svg_path）──
    for room in scene.rooms:
        g = _wall_hotspot(len(geoms), room, proj, half, scene.ceiling_height_m)
        if g is not None:
            geoms.append(g)

    # ── 三、门：每扇门一块，只覆盖墙厚那一小段 ──
    for op in scene.openings:
        if op.kind != "door":
            continue
        g = _door_hotspot(len(geoms), op, scene, proj, half)
        if g is not None:
            geoms.append(g)

    return geoms


def _wall_half_thickness(scene: Scene) -> float:
    """
    墙厚的一半。取场景里最厚的墙 —— 用最厚的那个，
    热区宁可略小（留一点儿缝）也不要越到隔壁房间去。
    """
    if not scene.walls:
        return DEFAULT_WALL_THICKNESS_M / 2
    return max(w.thickness_m for w in scene.walls) / 2


def _rect_rings(room: RoomShape, proj: Projection, half: float
                ) -> tuple[list[list[float]], tuple[float, float, float, float]] | None:
    """房间的米制包围盒 → 外扩/内缩 `half` 的两个像素矩形环。"""
    if len(room.polygon) < 3:
        return None
    xs = [p.x for p in room.polygon]
    ys = [p.y for p in room.polygon]
    x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
    if x2 - x1 < 2 * half or y2 - y1 < 2 * half:
        return None
    return (x1, y1, x2, y2), (x1 + half, y1 + half, x2 - half, y2 - half)


def _px_ring(proj: Projection, x1: float, y1: float, x2: float, y2: float
             ) -> tuple[list[list[float]], list[float]]:
    """米制矩形 → (像素顶点环, 像素 bbox)。**和画图用的是同一个 Projection。**"""
    left, top, right, bottom = proj.rect_to_px(x1, y1, x2, y2)
    ring = [[left, top], [right, top], [right, bottom], [left, bottom]]
    return ring, [left, top, right, bottom]


def _floor_hotspot(index: int, room: RoomShape, proj: Projection, half: float
                   ) -> HotspotGeom | None:
    boxes = _rect_rings(room, proj, half)
    if boxes is None:
        return None
    outside, inside = boxes
    ring, bbox = _px_ring(proj, *inside)
    return HotspotGeom(
        index=index,
        label=f"{room.name}地面",
        category=FLOOR_CATEGORY,
        rings=[ring],
        bbox=bbox,
        precision="exact",
        source="vector_layer",
        quantity=round(room.area_m2, 2),
        unit="元/㎡",
        note="面积由房间几何算出，未扣除固定家具占位",
    )


def _wall_hotspot(index: int, room: RoomShape, proj: Projection, half: float,
                  ceiling_height_m: float) -> HotspotGeom | None:
    """
    墙面：房间四周的一圈墙带。

    面积 = 周长 × 层高。
    ⚠️ **层高是假设值**（户型图是二维的），而且**未扣除门窗洞口**。
    两条都写进 `note`，让 UI 有机会显示 —— 这是"诚实"落地的具体位置。
    """
    boxes = _rect_rings(room, proj, half)
    if boxes is None:
        return None
    (x1, y1, x2, y2), (ix1, iy1, ix2, iy2) = boxes

    outer_ring, bbox = _px_ring(proj, x1, y1, x2, y2)
    inner_ring, _ = _px_ring(proj, ix1, iy1, ix2, iy2)

    w, d = x2 - x1, y2 - y1
    perimeter = 2 * (w + d)
    area = perimeter * ceiling_height_m

    return HotspotGeom(
        index=index,
        label=f"{room.name}墙面",
        category=WALL_CATEGORY,
        rings=[outer_ring, inner_ring],
        bbox=bbox,
        precision="exact",
        source="vector_layer",
        quantity=round(area, 2),
        unit="元/㎡",
        note=(f"按周长 {perimeter:.1f}m × 层高 {ceiling_height_m:.1f}m 估算，"
              f"层高为假设值，且未扣除门窗洞口"),
    )


def _door_hotspot(index: int, op: Opening, scene: Scene, proj: Projection,
                  half: float) -> HotspotGeom | None:
    """
    一扇门 = 一块盖住门洞的窄条。

    门洞中心点已经由几何内核落到墙上了（`wall_index` / `offset_along_wall_m`），
    这里只需要按墙的方向铺开 `width_m`。未关联到墙的门**仍然给热区**，
    但位置上只画一个小方块（图上也是一个虚线圆），不假装知道它的朝向。
    """
    cx, cy = op.center.x, op.center.y
    w = op.width_m
    d = max(half * 2, 0.2)

    if 0 <= op.wall_index < len(scene.walls):
        from .svg import wall_point_at

        wall = scene.walls[op.wall_index]
        placed = wall_point_at(wall, op.offset_along_wall_m)
        if placed is not None:
            p, u = placed
            # 沿墙铺开 width，垂直墙铺开一个墙厚
            nx, ny = -u.y, u.x
            corners = [
                Vec2(p.x - u.x * w / 2 - nx * d / 2, p.y - u.y * w / 2 - ny * d / 2),
                Vec2(p.x + u.x * w / 2 - nx * d / 2, p.y + u.y * w / 2 - ny * d / 2),
                Vec2(p.x + u.x * w / 2 + nx * d / 2, p.y + u.y * w / 2 + ny * d / 2),
                Vec2(p.x - u.x * w / 2 + nx * d / 2, p.y - u.y * w / 2 + ny * d / 2),
            ]
            ring = [list(proj.to_px(c)) for c in corners]
        else:
            ring = _square(cx, cy, w, proj)
    else:
        ring = _square(cx, cy, w, proj)

    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    note = "门洞位置由几何定位" if op.wall_index >= 0 else "未关联到墙体，位置未经校验"
    if op.width_is_assumed:
        note += "；宽度未识别，按标准值 0.9m 计"

    return HotspotGeom(
        index=index,
        label="门",
        category=DOOR_CATEGORY,
        rings=[ring],
        bbox=[min(xs), min(ys), max(xs), max(ys)],
        precision="exact",
        source="vector_layer",
        quantity=1.0,
        unit="元/樘",
        note=note,
    )


def _square(cx: float, cy: float, size_m: float, proj: Projection
            ) -> list[list[float]]:
    h = max(size_m, 0.3) / 2
    corners = [
        Vec2(cx - h, cy - h), Vec2(cx + h, cy - h),
        Vec2(cx + h, cy + h), Vec2(cx - h, cy + h),
    ]
    return [list(proj.to_px(c)) for c in corners]


# ══════════════════════════════════════════════════════════════════
# 价格
# ══════════════════════════════════════════════════════════════════


@dataclass
class PricedHotspot:
    geom: HotspotGeom
    price_range: list[float] | None = None
    estimate_range: list[float] | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    search_url: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = self.geom.to_dict()
        d.update(
            price_range=self.price_range,
            estimate_range=self.estimate_range,
            items=self.items,
            search_url=self.search_url,
            notes=self.notes,
        )
        return d


def build_hotspots(scene: Scene, proj: Projection,
                   *, catalog_path: str | None = None) -> list[PricedHotspot]:
    """几何 + 价格 = 前端能直接渲染的热区列表。"""
    return [
        _price(g, catalog_path=catalog_path)
        for g in hotspot_geometry(scene, proj)
    ]


def _price(g: HotspotGeom, *, catalog_path: str | None) -> PricedHotspot:
    """
    给一个热区挂上材料目录里的商品。

    ⚠️ **取不到商品目录不算错误。** 热区的几何是核心价值，价格是附加信息；
    目录缺失、JSON 损坏、品类对不上，都只让价格变空 + 记一条 note，
    **绝不抛异常** —— 抛出去会让整张矢量图渲染失败，那是拿主要功能给附加功能陪葬。
    """
    try:
        items, unit, search_url = _items_for(g.category, catalog_path)
    except Exception as e:  # noqa: BLE001
        return PricedHotspot(
            geom=g,
            notes=[f"材料目录不可用（{type(e).__name__}），本热区只显示几何位置"],
        )

    if not items:
        return PricedHotspot(
            geom=g,
            unit=unit or g.unit,
            notes=[f"材料目录中没有「{g.category}」品类的商品"],
        )

    prices = [i["price"] for i in items if i.get("price")]
    price_range = [min(prices), max(prices)] if prices else None

    estimate = None
    if price_range and g.quantity:
        estimate = [
            round(price_range[0] * g.quantity),
            round(price_range[1] * g.quantity),
        ]

    return PricedHotspot(
        geom=g,
        price_range=price_range,
        estimate_range=estimate,
        items=items,
        search_url=search_url,
        notes=[g.note] if g.note else [],
    )


def _items_for(category: str, catalog_path: str | None
               ) -> tuple[list[dict[str, Any]], str, str]:
    """
    取该品类下的候选商品。**全友优先**，然后按价格从低到高。

    排序刻意确定：热区里的商品顺序每次都要一样，否则前端悬停卡片的内容
    会随机变。AC-32 要的是"重放一致"。
    """
    from ..material import catalog

    products = [p for p in catalog.all_products(catalog_path) if p.category == category]
    products.sort(key=lambda p: (not p.is_quanyou, p.price_min, p.id))

    unit = ""
    try:
        for c in catalog.categories(catalog_path):
            if c.get("key") == category:
                unit = str(c.get("unit") or "")
                break
    except Exception:  # noqa: BLE001 —— 品类表读不到不影响商品列表
        unit = ""

    items = [
        {
            "id": p.id,
            "brand": p.brand,
            "name": p.name,
            "price": p.price_min,
            "price_range": list(p.price_range),
            "spec": p.spec,
            "eco_level": p.eco_level,
            "is_quanyou": p.is_quanyou,
            "url": p.search_url,
        }
        for p in products[:ITEMS_PER_HOTSPOT]
    ]
    search_url = next((i["url"] for i in items if i["url"] and i["is_quanyou"]),
                      next((i["url"] for i in items if i["url"]), ""))
    return items, unit, search_url


def hotspot_payload(scene: Scene, proj: Projection,
                    *, catalog_path: str | None = None) -> dict[str, Any]:
    """
    交给接口层的完整载荷。

    带 `disclaimer` —— 价格来自 `seed_data/material_catalog.json`
    （**演示数据**），必须跟着数据一起走，不能只写在文档里。
    """
    hotspots = build_hotspots(scene, proj, catalog_path=catalog_path)

    disclaimer = ""
    try:
        from ..material import catalog

        disclaimer = catalog.disclaimer(catalog_path)
    except Exception:  # noqa: BLE001
        disclaimer = ""

    by_precision: dict[str, int] = {}
    for h in hotspots:
        by_precision[h.geom.precision] = by_precision.get(h.geom.precision, 0) + 1

    return {
        "hotspots": [h.to_dict() for h in hotspots],
        "count": len(hotspots),
        "by_precision": by_precision,
        "source": "vector_layer",
        "disclaimer": disclaimer,
    }


__all__ = [
    "HotspotGeom", "PricedHotspot",
    "hotspot_geometry", "build_hotspots", "hotspot_payload",
    "FLOOR_CATEGORY", "WALL_CATEGORY", "DOOR_CATEGORY",
]
