"""
几何内核：像素级户型 JSON → 米制场景。

═══════════════════════════════════════════════════════════════════
这个文件重点守两类东西
═══════════════════════════════════════════════════════════════════
【一】**看不见的错**

Y 轴不翻转时，3D 里的户型是左右镜像的 —— 而镜像的户型**看上去完全正常**，
只是厨房跑到客厅左边去了。没有任何界面元素会提示这件事。只有拿原图
逐间比对，或者像这里一样量坐标，才能发现。

【二】**保守的错**

`_check_walls_closed` 写过两版，两版都把正常户型判成"未闭合"：

  第一版只比端点 → 漏掉 T 型接头（内墙接在外墙中间，不在端点上）
  第二版改判"端点到线段" → 却把闭合环自己的端点也要求接上别的墙

两版的共同点是：**判错的方向都是"更保守"** —— 结果是 3D 漫游一直用着
最差的房间盒体模型，而不报任何错。保守的错误不会崩，只会让功能悄悄变差。
"""

from __future__ import annotations

import copy

import pytest

from backend.app.services.geometry import normalize_layout
from backend.app.services.geometry.normalize import (
    DEFAULT_CEILING_HEIGHT_M,
    derive_scale,
)

# ══════════════════════════════════════════════════════════════════
# 一次真实解析的数据（从线上接口抄下来的，未经修饰）
# ══════════════════════════════════════════════════════════════════

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
        {"position": [367, 197], "width": 0.0, "swing": "unknown"},
        {"position": [142, 315], "width": 0.0, "swing": "unknown"},
        {"position": [400, 407], "width": 0.0, "swing": "unknown"},
    ],
    "windows": [
        {"position": [228, 40], "width": 1.7, "orientation": "unknown"},
        {"position": [575, 545], "width": 1.8, "orientation": "unknown"},
    ],
    "dimensions": [
        {"label": "8200", "value": 8.2, "unit": "mm"},
        {"label": "5600", "value": 5.6, "unit": "mm"},
    ],
    "total_area": 45.9,
    "confidence": 0.45,
}


@pytest.fixture
def scene():
    return normalize_layout(copy.deepcopy(REAL_LAYOUT))


# ══════════════════════════════════════════════════════════════════
# 比例尺
# ══════════════════════════════════════════════════════════════════


class TestScaleDerivation:
    def test_由总面积推导(self):
        """
        px_per_m = sqrt(Σbbox面积_px / total_area_m²)。

        实测这份数据：Σbbox = 345,925 px²，total_area = 45.9 m²
        ⇒ sqrt(345925/45.9) = 86.8
        """
        est = derive_scale(REAL_LAYOUT)
        assert est.source == "derived_from_area"
        assert abs(est.px_per_m - 86.8) < 0.5, f"实得 {est.px_per_m}"

    def test_与尺寸标注交叉校验并如实报出分歧(self):
        """
        A-01 的尺寸标注**没有和像素位置绑定** —— 它只给了数值，没说量的是哪一段。
        所以只能做量级校验，并把分歧写进 notes，不能假装两边一致。
        """
        est = derive_scale(REAL_LAYOUT)
        joined = " ".join(est.notes)
        assert "交叉校验" in joined, "必须报告交叉校验结果，哪怕分歧不大"
        assert "8.20m" in joined

    def test_分歧大时明确告警(self):
        """把标注改成和面积推导明显矛盾的值，应当出现告警。"""
        bad = copy.deepcopy(REAL_LAYOUT)
        bad["dimensions"] = [{"label": "3000", "value": 3.0, "unit": "mm"}]  # 明显过小
        notes = " ".join(derive_scale(bad).notes)
        assert "⚠️" in notes, "分歧超过阈值却不告警"

    def test_无总面积时用尺寸标注兜底(self):
        layout = copy.deepcopy(REAL_LAYOUT)
        layout.pop("total_area")
        est = derive_scale(layout)
        assert est.source == "from_dimension"
        # 兜底路径必须说明它的不可靠
        assert any("未必对应" in n or "误差" in n for n in est.notes)

    def test_什么都没有时退化而不崩(self):
        est = derive_scale({})
        assert est.source == "fallback_default"
        assert est.px_per_m > 0
        assert any("退化" in n for n in est.notes)

    def test_房间面积为零不除零(self):
        est = derive_scale({
            "rooms": [{"bbox": [10, 10, 10, 10]}],
            "total_area": 45.0,
        })
        assert est.px_per_m > 0 and est.source == "fallback_default"


