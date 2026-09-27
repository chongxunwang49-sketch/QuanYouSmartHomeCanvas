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

import asyncio
from concurrent.futures import ThreadPoolExecutor
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

    # ⚠️ **`to_dict` 在本类的下面**（它要带上 `citation`，而 `citation`
    #    是个 property，定义在中间）。这里**不要**再加一个 ——
    #    实测踩过：我加 `from_dict` 时顺手也写了一个 `to_dict`，
    #    于是同一个类里有了两个同名方法，**Python 不报错**，
    #    后面那个（原来的）胜出，我加的那个成了死代码。
    #    一个"看起来有、其实永远不执行"的方法，比没有更难查。
    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KnowledgeChunk":
        """
        `to_dict()` 的逆。`citation` 是派生属性，不从字典里读 —— 它会由
        `source` 与 `headings` 重新拼出来，所以往返之后仍然相等。
        """
        return cls(
            text=str(d.get("text") or ""),
            source=str(d.get("source") or ""),
            headings=str(d.get("headings") or ""),
            doc_type=str(d.get("doc_type") or "avoid_pit"),
            tags=[str(t) for t in (d.get("tags") or [])],
            similarity=float(d.get("similarity") or 0.0),
            rerank_score=d.get("rerank_score"),
        )

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

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RetrievalResult":
        """
        `to_dict()` 的逆。**给"检索先跑、审查后用"这条路径用。**

        ⚠️ 为什么要有它：审查链里检索与调用模型是同一个节点内的两步，
        于是 `/task/status` 只看得到 2 个阶段（`queued` → `reviewing`）——
        而 AC-36 要求一次执行里至少 3 个不同取值（见
        `core/progress.py` 里 `_REVIEW_STEPS` 的说明）。
        把检索拆成独立节点之后，它的结果要**穿过图状态**交给下一个节点，
        而状态是要被 checkpointer 序列化的 —— 所以走 dict。

        ⚠️ 键名与 `to_dict()` 逐字对应。少一个字段（比如 `reranked`）
        不会报错，只会让"本次是否重排过"这一条静默变成 False。
        """
        return cls(
            query=str(d.get("query") or ""),
            chunks=[KnowledgeChunk.from_dict(c) for c in (d.get("chunks") or [])],
            available=bool(d.get("available", True)),
            reason=str(d.get("reason") or ""),
            reranked=bool(d.get("reranked", False)),
        )


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
        # ⚠️ `reason` 会**上屏**（对话页的"知识库不可用：…"、审查页的
        #    数据缺口清单），所以给用户一句短话，异常类名与原文只进日志。
        logger.warning(f"[knowledge] embedding 异常：{type(e).__name__}: {e}")
        return RetrievalResult(query=query, available=False,
                               reason="知识库暂时连不上，本次没有取到参考资料")

    # ── ② 向量检索 ──────────────────────────────────────
    want_rerank = settings.rerank_enabled
    fetch = recall_k or (top_k * 3 if want_rerank else top_k)
    where = {"doc_type": doc_type} if doc_type else None

    try:
        raw = query_vectors(vectors[0], k=fetch, where=where)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[knowledge] 向量检索失败：{type(e).__name__}: {e}")
        return RetrievalResult(query=query, available=False,
                               reason="知识库暂时查不出结果，本次没有取到参考资料")

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
            # ⚠️ 这句会上屏（对话页 / 审查页的数据缺口清单）。
            #    所以不写内部的相似度阈值 —— 用户要知道的是
            #    "查到了东西但没有一条够相关"，而不是阈值是多少。
            reason=(
                f"检索到 {len(chunks)} 条，但相关程度均低于可用下限"
                if chunks else "知识库中没有相关内容"
            ),
        )

    # ── ④ 可选重排 ──────────────────────────────────────
    kept, reranked = _rerank(query, kept, top_k)

    return RetrievalResult(query=query, chunks=kept[:top_k],
                           available=True, reranked=reranked)


