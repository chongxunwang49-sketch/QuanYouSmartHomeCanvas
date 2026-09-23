"""
把一份户型 JSON 渲染成 SVG（含热区），写盘给人看。

═══════════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════════
接口层要有 `layout_id` 才能出图，而 `layout_id` 得先跑一次真实的
视觉解析（约 40 秒 + Token）。调试渲染样式时等 40 秒看一张图，太慢。

这个脚本直接吃一份户型 JSON，跳过整条链路。于是：

  · 改渲染样式 → 立刻看到结果
  · 复现用户报的"这张图渲歪了" → 把他的 layout JSON 存下来喂进来
  · 演示前预生成几张图 → 现场不用等

═══════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════
    # 用内置样例（一次真实解析的结果，未经修饰）
    python scripts/render_plan.py

    # 用自己的 JSON
    python scripts/render_plan.py path/to/layout.json -o out/

    # 调画布尺寸
    python scripts/render_plan.py --min-side 2048

产出：
    <out>/plan.svg        矢量图（浏览器直接打开，可无限放大）
    <out>/hotspots.json   热区数据（坐标是**画布像素**，与 SVG 配套）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 控制台默认 cp936，不认 ⚠️ 这类符号，会在最后一句提示上崩掉
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.services.geometry import normalize_layout  # noqa: E402
from backend.app.services.render import (  # noqa: E402
    CANONICAL_MIN_SIDE_PX,
    hotspot_payload,
    render_scene_svg,
)

#: 一次真实解析的结果，原样抄下来的。**不要"美化"它** ——
#: 里面那些 `width: 0`（门宽没识别出来）正是要暴露的真实情况。
SAMPLE = {
    "rooms": [
        {"name": "卧室", "type": "bedroom", "area": 11.7, "bbox": [40, 40, 380, 300]},
        {"name": "卧室", "type": "bedroom", "area": 11.1, "bbox": [40, 300, 380, 545]},
        {"name": "客厅", "type": "living_room", "area": 23.1, "bbox": [380, 40, 725, 545]},
    ],
    "walls": [
        {"type": "unknown",
         "coords": [[40, 40], [725, 40], [725, 545], [40, 545], [40, 40]]},
        {"type": "unknown", "coords": [[380, 40], [380, 545]]},
        {"type": "unknown", "coords": [[40, 300], [380, 300]]},
    ],
    "doors": [
        {"position": [367, 197], "width": 0.0, "swing": "unknown"},
        {"position": [142, 315], "width": 0.0, "swing": "unknown"},
        {"position": [400, 407], "width": 0.0, "swing": "unknown"},
    ],
    "windows": [
        {"position": [228, 40], "width": 1.7, "orientation": "unknown"},
        {"position": [575, 545], "width": 1.8, "orientation": "unknown"},
    ],
    "dimensions": [
        {"label": "8200", "value": 8.2, "unit": "mm"},
        {"label": "5600", "value": 5.6, "unit": "mm"},
    ],
    "total_area": 45.9,
    "confidence": 0.45,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="把户型 JSON 渲染成 SVG")
    ap.add_argument("layout", nargs="?", help="户型 JSON 文件；省略则用内置样例")
    ap.add_argument("-o", "--out", default="E:/quanyou/outputs/plans",
                    help="输出目录")
    # 默认取接口层那份**规范参数**，而不是自己再写一个数 ——
    # 两边一旦不一致，脚本产出的图就和线上接口产出的不是同一张，
    # 而"照着脚本调好的样式"到线上就不对了。
    ap.add_argument("--min-side", type=int, default=CANONICAL_MIN_SIDE_PX,
                    help="画布短边的下限（AC-07 要求 ≥512）")
    ap.add_argument("--no-hotspots", action="store_true",
                    help="不画热区层（出给 ControlNet 时用）")
    args = ap.parse_args()

    if args.layout:
        src = Path(args.layout)
        if not src.exists():
            print(f"文件不存在：{src}")
            return 1
        layout = json.loads(src.read_text(encoding="utf-8"))
        print(f"读入 {src}")
    else:
        layout = SAMPLE
        print("使用内置样例（一次真实解析的结果）")

    scene = normalize_layout(layout)
    plan = render_scene_svg(
        scene, min_side_px=args.min_side, with_hotspots=not args.no_hotspots
    )
    payload = hotspot_payload(scene, plan.projection)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    svg_path = out / "plan.svg"
    svg_path.write_text(plan.svg, encoding="utf-8")
    (out / "hotspots.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n画布      {plan.width_px}×{plan.height_px} px")
    print(f"比例尺    {scene.px_per_m:.1f} px/m（来源：{scene.scale_source}）")
    print(f"户型      {scene.width_m:.2f} × {scene.depth_m:.2f} m")
    print(f"房间/墙   {scene.quality.room_count} 间 / {scene.quality.wall_count} 段"
          f"（闭合 {scene.quality.walls_closed}）")
    print(f"热区      {payload['count']} 个 {payload['by_precision']}")
    print(f"\n→ {svg_path}")

    # ⚠️ 这些是"画不准的地方"，必须说出来。写进图里（<desc>）也打在这里。
    if plan.warnings:
        print("\n需要注意：")
        for w in plan.warnings:
            print(f"  · {w}")
    if scene.assumptions:
        print("\n本次渲染的假设：")
        for a in scene.assumptions:
            print(f"  · {a}")

    print("\n用浏览器打开 plan.svg 即可查看（矢量图，可无限放大）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
