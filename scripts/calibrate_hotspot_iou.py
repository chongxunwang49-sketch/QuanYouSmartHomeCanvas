"""
AI 图热区阈值标定（ADR-10 的标定规程 / AC-28）。

═══════════════════════════════════════════════════════════════════
为什么需要这个脚本
═══════════════════════════════════════════════════════════════════
需求文档 2.2.6 定的处置规则是「IoU < 0.70 → 不给热区」，而同一份文档的
0.2 节「坑 19」自己承认：

> 0.70 的阈值是预设的，**没有实测依据**。而 conditioning 边缘图与生成图
> 的 Canny 边缘图之间，即使几何完全对齐，IoU 也可能只有 0.4–0.6
> —— **0.70 的阈值会让所有 AI 图都不显示热区**。

一个"所有样本都不达标"的阈值，效果等于把功能关掉、却让人以为它在工作。
这个脚本把那个数字**量出来**。

═══════════════════════════════════════════════════════════════════
怎么用
═══════════════════════════════════════════════════════════════════
    python scripts/calibrate_hotspot_iou.py E:/quanyou/outputs

它会找目录下所有**成对**的文件：

    xxx.png          ← 生成图
    xxx_cond.png     ← 当时喂给 ControlNet 的 conditioning 图

用现场那张 conditioning 图而不是重新从 layout 渲染一张 —— 标定要复现的是
"当时到底对得怎么样"，重渲一张会引入额外的口径差。

═══════════════════════════════════════════════════════════════════
⚠️ 输出怎么读
═══════════════════════════════════════════════════════════════════
样本数少于 20 张时，**建议阈值那一行不要直接抄进配置**。
消融实验里那批图大半是"故意做坏的对照组"，它们拉低的分布不代表
正常出图的水平 —— 把对照组的分数当成产品表现，会把阈值定得离谱地低。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ⚠️ Windows 控制台默认是 cp936（GBK）。它认得汉字，但**不认 ⚠️ 这类符号**
# （U+26A0 + 变体选择符），于是脚本会死在最后那句提示上 —— 前面所有计算都白跑。
# 显式把标准输出改成 UTF-8，与本机代码页无关。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from backend.app.services.render import edge_iou  # noqa: E402

#: 需求文档里预设的那个值。本脚本要回答的就是它合不合适。
PRESET_THRESHOLD = 0.70


def find_pairs(root: Path) -> list[tuple[Path, Path]]:
    """找 `xxx_cond.png` 与配对的 `xxx.png`。"""
    pairs: list[tuple[Path, Path]] = []
    for cond in sorted(root.rglob("*_cond.png")):
        gen = cond.with_name(cond.name.replace("_cond.png", ".png"))
        if gen.exists():
            pairs.append((cond, gen))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description="标定 AI 图热区的几何一致性阈值")
    ap.add_argument("root", nargs="?", default="E:/quanyou/outputs",
                    help="含成对图片的目录（递归查找）")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"目录不存在：{root}")
        return 1

    pairs = find_pairs(root)[: args.limit]
    if not pairs:
        print(f"没有找到成对的 <name>.png + <name>_cond.png：{root}")
        print("（标定需要成对文件 —— 只有生成图的话无从比对）")
        return 1

    rows: list[tuple[str, float]] = []
    for cond, gen in pairs:
        try:
            with Image.open(cond) as c, Image.open(gen) as g:
                rows.append((gen.stem, edge_iou(c, g)))
        except Exception as e:  # noqa: BLE001 —— 单张坏图不该毁掉整批
            print(f"  跳过 {gen.name}：{type(e).__name__}: {e}")

    if not rows:
        print("所有样本都读取失败")
        return 1

    rows.sort(key=lambda r: -r[1])
    print(f"\n{'样本':<44}{'IoU':>7}")
    print("-" * 52)
    for name, v in rows:
        print(f"{name:<44}{v:>7.3f}")

    ious = [v for _, v in rows]
    passed = sum(1 for v in ious if v >= PRESET_THRESHOLD)
    print("-" * 52)
    print(f"n={len(ious)}  min={min(ious):.3f}  "
          f"median={sorted(ious)[len(ious) // 2]:.3f}  max={max(ious):.3f}")
    print(f"\n预设阈值 {PRESET_THRESHOLD:.2f} → 达标 {passed}/{len(ious)}")

    if passed == 0:
        print(
            "\n⚠️ 预设阈值一个都没过 —— 这正是需求文档「坑 19」预测的结果。\n"
            "   按 2.2.6 的规则，这意味着**所有 AI 图都不放热区**。\n"
            "   若确认如此，应把 IMAGE_HOTSPOT_ENABLED 保持 False，\n"
            "   并在文档里把「AI 图热区」明确标为「已实测不可用」，\n"
            "   而不是留一个永远不会触发的阈值。"
        )
    if len(ious) < 20:
        print(
            f"\n⚠️ 样本数只有 {len(ious)} 张（标定要求 ≥ 20）。\n"
            "   而且消融目录里的图**大半是故意做坏的对照组**（thinEdge 那几条\n"
            "   本身就是实验的失败分支），它们拉低的分布不代表正常出图水平。\n"
            "   这份结果只能作为「预设阈值确实偏高」的**证据**，\n"
            "   不能当作阈值标定值。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
