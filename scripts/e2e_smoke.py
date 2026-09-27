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
        # 用户给的图：按 AC-02 的门槛（≥4 间）
        expected_rooms = 4
        image_path = Path(sys.argv[1])
        if not image_path.exists():
            print(f"✗ 图片不存在: {image_path}")
            return 2
    else:
        image_path = make_floor_plan(ROOT / "logs" / "e2e_floor_plan.png")
        # 合成图只画了 3 间（外墙 + 两道隔墙），期望值跟着它走
        expected_rooms = 3
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

    # ── AC-36：语义化进度 ────────────────────────────────────
    #
    # ⚠️ **必须走任务管理器才采得到阶段序列。** 上面那条链是
    #    `graph.ainvoke` 一次跑完的，它只看得到**终态**的 phase ——
    #    而 AC-36 的原话是「`phase` 在任务执行中**至少经历 3 个不同取值**」，
    #    那要的是**过程中**的观测。
    #
    #    所以这里额外跑一次解析（约 50 秒），走的是接口层同一条路
    #    （`TaskManager.create` + `start` + 轮询 `status`）。
    #    多花一次解析的钱，换一条**可测量的** AC —— 值。
    #
    #    ⚠️ 用**一份全新的 state**，不复用上面那条链的：`ainvoke` 虽然不
    #    改传入的 dict，但两个任务共用一份状态是自找麻烦（task_id 之类的
    #    字段会互相看得到），而这里多构造一次的成本是零。
    phases_seen, phases_note = await _observe_phases(
        lambda: initial_state(
            task_id="e2e-ac36",
            image_ref=part.to_data_uri(),
            image_media_type=part.media_type,
            detail_level="full",
        ),
        kind="parse",
    )
    if phases_note:
        print(f"  ⚠️ AC-36 未能测量：{phases_note}")

    # ── 验收清单（AC-15）──
    passed, total = checklist(out, expected_rooms=expected_rooms,
                              phases=phases_seen, phases_note=phases_note)
    banner(f"验收清单：{passed}/{total}")
    ok = passed == total
    print(f"\n  {'✓ 链路通过' if ok else '✗ 链路存在问题'}")
    return 0 if ok else 1


async def _observe_phases(state_fn, *, kind: str,
                          attempts: int = 2) -> tuple[list[str], str]:
    """
    跑一个任务，把轮询过程中见过的 `phase` 按出现顺序记下来。

    ⚠️ 这是 AC-36 唯一的**测量**手段：`phase` 是**过程中**的量，
    终态里只剩一个值。上面那条主链是 `ainvoke` 一次跑完的，
    看不到中间过程 —— 所以这里另起一个任务，走接口层同一条路。

    返回 `(阶段序列, 失败原因)`。**失败原因非空 = 这次没测成**。

    ⚠️ **为什么要有重试。** 实测踩过一次：这个观测任务跑在主链之后，
    而那一次主链很慢（A-06 花了 88 秒、fan-out 段 120 秒）—— 观测任务
    在同一个本机模型服务上撞了节点超时而**失败**，于是只看到一个阶段
    `['analyzing']`，清单报的是"AC-36 未通过"。

    那是**测量失败**，不是 AC 失败：AC-36 关心的是"机制能不能产出 3 个
    阶段"，而那已经由 `tests/test_progress.py` 与审查链的实测守着。
    把一次 API 抖动报成"某条 AC 不达标"，与这个项目"不产出看起来合理的
    错误"是同一条纪律的反面。

    所以：重试一次；两次都失败才返回失败原因，让清单如实说
    "**未能测量**"而不是判它不过。

    ⚠️ `state_fn` 收的是**构造函数**不是 state —— 重试要一份干净的状态，
    复用同一份会让第二次带着第一次的中间产物（`review_sources` 之类）。
    """
    from backend.app.api.tasks import get_task_manager

    tm = get_task_manager()
    last_note = ""
    for attempt in range(1, attempts + 1):
        rec = tm.create(kind, trace_id=f"e2e-ac36-{attempt}")
        tm.start(rec, state_fn(), stages=kind)

        seen: list[str] = []
        deadline = time.perf_counter() + 240
        while time.perf_counter() < deadline:
            await asyncio.sleep(1.0)
            snap = await tm.status(rec.task_id)
            if snap is None:
                continue
            ph = str(snap.get("phase") or "")
            if ph and ph not in seen:
                seen.append(ph)
                print(f"      · [{attempt}] 阶段 {len(seen)}: {ph}  "
                      f"{snap.get('phase_text', '')}")
            if snap.get("status") in ("completed", "failed", "cancelled"):
                if snap.get("status") == "completed":
                    return seen, ""
                last_note = (f"第 {attempt} 次观测任务 {snap.get('status')}："
                             f"{snap.get('error') or '未给出原因'}")
                print(f"      · [{attempt}] 观测任务未跑完 —— {last_note}")
                break
        else:
            last_note = f"第 {attempt} 次观测任务超时（>240s）"
        if len(seen) < 3 and attempt < attempts:
            print(f"      · [{attempt}] 只看到 {len(seen)} 个阶段，重试一次")
            await asyncio.sleep(3)
    return [], last_note or "观测任务未能跑完，且没有给出原因"


