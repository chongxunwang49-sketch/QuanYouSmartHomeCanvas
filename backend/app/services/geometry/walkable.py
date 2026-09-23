"""
可漫游性 —— 第一人称行走需要的碰撞几何与房间连通图。

═══════════════════════════════════════════════════════════════════
这一层要回答的问题只有一个：**这户型能不能走**
═══════════════════════════════════════════════════════════════════
"能不能走"不是"墙画出来了没有"，而是四件事同时成立：

  1. 墙体闭合（`Scene.quality.can_build_walls`）—— 否则没有东西可碰撞
  2. 每间房都**站得下人**（净空 > 玩家直径）—— 否则进去就卡在几何里
  3. 门洞**真的能穿过去**（宽 > 玩家直径）—— 否则看得见进不去
  4. 覆盖面积够 —— 从出生点走得到的地方要占**大部分**面积

前三条任一不成立，就**不能**做第一人称漫游，必须降级成自由视角（可以穿墙飞）。

第四条刻意**不是"每一间房都要走得到"**。实测（2026-09-23）一份自己生成的
两居室：墙识别得很好、8 间房全对，但模型漏掉了客厅通阳台那扇 1.6m 的推拉门 ——
阳台没有入口。按"必须全都到得了"判，整个第一人称漫游就被这一间阳台废掉了，
而实际上其余 88% 的面积都能正常走。

所以改成按**面积覆盖率**判：走得到的部分 ≥ 一半即可。逛不到的阳台照样
如实列进 `issues`、界面上也会标灰 —— 该说的照说，只是不因此把功能关掉。

降级不是失败 —— 但**静默降级是**。所以每条原因都写进 `issues`。

═══════════════════════════════════════════════════════════════════
最容易漏的一件事：墙是连续的，门只是个符号 ═══
═══════════════════════════════════════════════════════════════════
几何内核给出的墙体折线是**一条连续的线**。2D 图上画门的时候，是先用
底色在墙上盖出一段缺口、再画门扇和开启弧（见 `render/svg.py`）——
**那只改了画面，没改几何**。

如果直接拿这份折线去做碰撞，人会被**关在房间里出不去**。而且不报错：
墙是对的、门画得也对、走过去就是过不去 —— 用户只会觉得"这功能没做好"，
定位不到是哪儿的问题。

所以这里必须真的**把门洞从碰撞线段里切掉**。切法是纯算术，但它有个
不报错的失败模式：门在墙角附近时，切口会越过线段端点，切出一个负长度的
碎段。所以下面每一段都要判长度，短的直接丢掉。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .normalize import (
    DEFAULT_WALL_THICKNESS_M,
    Scene,
    Vec2,
    _point_seg_distance,
    wall_point_at,
)

#: 玩家半径。按肩宽 0.5m 估 —— 这是行走类应用常用的碰撞半径，
#: 比"把人当成一个点"好得多：贴着墙走时不会穿模。
DEFAULT_PLAYER_RADIUS_M = 0.25

#: 眼高。中国成年男性平均身高约 1.70m，眼睛约 1.60m。
#: 这个值影响"看起来像不像自己在走"，低了像小孩视角，高了像无人机。
DEFAULT_EYE_HEIGHT_M = 1.6

#: 门洞切割的容差：门中心离墙超过这个距离就不切它。
#: 门是关联到墙上的（`wall_index >= 0`），但关联只保证"最近的墙在 1m 内"，
#: 不保证真贴着。切错的后果是墙上多一个莫名其妙的洞。
CUT_DISTANCE_TOLERANCE_M = 0.35

#: 切出来的碎段短于这个长度就丢掉（门在墙角时会切出这种）。
MIN_SEGMENT_M = 0.02

#: 走得到的面积占比低于这个值就不做第一人称漫游。
#:
#: 为什么是"面积"而不是"房间数"：一间 3㎡ 的储物间和一间 30㎡ 的客厅
#: 在"这个户型能不能逛"这件事上权重完全不同。按间数算，漏一个储物间
#: 和漏一个客厅是一样的 —— 那不合理。
MIN_REACHABLE_AREA_RATIO = 0.5


@dataclass(frozen=True)
class CollisionSeg:
    """
    一段**实心**墙。门洞处已经被切掉。

    ══════════════════════════════════════════════════════════════════
    `a`/`b` 与 `render_a`/`render_b` 为什么要分开
    ══════════════════════════════════════════════════════════════════
    碰撞用的是**中心线**（`a`/`b`）：玩家撞墙撞的是这条线。

    3D 渲染用的是一条**带宽度的墙体**：把中心线两侧各铺开半个墙厚。
    问题出在墙角 —— 两段墙的中心线在角点**正好相交于一点**，
    各自铺开 0.1m 之后，外侧会留下一个 0.1×0.1m 的**方口**。
    站在屋里看，每个墙角都有一道竖着的缝，缝后面是空的。

    直觉的修法是"每端各往外延半个墙厚"把角填实 —— 但那样会**把门洞挤窄**：
    0.9m 的门被两侧各吃掉 0.1m，变成 0.7m。而"门比看上去窄"这件事
    在画面上完全看不出来。

    所以这里分开：
      · `a`/`b`              精确的碰撞中心线，**不外延**
      · `render_a`/`render_b` 渲染用：**墙角外延，门洞边缘不外延**

    判据是"这个端点是不是贴着门洞"—— 贴着就不外延。
    """

    a: Vec2
    b: Vec2
    wall_index: int
    #: 3D 渲染用的端点。默认等于 a/b（没有墙角需要填时）。
    render_a: Vec2 | None = None
    render_b: Vec2 | None = None

    @property
    def length_m(self) -> float:
        return math.dist((self.a.x, self.a.y), (self.b.x, self.b.y))

    @property
    def ra(self) -> Vec2:
        return self.render_a if self.render_a is not None else self.a

    @property
    def rb(self) -> Vec2:
        return self.render_b if self.render_b is not None else self.b

    def to_dict(self) -> dict[str, Any]:
        # 毫米精度就够 —— 碰撞体比这精细没有意义，而小数位直接决定
        # 这份 JSON 的体积（它就压在轮询响应里）。
        def p3(v: Vec2) -> list[float]:
            return [round(v.x, 3), round(v.y, 3)]

        return {
            "a": p3(self.a),
            "b": p3(self.b),
            "render_a": p3(self.ra),
            "render_b": p3(self.rb),
            "wall": self.wall_index,
        }


@dataclass
class RoomNode:
    """一间可以站的房。"""

    index: int
    name: str
    kind: str
    center: Vec2
    #: 可站立矩形 `(x1, y1, x2, y2)`，米。房间 bbox 内缩半墙厚 + 玩家半径
    free_rect: tuple[float, float, float, float]
    area_m2: float
    #: 从出生点走得到吗
    reachable: bool = False

    @property
    def standable(self) -> bool:
        x1, y1, x2, y2 = self.free_rect
        return (x2 - x1) > 0 and (y2 - y1) > 0

    def to_dict(self) -> dict[str, Any]:
        x1, y1, x2, y2 = self.free_rect
        return {
            "index": self.index,
            "name": self.name,
            "kind": self.kind,
            "center": self.center.as_list(),
            "free_rect": [round(v, 3) for v in (x1, y1, x2, y2)],
            "size_m": [round(max(x2 - x1, 0), 3), round(max(y2 - y1, 0), 3)],
            "area_m2": round(self.area_m2, 2),
            "standable": self.standable,
            "reachable": self.reachable,
        }


@dataclass(frozen=True)
class DoorEdge:
    """一扇门，以及它连通的两个房间。"""

    door_index: int
    position: Vec2
    width_m: float
    from_room: int
    to_room: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.door_index,
            "position": self.position.as_list(),
            "width_m": round(self.width_m, 3),
            "from_room": self.from_room,
            "to_room": self.to_room,
        }


@dataclass
class Walkable:
    """能不能走、从哪走、走到哪。**前端 3D 漫游的全部输入。**"""

    ok: bool
    spawn: Vec2
    spawn_room: int
    #: 出生时的朝向（度，场景坐标系，0 = +X 方向）
    spawn_yaw_deg: float

    collision: list[CollisionSeg] = field(default_factory=list)
    rooms: list[RoomNode] = field(default_factory=list)
    doors: list[DoorEdge] = field(default_factory=list)

    player_radius_m: float = DEFAULT_PLAYER_RADIUS_M
    eye_height_m: float = DEFAULT_EYE_HEIGHT_M
    ceiling_height_m: float = 2.8

    #: 不能走的原因。**ok=False 时必定非空**。
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": "walk" if self.ok else "fly",
            "spawn": {
                "x": round(self.spawn.x, 3),
                "y": round(self.spawn.y, 3),
                "room": self.spawn_room,
                "yaw_deg": self.spawn_yaw_deg,
            },
            "player_radius_m": self.player_radius_m,
            "eye_height_m": self.eye_height_m,
            "ceiling_height_m": self.ceiling_height_m,
            "collision": [s.to_dict() for s in self.collision],
            "rooms": [r.to_dict() for r in self.rooms],
            "doors": [d.to_dict() for d in self.doors],
            "graph": [[d.from_room, d.to_room] for d in self.doors],
            "issues": self.issues,
            "notes": self.notes,
        }


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════


def build_walkable(
    scene: Scene,
    *,
    player_radius_m: float = DEFAULT_PLAYER_RADIUS_M,
    eye_height_m: float = DEFAULT_EYE_HEIGHT_M,
) -> Walkable:
    """
    场景 → 可漫游性报告。

    **永远不抛异常。** 它是个"能不能"的判断，判断本身失败也必须给出
    一个"不能"的结论 + 原因，而不是把调用方炸掉 —— 调用方（接口层）
    拿到 `ok=False` 就降级成自由视角，功能不会整块消失。
    """
    issues: list[str] = []
    notes: list[str] = []

    half = _half_wall(scene)

    rooms = _build_rooms(scene, half, player_radius_m)
    doors, door_issues = _build_doors(scene, rooms)
    issues.extend(door_issues)

    collision = _cut_door_gaps(scene, half)

    spawn_room, spawn, yaw = _pick_spawn(rooms, doors)
    _mark_reachable(rooms, doors, spawn_room)

    # ── 四条判据，逐条判并逐条说 ──
    if not scene.quality.can_build_walls:
        issues.append(
            "墙体未通过闭合性检查，没有可靠的碰撞面 —— 无法保证不会穿墙。"
            "降级为自由视角（可穿墙飞行）"
        )
    if not rooms:
        issues.append("没有识别出任何房间，无处可站")
    if not collision:
        issues.append("没有可用的碰撞线段（墙体被门窗切光了）")

    unstandable = [r for r in rooms if not r.standable]
    if unstandable:
        names = "、".join(r.name for r in unstandable[:3])
        issues.append(
            f"{len(unstandable)} 间房净空不足（{names}"
            f"{'…' if len(unstandable) > 3 else ''}）：面积小于玩家所占，进去会卡住"
        )

    narrow = [d for d in doors if d.width_m <= 2 * player_radius_m]
    if narrow:
        issues.append(
            f"{len(narrow)} 扇门过窄（宽 {narrow[0].width_m:.2f}m ≤ "
            f"玩家直径 {2 * player_radius_m:.2f}m），过不去"
        )

    unreachable = [r for r in rooms if not r.reachable]
    total_area = sum(r.area_m2 for r in rooms)
    reachable_area = sum(r.area_m2 for r in rooms if r.reachable)
    coverage = reachable_area / total_area if total_area > 0 else 0.0

    if unreachable and coverage < MIN_REACHABLE_AREA_RATIO:
        names = "、".join(r.name for r in unreachable[:3])
        issues.append(
            f"从出生点走不到 {len(unreachable)} 间房（{names}），"
            f"可逛面积只剩 {coverage:.0%} —— 这些房间没有可通行的门"
        )
    elif unreachable:
        # **不阻断**，但必须说出来。逛不到的那几间会在界面上标灰。
        names = "、".join(r.name for r in unreachable[:4])
        notes.append(
            f"{len(unreachable)} 间房没有可通行的门，逛不到（{names}）—— "
            f"仍可漫游其余 {coverage:.0%} 的面积"
        )

    ok = (
        scene.quality.can_build_walls
        and bool(rooms)
        and bool(collision)
        and not unstandable
        and not narrow
        and coverage >= MIN_REACHABLE_AREA_RATIO
    )

    if ok:
        notes.append(
            f"可漫游：{len(rooms)} 间房 / {len(doors)} 扇门 / "
            f"{len(collision)} 段碰撞墙（门洞已切开）"
        )
    if scene.quality.issues:
        notes.extend(scene.quality.issues)

    return Walkable(
        ok=ok,
        spawn=spawn,
        spawn_room=spawn_room,
        spawn_yaw_deg=yaw,
        collision=collision,
        rooms=rooms,
        doors=doors,
        player_radius_m=player_radius_m,
        eye_height_m=eye_height_m,
        ceiling_height_m=scene.ceiling_height_m,
        issues=issues,
        notes=notes,
    )


# ══════════════════════════════════════════════════════════════════
# 碰撞几何：把门洞从墙上切掉
# ══════════════════════════════════════════════════════════════════


def _half_wall(scene: Scene) -> float:
    if not scene.walls:
        return DEFAULT_WALL_THICKNESS_M / 2
    return max(w.thickness_m for w in scene.walls) / 2


def _cut_door_gaps(scene: Scene, half: float) -> list[CollisionSeg]:
    """
    墙体折线 → 实心线段（门洞处断开）。

    ⚠️ **这一步不做，人就被关在房间里出不去** —— 见模块说明。
    """
    # 先按墙归集：每段墙上有哪些门需要开洞
    gaps: dict[int, list[tuple[float, float]]] = {}
    for op in scene.openings:
        if op.kind != "door" or op.wall_index < 0:
            continue
        if op.wall_index >= len(scene.walls):
            continue
        wall = scene.walls[op.wall_index]
        segs = wall.segments()
        # 找最近的那一段，算出切口在这段上的参数区间
        best: tuple[float, float, float] | None = None   # (dist, seg_idx, t)
        for si, (a, b) in enumerate(segs):
            dist, t = _point_seg_distance(op.center, a, b)
            if best is None or dist < best[0]:
                best = (dist, float(si), t)
        if best is None or best[0] > half + CUT_DISTANCE_TOLERANCE_M:
            continue

        _dist, si, t = best
        a, b = segs[int(si)]
        seg_len = math.dist((a.x, a.y), (b.x, b.y))
        if seg_len <= 0:
            continue
        mid = t * seg_len
        lo = max(0.0, mid - op.width_m / 2)
        hi = min(seg_len, mid + op.width_m / 2)
        if hi > lo:
            gaps.setdefault(op.wall_index, []).append((lo, hi))

    gap_ends = _door_gap_ends(scene)

    out: list[CollisionSeg] = []
    for wi, wall in enumerate(scene.walls):
        for a, b in wall.segments():
            seg_len = math.dist((a.x, a.y), (b.x, b.y))
            if seg_len <= 0:
                continue
            cuts = _merge(gaps.get(wi, []))
            # 切出剩余区间
            cursor = 0.0
            for lo, hi in cuts:
                if lo - cursor >= MIN_SEGMENT_M:
                    out.append(
                        _make_seg(a, b, cursor, lo, seg_len, wi, half, gap_ends)
                    )
                cursor = max(cursor, hi)
            if seg_len - cursor >= MIN_SEGMENT_M:
                out.append(
                    _make_seg(a, b, cursor, seg_len, seg_len, wi, half, gap_ends)
                )
    return out


def _door_gap_ends(scene: Scene) -> list[tuple[Vec2, float]]:
    """
    每扇门的**两个洞缘点**及容差。用来判断"这段墙的端点是不是贴在门洞上"。

    只在门的两个边缘取点，不是取门中心 —— 要判的是"端点是否正好在洞口边上"，
    而洞口在门中心两侧各半个门宽处。
    """
    ends: list[tuple[Vec2, float]] = []
    for op in scene.openings:
        if op.kind != "door" or op.wall_index < 0:
            continue
        if op.wall_index >= len(scene.walls):
            continue
        placed = wall_point_at(scene.walls[op.wall_index], op.offset_along_wall_m)
        if placed is None:
            continue
        p, u = placed
        for sign in (1, -1):
            ends.append(
                (
                    Vec2(p.x + u.x * op.width_m / 2 * sign,
                         p.y + u.y * op.width_m / 2 * sign),
                    0.03,
                )
            )
    return ends


def _make_seg(a: Vec2, b: Vec2, lo: float, hi: float, seg_len: float,
              wi: int, half: float, gap_ends: list[tuple[Vec2, float]]
              ) -> CollisionSeg:
    """切出一段碰撞线，并算出它的**渲染**端点（墙角外延、门洞边缘不外延）。"""
    p, q = _lerp(a, b, lo, hi, seg_len)
    ux, uy = (q.x - p.x), (q.y - p.y)
    length = math.hypot(ux, uy) or 1.0
    ux, uy = ux / length, uy / length

    def at_gap(pt: Vec2) -> bool:
        return any(
            math.dist((pt.x, pt.y), (e.x, e.y)) <= tol for e, tol in gap_ends
        )

    ra = p if at_gap(p) else Vec2(p.x - ux * half, p.y - uy * half)
    rb = q if at_gap(q) else Vec2(q.x + ux * half, q.y + uy * half)
    return CollisionSeg(a=p, b=q, wall_index=wi, render_a=ra, render_b=rb)


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """合并重叠区间。两扇门挨着时会重叠，不合并会切出负长度的碎段。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    out = [ordered[0]]
    for lo, hi in ordered[1:]:
        if lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _lerp(a: Vec2, b: Vec2, lo: float, hi: float, seg_len: float
          ) -> tuple[Vec2, Vec2]:
    """把线段上的 `[lo, hi]`（米，从 a 起算）还原成两个端点。"""
    def at(d: float) -> Vec2:
        t = d / seg_len
        return Vec2(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t)
    return at(lo), at(hi)


