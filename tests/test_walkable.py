"""
可漫游性（第一人称行走的几何前提）。

═══════════════════════════════════════════════════════════════════
这个文件不检查"函数返回了东西"，而是**真的模拟一个人走一遍**
═══════════════════════════════════════════════════════════════════
`walkable.py` 里最容易错、也最难发现的一步是**把门洞从墙上切掉**。

不切的话：墙是对的、2D 图上门也画得好好的、碰撞线段也生成了一堆 ——
所有中间产物单看都正常，唯独**人走过去过不去**。而且不报错。

所以这里的核心断言不是"切口长度等于门宽"，而是：

    把户型铺成网格，从出生点做**洪水填充**，看能不能走到每一间房。

这是把"玩家能不能走过去"这件事真的模拟出来。切口算错了（位置偏了、
没切、切多了），洪水填充都会在多边形上绕不过去。

═══════════════════════════════════════════════════════════════════
第二组：不该通的地方必须通不过
═══════════════════════════════════════════════════════════════════
只测"能走过去"是不够的 —— 把碰撞线段全删掉也能过。所以还要反向测：
**没有门相连的两间房之间，直线必须被墙挡住。**

两个方向都测，才说明碰撞几何既"该开的地方开了"、又"该堵的地方堵着"。
"""

from __future__ import annotations

import copy
import json
import math
from collections import deque
import pathlib

import pytest

from backend.app.services.geometry import normalize_layout, walkable
from backend.app.services.geometry.normalize import (
    WALL_SNAP_TOLERANCE_M,
    Vec2,
    WallSeg,
    _locate_on_wall,
    wall_point_at,
)
from backend.app.services.geometry.walkable import (
    DEFAULT_PLAYER_RADIUS_M,
    build_walkable,
)

