"""
参考语料离线验证 —— **入 Chroma 之前的最后一道关**。

═══════════════════════════════════════════════════════════════════
为什么要有这一步
═══════════════════════════════════════════════════════════════════
把没验过的语料灌进向量库，代价不是"跑不起来"，是**跑起来了但检索质量差**——
而那种问题极难定位：你得先怀疑切块、再怀疑 embedding、再怀疑 prompt，
最后才发现是某几个文件编码不对或通篇是目录。

更麻烦的是它**不报错**。一个 5000 条低质量语料的库，和一个 200 条高质量规则的库，
在"能不能返回结果"这件事上表现一样；差别只在最终答案好不好，
而那时已经很难回头归因了。

所以这一步做四件事，**全部离线，不入库**：
    ① 按清单扫描语料，确认文件数与体积符合预期
    ② 校验排除目录确实存在（清单写错了名字要能发现）
    ③ 试切块，打印 chunk 数与抽样，**肉眼确认是完整语义而非半句话**
    ④ 试向量化前 N 条，确认返回维度是 1024 而非 768/1536

运行：
    python scripts/validate_references.py              # 含向量化验证
    python scripts/validate_references.py --no-embed   # 只做离线部分（Ollama 没开时）
    python scripts/validate_references.py --sample 5   # 抽样打印条数
"""

from __future__ import annotations

import argparse
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

from backend.app.services.knowledge import chunking  # noqa: E402

#: 通过标准。低于这些值说明语料没准备好，不该入库。
MIN_FILES = 10
MIN_CHUNKS = 100
MIN_CHUNK_CHARS = chunking.MIN_CHUNK_CHARS
MAX_CHUNK_CHARS = chunking.MAX_CHUNK_CHARS


def load_manifest(path: Path) -> dict:
    try:
        return chunking.load_manifest(path)
    except chunking.ManifestError as e:
        raise SystemExit(f"✗ {e}") from e


def resolve_corpus_root(manifest: dict, override: str | None) -> Path:
    if override:
        return Path(override)
    try:
        return chunking.corpus_root_of(manifest)
    except chunking.ManifestError as e:
        raise SystemExit(f"✗ {e}（可用 --root 指定语料根目录）") from e


# 切块逻辑统一在 backend/app/services/knowledge/chunking.py ——
# 验证脚本与入库脚本**必须用同一套**，否则"验证时 419 条、实际入库 500 条"
# 这种不一致极难发现（两边都跑通了）。
collect_files = chunking.collect_files
chunk_files = chunking.chunk_files


