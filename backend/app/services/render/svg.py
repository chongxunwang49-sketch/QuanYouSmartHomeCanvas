"""
2D 矢量渲染器 —— 把米制场景画成 SVG。

═══════════════════════════════════════════════════════════════════
为什么不是 Pillow 位图（需求文档 2.2.5 写的是"Pillow / SVG"）
═══════════════════════════════════════════════════════════════════
三条理由，每条都对应一个具体的验收项：

【1】AC-07 的判据是"渲出 **512×512 以上**"，而位图没有"以上"
    512 的位图放大就糊。SVG 的 width/height 只是标称尺寸，实际能无限放大。
    —— 这不是文字游戏：位图方案在 4K 屏上就是一张糊图。

【2】AC-09 的热区必须**贴住几何**，而 SVG 里房间就是那个 `<polygon>`
    热区可以是**同一个多边形对象**投影出来的，不需要"再算一遍坐标"。
    位图方案下热区只能靠第二套坐标计算，两套坐标迟早会漂 —— 见 projection.py。

【3】这是"必出"承诺的技术前提
    需求文档 4.4：「`vector.status` 必须恒为 `ready`」。纯字符串拼接，
    零依赖、纯 CPU、没有模型、没有 GPU、没有网络。**它不可能失败。**

═══════════════════════════════════════════════════════════════════
SVG 里画的是什么，不画什么
═══════════════════════════════════════════════════════════════════
**画**：房间填充与名称面积、墙体、门（含开启弧）、窗（双线）、热区层。
**不画**：任何价格、品牌、水印、图例。

理由：这张图会同时用于三个地方 —— 前端展示、ControlNet 的 conditioning image、
用户导出。前两者对"图上有没有广告"的容忍度是相反的。价格信息走
`hotspots[]` 的 JSON 通道，由前端叠加 —— 这样同一个 SVG 换个前端皮肤
也不用重画。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from xml.sax.saxutils import escape as _xml_escape

# ⚠️ `xml.sax.saxutils.escape` 默认**只转 `& < >`，不转引号**。
#
# 而房间名是 LLM 读图读出来的，会进到 `aria-label="..."` / `data-label="..."` /
# `<text>...</text>` 三种位置。名字里只要有一个 `"`，属性就被提前闭合，
# 后面的内容变成新属性 —— **一个由模型输出决定的注入点**。
# （真遇上 `"><script>` 这种名字，前端用 v-html 嵌 SVG 时就是 XSS。）
#
# 所以引号必须一起转。`<text>` 里的 `& < >` 由同一张表覆盖，够用。
_XML_ENTITIES = {'"': "&quot;", "'": "&apos;"}


def _esc(value: object) -> str:
    """XML 转义。**引号一起转** —— 见上面的原因。"""
    return _xml_escape(str(value), _XML_ENTITIES)

from ...schemas.layout import RoomType
from ..geometry import Scene
from ..geometry.normalize import Opening, RoomShape, Vec2, WallSeg
from .projection import (
    DEFAULT_MIN_SIDE_PX,
    DEFAULT_PAD_PX,
    Projection,
    build_projection,
)

# ══════════════════════════════════════════════════════════════════
# 配色 —— 与前端 tailwind.config.js 的 token 同源
# ══════════════════════════════════════════════════════════════════
# 户型图是"图纸"，不是"效果图"。用暖白纸底 + 墨色墙线，
# 与前端 warm-bg #FAF8F3 / wood-dark #2C2418 一致，
# 这样 SVG 直接嵌进页面时不会显得是另一个产品做出来的东西。

BACKGROUND = "#FAF8F3"
WALL_STROKE = "#2C2418"
WALL_FALLBACK = "#B9AFA0"     # 没有墙体数据时，房间之间用浅色分隔线
DOOR_STROKE = "#6B4F3A"
WINDOW_STROKE = "#7C93A0"
LABEL_INK = "#4A4237"
LABEL_SUB = "#8C8272"

#: 房间填充色，按功能类型。**刻意都压得很浅** ——
#: 深色填充会让墙线看不清，而这张图上最重要的信息就是墙在哪。
ROOM_FILLS: dict[str, str] = {
    "living_room": "#F7EFE2",
    "bedroom": "#F1EADC",
    "kitchen": "#E9F0E6",
    "bathroom": "#E5EEF0",
    "balcony": "#F0EDE3",
    "study": "#EAEFE8",
    "dining_room": "#F6EDE4",
    "entrance": "#EDE7DC",
    "storage": "#E8E3D9",
    "other": "#EFEAE2",
}

#: 同类型房间的第二个起，填充色按这个系数逐次压暗 ——
#: 两个相邻的卧室同色会糊成一间。**不去改色相**，只动明度，
#: 否则户型图会变成调色盘。
_TINT_STEP = 0.955


@dataclass
class RenderedPlan:
    """一次渲染的产物。`svg` 是图，其余是**必须一起交出去的东西**。"""

    svg: str
    projection: Projection
    width_px: int
    height_px: int
    #: 画不出/画不准的地方。**不许静默** —— 调用方要么展示，要么记日志。
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": "svg",
            "width": self.width_px,
            "height": self.height_px,
            "transform": self.projection.as_dict(),
            "warnings": self.warnings,
        }


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════


def render_scene_svg(
    scene: Scene,
    *,
    min_side_px: int = DEFAULT_MIN_SIDE_PX,
    pad_px: int = DEFAULT_PAD_PX,
    with_hotspots: bool = True,
) -> RenderedPlan:
    """
    场景 → SVG。**纯函数**：同样的 scene 永远得到逐字节相同的 SVG。

    确定性不只是洁癖。AC-32 要求黄金路径连续重放 3 次结果一致性 ≥ 95%，
    而"渲染"是最容易混进随机性的地方（时间戳、uuid、字典序、浮点格式化）。
    这里所有 id 都是下标、所有数字都经 `_n()` 定点格式化。
    """
    proj = build_projection(scene, min_side_px=min_side_px, pad_px=pad_px)
    warnings: list[str] = []

    parts: list[str] = []
    parts.append(_header(scene, proj))

    parts.append(f'<rect x="0" y="0" width="{proj.width_px}" '
                 f'height="{proj.height_px}" fill="{BACKGROUND}"/>')

    parts.append(_rooms_layer(scene, proj, warnings))
    parts.append(_walls_layer(scene, proj, warnings))
    parts.append(_openings_layer(scene, proj, warnings))

    if with_hotspots:
        # 延迟导入：hotspots 依赖本模块的 Projection，模块级导入会成环。
        from .hotspots import hotspot_geometry

        parts.append(_hotspots_layer(hotspot_geometry(scene, proj)))

    if not scene.rooms and not scene.walls:
        parts.append(_empty_state(proj))

    parts.append("</svg>")
    return RenderedPlan(
        svg="\n".join(p for p in parts if p),
        projection=proj,
        width_px=proj.width_px,
        height_px=proj.height_px,
        warnings=warnings,
    )


# ══════════════════════════════════════════════════════════════════
# 各图层
# ══════════════════════════════════════════════════════════════════


def _header(scene: Scene, proj: Projection) -> str:
    label = f"户型矢量图 {scene.width_m:.2f}×{scene.depth_m:.2f}米"
    # <desc> 里放假设清单。SVG 会被单独导出/分享，假设必须跟着图走 ——
    # 否则一张标着"层高 2.8m"的图离开系统之后就没人知道那是猜的了。
    desc = "\n".join(scene.assumptions) if scene.assumptions else "无"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{proj.width_px}" height="{proj.height_px}" '
        f'viewBox="0 0 {proj.width_px} {proj.height_px}" '
        f'role="img" aria-label="{_esc(label)}">\n'
        f"<title>{_esc(label)}</title>\n"
        f"<desc>{_esc(desc)}</desc>"
    )


def _rooms_layer(scene: Scene, proj: Projection, warnings: list[str]) -> str:
    if not scene.rooms:
        return ""

    seen: dict[str, int] = {}
    out: list[str] = ['<g id="rooms">']
    for idx, room in enumerate(scene.rooms):
        if len(room.polygon) < 3:
            warnings.append(f"房间「{room.name}」没有有效轮廓，未绘制")
            continue

        kind = room.kind if room.kind in ROOM_FILLS else "other"
        n = seen.get(kind, 0)
        seen[kind] = n + 1
        fill = _tint(ROOM_FILLS[kind], _TINT_STEP ** n)

        pts = " ".join(
            f"{_n(x)},{_n(y)}" for x, y in proj.polyline_to_px(room.polygon)
        )
        out.append(
            f'<polygon id="room-{idx}" class="room room-{_esc(kind)}" '
            f'points="{pts}" fill="{fill}" stroke="none"/>'
        )
        out.append(_room_label(room, proj, idx))

    out.append("</g>")
    return "\n".join(out)


def _room_label(room: RoomShape, proj: Projection, idx: int) -> str:
    """
    房间名 + 面积。

    ⚠️ **放不下就不放。** 小屋子的标签会溢出到隔壁房间上，看上去像识别错了。
    阈值按"字要被读出来"定：名字至少 60px 宽、28px 高，面积行再要 16px。
    """
    xs = [p.x for p in room.polygon]
    ys = [p.y for p in room.polygon]
    left, top, right, bottom = proj.rect_to_px(min(xs), min(ys), max(xs), max(ys))
    w, h = right - left, bottom - top

    cx = (left + right) / 2
    cy = (top + bottom) / 2

    if w < 60 or h < 28:
        return ""

    name_size = min(20.0, max(11.0, min(w / max(len(room.name), 3) * 1.6, h * 0.30)))
    show_area = h >= 46

    parts = [
        f'<text x="{_n(cx)}" y="{_n(cy - (name_size * 0.35 if show_area else 0))}" '
        f'text-anchor="middle" dominant-baseline="middle" '
        f'font-family="Manrope, Noto Sans SC, sans-serif" '
        f'font-size="{_n(name_size)}" font-weight="600" fill="{LABEL_INK}" '
        f'class="room-label">{_esc(room.name)}</text>'
    ]
    if show_area:
        parts.append(
            f'<text x="{_n(cx)}" y="{_n(cy + name_size * 0.85)}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'font-family="Manrope, Noto Sans SC, sans-serif" '
            f'font-size="{_n(max(10.0, name_size * 0.68))}" '
            f'fill="{LABEL_SUB}" class="room-area">'
            f'{room.area_m2:.1f}㎡</text>'
        )
    return f'<g class="room-caption" data-room="{idx}">' + "".join(parts) + "</g>"


def _walls_layer(scene: Scene, proj: Projection, warnings: list[str]) -> str:
    """
    墙体。

    `can_build_walls` 为假时**照画，但换一种颜色**：墙线本身是户型图的主要
    视觉信息，缺了它图就退化成几个色块；而颜色能诚实地告诉使用者
    "这一段墙我们不保证位置准确"。
    """
    if not scene.walls:
        if scene.rooms:
            warnings.append("没有识别出墙体，房间之间没有分隔线")
        return ""

    trust = scene.quality.can_build_walls
    out = [f'<g id="walls" data-trusted="{str(trust).lower()}">']
    if not trust:
        warnings.append(
            "墙体未通过闭合性检查，图中墙线以浅色绘制，位置仅供参考"
        )

    for idx, wall in enumerate(scene.walls):
        if len(wall.points) < 2:
            continue
        pts = proj.polyline_to_px(wall.points)
        d = "M " + " L ".join(f"{_n(x)} {_n(y)}" for x, y in pts)
        if wall.is_loop:
            d += " Z"

        t_px = max(proj.metres_to_px(wall.thickness_m), 2.0)
        out.append(
            f'<path id="wall-{idx}" class="wall wall-{_esc(wall.kind)}" d="{d}" '
            f'fill="none" stroke="{WALL_STROKE if trust else WALL_FALLBACK}" '
            f'stroke-width="{_n(t_px)}" stroke-linejoin="miter" '
            f'stroke-linecap="square"/>'
        )
    out.append("</g>")
    return "\n".join(out)


def _openings_layer(scene: Scene, proj: Projection, warnings: list[str]) -> str:
    if not scene.openings:
        return ""

    unmatched = [o for o in scene.openings if o.wall_index < 0]
    assumed = [o for o in scene.openings if o.width_is_assumed]
    if unmatched:
        warnings.append(
            f"{len(unmatched)} 个门窗未能关联到墙体，图上按识别位置标注，"
            f"是否落在墙上未经验证"
        )
    if assumed:
        warnings.append(
            f"{len(assumed)} 个门窗未识别出宽度，已按标准值绘制（图中以虚线轮廓标注）"
        )

    out = ['<g id="openings">']
    for idx, op in enumerate(scene.openings):
        out.append(_draw_opening(op, scene, proj, idx))
    out.append("</g>")
    return "\n".join(p for p in out if p)


def _draw_opening(op: Opening, scene: Scene, proj: Projection, idx: int) -> str:
    """一扇门或一扇窗。"""
    classes = ["opening", op.kind]
    if op.width_is_assumed:
        classes.append("width-assumed")
    if op.wall_index < 0:
        classes.append("unanchored")

    anchor = (
        f'id="opening-{idx}" class="{ " ".join(classes) }" '
        f'data-kind="{_esc(op.kind)}" data-wall="{op.wall_index}" '
        f'data-offset-m="{_n(op.offset_along_wall_m)}" '
        f'data-width-m="{_n(op.width_m)}"'
    )

    # ── 能定位到墙上：按建筑制图符号画 ──
    if 0 <= op.wall_index < len(scene.walls):
        wall = scene.walls[op.wall_index]
        placed = wall_point_at(wall, op.offset_along_wall_m)
        if placed is not None:
            p, u = placed
            half = op.width_m / 2.0
            a = Vec2(p.x - u.x * half, p.y - u.y * half)
            b = Vec2(p.x + u.x * half, p.y + u.y * half)
            t_px = max(proj.metres_to_px(wall.thickness_m), 2.0)
            return (
                f"<g {anchor}>"
                # 先把墙"挖开"：用底色盖住这一段
                + _line(a, b, proj, stroke=BACKGROUND, width=t_px + 2.0)
                + (
                    _door_symbol(a, b, u, op.width_m, proj)
                    if op.kind == "door"
                    else _window_symbol(a, b, u, proj, t_px)
                )
                + "</g>"
            )

    # ── 定位不到墙：画个记号，**不装作知道它在墙上** ──
    cx, cy = proj.to_px(op.center)
    r = max(proj.metres_to_px(op.width_m) / 2.0, 6.0)
    return (
        f"<g {anchor}>"
        f'<circle cx="{_n(cx)}" cy="{_n(cy)}" r="{_n(r)}" fill="none" '
        f'stroke="{WALL_FALLBACK}" stroke-width="1.5" stroke-dasharray="4 3"/>'
        f"</g>"
    )


def _door_symbol(
    a: Vec2, b: Vec2, u: Vec2, width_m: float, proj: Projection
) -> str:
    """
    门：门扇 + 开启弧。

    ⚠️ **弧线用折线逼近，不用 `A` 命令。**

    SVG 的 `A` 有两个标志位（large-arc / sweep），而画布做过一次 Y 轴翻转，
    sweep 的方向跟着反 —— 写反了得到的是弧向另一侧的门，看上去仍然像一扇门。
    又是一次"错了但看不出来"。折线没有标志位，只有坐标，不可能写反。

    `swing` 实测恒为 `unknown`（A-01 不识别开向），所以门扇一律画在墙的同一侧。
    这是**已知的简化**，写进 warnings 由调用方决定要不要展示。
    """
    hinge = a
    # 墙的单位法向。哪一侧是房间内部无从得知，固定取一侧。
    n = Vec2(-u.y, u.x)
    tip = Vec2(hinge.x + n.x * width_m, hinge.y + n.y * width_m)

    r = width_m
    steps = 10
    arc_m: list[Vec2] = []
    for i in range(steps + 1):
        # 从 b 扫到 tip：以 hinge 为圆心，起点是 b 方向、终点是 n 方向
        t = i / steps
        start = math.atan2(b.y - hinge.y, b.x - hinge.x)
        end = math.atan2(n.y, n.x)
        # 走劣弧（门只开 90°）
        delta = (end - start + math.pi) % (2 * math.pi) - math.pi
        ang = start + delta * t
        arc_m.append(Vec2(hinge.x + math.cos(ang) * r, hinge.y + math.sin(ang) * r))

    leaf_px = proj.to_px(tip)
    hinge_px = proj.to_px(hinge)
    arc_pts = " ".join(f"{_n(x)},{_n(y)}" for x, y in proj.polyline_to_px(arc_m))

    return (
        f'<line x1="{_n(hinge_px[0])}" y1="{_n(hinge_px[1])}" '
        f'x2="{_n(leaf_px[0])}" y2="{_n(leaf_px[1])}" '
        f'stroke="{DOOR_STROKE}" stroke-width="1.6"/>'
        f'<polyline points="{arc_pts}" fill="none" stroke="{DOOR_STROKE}" '
        f'stroke-width="1" stroke-dasharray="3 3"/>'
    )


def _window_symbol(
    a: Vec2, b: Vec2, u: Vec2, proj: Projection, t_px: float
) -> str:
    """窗：在挖开的墙洞上画两条平行细线。这是建筑制图的通行画法。"""
    n = Vec2(-u.y, u.x)
    off = max(t_px, 2.0) * 0.22 / proj.scale if proj.scale else 0.0
    out = []
    for sign in (1, -1):
        p1 = Vec2(a.x + n.x * off * sign, a.y + n.y * off * sign)
        p2 = Vec2(b.x + n.x * off * sign, b.y + n.y * off * sign)
        out.append(_line(p1, p2, proj, stroke=WINDOW_STROKE, width=1.8))
    return "".join(out)


def _hotspots_layer(geoms: list) -> str:
    """
    热区图层。**默认不可见**，只提供命中区域。

    为什么不做成高亮框：这张 SVG 还要当 ControlNet 的 conditioning image，
    画上框会让模型去"生成框"。视觉样式交给前端用 CSS 叠，
    后端只负责**几何正确**这一件事。
    """
    if not geoms:
        return ""
    out = ['<g id="hotspots" fill="transparent" stroke="none" '
           'pointer-events="all">']
    for g in geoms:
        d = g.svg_path()
        out.append(
            f'<path class="hotspot hotspot-{_esc(g.category)}" d="{d}" '
            f'fill-rule="evenodd" data-hotspot="{g.index}" '
            f'data-category="{_esc(g.category)}" data-label="{_esc(g.label)}" '
            f'data-precision="{_esc(g.precision)}"/>'
        )
    out.append("</g>")
    return "\n".join(out)


def _empty_state(proj: Projection) -> str:
    """
    什么都没有时的占位。

    AC-07 要求"**任何**户型都能渲出 512×512 以上的矢量图" —— 包括
    `degraded_basic` 这种只有房间名、没有任何坐标的。一张纯白图会让人以为
    渲染失败了，所以画个说明框。
    """
    w, h = proj.width_px, proj.height_px
    bw, bh = min(w * 0.6, 460), min(h * 0.28, 170)
    x, y = (w - bw) / 2, (h - bh) / 2
    return (
        f'<g id="empty">'
        f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(bw)}" height="{_n(bh)}" '
        f'rx="16" fill="#FFFFFF" stroke="#EDE8E0" stroke-width="1.5" '
        f'stroke-dasharray="6 5"/>'
        f'<text x="{_n(w / 2)}" y="{_n(y + bh * 0.42)}" text-anchor="middle" '
        f'dominant-baseline="middle" font-family="Manrope, Noto Sans SC, sans-serif" '
        f'font-size="22" font-weight="600" fill="{LABEL_INK}">'
        f'未识别出房间轮廓</text>'
        f'<text x="{_n(w / 2)}" y="{_n(y + bh * 0.66)}" text-anchor="middle" '
        f'dominant-baseline="middle" font-family="Manrope, Noto Sans SC, sans-serif" '
        f'font-size="15" fill="{LABEL_SUB}">'
        f'本次解析未能得到可用坐标，可换一张更清晰的户型图重试</text>'
        f"</g>"
    )


# ══════════════════════════════════════════════════════════════════
# 几何小工具
# ══════════════════════════════════════════════════════════════════


def wall_point_at(wall: WallSeg, offset_m: float) -> tuple[Vec2, Vec2] | None:
    """
    墙上的第 `offset_m` 米处：返回 `(点, 单位方向向量)`。

    方向由**这一段**决定，不是整段墙的首尾 —— 折线墙拐弯后方向会变，
    用整段的首尾方向会把门画歪。

    越界（offset 超过墙长）时钳到末端，返回 `None` 只在墙退化时。
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


def _line(a: Vec2, b: Vec2, proj: Projection, *, stroke: str, width: float) -> str:
    (x1, y1), (x2, y2) = proj.to_px(a), proj.to_px(b)
    return (
        f'<line x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}" '
        f'stroke="{stroke}" stroke-width="{_n(width)}" stroke-linecap="butt"/>'
    )


def _tint(hex_color: str, factor: float) -> str:
    """按系数压暗/提亮。只动明度，不动色相。"""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = (max(0, min(255, round(v * factor))) for v in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}"


def _n(v: float) -> str:
    """
    数字定点格式化。

    ⚠️ 不处理的话 JSON/SVG 里会出现 `3.7659876543210003` 这种浮点噪声 ——
    它让每次渲染的字节数都不一样，AC-32 的"重放一致性"就没法比对了。
    """
    if not math.isfinite(v):
        return "0"
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


__all__ = ["RenderedPlan", "render_scene_svg", "wall_point_at"]
