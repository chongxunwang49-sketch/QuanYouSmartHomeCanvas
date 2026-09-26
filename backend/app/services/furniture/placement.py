"""
家具摆放 —— 按规则算，**不用模型**。

═══════════════════════════════════════════════════════════════════
为什么是规则而不是"让模型给坐标"
═══════════════════════════════════════════════════════════════════
模型给得出一个看起来合理的坐标 —— 而"看起来合理"正是本项目一路在防的
东西。要让坐标**站得住**，它必须同时满足：在房间里、不堵门、不压别的家具。
这三条是可计算的，交给模型反而变成"祈祷它算对了"。

与 ADR-07（预算必须由规则引擎算）、`environment.py`（环保风险按规则算）
同一条纪律：**凡是能算的，都不交给模型。**

═══════════════════════════════════════════════════════════════════
它是"最小版"，边界写清楚
═══════════════════════════════════════════════════════════════════
做出来的：
  · 每间房 2-3 件主要家具，坐标轴对齐（`rot_deg` ∈ {0,90,180,270}）
  · 靠墙 / 居中 / 靠哪面墙，按目录里的 `need_wall` 与 `prefer` 决定
  · `clearance`（前方净空）**真的留出来**，不是记一笔就算
  · 摆不下就**不放**，并说明原因 —— 宁可少一件，不塞一个错的

**不做的**（每条都是有意取舍，不是遗漏）：
  · 不做家具之间的通道宽度约束（只看单件的前方净空，不做全局动线）
  · 不做非矩形房间（房间轮廓目前是 bbox 近似，见 `normalize.py`）
  · 不做转角柜 / L 形组合（目录里的条目都是单矩形）
  · 不做碰撞之外的观感（灯光、朝向、美观）—— 那些需要人眼

═══════════════════════════════════════════════════════════════════
三条几何断言
═══════════════════════════════════════════════════════════════════
`violations()` 是唯一的判据，**摆放时用它筛，测试里直接断言它**。
两份实现迟早会分叉，而分叉的表现是"测试说没问题、场景里衣柜在走廊上"。

  ① 在本房间内、且不越入邻室
  ② 不压门洞的开启扇区
  ③ 与已摆放的家具互不重叠（含彼此的前方净空）

⚠️ 第①条里"不越入邻室"是我加的（需求口径只说了"在房间里"）。
   不加的话，L 形房间的凹口处会把衣柜摆到隔壁 —— 而"在房间 bbox 内"
   这条断言照样通过。少一条判据不会报错，只会安静地错。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import catalog

__all__ = [
    "Placement", "Box", "violations", "place_all", "place_room",
    "overlap_area", "wall_boxes", "clip_to_walls", "room_interior",
    "MAX_FURNITURE_AREA_RATIO",
]

#: 家具的**投影占地**最多占房间内轮廓面积的多少。
#:
#: ══════════════════════════════════════════════════════════════════
#: 为什么从"每间房最多 3 件"换成了面积比
#: ══════════════════════════════════════════════════════════════════
#: 原来是一个固定的件数上限，`MAX_PER_ROOM = 3`。那个数字是我拍的，
#: 它有一个很硬的后果：**同样 3 件，放在 4.7㎡ 的卫生间和 18㎡ 的客厅里
#: 是两回事** —— 小房间挤得走不动，大房间空得像没装修。
#:
#: 实测（演示户型 2026-09-24，`scripts/inspect_layout.py` 同源）：
#: 件数上限从 3 提到无限，摆下的从 25 件变 32 件 —— 而多出来的那 7 件
#: 全挤在儿童房（地毯/单人椅/斗柜/办公椅/书柜/搁板/衣帽架），
#: 主卧只从 3 件到 4 件。**该多摆的没多摆，不该多摆的堆成仓库。**
#:
#: 面积比同时解决两个方向：房间越大能摆的越多，小房间自然收得住。
#:
#: ⚠️ **0.45 是一个设计选择，不是查来的标准。** 它的依据只有一句：
#:    家具占掉投影面积之后，剩下的要够人走动。这个比例我调过 0.35 /
#:    0.45 / 0.6 三档，0.35 会把主卧的衣柜也挤掉（衣柜本来就是
#:    "家具占面积"这件事本身），0.6 会让儿童房重新堆成仓库。
#:    实测数字写在这里，谁觉得不对可以拿它去复算。
#:
#: ⚠️ **不参与碰撞的件（地毯/地台）不计入**（`no_collide`）：它们不占人
#:    走路的地方，地毯更是压在茶几底下。计入的话客厅会因为一张地毯
#:    丢掉一件真家具。
MAX_FURNITURE_AREA_RATIO = 0.45

#: 墙的尝试顺序。**固定顺序是确定性的来源之一** ——
#: 同样一间房、同样的目录，两次跑必须得到同样的结果（AC-32 的 A 类口径）。
_SIDES: tuple[str, ...] = ("S", "E", "N", "W")

#: 判断一个门窗属于哪面墙时，允许离墙线的距离（米）。
#: 房间轮廓是 bbox 近似，门窗中心未必正好落在边上，给一点容差。
_ON_WALL_TOLERANCE_M = 0.55


@dataclass(frozen=True)
class Box:
    """轴对齐矩形，米，场景坐标。"""

    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def d(self) -> float:
        return self.y2 - self.y1

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    def overlaps(self, other: Box, *, tol: float = 1e-6) -> bool:
        """相交判定。**接触（面积为零）不算重叠** —— 否则贴着墙摆就永远失败。"""
        return (
            self.x1 < other.x2 - tol and other.x1 < self.x2 - tol
            and self.y1 < other.y2 - tol and other.y1 < self.y2 - tol
        )

    def contains(self, other: Box, *, tol: float = 1e-6) -> bool:
        return (
            self.x1 <= other.x1 + tol and other.x2 <= self.x2 + tol
            and self.y1 <= other.y1 + tol and other.y2 <= self.y2 + tol
        )

    def as_dict(self) -> dict[str, float]:
        return {"x1": round(self.x1, 3), "y1": round(self.y1, 3),
                "x2": round(self.x2, 3), "y2": round(self.y2, 3)}


@dataclass
class Placement:
    """一件摆好的家具。字段与前端 `three/furniture.ts` 一一对应。"""

    spec_id: str
    label: str
    room_index: int
    room_name: str
    box: Box
    rot_deg: int
    mount: str
    height_m: float
    color_role: str
    y_offset_m: float
    no_collide: bool
    #: 3D 体块的色号（`#RRGGBB`）。**由后端解析好给前端** ——
    #: 前端不再自己查一次目录，免得两处各判一次"这个族该是什么色"。
    #: 取的是**族色**（不同种类不同色），查不到时回落到材质角色色。
    color: str = ""
    extras: tuple[dict[str, Any], ...] = ()
    #: 为什么摆在这里。**要能说出口** —— 前端悬停、报告、排错都要用。
    basis: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        cx, cy = self.box.center
        return {
            "spec_id": self.spec_id,
            "label": self.label,
            "room_index": self.room_index,
            "room_name": self.room_name,
            "x": round(cx, 3), "y": round(cy, 3),
            "w": round(self.box.w, 3), "d": round(self.box.d, 3),
            "rot_deg": self.rot_deg,
            "mount": self.mount,
            "height_m": round(self.height_m, 3),
            "color_role": self.color_role,
            "y_offset_m": round(self.y_offset_m, 3),
            # 不参与行走碰撞的（地毯/地台/吊灯）—— 前端据此决定要不要
            # 把它并进碰撞体。**由后端给**，免得前端再判一次目录语义。
            "no_collide": self.no_collide,
            "color": self.color,
            "extras": [dict(e) for e in self.extras],
            "basis": list(self.basis),
        }


# ══════════════════════════════════════════════════════════════════
# 几何：房间内轮廓、四面墙、门洞扇区
# ══════════════════════════════════════════════════════════════════


def overlap_area(a: Box, b: Box) -> float:
    """
    两个矩形重叠的面积（㎡）。不相交返回 0。

    ⚠️ 与 `Box.overlaps()` **不是一回事**：那个是布尔的、且带容差
    （"接触不算重叠"是为了让家具能贴着墙摆）。而"嵌进墙多少"要的是
    **面积**——0.5c㎡ 和 607c㎡ 都是"重叠"，但一个是浮点毛刺、
    一个是肉眼可见的穿模。判据要看得出这个区别。
    """
    ox = min(a.x2, b.x2) - max(a.x1, b.x1)
    oy = min(a.y2, b.y2) - max(a.y1, b.y1)
    return 0.0 if ox <= 0 or oy <= 0 else ox * oy


def wall_boxes(walls: Sequence[dict[str, Any]]) -> list[Box]:
    """
    把每一段墙按**它自己的厚度**变成矩形。

    ⚠️ 家具"压墙"必须按这个量，不能按墙的中线量。墙有 0.20m 厚，
    家具紧贴内表面时离中线还有 0.10m —— 按中线量永远是 0 命中，
    而它在画面上已经嵌进去了。
    """
    out: list[Box] = []
    for w in walls or []:
        half = float(w.get("thickness_m") or 0.0) / 2.0
        pts = [tuple(p) for p in (w.get("points") or [])]
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            out.append(
                Box(min(ax, bx) - half, min(ay, by) - half,
                    max(ax, bx) + half, max(ay, by) + half)
            )
    return out


def clip_to_walls(box: Box, walls: Sequence[Box]) -> Box:
    """
    把房间内轮廓**收到墙的内表面上**。

    ══════════════════════════════════════════════════════════════════
    为什么必须收这一刀
    ══════════════════════════════════════════════════════════════════
    `room_interior()` 是从房间 bbox **反推**的：bbox 内缩（半墙厚 + 玩家半径）
    得到 `free_rect`，再加回玩家半径就是"墙到墙的内轮廓"。
    这个反推的**前提是「房间 bbox 的边 == 墙的中线」**。

    实测（2026-09-26，8 份真实解析 × 3 种风格）这个前提差了 **4~6cm**：
    模型给的多边形比墙中线**外扩**了一点点，于是反推出的"内表面"落在墙
    **里面** —— 贴墙摆的家具嵌进墙体。24 组里 **54 件**中招，
    最严重的一件（厨房餐桌）嵌进去 **607 c㎡**，在 3D 里整条边没入墙中。

    所以不信反推：**按真实的墙几何再收一遍**。

    ⚠️ 只按"墙在房间的哪一侧"收，不整体内缩 —— 整体缩会把没有墙的那一侧
    也一起缩掉，那些家具就白丢了。实测：整体缩 6cm 会让十几件家具被拒，
    而按侧收只动真正有墙的那一边。
    """
    x1, y1, x2, y2 = box.x1, box.y1, box.x2, box.y2
    cx, cy = box.center
    #: 哪几侧真的被收过 —— 余量只给这几侧（见函数末尾）
    touched: set[str] = set()
    for w in walls:
        if not (x1 < w.x2 and w.x1 < x2 and y1 < w.y2 and w.y1 < y2):
            continue                      # 这面墙不在这间房的内轮廓上

        # ⚠️ **方向按墙盒的长宽比判，不能按"墙中心离房间中心多远"判。**
        #    实测踩过：卫生间只有 2.4m 高，而它左边那面墙纵贯 5m ——
        #    拿中心距离比，"横着"的差值反而更小，于是这面**竖墙被当成横墙**，
        #    算出来的 y2 变成 -0.06（比 y1 还小），整个内轮廓反过来。
        #    后果是家具全被拒（一次实测：摆下 597 → 456 件）。
        horizontal = w.w >= w.d
        wcx, wcy = w.center
        if horizontal:                     # 横墙：收上边或下边
            if wcy < cy:
                y1 = max(y1, w.y2); touched.add("y1")
            else:
                y2 = min(y2, w.y1); touched.add("y2")
        else:                              # 竖墙：收左边或右边
            if wcx < cx:
                x1 = max(x1, w.x2); touched.add("x1")
            else:
                x2 = min(x2, w.x1); touched.add("x2")

    # 兜底：万一还是收反了（比如一堵墙横穿房间），**退回原盒子**。
    # 一个退化的内轮廓会让整间房一件家具都摆不下 —— 那比嵌 4cm 严重得多。
    if x2 - x1 < 0.5 or y2 - y1 < 0.5:
        return box

    # ⚠️ **给收过的那几侧再让出 5mm。**
    #    收完之后家具是**正好贴在墙表面上**的，而坐标要经过几次
    #    浮点运算与取整 —— 实测残留 0.5mm 的相交（54 件）。
    #    0.5mm 肉眼看不见，但它会让"是否压墙"的判据永远非零，
    #    于是那条断言只能写成"小于某个容差"，而不是"没有"。
    #
    #    ⚠️ **只让收过的那几侧，不是四边一起让。** 没有墙的那一侧让出去
    #    没有任何理由 —— 白白窄了 5mm，而它是"靠这面墙摆家具"的候选来源。
    m = 0.005
    if "x1" in touched:
        x1 += m
    if "y1" in touched:
        y1 += m
    if "x2" in touched:
        x2 -= m
    if "y2" in touched:
        y2 -= m
    return Box(x1, y1, x2, y2)


def room_interior(room: dict[str, Any], *, player_radius_m: float) -> Box | None:
    """
    房间的**真实内轮廓** = `free_rect` 向外扩一个玩家半径。

    `free_rect` 的定义是"可站立矩形"（房间 bbox 内缩半墙厚 **+ 玩家半径**），
    所以加回玩家半径就得到墙到墙的内轮廓 —— 家具的背面该贴这条线，
    贴 `free_rect` 会让每件家具离墙 0.3 米，看着像飘着。
    """
    fr = room.get("free_rect")
    if not fr or len(fr) != 4:
        return None
    r = float(player_radius_m or 0.0)
    x1, y1, x2, y2 = (float(v) for v in fr)
    box = Box(x1 - r, y1 - r, x2 + r, y2 + r)
    return box if box.w > 0.1 and box.d > 0.1 else None


def _side_line(rect: Box, key: str) -> tuple[float, float, float, float]:
    """一面墙：返回 (墙线坐标, 房间内侧法向, 沿墙方向的跨度起点, 跨度终点)。"""
    if key == "S":      # y = y1，内侧朝 +y
        return rect.y1, +1.0, rect.x1, rect.x2
    if key == "N":      # y = y2，内侧朝 -y
        return rect.y2, -1.0, rect.x1, rect.x2
    if key == "W":      # x = x1，内侧朝 +x
        return rect.x1, +1.0, rect.y1, rect.y2
    return rect.x2, -1.0, rect.y1, rect.y2     # "E"


def _is_horizontal(key: str) -> bool:
    return key in ("S", "N")


def door_zones(
    doors: Iterable[dict[str, Any]], room_index: int, *, open_side_only: bool = True,
) -> list[Box]:
    """
    门洞的**开启扇区**，用一个保守的矩形近似。

    ⚠️ 真实扇区是四分之一圆盘（绕铰链、半径 = 门宽）。用它的**外接矩形**
    会多圈掉一块本来能用的地方 —— 这是**有意的保守**：
    宁可少摆一件，也不能让沙发挡住门。与 `environment.py` 里
    "对流判据宁可说'不弱'"是同一种取法。

    `open_side_only=True` 时只算门扇**开向**的那一侧（由 `normal` 决定）——
    门只往一边开，另一边不该被占。**但 `normal` 指向哪间房是我推断的**：
    取该门连通的房间中，`normal` 指向的那一间。推断不出来时退回两侧都禁。
    """
    out: list[Box] = []
    for d in doors:
        if room_index not in (d.get("from_room"), d.get("to_room")):
            continue
        hinge = d.get("hinge") or []
        along = d.get("along") or []
        normal = d.get("normal") or []
        if len(hinge) != 2 or len(along) != 2 or len(normal) != 2:
            continue
        w = float(d.get("width_m") or 0.9)
        hx, hy = float(hinge[0]), float(hinge[1])
        ax, ay = float(along[0]) * w, float(along[1]) * w

        nx, ny = float(normal[0]) * w, float(normal[1]) * w
        xs = [hx, hx + ax]
        ys = [hy, hy + ay]
        if not open_side_only:
            xs += [hx - ax]
            ys += [hy - ay]
        xs.append(hx + nx)
        ys.append(hy + ny)
        out.append(Box(min(xs), min(ys), max(xs), max(ys)))
    return out


def _blockers(
    rect: Box, openings: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    每面墙上有什么。返回 `{墙名: {"has_door":…, "has_window":…, "door_at":…}}`。

    ⚠️ **用矩形近似判断门窗在哪面墙**，不是用 `wall_index`。
    理由：房间轮廓本来就是 bbox 近似（`normalize.py` 的立场），
    把真实墙段索引映射到矩形的四条边要额外一套几何，而收益只是
    判断"这面墙上有没有窗"这种粗粒度问题。容差 `_ON_WALL_TOLERANCE_M` 覆盖
    近似误差。
    """
    info = {k: {"has_door": False, "has_window": False, "door_at": None}
            for k in _SIDES}
    for o in openings:
        c = o.get("center") or []
        if len(c) != 2:
            continue
        cx, cy = float(c[0]), float(c[1])
        for key in _SIDES:
            line, _inward, lo, hi = _side_line(rect, key)
            span_ok = (lo - 0.1) <= (cx if _is_horizontal(key) else cy) <= (hi + 0.1)
            near = abs((cy if _is_horizontal(key) else cx) - line) <= _ON_WALL_TOLERANCE_M
            if span_ok and near:
                if o.get("kind") == "door":
                    info[key]["has_door"] = True
                    info[key]["door_at"] = (cx, cy)
                else:
                    info[key]["has_window"] = True
    return info