# ══════════════════════════════════════════════════════════════════
# 房间与门
# ══════════════════════════════════════════════════════════════════


def _build_rooms(scene: Scene, half: float, radius: float) -> list[RoomNode]:
    out: list[RoomNode] = []
    for i, r in enumerate(scene.rooms):
        if len(r.polygon) < 3:
            continue
        xs = [p.x for p in r.polygon]
        ys = [p.y for p in r.polygon]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        inset = half + radius
        out.append(
            RoomNode(
                index=i,
                name=r.name,
                kind=r.kind,
                # 中心直接用 RoomShape 的属性，不在两处各算一遍
                center=r.center,
                free_rect=(x1 + inset, y1 + inset, x2 - inset, y2 - inset),
                area_m2=r.area_m2,
            )
        )
    return out


def _build_doors(
    scene: Scene, rooms: list[RoomNode]
) -> tuple[list[DoorEdge], list[str]]:
    """
    每扇门连通哪两间房。

    ⚠️ **探法是从门中心沿墙的****法向****（垂直方向）各走 0.6m**，
    看落到哪个房间里。第一版沿墙方向探（`±u`），结果两侧都落在同一间房 ——
    因为沿墙走本来就在墙上，离两侧房间一样近。这种错会让连通图全变成
    自环，最终表现是"每间房都只跟自己连通"，于是判成不可漫游。
    """
    doors: list[DoorEdge] = []
    issues: list[str] = []
    stairs = 0.6

    for i, op in enumerate(scene.openings):
        if op.kind != "door":
            continue
        room_a = room_b = -1

        if 0 <= op.wall_index < len(scene.walls):
            placed = wall_point_at(scene.walls[op.wall_index],
                                   op.offset_along_wall_m)
            if placed is not None:
                p, u = placed
                n = Vec2(-u.y, u.x)          # 法向，不是切向
                room_a = _room_at(rooms, Vec2(p.x + n.x * stairs, p.y + n.y * stairs))
                room_b = _room_at(rooms, Vec2(p.x - n.x * stairs, p.y - n.y * stairs))

        if room_a < 0 or room_b < 0:
            # 关联不到房间的门**不猜** —— 猜错会让连通图凭空多出一条边，
            # 于是判成"能走"，而实际走过去是一堵墙。
            issues.append(
                f"第 {i} 扇门的两侧未能都识别出房间（{room_a} / {room_b}），"
                f"该门不计入通行图"
            )
            continue

        doors.append(
            DoorEdge(
                door_index=i,
                position=op.center,
                width_m=op.width_m,
                from_room=room_a,
                to_room=room_b,
            )
        )
    return doors, issues


