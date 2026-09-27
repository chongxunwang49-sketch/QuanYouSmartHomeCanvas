"""
一次性探针：3D 里**入户门有没有洞**。

    python scripts/probes/_probe_entrance_door.py                      # 用冻结的黄金解析产物
    python scripts/probes/_probe_entrance_door.py --layout <path.json>  # 用别处存下来的 layout

需求方 2026-09-28：
> 在动态漫游时，我发现你做的房间没有房间到外面的出口…平面图是显示的房间
> 朝外的大门的…仅仅是让生成的 3d 小屋有通外的大门，跟平面图对应。

「入户门」在这个项目里的定义是**开在外墙上的那扇门**：它两侧不会都有房间，
所以 `DoorEdge.passable = False`（看得见、走不出去）。它会画出来，
但**墙上有没有洞**是另一条代码路径（`_cut_door_gaps`），本探针就是把这两件事
分别量出来，看是哪一环漏的。

输出三张表：
  ① 解析报出来的每个门洞：`wall_index` / `offset_along_wall_m` / 宽度
  ② 场景里的每扇门：`passable` / 位置 / 落在哪面墙
  ③ 每扇门所在墙上、门的中心处**有没有碰撞线段**（有 = 墙是实的 = 没开洞）
"""

from __future__ import annotations

import json
import math
import pathlib
import sys

def _project_root() -> pathlib.Path:
    """
    往上找到项目根（**不要写死层数**）。

    ⚠️ 这里原先写的是 `Path(__file__).resolve().parent.parent` —— 在 `scripts/` 下
    正好是项目根。2026-09-28 把这批一次性探针收进 `scripts/probes/` 之后，
    它就变成了 `scripts/`：于是 `sys.path` 插错、`import backend` 直接失败，
    而 `ROOT / "演示素材"` 也会指到一个不存在的地方。
    改成"往上找带 backend/app 的那一级"，以后无论挪到哪一层都对。
    """
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend" / "app").is_dir():
            return parent
    return here.parent


ROOT = _project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass

from backend.app.services.geometry.walkable import build_walkable  # noqa: E402
from backend.app.services.render import render_plan_for  # noqa: E402


def main() -> int:
    src = ROOT / "scripts" / "fixtures" / "golden_layout.json"
    if "--layout" in sys.argv:
        src = pathlib.Path(sys.argv[sys.argv.index("--layout") + 1])
    raw = json.loads(src.read_text(encoding="utf-8"))
    # 传进来的可能是整份任务结果（含 layout 字段），也可能直接就是 layout
    layout = raw.get("layout") if isinstance(raw, dict) and "layout" in raw else raw
    print(f"输入：{src.name}  layout_id={layout.get('layout_id')}\n")

    scene, _plan = render_plan_for(layout)
    walk = build_walkable(scene)

    print(f"场景：{len(scene.walls)} 面墙 / {len(scene.openings)} 个洞口")
    print("\n① 解析报出来的洞口")
    print(f"   {'#':>3} {'kind':<7} {'wall':>5} {'offset':>8} {'width':>7}  center")
    for i, op in enumerate(scene.openings):
        off = "—" if op.offset_along_wall_m is None else f"{op.offset_along_wall_m:.2f}"
        print(f"   {i:>3} {op.kind:<7} {op.wall_index:>5} {off:>8} "
              f"{op.width_m:>7.2f}  ({op.center.x:.2f}, {op.center.y:.2f})")

    print(f"\n② 场景里的门（{len(walk.doors)} 扇）")
    print(f"   {'#':>3} {'passable':<9} {'width':>7}  位置              落在哪面墙")
    for d in walk.doors:
        wi = _wall_of(scene, d.position)
        print(f"   {d.door_index:>3} {str(d.passable):<9} {d.width_m:>7.2f}  "
              f"({d.position.x:>5.2f}, {d.position.y:>5.2f})   wall {wi}")

    print("\n③ 每扇门的**中心处有没有碰撞线段**（有 = 墙是实的 = 这个洞没切开）")
    for d in walk.doors:
        blocked = [s for s in walk.collision if _dist_pt_seg(d.position, s) < 0.30]
        flag = "✗ 没开洞" if blocked else "✓ 有洞"
        print(f"   {d.door_index:>3}  {flag}   位置 ({d.position.x:.2f}, {d.position.y:.2f})"
              f"  passable={d.passable}")

    print(f"\n可漫游：ok={walk.ok} mode={'walk' if walk.ok else 'fly'}")
    if walk.issues:
        print("issues:")
        for it in walk.issues:
            print(f"  · {it}")
    return 0


def _dist_pt_seg(p, seg) -> float:
    ax, ay = seg.a.x, seg.a.y
    bx, by = seg.b.x, seg.b.y
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom <= 1e-12:
        return math.dist((p.x, p.y), (ax, ay))
    t = max(0.0, min(1.0, ((p.x - ax) * dx + (p.y - ay) * dy) / denom))
    return math.dist((p.x, p.y), (ax + t * dx, ay + t * dy))


def _wall_of(scene, p) -> int:
    best, best_d = -1, 1e9
    for wi, wall in enumerate(scene.walls):
        for a, b in wall.segments():
            d = _dist_pt_seg(p, type("S", (), {"a": a, "b": b})())
            if d < best_d:
                best, best_d = wi, d
    return best


if __name__ == "__main__":
    raise SystemExit(main())
