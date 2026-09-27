"""
向量库存取 —— Chroma（嵌入式）＋ Ollama embedding。

═══════════════════════════════════════════════════════════════════
两个必须显式处理的坑（都不是"跑不起来"，是"跑起来但是错的"）
═══════════════════════════════════════════════════════════════════

【坑 1】Chroma 的 metadata **只支持 str / int / float / bool，不支持列表**。
    我们的 chunk 带 `tags: [报价审核, 增项]`，直接塞进去会抛
    `ValueError: Expected metadata value to be a str, int, float or bool`。
    所以写入时 join 成字符串，读出来再 split 还原（见 tags_to_str / tags_from_str）。
    这个坑的麻烦之处在于：它只在**写**的时候炸，等你发现时可能已经写了一半数据。

【坑 2】不给 `embeddings=` 参数的话，Chroma 会**自己去下载**一个默认的
    ONNX 模型（all-MiniLM-L6-v2，384 维）来算向量。
    本机 huggingface.co 直连不通（见 ADR-06：模型与数据集统一落盘 E 盘、
    下载走镜像源），那一步会卡很久然后失败；
    更糟的是它**可能成功**，然后你就得到了一个 384 维的库，
    而查询用的是 Ollama 的 1024 维 —— 维度不匹配才发现。
    所以本模块**一律显式传 embeddings**，从不依赖 Chroma 的默认向量函数。

【为什么用余弦距离】bge 系列是在归一化向量上训练的，用 L2 距离会把
"向量长度"混进相似度里。collection 建的时候就要定下来（建完改不了）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx
from loguru import logger

from ...core.config import settings
from .chunking import ManifestError  # noqa: F401 —— 对外统一从这里取

#: 一次请求塞多少条去算向量。实测单条约 600ms，批量能显著摊薄开销。
EMBED_BATCH = 16
#: 单条 embedding 请求的超时（秒）。整批算，所以要给够。
EMBED_TIMEOUT = 180.0


class KnowledgeUnavailableError(RuntimeError):
    """
    向量库或 embedding 服务不可用。

    注意本异常**在检索路径上是被吞掉的**（见 retriever.search）——
    知识库挂了不该让 A-06 整个失败，只是拿不到引用依据。
    这里保留异常类型是为了入库脚本能明确报错。
    """


def tags_to_str(tags: Sequence[str]) -> str:
    """列表 → 字符串。Chroma 不接受列表值（见模块头「坑 1」）。"""
    return "|".join(t for t in tags if t)


def tags_from_str(value: Any) -> list[str]:
    """字符串 → 列表。读回来的统一入口，避免各处各写一遍 split。"""
    if not value:
        return []
    return [t for t in str(value).split("|") if t]


def chunk_id(source: str, text: str) -> str:
    """
    确定性 id：同一条内容永远得到同一个 id。

    这样重复入库是幂等的（Chroma 的 upsert 会覆盖而不是追加），
    不会出现"跑了两遍入库，检索时同一条规则出来两次"。
    """
    h = hashlib.sha1()
    h.update(source.encode("utf-8"))
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


# ══════════════════════════════════════════════════════════════════
# Embedding（Ollama）
# ══════════════════════════════════════════════════════════════════


def embed_texts(texts: Sequence[str], *, timeout: float = EMBED_TIMEOUT) -> list[list[float]]:
    """
    调 Ollama 算向量。**失败即抛**（调用方决定怎么处理）。

    Ollama 原生 `/api/embed` 支持一次传多条，比逐条调快得多。
    """
    if not texts:
        return []

    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/embed"
    out: list[list[float]] = []

    with httpx.Client(timeout=timeout) as client:
        for i in range(0, len(texts), EMBED_BATCH):
            batch = list(texts[i:i + EMBED_BATCH])
            try:
                resp = client.post(url, json={
                    "model": settings.OLLAMA_EMBEDDING_MODEL,
                    "input": batch,
                })
                resp.raise_for_status()
            except Exception as e:  # noqa: BLE001
                # ⚠️ `KnowledgeUnavailableError` 的 message 会一路走到
                #    界面上（对话页的"知识库不可用：…"、审查页的数据缺口、
                #    知识库管理页的状态说明），所以这里写的是给用户的话。
                #    服务地址、模型名、异常类名都是排查用的，只进日志 ——
                #    下面这行日志是它们唯一的落点。
                logger.warning(
                    f"[knowledge] embedding 调用失败：{type(e).__name__}: {e}；"
                    f"url={url} model={settings.OLLAMA_EMBEDDING_MODEL}"
                )
                raise KnowledgeUnavailableError(
                    "文本向量化服务连不上，暂时无法检索或写入知识库。"
                    "请确认本机的向量化服务已启动，或稍后重试。"
                ) from e

            vecs = resp.json().get("embeddings") or []
            if len(vecs) != len(batch):
                logger.warning(
                    f"[knowledge] embedding 返回条数不符："
                    f"请求 {len(batch)}，返回 {len(vecs)}"
                )
                raise KnowledgeUnavailableError(
                    "文本向量化服务返回的数据不完整，本次没有写入或检索任何内容"
                )
            out.extend(vecs)

    # 维度校验放在最后统一做 —— 中途发现维度不对，说明模型换了，
    # 那整个库都得重建，早退反而看不到全貌
    if out and len(out[0]) != settings.OLLAMA_EMBEDDING_DIM:
        logger.warning(
            f"[knowledge] 向量维度不符：实际 {len(out[0])}，"
            f"配置 {settings.OLLAMA_EMBEDDING_DIM}"
        )
        raise KnowledgeUnavailableError(
            "知识库里的向量与当前使用的向量化模型不匹配，需要重建知识库后才能检索"
        )
    return out


# ══════════════════════════════════════════════════════════════════
# Chroma
# ══════════════════════════════════════════════════════════════════


@dataclass
class CollectionInfo:
    """库的现状。给检索层与健康检查用。"""

    name: str
    count: int
    path: str
    available: bool = True
    reason: str = ""
    dim: int = 0


def _client():
    import chromadb

    path = settings.CHROMA_PATH
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(path))


def get_collection(*, create: bool = True):
    """
    取（或建）知识库 collection。

    距离函数在建库时定死：bge 是在归一化向量上训练的，
    用余弦（`hnsw:space=cosine`）才对齐它的训练目标。
    """
    client = _client()
    name = settings.CHROMA_COLLECTION_KNOWLEDGE
    if create:
        return client.get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine"},
        )
    return client.get_collection(name=name)


def collection_info() -> CollectionInfo:
    """探一下库的状态。**不抛异常** —— 供健康检查与检索降级判断使用。"""
    name = settings.CHROMA_COLLECTION_KNOWLEDGE
    try:
        col = get_collection(create=False)
        return CollectionInfo(name=name, count=col.count(),
                              path=str(settings.CHROMA_PATH))
    except Exception as e:  # noqa: BLE001
        # ⚠️ `reason` 会上屏（知识库管理页 / 健康检查的说明文字），
        #    所以给用户一句短话；异常类名与原文只进日志。
        logger.warning(f"[knowledge] 探不到知识库 {name}：{type(e).__name__}: {e}")
        return CollectionInfo(
            name=name, count=0, path=str(settings.CHROMA_PATH),
            available=False,
            reason="知识库暂时连不上",
        )


def reset_collection() -> None:
    """删掉重建。**仅在"模型换了必须重建"时使用**（见 embed_texts 的维度校验）。"""
    client = _client()
    name = settings.CHROMA_COLLECTION_KNOWLEDGE
    try:
        client.delete_collection(name)
        logger.warning(f"已删除 collection: {name}")
    except Exception:  # noqa: BLE001 —— 不存在时删除失败是正常的
        pass


def upsert_chunks(chunks: list[dict[str, Any]], *, embeddings: list[list[float]]) -> int:
    """
    把切好的 chunk 连同向量写进库里。

    ⚠️ `embeddings` **必须显式传入**，绝不能让 Chroma 用默认向量函数（见模块头「坑 2」）。
    """
    if not chunks:
        return 0
    if len(chunks) != len(embeddings):
        raise ValueError(f"chunk 数 {len(chunks)} 与向量数 {len(embeddings)} 不符")

    col = get_collection(create=True)
    ids, docs, metas = [], [], []
    for c in chunks:
        ids.append(chunk_id(c["source"], c["text"]))
        docs.append(c["text"])
        metas.append({
            "doc_type": c["doc_type"],
            "tags": tags_to_str(c.get("tags") or []),   # ⚠️ 不能直接放列表
            "headings": c.get("headings", ""),
            "source": c["source"],
            "source_dir": c.get("source_dir", ""),
            "priority": c.get("priority", "support"),
        })

    col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embeddings)
    return len(ids)


def query_vectors(
    embedding: list[float], *, k: int, where: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """向量检索。返回原始结果，由检索层转成 KnowledgeChunk。"""
    col = get_collection(create=True)
    res = col.query(
        query_embeddings=[embedding],
        n_results=k,
        where=where or None,
        include=["documents", "metadatas", "distances"],
    )

    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]

    out = []
    for doc, meta, dist in zip(docs, metas, dists):
        out.append({
            "text": doc,
            "metadata": meta or {},
            # cosine 距离 → 相似度（Chroma 的 cosine 距离 = 1 - 余弦相似度）
            "similarity": round(1.0 - float(dist), 4),
        })
    return out


def list_documents() -> tuple[list[dict[str, Any]], str]:
    """
    库里的文档清单（按 `source` 聚合）。返回 `(文档列表, 不可用原因)`。

    ⚠️ **这是只读的、按来源聚合的一层，不是"把整个库读出来"。**
    Chroma 的 `get()` 会把命中行的 document 正文一起返回，几百条 chunk
    的正文有好几 MB —— 而清单只需要元数据。所以 `include` 里**只要
    `metadatas`**，正文一个字节都不取。这条由
    `tests/test_knowledge_api.py::test_清单只读元数据不读正文` 盯着。

    ⚠️ 不可用时返回空清单 + 一句原因，**不抛异常**：
    调用方是「知识库管理」页，它要能显示"依赖挂了"而不是自己崩掉。
    与 `/system/health` 的立场一致（哪个依赖挂了由响应说清楚）。
    """
    info = collection_info()
    if not info.available:
        return [], info.reason or "知识库不可用"

    try:
        col = get_collection(create=False)
        if col is None:
            return [], "知识库集合不存在（还没入过库）"
        got = col.get(include=["metadatas"])
    except Exception as e:  # noqa: BLE001
        # 同上：返回值里那句会直接显示在知识库管理页上。
        logger.warning(f"[knowledge] 读取文档清单失败：{type(e).__name__}: {e}")
        return [], "知识库暂时连不上，读不到文档清单"

    metas = list(got.get("metadatas") or [])
    buckets: dict[str, dict[str, Any]] = {}
    for m in metas:
        m = m or {}
        src = str(m.get("source") or "(未标注来源)")
        b = buckets.setdefault(src, {
            "source": src,
            "source_dir": str(m.get("source_dir") or ""),
            "doc_type": str(m.get("doc_type") or ""),
            "priority": str(m.get("priority") or ""),
            "tags": tags_from_str(m.get("tags")),
            "chunks": 0,
            "headings": [],
        })
        b["chunks"] += 1
        # 章节路径去重后留前几条 —— 清单是给人看的，不是全量目录
        h = str(m.get("headings") or "").strip()
        if h and h not in b["headings"] and len(b["headings"]) < 6:
            b["headings"].append(h)

    docs = sorted(buckets.values(), key=lambda d: (-d["chunks"], d["source"]))
    return docs, ""


__all__ = [
    "KnowledgeUnavailableError", "CollectionInfo",
    "embed_texts", "get_collection", "collection_info", "reset_collection",
    "upsert_chunks", "query_vectors", "list_documents",
    "chunk_id", "tags_to_str", "tags_from_str",
    "EMBED_BATCH", "EMBED_TIMEOUT",
]