#: 一次真实解析的结果，未经修饰
REAL_LAYOUT = {
    "rooms": [
        {"name": "卧室A", "type": "bedroom", "area": 11.7, "bbox": [40, 40, 380, 300]},
        {"name": "卧室B", "type": "bedroom", "area": 11.1, "bbox": [40, 300, 380, 545]},
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
    "dimensions": [{"label": "8200", "value": 8.2}],
    "total_area": 45.9,
    "confidence": 0.45,
}


def _walk(layout: dict | None = None):
    return build_walkable(normalize_layout(layout or REAL_LAYOUT))


# ── 网格与行走模拟 ────────────────────────────────────────────────


def _xy(v) -> tuple[float, float]:
    """`Vec2` 和元组都吃 —— 测试里两种都会传进来。"""
    return (v.x, v.y) if hasattr(v, "x") else (v[0], v[1])


def _dist_to_seg(p, a, b) -> float:
    a, b = _xy(a), _xy(b)
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _clearance(p, segs) -> float:
    """点到最近碰撞面的距离。**小于玩家半径就是撞墙。**"""
    return min(_dist_to_seg(p, s.a, s.b) for s in segs) if segs else float("inf")


def _flood_reachable(w, step: float = 0.15) -> set[tuple[int, int]]:
    """
    从出生点做洪水填充，模拟一个半径 `player_radius` 的人能走到哪些格子。

    ⚠️ 这是这个文件的核心。它不是"检查数据结构"，而是**真的走一遍**。
    """
    segs = w.collision
    r = w.player_radius_m
    got: set[tuple[int, int]] = set()

    def key(p) -> tuple[int, int]:
        return (round(p[0] / step), round(p[1] / step))

    def ok(p) -> bool:
        return _clearance(p, segs) > r

    start = (round(w.spawn.x / step) * step, round(w.spawn.y / step) * step)
    if not ok(start):
        return got

    q = deque([key(start)])
    got.add(key(start))
    while q:
        cx, cy = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nk = (cx + dx, cy + dy)
            if nk in got:
                continue
            p = (nk[0] * step, nk[1] * step)
            if not (-step <= p[0] <= 40 and -step <= p[1] <= 40):
                continue
            if ok(p):
                got.add(nk)
                q.append(nk)
    return got


# ══════════════════════════════════════════════════════════════════
# 核心：真的走一遍
# ══════════════════════════════════════════════════════════════════


class TestYouCanActuallyWalk:
    def test_能从出生点走到每一间房(self):
        """
        ⚠️ **这个文件的理由。**

        洪水填充模拟一个半径 25cm 的人从出生点出发。三间房都必须到得了。
        门洞没切干净 / 切歪了 / 切多了，这里都会红。
        """
        w = _walk()
        assert w.ok, f"这份户型应当可漫游，却判成不可：{w.issues}"

        reached = _flood_reachable(w)
        assert reached, "出生点本身就在墙里 —— 走不出去"

        for room in w.rooms:
            k = (round(room.center.x / 0.15), round(room.center.y / 0.15))
            assert k in reached, (
                f"走不到「{room.name}」（中心 {room.center.as_list()}）—— "
                f"门洞多半没切开"
            )

    def test_每扇门都开在墙上且洞够宽(self):
        """
        逐扇门确认两件事：

        ① **门确实在墙上** —— 门中心到最近碰撞面的距离应当在半个门宽
           到一米之间。太远说明门被定位到了房间中间（不是墙上）；
           太近（≈半个墙厚 0.1m）说明洞没切开。
        ② **洞比人宽** —— 否则看得见进不去。
        """
        w = _walk()
        assert w.doors, "没有识别出可通行的门"

        for d in w.doors:
            c = (d.position.x, d.position.y)
            clear = _clearance(c, w.collision)
            assert clear >= d.width_m / 2 - 0.03, (
                f"门 {d.door_index} 处净空 {clear:.3f}m < 半个门宽 "
                f"{d.width_m / 2:.3f}m —— 洞没切开"
            )
            assert clear <= 1.0, (
                f"门 {d.door_index} 的中心离最近的墙有 {clear:.3f}m，"
                f"不像在墙上 —— 门的定位算错了"
            )
            assert d.width_m > 2 * w.player_radius_m

    def test_每间房的中心都站得住(self):
        w = _walk()
        for room in w.rooms:
            assert _clearance((room.center.x, room.center.y), w.collision) > (
                w.player_radius_m
            ), f"「{room.name}」的中心在墙里"

    def test_没有门相连的房间之间是实心的(self):
        """
        反向确认 —— **只测"过得去"是不够的**：把所有碰撞线段删掉也能过。

        客厅与卧室A、卧室B 之间有门；但**任意两间房之间不该有一条直着
        穿过去的缝**。这里验证：从每间房向另一间房的净空矩形中心连线，
        一定会被碰撞面挡住。
        """
        w = _walk()
        rooms = w.rooms
        assert len(rooms) >= 3

        for i, a in enumerate(rooms):
            for b in rooms[i + 1:]:
                blocked = _line_blocked(
                    (a.center.x, a.center.y), (b.center.x, b.center.y),
                    w.collision, w.player_radius_m,
                )
                assert blocked, (
                    f"「{a.name}」与「{b.name}」之间有一条直通无阻的缝 —— "
                    f"玩家可以不走门直接穿过去，说明碰撞几何漏了"
                )

    def test_外墙围得住(self):
        """从户型里任何一个点往外走，都必须撞到外墙。"""
        w = _walk()
        for room in w.rooms:
            outside = (room.center.x + 30.0, room.center.y + 30.0)
            assert _line_blocked(
                (room.center.x, room.center.y), outside,
                w.collision, w.player_radius_m,
            ), f"从「{room.name}」可以径直走到户型外"


def _line_blocked(p, q, segs, radius: float, samples: int = 400) -> bool:
    """沿直线采样，看有没有一段离碰撞面近于玩家半径。"""
    for i in range(samples + 1):
        t = i / samples
        pt = (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
        if _clearance(pt, segs) <= radius:
            return True
    return False


# ══════════════════════════════════════════════════════════════════
# 门洞切割：守恒律
# ══════════════════════════════════════════════════════════════════


class TestDoorGaps:
    """
    门洞切割的**精确**判据。这些数字是先跑一遍量出来的，不是"看着差不多"。

    ══════════════════════════════════════════════════════════════════
    为什么"守恒律"和"端点对齐"两条都要有 —— 实测过它们分工不同
    ══════════════════════════════════════════════════════════════════
    故意把切割做错，看哪条抓得住：

        变异                        守恒差额    端点偏移    谁抓住
        切口加宽 0.6m               1.8000 m    0.6000 m    两条都抓
        切口按比例错位             0.1509 m    0.9887 m    两条都抓
        切口整体平移 0.3m          0.0000 m    0.3000 m    **只有端点抓**

    第三行是关键：**长度完全正确、位置整体挪了** —— 守恒律一点问题都没有。
    而那正是最阴的一种错：门开着，尺寸也对，只是不在该在的地方 ——
    表现出来是"这一侧的墙多出一截、人得侧身挤过去"，画面上看不出来。

    所以两条不是冗余，是互补：**守恒管宽度，端点管位置。**
    """

    def test_门洞确实是空心的(self):
        """
        门中心到最近碰撞面的距离，应当 ≥ 半个门宽。

        这条直接说"这儿有个洞，洞宽至少是门宽"。
        没切 → 距离是墙厚的一半（0.1m），远小于 0.45m，立刻红。
        """
        scene = normalize_layout(REAL_LAYOUT)
        w = build_walkable(scene)
        for op in scene.openings:
            if op.kind != "door" or op.wall_index < 0:
                continue
            c = (op.center.x, op.center.y)
            clearance = min(
                _dist_to_seg(c, (s.a.x, s.a.y), (s.b.x, s.b.y)) for s in w.collision
            )
            assert clearance >= op.width_m / 2 - 0.03, (
                f"门 {op.center.as_list()} 处的净空只有 {clearance:.3f}m，"
                f"小于半个门宽 {op.width_m / 2:.3f}m —— 墙没切开"
            )

    def test_门洞两端正好落在碰撞线段的端点上(self):
        """
        ⚠️ **只判长度是不够的，还要判位置。**

        切口的**两端**必须精确落在门洞的两端。切偏了的话：长度可能仍然
        对得上，但一侧的墙垛变短、另一侧多出一截 —— 表现为"门开着，
        但只能侧着身子从一半的位置挤过去"，而画面上完全看不出来。
        """
        scene = normalize_layout(REAL_LAYOUT)
        w = build_walkable(scene)
        ends = [(s.a.x, s.a.y) for s in w.collision] + \
               [(s.b.x, s.b.y) for s in w.collision]

        for op in scene.openings:
            if op.kind != "door" or op.wall_index < 0:
                continue
            placed = wall_point_at(scene.walls[op.wall_index], op.offset_along_wall_m)
            assert placed is not None
            p, u = placed
            for sign in (1, -1):
                expect = (p.x + u.x * op.width_m / 2 * sign,
                          p.y + u.y * op.width_m / 2 * sign)
                d = min(math.dist(expect, q) for q in ends)
                assert d < 0.03, (
                    f"门洞端点 {expect} 附近没有碰撞线段端点（最近 {d:.3f}m）—— "
                    f"切口位置偏了"
                )

    def test_墙体总长守恒(self):
        """
        `碰撞线段总长 + 门洞总长 == 墙体总长`。

        **这条守恒律很强**：多切、少切、切重、门互相重叠，都会破坏它。
        而且它不针对具体哪扇门 —— 换个户型照样成立。实测差额 0（浮点下 8 位）。
        """
        scene = normalize_layout(REAL_LAYOUT)
        w = build_walkable(scene)

        wall_total = sum(
            math.dist((a.x, a.y), (b.x, b.y))
            for wall in scene.walls for a, b in wall.segments()
        )
        collision_total = sum(s.length_m for s in w.collision)
        door_total = sum(
            o.width_m for o in scene.openings
            if o.kind == "door" and o.wall_index >= 0
        )

        assert collision_total + door_total == pytest.approx(wall_total, abs=1e-6), (
            f"碰撞 {collision_total:.5f} + 门洞 {door_total:.5f} "
            f"≠ 墙总长 {wall_total:.5f}"
        )

    def test_外墙没有被切开(self):
        """实测这份户型三扇门都在内墙上 —— 外墙必须完整。"""
        w = _walk()
        outer = [s for s in w.collision if s.wall_index == 0]
        assert len(outer) == 4, f"外墙被切成了 {len(outer)} 段，应当是完整 4 段"
        total = sum(s.length_m for s in outer)
        assert total == pytest.approx(2 * (7.891 + 5.817), abs=0.05)

    def test_不产生零长度碎段(self):
        """
        ⚠️ 门开在墙角附近时，切口会越过线段端点，切出**负长度**区间。

        而判长度的写法是 `>= MIN_SEGMENT_M`，负数会被放过去 ——
        于是碰撞几何里多出一条方向相反的线段，表现为贴着墙角原地卡住。
        """
        for s in _walk().collision:
            assert s.length_m > 0.01, f"出现了零长/负长碰撞段：{s.length_m:.4f}"

    def test_门开在墙角也不崩(self):
        """把门挪到墙的最末端（墙角），切口必然越过端点。"""
        layout = copy.deepcopy(REAL_LAYOUT)
        layout["doors"] = [{"position": [380, 43], "width": 0.0}]
        w = build_walkable(normalize_layout(layout))
        assert w.collision, "墙角开门之后碰撞线段全没了"
        for s in w.collision:
            assert s.length_m > 0.01

    def test_两扇门挨着时切口合并(self):
        """
        两扇门开得很近时，切口区间会重叠。不合并的话会切出
        中间一小段"本不该存在"的墙 —— 表现为门中间莫名其妙有根柱子。
        """
        layout = copy.deepcopy(REAL_LAYOUT)
        # 在墙#1 上开两个几乎重叠的门（像素位置差 3px ≈ 3.5cm）
        layout["doors"] = [
            {"position": [367, 197], "width": 0.0},
            {"position": [367, 200], "width": 0.0},
        ]
        w = build_walkable(normalize_layout(layout))
        for s in w.collision:
            assert s.length_m > 0.01
        # 两扇门重叠后，墙#1 上只该有 2 段（而不是 3 段夹一条缝）
        wall1 = [s for s in w.collision if s.wall_index == 1]
        assert len(wall1) == 2, f"重叠的门切出了 {len(wall1)} 段，应当合并成 2 段"

    def test_门宽为零时用兜底值(self):
        """实测三扇门的 width 全是 0。兜底之后仍然要切出可通行的口。"""
        w = _walk()
        assert w.doors
        for d in w.doors:
            assert d.width_m > 2 * w.player_radius_m, (
                f"门宽 {d.width_m} 不足以让半径 {w.player_radius_m} 的人通过"
            )


# ══════════════════════════════════════════════════════════════════
# 连通图与出生点
# ══════════════════════════════════════════════════════════════════


class TestGraphAndSpawn:
    def test_三间房全连通(self):
        w = _walk()
        assert len(w.rooms) == 3
        assert len(w.doors) == 3
        assert all(r.reachable for r in w.rooms), "有房间走不到"

    def test_门连的是两间不同的房(self):
        """回归：探针沿墙方向而不是法向时，两侧会落进同一间房，连通图全是自环。"""
        for d in _walk().doors:
            assert d.from_room != d.to_room, (
                f"门 {d.door_index} 的两侧是同一间房 —— 探针方向错了"
            )

    def test_出生点选在门最多的房间(self):
        """
        从"门最多的那间"出发，而不是第一间或最大的。

        从只有一扇门的卧室醒来，第一印象是"怎么出不去"。

        ⚠️ **通过性空间（走廊/玄关）不参选。** 实测踩过：星形连通的户型里
        每个房间的门都开向走廊，于是走廊门最多、永远被选中 ——
        而按 `G` 下到地面后第一眼是一面墙。规则因此收窄了一格，
        原意（不要死胡同）保留。见 `walkable._PASSAGE_ROOM_NAMES`。
        """
        from backend.app.services.geometry.walkable import _is_passage

        w = _walk()
        spawn_room = next(r for r in w.rooms if r.index == w.spawn_room)
        degree = {}
        for d in w.doors:
            degree[d.from_room] = degree.get(d.from_room, 0) + 1
            degree[d.to_room] = degree.get(d.to_room, 0) + 1

        candidates = [r for r in w.rooms if not _is_passage(r)] or list(w.rooms)
        best_deg = max(degree.get(r.index, 0) for r in candidates)
        assert degree.get(spawn_room.index, 0) == best_deg, (
            f"出生在「{spawn_room.name}」（{degree.get(spawn_room.index, 0)} 扇门），"
            f"但候选里门最多的是 {best_deg} 扇"
        )
        assert spawn_room in candidates, (
            f"出生点落在了通过性空间「{spawn_room.name}」—— 站进去看不到什么"
        )

    def test_出生点站得住(self):
        w = _walk()
        assert _clearance((w.spawn.x, w.spawn.y), w.collision) > w.player_radius_m, (
            "出生点落在墙里 —— 一进去就卡住"
        )

    def test_初始朝向面向房间纵深(self):
        """朝向错了不报错，只是第一眼看到 0.1m 外的一堵墙。"""
        w = _walk()
        room = next(r for r in w.rooms if r.index == w.spawn_room)
        x1, y1, x2, y2 = room.free_rect
        longer_axis_deg = 0.0 if (x2 - x1) >= (y2 - y1) else 90.0
        assert w.spawn_yaw_deg == longer_axis_deg


# ══════════════════════════════════════════════════════════════════
# 降级：不能走的时候必须说出来
# ══════════════════════════════════════════════════════════════════


class TestFallsBackHonestly:
    """
    ★ 这些是"能不能走"的判据。任一不成立就必须 `ok=False`，
    让前端降级成自由视角（可穿墙飞），**而不是**硬做第一人称然后把
    用户关在房间里。

    `ok=False` 时 `issues` **必须非空** —— 静默降级 = 欺骗用户（AC-17）。
    """

    def test_没有墙时不可漫游(self):
        layout = copy.deepcopy(REAL_LAYOUT)
        layout["walls"] = []
        w = build_walkable(normalize_layout(layout))
        assert not w.ok
        assert w.issues, "判成不可漫游却不给原因"
        assert any("闭合" in i or "碰撞" in i for i in w.issues)

    def test_没有房间时不可漫游(self):
        w = build_walkable(normalize_layout({"walls": REAL_LAYOUT["walls"]}))
        assert not w.ok and w.issues

    def test_门太窄不可漫游(self):
        """门宽 0.3m < 玩家直径 0.5m —— 看得见进不去。"""
        layout = copy.deepcopy(REAL_LAYOUT)
        layout["doors"] = [{"position": [367, 197], "width": 0.3},
                           {"position": [142, 315], "width": 0.3},
                           {"position": [400, 407], "width": 0.3}]
        w = build_walkable(normalize_layout(layout))
        assert not w.ok
        assert any("过窄" in i for i in w.issues), w.issues

    def test_少一扇门仍然连通时不误判(self):
        """
        ⚠️ **写这条测试时我自己先判错了一次。**

        初版写的是"去掉一扇门 → 应当判不可漫游"。实测 `ok=True` ——
        因为去掉的是卧室A↔卧室B 那扇，两间卧室仍然各自经客厅相通，
        **三间房依然全连通**。

        所以这不是 bug，是我的前提错了。这条测试留下来守这个认知：
        判据是**连通性**，不是"门的数量" —— 少一扇门不影响，少到不连通才影响。
        """
        layout = copy.deepcopy(REAL_LAYOUT)
        layout["doors"] = [
            {"position": [367, 197], "width": 0.9},   # 客厅 ↔ 卧室A
            {"position": [400, 407], "width": 0.9},   # 客厅 ↔ 卧室B
        ]
        w = build_walkable(normalize_layout(layout))
        assert w.ok, f"两间卧室仍经客厅相通，不该判成不可漫游：{w.issues}"
        assert all(r.reachable for r in w.rooms)

    def test_少一间房走不到不阻断漫游(self):
        """
        ⚠️ **这条断言被修正过（2026-09-23）—— 原来的判据太严。**

        它原来断言"有一间房走不到 → 整个不可漫游"。

        实测（自己生成的两居室，8 间房全识别对）碰到的情况是：模型漏掉了
        客厅通阳台那扇 1.6m 的推拉门，阳台没有入口。按原判据，**整条第一人称
        漫游就被这一间阳台废掉了** —— 而其余 88% 的面积完全能正常走。

        所以改成按**面积覆盖率**判（≥ 50% 即可），逛不到的房间照样如实
        列出来、界面上标灰。**该说的照说，只是不因此把功能关掉。**
        """
        layout = copy.deepcopy(REAL_LAYOUT)
        layout["doors"] = [{"position": [400, 407], "width": 0.9}]   # 只留 客厅 ↔ 卧室B
        w = build_walkable(normalize_layout(layout))

        assert w.ok, f"只差一间卧室就到不了，不该把整个漫游关掉：{w.issues}"
        unreachable = [r.name for r in w.rooms if not r.reachable]
        assert "卧室A" in unreachable
        # 但必须**说出来**，不能默默少一间
        assert any("卧室A" in n for n in w.notes), f"逛不到的房没写出来：{w.notes}"

    def test_大部分面积走不到才降级(self):
        """
        反向确认判据还拦得住真问题：出生点落在一个小片区里、
        而大面积的那间房被切在外面 —— 这种才该降级。
        """
        layout = copy.deepcopy(REAL_LAYOUT)
        # 门只连两间卧室，客厅没有任何入口
        layout["doors"] = [{"position": [142, 315], "width": 0.9}]
        w = build_walkable(normalize_layout(layout))

        coverage = (
            sum(r.area_m2 for r in w.rooms if r.reachable)
            / sum(r.area_m2 for r in w.rooms)
        )
        assert coverage < 0.5, f"这个用例的面积覆盖率 {coverage:.0%}，没到该降级的程度"
        assert not w.ok, "大面积走不到，却判成可漫游"
        assert any("可逛面积" in i for i in w.issues), w.issues

    def test_空场景不崩(self):
        w = build_walkable(normalize_layout({}))
        assert not w.ok
        assert w.issues
        assert w.to_dict()["mode"] == "fly"

    def test_可漫游时模式是_walk(self):
        assert _walk().to_dict()["mode"] == "walk"


class TestSerialization:
    def test_to_dict_可被_json_序列化(self):
        import json

        d = _walk().to_dict()
        back = json.loads(json.dumps(d, ensure_ascii=False))
        assert back["ok"] is True
        assert len(back["rooms"]) == 3
        assert len(back["collision"]) == 9
        assert back["player_radius_m"] == DEFAULT_PLAYER_RADIUS_M

    def test_坐标精度被裁剪(self):
        """不裁的话 JSON 里会出现 3.7659876543210003 这种噪声。"""
        d = _walk().to_dict()
        for c in d["collision"]:
            for v in list(c["a"]) + list(c["b"]):
                assert len(str(v).split(".")[-1]) <= 3, f"坐标精度未裁剪：{v}"


# ══════════════════════════════════════════════════════════════════
# 3D 渲染端点：墙角要填实，但**不能把门挤窄**
# ══════════════════════════════════════════════════════════════════


class TestRenderEndpoints:
    """
    碰撞用中心线，3D 渲染用"带宽度的墙条"。

    ⚠️ 两者对端点的要求是**相反**的，这是这个类存在的理由：

      · 墙角：两段墙的中心线正好交于一点，各自铺开半个墙厚之后
        外面留一个 0.1×0.1m 的方口 —— **必须往外延**才填得上
      · 门洞：往外延会**把门挤窄**（0.9m 变 0.7m）—— **绝不能延**

    而这两种错在画面上都不显眼：墙角缝是"一道细缝"，门变窄是"看着还行"。
    """

    def test_墙角被填实(self):
        """
        沿每段墙的中心线走一遍，除了门洞范围，**任何一点都不该离
        最近的渲染墙条超过半个墙厚**。

        这一条直接说"3D 的墙没有洞"。不外延的话，墙角处会出现
        一个中心线覆盖不到的小区域，这里就会红。
        """
        scene = normalize_layout(REAL_LAYOUT)
        w = build_walkable(scene)
        half = max(wall.thickness_m for wall in scene.walls) / 2

        gap_ranges = _door_ranges_along_walls(scene)

        for wall in scene.walls:
            for wi_seg in wall.segments():
                samples = 120
                for i in range(samples + 1):
                    t = i / samples
                    p = (wi_seg[0].x + (wi_seg[1].x - wi_seg[0].x) * t,
                         wi_seg[0].y + (wi_seg[1].y - wi_seg[0].y) * t)
                    if _in_any_gap(p, gap_ranges):
                        continue      # 门洞里本来就是空的
                    d = min(
                        _dist_to_seg(p, s.ra, s.rb) for s in w.collision
                    )
                    assert d <= half + 0.02, (
                        f"墙上的点 {p} 离最近的渲染墙条 {d:.3f}m "
                        f"（超过半墙厚 {half:.3f}m）—— 这里会漏出一条缝"
                    )

    def test_门洞没有被外延挤窄(self):
        """
        ⚠️ **这条守的是"门比看上去窄"这个看不见的错。**

        渲染端点在门洞两侧**必须保持原样**。各外延半个墙厚的话，
        0.9m 的门在 3D 里只剩 0.7m，而画面上完全看不出来。
        """
        scene = normalize_layout(REAL_LAYOUT)
        w = build_walkable(scene)

        for op in scene.openings:
            if op.kind != "door" or op.wall_index < 0:
                continue
            placed = wall_point_at(scene.walls[op.wall_index],
                                   op.offset_along_wall_m)
            assert placed is not None
            p, u = placed

            # 门洞两个边缘点，各自到最近的**渲染端点**的距离
            for sign in (1, -1):
                edge = (p.x + u.x * op.width_m / 2 * sign,
                        p.y + u.y * op.width_m / 2 * sign)
                d = min(
                    min(math.dist(edge, _xy(s.ra)), math.dist(edge, _xy(s.rb)))
                    for s in w.collision
                )
                assert d < 0.03, (
                    f"门洞边缘 {edge} 被渲染墙条侵入 {d:.3f}m —— "
                    f"3D 里这个门会比 {op.width_m}m 窄"
                )

    def test_外延量恰好是半个墙厚(self):
        """角落处的渲染端点应当恰好外延 half，多了会捅进隔壁房间。"""
        scene = normalize_layout(REAL_LAYOUT)
        w = build_walkable(scene)
        half = max(wall.thickness_m for wall in scene.walls) / 2

        outermost = max(
            max(_xy(s.ra)[i], _xy(s.rb)[i]) for s in w.collision for i in (0, 1)
        )
        # 外墙中心线在 0 与 7.8905；外延 half 后最远到 7.8905 + 0.1
        assert outermost == pytest.approx(7.8905 + half, abs=0.02), (
            f"最远渲染端点 {outermost:.3f}，应当是墙线 + 半墙厚 {7.8905 + half:.3f}"
        )

    def test_碰撞中心线保持在墙体上(self):
        """
        外延**只改渲染端点**。碰撞中心线（`a`/`b`）必须仍在原来那条墙上 ——
        否则玩家会被挡在离墙 0.1m 的地方，表现为"贴不到墙"，
        而画面上墙就在眼前，很难说清哪里不对。
        """
        scene = normalize_layout(REAL_LAYOUT)
        w = build_walkable(scene)
        wall_segs = [s for wall in scene.walls for s in wall.segments()]

        for s in w.collision:
            for e in (s.a, s.b):
                d = min(
                    _dist_to_seg((e.x, e.y), a, b) for a, b in wall_segs
                )
                assert d < 1e-6, (
                    f"碰撞端点 ({e.x:.3f},{e.y:.3f}) 偏离墙体中心线 {d:.4f}m —— "
                    f"外延泄漏进了碰撞几何"
                )


def _door_ranges_along_walls(scene) -> list[tuple[float, float]]:
    """所有门洞在**全局**坐标下的近似圆盘，用于"这点在不在门洞里"的判定。"""
    out = []
    for op in scene.openings:
        if op.kind == "door":
            out.append((op.center.x, op.center.y, op.width_m / 2 + 0.02))
    return out


def _in_any_gap(p, ranges) -> bool:
    return any(
        math.dist(p, (cx, cy)) <= r for cx, cy, r in ranges
    )


# ══════════════════════════════════════════════════════════════════
# 门的两侧判定：**窄空间**下的回归
# ══════════════════════════════════════════════════════════════════


class TestDoorSideDetectionInNarrowSpaces:
    """
    ⚠️ **这个类守的是一个必然翻车的算法。**

    初版判「门两侧是哪两间房」的办法是：从门中心沿墙法向**各走 0.6m**，
    看落在哪个房间里。

    实测（2026-09-23，自己生成的两居室）翻车了 —— 那条走廊的净宽只有
    **0.264m**：0.6m 的探针直接跨过整条走廊打到对面，两侧落进同一间房。
    连通图于是少一条边，走廊后面的房间全成孤岛，
    「可逛面积只剩 46%」→ 第一人称漫游被判定为不可用。

    再往前一版是沿墙的**切向**探（±u），症状是"每间房都只跟自己连通"。

    两次的共同点：用「走一段再看落在哪」回答一个**拓扑**问题。
    距离是猜的，空间宽度不是常量。

    现在的做法不问走多远，直接问门在哪两间房之间：沿法向分侧，
    各取最近的房间。下面用一个净宽 0.12m 的极端走廊钉住它。
    """

    #: 一条**极窄**走廊（净宽约 0.12m）连起两间房。
    #: 0.6m 的探针在这里必然穿过走廊打到对面。
    NARROW = {
        "rooms": [
            {"name": "客厅", "type": "living_room", "bbox": [0, 0, 400, 400]},
            {"name": "走廊", "type": "other", "bbox": [400, 0, 440, 400]},
            {"name": "卧室", "type": "bedroom", "bbox": [440, 0, 840, 400]},
        ],
        "walls": [
            {"type": "unknown", "coords": [[0, 0], [840, 0], [840, 400],
                                           [0, 400], [0, 0]]},
            {"type": "unknown", "coords": [[400, 0], [400, 400]]},
            {"type": "unknown", "coords": [[440, 0], [440, 400]]},
        ],
        # 两扇门分别开在走廊两侧的墙上
        "doors": [
            {"position": [400, 200], "width": 0.9},
            {"position": [440, 200], "width": 0.9},
        ],
        "windows": [],
        "total_area": 32.0,
        "confidence": 0.6,
    }

    def test_窄走廊两侧的门各自连对(self):
        w = build_walkable(normalize_layout(self.NARROW))
        assert len(w.doors) == 2, (
            f"两扇门应当都能定位到两侧房间，实得 {len(w.doors)} 扇"
            f"：{w.issues}"
        )
        pairs = {frozenset((d.from_room, d.to_room)) for d in w.doors}
        # 客厅(0) ↔ 走廊(1) 和 走廊(1) ↔ 卧室(2)
        assert frozenset((0, 1)) in pairs, f"客厅与走廊没连上：{pairs}"
        assert frozenset((1, 2)) in pairs, f"走廊与卧室没连上：{pairs}"

    def test_每扇门连的是两间不同的房(self):
        """探针方向错了会退化成"自己连自己"，这条直接钉住。"""
        w = build_walkable(normalize_layout(self.NARROW))
        for d in w.doors:
            assert d.from_room != d.to_room, (
                f"门 {d.door_index} 的两侧是同一间房 —— 两侧判定打偏了"
            )

    def test_走廊里的房间都够得到(self):
        """窄，但只要门连对了，三间房就都走得到。"""
        w = build_walkable(normalize_layout(self.NARROW))
        assert all(r.reachable for r in w.rooms), (
            f"有房间走不到：{[r.name for r in w.rooms if not r.reachable]}"
        )


# ══════════════════════════════════════════════════════════════════
# 门扇几何：3D 里要把门画在门洞上
# ══════════════════════════════════════════════════════════════════


class TestDoorLeafGeometry:
    """
    3D 要让门能开关，需要三样东西：**转轴在哪**、**门扇朝哪边长**、
    **往哪边开**。这三样由后端从 `wall_index` + `offset_along_wall_m` 算好，
    前端只管摆位置。

    反过来（前端自己推）就会多出第二套计算，两套差一点的表现是
    **门扇挂偏半个门宽、或者转轴埋进墙里** —— 而这类偏差在画面上不一定显眼。
    """

    def test_铰链加方向乘门宽落在门洞另一端(self):
        """
        `hinge + along × width` 必须正好是门洞的**另一端**：
        间距恰好是门宽，中点在墙线上。

        ⚠️ 中点**不等于**模型报的门中心 —— 实测两者差 **3–8cm**（六扇门实测）。
        模型给的门位置本来就略微偏离墙线（它是在图上"看"出来的一个点），
        而门必须开在**墙上**。

        所以后端用的是"把门中心投影到墙上"的那个点，不是原始位置。
        这条如果按"中点 == 模型报的位置"去断言会挂 —— 而且挂得对：
        **是断言错了，代码是对的**。
        """
        scene = normalize_layout(REAL_LAYOUT)
        w = build_walkable(scene)
        for d in w.doors:
            other = (d.hinge.x + d.along.x * d.width_m,
                     d.hinge.y + d.along.y * d.width_m)
            assert math.dist(other, (d.hinge.x, d.hinge.y)) == pytest.approx(
                d.width_m, abs=1e-9
            ), f"门 {d.door_index} 的两端间距不等于门宽"

            mid = ((d.hinge.x + other[0]) / 2, (d.hinge.y + other[1]) / 2)
            # 中点必须在**墙线上**（到墙中心线距离 ≈ 0）
            on_wall = min(
                _dist_to_seg(mid, (a.x, a.y), (b.x, b.y))
                for wall in scene.walls for a, b in wall.segments()
            )
            assert on_wall < 1e-6, (
                f"门 {d.door_index} 的门扇中点离墙线 {on_wall:.4f}m —— 门没开在墙上"
            )
            # 与模型报的位置的差距应当在"关联容差"之内（1m）
            off = math.dist(mid, (d.position.x, d.position.y))
            assert off < 1.0, f"门扇离模型报的位置 {off:.3f}m，太远了"

    def test_沿墙方向是单位向量(self):
        for d in _walk().doors:
            n = math.hypot(d.along.x, d.along.y)
            assert n == pytest.approx(1.0, abs=1e-9), f"along 不是单位向量：{n}"

    def test_法向与沿墙方向垂直(self):
        """
        ⚠️ 不垂直的话门扇"全开"之后不是贴着墙的，会以一个奇怪的角度戳出去。

        顺带：法向是**单位向量**也要查 —— 3D 里门扇摆动的方向由它给。
        """
        for d in _walk().doors:
            dot = d.along.x * d.normal.x + d.along.y * d.normal.y
            assert abs(dot) < 1e-9, f"门 {d.door_index} 的 along·normal = {dot}"
            assert math.hypot(d.normal.x, d.normal.y) == pytest.approx(1.0, abs=1e-9)

    def test_关着的门挡得住人(self):
        """
        `hinge → hinge+along×width` 这一段在门关着时是**实心**的。

        判据用门板自己的中点（在墙线上），不是模型报的门中心 ——
        见上一条的说明。
        """
        for d in _walk().doors:
            other = (d.hinge.x + d.along.x * d.width_m,
                     d.hinge.y + d.along.y * d.width_m)
            mid = ((d.hinge.x + other[0]) / 2, (d.hinge.y + other[1]) / 2)
            assert _dist_to_seg(mid, (d.hinge.x, d.hinge.y), other) == pytest.approx(
                0, abs=1e-9
            ), "门板中点不在门板自己上面（说明铰链/方向算错了）"

    def test_开门时那段不在碰撞几何里(self):
        """
        反向：碰撞几何里**本来就没有**这段（后端切门洞时把它切掉了）。
        所以"关着的门挡人"必须由前端运行时补一段 ——
        这也是为什么 `setExtraCollision` 存在。
        """
        w = _walk()
        for d in w.doors:
            other = (d.hinge.x + d.along.x * d.width_m,
                     d.hinge.y + d.along.y * d.width_m)
            mid = (d.position.x, d.position.y)
            # 静态碰撞线段离门中心的距离应当 ≥ 半个门宽（因为门洞被切开了）
            nearest = min(_dist_to_seg(mid, (s.a.x, s.a.y), (s.b.x, s.b.y))
                          for s in w.collision)
            assert nearest >= d.width_m / 2 - 0.05, (
                f"门 {d.door_index} 中心离最近的碰撞段只有 {nearest:.3f}m，"
                f"说明门洞没切成 —— 关门的碰撞叠加就没有意义了"
            )
            del other



# ══════════════════════════════════════════════════════════════════
# 门的去重与"门在哪"的唯一表示（2026-09-24 需求方反馈）
# ══════════════════════════════════════════════════════════════════


class TestDoorPositionIsOnTheWall:
    """
    需求方原话：「门的位置和平面图对不上，可能会出现一个根本不应该存在的
    门立在墙边」。

    ══════════════════════════════════════════════════════════════════
    根因：同一个"门在哪"有两个互相矛盾的值
    ══════════════════════════════════════════════════════════════════
    `DoorEdge` 里 `hinge` / `along` 来自**投影**（把解析给的门中心投到墙上），
    而 `position` 直接用了 `op.center`（解析给的原始中心，没投影）。

    实测（`scripts/inspect_layout.py`，演示户型 2026-09-24）：
    6 个门洞的原始中心离墙 **0.48 ~ 0.60m**。于是：

      · 3D 里的门楣按 `position` 摆 → **飘在离墙半米的地方**，
        看起来就是"一扇不该在的门立在墙边"
      · 走门时"离哪扇门最近"按 `position` 量 → 量的根本不是门

    两个表示只要有一个是错的，画面上就是"门和平面图对不上"。
    所以这里钉死：**门的位置必须在墙上**。
    """

    def test_每扇门的位置都落在墙上(self):
        w = _walk()
        walls = normalize_layout(REAL_LAYOUT).walls
        for d in w.doors:
            nearest = min(
                _seg_dist(_xy(d.position), _xy(a), _xy(b))
                for wall in walls for a, b in wall.segments()
            )
            assert nearest < 1e-6, (
                f"门 {d.door_index} 的位置 ({d.position.x:.3f},{d.position.y:.3f}) "
                f"离最近的墙 {nearest:.3f}m —— 门必须开在墙上。"
                f"（用解析给的原始中心会差 0.5m 上下，门楣就会飘出去）"
            )

    def test_位置与铰链在同一条线上(self):
        """`position` 必须是门洞的中点 —— 也就是铰链 + 半个门宽。"""
        for d in _walk().doors:
            mid = (d.hinge.x + d.along.x * d.width_m / 2,
                   d.hinge.y + d.along.y * d.width_m / 2)
            got = _xy(d.position)
            assert math.dist(mid, got) < 1e-6, (
                f"门 {d.door_index}：`position` 与 `hinge + along×宽度/2` 不一致"
                f"（{got} vs {mid}）—— 同一个量有两个值，迟早对不上"
            )


class TestDuplicateDoorsAreMerged:
    """
    同一扇门被解析模型读了两次（一次偏左、一次偏右），会产生两扇挨着的门。

    实测（演示户型）：第 3 与第 5 扇门投影到**同一面墙**、都连通
    「餐厅↔卫生间」，投影点相距 **1.11m**；而它们的原始中心分别是
    (6.56, 2.36) 与 (7.67, 1.25) —— y 相差 1.11m、落在**两间不同的房**里。
    在 3D 里就是"两扇贴得很近的门"，按 F 时开错的那一扇是常态。
    """

    @staticmethod
    def _setup() -> tuple[list, list]:
        """一面横墙 y=5.20，房间在它上下两侧。"""
        wall_u = Vec2(1.0, 0.0)
        rooms = [
            _room(0, "客厅", 3.0, 3.0, (0.5, 0.5, 5.5, 5.0)),
            _room(1, "餐厅", 3.0, 7.5, (0.5, 5.4, 5.5, 9.5)),
        ]
        return rooms, [(0, _op(3, 0.9, Vec2(3.0, 5.2)), Vec2(3.0, 5.2), wall_u, 0.5),
                       (1, _op(3, 0.9, Vec2(4.1, 5.2)), Vec2(4.1, 5.2), wall_u, 0.6)]

    def test_相距不足一个误差量级的同一扇门被合并(self):
        from backend.app.services.geometry.walkable import _dedupe_doors

        rooms, anchored = self._setup()   # 两个投影点相距 1.10m < 1.2m
        kept, dropped = _dedupe_doors(anchored, rooms)
        assert len(kept) == 1, f"没有合并重复的门，保留了 {len(kept)} 扇"
        assert dropped == [(1, 0)], f"去重结果不对：{dropped}"
        # **保留更贴墙的那一扇** —— 同一扇门的两次读数里更可信的一次
        assert kept[0][0] == 0, (
            "保留了离墙 0.6m 的那次读数，应当保留 0.5m 的那次"
        )

    def test_保留的是更贴墙的那一扇与顺序无关(self):
        from backend.app.services.geometry.walkable import _dedupe_doors

        rooms, anchored = self._setup()
        kept, dropped = _dedupe_doors(list(reversed(anchored)), rooms)
        assert len(kept) == 1
        assert kept[0][0] == 0, "换了个顺序就换了一扇 —— 结果不能取决于门洞排列顺序"

    def test_相距够远的两扇门不合并(self):
        from backend.app.services.geometry.walkable import _dedupe_doors

        rooms, anchored = self._setup()
        # 把第二扇挪到 3m 之外
        anchored[1] = (1, _op(3, 0.9, Vec2(6.0, 5.2)), Vec2(6.0, 5.2),
                       Vec2(1.0, 0.0), 0.6)
        kept, dropped = _dedupe_doors(anchored, rooms)
        assert len(kept) == 2, "相隔 3m 的两扇门被误合并了"
        assert not dropped

    def test_连通不同房间对的门即使挨着也不合并(self):
        """
        ⚠️ **这条是防误合并的。** 同一面墙上相邻两间房的门可以挨得很近
        （比如走廊两侧），只看距离就会把它们合成一扇 —— 那是把两间房
        的通行权弄丢一条。
        """
        from backend.app.services.geometry.walkable import _dedupe_doors

        wall_u = Vec2(1.0, 0.0)
        rooms = [
            _room(0, "客厅", 3.0, 3.0, (0.5, 0.5, 5.5, 5.0)),
            _room(1, "餐厅", 3.0, 7.5, (0.5, 5.4, 5.5, 7.0)),      # 中间
            _room(2, "厨房", 10.0, 7.5, (8.0, 5.4, 12.0, 9.5)),    # 右侧，房间对不同
        ]
        anchored = [
            (0, _op(3, 0.9, Vec2(3.0, 5.2)), Vec2(3.0, 5.2), wall_u, 0.5),
            (1, _op(3, 0.9, Vec2(3.3, 5.2)), Vec2(3.3, 5.2), wall_u, 0.5),
        ]
        kept, dropped = _dedupe_doors(anchored, rooms)
        # 两个投影点都落在 x∈[0.5,5.5] 的客厅一侧，两侧都是「客厅↔餐厅」……
        # 所以这一对**确实**是同一对房间。再用一对真正不同的房间验证。
        assert len(kept) == 1 and dropped == [(1, 0)], (
            f"同一对房间、相距 0.3m 的门没有被合并：kept={len(kept)}"
        )

        anchored2 = [
            (0, _op(3, 0.9, Vec2(3.0, 5.2)), Vec2(3.0, 5.2), wall_u, 0.5),
            (1, _op(3, 0.9, Vec2(10.0, 5.2)), Vec2(10.0, 5.2), wall_u, 0.5),
        ]
        kept2, dropped2 = _dedupe_doors(anchored2, rooms)
        assert len(kept2) == 2, (
            "连通不同房间对（客厅↔餐厅 与 客厅↔厨房）的两扇门被误合并了 —— "
            "那会凭空少掉一条通路"
        )
        assert not dropped2


class TestTruncatedNameListSaysSo:
    """
    ⚠️ 实测踩过：原文案是「走不到 6 间房（儿童房、餐厅、厨房）」——
    数字说 6、列表只有 3，而且没有"等"。读的人只会得出"另外 3 间是哪些"，
    或者更糟：以为只有这 3 间。

    **列表被截断而不说，和编一个数字是同一类问题。**
    """

    def test_列表被截断时加等字(self):
        from backend.app.services.geometry.walkable import _name_list

        rooms = [_room(i, f"房{i}", 0.0, 0.0, (0, 0, 1, 1)) for i in range(6)]
        assert _name_list(rooms, 3) == "房0、房1、房2等"
        assert _name_list(rooms[:3], 3) == "房0、房1、房2"
        assert _name_list(rooms, 4) == "房0、房1、房2、房3等"


def _room(index: int, name: str, cx: float, cy: float,
          rect: tuple[float, float, float, float]):
    from backend.app.services.geometry.walkable import RoomNode
    from backend.app.services.geometry.normalize import Vec2 as _V
    return RoomNode(index=index, name=name, kind="bedroom",
                    center=_V(cx, cy), free_rect=rect, area_m2=10.0)


def _op(wall_index: int, width_m: float, center):
    """一个最小的门洞替身：去重逻辑只读这两个字段。"""
    return type("Op", (), {"wall_index": wall_index, "width_m": width_m,
                           "center": center})()


def _seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return math.dist(p, a)
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))

