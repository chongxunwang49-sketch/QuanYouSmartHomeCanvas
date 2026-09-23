"""
几何内核 —— 把 A-01 的像素级户型 JSON 归一化成**米制场景**。

═══════════════════════════════════════════════════════════════════
为什么要有这一层
═══════════════════════════════════════════════════════════════════
同一个户型要被渲染两次：

    2D 矢量图（SVG，AC-07）+ 物品热区（AC-09 / AC-28）
    3D 场景（Three.js）+ 第一人称漫游

如果两个渲染器各自去解释 A-01 的输出，就会出现「2D 图上的材质热点
和 3D 里的墙对不上」这种极难排查的问题 —— 因为它们各自推导比例尺、
各自处理 Y 轴方向、各自决定原点。

所以坐标只归一化**一次**，两个渲染器共用同一份米制场景。

放在后端而不是前端，还有两个具体理由：
  1. AC-07 与 AC-28 的验证方式都是「自动化测试」，而本项目对前端的
     测试覆盖是零；放后端能被 pytest 直接测
  2. 比例尺推导要用到 `total_area` 与尺寸标注的交叉校验，属于纯计算，
     放前端只会让同一份逻辑在两处各写一遍
"""

from .normalize import (
    Vec2,
    WallSeg,
    Opening,
    RoomShape,
    Scene,
    normalize_layout,
    derive_scale,
    wall_point_at,
)
from .walkable import (
    DEFAULT_EYE_HEIGHT_M,
    DEFAULT_PLAYER_RADIUS_M,
    CollisionSeg,
    DoorEdge,
    RoomNode,
    Walkable,
    build_walkable,
)

__all__ = [
    "Vec2", "WallSeg", "Opening", "RoomShape", "Scene",
    "normalize_layout", "derive_scale", "wall_point_at",
    # 可漫游性（第一人称行走的几何前提）
    "Walkable", "RoomNode", "DoorEdge", "CollisionSeg", "build_walkable",
    "DEFAULT_PLAYER_RADIUS_M", "DEFAULT_EYE_HEIGHT_M",
]