# ══════════════════════════════════════════════════════════════════
# 候选位置
# ══════════════════════════════════════════════════════════════════


def _wall_box(spec: catalog.FurnitureSpec, rect: Box, key: str,
              offset: float | None = None) -> Box:
    """
    把一件靠墙家具贴到某面墙上。宽度沿墙、深度垂直墙面。

    `offset` 是家具**中心沿墙的位置**（米，墙坐标）。不给就取墙中点。
    """
    line, inward, lo, hi = _side_line(rect, key)
    mid = (lo + hi) / 2 if offset is None else offset
    if _is_horizontal(key):
        x1, x2 = mid - spec.width_m / 2, mid + spec.width_m / 2
        y1, y2 = (line, line + spec.depth_m) if inward > 0 else (line - spec.depth_m, line)
    else:
        y1, y2 = mid - spec.width_m / 2, mid + spec.width_m / 2
        x1, x2 = (line, line + spec.depth_m) if inward > 0 else (line - spec.depth_m, line)
    return Box(x1, y1, x2, y2)


def _wall_offsets(spec: catalog.FurnitureSpec, rect: Box, key: str) -> list[float]:
    """
    一件靠墙家具在某面墙上**值得一试的落点**（沿墙的中心坐标）。
    按"先中间、后靠角"排序，去重。

    ══════════════════════════════════════════════════════════════════
    ⚠️ 只试墙中点是不够的 —— 这条是"家具没全部放进"的主因之一
    ══════════════════════════════════════════════════════════════════
    初版 `attempts` 里每面墙只有一个候选位置：**墙中点**。后果在小房间里
    特别难看 —— 演示户型的卫生间净空 2.49×1.70m，淋浴房（0.9×0.9，
    前方净空 0.6）放在**南墙正中**之后，剩下的两个长条都放不下第二件：
    马桶无论放哪面墙，它的净空都压在淋浴房身上。

    而现实里 1.9×1.1 的卫生间是能放下「淋浴 + 马桶 + 洗手台」的 ——
    做法是**靠角摆**：淋浴房贴一个角，马桶贴另一个角，中间走人。
    只试中点等于把这个解从搜索空间里删掉了。

    这不是"放宽判据"（`violations()` 一个字没改），是**把本来摆得下、
    只是没试过的位置试出来**。摆不下仍然如实记进 `rejected`。

    顺序是**确定性的**，且中点优先：能居中的就居中（看起来是有意摆的），
    居不下才靠角。同一份输入两次跑结果一致（AC-32 的 A 类口径）。
    """
    _line, _inward, lo, hi = _side_line(rect, key)
    mid = (lo + hi) / 2
    half = spec.width_m / 2
    span = hi - lo
    if span <= spec.width_m:
        # 这面墙比家具还窄 —— 只有一个位置可试，交给判据去拒
        return [mid]

    # ⚠️ **每个落点都必须让家具整件留在墙上。** 直接取"墙长 × 0.25"会在
    #    墙长与家具宽度接近时把它推出墙外 —— 实测卫生间放浴室柜（宽 1.0）
    #    到一面 1.70m 的墙上时，0.25 落点是 lo+0.426，柜子左端伸出去 7.5cm，
    #    判据如实报「超出房间内轮廓」。那不是判据太严，是候选位置本身不合法。
    lo_ok = lo + half
    hi_ok = hi - half
    out = [
        mid,
        lo_ok,                      # 贴角：正好顶住墙角
        hi_ok,
        lo_ok + (hi_ok - lo_ok) * 0.25,
        lo_ok + (hi_ok - lo_ok) * 0.75,
    ]
    seen: list[float] = []
    for o in out:
        if lo_ok - 1e-9 <= o <= hi_ok + 1e-9 and all(abs(o - s) > 1e-6 for s in seen):
            seen.append(o)
    return seen or [mid]