def _room_at(rooms: list[RoomNode], p: Vec2) -> int:
    """
    点在哪个房间里。用**净空矩形**判，并且带一点外扩。

    带外扩是必要的：门就开在墙上，而净空是从墙面往里缩的 ——
    严格判定的话，门两侧的探针点会落进"墙里"，一间房都匹配不到。
    """
    for r in rooms:
        x1, y1, x2, y2 = r.free_rect
        if x1 - 0.5 <= p.x <= x2 + 0.5 and y1 - 0.5 <= p.y <= y2 + 0.5:
            return r.index
    return -1


def _pick_spawn(rooms: list[RoomNode], doors: list[DoorEdge]
                ) -> tuple[int, Vec2, float]:
    """
    出生点：**门最多的那间房**（通常就是客厅），门数相同取面积大的。

    为什么不是"第一间"或"面积最大的"：门最多的是动线枢纽，从那儿出发
    能最快看到最多空间；从一间只有一个门的卧室醒来，第一印象是"怎么出不去"。
    """
    if not rooms:
        return -1, Vec2(0.0, 0.0), 0.0

    degree: dict[int, int] = {r.index: 0 for r in rooms}
    for d in doors:
        degree[d.from_room] = degree.get(d.from_room, 0) + 1
        degree[d.to_room] = degree.get(d.to_room, 0) + 1

    best = max(rooms, key=lambda r: (degree.get(r.index, 0), r.area_m2))
    yaw = _facing_into_room(best)
    return best.index, best.center, yaw


