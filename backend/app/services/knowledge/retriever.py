"""
知识库检索 —— A-06 避坑审查的依据来源。

═══════════════════════════════════════════════════════════════════
本模块最重要的一条：**检索失败必须可表达，而不是抛异常**
═══════════════════════════════════════════════════════════════════

`search()` **永远不抛异常**。知识库不可用时它返回一个空结果 + 原因，
由 A-06 决定怎么处理 —— 而 A-06 的正确处理方式是：

    如实说"本次未取得知识库依据"，**而不是凭常识编几条规则出来**。

这与 A-02 的 `insufficient_data` 是同一条纪律：**一个编造的"依据"
比"没有依据"糟糕得多** —— AC-06 明确要求"引用知识库来源"，
如果来源是编的，那条引用比没有引用更误导人（用户会以为是查证过的）。

所以检索层的职责是：能查到就给带出处的 chunk，查不到就诚实地说查不到。

═══════════════════════════════════════════════════════════════════
关于 rerank
═══════════════════════════════════════════════════════════════════
`RERANK_URL` 默认为空（即关闭）。本机已有 `my-rerank:9999`（bge-reranker-v2-m3，
按需启停，见 ADR-06），但**其请求格式尚未在本项目中验证过** ——
所以启用路径写成了容错的（任何失败都退回向量排序并告警），
且在文档里标明"启用前需先确认服务契约"。
不假装一段没验证过的代码是能用的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

from ...core.config import settings
from .store import (
    KnowledgeUnavailableError,
    embed_texts,
    query_vectors,
    tags_from_str,
)

#: 默认召回条数。A-06 一次审查要覆盖多类风险，8 条够铺开又不至于塞满上下文。
DEFAULT_TOP_K = 8

#: 相似度下限。低于它的结果宁可不给 —— 硬塞进来的弱相关 chunk
#: 会让模型"看出"一个不存在的风险模式。
DEFAULT_MIN_SIMILARITY = 0.35


@dataclass
class KnowledgeChunk:
    """一条可引用的知识。`citation` 是给用户看的出处。"""

    text: str
    source: str                 # 相对路径，如 zhuangxiu-skills/装修报价审核/references/xxx.md
    headings: str = ""          # 章节路径
    doc_type: str = "avoid_pit"
    tags: list[str] = field(default_factory=list)
    similarity: float = 0.0
    rerank_score: float | None = None

    @property
    def citation(self) -> str:
        """
        人类可读的出处。

        形如「装修报价审核/references/单价陷阱与隐形消费.md § 二、11项隐形消费」——
        AC-06 要求"引用知识库来源"，只给文件名不够定位到具体条款。
        """
        short = self.source
        for prefix in ("zhuangxiu-skills/", "national_standards/", "report_rules/"):
            if short.startswith(prefix):
                short = short[len(prefix):]
                break
        short = short.replace("/references/", " / ").removesuffix(".md")
        return f"{short} § {self.headings}" if self.headings else short

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source": self.source,
            "headings": self.headings,
            "doc_type": self.doc_type,
            "tags": self.tags,
            "similarity": self.similarity,
            "rerank_score": self.rerank_score,
            "citation": self.citation,
        }


@dataclass
class RetrievalResult:
    """一次检索的结果。**带 available 与 reason**，让调用方能区分"查不到"和"库挂了"。"""

    query: str
    chunks: list[KnowledgeChunk] = field(default_factory=list)
    available: bool = True
    reason: str = ""
    reranked: bool = False

    def __bool__(self) -> bool:
        return bool(self.chunks)

    @property
    def citations(self) -> list[str]:
        return [c.citation for c in self.chunks]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "available": self.available,
            "reason": self.reason,
            "reranked": self.reranked,
            "count": len(self.chunks),
            "citations": self.citations,
            "chunks": [c.to_dict() for c in self.chunks],
        }


# ══════════════════════════════════════════════════════════════════
# Rerank（默认关闭）
# ══════════════════════════════════════════════════════════════════


def _rerank(query: str, chunks: list[KnowledgeChunk], top_k: int) -> tuple[list[KnowledgeChunk], bool]:
    """
    用 bge-reranker 重排。**任何失败都退回向量排序**，不抛。

    ⚠️ 本机 my-rerank:9999 的确切请求/响应格式**尚未在本项目中验证**。
    这里按 text-embeddings-inference 的常见契约写（`{query, documents}` →
    `{results:[{index, relevance_score}]}`），并做了宽容解析。
    启用前请先用 curl 确认该服务的真实契约，再改这里。
    """
    if not settings.rerank_enabled or not chunks:
        return chunks, False

    url = settings.RERANK_URL.strip()
    try:
        with httpx.Client(timeout=settings.RERANK_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json={
                "query": query,
                "documents": [c.text for c in chunks],
            })
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001 —— rerank 是锦上添花，不该拖垮检索
        logger.warning(f"rerank 调用失败（{type(e).__name__}: {e}），退回向量排序")
        return chunks, False

    results = data.get("results")
    if not isinstance(results, list) or not results:
        logger.warning("rerank 返回格式不符合预期，退回向量排序")
        return chunks, False

    try:
        ordered = sorted(results, key=lambda r: -float(r.get("relevance_score", 0)))
        out: list[KnowledgeChunk] = []
        for r in ordered[:top_k]:
            idx = int(r.get("index", -1))
            if 0 <= idx < len(chunks):
                c = chunks[idx]
                c.rerank_score = float(r.get("relevance_score", 0))
                out.append(c)
        return (out or chunks), bool(out)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"rerank 结果解析失败（{type(e).__name__}: {e}），退回向量排序")
        return chunks, False


# ══════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════


def search(
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    doc_type: str | None = None,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    recall_k: int | None = None,
) -> RetrievalResult:
    """
    检索知识库。**永远不抛异常**（见模块说明）。

    Args:
        query: 查询文本。A-06 会把"待审查的报价项描述"或风险主题作为查询。
        top_k: 返回条数。
        doc_type: 只检索某类语料（如 regulation）。None 表示不限。
        min_similarity: 相似度下限，低于它的丢弃。
        recall_k: 向量召回条数。启用 rerank 时通常设得比 top_k 大（先粗召再精排）。

    Returns:
        RetrievalResult。`available=False` 表示库不可用，`chunks=[]` 表示查不到。
    """
    query = (query or "").strip()
    if not query:
        return RetrievalResult(query=query, available=True, reason="查询为空")

    # ── ① 向量化查询 ────────────────────────────────────
    try:
        vectors = embed_texts([query])
    except KnowledgeUnavailableError as e:
        logger.warning(f"[knowledge] embedding 不可用：{e}")
        return RetrievalResult(query=query, available=False, reason=str(e))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[knowledge] embedding 异常：{type(e).__name__}: {e}")
        return RetrievalResult(query=query, available=False,
                               reason=f"{type(e).__name__}: {e}")

    # ── ② 向量检索 ──────────────────────────────────────
    want_rerank = settings.rerank_enabled
    fetch = recall_k or (top_k * 3 if want_rerank else top_k)
    where = {"doc_type": doc_type} if doc_type else None

    try:
        raw = query_vectors(vectors[0], k=fetch, where=where)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[knowledge] 向量检索失败：{type(e).__name__}: {e}")
        return RetrievalResult(query=query, available=False,
                               reason=f"向量检索失败：{type(e).__name__}: {e}")

    # ── ③ 组装 + 过滤 ───────────────────────────────────
    chunks = [
        KnowledgeChunk(
            text=r["text"],
            source=(r["metadata"] or {}).get("source", ""),
            headings=(r["metadata"] or {}).get("headings", ""),
            doc_type=(r["metadata"] or {}).get("doc_type", "avoid_pit"),
            tags=tags_from_str((r["metadata"] or {}).get("tags")),
            similarity=r.get("similarity", 0.0),
        )
        for r in raw
    ]
    kept = [c for c in chunks if c.similarity >= min_similarity]

    if not kept:
        return RetrievalResult(
            query=query, chunks=[], available=True,
            reason=(
                f"检索到 {len(chunks)} 条但相似度均低于 {min_similarity}"
                if chunks else "知识库中没有相关内容"
            ),
        )

    # ── ④ 可选重排 ──────────────────────────────────────
    kept, reranked = _rerank(query, kept, top_k)

    return RetrievalResult(query=query, chunks=kept[:top_k],
                           available=True, reranked=reranked)


def search_many(
    queries: list[str], *, top_k_each: int = 3, doc_type: str | None = None,
) -> RetrievalResult:
    """
    多查询检索后合并去重。**A-06 的主用入口。**

    一次报价审查要覆盖"增项/漏项/单价异常"等多类风险，一条查询召回不全。
    按主题分别查再合并，比把多个主题塞进一句话查效果更好。
    """
    merged: dict[str, KnowledgeChunk] = {}
    reasons: list[str] = []
    available = True

    for q in queries:
        r = search(q, top_k=top_k_each, doc_type=doc_type)
        if not r.available:
            available = False
            reasons.append(r.reason)
        for c in r.chunks:
            key = f"{c.source}::{c.text[:60]}"
            # 同一条被多个查询命中时保留相似度更高的那次
            if key not in merged or c.similarity > merged[key].similarity:
                merged[key] = c

    ordered = sorted(merged.values(), key=lambda c: -c.similarity)
    return RetrievalResult(
        query=" | ".join(queries),
        chunks=ordered,
        available=available,
        reason="；".join(dict.fromkeys(reasons)) if reasons else "",
    )


__all__ = [
    "KnowledgeChunk", "RetrievalResult", "search", "search_many",
    "DEFAULT_TOP_K", "DEFAULT_MIN_SIMILARITY",
]