def _center_box(spec: catalog.FurnitureSpec, rect: Box) -> Box:
    cx, cy = rect.center
    return Box(cx - spec.width_m / 2, cy - spec.depth_m / 2,
               cx + spec.width_m / 2, cy + spec.depth_m / 2)


def _clearance_box(spec: catalog.FurnitureSpec, item: Box, rect: Box) -> Box:
    """
    前方净空。**朝房间中心那一侧留** —— 家具背靠墙时，人要站的地方在它前面。
    """
    c = spec.clearance_m
    if c <= 0:
        return item
    cx, cy = rect.center
    ix, iy = item.center
    bx, by = item.center
    if abs(ix - cx) > abs(iy - cy):
        # 家具偏左/右 → 净空朝中间（水平方向）
        return Box(min(item.x1, bx + c if ix < cx else bx - c), item.y1,
                   max(item.x2, bx + c if ix < cx else bx - c), item.y2)
    return Box(item.x1, min(item.y1, by + c if iy < cy else by - c),
               item.x2, max(item.y2, by + c if iy < cy else by - c))


def _rot_of(key: str) -> int:
    """贴墙家具的朝向：0 = 背面朝南（+y 是正面）。"""
    return {"S": 0, "E": 90, "N": 180, "W": 270}[key]


# ══════════════════════════════════════════════════════════════════
# 判据
# ══════════════════════════════════════════════════════════════════


