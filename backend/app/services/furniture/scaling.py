"""
3D 场景的**等比放大** —— 为了让全部家具都摆得下。

═══════════════════════════════════════════════════════════════════
它为什么存在
═══════════════════════════════════════════════════════════════════
需求方口径（2026-09-26，明确采纳）：

> 如果家具没有放置完，或者出现家具异常的重叠，说明该 3D 屋的面积不够，
> 应该等比例进行放大，直到可以在家具不重叠的情况下放进全部家具。

═══════════════════════════════════════════════════════════════════
⚠️ 代价与边界，写在这里免得以后有人以为它没有代价
═══════════════════════════════════════════════════════════════════
**只影响 3D。** 2D 矢量户型图、物品热区、材料造价、五维诊断全部按
**真实尺寸**走 —— 那些地方放大就是撒谎（一个被放大的面积乘上单价，
会算出一个不存在的造价）。

因此 3D 里的尺寸与平面图**不再等比例对应**。所以有一条硬要求：
**界面上必须显示放大倍数**（`SceneViewer` 的 HUD 里那句"已等比放大 ×k"）。
不显示的话，用户会拿 3D 目测房间大小 —— 而那是个错的数，
正好是本项目最反对的「看起来合理的错误」。

**家具尺寸不变，变的是房子。** 这正是需求方选的那一支：家具看着是正常
大人，房子显得宽敞。反过来（缩家具）视觉上等价，但会让家具看着偏小。

墙厚、门宽、层高**一起**放大（等比），所以门窗与房间的相对关系不变 ——
放大后的 3D 仍然是平面图的一个等比例拷贝，只是尺子不同。

═══════════════════════════════════════════════════════════════════
⚠️ 它解决不了什么
═══════════════════════════════════════════════════════════════════
放**不**解决「同类只摆一件」和「家具占地达上限」这两类拒绝 ——
它们不是空间问题，放大一万倍也消不掉。

实测（8 份真实解析 × 3 种风格 = 24 组）拒绝原因分布：

    同类已有一件   135 次   39%   ← 政策，放大无用
    几何放不下     117 次   34%   ← 放大解决
    超出房间轮廓    54 次   16%   ← 已由 `clip_to_walls` 解决
    占地达上限      42 次   12%   ← 政策，但放大后额度按 k² 增长，会自动松开

所以 `place_room` 给每条拒绝打了一个 `kind`：只有 `"space"` 的那些
才算"摆不下"，`"policy"` 的另说。**不分开的话，"有 N 件摆不下"
这个数字永远归不了零，而用户会以为放大没生效。**
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "SCALE_LADDER", "MAX_SCENE_SCALE",
    "space_rejections", "scale_walkable_payload",
]

#: 放大倍数的候选阶梯。取**从小到大第一个够用的**，所以放得下时不放大。
#: 不用连续搜索：放大是给"摆不下"兜底的，差 2% 的边界没有意义，
#: 而一个固定阶梯让同一个户型每次得到同一个倍数（AC-32 的确定性口径）。
SCALE_LADDER: tuple[float, ...] = (1.0, 1.1, 1.25, 1.4, 1.6, 1.8, 2.0, 2.5)

#: 上限。再大就不是"这间房有点挤"，而是解析出来的尺寸本身错了 ——
#: 那种情况下把房子放大 10 倍只会做出一个荒唐的模型，
#: 不如如实说"摆不下"。
MAX_SCENE_SCALE: float = SCALE_LADDER[-1]


def space_rejections(result: dict[str, Any]) -> int:
    """
    有几件是**因为空间**没摆下的。

    ⚠️ 只数 `kind == "space"` 的。「同类已有一件」「占地达上限」是政策，
    放大房间消不掉它们 —— 混在一起数，这个计数器永远归不了零。
    """
    return sum(
        1 for r in (result.get("rejected") or []) if r.get("kind") == "space"
    )


def _scaled(v: Any, k: float) -> Any:
    """点 / 分量列表 / 标量，一律乘 k。"""
    if isinstance(v, (list, tuple)):
        return [float(x) * k for x in v]
    return float(v) * k


def scale_walkable_payload(payload: dict[str, Any], k: float) -> dict[str, Any]:
    """
    把 `/layout/{id}/walkable` 的返回体**原地**等比放大 k 倍。

    ⚠️ **三处不缩，缩了就是错的：**

      · `walkable.doors[].along` / `.normal` —— 它们是**单位方向向量**，
        只表示朝向，乘 k 就不再是单位向量，门扇会歪掉、转轴会跑偏。
      · `player_radius_m` / `eye_height_m` —— **人的尺寸**。放大的是房子，
        人不变；把玩家半径也乘 k 等于把人也放大了，那等于什么都没做。
      · `plan_transform.scale` —— 它是「米 → SVG 像素」的换算率，
        要**除** k 才能让同一间房映射到同一个像素位置。

    `plan_transform.scale` 那条最隐蔽：小地图上的绿点是拿
    `offset + 计划坐标 × scale` 算的。只放大几何、不调这个换算率的话，
    绿点会以房间中心为原点向外漂 —— 走两步就飘出户型图了。
    而它在截图里看起来"只是有点偏"，很容易被当成浮点误差放过去。
    """
    if abs(k - 1.0) < 1e-9:
        return payload

    scene = payload.get("scene")
    if isinstance(scene, dict):
        for w in scene.get("walls") or []:
            w["points"] = [_scaled(p, k) for p in (w.get("points") or [])]
            for key in ("length_m", "thickness_m"):
                if key in w:
                    w[key] = _scaled(w[key], k)
        for o in scene.get("openings") or []:
            if "center" in o:
                o["center"] = _scaled(o["center"], k)
            if "width_m" in o:
                o["width_m"] = _scaled(o["width_m"], k)
        for r in scene.get("rooms") or []:
            r["polygon"] = [_scaled(p, k) for p in (r.get("polygon") or [])]
            if "area_m2" in r:
                r["area_m2"] = float(r["area_m2"]) * k * k
        for key in ("width_m", "depth_m", "ceiling_height_m"):
            if key in scene:
                scene[key] = _scaled(scene[key], k)
        # px_per_m 不缩：它是"源图每米多少像素"，属于那张**图片**，不是场景。

    walk = payload.get("walkable")
    if isinstance(walk, dict):
        for r in walk.get("rooms") or []:
            if "free_rect" in r:
                r["free_rect"] = _scaled(r["free_rect"], k)
            for key in ("center", "size_m"):
                if key in r:
                    r[key] = _scaled(r[key], k)
            if "area_m2" in r:
                r["area_m2"] = float(r["area_m2"]) * k * k
        for d in walk.get("doors") or []:
            for key in ("position", "hinge"):
                if key in d:
                    d[key] = _scaled(d[key], k)
            if "width_m" in d:
                d["width_m"] = _scaled(d["width_m"], k)
            # along / normal：单位向量，**不缩**
        for seg in walk.get("collision") or []:
            for key in ("a", "b", "render_a", "render_b"):
                if seg.get(key):
                    seg[key] = _scaled(seg[key], k)
        sp = walk.get("spawn")
        if isinstance(sp, dict):
            for key in ("x", "y"):
                if key in sp:
                    sp[key] = _scaled(sp[key], k)
        if "ceiling_height_m" in walk:
            walk["ceiling_height_m"] = _scaled(walk["ceiling_height_m"], k)
        # player_radius_m / eye_height_m：**人的尺寸，不缩**

    t = payload.get("plan_transform")
    if isinstance(t, dict):
        if "scale" in t and t["scale"]:
            t["scale"] = float(t["scale"]) / k
        if "draw_depth_m" in t:
            t["draw_depth_m"] = _scaled(t["draw_depth_m"], k)
        if "draw_width_m" in t:
            t["draw_width_m"] = _scaled(t["draw_width_m"], k)
        # offset_x / offset_y 是画布留白，与尺度无关

    return payload
