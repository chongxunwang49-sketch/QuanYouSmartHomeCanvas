"""
3D 家具的四条新行为（2026-09-26 需求方口径）。

  · 按**族**上色 —— 不同种类不同色，同一种类同色
  · 家具**不嵌进墙**（房间 bbox 反推的内轮廓比真实内表面宽 4~6cm）
  · 摆不下时**等比放大 3D 场景**（只放大 3D，平面图与造价不动）
  · 放大倍数**必须能显示给用户**（后端给字段，前端展示）

前三条都有对应的真实现象，不是预防性设计 —— 每条都在注释里写了实测数字。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.services.furniture import catalog, placement, scaling
from backend.app.services.geometry.walkable import build_walkable
from backend.app.services.render import render_plan_for

FIXTURE = Path(__file__).resolve().parents[1] / "scripts" / "fixtures" / "golden_layout.json"


def _scene():
    layout = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scene, _plan = render_plan_for(layout)
    return scene, build_walkable(scene)


def _overlap(a: placement.Box, b: placement.Box) -> float:
    ox = min(a.x2, b.x2) - max(a.x1, b.x1)
    oy = min(a.y2, b.y2) - max(a.y1, b.y1)
    return 0.0 if ox <= 0 or oy <= 0 else ox * oy


# ══════════════════════════════════════════════════════════════════
# 一、按族上色
# ══════════════════════════════════════════════════════════════════


class TestFamilyColors:
    def test_每个族都有色号(self):
        """目录里 38 个族，三种风格都得有色 —— 少一个就会有族退回角色色。"""
        for style in ("modern", "nordic", "chinese"):
            table = catalog.style_palette(style).family_colors
            assert table, f"{style} 的 family_colors 是空的（配色脚本没跑过？）"
            missing = [s.family for s in catalog.specs() if not table.get(s.family)]
            assert not missing, f"{style} 缺族色：{sorted(set(missing))}"

    def test_同一种类同色_不同种类不同色(self):
        """
        ⚠️ 这条守的是需求方原话："不同的家具不同颜色区分，**相同的家具类型
        使用同一颜色**"。前半句与后半句都要成立，所以两头都断言。
        """
        scene, walk = _scene()
        res = placement.place_all(
            walkable=walk.to_dict(), scene=scene.to_dict(), style="modern")

        by_family: dict[str, set[str]] = {}
        spec_of = {s.id: s for s in catalog.specs()}
        for room in res["rooms"]:
            for p in room["placements"]:
                fam = spec_of[p["spec_id"]].family
                by_family.setdefault(fam, set()).add(p["color"])

        # 后半句：同族必须同色（一个族出现了两次却有两个色号 = 选了族色又漏了）
        split = {f: c for f, c in by_family.items() if len(c) > 1}
        assert not split, f"同一个族出现了多种颜色：{split}"

        # 前半句：**会同房的**族之间色号必须不同。
        # 不同的族共用色号是允许的（它们不同房，没人会同时看到），
        # 而会同房的族撞色正是"分不出这是什么"的那个毛病。
        for room in res["rooms"]:
            fams = {spec_of[p["spec_id"]].family for p in room["placements"]}
            colors = {p["color"] for p in room["placements"] if p["color"]}
            assert len(colors) == len(fams), (
                f"{room['name']}：{len(fams)} 个族只有 {len(colors)} 种颜色 —— "
                f"房间里会有两件家具长得一样"
            )

    def test_每个族色都对地面拉得开(self):
        """
        约束是**对 3D 地面 ≥2:1**。不满足的话家具和地面糊在一起 ——
        这正是 ADR-16 当初把 `palette` 换成 `surface` 的原因（实测 1.01:1）。
        """
        from scripts.derive_surface_palette import contrast_ratio, hex_to_rgb

        for style in ("modern", "nordic", "chinese"):
            pal = catalog.style_palette(style)
            floor = hex_to_rgb(pal.surface_floor)
            bad = [
                (fam, hexv, round(contrast_ratio(hex_to_rgb(hexv), floor), 2))
                for fam, hexv in pal.family_colors.items()
                if contrast_ratio(hex_to_rgb(hexv), floor) < 2.0
            ]
            assert not bad, f"{style} 有族色对地面不足 2:1：{bad[:4]}"

    def test_族色不是纯黑也不是纯白(self):
        """
        ⚠️ **这条是有来历的。** 最远点贪心在"只约束对比度"时，会立刻去挑
        **纯黑**（`#06060E`，相对亮度 0.002）—— 因为那样两两距离最大。
        报表上 ΔE 漂亮得很，画面是一团黑（ADR-16 记录过同一个坑）。
        所以窗口两端各收一刀，这条守的就是那两刀。
        """
        from scripts.derive_family_palette import LUMA_CEIL_MAX, LUMA_FLOOR_MIN
        from scripts.derive_surface_palette import hex_to_rgb, relative_luminance

        for style in ("modern", "nordic", "chinese"):
            for fam, hexv in catalog.style_palette(style).family_colors.items():
                lum = relative_luminance(hex_to_rgb(hexv))
                assert lum >= LUMA_FLOOR_MIN - 1e-6, (
                    f"{style}/{fam} = {hexv} 亮度 {lum:.3f} —— 太黑，看不出颜色"
                )
                assert lum <= LUMA_CEIL_MAX + 1e-6, (
                    f"{style}/{fam} = {hexv} 亮度 {lum:.3f} —— 太白，与墙分不开"
                )


# ══════════════════════════════════════════════════════════════════
# 二、家具不嵌进墙
# ══════════════════════════════════════════════════════════════════


class TestNoWallClipping:
    def test_没有一件家具嵌进墙里(self):
        """
        ⚠️ 实测踩过：`room_interior()` 是从房间 bbox **反推**的，前提是
        "房间 bbox 的边 == 墙的中线"。实测这个前提差了 **4~6cm**，
        于是贴墙摆的家具嵌进墙体 —— 24 组里 54 件中招，
        最严重的一件（厨房餐桌）嵌进去 **607 c㎡**。

        判据按**墙的真实厚度**取矩形，不按中线 —— 按中线量的话家具紧贴
        内表面时刚好不碰中线，结果是 0 命中，而画面上已经嵌进去了。
        """
        scene, walk = _scene()
        scene_d = scene.to_dict()
        walls = placement.wall_boxes(scene_d["walls"])

        for style in ("modern", "nordic", "chinese"):
            res = placement.place_all(
                walkable=walk.to_dict(), scene=scene_d, style=style)
            for room in res["rooms"]:
                for p in room["placements"]:
                    box = placement.Box(
                        p["x"] - p["w"] / 2, p["y"] - p["d"] / 2,
                        p["x"] + p["w"] / 2, p["y"] + p["d"] / 2)
                    worst = max((_overlap(box, w) for w in walls), default=0.0)
                    assert worst <= 1e-9, (
                        f"{style}/{room['name']}/{p['label']} 嵌进墙里 "
                        f"{worst * 10000:.0f} c㎡"
                    )

    def test_收内轮廓不会把家具全挤掉(self):
        """
        ⚠️ 收紧内轮廓是有代价的：收多了家具就没地方摆。
        实测踩过一次 —— "墙在房间哪一侧"按**中心距离**判时，矮房间里
        纵贯的长墙被判反，内轮廓收成负数，摆下的从 597 掉到 456。
        这条守的是"收完之后家具还摆得下"。
        """
        scene, walk = _scene()
        res = placement.place_all(
            walkable=walk.to_dict(), scene=scene.to_dict(), style="modern")
        assert res["placed_count"] >= 12, (
            f"只摆下 {res['placed_count']} 件 —— 内轮廓可能被收过头了"
        )

    def test_墙盒按厚度不按中线(self):
        """把一段 0.2m 厚的墙变成 0.2m 宽的矩形，不是一条线。"""
        walls = placement.wall_boxes([
            {"kind": "x", "thickness_m": 0.2, "points": [[0.0, 0.0], [4.0, 0.0]]},
        ])
        assert len(walls) == 1
        b = walls[0]
        assert b.w == pytest.approx(4.2)          # 两端各伸出半厚
        assert b.d == pytest.approx(0.2)
        assert b.y1 == pytest.approx(-0.1) and b.y2 == pytest.approx(0.1)


# ══════════════════════════════════════════════════════════════════
# 三、等比放大
# ══════════════════════════════════════════════════════════════════


class TestScaling:
    def test_放大时几何缩放而人的尺寸不缩(self):
        """
        ⚠️ **三处不能缩，缩了就是错的：**
          · `along` / `normal` 是单位方向向量 —— 乘 k 就不再是单位向量，
            门扇会歪、转轴会跑偏
          · `player_radius_m` / `eye_height_m` 是**人的尺寸** ——
            放大的是房子，人不变。把它们也乘 k 等于什么都没做。
          · `plan_transform.scale` 是"米 → SVG 像素"的换算率，要**除** k，
            否则小地图上的绿点会向外漂走
        """
        _s, walk = _scene()
        payload = {
            "scene": _scene()[0].to_dict(),
            "walkable": walk.to_dict(),
            "plan_transform": {
                "scale": 100.0, "offset_x": 10.0, "offset_y": 20.0,
                "draw_depth_m": 9.0, "draw_width_m": 8.0,
            },
        }
        before = json.loads(json.dumps(payload))
        scaling.scale_walkable_payload(payload, 2.0)

        # 几何缩了
        assert payload["walkable"]["spawn"]["x"] == pytest.approx(
            before["walkable"]["spawn"]["x"] * 2)
        assert payload["scene"]["width_m"] == pytest.approx(
            before["scene"]["width_m"] * 2)
        assert payload["walkable"]["rooms"][0]["free_rect"][0] == pytest.approx(
            before["walkable"]["rooms"][0]["free_rect"][0] * 2)

        # 单位向量**没缩**
        d0, d1 = before["walkable"]["doors"][0], payload["walkable"]["doors"][0]
        assert d1["along"] == d0["along"]
        assert d1["normal"] == d0["normal"]

        # 人的尺寸**没缩**
        assert payload["walkable"]["player_radius_m"] == \
            before["walkable"]["player_radius_m"]
        assert payload["walkable"]["eye_height_m"] == \
            before["walkable"]["eye_height_m"]

        # 换算率**除了** k
        assert payload["plan_transform"]["scale"] == pytest.approx(50.0)
        assert payload["plan_transform"]["draw_depth_m"] == pytest.approx(18.0)

    def test_倍数为一十原样返回(self):
        """k=1 时不动任何字段 —— 不分叉出一条"放大后"的代码路径。"""
        _s, walk = _scene()
        payload = {"scene": _scene()[0].to_dict(), "walkable": walk.to_dict()}
        same = json.loads(json.dumps(payload))
        scaling.scale_walkable_payload(payload, 1.0)
        assert payload == same

    def test_只数空间类拒绝不算政策类(self):
        """
        ⚠️ 「同类已有一件」「占地达上限」是**政策**，放大房间消不掉。
        混进来数的话，`_scene_scale` 会一路加到上限，
        白白把房子放大 2.5 倍 —— 而"摆不下"那件家具还是没解决。
        """
        res = {
            "rejected": [
                {"kind": "space", "reason": "与已摆放的家具重叠"},
                {"kind": "policy", "reason": "同类已有一件（family=bed）"},
                {"kind": "policy", "reason": "本间房的家具占地已达上限"},
            ],
        }
        assert scaling.space_rejections(res) == 1

    def test_阶梯是升序且从一十开始(self):
        """第一个够用的就选它，所以放得下时必须不放大。"""
        k = scaling.SCALE_LADDER
        assert k[0] == 1.0
        assert list(k) == sorted(k)
        assert scaling.MAX_SCENE_SCALE == k[-1]

    def test_放大后家具仍不重叠且不嵌墙(self):
        """
        放大是为了"全部摆下、且不重叠" —— 所以放大之后这两条必须仍然成立。
        """
        scene, walk = _scene()
        base = {"scene": scene.to_dict(), "walkable": walk.to_dict()}
        k = 1.8
        payload = json.loads(json.dumps(base))
        scaling.scale_walkable_payload(payload, k)
        res = placement.place_all(
            walkable=payload["walkable"], scene=payload["scene"], style="modern")

        walls = placement.wall_boxes(payload["scene"]["walls"])
        for room in res["rooms"]:
            boxes = [
                placement.Box(p["x"] - p["w"] / 2, p["y"] - p["d"] / 2,
                              p["x"] + p["w"] / 2, p["y"] + p["d"] / 2)
                for p in room["placements"]
            ]
            for b in boxes:
                assert max((_overlap(b, w) for w in walls), default=0.0) <= 1e-9, \
                    f"{room['name']} 放大后仍有家具嵌进墙"

            # 只有**都占地面的**两件才算真冲突（吊灯吊在地台上方是正常的）
            occupying = [
                (p, b) for p, b in zip(room["placements"], boxes)
                if placement._occupies_floor(placement.Placement(
                    spec_id="", label="", room_index=0, room_name="",
                    box=b, rot_deg=0, mount=p["mount"], height_m=p["height_m"],
                    color_role="", y_offset_m=0.0, no_collide=p["no_collide"]))
            ]
            for i in range(len(occupying)):
                for j in range(i + 1, len(occupying)):
                    assert _overlap(occupying[i][1], occupying[j][1]) <= 1e-9, (
                        f"{room['name']}：{occupying[i][0]['label']} 与 "
                        f"{occupying[j][0]['label']} 重叠了"
                    )


# ══════════════════════════════════════════════════════════════════
# 四、出生点不落在通过性空间
# ══════════════════════════════════════════════════════════════════


class TestSpawnNotPassage:
    def test_走道门洞这种名字也算通过性空间(self):
        """
        ⚠️ **按子串判，不是按全等。** 实测踩过：模型给那间房起的名字是
        「走道/门洞」—— 带斜杠后缀，全等匹配漏掉，出生点又落回通过性空间。
        名字是模型起的，格式不受我们控制。
        """
        from backend.app.services.geometry.walkable import _is_passage

        class _R:
            def __init__(self, name):
                self.name = name

        for name in ("走道/门洞", "走廊", "玄关", "入户花园", "过道"):
            assert _is_passage(_R(name)), f"{name} 没被判成通过性空间"
        for name in ("主卧", "客厅", "餐厅", "厨房", "卫生间", "阳台"):
            assert not _is_passage(_R(name)), f"{name} 被误判成通过性空间"

    def test_出生点不落在通过性空间(self):
        scene, walk = _scene()
        spawn = next(r for r in walk.rooms if r.index == walk.spawn_room)
        from backend.app.services.geometry.walkable import _is_passage

        assert not _is_passage(spawn), (
            f"出生在「{spawn.name}」—— 站进去看不到什么"
        )