def violations(
    item: Box,
    *,
    interior: Box,
    neighbors: Sequence[Box] = (),
    door_boxes: Sequence[Box] = (),
    placed: Sequence[Box] = (),
    walls: Sequence[Box] = (),
    clearance: Box | None = None,
) -> list[str]:
    """
    三条断言。返回不满足的**原因列表**，空列表 = 通过。

    ⚠️ **这就是唯一的判据**：摆放时用它筛，测试里直接断言它。
    各写一份的话，分叉的表现是"测试说没问题、场景里衣柜在走廊上"。
    """
    bad: list[str] = []

    # ① 在房间内、且不越入邻室
    if not interior.contains(item):
        bad.append("超出房间内轮廓")
    for n in neighbors:
        if item.overlaps(n):
            bad.append("越入相邻房间")
            break

    # ② 不压门洞开启扇区
    for z in door_boxes:
        if item.overlaps(z):
            bad.append("挡住门洞开启范围")
            break

    # ②′ 不嵌进墙体。**按墙的真实厚度判**（见 `wall_boxes`）。
    #
    #    这一条与 ① 的 `interior.contains` **有重叠但不冗余**：
    #    `interior` 是从房间 bbox 反推的，而它比真实内表面宽 4~6cm
    #    （见 `clip_to_walls`）。这里是拿真实墙几何兜底 ——
    #    反推错的那几厘米，由这一条挡住。
    for w in walls:
        if item.overlaps(w):
            bad.append("嵌进墙体")
            break

    # ③ 与已摆放的互不重叠（含彼此的前方净空）
    for other in placed:
        if item.overlaps(other):
            bad.append("与已摆放的家具重叠")
            break
    if clearance is not None and clearance is not item:
        for other in placed:
            if clearance.overlaps(other):
                bad.append("前方净空被别人占了")
                break
    return bad


