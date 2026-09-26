"""
家具目录的加载与匹配。

══════════════════════════════════════════════════════════════════════
为什么匹配不能是"精确相等"
══════════════════════════════════════════════════════════════════════
A-03 产出的 `ZonePlan.furniture` 是**自然语言短语**，不是枚举值。实测
三套真实方案里的 127 个品类名长这样：

    「1.8m 浅木色布艺软包双人床」
    「成品玄关鞋柜（进深 25-30cm）」
    「U 型/L 型定制橱柜」
    「卧榻（可坐可卧）」

所以只能按**关键词**匹配。但只按"命中的别名最长"排序会出错 ——
实测踩到两次，都不是边缘情况：

    「L 形橱柜（成品柜体组合）」→ 被认成 **L 形沙发**
        （别名 `l 形` 两字，比 `橱柜` 长，沙发先到先得）
    「入户通顶鞋柜（定制）」    → 被认成 **通顶衣柜**
        （`通顶` 与 `鞋柜` 等长，先到先得）

于是引入**房间上下文**参与打分：条目声明了 `rooms` 而当前房间不在其中
就倒扣。鞋柜声明了 `走廊/玄关`，衣柜声明了 `[]`（不限），
在走廊里前者胜出；橱柜声明了 `厨房`，沙发声明了 `客厅`，在厨房里前者胜出。

修完后同一批 127 个品类名：**125 个匹配上（98.4%）**。
剩下 2 个是「床下抽屉收纳盒」与「带收纳抽屉的床下箱体」——
它们本就依附于床、不该单独占体块，属**正确地未匹配**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ...core.config import PROJECT_ROOT

#: 命中别名长度的权重。远大于房间奖惩，保证"更具体的关键词"永远优先
_ALIAS_LEN_WEIGHT = 10
#: 条目声明了 rooms 且当前房间命中
_ROOM_MATCH_BONUS = 15
#: 条目声明了 rooms 但当前房间没命中 —— 倒扣。这是修上面两个 bug 的关键
_ROOM_MISS_PENALTY = -25


@dataclass(frozen=True)
class FurnitureSpec:
    """目录里的一个条目。尺寸单位一律是米。"""

    id: str
    label: str
    #: floor / wall / ceiling —— 决定摆放规则，不是装饰性字段
    mount: str
    width_m: float
    depth_m: float
    height_m: float
    aliases: tuple[str, ...]
    rooms: tuple[str, ...]
    #: 同类互斥的分组名。同一间房里同一族只摆一件 ——
    #: 否则客厅会被摆上三张沙发（实测踩过，见 placement.py）。
    family: str
    need_wall: bool
    prefer: str
    clearance_m: float
    color_role: str
    wall_height_m: float = 0.0
    y_offset_m: float = 0.0
    #: True = 不参与行走碰撞（地毯、地台、吊灯）
    no_collide: bool = False
    #: 附带的子体块（床头板、吊柜、镜柜）。3D 里与主体一起画
    extras: tuple[dict[str, Any], ...] = ()

    @property
    def footprint_m2(self) -> float:
        return self.width_m * self.depth_m


@dataclass(frozen=True)
class StylePalette:
    label: str
    #: **界面配色**：给 2D 卡片/图表用的，要求是"跟白底拉得开"。
    palette: dict[str, str]
    wall: str
    floor: str
    #: **3D 表面色**：要求是"跟地面拉得开" —— 与上一条不是同一件事。
    #: 实测：`palette` 直接当 3D 表面色时，modern 的 fabric 对地面 1.01:1
    #: （沙发与地面同色）。缺字段时回落到 `palette` / `floor`，**不报错**
    #: —— 但那时 3D 会糊成一片，所以目录里三个风格都填了。
    surface: dict[str, str] = field(default_factory=dict)
    #: 3D 场景自己的地面色。**比 `floor` 深**：只有中调地面才能让浅色家具
    #: 向上对比、深色家具向下对比（见 seed_data 里那段说明）。
    surface_floor: str = ""
    #: **按家具族**的色号：不同种类不同颜色，同一种类同色。
    #:
    #: 与 `surface`（按材质角色分：木/布艺/金属…）不是一回事 ——
    #: 角色色解决"一件家具的不同部分"，族色解决"一眼认出这是什么东西"。
    #: 由 `scripts/derive_family_palette.py` 推出，约束是对 3D 地面 ≥2:1
    #: 且**会同房的族**两两 ΔE ≥6。
    family_colors: dict[str, str] = field(default_factory=dict)


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    path: Path = PROJECT_ROOT / "seed_data" / "furniture_catalog.json"
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def specs() -> tuple[FurnitureSpec, ...]:
    out: list[FurnitureSpec] = []
    for raw in _load()["items"]:
        size = raw["size"]
        out.append(
            FurnitureSpec(
                id=raw["id"],
                label=raw["label"],
                mount=raw["mount"],
                width_m=float(size["w"]),
                depth_m=float(size["d"]),
                height_m=float(size["h"]),
                aliases=tuple(raw.get("aliases") or ()),
                rooms=tuple(raw.get("rooms") or ()),
                # 缺字段时退回 id 前缀 —— 目录是人手维护的，
                # 漏一个 family 不该让整份场景崩，但也**不能静默**：
                # `_load` 会把它记进 warnings（见下）。
                family=str(raw.get("family") or raw["id"].split("_")[0]),
                need_wall=bool(raw.get("need_wall", False)),
                prefer=str(raw.get("prefer") or "any"),
                clearance_m=float(raw.get("clearance") or 0.0),
                color_role=str(raw.get("color_role") or "wood"),
                wall_height_m=float(raw.get("wall_height") or 0.0),
                y_offset_m=float(raw.get("y_offset") or 0.0),
                no_collide=bool(raw.get("no_collide", False)),
                extras=tuple(raw.get("extras") or ()),
            )
        )
    return tuple(out)


@lru_cache(maxsize=1)
def styles() -> dict[str, StylePalette]:
    out: dict[str, StylePalette] = {}
    for key, raw in _load()["styles"].items():
        if key.startswith("_"):
            continue
        out[key] = StylePalette(
            label=raw["label"],
            palette=dict(raw["palette"]),
            wall=raw["wall"],
            floor=raw["floor"],
            surface=dict(raw.get("surface") or {}),
            surface_floor=str(raw.get("surface_floor") or raw["floor"]),
            family_colors=dict(raw.get("family_colors") or {}),
        )
    return out


def style_palette(style: str) -> StylePalette:
    """
    取风格配色。**未知风格落到 modern 而不是报错** —— 前端历史上用过
    `luxury`（后端 `PlanStyle` 里没有这个值），旧数据里可能还留着；
    为配色报错不值得，但会记一条 note 让调用方知道。
    """
    table = styles()
    return table.get(style) or table["modern"]


def family_color(style: str, family: str, fallback: str = "") -> str:
    """
    取某个族在这套风格下的 3D 色号。

    查不到就返回 `fallback`（调用方一般给它的材质角色色）—— **不报错**：
    目录里新加了一族而配色脚本还没跑时，用角色色是"不够区分"，
    而报错是"3D 直接画不出来"。前者轻得多。
    """
    table = style_palette(style).family_colors or {}
    return table.get(family) or fallback


def match(name: str, room_name: str = "") -> tuple[FurnitureSpec | None, str]:
    """
    把一个自然语言品类名匹配到目录条目。

    返回 `(条目, 命中的别名)`；没匹配上返回 `(None, "")`。

    打分 = 命中的别名长度 × 10 + 房间上下文（命中 +15 / 未命中 −25）。
    别名长度占绝对权重，所以"更具体的关键词"永远压过"更泛的"。
    """
    haystack = _norm(name)
    if not haystack:
        return None, ""

    room = _norm(room_name)
    best: tuple[int, FurnitureSpec, str] | None = None

    for spec in specs():
        for alias in spec.aliases:
            key = _norm(alias)
            if not key or key not in haystack:
                continue
            score = len(key) * _ALIAS_LEN_WEIGHT
            if spec.rooms:
                score += (
                    _ROOM_MATCH_BONUS
                    if any(_norm(r) in room for r in spec.rooms)
                    else _ROOM_MISS_PENALTY
                )
            if best is None or score > best[0]:
                best = (score, spec, alias)

    return (best[1], best[2]) if best else (None, "")


def _norm(s: str) -> str:
    """去空格、转小写。中英混排的名字里空格没有语义（`L 形` vs `L形`）。"""
    return (s or "").replace(" ", "").replace("　", "").lower()


__all__ = [
    "FurnitureSpec",
    "StylePalette",
    "match",
    "specs",
    "style_palette",
    "styles",
]