def search_many(
    queries: list[str], *, top_k_each: int = 3, doc_type: str | None = None,
    max_total: int | None = None,
) -> RetrievalResult:
    """
    多查询检索后合并去重。**A-06 的主用入口。**

    一次报价审查要覆盖"增项/漏项/单价异常"等多类风险，一条查询召回不全。
    按主题分别查再合并，比把多个主题塞进一句话查效果更好。

    ⚠️ **`max_total` 必须给。** 实测踩过：A-06 会并发 13 个查询（5 个主题 +
    8 个从报价单抽出的分项名），每个查询取 3 条，去重后仍有 **26 条**依据 ——
    是 `top_k` 的 2.6 倍。后果有两个，都很难归因：

      1. 提示词暴涨，模型 24 秒内写不完十几条风险，**直接超时**；
      2. 依据太多会稀释注意力，模型反而更容易漏掉关键项。

    所以合并后要按相似度截断到 `max_total`。
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
    if max_total is not None and len(ordered) > max_total:
        ordered = ordered[:max_total]

    return RetrievalResult(
        query=" | ".join(queries),
        chunks=ordered,
        available=available,
        reason="；".join(dict.fromkeys(reasons)) if reasons else "",
    )


# ══════════════════════════════════════════════════════════════════
# 异步入口：**不许在事件循环里做检索**
# ══════════════════════════════════════════════════════════════════
#
# 上面那两个函数是**同步阻塞**的：`embed_texts` 走同步 httpx 打 Ollama
# （实测单条 636ms），`query_vectors` 走 ChromaDB 的同步客户端
# （实测单次查询 844ms）。而 A-06 是在 async 节点里、用 `asyncio.gather`
# 对三套方案并发调它们的。
#
# 实测后果（2026-09-24，方案生成链第三次采样）：`review_risks` 一开始，
# **整个事件循环被卡住约 28–30 秒** —— 证据是那个窗口里轮询接口完全
# 没有响应（前后两条轮询记录之间凭空少了一次），而 `/task/{id}/status`
# 是纯内存查询。日志里 A-06 的三次 LLM 调用也因此错开 8 秒才发出。
#
# 连带后果直接打在用户可见的地方：**"方案先交付"晚到了 28 秒**
# （本该在第 54 秒交付，实际第 82 秒）—— 因为负责交付它的那段代码
# 也在同一个被卡住的循环上。
#
# ⚠️ **池子必须是单线程的。**
#
# 第一版想当然地写成 `asyncio.to_thread`（默认多线程），实测**直接坏掉** ——
# 三路并发同时碰 Chroma，三路全部失败：
#     ValueError: Could not connect to tenant default_tenant. Are you sure it exists?
#     AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'
#     KeyError: 'E:\quanyou\data\chroma'
# 而检索层的设计是"失败即返回空结果 + 原因"，所以**它不会报错，只会静默地
# 拿不到依据** —— A-06 于是退化成"凭常识审"，界面上看不出任何异常。
# 这正是本项目最防的那种失败。
#
# 单线程池把 Chroma 的访问收敛回"永远只有一个线程"，实测三路并发
# 全部正常（10.6s 跑完，事件循环心跳稳定在 0.25–0.27s，最大间隔 0.27s）。

_POOL: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qy-rag")
    return _POOL


async def search_many_async(
    queries: list[str], *, top_k_each: int = 3, doc_type: str | None = None,
    max_total: int | None = None,
) -> RetrievalResult:
    """
    `search_many` 的非阻塞版本。**async 调用方一律用这个。**

    语义与 `search_many` 完全一致 —— 同一个函数丢到单线程池里跑，
    唯一的区别是事件循环期间不会被阻塞。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _pool(),
        lambda: search_many(
            queries, top_k_each=top_k_each, doc_type=doc_type, max_total=max_total
        ),
    )


def shutdown_retrieval_pool() -> None:
    """
    停机时收掉检索线程池（AC-31）。

    ⚠️ **`cancel_futures=True` 是有意的。**

    池线程是**非 daemon** 的（`ThreadPoolExecutor` 一贯如此），解释器退出时
    `concurrent.futures` 会 join 它们 —— 而一次检索的超时上限是
    `EMBED_TIMEOUT=180s`，比 AC-31 定的 150 秒停机上限还长。

    ⚠️ **诚实地说清它做到了多少**：`cancel_futures=True` 能丢掉**排队中**的
    那些（并发三套方案时最多丢掉两套），但**正在跑的那一个丢不掉** ——
    Python 没有办法安全地打断一个卡在同步 IO 里的线程。所以最坏情况仍然要等
    它在飞的这一次检索自然结束（实测一次检索 3.5 秒量级；180 秒只是超时上限，
    不是常态）。这一点不能靠这段代码解决，只能靠把 `EMBED_TIMEOUT` 调到
    与停机预算相容，那是另一个决定。
    """
    global _POOL
    if _POOL is None:
        return
    _POOL.shutdown(wait=False, cancel_futures=True)
    _POOL = None


__all__ = [
    "KnowledgeChunk", "RetrievalResult", "search", "search_many",
    "search_many_async", "shutdown_retrieval_pool",
    "DEFAULT_TOP_K", "DEFAULT_MIN_SIMILARITY",
]
