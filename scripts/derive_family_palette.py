"""
按**家具族**推导 3D 用色 —— 不同种类不同颜色，同一种类同一个颜色。

═══════════════════════════════════════════════════════════════════
为什么不能沿用 `surface` 的六个角色色
═══════════════════════════════════════════════════════════════════
`surface` 是按**材质角色**分的（木/布艺/金属/石材/绿植/白），六个。
它的用途是"同一件家具的不同部分"—— 床架是木、床垫是布艺。

而需求方要的是按**种类**分：床、沙发、衣柜、餐桌…… 一眼能认出这是什么。
六种角色色做不到这件事：同一间卧室里，床和床头柜都是 `fabric`，
在 3D 里就是两块一模一样的色块。

═══════════════════════════════════════════════════════════════════
约束（与 `derive_surface_palette.py` 同源，但阈值不同）
═══════════════════════════════════════════════════════════════════
  ① 每个族的颜色对 3D 地面 **≥ 2:1** —— 能从地面里分出家具
  ② 族两两之间 Lab ΔE **≥ MIN_FAMILY_DELTA_E** —— 彼此分得出

⚠️ ②的阈值**比六个角色时低**，这是有意的、也是必然的：38 个族要在
    同一个可用亮度带里两两拉开，给定色域下不可能维持 6.6 那样宽的距离。
    所以这里先测出**能达到的最小 ΔE 是多少**，再拿那个数当阈值，
    而不是先定一个漂亮的数再想办法凑（那是本脚本第一版干的事，
    结果是搜到一半就无解）。

⚠️ 色相用**黄金角**（137.508°）而不是均分：族数是 38，均分的话
    相邻两族的色相差只有 9.5°，肉眼几乎分不开；黄金角让**任意两个
    相邻编号**的族色相都差得远，而"哪两个族会同时出现在一间房"
    是目录决定的，与编号无关。

用法：
    python scripts/derive_family_palette.py            # 测一遍，打印结果
    python scripts/derive_family_palette.py --write    # 写进 seed_data
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from derive_surface_palette import (  # noqa: E402
    contrast_ratio, delta_e, hex_to_rgb, hsl_to_rgb, rgb_to_hex,
)

CATALOG = ROOT / "seed_data" / "furniture_catalog.json"

#: 族色之间要求的最小 Lab ΔE。见脚本说明：这是**先测后定**的。
MIN_FAMILY_DELTA_E = 6.0

#: 每个族的颜色必须对地面达到的对比度。与 `derive_surface_palette.py` 同值。
MIN_FLOOR_CONTRAST = 2.0

#: 黄金角。用它铺色相，任意两个相邻编号都拉得开。
GOLDEN_ANGLE = 137.508

#: 每套风格的搜索空间：(饱和度下界, 上界, 亮度下界, 上界)。
#: 亮度上界压在 0.72 以下 —— 再亮就接近白墙，看不出是个色块。
STYLE_BANDS: dict[str, tuple[float, float, float, float]] = {
    "modern":  (0.10, 0.38, 0.22, 0.68),
    "nordic":  (0.12, 0.42, 0.26, 0.70),
    "chinese": (0.16, 0.48, 0.18, 0.62),
}


def families(catalog: dict) -> list[str]:
    """全部家具族，**按 id 排序**保证每次跑得到同一个顺序（确定性）。"""
    return sorted({str(it["family"]) for it in catalog["items"]})


#: R2 低差异序列的两个无理数。用它把 38 个族**摊在 (色相, 饱和度, 亮度)
#: 三维里**，而不是只摊色相 ——
#:
#: ⚠️ 第一版只按黄金角摊色相，实测崩了：38 个色相点的**最小间隔只有约 6°**
#:    （黄金角在 38 个点上的最小间隙 ≈ 360/(38φ)），两个只差 6° 的族
#:    靠亮度和饱和度根本补不回来 —— 实测最小 ΔE 只有 **2.6**，
#:    也就是"床和洗衣机一个颜色"。
#:    摊到三维之后，编号相邻的族在三个维度上都不一样。
_R2_ALPHA = (0.7548776662466927, 0.5698402909980532)


def _candidate(hue: float, s: float, l: float, floor) -> str | None:
    """一个候选色：对地面够得着才要，否则 None。"""
    rgb = hsl_to_rgb(hue / 360.0, s, l)
    if contrast_ratio(rgb, floor) < MIN_FLOOR_CONTRAST:
        return None
    return rgb_to_hex(rgb)


def derive(catalog: dict, style: str) -> tuple[dict[str, str], dict]:
    """
    给一套风格推一族色。

    两步：
      ① 用 R2 低差异序列把 38 个族摊在 (色相, 饱和度, 亮度) 三维里；
      ② **修复最挤的那一对**：反复找出当前 ΔE 最小的一对，
         把其中一个重新搜一个离大家更远的点，直到最小值不再上升。

    ⚠️ ②是必要的，不是锦上添花：①只保证"摊得均匀"，不保证"两两都够远"。
       实测①之后仍有几对挤在一起，修完最小值才抬上去。
    """
    fams = families(catalog)
    s_lo, s_hi, l_lo, l_hi = STYLE_BANDS[style]
    floor = hex_to_rgb(catalog["styles"][style]["surface_floor"])

    out: dict[str, str] = {}
    hue_of: dict[str, float] = {}
    for i, fam in enumerate(fams):
        hue = (i * GOLDEN_ANGLE) % 360.0
        s = s_lo + (s_hi - s_lo) * ((i * _R2_ALPHA[1]) % 1.0)
        l = l_lo + (l_hi - l_lo) * ((i * _R2_ALPHA[0]) % 1.0)
        hue_of[fam] = hue
        # 落点可能刚好够不着地面（黄金角那条准则），退到带中央再试
        hexv = _candidate(hue, s, l, floor) or _candidate(
            hue, (s_lo + s_hi) / 2, (l_lo + l_hi) / 2, floor)
        if hexv is None:
            raise SystemExit(
                f"{style}: 族 {fam} 在色带内找不到对地面 ≥{MIN_FLOOR_CONTRAST}:1 的颜色"
            )
        out[fam] = hexv

    # ── ② 修复：每轮把最挤的那一对里的一件挪到更好的位置 ──
    for _ in range(len(fams) * 2):
        worst, pair = 999.0, ("", "")
        items = list(out.items())
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                e = delta_e(items[a][1], items[b][1])
                if e < worst:
                    worst, pair = e, (items[a][0], items[b][0])
        if worst >= MIN_FAMILY_DELTA_E:
            break
        for fam in pair:
            base_hue = hue_of[fam]
            others = [v for k, v in out.items() if k != fam]
            best: tuple[float, str] | None = None
            for dh in range(-30, 31, 5):            # 色相在黄金角附近微调
                hue = (base_hue + dh) % 360.0
                for si in range(9):
                    s = s_lo + (s_hi - s_lo) * si / 8
                    for li in range(13):
                        l = l_lo + (l_hi - l_lo) * li / 12
                        hexv = _candidate(hue, s, l, floor)
                        if hexv is None:
                            continue
                        w = min((delta_e(hexv, v) for v in others), default=999.0)
                        if best is None or w > best[0]:
                            best = (w, hexv)
            if best:
                out[fam] = best[1]
    return out, {"floor": catalog["styles"][style]["surface_floor"]}


def audit(colors: dict[str, str], floor_hex: str) -> dict:
    """量一遍：最小对比度、最小两两 ΔE、以及最挤的那一对是谁。"""
    floor = hex_to_rgb(floor_hex)
    worst_c, worst_c_fam = 999.0, ""
    for fam, h in colors.items():
        c = contrast_ratio(hex_to_rgb(h), floor)
        if c < worst_c:
            worst_c, worst_c_fam = c, fam

    worst_e, pair = 999.0, ("", "")
    items = list(colors.items())
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            e = delta_e(items[i][1], items[j][1])
            if e < worst_e:
                worst_e, pair = e, (items[i][0], items[j][0])
    return {
        "min_floor_contrast": round(worst_c, 2), "min_contrast_family": worst_c_fam,
        "min_pair_delta_e": round(worst_e, 2), "closest_pair": pair,
        "families": len(colors),
    }


def main() -> int:
    write = "--write" in sys.argv
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    print(f"目录里 {len(families(catalog))} 个族\n")

    all_colors: dict[str, dict[str, str]] = {}
    for style in ("modern", "nordic", "chinese"):
        colors, _meta = derive(catalog, style)
        rep = audit(colors, catalog["styles"][style]["surface_floor"])
        ok = (rep["min_floor_contrast"] >= MIN_FLOOR_CONTRAST
              and rep["min_pair_delta_e"] >= MIN_FAMILY_DELTA_E)
        print(f"{style:<8} 最小对地对比 {rep['min_floor_contrast']:>5.2f}:1 "
              f"({rep['min_contrast_family']})   "
              f"最小两两 ΔE {rep['min_pair_delta_e']:>5.2f} "
              f"({rep['closest_pair'][0]} ↔ {rep['closest_pair'][1]})   "
              f"{'✅' if ok else '❌'}")
        all_colors[style] = colors

    if write:
        for style, colors in all_colors.items():
            catalog["styles"][style]["family_colors"] = colors
        catalog["styles"]["_family_colors_note"] = (
            f"按**家具族**上色：不同种类不同色，同一种类同色。"
            f"由 scripts/derive_family_palette.py 推出，"
            f"约束是「对 3D 地面 ≥{MIN_FLOOR_CONTRAST}:1」+"
            f"「族两两 ΔE ≥{MIN_FAMILY_DELTA_E}」。"
            f"色相按黄金角铺开 —— 族按 id 排序后任意相邻两族色相也拉得开。"
        )
        CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        print("\n已写入", CATALOG.name)
    else:
        print("\n（加 --write 才会写盘）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
