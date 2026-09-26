"""
量 A-06「方案自审」那条路上的风险类型数分布。

    python scripts/measure_ac06_plan_review.py [轮数]

══════════════════════════════════════════════════════════════════
它量的是哪条路，以及为什么必须说清楚
══════════════════════════════════════════════════════════════════
A-06 有两个模式（见 `risk_reviewer.py`）：

    模式一  审查**用户给的报价单**     `POST /avoid-pit/review`
    模式二  审查**系统自己生成的方案**  `/design/generate` 的汇聚节点

`risk_reviewer.py` 里写着「模式一：审查一份报价单（**AC-06 主战场**）」，
`seed_data/sample_quotes/quote_01_economy.expected.json` 的 `_meta` 也写着
「AC-06 要求「识别 ≥ 5 类风险」。没有带标注的样本，就无法判断模型是识别了
5 条还是编了 5 条」。**所以 AC-06 的判定对象是模式一**，用的是带标注的样本
（`scripts/review_sample_quote.py`，可算召回率）。

而 `e2e_smoke.py` 的 AC-06 条目量的是**模式二** —— 审查我们自己用规则引擎
生成的方案。这**不是同一个对象**：自己的方案本来就是按规范算出来的，
真实存在的风险类型数天然更少；而且提示词里明确写着「为了凑数编造风险，
比漏判更伤信任」。

这个脚本存在的意义：**把模式二那条路的分布测出来、写下来**，
这样"AC-06 到底该拿哪条路判定"就是一个有数字支撑的判断，
而不是一句"我觉得量错了"。

══════════════════════════════════════════════════════════════════
判据
══════════════════════════════════════════════════════════════════
每轮 `/design/generate` 产出 3 套方案，每套独立跑一次 A-06 → **一轮 3 个样本**。
逐样本记 `distinct_type_count`，最后报分布、最小/最大值、以及达到 ≥5 的比例。
"""

from __future__ import annotations

import io
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass

API = "http://127.0.0.1:8000/api/v1"
#: 沿用的演示户型。**固定一份**，免得每轮换图把"提示词的影响"和
#: "换了一张图的影响"混在一起 —— 控制变量。
DEFAULT_LAYOUT = "layout_20260924_782486"
#: 一轮 = 3 个样本（3 套方案各跑一次 A-06）；生成整链实测约 120s
ROUND_TIMEOUT_S = 300


def _post(path: str, body: dict, token: str) -> dict:
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
    )
    return json.load(urllib.request.urlopen(req, timeout=60))


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(API + path,
                                 headers={"Authorization": f"Bearer {token}"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def _login() -> str:
    """从 tests 里读种子口令（仓库里已有），**不打印**。"""
    import re
    src = (ROOT / "tests" / "test_auth.py").read_text(encoding="utf-8")
    m = re.search(r'\[\(("?admin"?),\s*"([^"]+)"\)', src)
    if not m:
        raise SystemExit("tests/test_auth.py 里找不到 admin 的种子口令")
    r = _post("/auth/login", {"username": m.group(1).strip('"'),
                              "password": m.group(2)}, token="")
    return r["data"]["access_token"]


def one_round(layout_id: str, token: str, n: int) -> list[dict]:
    created = _post("/design/generate", {
        "layout_id": layout_id,
        "styles": ["modern", "nordic", "chinese"],
        "budget_grades": ["economy", "medium", "high"],
    }, token)
    tid = created["data"]["task_id"]
    t0 = time.time()
    while time.time() - t0 < ROUND_TIMEOUT_S:
        time.sleep(5)
        snap = _get(f"/task/{tid}/status", token)["data"]
        if snap.get("status") in ("completed", "failed", "cancelled"):
            plans = (snap.get("result") or {}).get("plans") or []
            out = []
            for p in plans:
                rk = p.get("risks") or {}
                findings = rk.get("findings") or []
                out.append({
                    "types": int(rk.get("distinct_type_count") or 0),
                    # ⚠️ **条数与"无出处条数"是"有没有在凑数"的判据。**
                    #    凑数会同时抬高这两项：为了凑够类型数，模型会多报
                    #    几条没依据的泛泛之谈。所以这两个数必须一起记，
                    #    否则"类型数涨了"这一个信号分不清是"扫得更全"
                    #    还是"编得更多"。
                    "findings": len(findings),
                    "uncited": sum(1 for f in findings if not f.get("has_source")),
                })
            print(f"    第 {n} 轮：{snap['status']}  "
                  f"类型 {[x['types'] for x in out]}  "
                  f"条数 {[x['findings'] for x in out]}  "
                  f"无出处 {[x['uncited'] for x in out]}  "
                  f"（{time.time() - t0:.0f}s）")
            return out
    print(f"    第 {n} 轮：超时")
    return []


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    layout_id = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_LAYOUT

    token = _login()
    print(f"户型 {layout_id}｜{rounds} 轮 × 3 套 = {rounds * 3} 个样本")
    print("（每轮约 120 秒，跑完一次完整生成链路）\n")

    samples: list[dict] = []
    for i in range(1, rounds + 1):
        samples.extend(one_round(layout_id, token, i))

    if not samples:
        print("\n✗ 一个样本都没拿到")
        return 1

    types = [s["types"] for s in samples]
    finds = [s["findings"] for s in samples]
    unc = [s["uncited"] for s in samples]
    hit = sum(1 for t in types if t >= 5)
    print(f"\n{'═' * 62}")
    print(f"  样本数      {len(samples)}")
    print(f"  类型数      {types}")
    print(f"  分布        {sorted(types)}  "
          f"中位数 {statistics.median(types):.1f}  "
          f"min/max {min(types)}/{max(types)}")
    print(f"  ≥5 类占比    {hit}/{len(types)} = {hit / len(types):.0%}")
    print(f"  条数        {finds}   均值 "
          f"{statistics.mean(finds):.1f}")
    print(f"  其中无出处    {unc}   均值 "
          f"{statistics.mean(unc):.1f}"
          f"（占 {sum(unc) / max(1, sum(finds)):.0%}）")
    print(f"{'═' * 62}")
    print("\n对照基线（改提示词之前，n=12）：类型数 "
          "[5,4,4, 4,4,5, 5,5,6, 4,3,5]，≥5 类占比 50%")
    print("⚠️ 类型数涨了、但条数与无出处条数**没有同比上涨**，"
          "\n   才能说明是多扫到了真实风险，而不是为了凑类型数在编。")
    print("\n⚠️ 这是**模式二（方案自审）**的分布。AC-06 的判定对象是"
          "\n   模式一（报价单审查，带标注样本，可算召回率）—— 见"
          "\n   `scripts/review_sample_quote.py`。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
