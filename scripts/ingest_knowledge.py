"""
把参考语料切块、向量化、写入 Chroma。

    python scripts/ingest_knowledge.py            # 增量入库（幂等）
    python scripts/ingest_knowledge.py --reset    # 先清空再入库
    python scripts/ingest_knowledge.py --dry-run  # 只切块不写库

**幂等**：chunk id 是内容的 sha1，重复运行是 upsert 覆盖而不是追加，
所以"跑了两遍"不会让同一条规则在检索时出来两次。
换了 embedding 模型才需要 `--reset`（维度变了，旧向量作废）。
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


def main() -> int:
    ap = argparse.ArgumentParser(description="参考语料入库")
    ap.add_argument("--manifest", default=str(ROOT / "seed_data" / "references_manifest.yaml"))
    ap.add_argument("--reset", action="store_true", help="先清空 collection 再入库")
    ap.add_argument("--dry-run", action="store_true", help="只切块，不写库")
    args = ap.parse_args()

    from backend.app.core.config import settings
    from backend.app.services.knowledge import chunking, store

    print("═" * 66)
    print("  参考语料入库")
    print("═" * 66)

    manifest = chunking.load_manifest(Path(args.manifest))
    corpus_root = chunking.corpus_root_of(manifest)

    files, problems = chunking.collect_files(manifest, corpus_root)
    if problems:
        print("\n⚠ 清单问题:")
        for p in problems:
            print(f"    - {p}")
    if not files:
        print("✗ 没有可入库的文件")
        return 1

    chunks = chunking.chunk_files(files)
    print(f"\n【切块】{len(files)} 个文件 → {len(chunks)} 条 chunk")

    if args.dry_run:
        print("\n（--dry-run，未写库）")
        return 0

    print(f"\n【向量化】模型 {settings.OLLAMA_EMBEDDING_MODEL}，"
          f"每批 {store.EMBED_BATCH} 条")

    if args.reset:
        store.reset_collection()
        print("  已清空 collection（--reset）")

    started = time.perf_counter()
    total = len(chunks)
    done = 0
    written = 0

    try:
        for i in range(0, total, store.EMBED_BATCH):
            batch = chunks[i:i + store.EMBED_BATCH]
            vecs = store.embed_texts([c["text"] for c in batch])
            written += store.upsert_chunks(batch, embeddings=vecs)
            done += len(batch)
            pct = done * 100 // total
            bar = "█" * (pct // 4) + "·" * (25 - pct // 4)
            elapsed = time.perf_counter() - started
            eta = (elapsed / done) * (total - done) if done else 0
            print(f"\r  {bar} {pct:>3}%  {done}/{total}  已写 {written}  "
                  f"用时 {elapsed:.0f}s  预计剩余 {eta:.0f}s", end="", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"\n\n✗ 入库中断：{type(e).__name__}: {e}")
        print(f"  已写入 {written} 条。脚本是幂等的，修好问题后重跑即可。")
        return 1

    elapsed = time.perf_counter() - started
    info = store.collection_info()

    print(f"\n\n【完成】写入 {written} 条，用时 {elapsed:.0f}s")
    print(f"  collection : {info.name}")
    print(f"  库内总数   : {info.count}")
    print(f"  持久化目录 : {info.path}")

    if info.count != written and not args.reset:
        print(f"\n  ℹ 库内总数与本次写入数不同是正常的（增量入库，含此前已写入的）")

    print("\n✓ 入库完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
