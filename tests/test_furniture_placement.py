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
from backend.app.services.furniture.placement import Box, Placement, _occupies_floor
import math
import colorsys

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
        # 2026-09-27 改：这条 reason 会显示在 3D 页的"未摆放"清单里，
        # 所以不再写 `family=sofa`（内部字段名 + 英文取值）。
        assert any("已经有同类家具" in r["reason"] for r in rejected)

    def test_占地额度按房间面积算而不是按件数(self):
        """
        ⚠️ 换过一次：原来是一个固定的件数上限（`MAX_PER_ROOM = 3`）。

        那个数字的毛病是**同样 3 件，放在 4.7㎡ 的卫生间和 18㎡ 的客厅里
        是两回事**。实测（演示户型）：件数上限从 3 提到无限，摆下的从
        25 件变 32 件 —— 多出来的 7 件全挤在儿童房（地毯/单人椅/斗柜/
        办公椅/书柜/搁板/衣帽架），主卧只从 3 件到 4 件。
        该多摆的没多摆，不该多摆的堆成仓库。

        现在是"家具占地 ≤ 房间内轮廓的 45%"。这条用例钉两件事：
        小房间收得住、大房间放得开。
        """
        specs = [_spec(f"f{i}", w=0.5, d=0.5, family=f"f{i}", need_wall=False)
                 for i in range(20)]

        small = placement.place_room(
            _room(x2=2.0, y2=2.0), specs=specs,
            doors=[], openings=[], player_radius_m=0.25,
        )[0]
        # 内轮廓 2.5×2.5=6.25㎡，额度 45% = 2.81㎡；每件 0.25㎡ → 最多 11 件，
        # 而且受几何约束会更少。关键断言是"远少于 20 件"。
        assert 0 < len(small) < 20, f"小球房摆了 {len(small)} 件"

        big = placement.place_room(
            _room(x2=8.0, y2=8.0), specs=specs,
            doors=[], openings=[], player_radius_m=0.25,
        )[0]
        assert len(big) > len(small), (
            f"大房间摆了 {len(big)} 件、小房间 {len(small)} 件 —— "
            f"面积额度没有生效，摆几件跟房间大小无关"
        )

    def test_比房间还大的家具报的是放不下而不是房间满了(self):
        """
        ⚠️ 判据顺序：**几何在前，预算在后。**

        预算判据挡在几何判据前面的话，一个比房间还大的柜子会被报成
        「这间房的家具占地已达上限」—— 听起来像房间满了，其实是这件
        根本放不下。两种原因都会让用户做出不同的决定（换家具 vs 换房间）。
        """
        placed, rejected = placement.place_room(
            _room(x2=2.0, y2=2.0), specs=[_spec("huge", w=3.0, d=1.0)],
            doors=[], openings=[], player_radius_m=0.25,
        )
        assert placed == []
        assert rejected and "超出房间内轮廓" in rejected[0]["reason"], (
            f"报的是：{rejected[0]['reason'] if rejected else '(空)'}"
        )

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
        """
        ⚠️ 传的是**门的位置**，不是"这面墙上有没有门"。

        初版 `_door_distance` 量的是"这面墙自己那扇门"，没门的墙一律
        返回 1e6 —— 于是所有没门的墙并列，排序完全由固定顺序决定，
        这个偏好等于没生效（实测期望北墙、实际选了东墙）。
        这条用例现在是它的回归防线。
        """
        rect = Box(0, 0, 4.0, 3.0)
        blockers = {k: {"has_door": False, "has_window": False, "door_at": None}
                    for k in placement._SIDES}
        blockers["S"]["has_door"] = True
        order = placement._preference_order(
            _spec("sofa", w=2.0, d=0.9, prefer="opposite_door"),
            rect, blockers, [(2.0, 0.0)],     # 门在南墙正中
        )
        assert order[0] == "N", f"应当选离门最远的那面墙，实际 {order}"

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
            #    这里照着复现，否则回代会把"地毯压住了床脚"报成违规。
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
        ⚠️ **这条守的是"摆放真的放得下"这个结果，不是判据本身。**

        实测踩过：儿童房里地毯铺在正中、衣柜靠墙，两者**本体并不重叠**，
        但衣柜的前方净空伸进了地毯的范围 → 判据报「前方净空被别人占了」
        → 衣柜被拒。一间卧室因为一张地毯丢了衣柜。

        现在平铺件（`no_collide`：地毯/地台）不进障碍表，所以：
        **平铺件与实体件可以共处**，而实体件之间照旧互不重叠。
        """
        import json as _json

        from backend.app.services.geometry.walkable import build_walkable
        from backend.app.services.render import render_plan_for

        layout = _json.loads(FIXTURE.read_text(encoding="utf-8"))
        scene, _ = render_plan_for(layout)
        walk = build_walkable(scene)
        wk, sc = walk.to_dict(), scene.to_dict()

        solid_by_room: dict[int, list[Box]] = {}
        flat_by_room: dict[int, list[Box]] = {}
        for room in wk["rooms"]:
            idx = int(room["index"])
            for p in placement_placements(wk, sc, idx):
                box = Box(p["x"] - p["w"] / 2, p["y"] - p["d"] / 2,
                          p["x"] + p["w"] / 2, p["y"] + p["d"] / 2)
                (flat_by_room if p.get("no_collide") else solid_by_room) \
                    .setdefault(idx, []).append(box)

        # 实体件之间仍然互不重叠（这一条**不能**被"平铺件豁免"带松）
        for idx, boxes in solid_by_room.items():
            for i, a in enumerate(boxes):
                for b in boxes[i + 1:]:
                    assert not a.overlaps(b), (
                        f"房间 {idx} 里两件实体家具重叠了：{a.as_dict()} 与 {b.as_dict()}"
                    )
    def test_平铺件可以压在实体件上而实体件可以压在它上面(self):
        """
        ⚠️ **这条替换掉了原来一个"靠样本碰巧成立"的前提。**

        原先是同一个用例里的第二段断言：要求真实户型里**必须存在**
        "平铺件与实体件相交"的局面，用来证明豁免规则被覆盖到。

        而那个前提**是被一个 bug 满足的**：`shower`（淋浴房，0.9×0.9×2.0m
        的实心玻璃房）在目录里被误标成 `no_collide`，于是它被算作"平铺件"，
        和浴室柜相交了 0.45㎡ —— 前提正是靠这个错误的相交成立的。
        修掉那个误标（见 `placement._occupies_floor()`）之后，前提自然不成立。

        **"样本里恰好出现某种局面"不该是规则被覆盖的证据** —— 样本一变，
        这条断言要么空跑、要么误报。所以改成直接构造，与样本无关。
        """
        interior = Box(0.0, 0.0, 4.0, 4.0)
        rug = Box(1.0, 1.0, 3.0, 3.0)          # 平铺件：地毯
        bed = Box(1.0, 1.0, 3.0, 3.0)          # 实体件：双人床，与地毯完全重合

        # 障碍表里的件 = `_occupies_floor()` 为真的那些（见 `place_room` 的
        # `item_boxes`）。平铺件不在其中 —— **这就是"床可以压在地毯上"的机制**：
        # 地毯压根没进那张表，`violations` 当然不会报重叠。
        rug_p = Placement(
            spec_id="rug", label="地毯", room_index=0, room_name="样本",
            box=rug, rot_deg=0, mount="floor", height_m=0.02,
            color_role="fabric", y_offset_m=0.0, no_collide=True,
        )
        obstacles = [p.box for p in (rug_p,) if _occupies_floor(p)]
        assert obstacles == [], "平铺件进了障碍表 —— 床就没法压在地毯上了"
        assert placement.violations(bed, interior=interior, placed=obstacles) == [], (
            "床压在地毯上被判违规了 —— 而地毯本来就不该进障碍表"
        )

        # ⚠️ 但这条**不能被带松**：实体件之间照旧互斥。
        #    两件事分开断言，是因为它们守的是不同的东西。
        another_bed = Box(0.0, 0.0, 2.0, 2.0)
        assert "与已摆放的家具重叠" in placement.violations(
            bed, interior=interior, placed=[another_bed]
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

    def test_靠墙的落点不会把家具推出墙外(self):
        """
        ⚠️ **实测踩过。** 落点里有两个"墙长 × 0.25 / 0.75"的位置，
        在墙长与家具宽度接近时会把它推出墙外 —— 卫生间放浴室柜（宽 1.0m）
        到一面 1.70m 的墙上，0.25 落点是墙起点 +0.43m，柜子左端伸出去 7.5cm。
        判据如实报「超出房间内轮廓」，但那不是判据太严，**是候选位置本身
        不合法**：一个必然被拒的位置不该出现在候选里，它只会把真正的
        原因（这面墙放不下）淹掉。
        """
        rect = Box(0.0, 0.0, 3.0, 1.70)
        spec = _spec("vanity", w=1.0, d=0.5)
        for key in ("S", "N", "W", "E"):
            line, inward, lo, hi = placement._side_line(rect, key)
            for off in placement._wall_offsets(spec, rect, key):
                box = placement._wall_box(spec, rect, key, off)
                along_lo = box.x1 if key in ("S", "N") else box.y1
                along_hi = box.x2 if key in ("S", "N") else box.y2
                assert along_lo >= lo - 1e-9 and along_hi <= hi + 1e-9, (
                    f"{key} 墙上的落点 {off} 把家具推出了墙外："
                    f"[{along_lo:.3f}, {along_hi:.3f}] 不在 [{lo}, {hi}] 内"
                )

    def test_靠墙的落点不止墙中点一个(self):
        """
        小房间（实测卫生间 2.49×1.70m）里洁具是**靠角**摆的。
        只试墙中点等于把这个解从搜索空间里删掉 —— 实测卫生间因此
        只摆得下 1 件（淋浴房），马桶和浴室柜全被拒。
        """
        rect = Box(0.0, 0.0, 3.0, 2.0)
        offs = placement._wall_offsets(_spec("cab", w=0.6, d=0.4), rect, "S")
        assert len(offs) >= 3, f"南墙上只试了 {len(offs)} 个落点：{offs}"
        assert offs[0] == 1.5, "墙中点必须排第一（能居中的就居中）"
        assert offs[0] != min(offs), "没有靠角的落点"

    def test_房间类型能补上名字对不上的缺口(self):
        """
        ⚠️ **实测踩过：儿童房摆了 8 件东西，没有一件是床。**

        原因：目录的 `rooms` 是按中文房间名写的（"主卧"/"次卧"），而解析
        给的房间名是自由的。名字一条都命中不到时，只能退到"不限房间"的
        大杂烩里挑 —— 于是儿童房拿到的是地毯、斗柜、书柜、搁板、绿植、
        垃圾桶、壁灯、床头柜，**唯独没有床**。而它其实是一间卧室。

        现在按 `kind` 一起匹配（bedroom → 卧室），并且目录里也把
        儿童房补齐了。
        """
        from backend.app.services.furniture import catalog

        all_specs = catalog.specs()
        # 一个目录里从来没有出现过的房间名 —— 只能靠 kind 救
        specs = placement._specs_for_room("婴儿房", "bedroom", all_specs, set())
        assert specs, "婴儿房一条候选都没有"
        assert specs[0].family == "bed", (
            f"婴儿房排第一的是 {specs[0].id}（family={specs[0].family}），"
            f"应当先试床 —— 卧室里床是主角"
        )

        # 目录里明确写了 儿童房 的也要能命中原生条目
        kids = placement._specs_for_room("儿童房", "bedroom", all_specs, set())
        assert kids[0].family == "bed", f"儿童房先试的是 {kids[0].id}"

        # 没有 kind 也没有名字时不该崩，只是退回『不限房间』的池子
        generic = placement._specs_for_room("某个怪名字", "", all_specs, set())
        assert generic, "名字与类型都不认识时应当退回『不限房间』的池子"

    def test_每间房同类不重复(self, plan):
        """
        ⚠️ 注意 `plan` 是**夹具参数**，不是模块级的那个 `plan()` 函数。
        漏写参数时 Python 把 `plan` 解析成函数对象，报的是
        「'function' object is not subscriptable」—— 与"同类没去重"
        这个真正要测的东西毫无关系。第一版就是这么错的。
        """
        index = {s.id: s for s in catalog.specs()}
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
    specs = placement._specs_for_room(
        str(rooms[0]["name"]), str(rooms[0].get("kind") or ""),
        catalog.specs(), set())
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



# ══════════════════════════════════════════════════════════════════
# 3D 表面色：与界面配色是两回事
# ══════════════════════════════════════════════════════════════════


def _rgb(hexv: str) -> tuple[float, float, float]:
    h = hexv.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _lum(hexv: str) -> float:
    vals = []
    for v in _rgb(hexv):
        vals.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]


def _ratio(a: str, b: str) -> float:
    x, y = sorted([_lum(a), _lum(b)], reverse=True)
    return (x + 0.05) / (y + 0.05)


def _lab(hexv: str) -> tuple[float, float, float]:
    h = hexv.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    rgb = [((v + 0.055) / 1.055) ** 2.4 if v > 0.04045 else v / 12.92 for v in rgb]
    r, g, b = rgb
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116  # noqa: E731
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _de(a: str, b: str) -> float:
    return math.dist(_lab(a), _lab(b))


class TestSurfaceColors:
    """
    ⚠️ 这批用例来自一次**看出来的问题**：3D 家具画出来了（几何逐件验过
    24/24），但俯视图里跟地面糊成一片。

    量下来根因是**用错了色板**：`palette` 是给 2D 界面卡片用的，要求是
    "跟白底拉得开"；而 3D 表面要求是"跟地面拉得开"。实测 modern 的
    `fabric` 对地面只有 **1.01:1** —— 沙发和地面同色，等于没画。

    所以目录里另给一套 `surface` + `surface_floor`，并在这里钉住两个约束。
    """

    STYLES = ("modern", "nordic", "chinese")

    def test_界面配色与3D表面色确实不同(self):
        """防止有人"顺手统一"成一套 —— 统一的那一刻这两个约束就互斥了。"""
        for style in self.STYLES:
            p = catalog.style_palette(style)
            assert p.surface, f"{style} 没有 3D 表面色"
            assert p.surface != p.palette, f"{style} 的 surface 与 palette 相同"
            assert p.surface_floor and p.surface_floor != p.floor, (
                f"{style} 的 3D 地面与界面地面相同 —— 浅地面会让浅色家具糊掉"
            )

    def test_每个角色对3D地面至少2比1(self):
        """
        「家具能从地面里分出来」的可计算判据。1.01:1 是同色，2:1 是
        大面积色块能明确分开的量级。
        """
        for style in self.STYLES:
            p = catalog.style_palette(style)
            for role, hexv in p.surface.items():
                r = _ratio(hexv, p.surface_floor)
                assert r >= 2.0, (
                    f"{style}.{role} 对 3D 地面只有 {r:.2f}:1（要求 ≥2）—— "
                    f"这件家具在场景里看不出来"
                )

    def test_角色之间分得开(self):
        """
        只跟地面拉开还不够：六件家具要是彼此同色，场景就是一坨。
        用 Lab ΔE，**大色块的恰可分辨差约 2.3**，这里要求 ≥6（约三倍余量）。

        ⚠️ 阈值 6 是判据不是标准：最初我按 ΔE≥12 去调，调到发现
        在「≥2:1」的亮度约束下**六个角色根本排不下**（亮度区间被对比度
        吃掉一半，剩下的只够放三个），于是回头核实了 JND 才降到 6。
        """
        for style in self.STYLES:
            p = catalog.style_palette(style)
            roles = list(p.surface)
            worst = min(
                (a, b, _de(p.surface[a], p.surface[b]))
                for i, a in enumerate(roles) for b in roles[i + 1:]
            )
            assert worst[2] >= 6.0, (
                f"{style} 的 {worst[0]}/{worst[1]} 只有 ΔE={worst[2]:.1f}，分不出来"
            )

    def test_每个角色的亮度落在可用区间里(self):
        """
        ⚠️ **这条是补上"家具看着像地上的洞"那个问题的。**

        「对地面 ≥2:1」和「角色间 ΔE ≥ 6.6」这两条**只在数值上成立是不够的**：
        手工调那一版把暗的往黑里推、亮的往白里推，于是六个角色里
        **3 个接近纯黑、1 个接近纯白**：

            modern  metal #050506（相对亮度 0.0015）
                    green #0C170B（0.0072）
                    wood  #271D11（0.0135）
                    white #FCFCFC（0.9734）

        数值上它满足了那两条约束（对地面 5.16~6.35:1，ΔE 最小 9.17）——
        **约束全过，画面上一团黑。** 3D 俯瞰时暗色家具看着像地上的洞
        （`logs/d2-fly-topdown.png`）。

        所以补一条：每个角色的相对亮度落在 **[0.05, 0.72]**。
        区间不是查来的标准，是从"3D 里看不看得清"反推的经验值 ——
        0.05 ≈ sRGB 0x45（再暗在室内光下就是黑），0.72 ≈ sRGB 0xD8
        （再亮就与浅色墙面 `#F2ECE1`（0.84）糊在一起了）。

        ⚠️ 取值范围与推导过程见 `scripts/derive_surface_palette.py`：
           这套值是在这四条约束下**搜出来的**，不是手调的；
           而且搜索证明了一件事 —— 为了让"石面"和"白"这两个中性浅色
           也能拉开 6.6，**地面必须压暗**（现行地面亮度 0.26~0.27，
           原来 0.2775/0.2933 在亮侧只剩 5.86/4.18 的 ΔL* 空间，
           永远达不到 6.6）。
        """
        for style in self.STYLES:
            p = catalog.style_palette(style)
            for role, hexv in p.surface.items():
                y = _lum(hexv)
                assert 0.05 <= y <= 0.72, (
                    f"{style}.{role} = {hexv} 的相对亮度是 {y:.4f}，"
                    f"落在 [0.05, 0.72] 之外 —— "
                    f"{'接近纯黑，3D 里看着像地上的洞' if y < 0.05 else '接近纯白，与浅色墙面糊在一起'}"
                )

    def test_石面是中性灰不是带色的(self):
        """
        ⚠️ **这条是被"搜索钻空子"逼出来的，两次。**

        石面（台面、淋浴房）应当是中性灰。前两版的写法都留了余量：

          第一版  饱和度 ≤0.10 + 色相 30~220  → 搜出 #C7D0C6（H=114°，淡绿白）
          第二版  饱和度 ≤0.06 + 色相不限     → 搜出 #C8CEC8（H=120°）

        原因是 ΔE 里**色相也是分量**：把饱和度顶到上限就能从"白"那里
        多抠一点色差。数值全合格、语义全错。

        教训：**约束留下的一点余量，搜索引擎一定会用掉。**
        所以这里卡到 0.03（人眼在这个亮度上基本看不出色相）。
        """
        for style in self.STYLES:
            p = catalog.style_palette(style)
            hexv = p.surface["stone"]
            # ⚠️ `colorsys.rgb_to_hls` 返回的是 **(h, l, s)**，不是 (h, s, l)。
            #    解包顺序写错的话，断言的"饱和度"其实是明度 ——
            #    实测第一版就是这样：报出 s=0.780，而那是 #C6C6C8 的明度。
            #    两个字段都是 0~1 的浮点数，**错了也不会报类型错**。
            h, _l, s = colorsys.rgb_to_hls(*_rgb(hexv))
            assert s <= 0.03, (
                f"{style}.stone = {hexv} 的饱和度 {s:.3f} > 0.03 —— "
                f"色相 H={h * 360:.0f}°，它已经是一块**带色的**浅色了，"
                f"而石面应当是中性灰"
            )

    def test_3D地面比界面地面深(self):
        """
        ⚠️ 这条是**必须让步的那个量**。试过不动地面、只压家具：结果
        深色角色全塌成纯黑（metal #171717、wood #382E22），浅色三个
        角色全挤到纯白 —— 因为「对地面 ≥2:1」把亮度区间从中间劈成两半，
        每边只剩一半可用。地面压到中调，两边才都有空间。
        """
        for style in self.STYLES:
            p = catalog.style_palette(style)
            assert _lum(p.surface_floor) < _lum(p.floor), (
                f"{style} 的 3D 地面没有比界面地面深"
            )

    def test_缺字段时回落而不是崩(self):
        """目录是人手维护的；漏填不该让整个场景建不起来。"""
        p = catalog.StylePalette(label="x", palette={"wood": "#FFFFFF"},
                                 wall="#FFFFFF", floor="#EEEEEE")
        assert p.surface == {}
        assert p.surface_floor == ""

    def test_摆放接口给的是surface不是palette(self):
        """
        ⚠️ **接线也是判据的一部分。** 后端目录里两套色都齐了、约束也过了，
        而 `place_all` 里少写一个 `.surface` —— 3D 立刻回到糊成一片，
        且**没有任何测试会红**（前面的用例全是断言目录数据，不是断言接口输出）。

        这类"数据对了但没用上"的缺口，正是本项目一路在防的。
        """
        layout = json.loads(FIXTURE.read_text(encoding="utf-8"))
        from backend.app.services.geometry.walkable import build_walkable
        from backend.app.services.render import render_plan_for

        scene, _ = render_plan_for(layout)
        wk, sc = build_walkable(scene).to_dict(), scene.to_dict()
        for style in self.STYLES:
            out = placement.place_all(walkable=wk, scene=sc, style=style)
            p = catalog.style_palette(style)
            assert out["palette"] == p.surface, (
                f"{style} 的接口给的不是 surface（3D 会糊）"
            )
            assert out["surface_floor"] == p.surface_floor
            # 反过来确认它不是 palette —— 否则上面那条会因为"两套一样"而假通过
            assert out["palette"] != p.palette
