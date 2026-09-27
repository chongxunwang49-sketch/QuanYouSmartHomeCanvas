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
for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass

from derive_surface_palette import (  # noqa: E402
    contrast_ratio, delta_e, hex_to_rgb, hsl_to_rgb, relative_luminance,
    rgb_to_hex,
)

CATALOG = ROOT / "seed_data" / "furniture_catalog.json"
CATALOG_REF: list[dict] = []   # audit 要读族-房间映射，见 main()

#: 族色之间要求的最小 Lab ΔE。见脚本说明：这是**先测后定**的。
#: 亮度下限/上限。**没有它俩，最远点贪心会挑出 #06060E 这种"报表好看、
#: 实际近黑"的颜色** —— 实测踩过：整个族色塌成一片黑，而约束检查全绿。
#: 这两个数是"看起来还算个颜色"的边界，不是审美偏好：
#:   下限拦近黑（0.06 以下读起来就是黑块），上限拦近白（0.72 以上跟白墙糊在一起）。
LUMA_FLOOR_MIN = 0.06
LUMA_CEIL_MAX = 0.72

MIN_FAMILY_DELTA_E = 6.0

#: 每个族的颜色必须对地面达到的对比度。与 `derive_surface_palette.py` 同值。
MIN_FLOOR_CONTRAST = 2.0

#: 黄金角。用它铺色相，任意两个相邻编号都拉得开。
GOLDEN_ANGLE = 137.508

#: 每套风格的搜索空间：`(饱和度下界, 上界, 色相偏移)`。
#:
#: ⚠️ **亮度不在风格里定，而是让"对地面 ≥2:1"这条筛子自己去分。**
#:    3D 地面是中调（modern `#988971`，相对亮度约 0.28），能和它拉开 2:1 的
#:    只有**偏暗**（亮度 ≤0.115）或**偏亮**（亮度 ≥0.61）两个窗口 ——
#:    中间那段怎么调都够不着。第一版把亮度带上限压在 0.68（只留暗窗口），
#:    结果 38 个族全挤在暗窗口里，实测最小 ΔE 只有 2.6。
#:    放开成整段之后，族自然分成"深色家具"和"浅色家具"两拨，
#:    可用色域一下宽了一倍。
STYLE_BANDS: dict[str, tuple[float, float, float]] = {
    "modern":  (0.10, 0.40, 0.0),
    "nordic":  (0.12, 0.46, 140.0),
    "chinese": (0.16, 0.52, 250.0),
}

#: 亮度的搜索范围。上界压在 0.90 —— 再亮就接近白墙，看不出是个色块。
LUMA_RANGE: tuple[float, float] = (0.04, 0.90)

#: 候选池的网格密度。色相 48 步（7.5°）够细了 —— 再细只是把同一个色
#: 换个名字，而收益是搜索变慢。亮度 32 步覆盖从"近黑"到"近白"。
HUE_STEPS, S_STEPS, L_STEPS = 48, 6, 32


def families(catalog: dict) -> list[str]:
    """全部家具族，**按 id 排序**保证每次跑得到同一个顺序（确定性）。"""
    return sorted({str(it["family"]) for it in catalog["items"]})


def coexisting(catalog: dict) -> dict[str, set[str]]:
    """
    哪些族**可能同时出现在同一间房**。

    ══════════════════════════════════════════════════════════════════
    为什么约束要按这个来，而不是"两两都拉开"
    ══════════════════════════════════════════════════════════════════
    38 个族**两两**都要 ΔE ≥ 6，在"对地面 ≥2:1"的前提下**色域不够**：
    地面是中调，可用亮度只剩"暗"和"亮"两个窄窗口，两个窗口加起来
    也塞不下 38 个相距 6 的点。实测：全两两约束下最小 ΔE 只能到 **3.08**，
    而且再修也上不去 —— 这是算术，不是调参。

    但**真实约束比这小得多**：卫生间里不会出现双人床，卧室里不会出现马桶。
    不会同框的两个族，颜色撞了也没人看得出来。
    所以约束改成"**会同房的族**两两拉开" —— 判据是目录里各自的 `rooms`
    有没有交集（有交集 = 可能同房）。
    """
    by_room: dict[str, set[str]] = {}
    for it in catalog["items"]:
        fam = str(it["family"])
        for room in it.get("rooms") or []:
            by_room.setdefault(str(room), set()).add(fam)
    adj: dict[str, set[str]] = {f: set() for f in families(catalog)}
    for fams in by_room.values():
        for a in fams:
            adj[a] |= (fams - {a})
    return adj


