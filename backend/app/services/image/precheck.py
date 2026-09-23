"""
图片质量预检 —— 在图片进入多模态模型**之前**把它筛掉。

═══════════════════════════════════════════════════════════════════
为什么必须有这一步（AC-27）
═══════════════════════════════════════════════════════════════════
需求文档 2.2.4 的阶段表里有一行 `prechecking` ——「正在检查图片质量…」，
触发时机写的是「本地预检（**零 Token**）」。

**但在这之前，代码里根本没有这一步。** 任务启动时无条件写一个
`prechecking` 阶段，然后直接进模型调用。也就是说：

    界面上显示「正在检查图片质量…」的那一秒里，什么都没发生。

这跟项目自己的纪律是冲突的 —— AC-17 的原话是「**静默降级 = 欺骗用户**」。
UI 声称做了一件没做的事，是同一类问题，而且更隐蔽：降级至少还发生了，
这个连发生都没发生。

所以这个模块把那个阶段**做成真的**。它同时兑现 AC-27：
不合格的图片在进入 LLM 前被拒，Token 消耗为 0。

═══════════════════════════════════════════════════════════════════
两类判定，以及为什么必须分开
═══════════════════════════════════════════════════════════════════
    **拒收**（rejections）—— 客观、可复现、我能为它负责
    **提醒**（warnings）  —— 启发式阈值，**尚未标定**，不阻断

这个区分是刻意的。分辨率下限、长宽比、是否空白，这些都有明确依据，
拒了就是拒了。而"模糊度"是个统计量，阈值定多少要拿真实户型图标定 ——
**本项目没有做过这个标定**（需要一批标注过的模糊/清晰样本）。

一个没标定的阈值如果用来拒收，就会把好图误杀，而用户看到的理由是
「图片模糊」—— 他会以为是自己拍得不好，反复重拍，永远不知道是阈值错了。

所以模糊度只**提醒**，不拒收。宁可放一张略糊的图进去（代价是模型识别
质量下降，且这件事本身会体现在 `confidence` 上），也不要凭一个拍脑袋的
数字把好图挡在门外。
"""

from __future__ import annotations

import base64
import binascii
import io
import re
from dataclasses import dataclass, field
from typing import Any

# ── 阈值 ────────────────────────────────────────────────────────────

#: 短边下限（像素）。
#: 依据：户型图的信息密度在墙线与尺寸标注文字上。1200px 宽的图缩到 400px
#: 后，标注文字大约只剩 4–6 px 高 —— 那是读不出来的量级。
#: 这个数字是**几何推算**，不是标定值，但方向明确：低于它必然不可读。
MIN_SHORT_SIDE = 400

#: 长边上限。超过它，下游视觉模型会自行缩图（各家的上限多在 2048–4096），
#: 上传只是白耗带宽与一次编码。**不拒收**，只提醒 —— 高分辨率扫描件是好图，
#: 不是"不合格图片"。
MAX_LONG_SIDE = 8000

#: 极端长宽比。户型图接近矩形；超过 5:1 的几乎都是截图拼贴或手机长截图。
MAX_ASPECT = 5.0

#: 模糊度（Laplacian 方差）提醒阈值。
#:
#: ⚠️ **未标定。** M0 出图基准里那张家用户型图实测 412.7，据此往下留了
#: 很大余量取 60。它只用于提醒，不参与拒收 —— 理由见模块说明。
#: 另外：户型图是线稿，边缘本来就多，这个指标对线稿的区分力弱于对实景照片，
#: 真要用它做门禁，得先拿一批真实户型图标定。
BLUR_WARN_THRESHOLD = 60.0

#: 近乎纯色的判定：像素直方图超过这个比例的像素落在同一个窄色带里。
#: 用来抓"传了一张白纸/纯黑图"这种明显无内容的情况。
BLANK_RATIO = 0.995


@dataclass
class PrecheckResult:
    """预检结果。可直接塞进 state / 接口响应。"""

    ok: bool
    width: int = 0
    height: int = 0
    #: Laplacian 方差。越大越清晰。未能计算时为 None。
    blur_score: float | None = None
    #: 硬性不合格项。非空即 ok=False。
    rejections: list[str] = field(default_factory=list)
    #: 建议项。不阻断流程。
    warnings: list[str] = field(default_factory=list)
    #: 给人看的一句话结论
    advice: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "width": self.width,
            "height": self.height,
            "blur_score": self.blur_score,
            "rejections": list(self.rejections),
            "warnings": list(self.warnings),
            "advice": self.advice,
        }


# ── 输入解码 ────────────────────────────────────────────────────────


def decode_input(ref: str) -> bytes:
    """
    把 `image_ref` 还原成字节。接受 data URI 或裸 base64。

    与 `BaseAgent._image_from_state` 的约定一致 —— 两处必须接受同样的形式，
    否则会出现"预检过了但解析时说不认识"这种自相矛盾。
    """
    if ref.startswith("data:"):
        m = re.match(r"data:[^;]+;base64,(?P<data>.+)", ref, re.S)
        if not m:
            raise ValueError("不是合法的 data URI")
        ref = m.group("data")
    try:
        return base64.b64decode(ref, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"base64 解码失败：{e}") from e


# ── 主流程 ──────────────────────────────────────────────────────────


