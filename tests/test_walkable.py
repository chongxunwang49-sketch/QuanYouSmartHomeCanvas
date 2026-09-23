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
import math
from collections import deque

import pytest

from backend.app.services.geometry import normalize_layout
from backend.app.services.geometry.normalize import wall_point_at
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
        """
        w = _walk()
        spawn_room = next(r for r in w.rooms if r.index == w.spawn_room)
        degree = {}
        for d in w.doors:
            degree[d.from_room] = degree.get(d.from_room, 0) + 1
            degree[d.to_room] = degree.get(d.to_room, 0) + 1
        assert degree.get(spawn_room.index, 0) == max(degree.values()), (
            f"出生在「{spawn_room.name}」（{degree.get(spawn_room.index, 0)} 扇门），"
            f"但门最多的是 {max(degree.values())} 扇"
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