#: AC-15 的判据。每条都是对**真实产物**的断言，不是"跑完没报错"。
#: 之所以列这么多条，是因为"整条链跑通了"这句话本身没有信息量 ——
#: 三套方案里有一套没出预算、审查结论全是编造的引用，链路照样"跑通"。
def checklist_rows(
    out: dict, *, expected_rooms: int = 4, phases: list[str] | None = None,
    phases_note: str = "",
) -> list[tuple[str, bool, str]]:
    """
    构造逐条检查的结果，返回 `[(条目名, 是否通过, 证据)]`。

    ⚠️ `phases` **不给就跳过 AC-36 那一条**（不给一个假结论）。
    重放脚本不做阶段观测（它跑的是冻结的解析产物，不经过任务管理器），
    所以它报的总数会比 e2e 少一条 —— 那是实情，不是漏检。

    ⚠️ **抽出来是因为重放脚本也要用它**（`scripts/replay_golden_path.py`）。
    那个脚本需要知道"是哪一条没过"，好把结论指名道姓地报出来 ——
    而它此前只能拿到一个 `(通过数, 总数)`，于是任何一条红了都会
    被它概括成"AC-32 未通过"，让读的人去查重放，而问题其实在别处
    （比如 A-06 的提示词）。重写一份清单就等于多一个会漂移的副本，
    所以这里是抽函数而不是复制。
    """
    layout = out.get("layout") or {}
    diag = out.get("diagnosis") or {}
    plans = out.get("plans") or []
    comp = out.get("comparison") or {}
    errors = out.get("errors") or []

    def has_plan_field(field: str) -> bool:
        return bool(plans) and all(p.get(field) for p in plans)

    def budget_of(p: dict) -> dict:
        return p.get("budget") or {}

    def materials_of(p: dict) -> dict:
        return p.get("materials") or {}

    findings = [f for p in plans for f in ((p.get("risks") or {}).get("findings") or [])]

    # ⚠️ 每条都带一句**可读的证据**，失败时打印出来。
    #    没有它，一条 ✗ 只会让人再去重跑一遍找原因 —— 而这整条链要两分钟。
    checks: list[tuple[str, bool, str]] = [
        # ⚠️ 期望值要跟输入走：内置合成图只画了 3 间（外墙 + 两道隔墙），
        #    拿 AC-02 给真实户型图定的"≥4 间"去卡它，红的是这条判据而不是系统。
        #    真实图片走 AC-02 的门槛。
        (f"AC-02 解析出 ≥{expected_rooms} 个房间",
         len(layout.get("rooms") or []) >= expected_rooms,
         f"实际识别 {len(layout.get('rooms') or [])} 个房间"),
        ("AC-02 识别到墙体", bool(layout.get("walls")),
         f"墙体 {len(layout.get('walls') or [])} 段"),
        ("AC-02 识别到门窗", bool(layout.get("windows")) and bool(layout.get("doors")),
         f"窗 {len(layout.get('windows') or [])} / 门 {len(layout.get('doors') or [])}"),
        ("AC-02 有总面积且 > 0", (layout.get("total_area") or 0) > 0,
         f"total_area={layout.get('total_area')}"),
        ("AC-03 诊断含五个维度",
         all(diag.get(k) for k in ("lighting", "ventilation", "circulation",
                                   "space_utilization", "green_score")),
         f"缺失维度 {[k for k in ('lighting','ventilation','circulation','space_utilization','green_score') if not diag.get(k)]}"),
        ("AC-03 诊断带置信度", diag.get("confidence") is not None,
         f"confidence={diag.get('confidence')}"),
        ("AC-04 三套方案全部产出", len(plans) == 3, f"实际 {len(plans)} 套"),
        ("AC-04 每套都有空间规划", has_plan_field("space_plan"),
         f"缺空间规划的方案 {[p['plan_id'] for p in plans if not p.get('space_plan')]}"),
        ("AC-05 每套都有预算", has_plan_field("budget"),
         f"缺预算的方案 {[p['plan_id'] for p in plans if not p.get('budget')]}"),
        ("AC-05 预算分项 ≥ 7",
         bool(plans) and all(len(budget_of(p).get("lines") or []) >= 7 for p in plans),
         f"各方案分项数 {[len(budget_of(p).get('lines') or []) for p in plans]}"),
        ("AC-05 预算由规则引擎算（非 LLM）",
         bool(plans) and all("rule" in str(budget_of(p).get("computed_by", "")).lower()
                             for p in plans),
         f"computed_by={[budget_of(p).get('computed_by') for p in plans]}"),
        ("AC-05 预算上下限合理",
         bool(plans) and all(0 < budget_of(p).get("total_min", 0)
                             < budget_of(p).get("total_max", 0) for p in plans),
         f"区间 {[(budget_of(p).get('total_min'), budget_of(p).get('total_max')) for p in plans]}"),
        ("AC-18 每套都有选材且报告覆盖率",
         bool(plans) and all(materials_of(p).get("quanyou_coverage") is not None
                             for p in plans),
         f"覆盖率 {[materials_of(p).get('quanyou_coverage') for p in plans]}"),
        ("AC-18 没有幻觉商品",
         bool(plans) and all(not materials_of(p).get("invented_products")
                             for p in plans),
         f"幻觉商品 {[materials_of(p).get('invented_products') for p in plans]}"),
        ("AC-06 每套都有避坑审查", has_plan_field("risks"),
         f"缺审查的方案 {[p['plan_id'] for p in plans if not p.get('risks')]}"),
        # ⚠️ **不能要求"每条结论都有出处"。**
        #    本项目的设计是：结论允许没有出处，但**必须显式标出来**
        #    （`no_citation` / `data_gaps`，界面也会提示"本次没有可引用依据"）。
        #    要求 100% 带出处会把"如实报告依据不足"判成失败 —— 那等于逼着
        #    系统去编引用，与 AC-06 的本意正好相反。
        #    所以分两条：① RAG 真的用上了（至少一条带出处）；
        #              ② 没有出处的那些**被算出来了**（不是被悄悄放过）。
        ("AC-06 审查真的用上了知识库（至少一条带出处）",
         any(f.get("has_source") for f in findings),
         f"共 {len(findings)} 条结论，带出处 {sum(1 for f in findings if f.get('has_source'))} 条"),
        ("AC-06 识别 ≥5 类风险（ac06_met）",
         bool(plans) and all((p.get("risks") or {}).get("ac06_met") for p in plans),
         f"各类风险数 {[(p.get('risks') or {}).get('distinct_type_count') for p in plans]}"),
        # ⚠️ 这条查的是**诚实性**，不是"有没有问题"：
        #    结论允许没有出处（常识性提醒本来就没有依据），但**必须被报出来**。
        #    原来那版要求"每条都有出处"，等于逼系统去编引用 —— 与 AC-06 相反。
        ("AC-06 无出处/编造引用被如实写进 data_gaps",
         bool(plans) and all(
             # 没有出处的结论 → 必须有一条 data_gap 说明
             (not [f for f in ((p.get("risks") or {}).get("findings") or [])
                   if not f.get("has_source")])
             or any("没有知识库依据" in g
                    for g in ((p.get("risks") or {}).get("data_gaps") or []))
             for p in plans),
         f"无出处结论数 {[sum(1 for f in ((p.get('risks') or {}).get('findings') or []) if not f.get('has_source')) for p in plans]}；"
         f"对应 data_gaps {[[g[:28] for g in ((p.get('risks') or {}).get('data_gaps') or []) if '没有知识库依据' in g] for p in plans]}"),
        ("AC-20 每套都有环境风险（甲醛/TVOC）",
         bool(plans) and all((p.get("environment") or {}).get("formaldehyde")
                             for p in plans),
         f"缺环境评估的方案 {[p['plan_id'] for p in plans if not (p.get('environment') or {}).get('formaldehyde')]}"),
        ("AC-20 环境结论不带浓度数字",
         not any(u in json.dumps([p.get("environment") for p in plans],
                                 ensure_ascii=False)
                 for u in ("mg/m", "ppm", "µg", "μg")),
         "输出里出现了浓度单位"),
        ("AC-04 对比表可用且行数对得上",
         bool(comp.get("available")) and len(comp.get("rows") or []) == len(plans),
         f"available={comp.get('available')} 行数={len(comp.get('rows') or [])}/{len(plans)}"),
        ("AC-17 链路无错误条目", not errors, f"errors={[e.get('message','')[:60] for e in errors]}"),
    ]

    # ── AC-36 语义化进度 ──
    #
    # ⚠️ **只有真的观测过阶段序列时才出这一条。** 不给 `phases` 就跳过，
    #    而不是判成"通过"或"失败" —— 没测过的条目报任何一个结论都是假话。
    #    重放脚本（`replay_golden_path.py`）不经过任务管理器，所以它那里
    #    总数会少一条，那是实情。
    if phases_note:
        # ⚠️ **观测任务自己没跑完 —— 那是"没测成"，不是"不达标"。**
        #    报成一个红的 AC 是"看起来合理的错误"：读的人会去查阶段模型，
        #    而问题在一次 API 抖动上。所以这一条如实说"未能测量"。
        checks.append((
            "AC-36 语义化进度（本次未能测量）",
            False,
            f"观测任务没跑完：{phases_note}。"
            f"**这不代表 AC-36 不达标** —— 阶段模型本身由 "
            f"tests/test_progress.py 守着。重跑一次 `e2e_smoke` 通常就有结果。",
        ))
    elif phases is not None:
        checks.append((
            "AC-36 语义化进度（一次执行中 phase 至少 3 个不同取值）",
            len(phases) >= 3,
            f"实测经历 {len(phases)} 个阶段：{phases}",
        ))

    return checks


def checklist(out: dict, *, expected_rooms: int = 4,
              phases: list[str] | None = None,
              phases_note: str = "") -> tuple[int, int]:
    """
    逐条检查黄金路径的产物。返回 (通过数, 总数)。

    ⚠️ 每一条都对应需求文档里的一个验收项（编号写在描述里），
    这样"20/20"就不是一个自报的数字，而是能指回契约的结论。
    """
    checks = checklist_rows(out, expected_rooms=expected_rooms,
                            phases=phases, phases_note=phases_note)
    for name, ok, detail in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}")
        if not ok:
            print(f"      ↳ {detail}")
    return sum(1 for _, ok, _ in checks if ok), len(checks)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
