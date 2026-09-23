"""
几何一致性自检（AC-28 / AC-35）。

═══════════════════════════════════════════════════════════════════
这里守的是一条**反直觉的规则**：算不出来的时候，结论必须是"不给热区"
═══════════════════════════════════════════════════════════════════
`precheck.py` 里有一处相反的设计：图片预检器自己崩了，要**放行** ——
因为"检查器坏了"不该挡住用户的主流程。

这里必须反过来。自检没跑成，意味着**我们并没有验证过这张图的几何对不对**。
此时放热区，等于把"没验证"说成"已验证"。所以：

    precheck    检查器异常 → ok=True   （宁可放过，不要挡住）
    geo_check   检查器异常 → 不给热区   （宁可少显示，不要骗人）

同一个原则（别让故障造成伤害）在两个场景推出相反的默认值，
所以两个方向都要有测试钉住。

═══════════════════════════════════════════════════════════════════
默认整条路径关闭，而且**连算都不算**
═══════════════════════════════════════════════════════════════════
AC-35 的原文是「`IMAGE_HOTSPOT_ENABLED=false` 时 AI 图无热区，
且**不依赖任何未标定阈值**」。

注意后半句。一个"先算完 IoU、再判断开关"的实现，那个未标定的数字
仍然参与了决策路径 —— 它没有"不依赖"。所以这里把开关放在**最前面**，
并在测试里用 monkeypatch 把 IoU 函数替换成会抛异常的版本：
只要它被调到，测试就会红。
"""

from __future__ import annotations

import random

import pytest
from PIL import Image

from backend.app.core.config import settings
from backend.app.services.render import (
    calibration_report,
    check_geometry_consistency,
    geometry_iou,
    resolve_threshold,
)

LAYOUT = {
    "rooms": [
        {"name": "卧室", "type": "bedroom", "bbox": [40, 40, 380, 300]},
        {"name": "卧室", "type": "bedroom", "bbox": [40, 300, 380, 545]},
        {"name": "客厅", "type": "living_room", "bbox": [380, 40, 725, 545]},
    ],
    "total_area": 45.9,
}


@pytest.fixture
def aligned_image() -> Image.Image:
    """
    "几何完全对齐"的生成图 —— 直接拿 conditioning 图本身当样本。

    这是 IoU 的**上界**：ControlNet 实际看到的就长这样，所以任何真实
    生成图与它的重合度都不可能比这更高。
    """
    from backend.app.services.image.base import ImageProvider

    return ImageProvider.render_conditioning_image(LAYOUT, size=512)


