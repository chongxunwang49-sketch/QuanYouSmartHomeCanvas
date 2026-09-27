"""
每个门窗洞口**离最近的墙有多远**，以及当前 1.0m 的容差把谁挡在门外。

    python scripts/probes/_probe_wall_snap.py            # 三份演示图的真实解析产物

背景：`normalize._locate_on_wall` 超过 **1.0m** 就返回 `(-1, 0)`，
而解析给的门中心实测能偏到 **0.96～1.20m**（`walkable.py` 的注释里有记录）。
差一点点就整扇门被丢掉 —— 3D 里既没有门扇、墙上也没有洞，
而平面图上那扇门是画着的。这就是需求方说的"3D 小屋没有通外的大门"。
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

from backend.app.services.geometry import normalize as N  # noqa: E402

CANDIDATES = [1.0, 1.2, 1.5, 1.8, 2.0]


def wall_distances(center, walls):
    """(最近距离, 次近距离) —— 用来判断"放宽之后会不会吸到另一面墙"。"""
    ds = []
    for w in walls:
        for a, b in w.segments():
            d, _t = N._point_seg_distance(center, a, b)
            ds.append(d)
    ds.sort()
    return (ds[0] if ds else 1e9, ds[1] if len(ds) > 1 else 1e9)


def main() -> int:
    files = sorted((ROOT / "logs" / "demo-parse").glob("*-raw.json"))
    if not files:
        files = [ROOT / "scripts" / "fixtures" / "golden_layout.json"]
    for f in files:
        raw = json.loads(f.read_text(encoding="utf-8"))
        layout = raw.get("layout") if isinstance(raw, dict) and "layout" in raw else raw
        from backend.app.services.render import render_plan_for
        scene, _plan = render_plan_for(layout)
        print(f"\n════ {f.name}  ({len(scene.walls)} 面墙) ════")
        print(f"   {'#':>3} {'kind':<7} {'离最近墙':>9} {'离次近墙':>9}  当前(1.0m)  放宽后")
        missed = 0
        for i, op in enumerate(scene.openings):
            d1, d2 = wall_distances(op.center, scene.walls)
            cur = "保留" if op.wall_index >= 0 else "**丢掉**"
            loose = "→ " + next((f"{c}m 收下" for c in CANDIDATES if d1 <= c), "仍然太远")
            if op.wall_index < 0:
                missed += 1
            print(f"   {i:>3} {op.kind:<7} {d1:>9.2f} {d2:>9.2f}  {cur:<10} {loose}")
        print(f"  → 当前被丢掉的洞口：{missed} 个（门窗合计 {len(scene.openings)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
