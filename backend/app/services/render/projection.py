"""
米制场景 → 像素画布。**2D 渲染与热区共用的唯一一份坐标变换。**

═══════════════════════════════════════════════════════════════════
为什么这个"小工具"要单独成一个模块
═══════════════════════════════════════════════════════════════════
因为它守的是一个**不会报错的错**。

需求文档 2.2.6 里最担心的失败模式是这个：

    用户悬停在"看起来像地板"的地方，提示却说"这是主卧墙面"。

这种错不会抛异常、不会让测试变红、界面看上去完全正常 —— 唯一的发现方式是
拿鼠标一个个房间去试。而它的成因几乎总是同一个：**画图的坐标和热区的坐标
是两套**。两边各自算一遍比例尺、各自处理 Y 轴、各自决定留白，只要有一处
不一样，热区就会整体漂移百分之几 —— 正好是那种"说不清哪里不对"的偏差。

所以这里只留**一个** `Projection` 对象：渲染器用它画 `<polygon>`，
热区用它算 `bbox`。同一个对象，同一个 `scale`，同一个 `offset`，
物理上不可能漂。

═══════════════════════════════════════════════════════════════════
Y 轴在这里翻第二次
═══════════════════════════════════════════════════════════════════
    图片坐标  Y 向下  ──① 几何内核翻转 ──▶  场景坐标  Y 向上
                                             ──② 这里翻转 ──▶  画布坐标 Y 向下

两次翻转**一次都不能省**：
  · 省掉 ① —— 3D 场景是左右镜像的（而且看上去完全正常）
  · 省掉 ② —— 2D 图是**上下颠倒**的（这个肉眼能看出来，但会先怀疑是识别错了）
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..geometry import Scene
from ..geometry.normalize import Vec2

#: 画布四周留白的默认值（像素）。留白不是装饰 —— 门窗符号会向墙外画出
#: 门扇弧线，紧贴边界的户型不带留白就会被裁掉。
DEFAULT_PAD_PX = 40

#: AC-07 要求"512×512 以上"。默认给到 1024，因为矢量图放大不掉质量，
#: 而前端在高分屏上按 512 显示会略虚。
DEFAULT_MIN_SIDE_PX = 1024

#: 户型退化（宽或深为 0）时的兜底比例尺。不为 0 就行 —— 目的是让后续
#: 计算不出现 inf / nan，而不是画出一张有意义的图。
_FALLBACK_PX_PER_M = 50.0


@dataclass(frozen=True)
class Projection:
    """
    场景米制坐标 → 画布像素坐标。

    `scale` 是**等比的**（x 与 y 同一个因子）。这一条不是顺手写对就好 ——
    需求文档 0.2 的坑 9 记录过一次同类事故：条件图渲染对 x / y 各用一个
    缩放因子，等于把 685×505 的户型强行拉成正方形，ControlNet 于是照着
    变形的结构去生成。**只要有"分别归一化"的地方，就会长出这个 bug。**
    """

    scale: float
    """像素 / 米。等比的。"""

    offset_x: float
    offset_y: float
    """画布左上角到**绘图区**左上角的像素距离。"""

    draw_width_m: float
    draw_depth_m: float
    """实际绘图区的米制范围（退化时会被替换成兜底值）。"""

    width_px: int
    height_px: int

    def to_px(self, p: Vec2) -> tuple[float, float]:
        """
        场景点 → 画布点。**这里做 Y 轴翻转（②）。**

        场景原点在户型左下角、Y 向上；画布原点在左上角、Y 向下。
        所以场景里 y 越大（越靠上）的点，画布上 y 越小。
        """
        return (
            self.offset_x + p.x * self.scale,
            self.offset_y + (self.draw_depth_m - p.y) * self.scale,
        )

    def polyline_to_px(self, pts: list[Vec2]) -> list[tuple[float, float]]:
        return [self.to_px(p) for p in pts]

    def rect_to_px(
        self, x1: float, y1: float, x2: float, y2: float
    ) -> tuple[float, float, float, float]:
        """
        米制矩形（左下 / 右上）→ 画布矩形（左上 / 右下）。

        返回顺序统一为 `(left, top, right, bottom)`，可直接喂给 SVG 的
        `<rect>` 与热区的 `bbox` —— 两者用同一个函数，就不会出现
        "热区 bbox 是 (x1,y1,x2,y2)、SVG rect 是 (x,y,w,h)" 这种口径差。
        """
        lx, by = self.to_px(Vec2(x1, y1))     # 左下 → 画布左下
        rx, ty = self.to_px(Vec2(x2, y2))     # 右上 → 画布右上
        left, right = min(lx, rx), max(lx, rx)
        top, bottom = min(ty, by), max(ty, by)
        return left, top, right, bottom

    def metres_to_px(self, m: float) -> float:
        """长度（不是坐标）换算。没有翻转、没有平移。"""
        return m * self.scale

    def as_dict(self) -> dict[str, float | int]:
        """给前端用的变换参数 —— 前端要在图片上叠加自己的东西时用得到。"""
        return {
            "scale": round(self.scale, 4),
            "offset_x": round(self.offset_x, 3),
            "offset_y": round(self.offset_y, 3),
            "draw_width_m": round(self.draw_width_m, 4),
            "draw_depth_m": round(self.draw_depth_m, 4),
            "width_px": self.width_px,
            "height_px": self.height_px,
        }


def build_projection(
    scene: Scene,
    *,
    min_side_px: int = DEFAULT_MIN_SIDE_PX,
    pad_px: int = DEFAULT_PAD_PX,
) -> Projection:
    """
    按场景范围算出一套画布参数。

    ══════════════════════════════════════════════════════════════════
    画布的"至少 512×512"是**靠留白撑出来的，不是靠放大**
    ══════════════════════════════════════════════════════════════════
    AC-07 的字面要求是"512×512 以上"。最省事的实现是
    `scale = 512 / min(宽, 深)` —— 但那样一个 20m×2m 的长条户型会被放大成
    10:1 的画布（长边 5120px），比例尺荒诞，图上只有一条细线。

    这里改成：**比例尺由长边决定**（长边撑满绘图区），短边不足 min_side 时
    **补留白而不是补缩放**。于是任何户型都满足 512×512，且比例尺始终
    是"长边 = 绘图区"，物理意义稳定。

    退化的场景（宽或深为 0，比如 degraded_basic 没有任何 bbox）不抛异常 ——
    AC-07 说的是"**任何**户型都能渲出"，包含最烂的那一种。
    """
    draw_w = scene.width_m
    draw_d = scene.depth_m

    if not math.isfinite(draw_w) or draw_w <= 1e-6:
        draw_w = 1.0
    if not math.isfinite(draw_d) or draw_d <= 1e-6:
        draw_d = 1.0

    min_side_px = max(int(min_side_px), 64)
    pad_px = max(int(pad_px), 0)

    inner = max(min_side_px - 2 * pad_px, 16)
    long_side_m = max(draw_w, draw_d)
    scale = inner / long_side_m
    if not math.isfinite(scale) or scale <= 0:
        scale = _FALLBACK_PX_PER_M

    drawn_w, drawn_d = draw_w * scale, draw_d * scale

    # 短边用留白补齐到 min_side —— 见上面「靠留白撑」那段
    width_px = int(math.ceil(max(drawn_w + 2 * pad_px, min_side_px)))
    height_px = int(math.ceil(max(drawn_d + 2 * pad_px, min_side_px)))

    return Projection(
        scale=scale,
        offset_x=(width_px - drawn_w) / 2,
        offset_y=(height_px - drawn_d) / 2,
        draw_width_m=draw_w,
        draw_depth_m=draw_d,
        width_px=width_px,
        height_px=height_px,
    )
