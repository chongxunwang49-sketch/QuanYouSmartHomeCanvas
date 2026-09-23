"""
可漫游性 —— 第一人称行走需要的碰撞几何与房间连通图。

═══════════════════════════════════════════════════════════════════
这一层要回答的问题只有一个：**这户型能不能走**
═══════════════════════════════════════════════════════════════════
"能不能走"不是"墙画出来了没有"，而是四件事同时成立：

  1. 墙体闭合（`Scene.quality.can_build_walls`）—— 否则没有东西可碰撞
  2. 每间房都**站得下人**（净空 > 玩家直径）—— 否则进去就卡在几何里
  3. 门洞**真的能穿过去**（宽 > 玩家直径）—— 否则看得见进不去
  4. 从出生点**走得到每一个房间** —— 否则有一块永远逛不到

四条只要有任意一条不成立，就**不能**做第一人称漫游，必须降级成自由视角
（可以穿墙飞）。降级不是失败 —— 但**静默降级是**。所以这里把每条原因
都写进 `issues`，让界面能如实说出来。

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


@dataclass(frozen=True)
class CollisionSeg:
    """一段**实心**墙。门洞处已经被切掉。"""

    a: Vec2
    b: Vec2
    wall_index: int

    @property
    def length_m(self) -> float:
        return math.dist((self.a.x, self.a.y), (self.b.x, self.b.y))

    def to_dict(self) -> dict[str, Any]:
        # 毫米精度就够 —— 碰撞体比这精细没有意义，而小数位直接决定
        # 这份 JSON 的体积（它就压在轮询响应里）。
        return {
            "a": [round(self.a.x, 3), round(self.a.y, 3)],
            "b": [round(self.b.x, 3), round(self.b.y, 3)],
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
    if unreachable:
        names = "、".join(r.name for r in unreachable[:3])
        issues.append(
            f"从出生点走不到 {len(unreachable)} 间房（{names}）—— "
            f"这些房间没有可通行的门"
        )

    ok = (
        scene.quality.can_build_walls
        and bool(rooms)
        and bool(collision)
        and not unstandable
        and not narrow
        and not unreachable
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
                    out.append(CollisionSeg(*_lerp(a, b, cursor, lo, seg_len), wi))
                cursor = max(cursor, hi)
            if seg_len - cursor >= MIN_SEGMENT_M:
                out.append(CollisionSeg(*_lerp(a, b, cursor, seg_len, seg_len), wi))
    return out


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
                center=Vec2((x1 + x2) / 2, (y1 + y2) / 2),
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
]