def precheck_image(data: bytes) -> PrecheckResult:
    """
    对图片做本地质量检查。**不调模型、不发网络请求。**

    解析不了的时候**不抛异常**，而是返回 `ok=True` 加一条提醒 ——
    预检自己坏掉不该把用户的解析请求也一起弄停。这是刻意的：
    一个质检环节失败了却让主流程挂掉，比没有质检更糟。
    """
    rejections: list[str] = []
    warnings: list[str] = []

    try:
        import numpy as np
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        return PrecheckResult(
            ok=True,
            warnings=[f"图像库不可用，已跳过预检（{e}）"],
            advice="未做图片质量预检。",
        )

    # ── 1. 能不能解码 ──
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001 —— PIL 抛的异常类型很杂
        return PrecheckResult(
            ok=False,
            rejections=[f"无法解码为图片：{type(e).__name__}"],
            advice="这个文件不是有效的图片，或者格式不受支持。请上传 PNG / JPG / WebP。",
        )

    width, height = img.size
    short_side, long_side = min(width, height), max(width, height)

    # ── 2. 分辨率 ──
    if short_side < MIN_SHORT_SIDE:
        rejections.append(
            f"分辨率过低：{width}×{height}，短边 {short_side}px < 下限 {MIN_SHORT_SIDE}px"
        )
    if long_side > MAX_LONG_SIDE:
        warnings.append(
            f"尺寸偏大：长边 {long_side}px > {MAX_LONG_SIDE}px，模型侧仍会缩图，上传它只是多耗带宽"
        )

    # ── 3. 长宽比 ──
    aspect = long_side / short_side if short_side else 999.0
    if aspect > MAX_ASPECT:
        rejections.append(
            f"长宽比异常：{aspect:.1f}:1，超过 {MAX_ASPECT}:1。这更像是截图拼贴而不是户型图"
        )

    # ── 4. 内容与清晰度 ──
    #
    # ⚠️ 这一段整体包一层保险：**它自己出任何意外都要放行，不能拦图。**
    #
    # 上面那几条（解码、分辨率、长宽比）是本模块**明确负责**的判断，
    # 它们拒收是有依据的。而下面这些是统计计算 —— 一旦 numpy / OpenCV
    # 的某个版本行为变了、或者图有 PIL 能开但 getdata 会炸的怪格式，
    # 那是**我们自己的代码**出了问题，不该由用户的图片来承担后果。
    #
    # 一个质检环节坏掉却让主流程挂掉，比没有质检更糟。
    blur_score: float | None = None
    try:
        # 空图检测（在灰度上做，避免彩色噪声干扰）
        gray = img.convert("L")
        # 缩到长边 512 再统计：全尺寸直方图对一张 8000px 的图要跑几百毫秒，
        # 而"是不是一张白纸"这件事在缩略图上结论完全一样。
        if long_side > 512:
            ratio = 512 / long_side
            gray_small = gray.resize(
                (max(1, int(width * ratio)), max(1, int(height * ratio)))
            )
        else:
            gray_small = gray

        arr = np.asarray(gray_small, dtype=np.uint8)
        hist = np.bincount(arr.ravel(), minlength=256)
        blank_ratio = float(hist.max()) / float(arr.size)
        if blank_ratio >= BLANK_RATIO:
            rejections.append(
                f"画面近乎纯色（{blank_ratio:.1%} 的像素集中在同一灰度），没有可识别的内容"
            )

        # 清晰度：**只提醒，不拒收**（理由见模块说明的阈值一段）
        try:
            import cv2

            blur_score = float(cv2.Laplacian(arr, cv2.CV_64F).var())
            if blur_score < BLUR_WARN_THRESHOLD:
                warnings.append(
                    f"清晰度偏低（Laplacian 方差 {blur_score:.1f} < {BLUR_WARN_THRESHOLD}）。"
                    f"该阈值尚未用真实户型图标定，仅供参考"
                )
        except ImportError:  # pragma: no cover
            warnings.append("未安装 OpenCV，跳过清晰度检测")

    except Exception as e:  # noqa: BLE001 —— 见上：预检自身出错必须放行
        return PrecheckResult(
            ok=True,
            width=width,
            height=height,
            warnings=[
                f"内容分析未能完成（{type(e).__name__}: {e}），已跳过该项检查",
                *warnings,
            ],
            advice="图片尺寸合规；内容分析被跳过，解析仍会继续。",
        )

    # ── 结论 ──
    ok = not rejections
    if ok:
        advice = (
            f"图片可用（{width}×{height}"
            + (f"，清晰度 {blur_score:.0f}" if blur_score is not None else "")
            + "）。"
        )
        if warnings:
            advice += "有 " + str(len(warnings)) + " 条提醒，不影响解析。"
    else:
        advice = "图片未通过质量预检，已在本机拦下，未消耗任何模型调用。" + rejections[0]

    return PrecheckResult(
        ok=ok,
        width=width,
        height=height,
        blur_score=blur_score,
        rejections=rejections,
        warnings=warnings,
        advice=advice,
    )


def precheck_ref(image_ref: str) -> PrecheckResult:
    """从状态的 `image_ref` 直接预检。解码失败也算不合格。"""
    try:
        data = decode_input(image_ref)
    except ValueError as e:
        return PrecheckResult(ok=False, rejections=[str(e)], advice="图片数据无法解码。")
    return precheck_image(data)


__all__ = [
    "PrecheckResult",
    "precheck_image",
    "precheck_ref",
    "decode_input",
    "MIN_SHORT_SIDE",
    "MAX_LONG_SIDE",
    "MAX_ASPECT",
    "BLUR_WARN_THRESHOLD",
]
