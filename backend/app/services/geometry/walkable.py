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
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from .normalize import (
    DEFAULT_WALL_THICKNESS_M,
    WALL_SNAP_TOLERANCE_M,
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

#: 由 `offset_along_wall_m` 推出来的门位，与解析给的 `center` 最多能差多远。
#:
#: 超过就认为这个 offset 不可信（例如墙序被动过），**宁可不切也不切错位置**。
#: 它拦的是"定位到了另一面墙上"这种数量级的错误，不是解析噪声。
#:
#: ⚠️ 2026-09-27 起**与 `normalize.WALL_SNAP_TOLERANCE_M` 是同一个值**。
#:    这里原来自写一个 1.5，而"门窗挂到哪面墙"那边卡 1.0 —— 两个数问的是
#:    同一个问题（门离墙多远还算得准），却宽严不一，结果把离墙 1.11m 的
#:    那个洞口挡在门外（3D 里既没门扇也没洞）。那扇实测是**客厅↔阳台的
#:    推拉门**，不是入户门 —— 更正记录见 `normalize.WALL_SNAP_TOLERANCE_M`。
#:    现在只有一份定义。
MAX_DOOR_CENTER_OFFSET_M = WALL_SNAP_TOLERANCE_M

#: 切出来的碎段短于这个长度就丢掉（门在墙角时会切出这种）。
MIN_SEGMENT_M = 0.02

#: 门中心到「它应该连的那间房」的最大距离。超过就不认这条边 ——
#: 宁可标成不可通行，也不要凭空连出一条现实中不存在的通路。
MAX_DOOR_TO_ROOM_M = 1.2

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
    """
    一扇门：它连通的两个房间，**以及 3D 里画出这扇门所需的几何**。

    ══════════════════════════════════════════════════════════════════
    为什么后端要算铰链和法向
    ══════════════════════════════════════════════════════════════════
    3D 里要让门能开关，需要三样东西：**转轴在哪**、**门扇朝哪边长**、
    **往哪边开**。这三样全都能从 `wall_index` + `offset_along_wall_m`
    推出来 —— 而推它们的代码已经在 `wall_point_at` 里了。

    如果让前端自己推，就会多出第二套"从墙和偏移算门框"的计算。
    两套只要差一点，表现就是**门扇和门洞对不上**：门挂偏半个门宽、
    或者转轴跑到墙里面去。而这类偏差在画面上不一定显眼。

    所以这里一次算清，前端只管摆位置。
    """

    door_index: int
    position: Vec2
    width_m: float
    from_room: int
    to_room: int
    #: 转轴位置（米）。门扇绕它旋转
    hinge: Vec2
    #: 沿墙的单位方向：从铰链指向门洞的另一端
    along: Vec2
    #: 墙的单位法向。门扇"全开"时朝这个方向
    normal: Vec2
    #: 这扇门**通不通**。
    #:
    #: ⚠️ 加这个字段的原因是"平面图与 3D 不一致"（2026-09-26）：
    #: 原先判定失败的门**直接不进 `doors`** —— 于是它不出现在 3D 里，
    #: 而矢量平面图画的是**全部**门洞。实测演示户型 9 扇门里
    #: 常有一扇（入户门）被丢掉，用户看到的就是"平面图上有门、
    #: 3D 里那面墙上没有"。
    #:
    #: 所以改成：**门一律画出来，只是不通的那些不连进通行图。**
    #: 物理上也对 —— 入户门本来就该看得见、但走不出去。
    #:
    #: ⚠️ 放在字段表**末尾**是有原因的：dataclass 不允许无默认值的
    #: 字段跟在有默认值的后面。放中间会让整个类的构造直接 TypeError。
    passable: bool = True
    #: 这扇门是不是**通往户外的门**（入户门 / 大门）。
    #:
    #: 判据是几何的、不是猜的：门的一侧是房间、另一侧**不是任何房间**
    #: （见 `_is_exterior_door`）。入户门本来就该看得见、走不到别的房间去，
    #: 所以它同时也是 `passable=False` 的那一类。
    #:
    #: ⚠️ 需求方 2026-09-28：「在动态漫游时，我发现你做的房间没有房间到
    #:    外面的出口…平面图是显示的房间朝外的大门的…仅仅是让生成的 3d
    #:    小屋有通外的大门，跟平面图对应。」这个字段就是那条要求的落点 ——
    #:    有没有通外的门，现在是**算出来并且说得出口**的，而不是"看不见就算了"。
    is_entrance: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.door_index,
            "position": self.position.as_list(),
            "width_m": round(self.width_m, 3),
            "from_room": self.from_room,
            "to_room": self.to_room,
            "passable": self.passable,
            "is_entrance": self.is_entrance,
            "hinge": self.hinge.as_list(),
            "along": [round(self.along.x, 4), round(self.along.y, 4)],
            "normal": [round(self.normal.x, 4), round(self.normal.y, 4)],
        }


