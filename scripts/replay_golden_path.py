"""
黄金路径重放（AC-32）。

    python scripts/replay_golden_path.py              # 重放 3 次，比对一致性
    python scripts/replay_golden_path.py -n 5         # 重放 5 次
    python scripts/replay_golden_path.py --reparse    # 额外量解析步骤的漂移

═══════════════════════════════════════════════════════════════════
它测的是"同一份输入跑三次会不会给出同样的结果"
═══════════════════════════════════════════════════════════════════
需求文档附录 F 把这条写成演示前的检查项（F.2 第 6 步），理由很实在：
**面试现场翻车最可能的原因不是功能坏了，而是"这次跑出来的和准备时不一样"**。
所以它要回答的不是"能不能跑通"（那是 `scripts/e2e_smoke.py` 的活），
而是"跑出来的东西稳不稳"。

⚠️ 输入是**冻结的解析产物**（`scripts/fixtures/golden_layout.json`），
   不是那张图。理由写在 `backend/app/core/consistency.py` 的模块说明里，
   一句话：视觉解析是这条链上唯一的不可复现来源，把它混在分母里，
   "结果不一致"就永远说不清是下游有 bug 还是这次图本来就读得不一样。
   解析自己的漂移由 `--reparse` 单独量。

═══════════════════════════════════════════════════════════════════
判据（口径写在 core/consistency.py）
═══════════════════════════════════════════════════════════════════
  A 类 · 确定性字段（预算金额、plan_id 集合、分项明细…）
        **要求 100% 逐字段相等** —— 它们是纯函数算的，做不到就是有真 bug
  B 类 · 结构不变量（风险类型在枚举内、材料 id 都在目录里…）
        要求每次成立；不成立就是不合格，与分数无关
  C 类 · 漂移（风险条数、文本长度）
        **只记录**，不判失败

需求原文写的是"一致性 ≥ 95%"。这里按 **100%** 判 A 类并如实打印实际
分数：**A 类字段是规则引擎与纯代码算出来的，没有"95% 对"这种状态** ——
一个金额对不上就是错了。95% 那个数字是给"含 LLM 输出"的口径写的，
而本脚本把 LLM 输出挪进了 B/C 两类，所以门槛反而应当更严。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass

from backend.app.core.consistency import consistency_score, fingerprint  # noqa: E402
from backend.app.graph.state import initial_state  # noqa: E402
from backend.app.graph.workflow import build_graph  # noqa: E402

FIXTURE = ROOT / "scripts" / "fixtures" / "golden_layout.json"
OUT_DIR = ROOT / "logs" / "golden"

#: 黄金路径的固定参数。**重放的全部意义就是这几个值不变** ——
#: 改了它们，比出来的差异就不再是"不确定性"，而是"我换了输入"。
FROZEN_INPUT: dict = {
    "styles": ["modern", "nordic", "chinese"],
    "budget_grades": ["economy", "medium", "high"],
    "requirements": {"family_size": 3, "has_children": True, "smart_home": True,
                     "eco_level": "E0"},
}


def banner(text: str) -> None:
    print("\n" + "═" * 64)
    print(f"  {text}")
    print("═" * 64)


def load_fixture() -> dict:
    if not FIXTURE.exists():
        print(f"✗ 缺少固定输入 {FIXTURE}")
        print("  它是一份真实解析产物的存档。重新生成：")
        print("    docker exec qy-postgres psql -U qy -d quanyou -t -A \\")
        print("      -c \"select parsed_data from house_layouts "
              "where parsed_data->>'mode'='full' order by created_at desc limit 1\" \\")
        print(f"      > {FIXTURE.relative_to(ROOT)}")
        raise SystemExit(2)
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


async def one_run(layout: dict, index: int) -> dict:
    """跑一次方案链。**从 stages='generate' 进** —— 跳过视觉解析。"""
    graph = build_graph(stages="generate", with_checkpointer=False)
    state = initial_state(
        task_id=f"golden-{index}",
        layout=layout,
        layout_id=str(layout.get("layout_id") or "golden"),
        **FROZEN_INPUT,
    )
    started = time.perf_counter()
    out = await graph.ainvoke(state)
    out["_elapsed_seconds"] = round(time.perf_counter() - started, 1)
    return out


async def reparse_drift(times: int) -> dict:
    """
    额外量一次**解析步骤自己**的漂移（C 类）。

    它不会让重放失败 —— 视觉模型本来就不是确定性的。量它的价值在于把
    "这次为什么和上次不一样"拆成两个来源：如果 A 类全等而这里漂了，
    那差异全在解析，下游是好的；反过来才是下游的问题。
    """
    from backend.app.agents.layout_parser import LayoutParserAgent
    from backend.app.core.llm_client import ImagePart

    image = ROOT / "演示素材" / "户型图" / "03-三室两厅-98平.png"
    if not image.exists():
        return {"available": False, "reason": f"演示图不存在：{image}"}

    part = ImagePart.from_path(str(image))
    counts, areas = [], []
    for i in range(times):
        agent = LayoutParserAgent()
        state = initial_state(
            task_id=f"golden-reparse-{i}", image_ref=part.to_data_uri(),
            image_media_type=part.media_type, detail_level="full",
        )
        out = await agent.execute(state)
        layout = out.get("layout") or {}
        counts.append(len(layout.get("rooms") or []))
        areas.append(round(float(layout.get("total_area") or 0.0), 1))
        print(f"    第 {i + 1} 次：房间 {counts[-1]} 间，面积 {areas[-1]} ㎡，"
              f"mode={layout.get('mode')}")
    return {
        "available": True,
        "runs": times,
        "rooms": counts,
        "areas": areas,
        "rooms_stable": len(set(counts)) == 1,
        # 面积漂 15% 以上就足以让预算数字整体走样，演示会当场翻车
        "area_spread_pct": (
            round((max(areas) - min(areas)) / max(areas) * 100, 1)
            if max(areas) else 0.0
        ),
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="黄金路径重放（AC-32）")
    ap.add_argument("-n", type=int, default=3, help="重放次数（默认 3）")
    ap.add_argument("--reparse", action="store_true", help="额外量解析步骤的漂移")
    ap.add_argument("--reparse-times", type=int, default=3)
    args = ap.parse_args()

    layout = load_fixture()
    banner("黄金路径重放")
    print(f"  固定输入：{FIXTURE.relative_to(ROOT)}")
    print(f"    模式 {layout.get('mode')}｜{len(layout.get('rooms') or [])} 间房"
          f"｜{layout.get('total_area')} ㎡｜{len(layout.get('walls') or [])} 面墙")
    print(f"  固定参数：{FROZEN_INPUT['styles']} × {FROZEN_INPUT['budget_grades']}")
    print(f"  重放 {args.n} 次")

    # ── 重放 ─────────────────────────────────────────────────
    results: list[dict] = []
    for i in range(args.n):
        print(f"\n  ── 第 {i + 1}/{args.n} 次 ──")
        out = await one_run(layout, i)
        results.append(out)
        print(f"    耗时 {out['_elapsed_seconds']}s，"
              f"{len(out.get('plans') or [])} 套方案，"
              f"降级={bool(out.get('degraded'))}，"
              f"错误 {len(out.get('errors') or [])} 条")
        for t in out.get("trace") or []:
            llm = t.get("llm") or {}
            mark = "✓" if t["ok"] else "✗"
            print(f"      {mark} {t['agent']:6s} {t['elapsed_ms']:>7d}ms "
                  f"{llm.get('model', '-'):<18s} degraded={llm.get('degraded')}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 逐条清单（复用 e2e_smoke 的那 22 条）──────────────────
    #
    # ⚠️ 复用而不是重写：那是这个项目里唯一一份"20/20 指回契约"的清单，
    #    重写一份就等于多一个会漂移的副本。
    from scripts.e2e_smoke import checklist, checklist_rows

    banner("逐条清单（与 e2e_smoke 同一份）")
    expected_rooms = len(layout.get("rooms") or [])
    checklist_ok = True
    failed_items: list[str] = []
    for i, out in enumerate(results):
        passed, total = checklist(out, expected_rooms=expected_rooms)
        print(f"  第 {i + 1} 次：{passed}/{total}")
        if passed < total:
            checklist_ok = False
        # 记下是哪一条没过 —— 结论里要指名道姓。
        # ⚠️ 不能让"AC-32 未通过"概括成"重放有问题"：清单里那些条目
        #    属于**别的验收项**（AC-02/05/06/18/20…）。混在一起报，
        #    读的人会去查重放脚本，而真正的问题在 A-06 的提示词上。
        for name, ok, _detail in checklist_rows(out, expected_rooms=expected_rooms):
            if not ok and name not in failed_items:
                failed_items.append(name)

    # ── 一致性 ───────────────────────────────────────────────
    prints = [fingerprint(out) for out in results]
    report = consistency_score(prints)

    banner("一致性（AC-32）")
    print(f"  基线：第 1 次｜A 类字段 {report.get('baseline_keys')} 个"
          f"｜比对 {report.get('compared_fields')} 个字段")
    print(f"  A 类一致率：{report['score']:.4%}")

    for i, rep in enumerate(report.get("reports") or [], start=2):
        print(f"\n  ── 第 {i} 次 vs 第 1 次 ──")
        a = rep["a"]
        print(f"    A 类：{a['matched']}/{a['total']}")
        for key, diff in list(a["diffs"].items())[:8]:
            print(f"      ✗ {key}")
            print(f"          基线 {diff['baseline']!r}")
            print(f"          本次 {diff['current']!r}")
        if len(a["diffs"]) > 8:
            print(f"      （另有 {len(a['diffs']) - 8} 处未列出，见 {OUT_DIR}）")
        failed = rep["b"]["failed"]
        print(f"    B 类：{'全部成立' if not failed else '有 ' + str(len(failed)) + ' 条不成立'}")
        for key in failed:
            print(f"      ✗ {key}")
        if rep["c"]["drift"]:
            print(f"    C 类漂移（只记录，不判失败）：{len(rep['c']['drift'])} 项")
            for key, diff in list(rep["c"]["drift"].items())[:6]:
                print(f"      · {key}: {diff['baseline']} → {diff['current']}")

    # ── 解析漂移（可选）──────────────────────────────────────
    reparse = None
    if args.reparse:
        banner(f"解析步骤的漂移（{args.reparse_times} 次，真实调用）")
        reparse = await reparse_drift(args.reparse_times)
        if reparse.get("available"):
            print(f"  房间数：{reparse['rooms']}（一致={reparse['rooms_stable']}）")
            print(f"  面积：{reparse['areas']}（离散度 {reparse['area_spread_pct']}%）")
        else:
            print(f"  跳过：{reparse.get('reason')}")

    # ── 落盘 ─────────────────────────────────────────────────
    summary = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "fixture": str(FIXTURE.relative_to(ROOT)),
        "frozen_input": FROZEN_INPUT,
        "runs": args.n,
        "elapsed_seconds": [r["_elapsed_seconds"] for r in results],
        "consistency": {
            "score": report["score"],
            "identical": report["identical"],
            "baseline_keys": report.get("baseline_keys"),
            "compared_fields": report.get("compared_fields"),
            "a_diffs": [rep["a"]["diffs"] for rep in report.get("reports") or []],
            "b_failed": [rep["b"]["failed"] for rep in report.get("reports") or []],
            "c_drift": [rep["c"]["drift"] for rep in report.get("reports") or []],
        },
        "checklist_ok": checklist_ok,
        "reparse": reparse,
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for i, out in enumerate(results):
        (OUT_DIR / f"run_{i + 1}.json").write_text(
            json.dumps(prints[i], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    banner("结论")
    # ⚠️ **两个验收项分开判。**
    #
    # `checklist` 里那些条目属于别的 AC（AC-02/05/06/18/20…），
    # 把它们并进"AC-32 通不通过"会让结论说谎：AC-32 问的是"结果稳不稳"，
    # 而"AC-06 的风险类型够不够 5 类"是另一个问题，它的答案在这里只是**顺带观测到**。
    # 第一次跑出 100% 一致率却打印"AC-32 未通过"，就是这么来的。
    consistent = report["score"] >= 1.0 and report["identical"]

    print(f"  AC-32 一致性：A 类 {report['score']:.4%}（要求 100%）"
          f"｜B 类 {'全部成立' if report['identical'] or not failed_items else '见上'}"
          f" → {'通过' if consistent else '未通过'}")
    print(f"  附带的逐条清单（属 AC-02/05/06/18/20 等）："
          f"{'全部通过' if checklist_ok else '有未通过项'}")
    if failed_items:
        print("    未通过项（**不是 AC-32 的问题**，见各自验收行）：")
        for name in failed_items:
            print(f"      · {name}")
    print(f"  报告：{OUT_DIR.relative_to(ROOT)}/summary.json")

    print(f"\n  {'✓ AC-32 通过' if consistent else '✗ AC-32 未通过'}")
    if not checklist_ok:
        print(f"  ⚠️ 但附带清单有未通过项 —— 修好之前**不要拿去演示**："
              f"重放的意义是确认稳定，稳定地差不算通过。")
    # 退出码仍然对两者都敏感：清单红了就是不能演示，这一点不该因为
    # "AC-32 自己过了"而放过。
    return 0 if (consistent and checklist_ok) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
