"""
推导 3D 表面色 —— **在写死的约束下搜，而不是靠眼睛调**。

    python scripts/derive_surface_palette.py            # 报当前三套 + 建议
    python scripts/derive_surface_palette.py --check    # 只校验，不搜索

══════════════════════════════════════════════════════════════════
为什么要有这个脚本
══════════════════════════════════════════════════════════════════
`seed_data/furniture_catalog.json` 里每个风格有两套颜色：

    palette         2D 界面用（卡片、色块）—— 要跟**白底**拉得开
    surface         3D 表面用 —— 要跟**地面**拉得开

第二套是 2026-09-24 因为"家具看不见"才加的（`palette` 直接拿来当表面色时，
modern 的 fabric 对地面只有 **1.01:1**，等于没画）。加的时候是**手工调的，
调了 7 轮**，每一轮靠截图看 —— 这个过程本身不可复现，也没法回答
"再亮一点会不会更好"。

而"再亮一点"是真实存在的问题：手工那版为了让每个角色都跟地面拉开 2:1，
把暗的往黑里推、亮的往白里推，结果是 6 个角色里 **3 个接近纯黑
（相对亮度 0.0015~0.014）、1 个接近纯白（0.973）**，
3D 俯瞰时暗色家具看着像地上的洞（见 `logs/d2-fly-topdown.png`）。

所以把这件事从"调色"变成"带约束的搜索"：**约束写下来、可复算、
换一档亮度只要改一个数**。

══════════════════════════════════════════════════════════════════
约束
══════════════════════════════════════════════════════════════════
硬约束（不满足就是不合格）：

  C1  每个角色对地面 **对比度 ≥ 2:1**     —— 家具必须从地上"浮"出来
  C2  角色之间 **Lab ΔE ≥ 6.6**           —— 相邻两件家具能看出是两种东西
  C3  每个角色的相对亮度落在 **[0.05, 0.72]** —— 这条是这次新加的，见下
  C4  每个角色留在自己的**色相窗口**里     —— 木色还得是木色

目标：最大化最小的那一对 ΔE（把最挤的两个推开）。

关于 C3 的取值：
  · 下界 0.05 ≈ sRGB 0x45（中深）—— 比它暗的东西在室内光下就是一团黑。
    实测原来的 wood=#271D11 是 0.0138、metal=#050506 是 0.0015、
    green=#0C170B 是 0.0063，**三个全在下界以下**。
  · 上界 0.72 ≈ sRGB 0xD8 —— 比它亮的东西在浅色地面旁边会糊成一片白。
    原来只有 white=#FCFCFC(0.973) 越界，fabric/stone 在界内。
  · 这两条**不是查来的标准**，是从"3D 里看不看得清"反推的经验区间，写在这里
    是为了让它们可以被质疑和改动。

关于 ΔE 的 6.6：大色块的 JND 约 2.3，6.6 是它的近三倍 —— 取"一眼能分辨"
  而不是"勉强能分辨"。⚠️ 这个阈值一开始我拍的是 12，按它调发现六个角色
  根本排不下，回头核实才想起 JND 是 2.3 那个量级。

⚠️ **色相窗口是"角色身份"的载体**：木色是棕色、fabric 是暖中性、
metal 是冷灰、stone 是浅冷灰、green 是叶绿、white 是暖白。
没有它，搜索会为了一点点 ΔE 把"木色"搜成紫色 —— 数值更好看，语义全丢。
"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "seed_data" / "furniture_catalog.json"

# ⚠️ **必须在任何输出之前**把 stdout/stderr 切到 UTF-8。
#    Windows 控制台默认 GBK，打不出 ⚠ / ✓ / 制表符时会抛
#    `UnicodeEncodeError: 'gbk' codec can't encode character '⚠'` ——
#    而且**这个异常发生在打印错误信息的路上**，于是真正的报错被它吃掉，
#    看到的是一个和实际问题无关的编码异常。本项目在 init_db.py 里踩过一次，
#    这里忘了带，又踩了一次（2026-09-26）。
for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass

#: 角色 → (色相范围(度), 饱和度范围, HSL 亮度范围)
#: 色相用 `None` 表示"不限定色相"（灰/白类角色靠饱和度压住）。
ROLE_WINDOWS: dict[str, tuple[tuple[float, float] | None,
                               tuple[float, float], tuple[float, float]]] = {
    # 木色：棕/暖褐
    "wood": ((18.0, 40.0), (0.28, 0.62), (0.24, 0.52)),
    # 布艺：暖中性（沙发/床/地毯）
    # ⚠️ 饱和度上限从 0.22 放到 0.26：现行值 #D6CCBC 是 0.241，
    #    而它一眼就是米色 —— 窗口卡在 0.22 会把"本来就合格的颜色"判越界，
    #    于是这条检查的第一条告警就是假警报。**判据比现实更紧，
    #    和更松一样是错的**（只是方向相反）。
    "fabric": ((22.0, 52.0), (0.05, 0.26), (0.58, 0.82)),
    # 金属：冷灰（几乎无彩）
    "metal": (None, (0.0, 0.07), (0.34, 0.56)),
    # 石面：浅冷灰（台面、淋浴房）
    # ⚠️ 饱和度上限定在 **0.02**：低到看不出色相，才是真的"灰"。
    #    这个数字调过两次，两次都是被**搜索钻空子**逼出来的：
    #      · 第一版给 (0, 0.10) + 色相 30~220 → 搜出 #C7D0C6，**淡绿白**（H=114°）
    #      · 第二版给 (0, 0.06) + 色相不限 → 还是 #C8CEC8（H=120°）
    #    因为 ΔE 里色相也是分量：把饱和度顶到上限，就能从"白"那里多抠出
    #    一点色差。数值合格、语义全错 —— **石面不该是绿的。**
    #
    #    教训：**约束留下的一点余量，搜索引擎一定会用掉。**
    #    想让某个性质成立，就得把它写成"没有余量"（≤0.02 ≈ 人眼看不出），
    #    不能靠"上限松一点但它应该不会去顶"。
    #
    #    ⚠️ 上限定死之后，"石面"和"白"这两个中性浅色只能靠亮度差拉开，
    #       于是地面亮度必须压暗才够用 —— 见 `retune_floor` 的推导。
    "stone": (None, (0.0, 0.02), (0.45, 0.84)),
    # 叶绿
    "green": ((92.0, 152.0), (0.16, 0.46), (0.24, 0.50)),
    # 白（墙面件、卫浴）
    "white": ((30.0, 60.0), (0.0, 0.05), (0.84, 0.94)),
}

#: 相对亮度区间（见文件头 C3）
LUMA_MIN, LUMA_MAX = 0.05, 0.72
#: 对地面的最小对比度（C1）
MIN_FLOOR_CONTRAST = 2.0
#: 角色之间的最小 Lab ΔE（C2）
MIN_ROLE_DELTA_E = 6.6


# ══════════════════════════════════════════════════════════════════
# 颜色数学
# ══════════════════════════════════════════════════════════════════


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG 相对亮度。"""
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: tuple[float, float, float],
                   b: tuple[float, float, float]) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    lo, hi = min(la, lb), max(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """
    sRGB → CIE Lab（D65）。用最朴素的公式，不引第三方库 ——
    这一个函数是整个脚本的判据基础，写在明处比藏在依赖里好。
    """
    r, g, b = (_srgb_to_linear(c) for c in rgb)
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) / 1.00000
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(a: str | tuple[float, float, float],
            b: str | tuple[float, float, float]) -> float:
    ra = hex_to_rgb(a) if isinstance(a, str) else a
    rb = hex_to_rgb(b) if isinstance(b, str) else b
    return math.dist(rgb_to_lab(ra), rgb_to_lab(rb))


