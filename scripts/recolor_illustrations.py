#!/usr/bin/env python
"""
把 unDraw 插画重上色到项目的 Botanical Warmth 色板。

═══════════════════════════════════════════════════════════════════
为什么必须做这一步
═══════════════════════════════════════════════════════════════════
unDraw 的插画自带一套固定配色，主色是 `#6C63FF` —— 一个很典型的
**霓虹紫蓝**。实测这 18 张插画里它出现 172 次。

而 `ui参考/stitch_ai/botanical_warmth_natural_living/DESIGN.md` 明确写了
这套设计系统「**禁止霓虹饱和度、禁止科技未来感的渐变、禁止冷调临床中性色**」。

两者放在一起，效果是：整个界面是暖米色 + 叶绿的木质调，中间嵌一块
紫蓝色的插画 —— 那正是"看起来很 AI"的典型来源。空状态和引导页恰恰是
用户第一眼看到的地方。

═══════════════════════════════════════════════════════════════════
做法
═══════════════════════════════════════════════════════════════════
纯色值替换，不改 SVG 结构。unDraw 的插画是分层矢量图，颜色语义很稳定：

    主色（图形主体）      #6C63FF  →  叶绿 #4A7C59
    深色（人物/设备暗部）  #3F3D56  →  栗棕 #6B4F3A
    更深（轮廓）          #2F2E41  →  深咖 #2C2418
    浅紫（阴影/背景块）    #D6D6E3  →  浅鼠尾草 #E8F0E5
    近白（高光）          #E6E6F0  →  暖亚麻 #EDE8E0
    中性灰               #E6E6E6  →  暖亚麻 #EDE8E0

⚠️ **只改 `frontend/src/assets/illustrations/` 下的副本，
不动 `ui参考/` 里的原件** —— 采集物的原件要保值，这是项目一贯的纪律
（同 `references_manifest.yaml` 的"上游文件一律不修改"）。

用法：
    python scripts/recolor_illustrations.py
    python scripts/recolor_illustrations.py --dry-run
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = pathlib.Path(__file__).resolve().parent.parent
TARGET = REPO / "frontend" / "src" / "assets" / "illustrations"

#: 原色 → 目标色。键统一小写（SVG 里大小写混用）。
PALETTE: dict[str, str] = {
    # 主色：unDraw 的招牌霓虹紫 → 项目主色叶绿
    "#6c63ff": "#4A7C59",
    # 深色块（人物、设备外壳）→ 栗棕
    "#3f3d56": "#6B4F3A",
    # 轮廓 / 最深部 → 深咖（设计稿的正文色，不是纯黑）
    "#2f2e41": "#2C2418",
    # 新一代 unDraw 用的"黑" —— 实为近纯黑的冷调 #090814（rgb 9,8,20）。
    # DESIGN.md 明令「禁止 pitch blacks」，正文色是 #2C2418 这个暖深咖。
    "#090814": "#2C2418",
    # 浅紫阴影 → 浅鼠尾草绿
    "#d6d6e3": "#E8F0E5",
    "#e6e6f0": "#EDE8E0",
    # 中性灰 → 暖亚麻边框色
    "#e6e6e6": "#EDE8E0",
    "#f2f2f2": "#F5F2EC",
    "#f0f0f0": "#F5F2EC",
    "#b6b3c5": "#D3C4BA",
    # 肤色/衣物的粉调 → 暖黄铜与浅木，避免冷粉突兀
    "#ed9da0": "#C9A961",
    "#9f616a": "#A8845C",
    "#a0616a": "#A8845C",
    "#ffb9b9": "#E5C9A8",
    # 近白与变体
    "#fafafa": "#FAF8F3",
    "#f8f8f8": "#FAF8F3",
    "#2f2e43": "#2C2418",
}

_HEX = re.compile(r"#([0-9a-fA-F]{6})")


def recolor(text: str) -> tuple[str, dict[str, int]]:
    """替换所有匹配的十六进制色值，返回 (新文本, {原色: 命中次数})。"""
    hits: dict[str, int] = {}

    def sub(m: re.Match[str]) -> str:
        key = "#" + m.group(1).lower()
        if key in PALETTE:
            hits[key] = hits.get(key, 0) + 1
            return PALETTE[key]
        return m.group(0)

    return _HEX.sub(sub, text), hits


def main() -> int:
    ap = argparse.ArgumentParser(description="把 unDraw 插画重上色到项目色板")
    ap.add_argument("--dry-run", action="store_true", help="只报数不改文件")
    ap.add_argument("--target", default=str(TARGET), help="目标目录")
    args = ap.parse_args()

    target = pathlib.Path(args.target)
    if not target.is_dir():
        print(f"✗ 目录不存在：{target}")
        print("  先跑 python scripts/collect_ui_assets.py --only undraw 并复制素材")
        return 1

    svgs = sorted(target.glob("*.svg"))
    if not svgs:
        print(f"✗ {target} 下没有 SVG")
        return 1

    total: dict[str, int] = {}
    changed = 0
    for p in svgs:
        raw = p.read_text(encoding="utf-8")
        new, hits = recolor(raw)
        if not hits:
            continue
        changed += 1
        for k, v in hits.items():
            total[k] = total.get(k, 0) + v
        if not args.dry_run:
            p.write_text(new, encoding="utf-8")

    print(f"{'[DRY-RUN] ' if args.dry_run else ''}处理 {len(svgs)} 张插画，"
          f"其中 {changed} 张命中需替换的颜色")
    for src, n in sorted(total.items(), key=lambda kv: -kv[1]):
        print(f"   {src} → {PALETTE[src]}   ×{n}")
    if args.dry_run:
        print("\n（未写文件。去掉 --dry-run 才真的改）")
    else:
        print(f"\n✓ 已写回 {target.relative_to(REPO)}")
        print("  ⚠️ 原件在 ui参考/02-插画-unDraw/ 未改动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