# ══════════════════════════════════════════════════════════════════
# 坐标归一化 —— Y 轴翻转
# ══════════════════════════════════════════════════════════════════


class TestCoordinateNormalization:
    def test_原点在包围盒左下角(self, scene):
        xs = [p.x for r in scene.rooms for p in r.polygon]
        ys = [p.y for r in scene.rooms for p in r.polygon]
        assert min(xs) == pytest.approx(0, abs=1e-6)
        assert min(ys) == pytest.approx(0, abs=1e-6)

    def test_Y轴翻转_上面还是上面(self, scene):
        """
        ⚠️ **这条守的是"看不见的错"。**

        图片坐标 Y 向下。不翻转的话，3D 里的户型是**左右镜像**的 ——
        而镜像户型看上去完全正常，只是房间左右对调了。
        没有任何界面元素会提示，只有量坐标才发现。

        判据：原图里 **y 较小**（靠上）的房间，在场景里应当 y 较大。
        """
        # 原图里 [40,40,380,300] 的 y 范围是 40–300，是三者中最靠上的
        top_px = REAL_LAYOUT["rooms"][0]
        assert top_px["bbox"][1] == 40

        room = scene.rooms[0]
        other = scene.rooms[1]  # 原图 y 300–545，靠下
        assert min(p.y for p in room.polygon) > min(p.y for p in other.polygon), (
            "原本靠上的房间在场景里跑到下面去了 —— Y 轴没翻转（或翻反了）"
        )

    def test_整体尺寸等于包围盒(self, scene):
        """7.891m × 5.817m ≈ 45.9㎡，与 total_area 对得上。"""
        assert scene.width_m == pytest.approx(7.891, abs=0.01)
        assert scene.depth_m == pytest.approx(5.817, abs=0.01)
        assert scene.width_m * scene.depth_m == pytest.approx(45.9, abs=0.5)

    def test_房间面积由多边形算而不是抄模型的(self, scene):
        """
        A-01 的 `area` 是模型读图估的，`bbox` 是模型给的坐标。
        两者是**两份独立的数据**，不一致时应当以几何为准 ——
        因为 2D 渲染器画的是 bbox，用模型的 area 会导致"标的面积和画出来的对不上"。
        """
        total = sum(r.area_m2 for r in scene.rooms)
        assert total == pytest.approx(45.9, abs=0.5)

    def test_面积与几何明显不符时记入_issues(self):
        bad = copy.deepcopy(REAL_LAYOUT)
        bad["rooms"][0]["area"] = 99.0  # 与 bbox 算出的 11.7 相差悬殊
        s = normalize_layout(bad)
        assert any("相差过大" in i for i in s.quality.issues), (
            "模型给的面积与几何差了一个数量级却不记录"
        )


# ══════════════════════════════════════════════════════════════════
# 墙体拓扑 —— 两个 bug 的回归
# ══════════════════════════════════════════════════════════════════


def _layout_with(walls):
    d = copy.deepcopy(REAL_LAYOUT)
    d["walls"] = walls
    return d


OUTER_LOOP = {"type": "unknown", "coords": [[40, 40], [725, 40], [725, 545], [40, 545], [40, 40]]}
PARTITION_V = {"type": "unknown", "coords": [[380, 40], [380, 545]]}
PARTITION_H = {"type": "unknown", "coords": [[40, 300], [380, 300]]}


