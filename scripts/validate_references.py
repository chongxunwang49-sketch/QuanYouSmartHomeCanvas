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

import yaml  # noqa: E402

#: 通过标准。低于这些值说明语料没准备好，不该入库。
MIN_FILES = 10
MIN_CHUNKS = 100
#: 单条 chunk 的合理长度区间（字符）。太短是碎片，太长会稀释检索精度。
MIN_CHUNK_CHARS = 80
MAX_CHUNK_CHARS = 1200


def load_manifest(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"✗ 清单不存在: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "ingest" not in data:
        raise SystemExit(f"✗ 清单格式不对（缺 ingest）: {path}")
    return data


def resolve_corpus_root(manifest: dict, override: str | None) -> Path:
    if override:
        return Path(override)
    sources = manifest.get("sources") or []
    if sources and sources[0].get("local_path"):
        return Path(sources[0]["local_path"]).parent
    raise SystemExit("✗ 清单里没有 sources[].local_path，请用 --root 指定语料根目录")


def collect_files(manifest: dict, corpus_root: Path) -> tuple[list[dict], list[str]]:
    """
    按白名单展开成文件列表。

    返回 (文件条目, 问题列表)。条目里带上 doc_type/tags ——
    这些元数据**在切块时注入**，而不是写进上游文件（见清单顶部纪律 1）。
    """
    files: list[dict] = []
    problems: list[str] = []

    for entry in manifest["ingest"]:
        rel = entry["path"]
        target = corpus_root / rel
        if not target.exists():
            problems.append(f"入库目录不存在: {target}")
            continue
        found = sorted(target.rglob("*.md"))
        if not found:
            problems.append(f"入库目录里没有 md: {target}")
            continue
        for f in found:
            files.append({
                "path": f,
                "rel": str(f.relative_to(corpus_root)).replace("\\", "/"),
                "doc_type": entry.get("doc_type", "avoid_pit"),
                "tags": entry.get("tags") or [],
                "source": rel.split("/")[0],
                "priority": entry.get("priority", "support"),
            })

    # 排除项也校验一下存在性 —— 名字写错时能立刻发现，
    # 否则"排除了 5 个目录"这句话可能是排除了 5 个不存在的名字
    for entry in manifest.get("exclude", []):
        rel = entry["path"]
        if "*" in rel:
            continue
        if not (corpus_root / rel).exists():
            problems.append(f"排除目录不存在（清单可能写错）: {rel}")

    return files, problems


def build_splitter():
    """
    两级切块：先按标题切（保住结构），再对超长的按长度切。

    只用 MarkdownHeaderTextSplitter 的话，遇到一个 30KB 的大章节会切出
    一整块超长文本 —— 检索时它会盖过其它所有结果。
    只用 RecursiveCharacterTextSplitter 则会切碎标题，丢掉层级信息。
    """
    from langchain_text_splitters import (
        MarkdownHeaderTextSplitter,
        RecursiveCharacterTextSplitter,
    )

    by_header = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")],
        strip_headers=False,          # 标题要留在正文里，检索时才看得见语境
    )
    by_size = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=60,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    )
    return by_header, by_size


def chunk_files(files: list[dict]) -> list[dict]:
    by_header, by_size = build_splitter()
    chunks: list[dict] = []

    for entry in files:
        try:
            text = entry["path"].read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if not text.strip():
            continue

        try:
            parts = by_header.split_text(text)
        except Exception:
            parts = []

        for part in parts:
            body = part.page_content if hasattr(part, "page_content") else str(part)
            if len(body) > MAX_CHUNK_CHARS:
                pieces = by_size.split_text(body)
            else:
                pieces = [body]
            for piece in pieces:
                piece = piece.strip()
                if len(piece) < MIN_CHUNK_CHARS:
                    continue
                chunks.append({
                    "text": piece,
                    "doc_type": entry["doc_type"],
                    "tags": entry["tags"],
                    "source": entry["rel"],
                    "priority": entry["priority"],
                })
    return chunks


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
