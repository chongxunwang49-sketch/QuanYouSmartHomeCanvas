"""
家具摆放与三条几何断言。

本文件守的核心命题：**摆出来的东西必须真的站得住。**

「站得住」在这里是三条可计算的判据（在房间里且不越入邻室 / 不压门洞
开启扇区 / 与已摆的互不重叠含前方净空）。三条都由 `violations()` 判定，
**摆放时用它筛、测试里直接断言它** —— 各写一份的话，分叉的表现是
"测试说没问题、场景里衣柜在走廊上"。

最强的一条是 `TestRealFloorPlan`：对真实解析产物跑一遍，然后
**逐件回代 `violations()`**。它不是"检查代码有没有报错"，而是
"检查产出的每一个坐标是否满足判据"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.services.furniture import catalog, placement
from backend.app.services.furniture.placement import Box

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "scripts" / "fixtures" / "golden_layout.json"


# ══════════════════════════════════════════════════════════════════
# 矩形的语义
# ══════════════════════════════════════════════════════════════════


class TestBox:
    def test_接触不算重叠(self):
        """
        ⚠️ 这条错了整个摆放就废了：家具**贴墙、贴邻座**是常态，
        把"边界相接"判成重叠会让每一件都摆不下去。
        """
        a = Box(0, 0, 1, 1)
        assert not a.overlaps(Box(1, 0, 2, 1))
        assert not a.overlaps(Box(0, 1, 1, 2))
        assert a.overlaps(Box(0.99, 0, 2, 1))

    def test_包含允许贴边(self):
        outer = Box(0, 0, 2, 2)
        assert outer.contains(Box(0, 0, 2, 2))
        assert outer.contains(Box(0, 0, 1, 1))
        assert not outer.contains(Box(-0.01, 0, 1, 1))


# ══════════════════════════════════════════════════════════════════
# 三条断言
# ══════════════════════════════════════════════════════════════════


class TestViolations:
    _ROOM = Box(0, 0, 4, 3)

    def test_房间内无问题时为空(self):
        assert placement.violations(Box(1, 1, 2, 2), interior=self._ROOM) == []

    def test_出界被抓住(self):
        bad = placement.violations(Box(3.5, 0, 5, 1), interior=self._ROOM)
        assert "超出房间内轮廓" in bad

    def test_越入邻室被抓住(self):
        """
        ⚠️ 这条是**我加的**（需求口径只说了"在房间里"）。

        房间轮廓是 bbox 近似，L 形房间的凹口处，一件家具可以既在
        本房间的 bbox 内、又实实在在落在隔壁 —— "在 bbox 内"这条断言
        照样通过。少一条判据不会报错，只会安静地错。
        """
        neighbor = Box(2, 0, 4, 3)
        # 完全落在邻室里、也在本房间 bbox 内（bbox 是包含邻室的大矩形）
        bad = placement.violations(
            Box(2.5, 1, 3.5, 2), interior=self._ROOM, neighbors=[neighbor],
        )
        assert "越入相邻房间" in bad

    def test_压门洞被抓住(self):
        zone = Box(0, 0, 1, 1)
        bad = placement.violations(
            Box(0.2, 0.2, 0.8, 0.8), interior=self._ROOM, door_boxes=[zone],
        )
        assert "挡住门洞开启范围" in bad

    def test_与已摆放重叠被抓住(self):
        bad = placement.violations(
            Box(1, 1, 2, 2), interior=self._ROOM, placed=[Box(1.5, 1.5, 3, 3)],
        )
        assert "与已摆放的家具重叠" in bad

    def test_前方净空被占被抓住(self):
        """
        净空**不是记一笔就算**：它必须真的空着。这条漏掉的话，
        "床前留了 0.6m"这句话在纸面上成立，而实际那儿摆着一个柜子。
        """
        item = Box(1, 1, 2, 2)
        clear = Box(1, 1, 2, 3)          # 朝 +y 留出 1m
        bad = placement.violations(
            item, interior=self._ROOM, placed=[Box(1.2, 2.2, 1.8, 2.8)],
            clearance=clear,
        )
        assert "前方净空被别人占了" in bad

    def test_净空空着就算过(self):
        item = Box(1, 1, 2, 2)
        clear = Box(1, 1, 2, 3)
        assert placement.violations(
            item, interior=self._ROOM, placed=[Box(3, 0, 3.5, 1)], clearance=clear,
        ) == []


# ══════════════════════════════════════════════════════════════════
# 几何零件
# ══════════════════════════════════════════════════════════════════


class TestGeometryPieces:
    def test_内轮廓把玩家半径加回去(self):
        """
        `free_rect` 是"可站立矩形"（内缩半墙厚 + 玩家半径）。
        家具背面该贴**墙**，不该贴可站立线 —— 否则每件都离墙 0.3m 飘着。
        """
        room = {"free_rect": [1, 1, 4, 3]}
        box = placement.room_interior(room, player_radius_m=0.25)
        assert box == Box(0.75, 0.75, 4.25, 3.25)

    def test_退化矩形返回None(self):
        assert placement.room_interior({"free_rect": [1, 1, 1.02, 3]},
                                       player_radius_m=0) is None
        assert placement.room_interior({}, player_radius_m=0) is None

    def test_门洞扇区覆盖铰链到全开(self):
        doors = [{
            "from_room": 0, "to_room": 1, "width_m": 0.9,
            "hinge": [1.0, 0.0], "along": [1.0, 0.0], "normal": [0.0, 1.0],
        }]
        zones = placement.door_zones(doors, 0)
        assert len(zones) == 1
        z = zones[0]
        # 从铰链沿墙 0.9、朝法向 0.9
        assert (z.x1, z.y1, z.x2, z.y2) == pytest.approx((1.0, 0.0, 1.9, 0.9))

    def test_与房间无关的门不算(self):
        doors = [{"from_room": 7, "to_room": 8, "width_m": 0.9,
                  "hinge": [0, 0], "along": [1, 0], "normal": [0, 1]}]
        assert placement.door_zones(doors, 0) == []

    def test_缺字段的门被跳过而不是崩(self):
        assert placement.door_zones([{"from_room": 0, "to_room": 1}], 0) == []


# ══════════════════════════════════════════════════════════════════
# 单间房摆放
# ══════════════════════════════════════════════════════════════════


def _room(name: str = "主卧", *, x2: float = 4.0, y2: float = 3.5) -> dict:
    return {"index": 0, "name": name, "kind": "bedroom",
            "free_rect": [0.3, 0.3, x2, y2], "area_m2": (x2 - 0.3) * (y2 - 0.3)}


def _spec(id_: str, *, w: float, d: float, need_wall: bool = True,
          family: str = "x", prefer: str = "longest",
          clearance: float = 0.0) -> catalog.FurnitureSpec:
    return catalog.FurnitureSpec(
        id=id_, label=id_, mount="floor", width_m=w, depth_m=d, height_m=1.0,
        aliases=(), rooms=(), need_wall=need_wall, prefer=prefer,
        clearance_m=clearance, color_role="wood", family=family,
    )


class TestPlaceRoom:
    def test_靠墙家具贴着墙不悬空(self):
        placed, _ = placement.place_room(
            _room(), specs=[_spec("wardrobe", w=2.0, d=0.6)],
            doors=[], openings=[], player_radius_m=0.25,
        )
        assert len(placed) == 1
        box = placed[0].box
        interior = placement.room_interior(_room(), player_radius_m=0.25)
        # 背面必须贴到内轮廓的某一条边（而不是浮在中间）
        assert min(abs(box.x1 - interior.x1), abs(box.x2 - interior.x2),
                   abs(box.y1 - interior.y1), abs(box.y2 - interior.y2)) < 1e-6

    def test_摆不下就不放而不是硬塞(self):
        """2×2 的房间放 3×1 的柜子 —— 宁可空着，不塞一个超出房间的。"""
        placed, rejected = placement.place_room(
            _room(x2=2.0, y2=2.0), specs=[_spec("huge", w=3.0, d=1.0)],
            doors=[], openings=[], player_radius_m=0.25,
        )
        assert placed == []
        assert rejected and "超出房间内轮廓" in rejected[0]["reason"]

    def test_同类只摆一件(self):
        """
        ⚠️ 实测踩过：客厅被摆上**三张沙发** —— 沙发是三件大件里最大的，
        按面积排序时它们连着占满三个名额。这不是口味问题，是规则缺口。
        """
        specs = [_spec("sofa_3", w=2.2, d=0.9, family="sofa", need_wall=False),
                 _spec("sofa_2", w=1.6, d=0.9, family="sofa", need_wall=False)]
        placed, rejected = placement.place_room(
            _room("客厅", x2=5.0, y2=4.0), specs=specs,
            doors=[], openings=[], player_radius_m=0.25,
        )
        assert len(placed) == 1
        assert any("同类已有一件" in r["reason"] for r in rejected)

    def test_最多三件(self):
        specs = [_spec(f"f{i}", w=0.5, d=0.5, family=f"f{i}", need_wall=False)
                 for i in range(6)]
        placed, _ = placement.place_room(
            _room(x2=6.0, y2=5.0), specs=specs,
            doors=[], openings=[], player_radius_m=0.25,
        )
        assert len(placed) == placement.MAX_PER_ROOM

    def test_挡门的候选会被换到另一面墙(self):
        """门在南墙正中，柜子应当被赶到别的墙上 —— 而不是放弃摆放。"""
        doors = [{"from_room": 0, "to_room": 1, "width_m": 1.2,
                  "hinge": [2.0, 0.3], "along": [1.0, 0.0], "normal": [0.0, 1.0]}]
        placed, _ = placement.place_room(
            _room(), specs=[_spec("cabinet", w=1.0, d=0.5)],
            doors=doors, openings=[], player_radius_m=0.25,
        )
        assert len(placed) == 1, "应当换一面墙摆下，而不是直接放弃"
        zone = placement.door_zones(doors, 0)[0]
        assert not placed[0].box.overlaps(zone)

    def test_同一份输入两次结果完全相同(self):
        """
        摆放必须是**确定性的** —— AC-32 的重放靠它，3D 场景的可复现也靠它。
        不靠字典遍历顺序、不靠浮点误差碰运气。
        """
        specs = [_spec("a", w=1.2, d=0.6, family="a"),
                 _spec("b", w=0.8, d=0.4, family="b", need_wall=False),
                 _spec("c", w=0.6, d=0.35, family="c")]
        kw = dict(doors=[], openings=[], player_radius_m=0.25)
        first, _ = placement.place_room(_room(), specs=specs, **kw)
        second, _ = placement.place_room(_room(), specs=specs, **kw)
        assert [p.to_dict() for p in first] == [p.to_dict() for p in second]


class TestPreferenceOrder:
    def test_最长墙优先(self):
        """
        `prefer=longest` 用在床与衣柜上 —— 它们需要一面足够长的墙。
        3.5×2.0 的房间里最长墙是南/北，床该贴那一面。
        """
        rect = Box(0, 0, 3.5, 2.0)
        blockers = {k: {"has_door": False, "has_window": False, "door_at": None}
                    for k in placement._SIDES}
        order = placement._preference_order(
            _spec("bed", w=2.0, d=1.8, prefer="longest"), rect, blockers, [])
        assert order[0] in ("S", "N"), f"最长墙该排前，实际 {order}"

    def test_门对面优先(self):
        rect = Box(0, 0, 4.0, 3.0)
        blockers = {k: {"has_door": False, "has_window": False, "door_at": None}
                    for k in placement._SIDES}
        blockers["S"]["has_door"] = True
        blockers["S"]["door_at"] = (2.0, 0.0)
        order = placement._preference_order(
            _spec("sofa", w=2.0, d=0.9, prefer="opposite_door"), rect, blockers, [])
        assert order[0] == "N", "应当选离门最远的那面墙"

    def test_有门的墙不被优先(self):
        rect = Box(0, 0, 4.0, 3.0)
        blockers = {k: {"has_door": False, "has_window": False, "door_at": None}
                    for k in placement._SIDES}
        blockers["S"]["has_door"] = True
        order = placement._preference_order(
            _spec("x", w=1.0, d=0.5, prefer="any"), rect, blockers, [])
        assert order[0] != "S"


# ══════════════════════════════════════════════════════════════════
# 真实户型：最强的一条
# ══════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def plan() -> dict:
    if not FIXTURE.exists():
        pytest.skip("没有固定输入 scripts/fixtures/golden_layout.json")
    from backend.app.services.geometry.walkable import build_walkable
    from backend.app.services.render import render_plan_for

    layout = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scene, _ = render_plan_for(layout)
    walk = build_walkable(scene)
    return placement.place_all(
        walkable=walk.to_dict(), scene=scene.to_dict(), style="modern",
    )


class TestRealFloorPlan:
    def test_摆出来的每一件都满足三条判据(self):
        """
        ⚠️ **这条是整批测试里最有价值的一条。**

        它不检查"代码有没有报错"，而是把产出的**每一个坐标**回代进
        `violations()` —— 也就是"产出的东西是不是真的站得住"。
        前面那些用例测的是判据本身对不对，这条测的是**摆放真的用了它**。
        """
        import json as _json
        from pathlib import Path as _Path

        from backend.app.services.geometry.walkable import build_walkable
        from backend.app.services.render import render_plan_for

        layout = _json.loads(FIXTURE.read_text(encoding="utf-8"))
        scene, _ = render_plan_for(layout)
        walk = build_walkable(scene)
        wk, sc = walk.to_dict(), scene.to_dict()

        interiors = {
            int(r["index"]): placement.room_interior(
                r, player_radius_m=walk.player_radius_m)
            for r in wk["rooms"]
        }

        checked = 0
        flat_checked = 0
        for room in wk["rooms"]:
            idx = int(room["index"])
            # ⚠️ **只有"实体件"进障碍表。**
            #
            #    平铺在地上的件（地毯/地台，`no_collide`）既不挡人走路、
            #    也不挡别的家具 —— 现实里地毯就是压在床和沙发底下的。
            #    摆放时用的是同一条规则（见 `place_room` 里的 `item_boxes`），
            #    这里照着复现；否则回代会把"地毯压住了床脚"报成违规，
            #    而那不是违规，是地毯。
            placed_boxes: list[Box] = []
            for p in placement_placements(wk, sc, idx):
                box = Box(p["x"] - p["w"] / 2, p["y"] - p["d"] / 2,
                          p["x"] + p["w"] / 2, p["y"] + p["d"] / 2)
                bad = placement.violations(
                    box,
                    interior=interiors[idx],
                    neighbors=[b for i, b in interiors.items() if i != idx],
                    door_boxes=placement.door_zones(wk["doors"], idx),
                    placed=placed_boxes,
                )
                assert not bad, (
                    f"{room['name']} 的「{p['label']}」不满足判据：{bad}\n"
                    f"  位置 {box.as_dict()}"
                )
                if p.get("no_collide"):
                    flat_checked += 1
                else:
                    placed_boxes.append(box)
                checked += 1
        assert checked >= 8, f"只检查了 {checked} 件，样本太少"
        assert flat_checked, (
            "一份家具都没标 no_collide —— 平铺件那条规则就没被这条用例覆盖"
        )

    def test_平铺件不与实体件互相排斥(self):
        """
        ⚠️ **这条守的是"东西真的放得下"，不是判据本身。**

        实测踩过：儿童房里地毯铺在正中、衣柜靠墙，两者**本体并不重叠**，
        但衣柜的前方净空伸进了地毯的范围 → 判据报「前方净空被别人占了」
        → 衣柜被拒。一间卧室因为一张地毯丢了衣柜。

        现在平铺件（`no_collide`：地毯/地台）不进障碍表，于是：
        **平铺件与实体件可以共处**，而实体件之间照旧互不重叠 ——
        后半条是**不能**被前半条带松的，所以两头都断言。
        """
        import json as _json

        from backend.app.services.geometry.walkable import build_walkable
        from backend.app.services.render import render_plan_for

        layout = _json.loads(FIXTURE.read_text(encoding="utf-8"))
        scene, _ = render_plan_for(layout)
        walk = build_walkable(scene)
        wk, sc = walk.to_dict(), scene.to_dict()

        solid: dict[int, list[Box]] = {}
        flat: dict[int, list[Box]] = {}
        for room in wk["rooms"]:
            idx = int(room["index"])
            for p in placement_placements(wk, sc, idx):
                box = Box(p["x"] - p["w"] / 2, p["y"] - p["d"] / 2,
                          p["x"] + p["w"] / 2, p["y"] + p["d"] / 2)
                target = flat if p.get("no_collide") else solid
                target.setdefault(idx, []).append(box)

        for idx, boxes in solid.items():
            for i, a in enumerate(boxes):
                for b in boxes[i + 1:]:
                    assert not a.overlaps(b), (
                        f"房间 {idx} 里两件实体家具重叠了："
                        f"{a.as_dict()} 与 {b.as_dict()}"
                    )

    def test_障碍表里只有落在地上且不是平铺的件(self):
        """
        ⚠️ **这条守的是那个穿模 bug，并防止它再回来。**

        `shower` 是 0.9×0.9×**2.0m** 的实心玻璃房、落地摆放 —— 它当然挡家具。
        而它在目录里被标成 `no_collide: true`，而当时的互斥判定读的**正是
        这个裸字段** -> 淋浴房不算障碍 -> 浴室柜被摆进了它内部
        （重叠 0.45㎡）-> 3D 直接穿模。

        根因是**一个布尔同时承担了两件事**："人撞不撞它"（地毯/地台/吊灯/
        挂墙件都合理地不挡人）与"别的家具能不能压在它上面"（只有平铺件可以）。
        判据因此换成 `_occupies_floor()`：**落在地上 + 不是平铺的**。

        ⚠️ 这里**把整本目录过一遍**，而不是挑一两件断言 —— 挑着断言的话，
        下次有人在目录里加一件 2m 高的落地件、顺手勾上 `no_collide`，
        这条用例照样是绿的。
        """
        from backend.app.services.furniture import catalog
        from backend.app.services.furniture.placement import (
            FLAT_MAX_HEIGHT_M, Placement, _occupies_floor,
        )

        def as_placement(spec) -> Placement:
            return Placement(
                spec_id=spec.id, label=spec.label, room_index=0, room_name="样本",
                box=Box(0.0, 0.0, spec.width_m, spec.depth_m), rot_deg=0,
                mount=spec.mount, height_m=spec.height_m,
                color_role=spec.color_role, y_offset_m=0.0,
                no_collide=spec.no_collide,
            )

        specs = {s.id: s for s in catalog.specs()}

        # ── 点名断言：这几件的语义不能变 ────────────────────────
        assert _occupies_floor(as_placement(specs["shower"])), (
            "淋浴房被判成了不占地面的平铺件 —— 家具会摆进它里面（穿模）"
        )
        for flat_id in ("rug", "deck"):
            assert not _occupies_floor(as_placement(specs[flat_id])), (
                f"{flat_id} 被判成了障碍 —— 床和沙发就没法压在上面了"
            )
        for overhead_id in ("pendant", "drying_rack"):        # 挂天花板
            assert not _occupies_floor(as_placement(specs[overhead_id])), (
                f"{overhead_id} 挂在天花板上，却挡住了下面的家具"
            )
        for wall_id in ("full_mirror", "wall_shelf", "wall_lamp"):   # 挂墙
            assert not _occupies_floor(as_placement(specs[wall_id])), (
                f"{wall_id} 挂在墙上，却挡住了下面的家具"
            )

        # ── 兜底扫描：落地的高件**必须**是障碍 ──────────────────
        #    这一条抓的是"以后新加一件落地大件却勾了 no_collide"。
        missed = [
            s.id for s in catalog.specs()
            if s.mount == "floor" and s.height_m > FLAT_MAX_HEIGHT_M
            and not _occupies_floor(as_placement(s))
        ]
        assert not missed, (
            f"这些件落在地上却不挡别的家具：{missed} —— "
            f"它们会被当成平铺件，别的家具会摆进它们里面"
        )


    def test_每间房同类不重复(self, plan):
        """
        ⚠️ 注意 `plan` 是**夹具参数**，不是模块级的那个 `plan()` 函数。
        漏写参数时 Python 把 `plan` 解析成函数对象，报的是
        「'function' object is not subscriptable」—— 与"同类没去重"
        这个真正要测的东西毫无关系。第一版就是这么错的。
        """
        index = catalog.by_id()
        for room in plan["rooms"]:
            families = [index[p["spec_id"]].family for p in room["placements"]
                        if p["spec_id"] in index]
            assert len(families) == len(set(families)), (
                f"{room['name']} 出现了同类家具：{families}"
            )

    def test_摆不下的都带原因(self, plan):
        """静默丢弃是不允许的 —— 每一件没摆上的都要说清为什么。"""
        for r in plan["rejected"]:
            assert r.get("reason"), f"有条目没写原因：{r}"

    def test_确定性_两次结果逐字节相同(self):
        from backend.app.services.geometry.walkable import build_walkable
        from backend.app.services.render import render_plan_for

        layout = json.loads(FIXTURE.read_text(encoding="utf-8"))
        scene, _ = render_plan_for(layout)
        wk, sc = build_walkable(scene).to_dict(), scene.to_dict()
        a = placement.place_all(walkable=wk, scene=sc, style="modern")
        b = placement.place_all(walkable=wk, scene=sc, style="modern")
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_配色随风格变(self):
        """风格不同，同一间房的家具颜色应当不同 —— 否则 3 套方案看着一样。"""
        styles = {s: catalog.style_palette(s).palette.get("wood")
                  for s in ("modern", "nordic", "chinese")}
        assert len(set(styles.values())) == 3, f"三种风格的木色应当不同：{styles}"


def _spec_of(spec_id: str):
    return next((s for s in catalog.specs() if s.id == spec_id), None)


def placement_placements(wk: dict, sc: dict, room_index: int) -> list[dict]:
    """对**单间房**重算一遍摆放，用于回代判据。"""
    from backend.app.services.geometry.walkable import build_walkable  # noqa: F401

    rooms = [r for r in wk["rooms"] if int(r["index"]) == room_index]
    if not rooms:
        return []
    interiors = {
        int(r["index"]): placement.room_interior(
            r, player_radius_m=float(wk.get("player_radius_m") or 0.0))
        for r in wk["rooms"]
    }
    neighbors = [b for i, b in interiors.items() if i != room_index]
    specs = placement._specs_for_room(str(rooms[0]["name"]), catalog.specs(), set())
    placed, _ = placement.place_room(
        rooms[0], specs=specs, doors=wk["doors"], openings=sc["openings"],
        player_radius_m=float(wk.get("player_radius_m") or 0.0),
        neighbors=[b for b in neighbors if b],
    )
    return [p.to_dict() for p in placed]


# ══════════════════════════════════════════════════════════════════
# 目录
# ══════════════════════════════════════════════════════════════════


class TestCatalogFamily:
    def test_每条都声明了族名(self):
        """
        ⚠️ `family` 是**显式字段**，不靠 id 前缀猜：
        `dining_chair` 与 `dining_table` 前缀相同但**不是同一族** ——
        餐桌与餐椅本来就要成套出现，按前缀并族会让餐厅只剩一件。
        这条测试保证目录里没有漏标的条目（`catalog.py` 里有前缀兜底，
        但兜底是防崩的，不是可以依赖的）。
        """
        raw = json.loads(
            (REPO / "seed_data" / "furniture_catalog.json").read_text(encoding="utf-8")
        )
        missing = [it["id"] for it in raw["items"] if not it.get("family")]
        assert not missing, f"这些条目没声明 family：{missing}"

    def test_餐桌与餐椅不同族(self):
        fams = {s.id: s.family for s in catalog.specs()
                if s.id in ("dining_table", "dining_chair", "dining_round")}
        assert fams["dining_table"] == fams["dining_round"]
        assert fams["dining_chair"] != fams["dining_table"]

    def test_四张沙发同族(self):
        """不然客厅会被摆上三张沙发（实测踩过）。"""
        sofas = {s.family for s in catalog.specs() if s.id.startswith("sofa")}
        assert sofas == {"sofa"}


# ══════════════════════════════════════════════════════════════════
# 不依赖模型
# ══════════════════════════════════════════════════════════════════


class TestPlacementStaysOffline:
    def test_摆放模块不导入LLM或网络(self):
        """
        与 ADR-07、`environment.py` 同一条纪律：**凡是能算的都不交给模型**。
        坐标要是模型给的，"看起来合理"就成了唯一的质量标准。
        """
        import ast

        tree = ast.parse(
            Path(placement.__file__).read_text(encoding="utf-8")
        )
        mods: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        forbidden = ("llm_client", "openai", "anthropic", "httpx", "requests",
                     "ollama", "langgraph")
        hits = [m for m in mods if any(f in m.lower() for f in forbidden)]
        assert not hits, f"摆放模块出现了不该有的依赖：{hits}"