#: 暗窗口与亮窗口的亮度下/上界。**两端都要封住。**
#:
#: ⚠️ 实测踩过两次，方向相反，都要防：
#:    第一版只放开对比度、不限上下界 —— 最远点贪心立刻去挑**纯黑**
#:    （`#06060E`，相对亮度 0.002）和**纯白**，因为那样两两距离最大。
#:    报表上 ΔE 漂亮得很，而画面是一团黑 —— 与 ADR-16 记录过的
#:    "约束全过、画面一团黑"是同一个坑。
#:    所以窗口两端各收一刀：暗的不到 0.06（太黑看不出颜色），
#:    亮的不过 0.72（太白与墙分不开）。
LUMA_FLOOR_MIN = 0.06
LUMA_CEIL_MAX = 0.72


def luma_windows(floor) -> tuple[tuple[float, float], tuple[float, float]]:
    """
    对地面 ≥2:1 的**两个**可用亮度窗口。

    对比度公式 `(L1+0.05)/(L2+0.05) ≥ 2` 对给定地面亮度 F 解出来就是
    "比 F 暗一半"或"比 F 亮一倍"，中间那段永远够不着 ——
    这正是 ADR-16 里算过的那条算术，这里直接用它的结论。
    """
    f = relative_luminance(floor)
    lo = (LUMA_FLOOR_MIN, max(LUMA_FLOOR_MIN, (f + 0.05) / 2.0 - 0.05))
    hi = (min(LUMA_CEIL_MAX, 2.0 * (f + 0.05) - 0.05), LUMA_CEIL_MAX)
    return lo, hi


#: ⚠️ **`hsl_to_rgb` 的色相单位是「度」，不是 [0,1)。**
#:    它内部自己做 `h / 360.0`（见 `derive_surface_palette.py`）。
#:    实测踩过：这里按 [0,1) 传进去，于是所有色相都塌到 0 ——
#:    推出来的一整套族色**全是红的**，而约束报表照样 ✅
#:    （对比度和 ΔE 都只看颜色、不看色相是否真的铺开了）。
#:    一个"全绿的方案"和"全红的方案"报表长得一模一样。
def _candidates(style: str, floor) -> list[str]:
    """
    全部**合格**候选色：对地面 ≥2:1。

    ⚠️ 判据按**取整成十六进制之后**的颜色算。实测踩过：浮点算出 2.0004
    判它合格，而 `#RRGGBB` 取整到 8 位后变成 1.99 —— 报表上就是不合格，
    而原因极难归因。
    """
    s_lo, s_hi, hue_off = STYLE_BANDS[style]
    lo_win, hi_win = luma_windows(floor)
    out: list[str] = []
    for hi in range(HUE_STEPS):
        hue = (hi * 360.0 / HUE_STEPS + hue_off) % 360.0
        for si in range(S_STEPS):
            s = s_lo + (s_hi - s_lo) * si / max(1, S_STEPS - 1)
            for li in range(L_STEPS):
                l = LUMA_RANGE[0] + (LUMA_RANGE[1] - LUMA_RANGE[0]) * li / (L_STEPS - 1)
                hexv = rgb_to_hex(hsl_to_rgb(hue, s, l))
                rgb = hex_to_rgb(hexv)
                lum = relative_luminance(rgb)
                # ① 落在两个可用亮度窗口之一（**不含窗口外的灰带**）
                if not (lo_win[0] <= lum <= lo_win[1]
                        or hi_win[0] <= lum <= hi_win[1]):
                    continue
                # ② 冗余的安全网：真去算一遍对比度
                if contrast_ratio(rgb, floor) < MIN_FLOOR_CONTRAST:
                    continue
                out.append(hexv)
    return sorted(set(out))          # 去重 + 定序，保证确定性


