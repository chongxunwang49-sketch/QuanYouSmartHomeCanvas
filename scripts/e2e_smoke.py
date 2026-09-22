"""
端到端冒烟：真实调用 DeepSeek，跑完整条链路。

    户型图 → A-01 解析 → A-02 诊断 → A-03 三路并发规划 → fan-in 汇总

**为什么必须有这个脚本**：单元测试里的 Fake LLM 永远不会"编房间"、
不会输出坏 JSON、不会超时。而这三件事恰恰是真实模型最常干的。
fan-out 的并发行为、以及 A-03 的房间对齐在真实输出上是否有效，
只有在真模型上跑一遍才算验证过。

需要 DEEPSEEK_API_KEY。运行：

    python scripts/e2e_smoke.py            # 用内置合成户型图
    python scripts/e2e_smoke.py 某户型.png  # 用指定图片
"""

from __future__ import annotations

import asyncio
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


def make_floor_plan(path: Path) -> Path:
    """
    画一张合成户型图。

    刻意**不标房间名**——宁可让 A-01 的识别难度高一点，
    也别给一个理想输入来"证明"系统能跑。上一轮实测正是这种
    "没标名字的图"暴露了动线数据不足的处理路径。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (760, 560), "white")
    d = ImageDraw.Draw(img)

    # 外墙
    d.rectangle([40, 40, 720, 500], outline="black", width=8)
    # 承重墙（加粗）与隔墙
    d.line([380, 40, 380, 500], fill="black", width=8)      # 竖隔墙
    d.line([40, 300, 380, 300], fill="black", width=5)      # 左下分隔

    # 窗（双线表示）
    d.line([150, 36, 300, 36], fill="black", width=4)       # 北窗
    d.line([150, 44, 300, 44], fill="black", width=4)
    d.line([500, 496, 650, 496], fill="black", width=4)     # 南窗
    d.line([500, 504, 650, 504], fill="black", width=4)

    # 门（四分之一圆弧）
    d.arc([100, 270, 160, 330], start=0, end=90, fill="black", width=4)
    d.arc([350, 150, 410, 210], start=90, end=180, fill="black", width=4)
    d.arc([380, 380, 440, 440], start=180, end=270, fill="black", width=4)

    # 尺寸标注
    d.text((300, 508), "8200", fill="black")
    d.text((730, 260), "5600", fill="black")
    d.text((60, 40), "N/A", fill="black")

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def banner(text: str) -> None:
    print(f"\n{'═' * 66}\n  {text}\n{'═' * 66}")


async def main() -> int:
    from backend.app.core.config import settings
    from backend.app.core.llm_client import ImagePart
    from backend.app.graph.state import initial_state
    from backend.app.graph.workflow import build_graph
    from backend.app.services.material import catalog

    if not settings.DEEPSEEK_API_KEY:
        print("✗ 未配置 DEEPSEEK_API_KEY，无法运行真实链路")
        return 2

    if len(sys.argv) > 1:
        image_path = Path(sys.argv[1])
        if not image_path.exists():
            print(f"✗ 图片不存在: {image_path}")
            return 2
    else:
        image_path = make_floor_plan(ROOT / "logs" / "e2e_floor_plan.png")
        print(f"已生成合成户型图: {image_path}")

    part = ImagePart.from_path(str(image_path))

    state = initial_state(
        task_id="e2e-smoke",
        image_ref=part.to_data_uri(),
        image_media_type=part.media_type,
        detail_level="full",
        styles=["modern", "nordic", "chinese"],
        budget_grades=["economy", "medium", "high"],
        requirements={"family_size": 3, "has_children": True, "smart_home": True,
                      "eco_level": "E0"},
    )

    graph = build_graph(with_checkpointer=False)

    banner("开始执行（真实 DeepSeek）")
    started = time.perf_counter()
    out = await graph.ainvoke(state)
    wall = time.perf_counter() - started

    # ── trace ────────────────────────────────────────────────
    banner("各节点耗时")
    for t in out["trace"]:
        llm = t.get("llm") or {}
        mark = "✓" if t["ok"] else "✗"
        print(f"  {mark} {t['agent']:6s} {t['elapsed_ms']:>7d}ms  "
              f"{llm.get('model', '-'):<20s} degraded={llm.get('degraded')}")
        if not t["ok"]:
            errs = [e for e in out["errors"] if e["agent"] == t["agent"]]
            if errs:
                print(f"      └─ {errs[0]['message'][:100]}")

    # ── 解析 ─────────────────────────────────────────────────
    layout = out.get("layout") or {}
    banner("A-01 户型解析")
    print(f"  房间 {len(layout.get('rooms') or [])} 个，"
          f"窗 {len(layout.get('windows') or [])} 扇，"
          f"门 {len(layout.get('doors') or [])} 樘，"
          f"墙 {len(layout.get('walls') or [])} 段，"
          f"面积 {layout.get('total_area')}㎡")
    for r in layout.get("rooms") or []:
        print(f"      - {r.get('name')} ({r.get('type')}) "
              f"{r.get('area')}㎡ {r.get('orientation')}")

    # ── 诊断 ─────────────────────────────────────────────────
    diag = out.get("diagnosis")
    if diag:
        banner("A-02 户型诊断")
        print(f"  综合 {diag.get('overall_score')}  置信度 {diag.get('confidence')}")
        for dim, label in [("lighting", "采光"), ("ventilation", "通风"),
                           ("circulation", "动线"), ("space_utilization", "利用率"),
                           ("green_score", "环保")]:
            item = diag.get(dim) or {}
            flag = " ★数据不足" if item.get("insufficient_data") else ""
            print(f"      {label:4s} {item.get('score'):>4} {flag}")
    else:
        print("\n  (A-02 未产出诊断)")

    # ── 方案 ─────────────────────────────────────────────────
    plans = out.get("plans") or []
    banner(f"fan-in 汇总：{len(plans)} 套方案")
    for p in plans:
        sp = p.get("space_plan") or {}
        bg = p.get("budget") or {}
        mt = p.get("materials") or {}

        print(f"\n  【{p.get('plan_id')}】{p.get('style')} + {p.get('budget_grade')}")

        # ── A-03 空间规划 ──
        if sp:
            print(f"    规划: {sp.get('summary', '')[:70]}")
            zone_names = [z.get("room_name") for z in (sp.get("zones") or [])]
            print(f"    分区: {zone_names}")
            print(f"    收纳 {len(sp.get('storage_plans') or [])} 处，"
                  f"动线优化 {len(sp.get('circulation_fixes') or [])} 项，"
                  f"置信度 {sp.get('confidence')}")
            invented = sp.get("invented_rooms") or []
            unassigned = sp.get("unassigned_rooms") or []
            duplicates = sp.get("duplicate_zones") or []
            if invented:
                print(f"    ⚠ 幻觉房间被剔除 {len(invented)} 个: {invented}")
            if duplicates:
                print(f"    ⚠ 重复安排被去掉 {len(duplicates)} 个: {duplicates}")
            if unassigned:
                print(f"    ⚠ 未安排房间: {unassigned}")
        else:
            print("    ⚠ 空间规划未产出")

        # ── A-04 预算（规则引擎，不经模型）──
        if bg:
            flag = "  ⚠解说降级" if bg.get("narrative_degraded") else ""
            print(f"    预算: {bg.get('total_min', 0):,.0f} - {bg.get('total_max', 0):,.0f} 元"
                  f"  ({bg.get('price_per_sqm_min', 0):.0f}-{bg.get('price_per_sqm_max', 0):.0f} 元/㎡)"
                  f"  [{len(bg.get('lines') or [])} 项 / {bg.get('computed_by')}]{flag}")
            tips = (bg.get("narrative") or {}).get("negotiation_tips") or []
            if tips:
                print(f"    砍价: {tips[0][:60]}")
        else:
            print("    ⚠ 预算未产出")

        # ── A-05 材料选型（候选由目录给，价格由代码回填）──
        if mt:
            cov = mt.get("quanyou_coverage", 0)
            mark = "✓" if mt.get("quanyou_met") else "✗"
            print(f"    选材: {len(mt.get('items') or [])} 个品类，"
                  f"全友覆盖 {cov:.0%} {mark}"
                  f"（AC-18 门槛 {catalog.MIN_QUANYOU_COVERAGE:.0%}）")
            for item in (mt.get("items") or [])[:4]:
                lo, hi = item["price_range"]
                print(f"      - {item['category']:<9}{item['name'][:22]:<24}"
                      f"{lo:g}-{hi:g} {item['brand']}")
            if len(mt.get("items") or []) > 4:
                print(f"      …… 另有 {len(mt['items']) - 4} 项")
            if mt.get("auto_substitutions"):
                print(f"    ⚠ 代码替换以达标 {len(mt['auto_substitutions'])} 项")
            if mt.get("invented_products"):
                print(f"    ⚠ 幻觉商品被剔除: {mt['invented_products']}")
        else:
            print("    ⚠ 材料选型未产出")

        # ── A-06 避坑审查（汇聚节点，在三个产出者之后）──
        rk = p.get("risks") or {}
        if rk:
            met = "✓" if rk.get("ac06_met") else "✗"
            print(f"    避坑: {rk.get('finding_count', 0)} 条风险 / "
                  f"{rk.get('distinct_type_count', 0)} 类 {met} "
                  f"（AC-06 要求 ≥5 类）｜整体 {rk.get('overall_risk')}")
            for f in (rk.get("findings") or [])[:3]:
                mark = "📎" if f.get("has_source") else "  "
                print(f"      {mark} [{f['severity']:<6}] {f.get('title')}")
            if len(rk.get("findings") or []) > 3:
                print(f"      …… 另有 {len(rk['findings']) - 3} 条")
            if rk.get("invented_citations"):
                print(f"    ⚠ 模型编造引用被剔除: {rk['invented_citations']}")
        else:
            print("    ⚠ 避坑审查未产出")

        if p.get("missing_artifacts"):
            print(f"    ⚠ 缺失产物: {p['missing_artifacts']}")

    comp = out.get("comparison") or {}
    banner("对比表")
    print(f"  可用: {comp.get('available')}  "
          f"实际/请求: {comp.get('plan_count')}/{comp.get('requested_count')}")
    print(f"  {'方案':<22}{'档位':<9}{'预算下限':>10}{'预算上限':>10}"
          f"{'元/㎡':>14}{'全友覆盖':>10}")
    for row in comp.get("rows") or []:
        lo, hi = row.get("budget_total_min"), row.get("budget_total_max")
        cov = row.get("quanyou_coverage")
        cov_txt = f"{cov:.0%}" if cov is not None else "-"
        rk = row.get("risk_count"); rt = row.get("risk_type_count")
        risk_txt = f"{rk}条/{rt}类" if rk is not None else "-"
        print(f"  {row['plan_id']:<22}{row['budget_grade']:<9}"
              f"{lo:>10,.0f}{hi:>10,.0f}")
        if row.get("budget_per_sqm_min"):
            print(f"  {'':<31}{row['budget_per_sqm_min']:>8.0f}-"
                  f"{row['budget_per_sqm_max']:<7.0f} 元/㎡{cov_txt:>10}{risk_txt:>10}")
    for note in comp.get("notes") or []:
        print(f"  ⚠ {note}")

    # ── 演示数据声明（R-09 要求显著标注）──
    warn = (plans[0].get("materials") or {}).get("disclaimer") if plans else ""
    if warn:
        print(f"\n  ⚠ 数据声明：{warn[:100]}…")

    # ── 汇总 ─────────────────────────────────────────────────
    banner("总览")
    _BRANCH_CODES = ("A-03", "A-04", "A-05", "A-06")
    branch_ms = [t["elapsed_ms"] for t in out["trace"] if t["agent"] in _BRANCH_CODES]
    print(f"  墙钟总耗时 : {wall:.1f}s")

    if branch_ms:
        for code in _BRANCH_CODES:
            ms = [t["elapsed_ms"] for t in out["trace"] if t["agent"] == code]
            if ms:
                print(f"  {code} 分支  : {ms}  (max {max(ms)}ms / sum {sum(ms)}ms)")

        # ⚠️ 并发判定必须量「fan-out 阶段的时长」，不能拿总墙钟去比。
        # 总墙钟含 A-01 + A-02，把它们算进去会得出"疑似串行"的错误结论。
        # fan-out 阶段 ≈ 总墙钟 − 上游各节点耗时（fan-in 是纯计算，可忽略）。
        upstream_ms = sum(t["elapsed_ms"] for t in out["trace"] if t["agent"] in ("A-01", "A-02"))
        fanout_s = wall - upstream_ms / 1000
        slowest = max(branch_ms) / 1000
        ratio = fanout_s / slowest if slowest else 0
        # 并发时 ratio ≈ 1.0（略大于 1，含调度开销）；串行时会接近任务数
        verdict = "并发" if ratio < 1.6 else f"疑似串行（{ratio:.1f}× 于最慢任务）"
        print(f"  fan-out 阶段: {fanout_s:.1f}s  "
              f"({len(branch_ms)} 个任务，最慢 {slowest:.1f}s，比值 {ratio:.2f}×)")
        print(f"  并发判定   : {verdict}")
    print(f"  phase      : {out.get('phase')}")
    print(f"  degraded   : {out.get('degraded')}")
    print(f"  错误       : {len(out.get('errors') or [])} 条")

    ok = bool(plans) and not out.get("errors")
    print(f"\n  {'✓ 链路通过' if ok else '✗ 链路存在问题'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