class TestDoorGapUsesWallOffsetNotRawCenter:
    """
    ⚠️ **门要开在墙上，洞也要开在门上** —— 两处必须用同一个锚点。

    ══════════════════════════════════════════════════════════════════
    需求方 2026-09-27 的原话
    ══════════════════════════════════════════════════════════════════
    > 明明平面图上面显示有门，这个墙体上却没有门……有时候明明已经识别到
    > 并生成了门，却没有对应可以行走的口。

    根因在切洞那一步：它拿解析给的 `opening.center` 去找"最近的墙段"，
    准入条件是"离墙 ≤ 半墙厚 + 0.35m（合计 0.55m）"。而解析给的 center
    实测能离墙 **0.96 ~ 1.20 m**（`scripts/inspect_layout.py` 早量过、
    `_build_doors` 的注释里也早写着）—— 于是那几扇门**根本没被切开**：
    墙上没有洞，门扇却照画。

    演示户型实测：6 扇门里 **4 扇**中招（wall 1 / 5 / 7 各自是一整段完整的墙，
    wall 8 / 9 只因为 center 恰好贴墙才有洞）。

    另一处（`_build_doors`）早就改用 `wall_index + offset_along_wall_m` 定位了 ——
    只有切洞还留在 `center` 上，这正是 `DoorEdge` 文档里警告的"两套坐标"。
    """

    @staticmethod
    def _layout_with_far_center() -> dict:
        """
        把门#0 往房间里推约 1.2m，复现"解析给的门中心离墙很远"那种读数。

        参考场景的像素尺度约 87 px/m（685px ↔ 7.89m），所以推 105px ≈ 1.2m。
        墙还是同一面（`_locate_on_wall` 取最近墙），变的是 center 离墙的距离。
        """
        layout = copy.deepcopy(REAL_LAYOUT)
        layout["doors"][0]["position"] = [275, 197]
        return layout

    def test_构造的样本确实复现了那个偏差(self):
        """先确认这条用例有意义 —— 样本的 center 必须真的离墙 > 旧门槛 0.55m。"""
        sc = normalize_layout(self._layout_with_far_center())
        op = [o for o in sc.openings if o.kind == "door"][0]
        wall = sc.walls[op.wall_index]
        off = min(_seg_dist((op.center.x, op.center.y), (a.x, a.y), (b.x, b.y))
                  for a, b in wall.segments())
        assert off > 0.55, (
            f"构造的样本离墙只有 {off:.2f}m，没到旧门槛 0.55m —— "
            f"这条用例会变成「恒过」，失去意义"
        )

    def test_离墙很远的门也必须在墙上开出洞(self):
        """
        核心断言：门的位置上**不许有墙**。

        旧实现在这条上会红：center 离墙 1.2m > 0.55m，切洞那一步直接 continue，
        于是墙是完整的一段，人走到门口被挡住 —— 而门扇照样画着。
        """
        sc = normalize_layout(self._layout_with_far_center())
        w = build_walkable(sc)
        assert w.doors, "样本里没有门，用例失效"
        for d in w.doors:
            blocked = _blocking_segments(w, d)
            assert not blocked, (
                f"门 {d.door_index}（{d.position.x:.2f},{d.position.y:.2f}）"
                f"被 {len(blocked)} 段碰撞墙堵着 —— 有门没有口。"
                f"切洞必须用 wall_index + offset_along_wall_m，"
                f"不能用解析给的 center（它离墙能到 1.2m）"
            )

    def test_人能从这扇门走过去(self):
        """把"能走过去"这件事真的走一遍：洪水填充要覆盖到门两侧的房间。"""
        sc = normalize_layout(self._layout_with_far_center())
        w = build_walkable(sc)
        cells = _flood_reachable(w)

        def reached(room) -> bool:
            cx, cy = room.center.x, room.center.y
            return any(
                math.dist((k[0] * 0.15, k[1] * 0.15), (cx, cy)) < 0.9
                for k in cells
            )

        for r in w.rooms:
            if r.standable and r.reachable:
                assert reached(r), (
                    f"「{r.name}」被判成可达，但洪水填充走不到它 —— "
                    f"门洞没切干净"
                )


