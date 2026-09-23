"""
ControlNet 条件图渲染。

═══════════════════════════════════════════════════════════════════
为什么这里要量像素而不是"看一眼觉得对"
═══════════════════════════════════════════════════════════════════
这个函数出过一次**看起来没问题但其实错了**的 bug：它对 x / y 各用一个
缩放因子，等于把任意户型强行拉成正方形。代码读起来很合理
（`x1 / max_x`、`y1 / max_y` 各自归一化），画出来也是一张像模像样的
户型图 —— 只有**量一下画出来内容的宽高比**才发现 685×505 的户型被压扁了。

所以这个文件的断言都是"从像素里量出来的性质"，不是"函数返回了东西"。
"""

from __future__ import annotations

import pytest

from backend.app.services.image.base import ImageProvider

render = ImageProvider.render_conditioning_image


def _content_bbox(img, *, bg=(255, 255, 255), tol: int = 12):
    """
    量出画面里**非背景内容**的包围盒。

    `tol` 是容差：房间填充色是浅色（如 #FFF0E1），跟纯白只差几个色阶，
    严格相等会量不到任何东西。用一个小的曼哈顿距离阈值把它们算作内容。
    """
    px = img.convert("RGB").load()
    w, h = img.size
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) > tol:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


class TestAspectRatioPreserved:
    def test_宽户型不被压扁(self):
        """
        ⚠️ 这条是这个文件存在的理由。

        685×505（1.36:1）的户型画出来必须还是 1.36:1 —— 而不是被拉成 1:1。
        """
        layout = {"rooms": [
            {"bbox": [40, 40, 380, 300]},
            {"bbox": [40, 300, 380, 545]},
            {"bbox": [380, 40, 725, 545]},
        ]}
        img = render(layout, size=512)
        box = _content_bbox(img)
        assert box is not None, "画面上没有内容"

        x1, y1, x2, y2 = box
        drawn = (x2 - x1 + 1) / (y2 - y1 + 1)
        src = 685 / 505
        assert abs(drawn - src) / src < 0.06, (
            f"宽高比被改变了：源 {src:.2f} → 画出来 {drawn:.2f}。"
            f"条件图被拉伸会让 ControlNet 照着变形的结构生成"
        )

    def test_高户型不被压扁(self):
        """反向：竖长户型同样是形状信息，不该被拉成方的。"""
        layout = {"rooms": [{"bbox": [0, 0, 300, 900]}]}
        img = render(layout, size=512)
        box = _content_bbox(img)
        assert box is not None
        x1, y1, x2, y2 = box
        drawn = (x2 - x1 + 1) / (y2 - y1 + 1)
        src = 300 / 900
        assert abs(drawn - src) / src < 0.06, f"源 {src:.2f} → 画出 {drawn:.2f}"

    def test_正方形户型仍然撑满(self):
        """等比缩放的另一面：正方形不该被缩得比需要的更小。"""
        layout = {"rooms": [{"bbox": [0, 0, 600, 600]}]}
        img = render(layout, size=512)
        box = _content_bbox(img)
        assert box is not None
        x1, y1, x2, y2 = box
        # 长边应当占满约 90%
        assert (x2 - x1 + 1) / 512 > 0.85


class TestOffsetHandling:
    def test_户型不在原点时内容居中(self):
        """
        初版用 `max(bbox[2])` 当分母而不是整体包围盒 —— 户型从 [40,40] 开始时，
        内容整体偏移、边上留出一条空白带。

        这里断言的是"内容居中"，与户型在原图中的绝对位置无关。
        """
        layout = {"rooms": [{"bbox": [400, 300, 900, 700]}]}
        img = render(layout, size=512)
        x1, y1, x2, y2 = _content_bbox(img)  # type: ignore[misc]

        left_margin, right_margin = x1, 512 - x2
        top_margin, bottom_margin = y1, 512 - y2
        assert abs(left_margin - right_margin) <= 2, (
            f"水平没居中：左 {left_margin} / 右 {right_margin}"
        )
        assert abs(top_margin - bottom_margin) <= 2, (
            f"垂直没居中：上 {top_margin} / 下 {bottom_margin}"
        )

    def test_平移户型不改变画出来的形状(self):
        """同一个户型整体平移，画出来应当**完全一样**（像素级）。"""
        a = {"rooms": [{"bbox": [0, 0, 400, 300]}]}
        b = {"rooms": [{"bbox": [777, 555, 1177, 855]}]}
        ia, ib = render(a, size=256), render(b, size=256)
        assert list(ia.getdata()) == list(ib.getdata()), (
            "平移改变了输出 —— 说明绝对坐标泄漏进了渲染，户型位置会影响生成结果"
        )


class TestEdgeCases:
    def test_没有_bbox_时给一个兜底矩形(self):
        img = render({"rooms": []}, size=256)
        box = _content_bbox(img)
        assert box is not None, "没有 bbox 时也该有内容，否则 ControlNet 拿不到输入"

    def test_退化_bbox_不崩(self):
        """面积为 0 的 bbox（模型偶尔会返回）不该让渲染炸掉。"""
        img = render({"rooms": [{"bbox": [10, 10, 10, 10]}]}, size=128)
        assert img.size == (128, 128)

    def test_缺少_rooms_键不崩(self):
        assert render({}, size=128).size == (128, 128)

    def test_输出尺寸与白底(self):
        img = render({"rooms": [{"bbox": [0, 0, 100, 100]}]}, size=512)
        assert img.size == (512, 512)
        assert img.getpixel((2, 2))[0] > 240, "角落应当是白底"


class TestRealLayoutRegression:
    """
    用**真实解析出来的**户型做回归。

    合成用例容易被"刚好对上"的参数骗过；这组数据是从一次真实解析里抄下来的
    （3 房间 / 3 段墙 / 45.9㎡），形状是实际会遇到的。
    """

    REAL = {
        "rooms": [
            {"name": "卧室", "bbox": [40, 40, 380, 300]},
            {"name": "卧室", "bbox": [40, 300, 380, 545]},
            {"name": "客厅", "bbox": [380, 40, 725, 545]},
        ],
        "total_area": 45.9,
    }

    def test_真实户型比例正确(self):
        img = render(self.REAL, size=512)
        x1, y1, x2, y2 = _content_bbox(img)  # type: ignore[misc]
        assert 1.25 < (x2 - x1 + 1) / (y2 - y1 + 1) < 1.45, (
            "真实三居室（685×505 ≈ 1.36:1）被画歪了"
        )

    def test_每个房间都画出来了(self):
        """三个房间应当有三种不同的填充色 —— 相邻房间同色会糊成一片。"""
        img = render(self.REAL, size=512)
        colors = {img.getpixel(p) for p in ((150, 150), (150, 400), (450, 300))}
        assert len(colors) == 3, f"三个房间应当有 3 种填充色，实际 {len(colors)}"
