"""
语料切块 —— 由验证脚本与入库脚本**共用**。

为什么要单独抽一个模块：切块参数是检索质量的**首要变量**，
而它同时被"离线验证"和"正式入库"两条路径使用。两处各写一套的话，
验证时看到的 chunk 数和实际上库的 chunk 数会对不上 ——
那种不一致极难发现，因为两边都"跑通了"。

═══════════════════════════════════════════════════════════════════
两级切块，缺一不可
═══════════════════════════════════════════════════════════════════
只用 `MarkdownHeaderTextSplitter`：遇到一个 30KB 的大章节会切出一整块超长文本，
检索时它会盖过其它所有结果（相似度被长文本里的某个词撑起来）。

只用 `RecursiveCharacterTextSplitter`：会把标题切碎，丢掉层级信息，
于是"这条规则属于哪一节"就无从得知 —— 而 A-06 引用时要带上这个上下文。

所以先按标题切（保结构），再对超长的按长度切（保粒度）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

#: 单条 chunk 的合理长度区间（字符）。
#: 太短是碎片（"以上都不含"这种半句话无法独立检索）；
#: 太长会稀释检索精度。80-1200 是实测调出来的。
MIN_CHUNK_CHARS = 80
MAX_CHUNK_CHARS = 1200

SIZE_CHUNK = 500
SIZE_OVERLAP = 60


class ManifestError(RuntimeError):
    """语料清单缺失或格式不对。"""


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ManifestError(f"语料清单不存在: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "ingest" not in data:
        raise ManifestError(f"语料清单格式不对（缺 ingest）: {path}")
    return data


def corpus_root_of(manifest: dict[str, Any]) -> Path:
    """从清单的 sources[0].local_path 推出语料根目录。"""
    sources = manifest.get("sources") or []
    if not sources or not sources[0].get("local_path"):
        raise ManifestError("清单里没有 sources[].local_path")
    return Path(sources[0]["local_path"]).parent


def collect_files(manifest: dict[str, Any], corpus_root: Path) -> tuple[list[dict], list[str]]:
    """
    按白名单展开成文件列表，附带该文件应有的元数据。

    元数据**在这里注入**，而不是写进上游文件 ——
    上游文件保持原样才能 diff 上游更新（见清单顶部纪律 1）。
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
                "tags": list(entry.get("tags") or []),
                "source": rel.split("/")[0],
                "priority": entry.get("priority", "support"),
            })

    # 排除项一并校验存在性 —— 名字写错时能立刻发现，
    # 否则"排除了 5 个目录"这句话可能排的是 5 个不存在的名字
    for entry in manifest.get("exclude", []):
        rel = entry.get("path", "")
        if "*" in rel or not rel:
            continue
        if not (corpus_root / rel).exists():
            problems.append(f"排除目录不存在（清单可能写错）: {rel}")

    return files, problems


def build_splitters():
    from langchain_text_splitters import (
        MarkdownHeaderTextSplitter,
        RecursiveCharacterTextSplitter,
    )

    by_header = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")],
        strip_headers=False,      # 标题留在正文里，检索时才看得见语境
    )
    by_size = RecursiveCharacterTextSplitter(
        chunk_size=SIZE_CHUNK,
        chunk_overlap=SIZE_OVERLAP,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    )
    return by_header, by_size


def _headings_of(text: str) -> list[str]:
    """
    抽出 chunk 里的标题行，作为可读的"章节路径"。

    A-06 引用来源时要能说清"这条规则出自哪一节"，
    只给文件名是不够定位的。
    """
    out: list[str] = []
    for line in text.splitlines()[:6]:
        s = line.strip()
        if s.startswith("#"):
            title = s.lstrip("#").strip()
            if title and title not in out:
                out.append(title)
    return out


def chunk_files(files: list[dict]) -> list[dict[str, Any]]:
    """把文件列表切成 chunk，每条带上溯源元数据。"""
    by_header, by_size = build_splitters()
    chunks: list[dict[str, Any]] = []

    for entry in files:
        try:
            text = entry["path"].read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not text.strip():
            continue

        try:
            parts = by_header.split_text(text)
        except Exception:  # noqa: BLE001 —— 单个文件解析失败不该中断整批
            parts = []

        for part in parts:
            body = part.page_content if hasattr(part, "page_content") else str(part)
            pieces = by_size.split_text(body) if len(body) > MAX_CHUNK_CHARS else [body]
            for piece in pieces:
                piece = piece.strip()
                if not (MIN_CHUNK_CHARS <= len(piece)):
                    continue
                chunks.append({
                    "text": piece,
                    "doc_type": entry["doc_type"],
                    "tags": entry["tags"],
                    # 标题路径 → 溯源时可读到"出自哪一节"
                    "headings": " / ".join(_headings_of(piece)),
                    "source": entry["rel"],
                    "source_dir": entry["source"],
                    "priority": entry["priority"],
                })
    return chunks


__all__ = [
    "ManifestError", "load_manifest", "corpus_root_of", "collect_files",
    "build_splitters", "chunk_files",
    "MIN_CHUNK_CHARS", "MAX_CHUNK_CHARS", "SIZE_CHUNK", "SIZE_OVERLAP",
]
