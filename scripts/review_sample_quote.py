"""
A-06 避坑审查的验收脚本 —— 对着**带标注的样本报价单**算召回率。

    python scripts/review_sample_quote.py

═══════════════════════════════════════════════════════════════════
为什么必须有这个脚本
═══════════════════════════════════════════════════════════════════
AC-06 的原文是「识别 ≥ 5 类风险，引用知识库来源」。这条指标有个陷阱：
**它只要求"识别"，不要求"识别对"** —— 一个把报价单里所有条目都报成风险的
实现，也能轻松"识别 10 类风险"。

没有带标注的样本，你永远不知道模型是识别了 5 条还是编了 5 条。
`seed_data/sample_quotes/quote_01_economy.md` 里埋了 14 条已知问题，
`.expected.json` 是答案。这个脚本把两者对上，算出**可审计的**召回率 ——
"算不算命中"由 `match_keywords` 决定，不是主观判断。

真出网：需要 DEEPSEEK_API_KEY 与本机 Ollama（embedding）。
"""

from __future__ import annotations

import asyncio
import json
import sys
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

QUOTE_DIR = ROOT / "seed_data" / "sample_quotes"


def banner(t: str) -> None:
    print(f"\n{'═' * 68}\n  {t}\n{'═' * 68}")


def match_planted(planted: dict, findings: list[dict]) -> dict | None:
    """
    在模型的输出里找这条埋点。

    判据是 `match_keywords`：风险项的 title + where + detail 里
    出现**任一**关键词即算命中。用关键词而不是语义相似度，
    是为了让"算不算命中"可审计 —— 否则召回率本身就没有说服力。
    """
    haystack = " ".join(
        f"{f.get('title', '')} {f.get('where', '')} {f.get('detail', '')} {f.get('suggestion', '')}"
        for f in findings
    )
    for kw in planted.get("match_keywords") or []:
        if kw in haystack:
            # 找出具体是哪条命中的，便于人工复核
            for f in findings:
                blob = f"{f.get('title', '')} {f.get('where', '')} {f.get('detail', '')}"
                if kw in blob:
                    return f
            return {}
    return None


async def main() -> int:
    from backend.app.core.config import settings
    from backend.app.agents.risk_reviewer import RiskReviewAgent
    from backend.app.graph.state import initial_state
    from backend.app.services.knowledge import store

    quote_file = QUOTE_DIR / "quote_01_economy.md"
    expected_file = QUOTE_DIR / "quote_01_economy.expected.json"

    if not quote_file.exists() or not expected_file.exists():
        print(f"✗ 样本不存在：{quote_file}")
        return 2

    quote = quote_file.read_text(encoding="utf-8")
    expected = json.loads(expected_file.read_text(encoding="utf-8"))
    planted = expected["planted"]

    banner("环境检查")
    print(f"  DeepSeek key : {'已配置' if settings.DEEPSEEK_API_KEY else '✗ 未配置'}")
    info = store.collection_info()
    print(f"  知识库       : {info.name}  {info.count} 条"
          f"{'' if info.available else f'  ✗ {info.reason}'}")
    if not info.available or info.count == 0:
        print("\n  ⚠ 知识库不可用或为空。先跑：python scripts/ingest_knowledge.py")
        print("     （审查仍会执行，但拿不到引用依据，AC-06 的「引用来源」部分无法验证）")

    state = initial_state(
        task_id="a06-acceptance",
        quote_text=quote,
        requirements={"has_children": True, "eco_level": "E0"},
    )

    banner("执行审查（真实 DeepSeek + Chroma 检索）")
    out = await RiskReviewAgent().execute(state)
    r = out.get("risk_review") or {}

    trace = out["trace"][0]
    print(f"  耗时 {trace['elapsed_ms']}ms  ok={trace['ok']}")
    if trace.get("llm"):
        print(f"  模型 {trace['llm'].get('model')}  "
              f"tokens {trace['llm'].get('prompt_tokens')}+{trace['llm'].get('completion_tokens')}")
    print(f"  检索到依据 {r.get('source_count', 0)} 条"
          f"{'（改写重排）' if r.get('reranked') else ''}")

    findings = r.get("findings") or []
    banner(f"审查结果：{len(findings)} 条风险 / {r.get('distinct_type_count', 0)} 类")
    print(f"  整体风险：{r.get('overall_risk')}    置信度 {r.get('confidence')}")
    print(f"  {r.get('summary', '')[:100]}")
    for i, f in enumerate(findings, 1):
        mark = "📎" if f.get("has_source") else "  "
        print(f"\n  {i:>2}. [{f['severity']:<6}] [{f['risk_type']}] {mark} {f.get('title')}")
        print(f"      位置：{f.get('where')}")
        print(f"      {f.get('detail', '')[:88]}")
        if f.get("citations"):
            print(f"      依据：{f['citations'][0]}")

    # ── 对答案 ────────────────────────────────────────────
    banner("对照答案：召回率")
    hits, misses = [], []
    for p in planted:
        m = match_planted(p, findings)
        (hits if m is not None else misses).append(p)

    print(f"  {'':<4}{'埋点':<6}{'类型':<14}{'严重度':<8}说明")
    for p in planted:
        ok = match_planted(p, findings) is not None
        print(f"  {'✓' if ok else '·':<4}{p['id']:<6}{p['risk_type']:<12}"
              f"{p['severity']:<10}{p['category']}")

    recall = len(hits) / len(planted) if planted else 0.0
    hit_types = sorted({p["risk_type"] for p in hits})
    all_types = sorted({p["risk_type"] for p in planted})

    print(f"\n  召回：{len(hits)}/{len(planted)} = {recall:.0%}")
    print(f"  命中类型：{len(hit_types)} 类  {hit_types}")
    print(f"  全部类型：{len(all_types)} 类  {all_types}")
    if misses:
        print(f"  漏判：{'、'.join(p['id'] for p in misses)}")

    # 误报：模型报的风险里，一条埋点都没对上的
    unmatched = [f for f in findings if not any(
        match_planted(p, [f]) is not None for p in planted
    )]
    print(f"  未对应到埋点的风险项（可能是合理补充，也可能是误报）：{len(unmatched)} 条")
    for f in unmatched[:5]:
        print(f"      - {f.get('title')}")

    # ── AC-06 判定 ────────────────────────────────────────
    banner("AC-06 判定")
    th = expected["expected_summary"]["pass_threshold"]
    checks = [
        (f"识别 ≥ {th['distinct_types']} 类风险", len(hit_types) >= th["distinct_types"],
         f"{len(hit_types)} 类"),
        (f"召回率 ≥ {th['recall']:.0%}", recall >= th["recall"], f"{recall:.0%}"),
        ("引用知识库来源", any(f.get("has_source") for f in findings),
         f"{sum(1 for f in findings if f.get('has_source'))}/{len(findings)} 条带依据"),
    ]
    ok_all = True
    for name, passed, value in checks:
        print(f"  {'✓' if passed else '✗'} {name:<22} {value}")
        ok_all = ok_all and passed

    print(f"\n  {'✓ AC-06 通过' if ok_all else '✗ AC-06 未通过'}")
    if r.get("invented_citations"):
        print(f"  ⚠ 模型编造了来源编号：{r['invented_citations']}（已被剔除）")
    if r.get("data_gaps"):
        print("\n  数据缺口：")
        for g in r["data_gaps"]:
            print(f"      - {g}")

    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
