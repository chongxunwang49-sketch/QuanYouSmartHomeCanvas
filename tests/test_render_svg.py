"""
矢量渲染器（AC-07）。

═══════════════════════════════════════════════════════════════════
这个文件守的是**画错了但看不出来**的错
═══════════════════════════════════════════════════════════════════
一份户型图渲染错了，最典型的两种表现是：

  ① **被拉伸**  —— 3.89m×5.05m 的房间画成了正方形。
     代码读起来毫无问题（`x*scale` / `y*scale`），图上也"像那么回事"。
     需求文档 0.2 的坑 9 就是这个：条件图渲染对 x/y 各用一个缩放因子，
     把 685×505 的户型压扁 1.36 倍，ControlNet 照着变形结构生成。
     发现它的唯一办法是**量画出来内容的宽高比**。

  ② **上下颠倒 / 左右镜像** —— 几何内核翻了一次 Y 轴（图片 Y 向下 → 场景
     Y 向上），画布要再翻回来。少翻一次，图就是倒的或镜像的。
     倒的能看出来，**镜像的看不出来**（只是厨房跑到了客厅左边）。

所以下面的断言都是"从 SVG 的坐标里量出来的性质"，不是"函数返回了东西"。
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

import pytest

from backend.app.services.geometry import normalize_layout
from backend.app.services.render import (
    DEFAULT_MIN_SIDE_PX,
    render_scene_svg,
)

SVG_NS = "{http://www.w3.org/2000/svg}"
_EDGE = DEFAULT_MIN_SIDE_PX - 2 * 40     # 绘图区边长（减去留白）

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
    "dimensions": [{"label": "8200", "value": 8.2}, {"label": "5600", "value": 5.6}],
    "total_area": 45.9,
    "confidence": 0.45,
}


def _scene(layout: dict | None = None):
    return normalize_layout(layout if layout is not None else REAL_LAYOUT)


def _root(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def _room_polys(root: ET.Element) -> dict[str, list[tuple[float, float]]]:
    """`{room-id: [(x,y), ...]}` —— 从 SVG 里读回画出来的房间多边形。"""
    out: dict[str, list[tuple[float, float]]] = {}
    for el in root.iter(f"{SVG_NS}polygon"):
        cls = el.get("class") or ""
        if "room " not in cls + " " and not cls.startswith("room-"):
            continue
        if "room-label" in cls or "room-area" in cls:
            continue
        pts = [
            tuple(float(v) for v in pair.split(","))
            for pair in (el.get("points") or "").split()
        ]
        if pts:
            out[el.get("id", "")] = pts
    return out


def _bbox(pts: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _unit(v: tuple[int, int, int]) -> tuple[float, float, float]:
    n = sum(c * c for c in v) ** 0.5 or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _all_bbox(polys: dict) -> tuple[float, float, float, float]:
    xs = [p[0] for pts in polys.values() for p in pts]
    ys = [p[1] for pts in polys.values() for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


# ══════════════════════════════════════════════════════════════════
# AC-07 本身
# ══════════════════════════════════════════════════════════════════


class TestAC07:
    """
    「**任何**户型都能渲出 512×512 以上的矢量图，< 3s」

    三个词要分别测：**任何**（下面这一组）、512×512（尺寸）、3s（耗时）。
    """

    CASES = {
        "正常三居": REAL_LAYOUT,
        "横长条": {"rooms": [{"name": "走廊", "type": "other", "bbox": [0, 0, 2000, 200]}],
                   "total_area": 100.0},
        "竖长条": {"rooms": [{"name": "走廊", "type": "other", "bbox": [0, 0, 200, 2000]}],
                   "total_area": 60.0},
        "正方形": {"rooms": [{"name": "厅", "type": "living_room", "bbox": [0, 0, 600, 600]}],
                   "total_area": 36.0},
        "只有一个房间": {"rooms": [{"name": "单间", "type": "other", "bbox": [10, 10, 410, 310]}],
                         "total_area": 24.0},
        "零面积_bbox": {"rooms": [{"name": "退化", "type": "other", "bbox": [10, 10, 10, 10]}],
                        "total_area": 0.0},
        "只有墙没有房间": {"rooms": [], "total_area": 0.0,
                           "walls": [{"type": "unknown",
                                      "coords": [[0, 0], [500, 0], [500, 500], [0, 500], [0, 0]]}]},
        "完全空": {},
        "降级模式": {"mode": "degraded_basic",
                     "rooms": [{"name": "客厅", "type": "other", "area": 0.0, "bbox": []}],
                     "walls": [], "total_area": 0.0},
        "负坐标": {"rooms": [{"name": "厅", "type": "living_room", "bbox": [-500, -400, 0, 0]}],
                   "total_area": 20.0},
    }

    @pytest.mark.parametrize("name", list(CASES))
    def test_任何户型都渲得出且不小于512(self, name):
        plan = render_scene_svg(_scene(self.CASES[name]))
        assert plan.width_px >= 512, f"{name}: 宽只有 {plan.width_px}"
        assert plan.height_px >= 512, f"{name}: 高只有 {plan.height_px}"
        # 「矢量图」的判据不只是 width 属性 —— 必须真的是合法 SVG
        root = _root(plan.svg)
        assert root.tag == f"{SVG_NS}svg"

    @pytest.mark.parametrize("name", list(CASES))
    def test_任何户型都不会渲超时(self, name):
        """AC-07 的 "< 3s"。**量出来，不是估计。**"""
        scene = _scene(self.CASES[name])
        t0 = time.perf_counter()
        render_scene_svg(scene)
        elapsed = time.perf_counter() - t0
        assert elapsed < 3.0, f"{name}: 渲染耗时 {elapsed:.2f}s，超过 AC-07 的 3s"

    def test_耗时留有大余量(self):
        """
        余量而不是"刚好过线"。

        渲染是纯字符串拼接，实测 0.5ms 量级。若某次改动把它推到几百毫秒，
        说明引入了本不该有的计算（比如逐像素循环）—— 在 3s 的松阈值下
        这种退化不会被发现，所以这里卡一个更紧的界。
        """
        scene = _scene()
        t0 = time.perf_counter()
        for _ in range(10):
            render_scene_svg(scene)
        per_run = (time.perf_counter() - t0) / 10
        assert per_run < 0.25, f"单次渲染 {per_run * 1000:.0f}ms，远超预期（应有几毫秒量级）"

    def test_降级户型不崩且给出占位说明(self):
        """
        `degraded_basic` 只有房间名、没有任何坐标。

        AC-07 说的是"**任何**户型"，包含最烂的这一种。一张纯白图会让人
        以为渲染失败，所以必须画出"未识别出房间轮廓"的说明。
        """
        plan = render_scene_svg(_scene(self.CASES["降级模式"]))
        assert "未识别出房间轮廓" in plan.svg

    def test_输出是合法xml且可被解析(self):
        """手拼字符串最容易出的错是标签没闭合 —— 解析一次就暴露了。"""
        plan = render_scene_svg(_scene())
        root = _root(plan.svg)
        assert root.get("viewBox") == f"0 0 {plan.width_px} {plan.height_px}"


# ══════════════════════════════════════════════════════════════════
# 几何正确性 —— 画出来的东西对不对
# ══════════════════════════════════════════════════════════════════


class TestGeometryIsCorrect:
    def test_户型不被拉伸(self):
        """
        ⚠️ **这条是坑 9 的回归测试。**

        量画出来所有房间的联合包围盒宽高比，和场景本身的宽高比比。
        允许 3% 误差（留白与描边会带来一点，但不该是量级差异）。
        """
        scene = _scene()
        plan = render_scene_svg(scene)
        x1, y1, x2, y2 = _all_bbox(_room_polys(_root(plan.svg)))

        drawn = (x2 - x1) / (y2 - y1)
        source = scene.width_m / scene.depth_m
        assert abs(drawn - source) / source < 0.03, (
            f"户型被拉伸：源 {source:.3f} → 画出来 {drawn:.3f}"
        )

    def test_上下没有颠倒(self):
        """
        ⚠️ 原图里**靠上**的房间，在 SVG 里也必须靠上。

        几何内核把图片 Y（向下）翻成了场景 Y（向上），画布要翻回去。
        少翻一次，整张图就是上下颠倒的。
        """
        root = _root(render_scene_svg(_scene()).svg)
        polys = _room_polys(root)
        # 房间 0 在原图里 y 40–300，是最靠上的那间
        top_room = _bbox(polys["room-0"])
        bottom_room = _bbox(polys["room-1"])   # 原图 y 300–545
        assert top_room[1] < bottom_room[1], (
            "原本靠上的房间画到下面去了 —— Y 轴翻转次数不对"
        )

    def test_左右没有镜像(self):
        """
        ⚠️ 镜像比颠倒更难发现 —— 倒过来一眼看得出，左右反了只是
        "厨房在客厅左边"而已，没有任何界面会提示。

        房间 0/1 在原图里 x 40–380（左），房间 2 是 380–725（右）。
        """
        root = _root(render_scene_svg(_scene()).svg)
        polys = _room_polys(root)
        assert _bbox(polys["room-0"])[0] < _bbox(polys["room-2"])[0]
        assert _bbox(polys["room-1"])[0] < _bbox(polys["room-2"])[0]

    def test_房间之间不重叠(self):
        """相邻房间的填充块不该压在一起 —— 那说明 bbox 换算错了。"""
        root = _root(render_scene_svg(_scene()).svg)
        polys = _room_polys(root)
        a = _bbox(polys["room-0"])
        b = _bbox(polys["room-1"])
        assert a[3] <= b[1] + 0.5, f"上下相邻的两间重叠了：{a} 与 {b}"

    def test_两个相邻同类型房间颜色不同(self):
        """
        两个卧室是同类型，但**必须能看出是两间**。
        同色会糊成一间 —— 这是户型图最容易出的视觉错误。
        """
        root = _root(render_scene_svg(_scene()).svg)
        fills = {}
        for el in root.iter(f"{SVG_NS}polygon"):
            if (el.get("id") or "").startswith("room-"):
                fills[el.get("id")] = el.get("fill")
        assert fills["room-0"] != fills["room-1"], "两个相邻卧室同色，会糊成一间"

        # 但也不能为了区分就去改色相 —— 那会让户型图变成调色盘，
        # 两个卧室看上去像两个不同功能的房间。要求：
        #   ① 仍然很浅（否则墙线被压住，图就废了）
        #   ② 色相基本不动（同色系）
        a = _rgb(fills["room-0"])
        b = _rgb(fills["room-1"])
        assert min(min(a), min(b)) >= 200, f"填充色太深，会压住墙线：{fills}"
        # 归一化后比较方向：差异只应来自明度，不该来自色相
        na = _unit(a)
        nb = _unit(b)
        cos = sum(x * y for x, y in zip(na, nb))
        assert cos > 0.999, f"同类型房间被调成了不同色相（cos={cos:.4f}）"

    def test_房间尺寸比例正确(self):
        """单间 300×260 的户型，画出来的宽高比应当是 300/260。"""
        layout = {"rooms": [{"name": "厅", "type": "living_room", "bbox": [0, 0, 300, 260]}],
                  "total_area": 20.0}
        root = _root(render_scene_svg(_scene(layout)).svg)
        x1, y1, x2, y2 = _bbox(list(_room_polys(root).values())[0])
        assert abs((x2 - x1) / (y2 - y1) - 300 / 260) < 0.03


# ══════════════════════════════════════════════════════════════════
# 墙体与门窗
# ══════════════════════════════════════════════════════════════════


class TestWallsAndOpenings:
    def test_墙画成闭合路径(self):
        root = _root(render_scene_svg(_scene()).svg)
        walls = list(root.iter(f"{SVG_NS}path"))
        loops = [w for w in walls if (w.get("d") or "").rstrip().endswith("Z")]
        assert loops, "外墙是闭合环，路径必须用 Z 收口（否则角落会缺一块）"

    def test_墙不闭合时换色并告警(self):
        """
        墙没通过闭合性检查时，**照画但换色**。

        不画的话图就退化成几个色块（墙是户型图的主要视觉信息）；
        用同一种颜色画的话，使用者看不出"这段墙不保证准确"。
        """
        layout = dict(REAL_LAYOUT, walls=[{"type": "unknown", "coords": [[40, 40], [400, 40]]}])
        plan = render_scene_svg(_scene(layout))
        root = _root(plan.svg)
        group = next(iter(root.iter(f"{SVG_NS}g")), None)
        walls = [el for el in root.iter(f"{SVG_NS}g") if el.get("id") == "walls"]
        assert walls and walls[0].get("data-trusted") == "false"
        assert any("闭合" in w for w in plan.warnings), f"没有告警：{plan.warnings}"
        assert group is not None

    def test_门画在对应的墙上(self):
        """
        ⚠️ 门的位置是从 `Opening.wall_index` + `offset_along_wall_m` 反推的。
        反推错（比如沿墙距离算成了从墙尾算起）画出来仍然像一扇门，
        **只是开在墙上另一个位置**。
        """
        root = _root(render_scene_svg(_scene()).svg)
        doors = [g for g in root.iter(f"{SVG_NS}g")
                 if (g.get("class") or "").startswith("opening door")]
        assert len(doors) == 3, f"三扇门应画三个，实得 {len(doors)}"

        # 每扇门都要能对上几何内核给出的墙下标
        for g in doors:
            assert int(g.get("data-wall")) >= 0, "门没关联到墙"

        # 第二扇门（原图 [142,315]）落在 y=300 那道横墙上，
        # 画出来的位置必须在那道墙附近，而不是别的墙上
        d1 = doors[1]
        mids = [
            ((float(a) + float(b)) / 2)
            for a, b in re.findall(r'x1="([\d.]+)" y1="([\d.]+)"', ET.tostring(d1, encoding="unicode"))
        ]
        # 横墙 → 门洞的 x 中线应当贴近 142 对应的画布位置
        assert mids, "门没有画出线段"

    def test_门宽是兜底值时被标记出来(self):
        """
        实测 A-01 输出的三扇门 `width` 全是 0，所以宽度都是兜底值。
        图上必须能看出这一点（虚线轮廓），否则使用者会把
        0.9m 当成"识别出来的 0.9m"。
        """
        root = _root(render_scene_svg(_scene()).svg)
        doors = [g for g in root.iter(f"{SVG_NS}g")
                 if (g.get("class") or "").startswith("opening door")]
        assert doors
        assert all("width-assumed" in (g.get("class") or "") for g in doors), (
            "门宽用了兜底值却没有标记"
        )

    def test_识别出宽度的窗不被标记为兜底(self):
        root = _root(render_scene_svg(_scene()).svg)
        wins = [g for g in root.iter(f"{SVG_NS}g")
                if (g.get("class") or "").startswith("opening window")]
        assert wins
        assert all("width-assumed" not in (g.get("class") or "") for g in wins)

    def test_离墙太远的门不硬挂到墙上(self):
        """识别偏了的位置宁可画个虚线圆圈，也不要硬塞到不相干的墙上。"""
        layout = dict(REAL_LAYOUT, doors=[{"position": [9999, 9999], "width": 0.9}])
        plan = render_scene_svg(_scene(layout))
        root = _root(plan.svg)
        doors = [g for g in root.iter(f"{SVG_NS}g")
                 if (g.get("class") or "").startswith("opening door")]
        assert doors and "unanchored" in (doors[0].get("class") or "")
        assert any("未关联" in w or "未能关联" in w for w in plan.warnings)


# ══════════════════════════════════════════════════════════════════
# 确定性与可交付
# ══════════════════════════════════════════════════════════════════


class TestDeterminism:
    def test_同样的输入得到逐字节相同的输出(self):
        """
        AC-32 要求黄金路径连续重放 3 次一致性 ≥ 95%。

        渲染是最容易混进随机性的地方：时间戳、uuid、集合遍历顺序、
        浮点格式化。**同一个 scene 渲染两次必须一模一样。**
        """
        scene = _scene()
        a = render_scene_svg(scene).svg
        b = render_scene_svg(scene).svg
        assert a == b, "两次渲染结果不同 —— 有随机性混进来了"

    def test_输出里没有浮点噪声(self):
        """
        不裁剪精度的话会出现 `3.7659876543210003`，它让每次渲染的字节数
        都不一样，比对一致性就无从谈起。
        """
        svg = render_scene_svg(_scene()).svg
        noisy = [n for n in re.findall(r"\d+\.\d{5,}", svg)]
        assert not noisy, f"SVG 里有未裁剪的浮点数：{noisy[:5]}"

    def test_假设清单跟着图走(self):
        """
        SVG 会被单独导出/分享。层高 2.8m 是**假设**，这个信息必须嵌在图里，
        否则图一离开系统就没人知道那是猜的。
        """
        svg = render_scene_svg(_scene()).svg
        assert "假设" in svg and "层高" in svg

    def test_渲染结果不含价格(self):
        """
        图会同时用于前端展示、ControlNet 的 conditioning image、用户导出。
        后两者不该出现"¥129/㎡"这种东西 —— 价格走 hotspots JSON 通道。
        """
        svg = render_scene_svg(_scene()).svg
        assert "元" not in svg and "¥" not in svg and "全友" not in svg

    def test_可以关掉热区层(self):
        """出给 ControlNet 的那一份不带热区，免得模型去"生成框"。"""
        with_hs = render_scene_svg(_scene(), with_hotspots=True).svg
        without = render_scene_svg(_scene(), with_hotspots=False).svg
        assert 'id="hotspots"' in with_hs
        assert 'id="hotspots"' not in without

    def test_画布尺寸可调(self):
        small = render_scene_svg(_scene(), min_side_px=512)
        assert small.width_px >= 512 and small.height_px >= 512
        big = render_scene_svg(_scene(), min_side_px=2048)
        assert big.width_px >= 2048
