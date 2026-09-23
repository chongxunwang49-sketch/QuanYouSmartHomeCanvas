"""
家具目录与匹配器。

这个文件的重点不是"覆盖率"，而是**钉住三个真实踩过的误判** ——
它们都不是构造出来的边缘情况，是从三套真实方案里 127 个品类名跑出来的。
"""

from __future__ import annotations

from typing import get_args

import pytest

from backend.app.schemas.plan import PlanStyle
from backend.app.services.furniture import catalog


# ══════════════════════════════════════════════════════════════════
# 目录本身
# ══════════════════════════════════════════════════════════════════


def test_目录可加载且条目齐全():
    specs = catalog.specs()
    assert len(specs) >= 40, "目录条目异常少，可能 JSON 被改坏了"
    ids = [s.id for s in specs]
    assert len(ids) == len(set(ids)), f"id 重复：{sorted(ids)}"


def test_每个条目都有合法的安装方式与正尺寸():
    for s in catalog.specs():
        assert s.mount in ("floor", "wall", "ceiling"), f"{s.id} 的 mount 非法：{s.mount}"
        assert s.width_m > 0 and s.depth_m > 0 and s.height_m > 0, f"{s.id} 尺寸非正"
        assert s.aliases, f"{s.id} 没有别名，永远匹配不上"
        assert s.color_role in ("wood", "fabric", "metal", "stone", "green", "white")


def test_墙面件必须有离地高度():
    """
    `wall_height` 只在 mount=wall 时有意义。少了它，挂墙件会被摆到地上 ——
    这正是把 `mount` 做成显式字段要防的事（吊灯落地）。
    """
    for s in catalog.specs():
        if s.mount == "wall":
            assert s.wall_height_m > 0, f"{s.id} 是挂墙件但没有 wall_height"


def test_三种风格都有完整配色():
    table = catalog.styles()
    # 后端 PlanStyle 里有 6 个风格，但只有默认三套需要配色
    # （另外三个是"可选项"，可以没有专属配色，会回落到 modern）
    for key in ("modern", "nordic", "chinese"):
        assert key in table, f"缺少 {key} 的配色"
        p = table[key]
        for role in ("wood", "fabric", "metal", "stone", "green", "white"):
            assert role in p.palette, f"{key} 配色缺 {role}"
            assert p.palette[role].startswith("#")
        assert p.wall.startswith("#") and p.floor.startswith("#")


# ══════════════════════════════════════════════════════════════════
# 匹配器 —— 三个真实误判的回归
# ══════════════════════════════════════════════════════════════════


def test_L形橱柜不能被认成沙发():
    """
    真实踩过：`L 形橱柜（成品柜体组合）` 被判成 **L 形沙发**。

    根因是只按"命中的别名最长"排序 —— 沙发有别名 `l 形`（2 字），
    比橱柜的 `橱柜`（2 字）不短，先到先得就错了。
    引入房间上下文后，厨房这个房间让橱柜胜出。
    """
    spec, alias = catalog.match("L 形橱柜（成品柜体组合）", "厨房")
    assert spec is not None
    assert spec.id == "kitchen_l", f"被认成了 {spec.id}（命中别名 {alias}）"


def test_通顶鞋柜不能被认成通顶衣柜():
    """真实踩过：`入户通顶鞋柜（定制）` 被判成 **通顶衣柜**。姓名里"鞋柜"二字在。"""
    spec, alias = catalog.match("入户通顶鞋柜（定制）", "走廊")
    assert spec is not None
    assert spec.id == "shoe_cabinet", f"被认成了 {spec.id}（命中别名 {alias}）"


def test_三人布艺沙发能匹配上():
    """
    `三人沙发` 这个别名匹配不到 `三人布艺沙发`（中间夹了材质词），
    必须补 `布艺沙发`。这类"中间插词"的失败在真实数据里很常见。
    """
    spec, _ = catalog.match("三人布艺沙发", "客厅")
    assert spec is not None and spec.id == "sofa_3"


@pytest.mark.parametrize(
    "name,room,expect",
    [
        ("1.8m 双人床", "主卧", "bed_double"),
        ("1.4m 长条餐桌（可坐 4-6 人）", "餐厅", "dining_table"),
        ("通顶定制衣柜（占一面短墙）", "主卧", "wardrobe_tall"),
        ("电动晾衣架", "阳台", "drying_rack"),
        ("人体工学椅", "书房", "office_chair"),
        ("几何纹地毯", "客厅", "rug"),
        ("镜柜+壁龛收纳", "卫生间", "wall_shelf"),
        ("成品浴室柜（挂墙式）", "卫生间", "vanity"),
    ],
)
def test_真实品类名能匹配到预期条目(name, room, expect):
    """抽样自三套真实方案 —— 名字都在 `seed_data/` 之外，是模型现编的。"""
    spec, _ = catalog.match(name, room)
    assert spec is not None, f"{name!r} 没匹配上"
    assert spec.id == expect, f"{name!r} 认成了 {spec.id}，期望 {expect}"


def test_完全不认识的词返回None而不是硬凑():
    """
    匹配不上就返回 None，由调用方**如实上报"未建模"** ——
    不能随便挑一个条目凑数，那会让 3D 里出现一件凭空长出来的家具。
    """
    spec, alias = catalog.match("量子传送门", "客厅")
    assert spec is None and alias == ""
    assert catalog.match("", "客厅") == (None, "")


# ══════════════════════════════════════════════════════════════════
# 风格配色回落
# ══════════════════════════════════════════════════════════════════


def test_未知风格回落到modern而不是报错():
    """
    旧数据里可能存着 `luxury`（前端曾用过而后端没有的值）。
    为配色报错不值得 —— 3D 该照常渲染出来，只是没有专属配色。
    """
    assert catalog.style_palette("luxury").label == catalog.style_palette("modern").label
    assert catalog.style_palette("").label == catalog.style_palette("modern").label


def test_三个默认风格的配色互不相同():
    """三套方案在 3D 里"看得出差别"全靠这个 —— 配色撞了就等于没做。"""
    floors = {k: catalog.style_palette(k).floor for k in ("modern", "nordic", "chinese")}
    assert len(set(floors.values())) == 3, f"地面色重复：{floors}"
    woods = {k: catalog.style_palette(k).palette["wood"] for k in ("modern", "nordic", "chinese")}
    assert len(set(woods.values())) == 3, f"木色重复：{woods}"


def test_可选风格的集合与后端枚举一致():
    """
    目录里配了色的风格应当是后端 `PlanStyle` 的子集 ——
    配了后端不认识的风格等于白配，反过来后端有而目录没有则只能回落。
    """
    assert set(catalog.styles()) <= set(get_args(PlanStyle)), (
        f"目录里有后端不认识的风格：{set(catalog.styles()) - set(get_args(PlanStyle))}"
    )