def verify_embeddings(chunks: list[dict], sample: int) -> tuple[bool, str]:
    """调 Ollama 验一次向量维度。**只验前几条，不批量入库。**"""
    import httpx

    from backend.app.core.config import settings

    picked = [c["text"][:400] for c in chunks[:sample]]
    if not picked:
        return False, "没有可用的 chunk"

    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/embed"
    payload = {"model": settings.OLLAMA_EMBEDDING_MODEL, "input": picked}

    try:
        started = time.perf_counter()
        resp = httpx.post(url, json=payload, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        return False, f"调用失败（{type(e).__name__}: {e}）—— Ollama 开了吗？模型拉了吗？"

    vecs = data.get("embeddings") or []
    if not vecs:
        return False, "返回里没有 embeddings 字段"

    dim = len(vecs[0])
    elapsed = time.perf_counter() - started
    expected = settings.OLLAMA_EMBEDDING_DIM

    if dim != expected:
        return False, f"维度是 {dim}，期望 {expected} —— 模型不对"

    # 顺带确认向量不是全零（模型加载异常时会出现）
    if all(abs(x) < 1e-9 for x in vecs[0][:50]):
        return False, "向量前 50 维全为 0，模型可能没正常加载"

    return True, f"{len(vecs)} 条 / {dim} 维 / {elapsed:.1f}s（{elapsed/len(vecs)*1000:.0f}ms 每条）"


def main() -> int:
    ap = argparse.ArgumentParser(description="参考语料离线验证")
    ap.add_argument("--manifest", default=str(ROOT / "seed_data" / "references_manifest.yaml"))
    ap.add_argument("--root", default=None, help="语料根目录（默认取清单里的 local_path）")
    ap.add_argument("--no-embed", action="store_true", help="跳过向量化验证")
    ap.add_argument("--sample", type=int, default=3, help="抽样打印条数")
    args = ap.parse_args()

    manifest = load_manifest(Path(args.manifest))
    corpus_root = resolve_corpus_root(manifest, args.root)

    print("═" * 66)
    print("  参考语料离线验证")
    print("═" * 66)

    src = (manifest.get("sources") or [{}])[0]
    print(f"\n【来源】{src.get('repo', '?')}")
    print(f"  commit  {src.get('commit', '?')[:12]}  ({src.get('commit_date', '?')})")
    print(f"  license {src.get('license', '?')}")
    print(f"  体积    {src.get('size_md', '?')}")
    print(f"  本地    {corpus_root}")

    # ── ① 扫描 ──────────────────────────────────────────
    files, problems = collect_files(manifest, corpus_root)
    total_kb = sum(f["path"].stat().st_size for f in files) / 1024

    print(f"\n【① 扫描】入库 {len(files)} 个文件 / {total_kb:.0f} KB")
    by_dir: dict[str, int] = {}
    for f in files:
        by_dir[f["source"]] = by_dir.get(f["source"], 0) + 1
    for d, n in sorted(by_dir.items()):
        print(f"    {d:<34} {n:>3} 个")

    excluded = [e for e in manifest.get("exclude", []) if "*" not in e["path"]]
    print(f"  已排除 {len(excluded)} 个目录（均有理由，见清单 exclude 段）")

    if problems:
        print("\n  ⚠ 问题:")
        for p in problems:
            print(f"    - {p}")

    # ── ② 切块 ──────────────────────────────────────────
    print("\n【② 切块】")
    chunks = chunk_files(files)
    if not chunks:
        print("    ✗ 没有切出任何 chunk")
        return 1

    lens = [len(c["text"]) for c in chunks]
    too_short = sum(1 for n in lens if n < MIN_CHUNK_CHARS)
    too_long = sum(1 for n in lens if n > MAX_CHUNK_CHARS)
    print(f"    chunk 数 {len(chunks)}")
    print(f"    长度   最短 {min(lens)} / 平均 {sum(lens)//len(lens)} / 最长 {max(lens)} 字符")
    if too_short or too_long:
        print(f"    ⚠ 超出建议区间: 过短 {too_short} 条 / 过长 {too_long} 条")

    print(f"\n    抽样（前 {args.sample} 条，**请肉眼确认是完整语义而非半句话**）:")
    for c in chunks[:args.sample]:
        head = c["text"].replace("\n", " ")[:110]
        print(f"      [{c['doc_type']}] {c['source']}")
        print(f"        {head}…")

    # ── ③ 向量化 ────────────────────────────────────────
    embed_ok = True
    if args.no_embed:
        print("\n【③ 向量化】已跳过（--no-embed）")
    else:
        print("\n【③ 向量化】")
        embed_ok, msg = verify_embeddings(chunks, args.sample)
        print(f"    {'✓' if embed_ok else '✗'} {msg}")

    # ── ④ 汇总 ──────────────────────────────────────────
    print("\n【④ 汇总】")
    checks = [
        (f"文件数 ≥ {MIN_FILES}", len(files) >= MIN_FILES, len(files)),
        (f"chunk 数 ≥ {MIN_CHUNKS}", len(chunks) >= MIN_CHUNKS, len(chunks)),
        ("无清单问题", not problems, len(problems)),
    ]
    if not args.no_embed:
        checks.append(("向量维度正确", embed_ok, "—"))

    ok = True
    for name, passed, value in checks:
        print(f"    {'✓' if passed else '✗'} {name:<18} {value}")
        ok = ok and passed

    if ok:
        print(f"\n    ✓ 通过 —— 可以入库（{len(chunks)} 条 chunk）")
    else:
        print("\n    ✗ 未通过 —— **先别入库**，按上面的问题逐条处理")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