class TestWallTopology:
    def test_T型接头判为闭合(self):
        """
        ⚠️ **回归测试（第一版 bug）。**

        内墙接在外墙**中间**（T 型接头）是最常见的接法，
        它的端点不落在任何别的墙的端点上。
        第一版只比端点，于是把每一个正常户型都判成"未闭合"。
        """
        s = normalize_layout(_layout_with([OUTER_LOOP, PARTITION_V, PARTITION_H]))
        assert s.quality.walls_closed, "正常户型（外墙环 + T 型内墙）被判为未闭合"
        assert s.quality.can_build_walls

    def test_闭合环自己的端点不算游离(self):
        """
        ⚠️ **回归测试（第二版 bug）。**

        闭合环的起点与终点是**同一个点**，它谁也不挨着 —— 但那是正常的。
        第二版把闭合环的端点也要求"接上别的墙"，于是又全判错了。
        这条用"只有外墙、没有任何内墙"来隔离这个情形。
        """
        s = normalize_layout(_layout_with([OUTER_LOOP]))
        assert s.quality.walls_closed, "只有闭合外墙时被判为未闭合"

    def test_游离的墙不影响闭合判定(self):
        """
        ⚠️ **这条测试的断言被修正过（2026-09-23）。**

        它原来断言"户型外多一截不相干的墙 → walls_closed = False"。
        改成洪水填充判据之后不成立了 —— 而且**原来那个断言本来就是错的**：

        `walls_closed` 要回答的是"**人走不走得出去**"。远处有一截孤立的墙，
        既不影响外墙的围合、也不会让人穿墙出去，它就是个多余的识别结果。
        把它算成"墙没闭合"，会让 3D 漫游因为一段无关的墙而整体降级。

        这类问题归 `unmatched_wall_ratio`（墙没被任何房间覆盖）管 ——
        分工明确：**围不围得住**看这条，**墙识别得干不干净**看那条。
        """
        floating = {"type": "unknown", "coords": [[900, 900], [1000, 900]]}
        s = normalize_layout(_layout_with([OUTER_LOOP, PARTITION_V, PARTITION_H, floating]))
        assert s.quality.walls_closed, "多一截外墙不该判成 围不住 —— 那是另一个问题"
        # 但它确实被记进了"未匹配墙"里
        assert s.quality.unmatched_wall_ratio > 0

    def test_外墙缺一段判为未闭合(self):
        broken = {"type": "unknown", "coords": [[40, 40], [725, 40], [725, 545]]}
        s = normalize_layout(_layout_with([broken]))
        assert not s.quality.walls_closed

    def test_没有墙时判为未闭合(self):
        """没有墙就别声称能建真实墙体 —— 渲染器要据此降级。"""
        s = normalize_layout(_layout_with([]))
        assert not s.quality.walls_closed
        assert not s.quality.can_build_walls

    def test_未闭合时记入_issues_并说明降级(self, ):
        s = normalize_layout(_layout_with([]))
        assert any("降级" in i or "盒体" in i for i in s.quality.issues), (
            "墙不可用时必须明说该降级，而不是让渲染器自己猜"
        )

    def test_墙坐标抖动被焊接(self):
        """
        视觉模型给的点往往差几个像素。不焊接的话，"首尾重合"判不出来，
        闭合环会被判成开放的折线。
        """
        jittery = {"type": "unknown", "coords": [
            [40, 40], [725, 42], [723, 545], [42, 543], [41, 41],
        ]}
        s = normalize_layout(_layout_with([jittery]))
        assert s.quality.wall_count == 1
        assert s.quality.walls_closed, "端点相差 1–2px 的抖动不该被判成断开"


# ══════════════════════════════════════════════════════════════════
# 门窗定位
# ══════════════════════════════════════════════════════════════════


class TestOpenings:
    def test_门窗被关联到墙上(self, scene):
        """每扇门窗都应当落到某段墙上（wall_index >= 0）。"""
        assert scene.openings, "没有解析出门窗"
        assert all(o.wall_index >= 0 for o in scene.openings), (
            f"有门窗没落到墙上：{[o for o in scene.openings if o.wall_index < 0]}"
        )

    def test_沿墙距离在墙长范围内(self, scene):
        for o in scene.openings:
            w = scene.walls[o.wall_index]
            assert 0 <= o.offset_along_wall_m <= w.length_m + 0.01, (
                f"{o.kind} 的沿墙距离 {o.offset_along_wall_m:.2f} "
                f"超出墙长 {w.length_m:.2f}"
            )

    def test_宽度为0时用兜底值并标记(self, scene):
        """实测三扇门的 width 全是 0 —— 必须兜底，且必须标记出来。"""
        doors = [o for o in scene.openings if o.kind == "door"]
        assert doors
        for d in doors:
            assert d.width_m > 0
            assert d.width_is_assumed, "用了兜底宽度却没标记，渲染器无从得知"
        assert any("兜底" in a for a in scene.assumptions)

    def test_有宽度的窗不标记为兜底(self, scene):
        windows = [o for o in scene.openings if o.kind == "window"]
        assert windows
        assert all(not w.width_is_assumed for w in windows), (
            "窗宽是识别出来的，不该被标成兜底"
        )

    def test_离墙太远的门窗不强行关联(self):
        """识别偏了的位置宁可标成未关联，也不要硬挂到不相干的墙上。"""
        d = copy.deepcopy(REAL_LAYOUT)
        d["doors"] = [{"position": [9999, 9999], "width": 0.9}]
        s = normalize_layout(d)
        assert s.openings[0].wall_index == -1


