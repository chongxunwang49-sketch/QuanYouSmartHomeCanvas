"""
生成演示用户型图。

═══════════════════════════════════════════════════════════════════
为什么自己画，不去网上找
═══════════════════════════════════════════════════════════════════
【一】版权。项目里已经定过一条规矩：**有版权的东西不进这个仓库**。
      网上的户型图几乎都有来源，扒下来当演示素材再用到面试里，
      是个没必要的风险。

【二】可解析性。这条更实际。视觉模型读户型图，难点在于：
      · 照片/扫描件的墙线发虚、有阴影、有透视
      · 网图往往带着家具、指北针、彩色填充、水印
      · 各种图例符号（尺寸线、标注、折线）会被当成墙
      自己画的话，这些干扰全都能去掉，**墙线干净、房间是纯矩形**，
      解析成功率比任何真实图都高。

【三】可控。想演示"L 形房间""长条形户型""极小户型"这些边界情况时，
      得能自己造。网上找不到刚好合适的。

═══════════════════════════════════════════════════════════════════
画的是什么
═══════════════════════════════════════════════════════════════════
按建筑制图的通行画法：
  · 外墙厚、内墙薄（0.24m / 0.12m）
  · 门：墙上留缺口 + 门扇 + 开启弧
  · 窗：墙上留缺口 + 两条平行细线
  · 房间填浅色块（帮助模型区分房间，但浅到不影响墙线）
  · 中文房间名 + 面积
  · 总尺寸标注

⚠️ **刻意不画**：家具、指北针、彩色装饰、水印、图例。
   这些东西会让模型把家具线当成墙。

═══════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════
    python scripts/make_demo_floorplans.py
    python scripts/make_demo_floorplans.py -o 某个目录
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── 制图参数 ────────────────────────────────────────────────────────

#: 每米多少像素。**取大一点**：视觉模型对细线的识别很敏感，
#: 1200px 宽的图比 600px 的解析成功率高得多（实测 A-01 在低分辨率图上
#: 会把墙认成家具边）。120 px/m 下 11.4m 的户型约 1370px。
PX_PER_M = 120

#: 图边距（像素），留给尺寸标注
MARGIN = 110
#: 顶部额外留给标题
TOP_EXTRA = 46

#: 墙厚（米）
OUTER_WALL_M = 0.24
INNER_WALL_M = 0.12

#: 门洞标准宽（米）
DOOR_M = 0.9
#: 阳台推拉门更宽
SLIDING_DOOR_M = 1.6

BG = "#FFFFFF"
WALL = "#1A1A1A"
ROOM_TINT = "#F4F1EC"
ROOM_TINT_ALT = "#EDF2EE"
FURNITURE_NONE = None      # 刻意不画家具，见文件头
INK = "#2B2B2B"
INK_SOFT = "#6B6B6B"
DOOR_COLOR = "#1A1A1A"

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/Deng.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """找一个能显示中文的字体。找不到就退回默认（会显示成方块，但不会崩）。"""
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


# ══════════════════════════════════════════════════════════════════
# 户型的声明式定义
# ══════════════════════════════════════════════════════════════════
#
# 坐标是**米**，原点在左上角、y 向下（跟图片一致，画的时候不用再翻）。
# 房间矩形必须**恰好铺满**整体范围，不留缝也不重叠 ——
# 留缝的话墙上会出现莫名其妙的双线，重叠的话模型会看到两条平行墙。


@dataclass
class Opening:
    """门窗。`along` 是「门洞中心所在的坐标」，`span` 是「沿墙方向的区间」。"""

    axis: str           # "h" = 洞在水平墙上（沿 x 展开）；"v" = 在竖直墙上
    at: float           # 墙所在的坐标（水平墙给 y，竖直墙给 x）
    center: float       # 洞中心的另一轴坐标
    width: float        # 洞宽（米）
    kind: str = "door"  # door / window


@dataclass
class Plan:
    name: str
    title: str
    size: tuple[float, float]                    # (宽 m, 深 m)
    rooms: list[tuple[str, float, float, float, float]]   # (名, x, y, w, h)
    openings: list[Opening] = field(default_factory=list)


# ── 一室一厅 · 小户型 ───────────────────────────────────────────────

PLAN_1BR = Plan(
    name="01-一室一厅-45平",
    title="一室一厅一厨一卫 · 建筑面积约 45㎡",
    size=(7.8, 6.0),
    rooms=[
        ("客厅", 0.0, 0.0, 4.2, 3.6),
        ("卧室", 0.0, 3.6, 3.0, 2.4),
        ("厨房", 4.2, 0.0, 3.6, 2.2),
        ("卫生间", 4.2, 2.2, 2.0, 1.4),
        ("阳台", 6.2, 2.2, 1.6, 3.8),
        ("玄关", 3.0, 3.6, 3.2, 2.4),
    ],
    openings=[
        Opening("h", 3.6, 2.4, DOOR_M),                 # 客厅 → 卧室
        Opening("v", 4.2, 1.2, DOOR_M),                 # 客厅 → 厨房
        Opening("h", 2.2, 5.0, 0.75),                   # 厨房 → 卫生间
        Opening("h", 3.6, 3.6, DOOR_M),                 # 客厅 → 玄关
        Opening("v", 6.2, 4.2, SLIDING_DOOR_M),         # 玄关 → 阳台
        Opening("h", 3.6, 5.3, DOOR_M),                 # 玄关 → 卫生间
        Opening("h", 6.0, 4.6, 1.0),                    # 入户门（玄关外墙）
        Opening("h", 0.0, 2.1, 1.8, "window"),          # 客厅外窗
        Opening("v", 0.0, 4.6, 1.6, "window"),          # 卧室外窗
        Opening("v", 7.8, 4.0, 1.6, "window"),          # 阳台外窗
        Opening("h", 0.0, 6.0, 1.4, "window"),          # 厨房外窗
    ],
)

# ── 两室一厅 · 主力户型 ─────────────────────────────────────────────

PLAN_2BR = Plan(
    name="02-两室一厅-78平",
    title="两室一厅一厨一卫 · 建筑面积约 86㎡",
    size=(9.6, 9.6),
    rooms=[
        # ── 上排：三间房，门全开向走廊 ──
        ("主卧", 0.0, 0.0, 4.0, 3.2),
        ("次卧", 4.0, 0.0, 3.0, 3.2),
        ("书房", 7.0, 0.0, 2.6, 3.2),
        # ── 中间：一条横贯的走廊 ──
        ("走廊", 0.0, 3.2, 9.6, 1.2),
        # ── 下排 ──
        ("客厅", 0.0, 4.4, 5.0, 4.0),
        ("餐厅", 5.0, 4.4, 4.6, 2.4),
        ("卫生间", 5.0, 6.8, 2.2, 2.8),
        ("厨房", 7.2, 6.8, 2.4, 2.8),
        ("阳台", 0.0, 8.4, 5.0, 1.2),
    ],
    openings=[
        # ⚠️ 每个房间**直接开向走廊**，不串成链。
        # 链式连通（A→B→C）的坏处：模型漏检 B→C 那一扇门，
        # C 以及 C 后面的所有房间一起变成孤岛 —— 一次漏检损失一大片。
        # 实测就是这么翻的车：漏掉"客厅↔餐厅"一处敞口，覆盖率掉到 49%，
        # 整个第一人称漫游被判定为不可用。
        # 星形连通的坏处只有"漏一间丢一间"。
        Opening("h", 3.2, 2.0, DOOR_M),                 # 主卧 → 走廊
        Opening("h", 3.2, 5.3, DOOR_M),                 # 次卧 → 走廊
        Opening("h", 3.2, 8.2, DOOR_M),                 # 书房 → 走廊
        Opening("h", 4.4, 2.5, 1.1),                    # 客厅 → 走廊
        Opening("h", 4.4, 6.4, 1.1),                    # 餐厅 → 走廊
        Opening("v", 0.0, 3.8, 1.0),                    # 入户门（走廊西端）
        Opening("h", 6.8, 6.0, 0.75),                   # 餐厅 → 卫生间
        Opening("h", 6.8, 8.4, DOOR_M),                 # 餐厅 → 厨房
        Opening("h", 8.4, 2.5, SLIDING_DOOR_M),         # 客厅 → 阳台
        # 外窗
        Opening("h", 0.0, 1.6, 2.0, "window"),          # 主卧
        Opening("h", 0.0, 5.3, 1.6, "window"),          # 次卧
        Opening("h", 0.0, 8.2, 1.4, "window"),          # 书房
        Opening("v", 0.0, 6.0, 2.0, "window"),          # 客厅
        Opening("v", 9.6, 5.2, 1.6, "window"),          # 餐厅
        Opening("v", 9.6, 7.5, 0.9, "window"),          # 厨房
        Opening("h", 9.6, 2.0, 2.4, "window"),          # 阳台
    ],
)

# ── 三室两厅 · 大户型 ───────────────────────────────────────────────

PLAN_3BR = Plan(
    name="03-三室两厅-98平",
    title="三室两厅一厨一卫 · 建筑面积约 98㎡",
    size=(11.4, 8.6),
    rooms=[
        ("主卧", 0.0, 0.0, 4.0, 3.4),
        ("次卧", 4.0, 0.0, 3.4, 3.4),
        ("儿童房", 7.4, 0.0, 4.0, 3.4),
        ("客厅", 0.0, 3.4, 5.4, 3.2),
        ("餐厅", 5.4, 3.4, 3.4, 3.2),
        ("厨房", 8.8, 3.4, 2.6, 3.2),
        ("阳台", 0.0, 6.6, 5.4, 2.0),
        ("卫生间", 5.4, 6.6, 2.6, 2.0),
        ("玄关", 8.0, 6.6, 3.4, 2.0),
    ],
    openings=[
        Opening("h", 3.4, 3.0, DOOR_M),                 # 主卧 → 客厅
        Opening("h", 3.4, 4.7, DOOR_M),                 # 次卧 → 客厅
        Opening("h", 3.4, 8.0, DOOR_M),                 # 儿童房 → 餐厅
        Opening("v", 5.4, 5.0, 1.8),                    # 客厅 ↔ 餐厅（敞口）
        Opening("v", 8.8, 5.0, DOOR_M),                 # 餐厅 → 厨房
        Opening("h", 6.6, 2.7, SLIDING_DOOR_M),         # 客厅 → 阳台
        Opening("h", 6.6, 6.4, 0.75),                   # 餐厅 → 卫生间
        Opening("h", 6.6, 10.0, DOOR_M),                # 厨房 → 玄关
        Opening("v", 8.0, 7.6, DOOR_M),                 # 卫生间 → 玄关
        Opening("h", 0.0, 1.4, 2.0, "window"),          # 主卧外窗
        Opening("h", 0.0, 5.4, 1.8, "window"),          # 次卧外窗
        Opening("h", 0.0, 9.2, 1.8, "window"),          # 儿童房外窗
        Opening("v", 0.0, 5.0, 2.2, "window"),          # 客厅外窗
        Opening("v", 11.4, 5.0, 1.6, "window"),         # 厨房外窗
        Opening("h", 8.6, 2.7, 3.0, "window"),          # 阳台外窗
    ],
)

PLANS = [PLAN_1BR, PLAN_2BR, PLAN_3BR]


# ══════════════════════════════════════════════════════════════════
# 绘制
# ══════════════════════════════════════════════════════════════════


def draw(plan: Plan) -> Image.Image:
    w_m, h_m = plan.size
    W = int(w_m * PX_PER_M) + 2 * MARGIN
    H = int(h_m * PX_PER_M) + 2 * MARGIN + TOP_EXTRA

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    ox = MARGIN
    oy = MARGIN + TOP_EXTRA

    def X(m: float) -> float:
        return ox + m * PX_PER_M

    def Y(m: float) -> float:
        return oy + m * PX_PER_M

    # ── 房间浅色块 ──
    for i, (name, x, y, rw, rh) in enumerate(plan.rooms):
        d.rectangle(
            [X(x), Y(y), X(x + rw), Y(y + rh)],
            fill=ROOM_TINT if i % 2 == 0 else ROOM_TINT_ALT,
        )

    # ── 墙体 ──
    #
    # ⚠️ **画成双线，不是一条粗线。**
    #
    # 初版把墙画成一条 29px 粗线，然后在窗的位置**用底色盖出一个缺口**、
    # 再补两条细线当窗。看上去没问题，实测却把这张图毁了：
    #
    #     解析结果 9 间房全对（名字、类型、面积一字不差），
    #     但 **墙闭合 = False、可建墙 = False** ——
    #     外墙被 6 个窗洞切成 12 段，视觉模型照实读出「墙是断的」。
    #     后果是 3D 漫游直接降级成自由视角，**第一人称行走根本起不来**。
    #
    # 而且那么画本来就不对：建筑制图里**窗不打断墙**，只有门才打断。
    # 墙用两条平行线表示内外墙面，窗户只是在墙带中间加线。
    #
    # 改成双线后，墙的轮廓线在窗的位置是**连续的**，模型能读出闭合外墙。
    # 端点各外延半个墙厚把墙角填实 —— 与 3D 渲染那里同一个理由
    # （不外延的话四个角各留一个方口）。
    for (name, x, y, rw, rh) in plan.rooms:
        edges = [
            (X(x), Y(y), X(x + rw), Y(y), y == 0),                    # 上
            (X(x), Y(y + rh), X(x + rw), Y(y + rh), y + rh == h_m),   # 下
            (X(x), Y(y), X(x), Y(y + rh), x == 0),                    # 左
            (X(x + rw), Y(y), X(x + rw), Y(y + rh), x + rw == w_m),   # 右
        ]
        for x1, y1, x2, y2, outer in edges:
            _wall_band(d, (x1, y1), (x2, y2),
                       (OUTER_WALL_M if outer else INNER_WALL_M) * PX_PER_M)

    # ── 门：**打断**墙（用底色盖掉一段）──
    for op in plan.openings:
        if op.kind != "door":
            continue
        p1, p2 = _opening_ends(op, X, Y)
        t = _thickness_px(op, plan)
        d.line([p1, p2], fill=BG, width=round(t) + 8)
        _draw_door(d, p1, p2, op.axis)

    # ── 窗：**不打断**墙，只在墙带里加线 ──
    for op in plan.openings:
        if op.kind != "window":
            continue
        p1, p2 = _opening_ends(op, X, Y)
        _draw_window(d, p1, p2, op.axis, _thickness_px(op, plan))

    # ── 房间名与面积 ──
    f_name = load_font(30)
    f_area = load_font(21)
    for (name, x, y, rw, rh) in plan.rooms:
        cx, cy = X(x + rw / 2), Y(y + rh / 2)
        area = rw * rh
        # 太窄的房间只写名字，挤在一起反而认不出来
        if rw * PX_PER_M < 90 or rh * PX_PER_M < 70:
            d.text((cx, cy), name, font=f_area, fill=INK, anchor="mm")
            continue
        d.text((cx, cy - 16), name, font=f_name, fill=INK, anchor="mm")
        d.text((cx, cy + 18), f"{area:.1f}㎡", font=f_area, fill=INK_SOFT, anchor="mm")

    # ── 总尺寸标注 ──
    _draw_dimension(d, (X(0), Y(h_m) + 46), (X(w_m), Y(h_m) + 46), f"{w_m * 1000:.0f}")
    _draw_dimension(
        d, (X(0) - 46, Y(0)), (X(0) - 46, Y(h_m)), f"{h_m * 1000:.0f}", vertical=True
    )

    # ── 标题 ──
    f_title = load_font(30)
    d.text((W / 2, MARGIN * 0.5 + 6), plan.title, font=f_title, fill=INK, anchor="mm")

    return img


def _draw_door(d: ImageDraw.ImageDraw, p1, p2, axis: str) -> None:
    """门：门扇 + 开启弧。与后端 2D 渲染器同一套画法。"""
    (x1, y1), (x2, y2) = p1, p2
    length = math.dist(p1, p2)
    if length < 4:
        return

    if axis == "h":
        hinge = (x1, y1)
        other = (x2, y2)
        normal = (0, -1)          # 门扇朝房间内侧摆
    else:
        hinge = (x1, y1)
        other = (x2, y2)
        normal = (-1, 0)

    tip = (hinge[0] + normal[0] * length, hinge[1] + normal[1] * length)
    d.line([hinge, tip], fill=DOOR_COLOR, width=4)

    # 弧线用折线逼近 —— SVG 那边也是这个理由：标志位写反了看不出来，
    # 折线只有坐标，不可能写反
    start = math.atan2(other[1] - hinge[1], other[0] - hinge[0])
    end = math.atan2(normal[1], normal[0])
    delta = (end - start + math.pi) % (2 * math.pi) - math.pi
    pts = []
    for i in range(13):
        a = start + delta * (i / 12)
        pts.append((hinge[0] + math.cos(a) * length, hinge[1] + math.sin(a) * length))
    d.line(pts, fill=INK_SOFT, width=2)


def _opening_ends(op: "Opening", X, Y):
    """门窗洞的两端（像素）。"""
    if op.axis == "h":
        return ((X(op.center - op.width / 2), Y(op.at)),
                (X(op.center + op.width / 2), Y(op.at)))
    return ((X(op.at), Y(op.center - op.width / 2)),
            (X(op.at), Y(op.center + op.width / 2)))


def _on_outer(op: "Opening", plan: "Plan") -> bool:
    """这个洞开在外墙上吗（外墙更厚）。"""
    w_m, h_m = plan.size
    if op.axis == "h":
        return abs(op.at) < 1e-6 or abs(op.at - h_m) < 1e-6
    return abs(op.at) < 1e-6 or abs(op.at - w_m) < 1e-6


def _thickness_px(op: "Opening", plan: "Plan") -> float:
    return (OUTER_WALL_M if _on_outer(op, plan) else INNER_WALL_M) * PX_PER_M


def _wall_band(d: ImageDraw.ImageDraw, p1, p2, thickness_px: float) -> None:
    """
    一段墙：两条平行线（内外墙面）。

    端点各沿墙向外延半个墙厚，把墙角填实 —— 不外延的话每个角上
    两条线各自停在角点，外侧会缺一块。
    """
    (x1, y1), (x2, y2) = p1, p2
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 1e-6:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    nx, ny = -uy, ux
    half = thickness_px / 2
    ext = half

    ax, ay = x1 - ux * ext, y1 - uy * ext
    bx, by = x2 + ux * ext, y2 + uy * ext

    for s in (1, -1):
        d.line(
            [(ax + nx * half * s, ay + ny * half * s),
             (bx + nx * half * s, by + ny * half * s)],
            fill=WALL, width=3,
        )


def _draw_door(d: ImageDraw.ImageDraw, p1, p2, axis: str) -> None:
    """门：门扇 + 开启弧。与后端 2D 渲染器同一套画法。"""
    (x1, y1), (x2, y2) = p1, p2
    length = math.dist(p1, p2)
    if length < 4:
        return

    hinge = (x1, y1)
    other = (x2, y2)
    normal = (0, -1) if axis == "h" else (-1, 0)

    tip = (hinge[0] + normal[0] * length, hinge[1] + normal[1] * length)
    d.line([hinge, tip], fill=DOOR_COLOR, width=4)

    # 弧线用折线逼近 —— SVG 那边也是这个理由：标志位写反了看不出来，
    # 折线只有坐标，不可能写反
    start = math.atan2(other[1] - hinge[1], other[0] - hinge[0])
    end = math.atan2(normal[1], normal[0])
    delta = (end - start + math.pi) % (2 * math.pi) - math.pi
    pts = []
    for i in range(13):
        a = start + delta * (i / 12)
        pts.append((hinge[0] + math.cos(a) * length, hinge[1] + math.sin(a) * length))
    d.line(pts, fill=INK_SOFT, width=2)


def _draw_window(d: ImageDraw.ImageDraw, p1, p2, axis: str, t_px: float) -> None:
    """
    窗：在**墙带内部**画两条平行细线。

    ⚠️ 关键是**不擦墙**。初版用底色在墙上盖出缺口当窗，结果外墙被窗
    切成一段段，视觉模型读出「墙断了」，3D 漫游直接失去第一人称模式。
    建筑制图里窗本来就不打断墙。
    """
    (x1, y1), (x2, y2) = p1, p2
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 4:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    nx, ny = -uy, ux
    off = t_px * 0.22

    for s in (1, -1):
        d.line([(x1 + nx * off * s, y1 + ny * off * s),
                (x2 + nx * off * s, y2 + ny * off * s)], fill=WALL, width=3)

    # 两侧封头，让窗户看起来是"一段构件"而不是两根悬空的线
    for (px, py) in (p1, p2):
        d.line([(px + nx * t_px * 0.5, py + ny * t_px * 0.5),
                (px - nx * t_px * 0.5, py - ny * t_px * 0.5)],
               fill=WALL, width=3)


def _draw_dimension(d: ImageDraw.ImageDraw, p1, p2, label: str, vertical: bool = False) -> None:
    """尺寸标注：一条带端点的线 + 数字。数字**必须画** —— A-01 会读它。"""
    d.line([p1, p2], fill=INK_SOFT, width=2)
    tick = 9
    for p in (p1, p2):
        if vertical:
            d.line([(p[0] - tick, p[1]), (p[0] + tick, p[1])], fill=INK_SOFT, width=2)
        else:
            d.line([(p[0], p[1] - tick), (p[0], p[1] + tick)], fill=INK_SOFT, width=2)

    f = load_font(22)
    mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
    if vertical:
        # 竖排数字要转 90°，PIL 不支持旋转文字 —— 用一个临时图层转
        tmp = Image.new("RGBA", (110, 30), (0, 0, 0, 0))
        ImageDraw.Draw(tmp).text((55, 15), label, font=f, fill=INK_SOFT, anchor="mm")
        tmp = tmp.rotate(90, expand=True)
        d._image.paste(tmp, (int(mid[0] - tmp.width / 2), int(mid[1] - tmp.height / 2)), tmp)
    else:
        d.text(mid, label, font=f, fill=INK_SOFT, anchor="mm")


# ══════════════════════════════════════════════════════════════════


def main() -> int:
    ap = argparse.ArgumentParser(description="生成演示用户型图")
    ap.add_argument("-o", "--out", default="演示素材/户型图", help="输出目录")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for plan in PLANS:
        img = draw(plan)
        path = out / f"{plan.name}.png"
        img.save(path, "PNG", optimize=True)
        print(f"  {path}   {img.size[0]}×{img.size[1]}  "
              f"{plan.size[0]}×{plan.size[1]}m  {len(plan.rooms)} 间房")

    print(f"\n共 {len(PLANS)} 张，输出到 {out.resolve()}")
    print("在解析页直接上传即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