def _blocking_segments(w, door) -> list:
    """与门同向、且横跨门中心的碰撞段（= 门被墙堵着）。"""
    px, py = door.position.x, door.position.y
    out = []
    for seg in w.collision:
        a, b = _xy(seg.a), _xy(seg.b)
        seg_len = math.dist(a, b)
        if seg_len <= 1e-9:
            continue
        cross = abs((b[0] - a[0]) / seg_len * door.along.y
                    - (b[1] - a[1]) / seg_len * door.along.x)
        if cross > 1e-3:
            continue
        dx, dy = b[0] - a[0], b[1] - a[1]
        t = ((px - a[0]) * dx + (py - a[1]) * dy) / (dx * dx + dy * dy)
        dist = math.hypot(px - (a[0] + min(1.0, max(0.0, t)) * dx),
                          py - (a[1] + min(1.0, max(0.0, t)) * dy))
        if 1e-6 < t < 1 - 1e-6 and dist <= 0.35:
            out.append(seg)
    return out


class TestDoorSnapToleranceMatchesTheParsesOwnNoise:
    """
    ⚠️ **离墙 1.11m 的门，必须照样挂得上墙。**

    （⚠️ 2026-09-28 更正：那扇门实测是**客厅↔阳台的推拉门**，不是入户门 ——
      我最初按需求方"3D 没有通外的大门"那句话想当然地归因成了入户门。
      "通到户外的门"是另一件事，见 `TestExteriorDoorIsIdentified`。）

    ══════════════════════════════════════════════════════════════════
    需求方 2026-09-28 的原话
    ══════════════════════════════════════════════════════════════════
    > 在动态漫游时，我发现你做的房间没有房间到外面的出口…平面图是显示的
    > 房间朝外的大门的…仅仅是让生成的 3d 小屋有通外的大门，跟平面图对应。

    量出来的账（`scripts/_probe_wall_snap.py`，跑在冻结的黄金解析产物上）：

      · 那个洞口的中心离最近的墙 **1.11m**（次近的墙在 2.25m 外）；
      · 而 `_locate_on_wall` 当时卡的是 **1.0m** —— 差 0.11m，整扇门被丢掉：
        `wall_index = -1` → 3D 里既不画门扇、墙上也不开洞；
      · 同一份产物里其余 13 个洞口都在 0.60m 以内，**只有这一扇中招**，
        表现就是"别的门都在，唯独通外的大门没有"。

    这不是"门槛稍微紧了点"，而是**两处各写各的**：
    `walkable.MAX_DOOR_CENTER_OFFSET_M` 早就写了 1.5（它的注释里明说
    "实测解析的 center 离墙 0.5–1.2m，所以放到 1.5"），而挂墙那一步
    还在用 1.0 —— 同一个问题两个答案，宽的那个在下面、紧的那个在上面，
    于是紧的那个说了算。

    所以下面第一条测的是"它们必须是同一个数"，后两条测这个数的大小：
    能收下解析噪声（≥1.2m），又不至于把三里地外的门吸过来。
    """

    @staticmethod
    def _one_wall() -> list:
        """一面从 (0,0) 到 (5,0) 的直墙。"""
        return [WallSeg(points=[Vec2(0.0, 0.0), Vec2(5.0, 0.0)], kind="unknown")]

    def test_两处容差必须是同一个数(self):
        """
        `_locate_on_wall`（挂到哪面墙）与 `walkable`（offset 可不可信）
        问的是同一个问题：「门离墙多远还算得准」。各写各的就会再次分叉。
        """
        assert walkable.MAX_DOOR_CENTER_OFFSET_M == WALL_SNAP_TOLERANCE_M, (
            "两个容差又分叉了 —— 见这个类与 normalize.WALL_SNAP_TOLERANCE_M 的说明"
        )

    def test_容差至少覆盖解析实测到的最大偏差(self):
        """实测解析给的门中心离墙 0.5～1.2m（门是用开启弧估的，比窗飘）。"""
        assert WALL_SNAP_TOLERANCE_M >= 1.2, (
            f"容差 {WALL_SNAP_TOLERANCE_M}m 小于解析自身的噪声 1.2m —— "
            f"那就是在按噪声判门，离得稍远的那扇会被整扇丢掉"
        )

    @pytest.mark.parametrize("distance", [0.5, 1.0, 1.11, 1.4])
    def test_离墙这么远的门仍然挂得上(self, distance: float):
        """1.11 是黄金产物里那个洞口的实测值，不是编的。"""
        idx, off = _locate_on_wall(Vec2(2.5, distance), self._one_wall())
        assert idx == 0, f"离墙 {distance}m 的门挂不上墙（→ 3D 里既没门扇也没洞）"
        assert off == pytest.approx(2.5, abs=0.01)

    def test_三里地之外的门不能硬吸到墙上(self):
        """
        反例。容差不能无限大 —— 那会把飘在房间中间的门也吸到最近的墙上，
        凭空多出一扇现实里不存在的门。
        """
        idx, _off = _locate_on_wall(Vec2(2.5, 3.0), self._one_wall())
        assert idx == -1, "离墙 3m 的门也被吸上墙了 —— 容差放得太宽"

    def test_黄金产物里每个洞口都挂上了墙(self):
        """
        真实数据回归：这份冻结产物里曾有 1 个门挂不上墙（就是上面那扇入户门）。
        它同时是"3D 里少了扇门"的根因，所以直接钉住整份产物。
        """
        fixture = (pathlib.Path(__file__).resolve().parents[1]
                   / "scripts" / "fixtures" / "golden_layout.json")
        if not fixture.is_file():
            pytest.skip("没有固定输入 scripts/fixtures/golden_layout.json")
        sc = normalize_layout(json.loads(fixture.read_text(encoding="utf-8")))
        orphans = [i for i, o in enumerate(sc.openings) if o.wall_index < 0]
        assert not orphans, (
            f"第 {orphans} 个洞口没挂到任何墙上 —— 它们在 3D 里既没有门窗、"
            f"墙上也没有洞，而上传的平面图上是画着的"
        )