def _preference_order(
    spec: catalog.FurnitureSpec, rect: Box, blockers: dict[str, dict[str, Any]],
    door_points: Sequence[tuple[float, float]],
) -> list[str]:
    """
    按 `prefer` 给四面墙排序。**同分时按 `_SIDES` 的固定顺序** —— 确定性。
    """
    free = [k for k in _SIDES if not blockers[k]["has_door"]]
    # 靠墙家具不该占掉有门的墙；没有一面干净墙时退回全部
    candidates = free or list(_SIDES)

    if spec.prefer == "longest":
        return sorted(candidates,
                      key=lambda k: (-_side_len(rect, k), _SIDES.index(k)))
    if spec.prefer == "blank":
        # 「空墙」：不挡门窗。窗在墙上，家具靠上去仍然会挡 —— 所以有窗也扣分
        return sorted(
            candidates,
            key=lambda k: (blockers[k]["has_window"], _SIDES.index(k)),
        )
    if spec.prefer == "opposite_door":
        return sorted(candidates,
                      key=lambda k: (-_door_distance(rect, k, door_points),
                                     _SIDES.index(k)))
    return sorted(candidates, key=lambda k: _SIDES.index(k))


def _side_len(rect: Box, key: str) -> float:
    return rect.w if _is_horizontal(key) else rect.d


def _door_distance(rect: Box, key: str, door_points: Sequence[tuple[float, float]]) -> float:
    """
    这面墙**中点**到房间里最近一扇门的距离。房间没有门 → 0（全都并列）。

    ⚠️ 初版量的是"**这面墙上**那扇门到墙中点的距离"，没门的墙一律返回 1e6
    （本意是"离门无穷远、最优先"）。后果：**所有没门的墙并列在 1e6**，
    排序完全由固定顺序决定，`opposite_door` 这个偏好等于没生效 ——
    实测期望"门在南墙时选北墙"，结果选了东墙。
    语义应当是"离门远"，而不是"自己身上有没有门"。
    """
    if not door_points:
        return 0.0
    line, _inward, lo, hi = _side_line(rect, key)
    mid = ((lo + hi) / 2, line) if _is_horizontal(key) else (line, (lo + hi) / 2)
    return min(math.dist(mid, p) for p in door_points)


# ══════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════