# ══════════════════════════════════════════════════════════════════
# 假设与质量
# ══════════════════════════════════════════════════════════════════


class TestAssumptionsAreRecorded:
    def test_层高是假设且被记录(self, scene):
        """
        户型图是二维的，**没有层高信息**。
        不记这一条，渲染出来的 3D 会让人以为层高是量出来的。
        """
        assert scene.ceiling_height_m == DEFAULT_CEILING_HEIGHT_M
        assert any("假设" in a and "层高" in a for a in scene.assumptions), (
            f"层高假设没被记录：{scene.assumptions}"
        )

    def test_房间轮廓是矩形近似且被记录(self, scene):
        assert any("bbox" in a for a in scene.assumptions)

    def test_比例尺是近似且被记录(self, scene):
        assert any("近似" in n for n in scene.scale_notes)

    def test_置信度透传(self, scene):
        assert scene.confidence == pytest.approx(0.45)

    def test_降级户型不崩(self):
        """
        `degraded_basic` 只有房间名、没有 bbox。归一化必须能处理，
        而不是在 3D 渲染器那里才炸。
        """
        degraded = {"mode": "degraded_basic",
                    "rooms": [{"name": "客厅", "type": "other", "area": 0.0, "bbox": []}],
                    "walls": [], "doors": [], "windows": [], "dimensions": [],
                    "total_area": 0.0, "confidence": 0.4}
        s = normalize_layout(degraded)
        assert s.rooms == []
        assert not s.quality.can_build_walls
        assert s.to_dict()["units"] == "m"

    def test_空输入不崩(self):
        s = normalize_layout({})
        assert s.px_per_m > 0
        assert s.to_dict()["quality"]["can_build_walls"] is False


class TestSerialization:
    def test_to_dict_可被_json_序列化(self, scene):
        """场景要发给前端做 Three.js 渲染，必须能过 JSON。"""
        import json

        payload = json.dumps(scene.to_dict(), ensure_ascii=False)
        back = json.loads(payload)
        assert back["units"] == "m"
        assert len(back["rooms"]) == 3
        assert len(back["walls"]) == 3

    def test_坐标都带合理精度(self, scene):
        """不裁剪精度的话，JSON 里会出现 3.7659876543210003 这种噪声。"""
        payload = scene.to_dict()
        for room in payload["rooms"]:
            for x, y in room["polygon"]:
                assert len(str(x).split(".")[-1]) <= 4
                assert len(str(y).split(".")[-1]) <= 4


# ══════════════════════════════════════════════════════════════════
# 闭合性判据的回归：**模型给的是碎段，不是环**
# ══════════════════════════════════════════════════════════════════


#: 一次真实解析的输出（2026-09-23，用自己生成的户型图跑出来的，未经修饰）
#:
#: ⚠️ 关键特征：**四条外墙是四段独立的墙，没有任何一段自己是闭合环**。
REAL_SEGMENTED = {
    "rooms": [
        {"name": "主卧", "type": "bedroom", "bbox": [100, 145, 600, 555]},
        {"name": "次卧", "type": "bedroom", "bbox": [608, 145, 1010, 555]},
        {"name": "儿童房", "type": "bedroom", "bbox": [1020, 145, 1520, 555]},
        {"name": "客厅", "type": "living_room", "bbox": [100, 568, 762, 955]},
        {"name": "餐厅", "type": "dining_room", "bbox": [780, 568, 1165, 955]},
        {"name": "厨房", "type": "kitchen", "bbox": [1175, 568, 1520, 955]},
        {"name": "阳台", "type": "balcony", "bbox": [100, 968, 762, 1188]},
        {"name": "卫生间", "type": "bathroom", "bbox": [780, 968, 1078, 1188]},
        {"name": "玄关", "type": "entrance", "bbox": [1088, 968, 1520, 1188]},
    ],
    "walls": [
        {"type": "unknown", "coords": [[95, 140], [1520, 140]]},      # 顶
        {"type": "unknown", "coords": [[95, 1190], [1520, 1190]]},    # 底
        {"type": "unknown", "coords": [[95, 140], [95, 1190]]},       # 左
        {"type": "unknown", "coords": [[1520, 140], [1520, 1190]]},   # 右
        {"type": "unknown", "coords": [[604, 140], [604, 560]]},
        {"type": "unknown", "coords": [[1013, 140], [1013, 560]]},
        {"type": "unknown", "coords": [[95, 560], [1520, 560]]},
        {"type": "unknown", "coords": [[770, 568], [770, 960]]},
        {"type": "unknown", "coords": [[1170, 568], [1170, 960]]},
        {"type": "unknown", "coords": [[95, 960], [770, 960]]},
        {"type": "unknown", "coords": [[775, 960], [775, 1190]]},
    ],
    "doors": [
        {"position": [300, 560]}, {"position": [1270, 560]}, {"position": [1000, 560]},
        {"position": [770, 700]}, {"position": [1170, 700]}, {"position": [900, 960]},
    ],
    "windows": [],
    "total_area": 98.0,
    "confidence": 0.62,
}