def hsl_to_rgb(h: float, s: float, l: float) -> tuple[float, float, float]:
    return colorsys.hls_to_rgb(h / 360.0, l, s)


def rgb_to_hsl(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    h, l, s = colorsys.rgb_to_hls(*rgb)
    return h * 360.0, s, l


# ══════════════════════════════════════════════════════════════════
# 约束检查
# ══════════════════════════════════════════════════════════════════


def audit(surface: dict[str, str], floor: str) -> dict:
    """把一套色板的全部指标算出来 —— **报告与断言用的是同一份数字**。"""
    floor_rgb = hex_to_rgb(floor)
    roles = {k: hex_to_rgb(v) for k, v in surface.items()}

    floor_contrast = {k: contrast_ratio(v, floor_rgb) for k, v in roles.items()}
    luma = {k: relative_luminance(v) for k, v in roles.items()}
    pairs: list[tuple[str, str, float]] = []
    keys = sorted(roles)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            pairs.append((a, b, delta_e(roles[a], roles[b])))
    min_pair = min(pairs, key=lambda x: x[2]) if pairs else ("", "", 0.0)

    return {
        "floor_contrast": floor_contrast,
        "luma": luma,
        "pairs": pairs,
        "min_delta_e": min_pair,
        "floor_contrast_ok": all(v >= MIN_FLOOR_CONTRAST
                                 for v in floor_contrast.values()),
        "luma_ok": all(LUMA_MIN <= v <= LUMA_MAX for v in luma.values()),
        "hue_ok": all(_in_window(k, v) for k, v in roles.items()),
        "min_delta_e_ok": min_pair[2] >= MIN_ROLE_DELTA_E,
    }


def window_violation(role: str, rgb: tuple[float, float, float]) -> str:
    """
    这个颜色偏离了它的角色窗口吗？返回越界的**具体维度**（空串 = 没越界）。

    ⚠️ 返回具体维度而不是一个 bool：报告里原先一律写"色相越界"，
       而实测第一次触发的其实是**饱和度**越界（fabric #D6CCBC 的 S=0.241
       略高于窗口上限 0.22）。**报错指错维度比不报还坏** —— 看的人会去调
       色相，而问题在饱和度上。
    """
    win = ROLE_WINDOWS.get(role)
    if win is None:
        return ""
    hue_win, sat_win, lit_win = win
    h, s, l = rgb_to_hsl(rgb)
    bad: list[str] = []

    hlo, hhi = hue_win if hue_win is not None else (None, None)
    # 饱和度低于 0.07 的灰**任何色相都算灰** —— 否则"metal 必须是冷色相"
    # 会把 #7A7A7A（H=0°）判成不合格，而 0° 和 240° 在饱和度 0 时是同一个颜色。
    if hlo is not None and s >= 0.07:
        if not ((hlo <= h <= hhi) or (hlo > hhi and (h >= hlo or h <= hhi))):
            bad.append(f"色相 {h:.0f}°∉[{hlo:.0f},{hhi:.0f}]")
    slo, shi = sat_win
    if not (slo <= s <= shi):
        bad.append(f"饱和度 {s:.2f}∉[{slo},{shi}]")
    llo, lhi = lit_win
    if not (llo <= l <= lhi):
        bad.append(f"明度 {l:.2f}∉[{llo},{lhi}]")
    return "；".join(bad)


def _in_window(role: str, rgb: tuple[float, float, float]) -> bool:
    return not window_violation(role, rgb)


def evaluate(surface: dict[str, str], floor: str) -> dict:
    """
    目标值 + **违反了什么**。

    ⚠️ 只返回一个分数的话，搜不到解时无从知道是"约束太紧"还是"搜索不够"，
    只能看到一句"没找到" —— 而这两种情况的处置完全不同。所以这里把
    每一类违反各差多少都报出来。
    """
    if not surface:
        return {"feasible": False, "objective": -1.0,
                "violations": ["没生成出候选"], "min_de": 0.0}
    a = audit(surface, floor)
    v: list[str] = []
    if not a["floor_contrast_ok"]:
        worst = min(a["floor_contrast"].items(), key=lambda kv: kv[1])
        v.append(f"对地面对比度最低 {worst[1]:.2f}:1（{worst[0]}），"
                 f"差 {MIN_FLOOR_CONTRAST - worst[1]:.2f}")
    if not a["luma_ok"]:
        low = [(k, x) for k, x in a["luma"].items() if x < LUMA_MIN]
        high = [(k, x) for k, x in a["luma"].items() if x > LUMA_MAX]
        if low:
            k, x = min(low, key=lambda kv: kv[1])
            v.append(f"亮度低于下界：{k}={x:.4f}（下界 {LUMA_MIN}，差 {LUMA_MIN - x:.4f}）")
        if high:
            k, x = max(high, key=lambda kv: kv[1])
            v.append(f"亮度高于上界：{k}={x:.4f}（上界 {LUMA_MAX}，超 {x - LUMA_MAX:.4f}）")
    bad_hue = [f"{k}: {window_violation(k, hex_to_rgb(c))}"
               for k, c in surface.items() if not _in_window(k, hex_to_rgb(c))]
    if bad_hue:
        v.append("角色窗口越界 —— " + "；".join(bad_hue[:2]))
    if not a["min_delta_e_ok"]:
        a1, b1, de = a["min_delta_e"]
        v.append(f"角色间最挤 ΔE {de:.2f}（{a1}↔{b1}），差 {MIN_ROLE_DELTA_E - de:.2f}")
    return {
        "feasible": not v,
        "objective": -1.0 if v else a["min_delta_e"][2],
        "violations": v,
        "min_de": a["min_delta_e"][2],
    }


def _score_tuple(surface: dict[str, str], floor: str) -> tuple[int, float, float]:
    """
    给搜索用的排序键：**(可行的排前面，同可行比最小 ΔE；不可行比"缺多少")**。

    ⚠️ 不可行时用 `min_de - 各类缺口` 当代理指标 —— 否则搜索在不可行区里
    就没有梯度，只能瞎撞。这是这个搜索能找到解的关键。
    """
    e = evaluate(surface, floor)
    if e["feasible"]:
        return (1, e["objective"], 0.0)
    a = audit(surface, floor)
    gap = 0.0
    gap += max(0.0, MIN_FLOOR_CONTRAST
               - min(a["floor_contrast"].values(), default=0.0))
    gap += max(0.0, LUMA_MIN - min(a["luma"].values(), default=1.0)) * 10
    gap += max(0.0, max(a["luma"].values(), default=0.0) - LUMA_MAX) * 10
    gap += sum(0.05 for k, c in surface.items()
               if not _in_window(k, hex_to_rgb(c))) * 10
    gap += max(0.0, MIN_ROLE_DELTA_E - a["min_delta_e"][2])
    return (0, -gap, a["min_delta_e"][2])


def score(surface: dict[str, str], floor: str) -> float:
    """兼容旧调用：可行返回最小 ΔE，不可行返回负数。"""
    e = evaluate(surface, floor)
    return e["objective"]


# ══════════════════════════════════════════════════════════════════
# 搜索
# ══════════════════════════════════════════════════════════════════


def sample_role(role: str) -> str:
    hue_win, (slo, shi), (llo, lhi) = ROLE_WINDOWS[role]
    if hue_win is None:
        h = 0.0
    else:
        hlo, hhi = hue_win
        if hlo > hhi:
            # 跨 0 的窗口
            h = (random.uniform(hlo, 360.0) if random.random() < 0.5
                 else random.uniform(0.0, hhi))
        else:
            h = random.uniform(hlo, hhi)
    return rgb_to_hex(hsl_to_rgb(h, random.uniform(slo, shi), random.uniform(llo, lhi)))


def jitter(hexv: str, amount: int) -> str:
    """
    在 RGB 上微调几个色阶。

    ⚠️ **只靠"整角色重新采样"是搜不到可行解的。** 实测这个约束集的可行域
    很窄：最近的一版只差 **0.0015 的亮度**和 **0.02 的对比度**，而
    均匀重采样落在那个角落的概率极低 —— 于是搜索报告"不可行"，
    而实际上差之毫厘。

    这个函数让它能做真正的爬山：一次只动 ±1~±3 个色阶。
    """
    rgb = hex_to_rgb(hexv)
    out = []
    for c in rgb:
        step = random.randint(-amount, amount) / 255.0
        out.append(min(1.0, max(0.0, c + step)))
    return rgb_to_hex(tuple(out))  # type: ignore[arg-type]


def derive(floor: str, seed: int = 7, restarts: int = 400,
           steps: int = 900) -> tuple[dict[str, str], dict]:
    """
    随机重启 + （重采样 | 微调）的爬山。角色只有 6 个、约束很紧，
    这个规模上不需要更聪明的搜索；**要的是可复现**（固定 seed）。

    返回 `(最好的那个候选, 它的评估)` —— **即使不可行也返回**，
    因为"最接近可行的那一版还差多少"正是需要看到的信息。
    """
    rng = random.Random(seed)
    roles = list(ROLE_WINDOWS)
    best: dict[str, str] = {}
    best_key = (-1, -1e9, -1e9)

    for _ in range(restarts):
        cur = {r: sample_role(r) for r in roles}
        cur_key = _score_tuple(cur, floor)
        for i in range(steps):
            r = rng.choice(roles)
            trial = dict(cur)
            # 前 1/3 大步（换角色），之后小步（爬山）—— 见 `jitter`
            if i < steps // 3:
                trial[r] = sample_role(r)
            else:
                trial[r] = jitter(cur[r], 3)
            k = _score_tuple(trial, floor)
            if k > cur_key:
                cur, cur_key = trial, k
        if cur_key > best_key:
            best, best_key = cur, cur_key
    return best, evaluate(best, floor)


# ══════════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════════


def report(name: str, surface: dict[str, str], floor: str, luma_floor: str) -> bool:
    a = audit(surface, floor)
    ok = (a["floor_contrast_ok"] and a["luma_ok"] and a["hue_ok"]
          and a["min_delta_e_ok"])
    print(f"\n{'─' * 66}\n{name}   地面 {floor}（亮度 "
          f"{relative_luminance(hex_to_rgb(floor)):.3f}）")
    print(f"  {'角色':<8}{'色值':<10}{'相对亮度':>9}{'对地面':>8}{'色相窗口':>10}")
    for r in sorted(surface):
        rgb = hex_to_rgb(surface[r])
        h, s, l = rgb_to_hsl(rgb)
        mark = ""
        luma = relative_luminance(rgb)
        if not (LUMA_MIN <= luma <= LUMA_MAX):
            mark = f"  ⚠ 亮度 {luma:.4f} 越界"
        bad = window_violation(r, rgb)
        if bad:
            mark += f"  ⚠ {bad}"
        print(f"  {r:<8}{surface[r]:<10}{luma:>9.4f}"
              f"{a['floor_contrast'][r]:>7.2f}:1{h:>9.0f}°{mark}")
    worst = min(a["pairs"], key=lambda x: x[2])
    print(f"  最挤的一对 ΔE：{worst[0]} ↔ {worst[1]} = {worst[2]:.2f}"
          f"（门槛 {MIN_ROLE_DELTA_E}）")
    print(f"  判定：{'✅ 全部满足' if ok else '❌ 有约束不满足'}")
    return ok


def retune_floor(floor: str, target_luma: float) -> str:
    """
    把地面调到指定的相对亮度，**色相与饱和度不变**。

    ⚠️ 为什么地面要参与调参 —— 这一条是算出来的，不是试出来的：

    对地面 ≥2:1 这条约束把可用亮度**从地面处劈成两半**：

        暗侧可用上限 = (F + 0.05) / 2 − 0.05
        亮侧可用下限 = 2 (F + 0.05) − 0.05

    于是 F 越高，暗侧越宽、**亮侧越窄**。而亮侧要塞进三个浅色角色
    （fabric / stone / white），其中 stone 与 white 都是中性色 ——
    两个中性色只能靠亮度差拉开，ΔE ≈ ΔL*。

    实测（`--check` 与下表）：F=0.2775（modern）时亮侧只剩 **5.86** 的
    ΔL* 空间，F=0.2933（nordic）只剩 **4.18**，而门槛是 **6.6** ——
    **永远达不到**。搜索当时给出的"解"是给 stone 抹一点淡绿（H=120°，
    饱和度 0.058）来制造色差，数值合格、语义错了（石面不该是绿的）。

    把 F 压到 ~0.25 两侧就都够：暗带 11.11 ΔL*、亮带 8.93 ΔL*。
    """
    h, s, _l = rgb_to_hsl(hex_to_rgb(floor))
    lo, hi = 0.0, 1.0
    for _ in range(60):                     # 二分：HSL 亮度 → 相对亮度是单调的
        mid = (lo + hi) / 2
        if relative_luminance(hsl_to_rgb(h, s, mid)) < target_luma:
            lo = mid
        else:
            hi = mid
    return rgb_to_hex(hsl_to_rgb(h, s, (lo + hi) / 2))


def main() -> int:
    ap = argparse.ArgumentParser(description="推导 3D 表面色")
    ap.add_argument("--check", action="store_true", help="只校验当前三套，不搜索")
    ap.add_argument("--floor-from", type=float, default=0.30,
                    help="地面亮度搜索的上界（从亮往暗找，取第一个可行的）")
    ap.add_argument("--floor-to", type=float, default=0.18,
                    help="地面亮度搜索的下界")
    args = ap.parse_args()

    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    print("约束：对地面 ≥ %.1f:1 ｜ 角色间 ΔE ≥ %.1f ｜ 相对亮度 ∈ [%.2f, %.2f]"
          % (MIN_FLOOR_CONTRAST, MIN_ROLE_DELTA_E, LUMA_MIN, LUMA_MAX))

    all_ok = True
    for style, st in data["styles"].items():
        if style.startswith("_"):
            continue
        floor = st.get("surface_floor") or st["floor"]
        all_ok &= report(f"当前 · {style}", st["surface"], floor, st["floor"])

    if args.check:
        return 0 if all_ok else 1

    print(f"\n{'═' * 66}\n搜索建议值\n{'═' * 66}")
    print(f"地面亮度从 {args.floor_from} 往下试到 {args.floor_to}"
          f"（**取第一个可行的，尽量少动原值**）")

    feasible_all = True
    for style, st in data["styles"].items():
        if style.startswith("_"):
            continue
        orig = st.get("surface_floor") or st["floor"]
        orig_luma = relative_luminance(hex_to_rgb(orig))
        print(f"\n── {style}：原地面 {orig}（亮度 {orig_luma:.4f}）──")
        chosen = None
        F = min(args.floor_from, orig_luma)
        while F >= args.floor_to - 1e-9:
            fl = retune_floor(orig, F)
            cand, ev = derive(fl)
            if ev["feasible"]:
                chosen = (fl, cand, ev)
                break
            print(f"   亮度 {F:.3f} → 不可行（{ev['violations'][0][:46]}…）")
            F -= 0.02
        if chosen is None:
            feasible_all = False
            fl = retune_floor(orig, args.floor_to)
            cand, ev = derive(fl)
            report(f"建议 · {style}（最暗也做不到）", cand, fl, st["floor"])
            print("  ❌ 试到最暗仍然不可行；最接近的一版：")
            for line in ev["violations"]:
                print(f"      · {line}")
            print("    " + json.dumps({"surface_floor": fl, **cand},
                                      ensure_ascii=False))
            continue
        fl, cand, ev = chosen
        print(f"   ✅ 地面亮度取 {F:.3f} → {fl}"
              f"（原值 {orig_luma:.4f}，{'未改' if fl == orig else '压暗了'}）")
        report(f"建议 · {style}", cand, fl, st["floor"])
        print("  可直接粘贴：")
        print("    " + json.dumps({"surface_floor": fl, **cand},
                                  ensure_ascii=False))

    if not feasible_all:
        # ⚠️ 这里写的是「」不是引号：中文文案里嵌 `"…"` 会把字符串截断，
        #    而报出来的是一个**语法错误**，指向的位置和真正的问题毫无关系。
        #    同一处坑在本项目踩过两次（另一次在测试的文案里）。
        print(
            "\n⚠️ 有风格搜不到可行解 —— **这说明约束之间互相打架**，"
            "\n   不是搜索不够努力（加大重启次数只是更努力地找一个不存在的解）。"
            "\n   处置顺序应当是：先确认哪一条是「必须有」的，再放松另一条。"
            "\n   三条硬约束里，C1（对地面 2:1）和 C3（亮度区间）"
            "\n   都直接服务于「家具看不看得见」；C2（ΔE）服务于"
            "\n   「两件家具区分得开」。真要在两者间取舍，先降 C2。"
        )
    return 0 if (all_ok and feasible_all) else 1


if __name__ == "__main__":
    raise SystemExit(main())