def place_room(
    room: dict[str, Any],
    *,
    specs: Sequence[catalog.FurnitureSpec],
    doors: Sequence[dict[str, Any]],
    openings: Sequence[dict[str, Any]],
    player_radius_m: float,
    neighbors: Sequence[Box] = (),
    walls: Sequence[Box] = (),
    area_ratio: float = MAX_FURNITURE_AREA_RATIO,
    family_colors: Mapping[str, str] | None = None,
    surface_colors: Mapping[str, str] | None = None,
) -> tuple[list[Placement], list[dict[str, Any]]]:
    """
    给一间房摆家具。返回 `(摆好的, 被拒的)`。

    ⚠️ **摆不下就返回空，不塞一个错的。** 被拒的每一条都带原因，
    调用方把它们汇总成 `warnings` 给用户看 —— 与"不产出看起来合理的
    错误"是同一条纪律：宁可场景里少一件，也不要一件挡住门的。
    """
    index = int(room.get("index") or 0)
    name = str(room.get("name") or "")
    interior = room_interior(room, player_radius_m=player_radius_m)
    if interior is None:
        return [], [{"room": name, "kind": "space",
                     "reason": "房间没有可用的内轮廓（free_rect 缺失或退化）"}]
    # ⚠️ 反推出来的内轮廓会**伸进墙里** 4~6cm（见 `clip_to_walls`），
    #    先按真实墙几何收一遍，否则贴墙摆的家具全都会嵌进墙。
    if walls:
        interior = clip_to_walls(interior, walls)

    #: 这间房能放多少件家具的**占地面积**（㎡）。见 `MAX_FURNITURE_AREA_RATIO`。
    area_budget = interior.w * interior.d * area_ratio
    #: 已经用掉的占地。不参与碰撞的件（地毯/地台）不计 —— 它们不挡人走路。
    area_used = 0.0

    blockers = _blockers(interior, openings)
    zones = door_zones(doors, index)
    # `opposite_door` 要的是"离门远"，所以传**房间里门的位置**（不是门扇区）：
    # 扇区是给"别挡住"用的，位置是给"离它远点"用的，两件事。
    door_points = [
        (float(d["position"][0]), float(d["position"][1]))
        for d in doors
        if index in (d.get("from_room"), d.get("to_room"))
        and isinstance(d.get("position"), list) and len(d["position"]) == 2
    ]
    placed: list[Placement] = []
    rejected: list[dict[str, Any]] = []
    #: 因为"这间房已经摆满"而没轮到的（与"试了但摆不下"是两件事，见下）
    skipped_by_capacity: list[str] = []
    #: 本间房已经摆下的族。同一族不再摆第二件（见循环里的说明）
    families_placed: set[str] = set()

    for i, spec in enumerate(specs):
        # ⚠️ **同类只摆一件。** 实测踩过：客厅被摆上三张沙发 ——
        #    沙发是三件大件里最大的，按面积排序时它们连着占满三个名额。
        #    这不是口味问题，是规则缺口：目录里的 `family` 就是为这一条加的。
        if spec.family in families_placed:
            rejected.append({
                "room": name, "spec_id": spec.id, "label": spec.label,
                "kind": "policy",     # 不是放不下，是"这间房已经有这类了"
                "reason": f"同类已有一件（family={spec.family}）",
            })
            continue
        # 这件家具放下去会不会超出这间房的占地额度。**判据的位置很关键**：
        # 预算不是"这件放不下"的理由，是"这间房满了"的理由 ——
        # 所以它**不能挡在几何判据前面**（见下面 attempts 循环里的用法）。
        fits_budget = (
            spec.no_collide or area_used + spec.footprint_m2 <= area_budget
        )

        # ⚠️ **不参与碰撞的件（地毯/地台）不算障碍。**
        #
        #    它们平铺在地上（地毯厚 0.02m、地台 0.12m），既不挡人走路，
        #    也不挡别的家具 —— 现实里地毯就是压在床和沙发底下的。
        #
        #    把它们算进 `placed` 的后果实测很难看：儿童房里地毯铺在正中，
        #    衣柜靠墙，两者**本体并不重叠**，但衣柜的前方净空伸进了地毯
        #    的范围 → 判据报"前方净空被别人占了" → 衣柜被拒。
        #    ⚠️ 判据后来改过一次，**从裸的 `no_collide` 改成 `_occupies_floor()`**。
        #    原先那句注释写的是"`no_collide` 是目录声明过的语义，照着用就行" ——
        #    而那个前提被实测打破了：淋浴房被标成 `no_collide: true`，
        #    于是浴室柜摆进了淋浴房里，重叠 0.45㎡，3D 穿模。
        #    **一个布尔字段同时承担"人不撞它"与"别的家具不能压它"两件事，
        #    就一定会在其中一件上出错。** 详见 `_occupies_floor()` 的说明。
        item_boxes = [p.box for p in placed if _occupies_floor(p)]
        # 靠墙的按 prefer 试墙；不靠墙的试"居中" + 各面墙。
        # 每面墙上试**多个落点**（先中点后靠角），见 `_wall_offsets` 的说明。
        order = _preference_order(spec, interior, blockers, door_points)
        attempts: list[tuple[str, Box]] = []
        if not spec.need_wall:
            attempts.append(("C", _center_box(spec, interior)))
        for k in order:
            attempts.extend(
                (k, _wall_box(spec, interior, k, off))
                for off in _wall_offsets(spec, interior, k)
            )

        last_reason: list[str] = ["没有可用的候选位置"]
        for key, box in attempts:
            clear = _clearance_box(spec, box, interior)
            bad = violations(
                box, interior=interior, neighbors=neighbors, door_boxes=zones,
                placed=item_boxes, walls=walls, clearance=clear,
            )
            # ⚠️ 净空也要在房间内 —— 否则"留了净空"这件事只存在于纸面上，
            #    而净空伸到隔壁房间等于没留。这条一开始漏了，
            #    是写用例时发现"床前净空指向墙外"才补上的。
            if not bad and not interior.contains(clear):
                bad = ["前方净空超出房间"]
            if not bad and not fits_budget:
                # ⚠️ **几何上放得下，挡住它的是这间房的占地额度。**
                #
                #    这时记进"没轮到"（`skipped_by_capacity`）而不是
                #    `rejected` —— 前者是"这间房满了"，后者是"这件试过
                #    摆不下"，两种含义混在一起会让警告数字虚高。
                #    `break` 会跳过下面的 `else` 分支，正好是想要的效果：
                #    换面墙也一样超预算，没必要逐件记一行。
                skipped_by_capacity.append(spec.id)
                break
            if not bad:
                where = "房间中部" if key == "C" else {
                    "S": "南墙", "N": "北墙", "W": "西墙", "E": "东墙",
                }[key]
                families_placed.add(spec.family)
                if not spec.no_collide:
                    area_used += spec.footprint_m2
                placed.append(Placement(
                    spec_id=spec.id, label=spec.label,
                    room_index=index, room_name=name, box=box,
                    rot_deg=0 if key == "C" else _rot_of(key),
                    mount=spec.mount, height_m=spec.height_m,
                    color_role=spec.color_role, y_offset_m=spec.y_offset_m,
                    no_collide=spec.no_collide,
                    # 族色优先；查不到退回材质角色色（见 catalog.family_color）
                    color=((family_colors or {}).get(spec.family)
                           or (surface_colors or {}).get(spec.color_role, "")),
                    extras=spec.extras,
                    basis=[
                        f"放在{where}",
                        f"按 prefer={spec.prefer} 选的位置",
                        (f"前方留出 {spec.clearance_m}m 净空"
                         if spec.clearance_m > 0 else "无需前方净空"),
                    ],
                ))
                break
            last_reason = bad
        else:
            rejected.append({
                "room": name, "spec_id": spec.id, "label": spec.label,
                # 试过所有候选位置都放不下 —— 这才是**空间**问题
                "kind": "space",
                "reason": "；".join(dict.fromkeys(last_reason)),
            })

    # 因为"这间房放不下了"而没轮到的，合并成一条 —— 它不需要每件家具一行。
    # 与"试过但摆不下"分开记，两种含义分得开（见上面 `rejected` 的说明）。
    if skipped_by_capacity:
        rejected.append({
            "room": name, "spec_id": "", "label": "",
            "kind": "policy",         # 同上：是额度，不是空间
            "reason": f"本间房的家具占地已达上限"
                      f"（房间内轮廓的 {area_ratio:.0%}，约 "
                      f"{area_budget:.2f}㎡，已用 {area_used:.2f}㎡），"
                      f"另有 {len(skipped_by_capacity)} 件未尝试",
            "skipped_by_capacity": skipped_by_capacity,
        })
    return placed, rejected