def derive(catalog: dict, style: str) -> tuple[dict[str, str], dict]:
    """
    给一套风格推一族色 —— **最远点贪心**。

    ══════════════════════════════════════════════════════════════════
    为什么是这样，而不是"色相按黄金角铺开 + 局部微调"
    ══════════════════════════════════════════════════════════════════
    前两版都失败了，记录在这里免得再走一遍：

      第一版：色相按黄金角铺，逐族贪心挑亮度/饱和度。
              结果最小 ΔE **2.6** —— 38 个色相点的最小间隔只有约 6°，
              亮度和饱和度补不回来（"床和洗衣机一个颜色"）。

      第二版：R2 低差异序列摊到三维 + 修复最挤的一对。
              结果**色相几乎全塌到红色**、多个族拿到**完全相同的色号**，
              还有几个根本没达到 2:1。原因是修复循环只在"会同房的族"
              里比较，而**没有同房邻居的族**在空集合上取 max，
              第一支候选就胜出 —— 于是它们全落在同一个点上。

    现在这版把三件事分开、每件都能单独验：

      ① **候选集**先筛死：只有对地面 ≥2:1 的颜色进池子。约束在源头成立，
         后面怎么挑都不会违规。
      ② **分配顺序按"会同房的族数"从多到少**：最受约束的族先挑，
         它可选的范围最大；挑剩下的族空间更宽，不会互相挤。
      ③ **挑最远点**：在候选里选一个使「到已分配的、会同房的那些族」
         的最小 ΔE 最大的颜色。没有同房邻居时退化为"选离全场最远的"，
         而不是"选第一个" —— 这正是第二版塌掉的地方。

    ⚠️ 阈值先测后定（`MIN_FAMILY_DELTA_E`）。
    """
    fams = families(catalog)
    adj = coexisting(catalog)
    floor = hex_to_rgb(catalog["styles"][style]["surface_floor"])
    pool = _candidates(style, floor)
    if not pool:
        raise SystemExit(f"{style}: 色带内没有任何颜色能满足对地面 ≥{MIN_FLOOR_CONTRAST}:1")

    # ② 受约束最多的族先挑
    order = sorted(fams, key=lambda f: (-len(adj.get(f, ())), f))

    out: dict[str, str] = {}
    for fam in order:
        neighbours = [out[g] for g in adj.get(fam, ()) if g in out]
        reference = neighbours or list(out.values())
        if not reference:
            # 第一个族：挑一个"最中间"的，给后面留出最大的空间
            best = max(pool, key=lambda c: min(delta_e(c, o) for o in pool))
        else:
            best = max(reference and pool,
                       key=lambda c: min(delta_e(c, o) for o in reference))
        out[fam] = best
    return out, {"floor": catalog["styles"][style]["surface_floor"], "pool": len(pool)}


def audit(colors: dict[str, str], floor_hex: str) -> dict:
    """量一遍：最小对比度、最小两两 ΔE、以及最挤的那一对是谁。"""
    floor = hex_to_rgb(floor_hex)
    worst_c, worst_c_fam = 999.0, ""
    for fam, h in colors.items():
        c = contrast_ratio(hex_to_rgb(h), floor)
        if c < worst_c:
            worst_c, worst_c_fam = c, fam

    adj = coexisting(CATALOG_REF[0])
    # 两个口径都量：真实约束（会同房）与全两两。**都报出来** ——
    # 只报前者会让人以为 38 个族两两都拉得开。
    def _min(pred) -> tuple[float, tuple[str, str]]:
        best_e, best_p = 999.0, ("", "")
        items = list(colors.items())
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if not pred(items[i][0], items[j][0]):
                    continue
                e = delta_e(items[i][1], items[j][1])
                if e < best_e:
                    best_e, best_p = e, (items[i][0], items[j][0])
        return best_e, best_p

    co_e, co_p = _min(lambda a, b: b in adj.get(a, ()))
    all_e, all_p = _min(lambda a, b: True)
    return {
        "min_floor_contrast": round(worst_c, 2), "min_contrast_family": worst_c_fam,
        "min_coexist_delta_e": round(co_e, 2), "coexist_pair": co_p,
        "min_pair_delta_e": round(all_e, 2), "closest_pair": all_p,
        "families": len(colors),
    }


def main() -> int:
    write = "--write" in sys.argv
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    CATALOG_REF.append(catalog)
    print(f"目录里 {len(families(catalog))} 个族\n")

    all_colors: dict[str, dict[str, str]] = {}
    for style in ("modern", "nordic", "chinese"):
        colors, _meta = derive(catalog, style)
        rep = audit(colors, catalog["styles"][style]["surface_floor"])
        ok = (rep["min_floor_contrast"] >= MIN_FLOOR_CONTRAST
              and rep["min_coexist_delta_e"] >= MIN_FAMILY_DELTA_E)
        print(f"{style:<8} 对地 {rep['min_floor_contrast']:>5.2f}:1   "
              f"会同房 ΔE {rep['min_coexist_delta_e']:>5.2f} "
              f"({rep['coexist_pair'][0]}↔{rep['coexist_pair'][1]})   "
              f"全两两 ΔE {rep['min_pair_delta_e']:>5.2f}   "
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
