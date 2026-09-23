"""
几何一致性自检 —— 决定 AI 图上**放不放热区**（AC-28）。

═══════════════════════════════════════════════════════════════════
这个模块要防的是一件具体的事
═══════════════════════════════════════════════════════════════════
需求文档 2.2.6 的原话：

> 若 AI 图把客厅整体画偏了 8%，热区就会落到错误的位置：用户悬停在
> "看起来像地板"的地方，提示却说"这是主卧墙面"。
> **这种错误比没有热区更伤信任。**

所以规则是硬的：**IoU < 阈值 → `precision = none`，这张图不给热区。**

═══════════════════════════════════════════════════════════════════
⚠️ 阈值是预设的，没有实测依据 —— 所以默认整条路径关闭
═══════════════════════════════════════════════════════════════════
需求文档 0.2 的坑 19 自己记了这件事：

> 即使几何完全对齐，conditioning 边缘图与生成图的 Canny 边缘图之间
> IoU 也可能只有 0.4–0.6 —— **0.70 的阈值会让所有 AI 图都不显示热区**。

一个"所有图都不达标"的阈值，效果等于把功能关掉，但又让人以为它在工作。
所以 V2.2 的处理是：

    IMAGE_HOTSPOT_ENABLED = False      ← 默认。**根本不计算 IoU**
    IMAGE_HOTSPOT_IOU_THRESHOLD = None ← 待标定

这样做的意义不止是"省算力"。AC-35 要求的是
「`IMAGE_HOTSPOT_ENABLED=false` 时 AI 图无热区，且**不依赖任何未标定阈值**」——
把开关放在**最前面**、`False` 时直接返回，才算真的"不依赖"。
如果先算完 IoU 再判断开关，那个未标定的数字仍然在参与决策。

标定规程见 `calibration_report()`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from PIL import Image, ImageChops, ImageFilter

#: 比对用的统一分辨率。512 → 256 是为了速度：IoU 对分辨率不敏感
#: （两边同样下采样），而像素数少 4 倍。
COMPARE_SIZE = 256

#: 边缘二值化的阈值。PIL 的 FIND_EDGES 输出是"边缘强度"，不取阈值就全是噪声。
EDGE_THRESHOLD = 40

#: 膨胀半径（像素，在 COMPARE_SIZE 尺度上）。容忍 1–2px 的抖动 ——
#: 不膨胀的话，两条差一个像素的线算出来的 IoU 是 0，毫无意义。
DILATE_RADIUS = 2

#: AI 图**永远**达不到 exact —— 它是生成出来的，几何只能是近似。
#: 这一条写死在这里，不给调用方覆盖的机会。
AI_MAX_PRECISION = "room_level"


@dataclass
class GeoCheck:
    """一次几何自检的结论。"""

    #: none / room_level。**不给 exact**，见 AI_MAX_PRECISION
    precision: str
    iou: float | None
    threshold: float | None
    #: 阈值是哪来的（用于事后追责：这个数字是标定过的还是拍的）
    threshold_source: str
    calibrated: bool
    enabled: bool
    warning: str = ""
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def allow_hotspots(self) -> bool:
        return self.precision != "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "precision": self.precision,
            "allow_hotspots": self.allow_hotspots,
            "iou": round(self.iou, 4) if self.iou is not None else None,
            "threshold": self.threshold,
            "threshold_source": self.threshold_source,
            "calibrated": self.calibrated,
            "enabled": self.enabled,
            "warning": self.warning,
            "reason": self.reason,
        }


# ══════════════════════════════════════════════════════════════════
# 阈值解析
# ══════════════════════════════════════════════════════════════════


def resolve_threshold(explicit: float | None = None) -> tuple[float | None, str, bool]:
    """
    决定用哪个阈值。返回 `(阈值, 来源, 是否已标定)`。

    这里是**唯一**读配置的地方 —— 决策依据只有一个来源，
    免得"开关说关、阈值说开"这种自相矛盾的状态出现。
    """
    from ...core.config import settings

    if explicit is not None:
        return float(explicit), "调用方显式指定", True

    if settings.IMAGE_HOTSPOT_IOU_THRESHOLD is not None:
        return (
            float(settings.IMAGE_HOTSPOT_IOU_THRESHOLD),
            "已标定（IMAGE_HOTSPOT_IOU_THRESHOLD）",
            True,
        )

    # 未标定 —— 用一个比预设 0.70 更宽的兜底值，并**如实标记**
    return (
        float(settings.IMAGE_HOTSPOT_IOU_FALLBACK),
        "⚠️ 未标定，暂用兜底值（IMAGE_HOTSPOT_IOU_FALLBACK）",
        False,
    )


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════


def check_geometry_consistency(
    layout: dict[str, Any],
    generated: Image.Image | None,
    *,
    enabled: bool | None = None,
    threshold: float | None = None,
) -> GeoCheck:
    """
    比对"户型渲染出来的边缘"与"生成图的边缘"，给出该不该放热区。

    ⚠️ **开关判断在最前面。** `enabled=False` 时连图都不碰 —— 见模块说明里
    关于 AC-35 的那段。
    """
    from ...core.config import settings

    on = settings.IMAGE_HOTSPOT_ENABLED if enabled is None else bool(enabled)

    if not on:
        return GeoCheck(
            precision="none",
            iou=None,
            threshold=None,
            threshold_source="未使用",
            calibrated=False,
            enabled=False,
            warning="本图不提供热区，材料价格请查看矢量图",
            reason=(
                "AI 图热区默认关闭（ADR-10 / AC-35）：ControlNet 只做结构引导、"
                "不保证几何对齐，且 IoU 阈值尚未标定。"
                "本分支**未计算 IoU**，因此不依赖任何未标定阈值。"
            ),
        )

    thr, source, calibrated = resolve_threshold(threshold)

    if generated is None:
        return GeoCheck(
            precision="none", iou=None, threshold=thr, threshold_source=source,
            calibrated=calibrated, enabled=True,
            warning="生成图不可用，本图不提供热区",
            reason="AI 图缺失，无法做几何一致性检查",
        )

    try:
        iou = geometry_iou(layout, generated)
    except Exception as e:  # noqa: BLE001
        # 自检本身失败时**不给热区**：这是一个"宁可少显示"的判断。
        # 注意方向与 precheck 相反 —— 那里是"检查器坏了不能挡住主流程"，
        # 这里是"检查器坏了就不能声称自己验证过"。两者的正确方向本就不同。
        return GeoCheck(
            precision="none", iou=None, threshold=thr, threshold_source=source,
            calibrated=calibrated, enabled=True,
            warning="几何一致性自检未能完成，本图不提供热区",
            reason=f"检查过程异常（{type(e).__name__}: {e}）",
        )

    if iou < thr:
        return GeoCheck(
            precision="none", iou=iou, threshold=thr, threshold_source=source,
            calibrated=calibrated, enabled=True,
            warning="AI 图与户型结构偏差较大，本图不提供热区，请参考矢量图",
            reason=f"IoU {iou:.3f} < 阈值 {thr:.2f}",
            detail={"compare_size": COMPARE_SIZE, "dilate_radius": DILATE_RADIUS},
        )

    warning = "热区为房间级近似，仅供参考"
    if not calibrated:
        warning += "（一致性阈值尚未标定，判定结果仅供参考）"

    return GeoCheck(
        precision=AI_MAX_PRECISION, iou=iou, threshold=thr,
        threshold_source=source, calibrated=calibrated, enabled=True,
        warning=warning,
        reason=f"IoU {iou:.3f} ≥ 阈值 {thr:.2f}",
        detail={"compare_size": COMPARE_SIZE, "dilate_radius": DILATE_RADIUS},
    )


# ══════════════════════════════════════════════════════════════════
# 计算
# ══════════════════════════════════════════════════════════════════


def geometry_iou(layout: dict[str, Any], generated: Image.Image,
                 *, size: int = COMPARE_SIZE) -> float:
    """
    两边都先转成边缘图，再算 IoU。

    ⚠️ **不能拿房间 bbox 直接和生成图求 IoU。** 一个是"区域"、一个是"像素"，
    量纲不同，算出来的数字没有任何含义 —— 需求文档 2.2.6 专门提醒了这一点。
    必须两边都是**边缘图**，IoU 才有定义。

    参考侧用的是 `render_conditioning_image` —— 也就是 ControlNet
    **实际看到的那张图**。用它而不是另画一张，比对才是同口径的。
    """
    from ..image.base import ImageProvider

    ref = ImageProvider.render_conditioning_image(layout, size=size)
    return edge_iou(ref, generated, size=size)


def edge_iou(reference: Image.Image, generated: Image.Image,
             *, size: int = COMPARE_SIZE) -> float:
    """
    两张图 → 边缘 IoU。

    与 `geometry_iou` 分开是为了**标定**：标定时手上有的是
    「生成图 + 当时那张 conditioning 图」的成对文件，
    重新从 layout 渲染一张参考图反而不如直接用现场那张忠实。
    """
    a = _dilate(_edge_mask(reference, size), DILATE_RADIUS)
    b = _dilate(_edge_mask(generated, size), DILATE_RADIUS)

    inter = _count(ImageChops.logical_and(a, b))
    union = _count(ImageChops.logical_or(a, b))
    if union == 0:
        # 两张图都没检出任何边缘。不是"完全一致"，是"没有可比的信息"。
        return 0.0
    return inter / union


def _edge_mask(img: Image.Image, size: int) -> Image.Image:
    """图 → 二值边缘图（mode `"1"`）。只用 Pillow，不依赖 opencv。"""
    gray = img.convert("L")
    if gray.size != (size, size):
        gray = gray.resize((size, size))
    edges = gray.filter(ImageFilter.FIND_EDGES)
    lut = [255 if v > EDGE_THRESHOLD else 0 for v in range(256)]
    return edges.point(lut, mode="1")


def _dilate(mask: Image.Image, radius: int) -> Image.Image:
    """
    膨胀，容忍 1–2px 的抖动。

    ⚠️ 不膨胀的话，两条**视觉上完全重合**、但像素网格差一位的线，
    IoU 会算成 0 —— 而它们显然是"对齐"的。膨胀把"像素级相等"放宽成
    "邻域内存在"，这才是人眼判断几何对齐时实际用的标准。
    """
    if radius <= 0:
        return mask
    size = 2 * radius + 1
    return mask.convert("L").filter(ImageFilter.MaxFilter(size)).point(
        [255 if v > 127 else 0 for v in range(256)], mode="1"
    )


def _count(mask: Image.Image) -> int:
    """mode `"1"` 图里白像素的个数。"""
    return mask.convert("L").histogram()[255]


# ══════════════════════════════════════════════════════════════════
# 标定
# ══════════════════════════════════════════════════════════════════


@dataclass
class CalibrationReport:
    """阈值标定结果。**用来把 `IMAGE_HOTSPOT_IOU_THRESHOLD` 填上。**"""

    samples: int
    ious: list[float]

    @property
    def minimum(self) -> float:
        return min(self.ious) if self.ious else 0.0

    @property
    def maximum(self) -> float:
        return max(self.ious) if self.ious else 0.0

    @property
    def median(self) -> float:
        if not self.ious:
            return 0.0
        s = sorted(self.ious)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    def suggested_threshold(self, *, keep_ratio: float = 0.8) -> float:
        """
        建议阈值 = 分位数。

        **取分位数而不是最小值**：最小值是"最差的那一张"，照它设阈值等于
        要求所有图都达到最差水平。取 20% 分位则是"接受 80% 的样本"，
        与"宁可少显示几个热区、也不要放错"的取向一致。
        """
        if not self.ious:
            return 0.0
        s = sorted(self.ious)
        idx = max(0, min(len(s) - 1, int(len(s) * (1 - keep_ratio))))
        return s[idx]

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "min": round(self.minimum, 4),
            "median": round(self.median, 4),
            "max": round(self.maximum, 4),
            "suggested_threshold": round(self.suggested_threshold(), 4),
            "note": (
                "⚠️ 标定量本身受 EDGE_THRESHOLD / DILATE_RADIUS 影响，"
                "换参数必须重新标定。样本数少于 20 张时结论不可用。"
            ),
        }


def calibration_report(pairs: Iterable[tuple[dict[str, Any], Image.Image]]
                       ) -> CalibrationReport:
    """
    ADR-10 的标定规程：给一批 `(layout, 生成图)` 样本，量出 IoU 分布。

    用**未标定的样本**（还没人工判断过好坏的那批），跑出来的分布才知道
    真实的 IoU 落在哪个区间。需求文档 0.2 坑 19 的担心是它可能整体低于 0.70，
    这个函数就是用来证伪或证实那句话的 —— 而不是继续拍一个阈值。
    """
    ious: list[float] = []
    for layout, img in pairs:
        try:
            ious.append(geometry_iou(layout, img))
        except Exception:  # noqa: BLE001 —— 单个样本失败不该毁掉整批
            continue
    return CalibrationReport(samples=len(ious), ious=ious)


__all__ = [
    "GeoCheck", "check_geometry_consistency", "geometry_iou",
    "resolve_threshold", "calibration_report", "CalibrationReport",
    "COMPARE_SIZE", "EDGE_THRESHOLD", "DILATE_RADIUS",
]