@pytest.fixture
def noise_image() -> Image.Image:
    """几何完全无关的图。IoU 的**下界**。"""
    rng = random.Random(20260923)          # 固定种子，测试可复现
    img = Image.new("RGB", (512, 512))
    img.putdata([
        (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        for _ in range(512 * 512)
    ])
    return img


# ══════════════════════════════════════════════════════════════════
# AC-35：默认关闭，且不依赖未标定阈值
# ══════════════════════════════════════════════════════════════════


class TestDisabledByDefault:
    def test_配置默认就是关的(self):
        assert settings.IMAGE_HOTSPOT_ENABLED is False, (
            "ADR-10 定了默认关闭 —— 阈值没标定之前不能开"
        )

    def test_默认调用不给热区(self, aligned_image):
        """就算给一张**完美对齐**的图，默认配置下也不给热区。"""
        check = check_geometry_consistency(LAYOUT, aligned_image)
        assert check.precision == "none"
        assert check.allow_hotspots is False
        assert check.enabled is False
        assert check.iou is None, "关闭时不该产生 IoU 数字"
        assert check.threshold is None
        assert "矢量图" in check.warning, "必须告诉用户去哪儿看价格"

    def test_关闭时连_iou_都不算(self, aligned_image, monkeypatch):
        """
        ⚠️ **这条是 AC-35 后半句的落地。**

        把 `geometry_iou` 换成会抛异常的版本。如果实现是"先算后判断开关"，
        异常会冒出来（或被 except 吞掉后返回 none，但 `iou` 字段会非空）。
        两条路都堵住：既断言没抛，也断言没调过。
        """
        from backend.app.services.render import geo_check

        called = {"n": 0}

        def spy(*a, **kw):
            called["n"] += 1
            raise AssertionError("关闭状态下不该计算 IoU")

        monkeypatch.setattr(geo_check, "geometry_iou", spy)
        check = check_geometry_consistency(LAYOUT, aligned_image, enabled=False)

        assert called["n"] == 0, "关闭状态下仍然计算了 IoU —— 阈值仍在参与决策"
        assert check.precision == "none" and check.iou is None

    def test_关闭时图上没有热区层(self):
        """配置断言之外，还要确认前端拿到的那份数据里确实没有热区可放。"""
        check = check_geometry_consistency(LAYOUT, None, enabled=False)
        assert check.to_dict()["allow_hotspots"] is False


# ══════════════════════════════════════════════════════════════════
# AC-28：IoU 低于阈值就不给热区
# ══════════════════════════════════════════════════════════════════


class TestThresholdGate:
    def test_iou_计算落在合法区间(self, aligned_image, noise_image):
        a = geometry_iou(LAYOUT, aligned_image)
        b = geometry_iou(LAYOUT, noise_image)
        assert 0.0 <= b <= a <= 1.0, f"对齐 {a:.3f} / 噪声 {b:.3f} 不满足 0≤噪声≤对齐≤1"

    def test_对齐时_iou_明显高于噪声(self, aligned_image, noise_image):
        """
        IoU 得真的**有区分度**，否则阈值卡在哪儿都是拍脑袋。

        这里不写死具体数值（那是标定要测的），只要求差距是量级上的。
        """
        a = geometry_iou(LAYOUT, aligned_image)
        b = geometry_iou(LAYOUT, noise_image)
        assert a > 0.3, f"连完美对齐的图 IoU 都只有 {a:.3f} —— 阈值再低也没意义"
        assert a - b > 0.15, f"对齐 {a:.3f} 与噪声 {b:.3f} 区分度过低"

    def test_几何偏差大的图不给热区(self, noise_image):
        check = check_geometry_consistency(
            LAYOUT, noise_image, enabled=True, threshold=0.5
        )
        assert check.precision == "none"
        assert check.allow_hotspots is False
        assert "不提供热区" in check.warning
        assert check.iou is not None and check.iou < 0.5

    def test_阈值卡在_iou_之上时不给热区(self, aligned_image):
        """把阈值提到 1.01 —— 任何图都过不了，必须判 none。"""
        check = check_geometry_consistency(
            LAYOUT, aligned_image, enabled=True, threshold=1.01
        )
        assert check.precision == "none"

    def test_通过时给_room_level_并带提示(self, aligned_image):
        check = check_geometry_consistency(
            LAYOUT, aligned_image, enabled=True, threshold=0.1
        )
        assert check.precision == "room_level"
        assert check.allow_hotspots is True
        assert "近似" in check.warning, (
            "AC-09 的诚实性原则：room_level 必须说清楚是近似，"
            "不能假装精确到某一件家具"
        )

    def test_ai_图永远不会被判为_exact(self, aligned_image):
        """
        就算这张"生成图"与 conditioning 图逐像素相同，也只能给 room_level。

        AI 图是**生成**出来的，几何只能是近似 —— 这一条不能因为
        IoU 高就被绕过，否则精心设计的诚实性只在分数低时才生效。
        """
        check = check_geometry_consistency(
            LAYOUT, aligned_image, enabled=True, threshold=0.0
        )
        assert check.precision != "exact"
        assert check.precision == "room_level"


# ══════════════════════════════════════════════════════════════════
# 阈值来源
# ══════════════════════════════════════════════════════════════════


class TestThresholdProvenance:
    def test_未标定时如实标记(self):
        """
        配置里 `IMAGE_HOTSPOT_IOU_THRESHOLD` 是 None（待标定），
        此时用兜底值，但**必须标记出来它没标定过**。

        不标记的话，界面上那句"几何一致性已校验"就是假的。
        """
        settings_threshold = settings.IMAGE_HOTSPOT_IOU_THRESHOLD
        if settings_threshold is not None:
            pytest.skip("阈值已标定，未标定分支不适用")

        thr, source, calibrated = resolve_threshold()
        assert thr == settings.IMAGE_HOTSPOT_IOU_FALLBACK
        assert calibrated is False
        assert "未标定" in source

    def test_显式指定时标记为已标定(self):
        thr, source, calibrated = resolve_threshold(0.70)
        assert thr == 0.70 and calibrated is True

    def test_未标定的判定结果带额外提示(self, aligned_image):
        """没标定过就直接给结论，用户有权知道这个结论的成色。"""
        check = check_geometry_consistency(
            LAYOUT, aligned_image, enabled=True, threshold=0.1
        )
        if not check.calibrated:
            assert "未标定" in check.warning
            assert check.threshold_source.startswith("⚠️")


# ══════════════════════════════════════════════════════════════════
# 失败方向：算不出来 → **不给**热区
# ══════════════════════════════════════════════════════════════════


class TestFailsClosed:
    def test_自检异常时不给热区(self, aligned_image, monkeypatch):
        """
        ⚠️ 方向与 `precheck.py` **相反**，见模块说明。

        自检没跑成 = 没有验证过。此时给热区就是把"没验证"说成"已验证"。
        """
        from backend.app.services.render import geo_check

        def boom(*a, **kw):
            raise RuntimeError("模拟自检内部故障")

        monkeypatch.setattr(geo_check, "geometry_iou", boom)
        check = check_geometry_consistency(LAYOUT, aligned_image, enabled=True)

        assert check.precision == "none"
        assert check.iou is None
        assert "未能完成" in check.warning
        assert "RuntimeError" in check.reason, "失败原因要留下来，便于排查"

    def test_生成图缺失时不给热区(self):
        check = check_geometry_consistency(LAYOUT, None, enabled=True)
        assert check.precision == "none" and check.allow_hotspots is False

    def test_空户型也不崩(self):
        check = check_geometry_consistency({}, None, enabled=False)
        assert check.precision == "none"


# ══════════════════════════════════════════════════════════════════
# 标定
# ══════════════════════════════════════════════════════════════════


class TestCalibration:
    def test_标定报告给出分布与建议阈值(self, aligned_image, noise_image):
        """
        需求文档 0.2 的坑 19 自己承认了：0.70 是拍的，而且可能**让所有图
        都不达标**。这个报告就是用来把那个数字换成实测值的。
        """
        report = calibration_report([
            (LAYOUT, aligned_image),
            (LAYOUT, noise_image),
        ])
        d = report.to_dict()
        assert report.samples == 2
        assert d["min"] <= d["suggested_threshold"] <= d["max"]
        assert "标定量本身受" in d["note"], (
            "标定结论依赖 EDGE_THRESHOLD / DILATE_RADIUS，必须写清楚"
        )

    def test_建议阈值取分位而不是最小值(self):
        """
        取最小值 = 要求所有图都达到最差的那张的水平。
        取分位才是"接受大部分样本、放弃最差的一小撮" —— 与
        "宁可少显示几个热区，也不要放错"的取向一致。
        """
        report = calibration_report([])
        report.ious = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        assert report.suggested_threshold(keep_ratio=0.8) > report.minimum
        assert report.median == pytest.approx(0.55)

    def test_空样本不崩(self):
        report = calibration_report([])
        assert report.samples == 0
        assert report.suggested_threshold() == 0.0

    def test_单个样本失败不影响整批(self, monkeypatch):
        """标定要跑几十张图，中间一张坏掉不该让整批作废。"""
        from backend.app.services.render import geo_check

        calls = {"n": 0}
        real = geo_check.geometry_iou

        def flaky(layout, img, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟某一张图损坏")
            return real(layout, img, **kw)

        monkeypatch.setattr(geo_check, "geometry_iou", flaky)
        img = Image.new("RGB", (256, 256), "white")
        report = calibration_report([(LAYOUT, img), (LAYOUT, img)])
        assert report.samples == 1, "坏样本应当被跳过，而不是让整批失败"
