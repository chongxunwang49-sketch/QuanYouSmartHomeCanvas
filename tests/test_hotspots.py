"""
物品热区（AC-09 / AC-21）。

═══════════════════════════════════════════════════════════════════
这个文件的核心只有一条断言
═══════════════════════════════════════════════════════════════════
**热区必须和画出来的房间重合。**

需求文档 2.2.6 里最担心的失败模式是这个：

> 用户悬停在"看起来像地板"的地方，提示却说"这是主卧墙面"。

它的成因几乎总是"画图的坐标和热区的坐标是两套"。所以这里不检查
"热区函数返回了东西"，而是**从 SVG 里把画出来的房间多边形读回来**，
再把热区多边形放上去比对 —— 拿两边的**实际坐标**做几何判断。

这样即使 `svg.py` 和 `hotspots.py` 哪天各改各的（比如渲染加了留白、
热区没加），这条也会立刻变红。两个模块共用一个 `Projection` 是设计上的
保证，这个文件是**验证那个保证真的成立**。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from backend.app.services.geometry import normalize_layout
from backend.app.services.render import hotspot_payload, render_scene_svg
from backend.app.services.render.hotspots import hotspot_geometry

SVG_NS = "{http://www.w3.org/2000/svg}"

REAL_LAYOUT = {
    "rooms": [
        {"name": "卧室", "type": "bedroom", "area": 11.7, "bbox": [40, 40, 380, 300]},
        {"name": "卧室", "type": "bedroom", "area": 11.1, "bbox": [40, 300, 380, 545]},
        {"name": "客厅", "type": "living_room", "area": 23.1, "bbox": [380, 40, 725, 545]},
    ],
    "walls": [
        {"type": "unknown", "coords": [[40, 40], [725, 40], [725, 545], [40, 545], [40, 40]]},
        {"type": "unknown", "coords": [[380, 40], [380, 545]]},
        {"type": "unknown", "coords": [[40, 300], [380, 300]]},
    ],
    "doors": [
        {"position": [367, 197], "width": 0.0},
        {"position": [142, 315], "width": 0.0},
        {"position": [400, 407], "width": 0.0},
    ],
    "windows": [
        {"position": [228, 40], "width": 1.7},
        {"position": [575, 545], "width": 1.8},
    ],
    "dimensions": [{"label": "8200", "value": 8.2}],
    "total_area": 45.9,
    "confidence": 0.45,
}

DEGRADED = {"mode": "degraded_basic",
            "rooms": [{"name": "客厅", "type": "other", "area": 0.0, "bbox": []}],
            "walls": [], "doors": [], "windows": [], "total_area": 0.0}


def _setup(layout: dict | None = None):
    scene = normalize_layout(layout if layout is not None else REAL_LAYOUT)
    plan = render_scene_svg(scene)
    geoms = hotspot_geometry(scene, plan.projection)
    return scene, plan, geoms


# ── 几何小工具（都按"量出来"写，不做近似判断）────────────────────


def _svg_polys(svg: str) -> dict[str, list[tuple[float, float]]]:
    root = ET.fromstring(svg)
    out = {}
    for el in root.iter(f"{SVG_NS}polygon"):
        if (el.get("id") or "").startswith("room-"):
            out[el.get("id")] = [
                tuple(float(v) for v in pair.split(","))
                for pair in (el.get("points") or "").split()
            ]
    return out


def _hotspot_paths(svg: str) -> dict[int, str]:
    root = ET.fromstring(svg)
    return {
        int(el.get("data-hotspot")): el.get("d") or ""
        for el in root.iter(f"{SVG_NS}path")
        if el.get("data-hotspot") is not None
    }


def _path_rings(d: str) -> list[list[tuple[float, float]]]:
    rings = []
    for sub in d.split(" Z"):
        sub = sub.strip()
        if not sub.startswith("M"):
            continue
        rings.append([tuple(float(v) for v in pt.split()) for pt in sub[1:].split(" L")])
    return rings


def _rings_bbox(rings) -> tuple[float, float, float, float]:
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return min(xs), min(ys), max(xs), max(ys)


def _inside(pt, ring) -> bool:
    """射线法。"""
    x, y = pt
    n, inside = len(ring), False
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def _area(ring) -> float:
    s = 0.0
    for a, b in zip(ring, ring[1:] + ring[:1]):
        s += a[0] * b[1] - b[0] * a[1]
    return abs(s) / 2


# ══════════════════════════════════════════════════════════════════
# 核心：热区与画面重合
# ══════════════════════════════════════════════════════════════════


class TestHotspotsMatchTheDrawing:
    def test_地面热区落在对应房间里(self):
        """
        ⚠️ **这个文件存在的理由。**

        地面热区是按房间顺序产出的，所以第 k 个地面热区必须落在
        `room-k` 里 —— 用两边**实际的像素坐标**判断，不是靠字段名对上。
        """
        _, plan, geoms = _setup()
        room_polys = _svg_polys(plan.svg)
        floors = [g for g in geoms if g.category == "floor"]
        assert len(floors) == 3

        for i, g in enumerate(floors):
            room = room_polys[f"room-{i}"]
            for pt in g.rings[0]:
                assert _inside(pt, room), (
                    f"「{g.label}」的顶点 {pt} 不在它对应的 room-{i} 里 —— "
                    f"热区和画面用的是两套坐标"
                )

    def test_地面热区与墙保持一个墙厚(self):
        """
        热区边界不该压在墙线下面。

        几何上应当是"房间边界往内缩半个墙厚"。这里量缩了多少，
        而不是相信 `_rect_rings` 缩过 —— 缩错了（比如缩反成外扩）
        热区就会盖住墙线，视觉上完全看不出来。
        """
        scene, plan, geoms = _setup()
        proj = plan.projection
        room_polys = _svg_polys(plan.svg)
        half_px = max(w.thickness_m for w in scene.walls) / 2 * proj.scale

        for i, g in enumerate([g for g in geoms if g.category == "floor"]):
            room = _rings_bbox([room_polys[f"room-{i}"]])
            hot = _rings_bbox([g.rings[0]])
            # 四条边各自内缩约 half_px（左右上下）
            for got, expect in ((hot[0] - room[0], half_px),
                                (hot[1] - room[1], half_px),
                                (room[2] - hot[2], half_px),
                                (room[3] - hot[3], half_px)):
                assert abs(got - expect) < 2.0, (
                    f"「{g.label}」内缩 {got:.1f}px，应当是 {expect:.1f}px"
                )

    def test_地面热区不越到隔壁房间(self):
        """相邻房间的地面热区互相不重叠 —— 重叠处悬停会命中错的那间。"""
        _, plan, geoms = _setup()
        floors = [g for g in geoms if g.category == "floor"]
        for i, a in enumerate(floors):
            for j, b in enumerate(floors):
                if i >= j:
                    continue
                for pt in b.rings[0]:
                    assert not _inside(pt, a.rings[0]), (
                        f"「{b.label}」的顶点 {pt} 落进了「{a.label}」"
                    )

    def test_墙面热区是挖空的(self):
        """
        ⚠️ 墙面热区必须是**两个环**（外环 + 内环）。

        只画外环的话，整间房都会被"墙面"盖住，用户永远悬停不到地面 ——
        而界面上看起来完全正常（墙面热区当然覆盖整间房嘛）。
        这是"错了但不报错"的典型，所以直接数环数。

        另外内环必须真的在外环里面；两个环写反了会得到一个空命中区域。
        """
        _, _, geoms = _setup()
        walls = [g for g in geoms if g.category == "paint"]
        assert walls
        for g in walls:
            assert len(g.rings) == 2, f"「{g.label}」只有 {len(g.rings)} 个环，没挖空"
            outer, inner = g.rings
            assert _area(outer) > _area(inner)
            for pt in inner:
                assert _inside(pt, outer), "内环跑到外环外面去了"
            # 内环有实际面积（不是退化成一条线）
            assert _area(inner) > 100, f"内环太小（{_area(inner):.0f}px²），挖不出洞"

    def test_门热区盖住墙线(self):
        """
        门热区应当**跨在墙上** —— 门的中心点由几何内核落在墙上，
        热区是以那个点为中心、沿墙铺开一个门宽。

        量法：热区中心到最近墙段的距离必须小于半个墙厚。
        距离算法反了（比如沿墙位置算成从墙尾起算）门就会偏出去，
        而画面上依然是个规规矩矩的小方块。
        """
        import math

        scene, plan, geoms = _setup()
        proj = plan.projection
        half_px = max(w.thickness_m for w in scene.walls) / 2 * proj.scale
        wall_px = [
            [proj.to_px(p) for p in w.points] for w in scene.walls
        ]

        def dist_to_walls(pt) -> float:
            best = float("inf")
            for poly in wall_px:
                for a, b in zip(poly, poly[1:]):
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    if dx == dy == 0:
                        continue
                    t = max(0.0, min(1.0, ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy)
                                        / (dx * dx + dy * dy)))
                    best = min(best, math.dist(pt, (a[0] + t * dx, a[1] + t * dy)))
            return best

        doors = [g for g in geoms if g.category == "door"]
        assert len(doors) == 3
        for g in doors:
            cx = (g.bbox[0] + g.bbox[2]) / 2
            cy = (g.bbox[1] + g.bbox[3]) / 2
            assert dist_to_walls((cx, cy)) <= half_px + 2, (
                f"门热区中心 ({cx:.0f},{cy:.0f}) 离最近的墙超过一个墙厚"
                f"（{dist_to_walls((cx, cy)):.1f}px > {half_px:.1f}px）"
            )


# ══════════════════════════════════════════════════════════════════
# 字段契约（AC-09 的"precision 字段正确传递并正确渲染"）
# ══════════════════════════════════════════════════════════════════


class TestFieldContract:
    def test_每条热区都带_precision_和_source(self):
        """AC-09 点名要求这两个字段"正确传递"。先守住取值范围。"""
        payload = hotspot_payload(*_payload_args())
        assert payload["hotspots"]
        for h in payload["hotspots"]:
            assert h["precision"] in ("exact", "room_level", "none")
            assert h["source"] in ("vector_layer", "layout_bbox_mapping")
            assert h["precision"] != "none", "矢量图的热区不该是 none（几何是量出来的）"

    def test_矢量图热区全是_exact(self):
        """坐标来自几何本身，不是生成图 —— 所以是 exact，不需要打折扣。"""
        payload = hotspot_payload(*_payload_args())
        assert payload["by_precision"] == {"exact": payload["count"]}

    def test_每条热区都有包围盒和顶点(self):
        payload = hotspot_payload(*_payload_args())
        for h in payload["hotspots"]:
            assert len(h["bbox"]) == 4
            l, t, r, b = h["bbox"]
            assert r > l and b > t, f"退化的包围盒：{h['bbox']}"
            assert len(h["polygon"]) >= 3
            assert len(h["rings"]) >= 1

    def test_热区编号与_svg_里的_data_hotspot_一一对应(self):
        """
        前端是靠 `data-hotspot` 下标去 `hotspots[]` 里查商品的。
        两边对不上，悬停就会拿到别人家的价格 —— 而页面看起来很正常。
        """
        scene, plan, geoms = _setup()
        path_idx = set(_hotspot_paths(plan.svg))
        geom_idx = {g.index for g in geoms}
        assert path_idx == geom_idx, (
            f"SVG 里的热区下标 {sorted(path_idx)} 与数据里的 {sorted(geom_idx)} 对不上"
        )
        assert geom_idx == set(range(len(geoms))), "下标必须是从 0 起的连续整数"

    def test_热区顺序是从大到小(self):
        """
        顺序 = 绘制顺序 = 命中优先级（后画的先命中）。

        必须做到"越具体的目标越靠后"：地面 → 墙面 → 门。
        否则沿着墙走鼠标时，永远命中"整间房的地面"，门热区形同虚设。
        """
        _, _, geoms = _setup()
        order = [g.category for g in geoms]
        floor_last = max(i for i, c in enumerate(order) if c == "floor")
        wall_first = min(i for i, c in enumerate(order) if c == "paint")
        door_first = min(i for i, c in enumerate(order) if c == "door")
        assert floor_last < wall_first, "地面必须画在墙面之前"
        assert wall_first < door_first, "墙面必须画在门之前"

    def test_热区都在画布内(self):
        _, plan, geoms = _setup()
        for g in geoms:
            l, t, r, b = g.bbox
            assert 0 <= l and 0 <= t and r <= plan.width_px and b <= plan.height_px, (
                f"「{g.label}」的包围盒跑到画布外了：{g.bbox} "
                f"（画布 {plan.width_px}×{plan.height_px}）"
            )

    def test_地面热区覆盖了大部分户型(self):
        """
        反向确认热区不是几块退化成细条的碎片。

        地面热区面积之和应当占画布上户型面积的 60% 以上 ——
        扣掉墙带和内缩之后正常应当接近 90%。低于这个数说明
        内缩量算错了（比如缩了一整个墙厚而不是半个）。
        """
        scene, plan, geoms = _setup()
        floors = [g for g in geoms if g.category == "floor"]
        covered = sum(_area(g.rings[0]) for g in floors)

        proj = plan.projection
        total = proj.metres_to_px(scene.width_m) * proj.metres_to_px(scene.depth_m)
        ratio = covered / total
        assert ratio > 0.60, f"地面热区只覆盖了户型的 {ratio:.0%}，像是内缩过头了"

    def test_没有房间时没有热区(self):
        scene = normalize_layout(DEGRADED)
        plan = render_scene_svg(scene)
        assert hotspot_geometry(scene, plan.projection) == []


def _payload_args():
    scene = normalize_layout(REAL_LAYOUT)
    plan = render_scene_svg(scene)
    return scene, plan.projection


# ══════════════════════════════════════════════════════════════════
# 价格（AC-21）
# ══════════════════════════════════════════════════════════════════


class TestPrices:
    def test_地面热区带上商品和单价区间(self):
        payload = hotspot_payload(*_payload_args())
        floor = next(h for h in payload["hotspots"] if h["category"] == "floor")
        assert floor["items"], "地面热区没有商品"
        assert floor["price_range"] and floor["price_range"][0] > 0
        assert floor["unit"] == "元/㎡"
        assert floor["search_url"], "没有跳转链接，AC-21 的'购买链接'就没着落"

    def test_全友商品排在前面(self):
        """
        需求文档 AC-18 要求全友产品覆盖率 ≥ 60%。
        热区卡片只放 3 件，全友不排前面的话覆盖率会被稀释。
        """
        payload = hotspot_payload(*_payload_args())
        for h in payload["hotspots"]:
            if not h["items"]:
                continue
            assert h["items"][0]["is_quanyou"], (
                f"「{h['label']}」的第一件不是全友：{h['items'][0]['brand']}"
            )

    def test_给出区域总价估算(self):
        """
        用户悬停时想看的不只是单价，还有"这一间地面大概多少钱"。
        数量 × 单价区间 = 估算区间。
        """
        payload = hotspot_payload(*_payload_args())
        floor = next(h for h in payload["hotspots"] if h["category"] == "floor")
        assert floor["estimate_range"], "没有区域总价估算"
        lo, hi = floor["estimate_range"]
        assert hi > lo > 0
        # 估算 = 单价 × 面积
        assert lo >= floor["price_range"][0] * floor["quantity"] * 0.99

    def test_墙面面积按周长乘层高(self):
        """
        墙面面积不是房间面积 —— 这两者搞混的话，一间 12㎡ 的卧室
        会被报出 12㎡ 的墙漆用量，少算近 3 倍。
        """
        payload = hotspot_payload(*_payload_args())
        wall = next(h for h in payload["hotspots"] if h["category"] == "paint")
        assert wall["quantity"] > 30, f"墙面面积只有 {wall['quantity']}㎡，像是少算了"
        assert "层高" in wall["note"] and "假设" in wall["note"], (
            "层高是假设值，必须写在 note 里"
        )
        assert "未扣除" in wall["note"], "未扣门窗洞口这件事必须说出来"

    def test_材料目录不可用时只丢价格不丢几何(self, monkeypatch):
        """
        ⚠️ **热区的几何是核心价值，价格是附加信息。**

        目录缺失/损坏时若抛异常，整张矢量图就渲染失败了 ——
        那是拿主要功能给附加功能陪葬。必须降级成"有位置、没价格"。
        """
        from backend.app.services.material import catalog

        def boom(*a, **kw):
            raise catalog.CatalogError("模拟目录损坏")

        monkeypatch.setattr(catalog, "all_products", boom)
        scene, proj = _payload_args()
        payload = hotspot_payload(scene, proj)

        assert payload["count"] == 9, "几何不该受价格影响"
        for h in payload["hotspots"]:
            assert h["price_range"] is None
            assert h["items"] == []
            assert any("目录不可用" in n for n in h["notes"]), (
                "价格缺失的原因必须写出来，不能静默返回空数组"
            )
        # 图本身必须还能渲染
        assert render_scene_svg(scene).width_px >= 512

    def test_免责声明跟着数据走(self):
        """
        价格来自 `seed_data/material_catalog.json`（**演示数据**）。
        免责声明必须和数据一起走，不能只写在文档里。
        """
        payload = hotspot_payload(*_payload_args())
        assert payload["disclaimer"], "热区载荷里没有免责声明"
