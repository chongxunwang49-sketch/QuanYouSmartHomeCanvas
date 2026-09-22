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
        print(f"\n  【{p.get('plan_id')}】{p.get('style')} + {p.get('budget_grade')}")
        print(f"    {p.get('summary', '')[:80]}")
        zone_names = [z.get("room_name") for z in (p.get("zones") or [])]
        print(f"    分区: {zone_names}")
        print(f"    收纳 {len(p.get('storage_plans') or [])} 处，"
              f"动线优化 {len(p.get('circulation_fixes') or [])} 项，"
              f"置信度 {p.get('confidence')}")

        # ⚠️ 这两个字段是真实模型输出上最该看的
        invented = p.get("invented_rooms") or []
        unassigned = p.get("unassigned_rooms") or []
        if invented:
            print(f"    ⚠ 幻觉房间被剔除 {len(invented)} 个: {invented}")
        if unassigned:
            print(f"    ⚠ 未安排房间: {unassigned}")
        if not invented and not unassigned:
            print("    ✓ 房间对齐无误")

    comp = out.get("comparison") or {}
    banner("对比表")
    print(f"  可用: {comp.get('available')}  "
          f"实际/请求: {comp.get('plan_count')}/{comp.get('requested_count')}")
    for note in comp.get("notes") or []:
        print(f"  ⚠ {note}")

    # ── 汇总 ─────────────────────────────────────────────────
    banner("总览")
    branch_ms = [t["elapsed_ms"] for t in out["trace"] if t["agent"] == "A-03"]
    print(f"  墙钟总耗时 : {wall:.1f}s")

    if branch_ms:
        print(f"  A-03 分支  : {branch_ms}  (max {max(branch_ms)}ms / sum {sum(branch_ms)}ms)")

        # ⚠️ 并发判定必须量「fan-out 阶段的时长」，不能拿总墙钟去比。
        # 总墙钟含 A-01 + A-02，把它们算进去会得出"疑似串行"的错误结论。
        # fan-out 阶段 ≈ 总墙钟 − 上游各节点耗时（fan-in 是纯计算，可忽略）。
        upstream_ms = sum(t["elapsed_ms"] for t in out["trace"] if t["agent"] in ("A-01", "A-02"))
        fanout_s = wall - upstream_ms / 1000
        ratio = fanout_s / (max(branch_ms) / 1000) if max(branch_ms) else 0
        # 并发时 ratio ≈ 1.0（略大于 1，含调度开销）；串行时会接近分支数
        verdict = "并发" if ratio < 1.6 else f"疑似串行（{ratio:.1f}× 于单分支）"
        print(f"  fan-out 阶段: {fanout_s:.1f}s  (比值 {ratio:.2f}× 单分支最慢值)")
        print(f"  并发判定   : {verdict}")
    print(f"  phase      : {out.get('phase')}")
    print(f"  degraded   : {out.get('degraded')}")
    print(f"  错误       : {len(out.get('errors') or [])} 条")

    ok = bool(plans) and not out.get("errors")
    print(f"\n  {'✓ 链路通过' if ok else '✗ 链路存在问题'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