def _facing_into_room(room: RoomNode) -> float:
    """
    初始朝向：沿房间**较长的那条净空轴**看。

    这样一进来看的是房间的纵深方向，而不是"面壁"（正对着 0.1m 外的墙）。
    朝向错了不会报错，只是第一眼看到一堵墙，观感很差。
    """
    x1, y1, x2, y2 = room.free_rect
    return 0.0 if (x2 - x1) >= (y2 - y1) else 90.0


def _mark_reachable(rooms: list[RoomNode], doors: list[DoorEdge],
                    spawn_room: int) -> None:
    """从出生点做广度优先，标出哪些房间走得到。"""
    if spawn_room < 0:
        return
    adj: dict[int, set[int]] = {r.index: set() for r in rooms}
    for d in doors:
        if d.from_room in adj and d.to_room in adj:
            adj[d.from_room].add(d.to_room)
            adj[d.to_room].add(d.from_room)

    seen = {spawn_room}
    queue = [spawn_room]
    while queue:
        cur = queue.pop()
        for nxt in adj.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)

    for r in rooms:
        r.reachable = r.index in seen


__all__ = [
    "CollisionSeg", "RoomNode", "DoorEdge", "Walkable",
    "build_walkable",
    "DEFAULT_PLAYER_RADIUS_M", "DEFAULT_EYE_HEIGHT_M",
    "MIN_REACHABLE_AREA_RATIO",
]