def place_all(
    *,
    walkable: dict[str, Any],
    scene: dict[str, Any],
    style: str,
    room_phrases: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """
    给整份户型摆家具。

    Args:
        walkable: `build_walkable(scene).to_dict()`。
        scene: `Scene.to_dict()` —— 门窗在里面（`openings`），
               `walkable` 里没有窗。
        style: 方案风格，决定配色角色 → 色值。
        room_phrases: `{房间名: [家具短语]}`。给了就用**方案里说的那些家具**
                      （经 `catalog.match` 匹配）；不给则按房间名从目录挑。

    Returns:
        可直接进 API 响应的 dict。
    """
    rooms = walkable.get("rooms") or []
    doors = walkable.get("doors") or []
    openings = scene.get("openings") or []
    radius = float(walkable.get("player_radius_m") or 0.0)
    palette = catalog.style_palette(style)
    # 墙按**真实厚度**取矩形，用来把家具挡在墙外（见 `wall_boxes` / `clip_to_walls`）
    wall_boxes_list = wall_boxes(scene.get("walls") or [])

    interiors: dict[int, Box] = {}
    for r in rooms:
        box = room_interior(r, player_radius_m=radius)
        if box is not None:
            interiors[int(r.get("index") or 0)] = box

    all_specs = catalog.specs()
    out_rooms: list[dict[str, Any]] = []
    all_rejected: list[dict[str, Any]] = []
    warnings: list[str] = []

    used_specs: set[str] = set()
    for r in sorted(rooms, key=lambda x: int(x.get("index") or 0)):
        idx = int(r.get("index") or 0)
        name = str(r.get("name") or "")
        # 邻室 = 其它房间的内轮廓。用来拦住"摆到隔壁去"
        neighbors = [b for i, b in interiors.items() if i != idx]

        if room_phrases and room_phrases.get(name):
            specs = _specs_from_phrases(room_phrases[name], name)
        else:
            # ⚠️ 同一件家具不在两间房都摆 —— 但**只在退到目录挑时**这么做。
            #    方案里明确说了两间房都要衣柜的话，那是规划的决定，不该拦。
            specs = _specs_for_room(name, str(r.get("kind") or ""),
                                    all_specs, used_specs)

        placements, rejected = place_room(
            r, specs=specs, doors=doors, openings=openings,
            player_radius_m=radius, neighbors=neighbors, walls=wall_boxes_list,
            family_colors=palette.family_colors, surface_colors=palette.surface,
        )
        for p in placements:
            used_specs.add(p.spec_id)
        all_rejected.extend(rejected)
        out_rooms.append({
            "index": idx,
            "name": name,
            "kind": r.get("kind"),
            "area_m2": r.get("area_m2"),
            "free_rect": r.get("free_rect"),
            "placements": [p.to_dict() for p in placements],
        })

    if all_rejected:
        warnings.append(
            f"有 {len(all_rejected)} 件家具没能摆下（已跳过，没有塞进不该在的位置）"
        )
    if not any(r["placements"] for r in out_rooms):
        warnings.append("没有摆下任何家具 —— 房间净空可能过小，或房间名与目录对不上")

    return {
        "style": style,
        # ⚠️ 给的是 **surface**（3D 表面色），不是 `palette`（界面配色）。
        #    两者要求的对比对象不同：界面要跟白底拉得开，3D 要跟地面拉得开。
        #    拿 `palette` 当表面色的话，modern 的 fabric 对地面只有 1.01:1 ——
        #    沙发和地面一个色，等于没画。实测数字见 seed_data 里的字段说明。
        "palette": dict(palette.surface or palette.palette),
        "wall_color": palette.wall,
        # `surface_floor` 是 3D 专用的地面色（比 `floor` 深）。
        # `floor_color` 保留原值，给"同一页上还要画 2D 平面图"的场景用。
        "surface_floor": palette.surface_floor or palette.floor,
        "floor_color": palette.floor,
        "rooms": out_rooms,
        "placed_count": sum(len(r["placements"]) for r in out_rooms),
        "rejected": all_rejected,
        "warnings": warnings,
        # 口径写进响应里：前端不必猜这些坐标是什么坐标系的
        "notes": [
            "坐标与 /layout/{id}/walkable 同一套场景坐标系（米），"
            "前端可直接用 three/coords.ts 转换",
            "家具朝向只有 0/90/180/270 四个值（轴对齐放置），不做自由角度",
            "摆不下的家具在 rejected 里，带原因 —— 不是静默丢弃",
        ],
    }


def _specs_from_phrases(
    phrases: Sequence[str], room_name: str,
) -> list[catalog.FurnitureSpec]:
    """
    把方案里写的家具短语匹配成目录条目（用已经测好的 `catalog.match`）。

    ⚠️ **保持短语顺序**：A-03 给的是"主要家具"列表，顺序本身有主次含义，
    而 `MAX_PER_ROOM` 会截断 —— 重排一下就会把主家具截掉。
    """
    out: list[catalog.FurnitureSpec] = []
    seen: set[str] = set()
    for phrase in phrases:
        spec, _alias = catalog.match(phrase, room_name)
        if spec is None or spec.id in seen:
            continue
        seen.add(spec.id)
        out.append(spec)
    return out


#: 房间类型 → 目录里用的中文名。用来**补充房间名匹配**。
#:
#: ⚠️ 目录的 `rooms` 是按**中文房间名**写的（"主卧" / "次卧" / "卫生间"），
#: 而解析给的房间名是自由的 —— 实测出现过「儿童房」「主卧A」「婴儿房」。
#: 只按名字匹配的话，这些房间一条专属条目都命中不到，只能退到"不限房间"
#: 的大杂烩里挑，于是**儿童房摆上了地毯、单人椅、办公椅，唯独没有床**。
#:
#: 加上 `kind` 一起匹配之后，儿童房（kind=bedroom）能命中声明了"卧室"
#: 的条目。这不是"放宽判据"，是把本来就在目录里的对应关系用起来。
ROOM_KIND_LABEL: dict[str, str] = {
    "bedroom": "卧室",
    "living_room": "客厅",
    "dining_room": "餐厅",
    "kitchen": "厨房",
    "bathroom": "卫生间",
    "study": "书房",
    "balcony": "阳台",
    "entrance": "玄关",
    "storage": "储藏",
}


def _specs_for_room(
    room_name: str, room_kind: str, all_specs: Sequence[catalog.FurnitureSpec],
    used: set[str],
) -> list[catalog.FurnitureSpec]:
    """
    退路：按房间名 + 房间类型从目录里挑候选。

    排序规则（确定性）：
      1. 声明了 `rooms` 且命中这间房（**名字或类型任一命中**）→ 优先；
         一条都没命中时才用"不限房间"的。
         （与 `catalog.match` 的房间奖惩同一条理由：鞋柜不该进卧室）
      2. **占地面积大的排前** —— "主要家具"本来就该先摆，先摆的赢。
      3. 同分按 id：保证确定性。

    ⚠️ 第 2 条一开始写的是"不靠墙的排前"，理由是"不靠墙的是房间的功能中心
    （床、沙发、餐桌）"。**那个理由是错的**：床尾凳也不靠墙，还排在双人床
    前面 —— 实测结果是主卧摆了床尾凳与梳妆台、**没有床**（床被前两件挤掉了）。
    靠不靠墙与"是不是主角"没有关系，占地面积才是。

    ══════════════════════════════════════════════════════════════════
    ⚠️ "同一件家具不在两间房都摆" —— 但**要按族判，不能一刀切**
    ══════════════════════════════════════════════════════════════════
    初版是 `if s.id in used: continue`（用过的一律删）。动机是对的
    （免得整屋摆同一个衣柜），后果却很硬：主卧先摆（下标小），把
    "床头柜"占了；轮到次卧时，次卧自己的专属条目里就只剩床和桌 ——
    实测次卧**只摆下 1 件**（一张床）。而两间卧室各有一个床头柜，
    本来就是正常装修。

    第二版改成"没被用过的排前面，用过的接在后面"。**还是错的**：
    用过的排后面意味着"床"（同族里只有双人床被主卧用过）要排在一堆
    小件之后 —— 实测儿童房最终摆下的 8 件是
    「地毯 + 斗柜 + 书柜 + 搁板 + 绿植 + 垃圾桶 + 壁灯 + 床头柜」，
    **没有床**。而它预算还够，只是床排在最后，轮到它时已经排满了。

    现在的规则按**族**判，一句话：

        同族里只要还有**没用过**的条目，就把用过的那些淘汰；
        整族都被用过了，才允许复用。

    儿童房于是拿 `bed_queen`/`bed_single`（fresh）而不是主卧用过的
    `bed_double`；而"床头柜"整族只剩用过的那一个，允许复用 ——
    两间卧室各一个，正常。
    """
    keys = [catalog._norm(room_name)]
    kind_label = ROOM_KIND_LABEL.get(room_kind)
    if kind_label:
        keys.append(catalog._norm(kind_label))

    declared: list[catalog.FurnitureSpec] = []
    generic: list[catalog.FurnitureSpec] = []
    for s in all_specs:
        if s.rooms:
            if any(catalog._norm(r) in k for r in s.rooms for k in keys):
                declared.append(s)
        else:
            generic.append(s)

    # 声明了房间的优先；`rooms` 为空的（不限房间）只有在该房间没有专属条目时才用
    pool = declared or generic

    fresh_families = {s.family for s in pool if s.id not in used}
    kept = [s for s in pool if s.id not in used or s.family not in fresh_families]
    # 排序：**占地面积大的排前**（"主要家具"先摆，先摆的赢），
    # 但**平铺的附件排最后**（见 `_is_flat_accessory`），同分按 id 定序。
    return sorted(
        kept,
        key=lambda s: (_is_flat_accessory(s), -s.footprint_m2, s.need_wall, s.id),
    )


#: 低于这个厚度的件算"平铺的附件"（米）。一个台阶的高度约 0.15m ——
#: 比它矮的东西，人**不绕开它，而是走在它上面**。
FLAT_MAX_HEIGHT_M = 0.15


def _occupies_floor(p: "Placement") -> bool:
    """
    这件家具**占不占地面** —— 也就是"别的家具能不能摆在它这儿"。

    ⚠️ **判据是「落在地上 + 不是平铺件」，不是裸的 `no_collide`。**

    实测踩过（2026-09-26，演示户型连量 5 次、三种风格全中）：
    `shower`（淋浴房，0.9×0.9×**2.0m**，实心玻璃）在目录里被标成
    `no_collide: true`，于是它**不算障碍** —— 浴室柜被摆进了淋浴房内部，
    两个盒子重叠 **0.45㎡**，3D 里直接穿模。

    为什么不能只看 `no_collide`：那个字段的语义是「人撞不撞它」，
    而目录把它同时用在了两件不同的事上 —— 地毯/地台/吊灯/挂墙件
    （**本来就不占地面**），以及**被误标的淋浴房**。两个问题混在一个
    布尔里，错一个就穿模。

    为什么不能只看 `height_m`：吊灯高 0.35m、晾衣架 0.1m，按高度都"不高"，
    但它们挂在天花板上，**不该挡住下面的家具**。`mount` 说得出这件事，
    高度说不出。

    所以：**落在地上 + 不是平铺的 = 占地面。**
    """
    return p.mount == "floor" and not (
        p.no_collide and p.height_m <= FLAT_MAX_HEIGHT_M
    )


def _is_flat_accessory(spec: "catalog.FurnitureSpec") -> bool:
    """
    是不是"平铺在地面/顶面上的附件"（地毯、地台、晾衣架、墙面搁板）。

    ══════════════════════════════════════════════════════════════════
    ⚠️ 为什么排序要单独把它们拎出来
    ══════════════════════════════════════════════════════════════════
    候选是按**占地面积**从大到小排的（"主要家具先摆"）。而地毯
    2.4×1.6 = **3.84㎡，比双人床的 3.6㎡ 还大** —— 于是它排在了床前面。

    实测（演示户型 2026-09-24）儿童房因此拿到的是
    「地毯 + 斗柜 + 书柜 + 搁板 + 绿植 + 垃圾桶 + 壁灯 + 床头柜」，
    **没有床**。它预算还够，只是床排在最后，轮到它时已经排满了。

    一条地毯不该排在床前面。这不是算法问题，是"房间的主角是谁"
    这件事没被写进排序里 —— 而它可以用一个物理量说清楚：
    **人走在地毯上，不是绕开地毯。**
    """
    return spec.no_collide and spec.height_m <= FLAT_MAX_HEIGHT_M