class TestSegmentedWallsRegression:
    """
    ⚠️ **这个类守的是一个让"第一人称漫游"完全起不来的 bug。**

    `_check_walls_closed` 初版的判据是「存在一个**首尾重合的折线**」。
    它在一份早期解析数据上碰巧成立（那次模型确实吐了个 5 点闭环），
    于是被当成了通用规则。

    实测发现模型**大部分时候给的是碎段**：

        顶 (95,140)->(1520,140)   底 (95,1190)->(1520,1190)
        左 (95,140)->(95,1190)    右 (1520,140)->(1520,1190)

    四条边严丝合缝围成矩形，但没有一段自己是环 → 判 False →
    `can_build_walls=False` → 3D 漫游降级成"自由视角"，
    **第一人称行走永远起不来**，而界面上一切正常。

    判据改成"从户型外洪水填充能不能渗进来"之后，与模型的表示方式无关。
    """

    def test_四条边各自成段也算闭合(self):
        s = normalize_layout(REAL_SEGMENTED)
        assert s.quality.walls_closed, "四条外墙围成了矩形，却判成不闭合"
        assert s.quality.can_build_walls

    def test_没有一段墙自己是环(self):
        """先确认这份数据确实没有闭合环 —— 否则上面的用例证明不了什么。"""
        s = normalize_layout(REAL_SEGMENTED)
        assert not any(w.is_loop for w in s.walls), (
            "这份数据里出现了闭合环，那就测不到'碎段'这条路径了"
        )

    def test_缺一整条边判为不闭合(self):
        lay = dict(REAL_SEGMENTED, walls=REAL_SEGMENTED["walls"][1:])
        s = normalize_layout(lay)
        assert not s.quality.walls_closed, "少了顶墙，人可以直接走出去，却判成闭合"
        assert not s.quality.can_build_walls

    def test_缺半条边也判为不闭合(self):
        """缺口 3.6m —— 不是"焊接容差"能糊过去的小缝。"""
        lay = dict(REAL_SEGMENTED, walls=REAL_SEGMENTED["walls"][1:] + [
            {"type": "unknown", "coords": [[95, 140], [1100, 140]]},
        ])
        s = normalize_layout(lay)
        assert not s.quality.walls_closed

    def test_墙上开一个一米五的口判为不闭合(self):
        """
        缺口 1.5m，比墙厚（0.2m）大得多 —— 人走得出去，必须判不闭合。

        这条同时确认判据**不是**在瞎宽容：容差只有半个墙厚，
        小抖动放行、真缺口拦下。
        """
        walls = [w for i, w in enumerate(REAL_SEGMENTED["walls"]) if i != 3]
        walls += [
            {"type": "unknown", "coords": [[1520, 140], [1520, 600]]},
            {"type": "unknown", "coords": [[1520, 750], [1520, 1190]]},
        ]
        s = normalize_layout(dict(REAL_SEGMENTED, walls=walls))
        assert not s.quality.walls_closed

    def test_小抖动不算缺口(self):
        """
        反向：墙端点差几厘米是识别抖动，不是缺口 —— 判据必须放行，
        否则每个真实户型都会被判成"围不住"。
        """
        jittery = []
        for w in REAL_SEGMENTED["walls"]:
            c = w["coords"]
            jittery.append({"type": "unknown",
                            "coords": [[c[0][0] + 3, c[0][1] + 2], [c[1][0] - 3, c[1][1] - 2]]})
        s = normalize_layout(dict(REAL_SEGMENTED, walls=jittery))
        assert s.quality.walls_closed, "±3px（约 3cm）的抖动被当成了缺口"
