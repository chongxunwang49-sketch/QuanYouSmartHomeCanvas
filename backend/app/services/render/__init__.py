"""
渲染层 —— 把米制场景变成能看的东西。

═══════════════════════════════════════════════════════════════════
两个渲染器，一份坐标
═══════════════════════════════════════════════════════════════════
    几何内核 (services/geometry)  ──▶  Scene（米制，Y 向上）
                                        │
                        ┌───────────────┴───────────────┐
                        ▼                               ▼
              svg.py  VectorRenderer             (M5+) three.js 3D
              AC-07 / AC-09 / AC-28              第一人称漫游
                        │
                        └── 都由 projection.Projection 投到画布

**这个包不重新解释户型 JSON，也不碰像素坐标。** 它只接受 `Scene`。
理由是几何归一化里的四个坑（比例尺、Y 轴、原点、墙体拓扑）只需要、
也只允许被填一次 —— 两个渲染器各填一遍，就会出现"热区说客厅在这儿、
3D 说客厅在那儿"这种没人能一眼看出来的错。

═══════════════════════════════════════════════════════════════════
模块分工
═══════════════════════════════════════════════════════════════════
`projection`  米 ↔ 像素。**渲染与热区共用的唯一一份变换**（防漂移）
`svg`         矢量图渲染。纯字符串拼接，零依赖，永不失败
`hotspots`    热区几何 + 材料价格。不用目标检测，全部由坐标推导
`geo_check`   AI 图的几何一致性自检（AC-28）。默认关闭，见该模块说明
"""

from .projection import (
    DEFAULT_MIN_SIDE_PX,
    DEFAULT_PAD_PX,
    Projection,
    build_projection,
)
from ..geometry import Scene, normalize_layout
from .svg import RenderedPlan, render_scene_svg, wall_point_at
from .hotspots import (
    DOOR_CATEGORY,
    FLOOR_CATEGORY,
    WALL_CATEGORY,
    HotspotGeom,
    PricedHotspot,
    build_hotspots,
    hotspot_geometry,
    hotspot_payload,
)
from .geo_check import (
    CalibrationReport,
    GeoCheck,
    calibration_report,
    check_geometry_consistency,
    edge_iou,
    geometry_iou,
    resolve_threshold,
)

#: 接口层用的**规范渲染参数**。
#:
#: ⚠️ `GET .../plan.svg` 与 `GET .../hotspots` 是两次**独立**的 HTTP 请求。
#: 如果两边各自决定留白/画布尺寸，就会得到两套 `Projection` —— 图上一个位置、
#: 热区另一个位置，而两个接口各自看都完全正常。这正是需求文档 2.2.6 里
#: "悬停在地板上却提示主卧墙面"的成因。
#:
#: 这里靠**确定性**而不是缓存来保证一致：同样的 layout 走同一个函数，
#: `build_projection` 是纯函数，两次调用必然得到逐字段相同的参数。
CANONICAL_MIN_SIDE_PX = DEFAULT_MIN_SIDE_PX
CANONICAL_PAD_PX = DEFAULT_PAD_PX


def render_plan_for(layout: dict) -> tuple[Scene, RenderedPlan]:
    """
    接口层**唯一**的渲染入口：`(layout JSON) → (场景, 渲染结果)`。

    两个接口都必须走这里。约定成"同一个函数"，比约定成"记得用同样的参数"
    可靠 —— 后者是纪律，前者是结构。

    失败时向上抛：调用方（接口层）负责决定是 404、400 还是降级，
    渲染层不该替它做这个决定，也不该悄悄返回一张空图。
    """
    scene = normalize_layout(layout)
    plan = render_scene_svg(
        scene, min_side_px=CANONICAL_MIN_SIDE_PX, pad_px=CANONICAL_PAD_PX
    )
    return scene, plan


__all__ = [
    # projection
    "Projection", "build_projection", "DEFAULT_MIN_SIDE_PX", "DEFAULT_PAD_PX",
    # 接口层入口
    "render_plan_for", "CANONICAL_MIN_SIDE_PX", "CANONICAL_PAD_PX",
    # svg
    "RenderedPlan", "render_scene_svg", "wall_point_at",
    # hotspots
    "HotspotGeom", "PricedHotspot", "hotspot_geometry", "build_hotspots",
    "hotspot_payload", "FLOOR_CATEGORY", "WALL_CATEGORY", "DOOR_CATEGORY",
    # geo check
    "GeoCheck", "CalibrationReport", "check_geometry_consistency", "geometry_iou",
    "edge_iou", "resolve_threshold", "calibration_report",
]