class TestExteriorDoorIsIdentified:
    """
    通到户外的门（入户门）要**认得出来**；一扇都没有时要**说出来**。

    ══════════════════════════════════════════════════════════════════
    需求方的原话（2026-09-28）
    ══════════════════════════════════════════════════════════════════
    > 在动态漫游时，我发现你做的房间没有房间到外面的出口…平面图是显示的房间
    > 朝外的大门的…仅仅是让生成的 3d 小屋有通外的大门，跟平面图对应。

    ⚠️ 这条要求的**前一半**（"平面图上有门，3D 里也必须有"）落在这里：
       把"哪扇门通向户外"算出来标在门上，而不是靠人看图。
       它的**另一半**是"没有就得说" —— 一个封死的盒子如果什么都不说，
       用户只会以为是自己没找到门（`_probe_entrance_door.py` 量过：
       三份演示户型各自都恰好有一扇通外的门，而冻结产物里那扇入户门
       曾因为挂墙容差太紧被整扇丢掉，3D 里既没门扇也没洞）。
    """

    @staticmethod
    def _with_entrance() -> dict:
        """
        在客厅南侧的外墙（y=545）上加一扇 1.0m 的门 —— 那就是入户门。

        参考场景的像素尺度约 87 px/m，所以 1.0m ≈ 87px，门宽按 1.0 给。
        """
        layout = copy.deepcopy(REAL_LAYOUT)
        layout["doors"].append({"position": [550, 545], "width": 1.0})
        return layout

    def test_通到户外的门会被认出来(self):
        w = build_walkable(normalize_layout(self._with_entrance()))
        ent = [d for d in w.doors if d.is_entrance]
        assert len(ent) == 1, (
            f"应当且只应当认出一扇通外的门，实际认出 {len(ent)} 扇 —— "
            f"3D 里就没有「大门」可言了"
        )
        assert ent[0].width_m == pytest.approx(1.0, abs=0.05)
        assert not any("户外" in n for n in w.issues), "认出来了就不该再说没有"

    def test_室内门不会被误认成入户门(self):
        """三扇都开在房间之间的门，一扇都不该标成通外。"""
        w = build_walkable(normalize_layout(REAL_LAYOUT))
        assert not any(d.is_entrance for d in w.doors), (
            "把室内门说成入户门，比漏报更糟 —— 用户会以为那是大门"
        )

    def test_一扇通外的门都没有时必须明说(self):
        """
        REAL_LAYOUT 这份真实解析里没有入户门（三扇门都在房间之间）。
        这时必须有一句话说明"围墙是整圈闭合的"，否则用户只会以为自己没找到门。
        """
        w = build_walkable(normalize_layout(REAL_LAYOUT))
        # ⚠️ 断言的是 `issues` 不是 `notes`：**界面上只渲染 issues**
        #    （`SceneViewer` 的"后端自己报出来的问题"那一块）。
        #    这句话本来写进 notes，等于写在没人看的地方。
        assert any("没有识别到通往户外的门" in n for n in w.issues), (
            f"没有通外的门却不吭声。当前 issues：{w.issues}"
        )