@dataclass
class Walkable:
    """能不能走、从哪走、走到哪。**前端 3D 漫游的全部输入。**"""

    ok: bool
    spawn: Vec2
    spawn_room: int
    #: 出生时的朝向（度，场景坐标系，0 = +X 方向）。**行走模式用这个。**
    spawn_yaw_deg: float

    #: 自由视角（俯瞰）的朝向：从出生点**朝整个户型的中心**看。
    #:
    #: ⚠️ 为什么不能和上面共用一个：那个是"沿房间长轴看"（为了不面壁），
    #: 而自由视角在 6m 高处俯瞰 —— 它要的是"看到整个户型"。
    #: 实测踩过（2026-09-26）：出生点落在一份户型的「餐厅」，靠 +X 边缘，
    #: 而"沿长轴看"恰好让它**朝房子外面看** —— 打开 3D 是一大片没有内容的
    #: 地面，户型整个在身后。用户的原话是"3D 是空的"。
    spawn_yaw_fly_deg: float = 0.0

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
                "yaw_fly_deg": self.spawn_yaw_fly_deg,
            },
            "player_radius_m": self.player_radius_m,
            "eye_height_m": self.eye_height_m,
            "ceiling_height_m": self.ceiling_height_m,
            "collision": [s.to_dict() for s in self.collision],
            "rooms": [r.to_dict() for r in self.rooms],
            "doors": [d.to_dict() for d in self.doors],
            # 通行图只含**真能走**的门。画出来的门可以多于图里的边 ——
            # 入户门就是"看得见、走不出去"的那一扇。
            "graph": [[d.from_room, d.to_room] for d in self.doors if d.passable],
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
    doors, door_issues, dropped_doors = _build_doors(scene, rooms)
    issues.extend(door_issues)

    # ⚠️ 被去重丢掉的那几扇门**不再单独开洞**：门扇只画一扇，洞也只该有一个。
    #    不传这个集合的话会留下"有口没有门"（需求方 2026-09-27 明确点出这条）。
    collision = _cut_door_gaps(scene, half, skip=set(dropped_doors))

    spawn_room, spawn, yaw = _pick_spawn(rooms, doors)
    _mark_reachable(rooms, doors, spawn_room)

    # ── 一扇通外的门都没有时，如实说 ──
    #
    # ⚠️ 需求方 2026-09-28：3D 里"没有通外的大门"。这条要求有两半，
    #    这一半是"**有没有**都得说清楚"：有就标出来（门上的 `is_entrance`
    #    在 `_build_doors` 里已经算好），一扇都没有就明说 —— 一个封死的盒子
    #    如果什么都不说，用户只会以为是自己没找到门。
    if doors and not any(d.is_entrance for d in doors):
        # ⚠️ 放 `issues` 而不是 `notes` 是有教训的：**界面上只渲染 issues**，
        #    `notes` 目前没有任何消费方（前端 grep 不到）。这句话本来是写给
        #    用户看的（"这个户型的围墙是整圈闭合的"），写进 notes 就等于没写 ——
        #    而"信号写进了一个没人看的地方"正是本项目最反对的那类失败。
        #    它不该阻断漫游（自由视角照常可用），所以只是条 issue，不参与 ok 判定。
        issues.append(
            "没有识别到通往户外的门（入户门）—— 3D 里这户的围墙是整圈闭合的。"
            "平面图上若有入户门，换一张门洞更清晰的户型图重试通常能读出来；"
            "现在也可以继续用自由视角查看整个户型。"
        )

    # ── 门与门洞必须一一对应；进不去的房间要说出来 ──
    issues.extend(_audit_doors_and_access(rooms, doors, collision, scene))

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

    # ⚠️ 只看**能走的**门。不可通行的门（入户门）本来就过不去，
    #    把它算进"过窄"会给用户一条查不出原因的告警。
    narrow = [d for d in doors
              if d.passable and d.width_m <= 2 * player_radius_m]
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
        issues.append(
            f"从出生点走不到 {len(unreachable)} 间房"
            f"（{_name_list(unreachable, 3)}），"
            f"可逛面积只剩 {coverage:.0%} —— 这些房间没有可通行的门"
        )
    elif unreachable:
        # **不阻断**，但必须说出来。逛不到的那几间会在界面上标灰。
        notes.append(
            f"{len(unreachable)} 间房没有可通行的门，逛不到"
            f"（{_name_list(unreachable, 4)}）—— "
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
        # ⚠️ 用 spawn_room 反查，不要指望 `_pick_spawn` 里的局部变量 ——
        #    它在另一个函数里，这里拿不到（第一版就写了 `best`，直接 NameError）。
        spawn_yaw_fly_deg=_facing_house_center(
            rooms,
            next((r for r in rooms if r.index == spawn_room), None),
        ),
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


def _wall_seg_at(wall: WallSeg, offset_m: float | None) -> tuple[int, float] | None:
    """
    墙上第 `offset_m` 米落在哪一段、段内参数是多少。

    与 `normalize.wall_point_at` 是同一套遍历（那个返回点，这里返回索引+参数），
    因为它们回答的是同一个问题 —— 门挂在墙的哪个位置。
    """
    if offset_m is None:
        return None
    travelled = 0.0
    for si, (a, b) in enumerate(wall.segments()):
        seg_len = math.dist((a.x, a.y), (b.x, b.y))
        if seg_len <= 1e-9:
            continue
        if travelled + seg_len >= offset_m:
            t = (offset_m - travelled) / seg_len
            return si, min(1.0, max(0.0, t))
        travelled += seg_len
    return None


def _cut_door_gaps(
    scene: Scene, half: float, skip: Collection[int] = (),
) -> list[CollisionSeg]:
    """
    墙体折线 → 实心线段（门洞处断开）。

    ⚠️ **这一步不做，人就被关在房间里出不去** —— 见模块说明。

    ══════════════════════════════════════════════════════════════════
    ⚠️ 门洞位置**一律从 `wall_index` + `offset_along_wall_m` 推**
    （`wall_point_at` 那一套），和门扇、2D 平面图、连通图用**同一个锚点**。
    ══════════════════════════════════════════════════════════════════
    2026-09-27 修：原先这里拿 `opening.center` 去"找最近的墙段"，
    并以"离墙 ≤ 半墙厚 + 容差（合计 0.55m）"为准入。而解析给的 `center`
    实测能离墙 **0.96～1.20 m**（`_build_doors` 的注释里早就写了这件事）——
    于是那几扇门**根本没被切开**：墙上没有洞，门扇却照画。

    用户看到的就是两句话：「平面图上这面墙有门，3D 里是实的」和
    「明明生成了门，却走不过去」。演示户型 6 扇门里 **4 扇**中招
    （wall 1 / 5 / 7 各自是一整段；wall 8 / 9 只因为 center 恰好贴墙才有洞）。

    现在两处用同一个锚点，**门与门洞一一对应**这句话就是构造出来的，
    不是靠容差碰运气。

    Args:
        skip: 被判为「重复、已并入另一扇门」的 opening 下标 —— 不再单独开洞，
              否则会出现"有口没有门"（`_dedupe_doors` 丢掉的那扇只剩洞）。
    """
    # 先按墙归集：每段墙上有哪些门需要开洞
    gaps: dict[int, list[tuple[float, float]]] = {}
    for i, op in enumerate(scene.openings):
        if op.kind != "door" or op.wall_index < 0:
            continue
        if i in skip:
            continue
        if op.wall_index >= len(scene.walls):
            continue
        wall = scene.walls[op.wall_index]
        segs = wall.segments()

        # ── ① 主路径：按墙上的偏移量定位（与门扇同一套）──
        best: tuple[float, float, float] | None = None   # (dist, seg_idx, t)
        hit = _wall_seg_at(wall, op.offset_along_wall_m)
        if hit is not None:
            si, t = hit
            a, b = segs[si]
            p = Vec2(a.x + t * (b.x - a.x), a.y + t * (b.y - a.y))
            dist = math.dist((p.x, p.y), (op.center.x, op.center.y))
            # 校验：由偏移量算出的点也得离解析给的 center 不太远，
            # 否则说明 offset 不可信（解析改了墙序之类），退回按 center 投影
            if dist <= half + MAX_DOOR_CENTER_OFFSET_M:
                best = (dist, float(si), t)

        # ── ② 兜底：没有 offset 或校验不过时，退回"找离 center 最近的段" ──
        if best is None:
            if op.offset_along_wall_m is not None:
                continue          # 有 offset 但校验不过 —— 宁可不切，也不切错地方
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

    ══════════════════════════════════════════════════════════════════
    ⚠️ 这里换过一次算法，因为**固定距离的探针在窄空间里必然打偏**
    ══════════════════════════════════════════════════════════════════
    初版是「从门中心沿墙的**法向**各走 0.6m，看落到哪个房间里」。
    它在一份走廊净宽只有 **0.264m** 的真实数据上翻了车：
    0.6m 的探针直接跨过整条走廊打到对面，两侧落进同一间房，
    连通图少一条边 —— 那间房以及它背后的房间全部变成孤岛。

    更早还有一版是沿墙的**切向**探（±u），两侧也都落在同一间房，
    因为沿墙走本来就在墙上。

    两次翻车的共同点：**用「走一段再看落在哪」去回答一个拓扑问题**。
    距离是猜的，而空间宽度不是常量。

    现在的做法不问「走多远」，直接问「这扇门在哪两间房之间」：
    沿法向把每间房分到门的一侧或另一侧，然后**各取最近的那间**。
    与空间宽窄无关。
    """
    issues: list[str] = []

    # ── ① 投影：把每个门洞落到它所属的那面墙上 ──
    #
    # ⚠️ 投影之后的点 `p` 才是这扇门**在场景里的位置**。解析给的
    #    `op.center` 是模型读图读出来的，实测能离墙 0.5～1.1m
    #    （见下面 `_dedupe_doors` 的实测数字）。
    anchored: list[tuple[int, Any, Vec2, Vec2, float]] = []
    for i, op in enumerate(scene.openings):
        if op.kind != "door":
            continue
        if not (0 <= op.wall_index < len(scene.walls)):
            # ⚠️ 这些 issues 会**显示在 3D 页的问题清单上**，所以不写
            #    "通行图"这种内部结构名。
            issues.append(f"第 {i} 扇门没有关联到任何墙体，不参与连通判断")
            continue
        placed = wall_point_at(scene.walls[op.wall_index],
                               op.offset_along_wall_m)
        if placed is None:
            issues.append(f"第 {i} 扇门在墙上定位失败，不参与连通判断")
            continue
        p, u = placed
        off = math.dist((p.x, p.y), (op.center.x, op.center.y))
        anchored.append((i, op, p, u, off))

    # ── ② 去重：识别重复报出的同一扇门 ──
    kept, duplicates = _dedupe_doors(anchored, rooms)
    for loser, winner in duplicates:
        issues.append(
            f"第 {loser} 扇门与第 {winner} 扇门落在同一面墙的同一处、"
            f"连通同一对房间（相距 {SAME_DOOR_GAP_M}m 以内，小于解析自身的"
            f"位置误差）—— 判定为同一扇门被识别了两次，"
            f"只保留位置更贴墙的那一扇"
        )

    # ── ③ 定房间、建 DoorEdge ──
    doors: list[DoorEdge] = []
    for i, op, p, u, _off in kept:
        n = Vec2(-u.y, u.x)          # 法向，不是切向
        a, b = _rooms_beside(rooms, p, n)

        # ⚠️ **判不出两侧房间的门不再被丢掉，而是标 `passable=False` 照画。**
        #    典型的就是入户门：它开在外墙上，所有房间都在同一侧，
        #    拓扑上本来就不该连通 —— 但它**在平面图上是一扇门**，
        #    3D 里也必须有，否则两处对不上。详见 `DoorEdge.passable`。
        passable = a >= 0 and b >= 0
        if not passable:
            # ⚠️ 这句话会**原样显示在界面上**，所以不能带 Markdown 星号
            #    （本项目有测试扫这个，第一次就抓到了我写的 `**`）。
            issues.append(
                f"第 {i} 扇门的两侧没能都识别出房间，"
                f"这扇门仍然会画出来，但走不过去（不参与连通判断）"
            )

        # ⚠️ 通不通户外的判据在**构造时**就算好 —— `DoorEdge` 是 frozen
        #    dataclass，构造之后再赋值会 `FrozenInstanceError`（第一次就撞上了）。
        is_entrance = _is_exterior_door(rooms, p, n)

        # 铰链取门洞的一端。哪一端都行（门可以左开也可以右开），
        # 取靠近墙起点的这一端，保证同一份输入每次得到同一扇门 ——
        # 否则重放时门会左右横跳。
        half = op.width_m / 2
        doors.append(
            DoorEdge(
                door_index=i,
                # ⚠️ **用投影点，不用 `op.center`。**
                #
                # 这里原本放的是 `op.center`（解析给的原始中心），而
                # `hinge` / `along` 来自投影 —— 于是同一个"门在哪"有了
                # 两个互相矛盾的值，实测差 0.48m（第 0 扇门：
                # center=(3.10, 5.68)，而墙在 y=5.20）。
                #
                # 后果是**看得见的**：3D 里的门楣（门洞上方那道墙）
                # 是按 `position` 摆的，于是它飘在离墙半米的地方；
                # 而"离哪扇门最近"也是按 `position` 量的，量的位置不是门。
                # 用户的原话是"门的位置和平面图对不上"。
                position=p,
                width_m=op.width_m,
                from_room=a if passable else -1,
                to_room=b if passable else -1,
                passable=passable,
                is_entrance=is_entrance,
                hinge=Vec2(p.x - u.x * half, p.y - u.y * half),
                along=u,
                normal=n,
            )
        )
    return doors, issues, [loser for loser, _winner in duplicates]


#: 同一面墙、同一对房间的两扇门，投影点相距小于这个值就判定为**同一扇**。
#:
#: ══════════════════════════════════════════════════════════════════
#: 阈值不是我拍的，是**量出来的解析误差的两倍**
#: ══════════════════════════════════════════════════════════════════
#: `scripts/inspect_layout.py` 实测（演示户型 2026-09-24）：7 个门洞里
#: 有 6 个被解析模型放偏，**离墙 0.48 ~ 0.60m**（平均 0.51）。也就是
#: 这份解析给出的"门在哪"自带约 0.6m 的不确定度。
#:
#: 两个投影点相距不足 2×0.6=1.2m 时，**它们之间的距离比测量误差本身还小** ——
#: 那就没有依据说它们是两扇门。取 1.2m 做阈值是这条推理的直接结果，
#: 不是"看着差不多"。
#:
#: ⚠️ 实测第一版取的是"一个门宽 0.9m"，**它漏掉了真实的重复**：
#: 第 3 扇与第 5 扇投影到同一面墙（都连通「餐厅↔卫生间」），投影点相距
#: **1.11m** > 0.9 → 没合并。而它们的原始中心是 (6.56, 2.36) 与
#: (7.67, 1.25)，y 相差 1.11m、落在**两间不同的房**里 —— 一个门洞被
#: 读成了两扇门，各偏一边。这正是需求方说的"一个根本不应该存在的门
#: 立在墙边"，以及"两个门贴得很近时按 F 响应的不是自己想开的那扇"。
#:
#: ⚠️ **只看距离会误合并**（同一面墙上相邻两间房的门可以很近），
#:    所以这里同时要求**连通的是同一对房间**。两个条件都满足才合并。
SAME_DOOR_GAP_M = 1.2


def _dedupe_doors(
    anchored: list[tuple[int, Any, Vec2, Vec2, float]],
    rooms: list[RoomNode],
) -> tuple[list[tuple[int, Any, Vec2, Vec2, float]], list[tuple[int, int]]]:
    """
    去掉"同一扇门被测到两次"的重复。

    返回 `(保留的, [(被丢掉的下标, 保留的胜者下标), …])`。

    保留哪一扇：**离墙更近的那一扇** —— 那是同一扇门的两次读数里
    更可信的一次。不用"先来后到"，是因为那会让结果取决于门洞的排列顺序，
    而顺序来自模型输出，会抖。
    """
    kept: list[list[Any]] = []          # 可变：可能被更贴墙的替换掉
    dropped: list[tuple[int, int]] = []

    for item in anchored:
        i, op, p, u, off = item
        n = Vec2(-u.y, u.x)
        a, b = _rooms_beside(rooms, p, n)
        pair = tuple(sorted((a, b)))

        rival = None
        for k in kept:
            if k[0] != op.wall_index or k[1] != pair:
                continue
            if math.dist((k[2].x, k[2].y), (p.x, p.y)) < SAME_DOOR_GAP_M:
                rival = k
                break

        if rival is None:
            kept.append([op.wall_index, pair, p, u, i, op, off])
            continue

        if off < rival[6]:
            # 新的这一扇更贴墙 → 换掉旧的
            dropped.append((rival[4], i))
            kept[kept.index(rival)] = [op.wall_index, pair, p, u, i, op, off]
        else:
            dropped.append((i, rival[4]))

    return [(k[4], k[5], k[2], k[3], k[6]) for k in kept], dropped


def _name_list(rooms: list[RoomNode], limit: int) -> str:
    """
    房间名列表，**截断时必须说"等"**。

    ⚠️ 初版是 `"、".join(r.name for r in unreachable[:3])`，没有"等"。
    于是文案变成「走不到 6 间房（儿童房、餐厅、厨房）」—— 6 间却只列 3 个，
    读的人只会得出"另外 3 间是哪些"的疑问，或者更糟：以为只有这 3 间。
    **列表被截断而不说，和编一个数字是同一类问题。**
    """
    names = [r.name for r in rooms[:limit]]
    suffix = "等" if len(rooms) > limit else ""
    return "、".join(names) + suffix


def _rooms_beside(rooms: list[RoomNode], p: Vec2,
                  n: Vec2) -> tuple[int, int]:
    """
    门的两侧各是哪间房。

    沿墙法向 `n` 把每间房按**房间中心落在门的哪一侧**分开，
    两侧各取「矩形离门最近」的那间。

    比「探针走一段」稳的地方在于它不需要猜一个距离 ——
    无论走廊是 0.26m 还是 3m 宽，答案都一样。
    """
    best: dict[int, tuple[float, int]] = {}       # 侧 → (距离, 房间下标)
    for r in rooms:
        side = 1 if (r.center.x - p.x) * n.x + (r.center.y - p.y) * n.y >= 0 else -1
        d = _rect_distance(p, r.free_rect)
        if side not in best or d < best[side][0]:
            best[side] = (d, r.index)

    if 1 not in best or -1 not in best:
        return -1, -1

    # 两侧都太远，说明这扇门不在两间房之间（多半是定位偏了）。
    # 宁可标成不可通行，也不要凭空连出一条现实中不存在的通路。
    if best[1][0] > MAX_DOOR_TO_ROOM_M or best[-1][0] > MAX_DOOR_TO_ROOM_M:
        return -1, -1
    return best[1][1], best[-1][1]


def _rect_distance(p: Vec2, rect: tuple[float, float, float, float]) -> float:
    """点到轴对齐矩形的距离。点在矩形内时为 0。"""
    x1, y1, x2, y2 = rect
    dx = max(x1 - p.x, 0.0, p.x - x2)
    dy = max(y1 - p.y, 0.0, p.y - y2)
    return math.hypot(dx, dy)


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


#: 这些房间**不作出生点**：它们是人"路过"的地方，不是"待在"的地方。
#:
#: ⚠️ **实测踩过**（2026-09-26）：规则本来是"门最多的那间房"，
#: 理由是"门最多的是动线枢纽，从那儿出发能看到最多空间"。而在一份
#: **星形连通**的真实户型里（每个房间的门都直接开向走廊），走廊的门最多
#: —— 于是出生点永远是走廊。演示里按 `G` 下到地面，第一眼是一面墙，
#: 走两步就到头。
#:
#: 所以规则收窄一格：**先排除通过性空间，再按门数取**（门数相同取面积大的）。
#: 原意保住了 —— 客厅/餐厅照旧胜过一个只有单门的卧室，
#: 只是不再让"走廊"赢。
_PASSAGE_ROOM_NAMES: tuple[str, ...] = (
    "走廊", "过道", "玄关", "门厅", "玄关走廊", "入户花园", "走道",
)


def _is_passage(room: RoomNode) -> bool:
    """
    是不是"通过性空间"（走廊/过道/玄关）。

    ⚠️ **按子串判，不是按全等。** 实测踩过：模型给这间房起的名字是
    「**走道/门洞**」—— 带个斜杠后缀，全等匹配直接漏掉，
    出生点又落回了通过性空间（需求方的截图里就是它）。
    名字是模型起的，格式不受我们控制，所以判据要宽。
    """
    name = (room.name or "").strip()
    return any(p in name for p in _PASSAGE_ROOM_NAMES)


def _pick_spawn(rooms: list[RoomNode], doors: list[DoorEdge]
                ) -> tuple[int, Vec2, float]:
    """
    出生点：**门最多的那间房**（通常就是客厅），门数相同取面积大的。
    **走廊/玄关这类通过性空间不参选**（见 `_PASSAGE_ROOM_NAMES`）。

    为什么不是"第一间"或"面积最大的"：门最多的是动线枢纽，从那儿出发
    能最快看到最多空间；从一间只有一个门的卧室醒来，第一印象是"怎么出不去"。
    """
    if not rooms:
        return -1, Vec2(0.0, 0.0), 0.0

    degree: dict[int, int] = {r.index: 0 for r in rooms}
    for d in doors:
        if not d.passable:      # 不可通行的门不参与"谁是枢纽"的判断
            continue
        degree[d.from_room] = degree.get(d.from_room, 0) + 1
        degree[d.to_room] = degree.get(d.to_room, 0) + 1

    # 全是通过性空间时（比如只识别出走廊和玄关）退回全集 ——
    # 总得有个地方站，不能因为"都不合适"就没有出生点。
    candidates = [r for r in rooms if not _is_passage(r)] or list(rooms)

    best = max(candidates, key=lambda r: (degree.get(r.index, 0), r.area_m2))
    yaw = _facing_into_room(best)
    return best.index, best.center, yaw


def _facing_house_center(rooms: list[RoomNode],
                         from_room: RoomNode | None) -> float:
    """
    自由视角的朝向：从出生房间**朝整个户型的中心**看（度，场景坐标系）。

    自由视角的全部意义是"俯瞰格局"，所以它该朝着户型看 ——
    而行走模式那个"沿房间长轴看"的朝向在边缘房间上会朝屋外
    （见 `spawn_yaw_fly_deg` 的说明）。

    户型中心取**各房间中心的平均**（面积加权）—— 用 bbox 中心的话，
    L 形户型会把朝向拉到一个空角落上去。
    """
    if from_room is None:
        return 0.0
    total = sum(max(r.area_m2, 0.0) for r in rooms)
    if total <= 0 or not rooms:
        return _facing_into_room(from_room)
    cx = sum(r.center.x * max(r.area_m2, 0.0) for r in rooms) / total
    cy = sum(r.center.y * max(r.area_m2, 0.0) for r in rooms) / total
    dx, dy = cx - from_room.center.x, cy - from_room.center.y
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return _facing_into_room(from_room)      # 出生点正好在中心
    return math.degrees(math.atan2(dy, dx)) % 360.0


def _facing_into_room(room: RoomNode) -> float:
    """
    初始朝向：沿房间**较长的那条净空轴**看。

    这样一进来看的是房间的纵深方向，而不是"面壁"（正对着 0.1m 外的墙）。
    朝向错了不会报错，只是第一眼看到一堵墙，观感很差。
    """
    x1, y1, x2, y2 = room.free_rect
    return 0.0 if (x2 - x1) >= (y2 - y1) else 90.0


def _gap_exists_on_wall(collision: list[CollisionSeg], door: DoorEdge,
                        half: float) -> bool:
    """
    这扇门的位置上，墙体是不是**真的断开了**。

    判据：有没有哪一段**与门同向**的碰撞墙，横跨了门中心（投影参数落在段内部），
    且垂距在墙厚量级内。有 → 门被墙堵着（有门没有口）。
    """
    px, py = door.position.x, door.position.y
    for seg in collision:
        dx = seg.b.x - seg.a.x
        dy = seg.b.y - seg.a.y
        seg_len = math.hypot(dx, dy)
        if seg_len <= 1e-9:
            continue
        # 平行才算同一面墙（门洞只会开在它自己那面墙上）
        cross = abs((dx / seg_len) * door.along.y - (dy / seg_len) * door.along.x)
        if cross > 1e-3:
            continue
        dist, t = _point_seg_distance(Vec2(px, py), seg.a, seg.b)
        if 1e-6 < t < 1 - 1e-6 and dist <= half * 1.5 + 1e-6:
            return False
    return True


#: 判"门外面是不是房间"时，从门往两侧各探多远（米）。
#: 取 1.0 是因为墙厚最多 0.24m，探针必须跨出墙带之外才问得清"那侧有没有房间"。
_EXTERIOR_PROBE_M = 1.0


def _is_exterior_door(rooms: list[RoomNode], p: Vec2, n: Vec2) -> bool:
    """
    门的一侧是房间、另一侧不是 —— 那就是**通到户外的门**（入户门）。

    ⚠️ 用几何判，不用"能不能连通"判：`passable=False` 只能说明"两侧没都识别出
       房间"，室内门识别不清时也会是 False。而"一侧有房、另一侧空"是**外面**
       才有的形状 —— 判错的方向只会漏报，不会把室内门说成大门。

    ⚠️ 参数是位置与法向、不是 `DoorEdge`：`DoorEdge` 是 frozen dataclass，
       而这个值要在**构造它的时候**就算好（构造之后没法再赋值）。
    """
    here = _room_at(rooms, Vec2(p.x - n.x * _EXTERIOR_PROBE_M,
                                p.y - n.y * _EXTERIOR_PROBE_M))
    there = _room_at(rooms, Vec2(p.x + n.x * _EXTERIOR_PROBE_M,
                                 p.y + n.y * _EXTERIOR_PROBE_M))
    return (here >= 0) != (there >= 0)


def _audit_doors_and_access(
    rooms: list[RoomNode], doors: list[DoorEdge],
    collision: list[CollisionSeg], scene: Scene,
) -> list[str]:
    """
    门 ↔ 门洞 ↔ 可达性 的一致性自检。

    ══════════════════════════════════════════════════════════════════
    需求方 2026-09-27 的口径
    ══════════════════════════════════════════════════════════════════
    > 门和门的口都是一一对应的，不要出现只有门没有口，也不要出现只有口没有门；
    > 不要出现平面图显示这个地方可以进入，却没有相应的入口和门；
    > 一些房间没有任何入口出口这种情况应该规避。

    三条对应这里的三个检查：

      ① **有门必有口**：每扇画出来的门，它那面墙必须真的断开。
         这是 `_cut_door_gaps` 的**验收**，不是它的重复 —— 切洞那一步曾经
         因为拿错锚点（`center` 而不是 `offset_along_wall_m`）漏切 4/6 扇门，
         而那种失败在数据里看不出来（门照样画、几何照样"合法"）。
      ② **有口必有门**：洞是由门切出来的，构造上成立；`skip` 那批重复门
         也已经在切洞时排除。这里不重复检查，写成注释留痕。
      ③ **进不去的房间要说出来**：解析漏门时我们**不能凭空画一扇**
         （那是编造），但必须让用户看到"这间房进不去、原因是解析没给门"。

    返回的是**给用户看的句子**，会原样显示在 3D 页的 issues 里。
    """
    issues: list[str] = []
    half = _half_wall(scene)

    blocked = [d.door_index for d in doors
               if not _gap_exists_on_wall(collision, d, half)]
    if blocked:
        # ⚠️ 这句会显示在 3D 页上：不写 `layout_id`（内部编号），
        #    也不说"报给开发"——用户能做的动作是"换张图重试 / 告诉我们"。
        issues.append(
            "有 "
            + "、".join(f"第 {i} 扇" for i in blocked)
            + "门所在的墙没有开出对应的门洞（平面图上有门、3D 里是实墙）—— "
            "这属于几何生成的问题，麻烦把这套户型反馈给我们排查"
        )

    connected: set[int] = set()
    for d in doors:
        if d.passable:
            connected.add(d.from_room)
            connected.add(d.to_room)

    doorless = [r for r in rooms if r.index not in connected]
    if doorless:
        names = "、".join(f"「{r.name}」" for r in doorless)
        issues.append(
            f"{names}在解析结果里没有任何一扇门通往它 —— "
            "我们没有凭空给它补一扇门（那会画出一条现实里不存在的通路），"
            "所以在 3D 里进不去：这几间会标成不可达、并且不计入可漫游面积。"
            "要完整走进去，请换一张门洞更清晰的户型图重试"
        )

    # 有门、但从出生点走不过去的那批：常见于"解析漏了连通走廊的那扇门"，
    # 于是整块区域成了孤岛。同样不能凭空补门，但必须说清楚是哪几间。
    stranded = [r for r in rooms if not r.reachable and r.index in connected]
    if stranded:
        names = "、".join(f"「{r.name}」" for r in stranded)
        issues.append(
            f"{names}自己有门，但从出生点出发走不到它们（通常是解析"
            "漏掉了连接这片区域的那扇门）—— 3D 里同样走不进去，"
            "这几间在界面上会标成不可达"
        )
    return issues


def _mark_reachable(rooms: list[RoomNode], doors: list[DoorEdge],
                    spawn_room: int) -> None:
    """从出生点做广度优先，标出哪些房间走得到。"""
    if spawn_room < 0:
        return
    adj: dict[int, set[int]] = {r.index: set() for r in rooms}
    for d in doors:
        if d.passable and d.from_room in adj and d.to_room in adj:
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
