"""
知识库服务测试（切块 / 存取 / 检索）。

本文件守的是**两个不会报错、只会出错**的坑，以及一条纪律：

【坑 1】Chroma 的 metadata 不支持列表值。chunk 带 `tags: [...]`，
    直接塞会抛 ValueError —— 麻烦在于它只在**写**的时候炸，
    等发现时可能已经写了一半数据。
【坑 2】不给 `embeddings=` 时 Chroma 会自己去下载一个 384 维的默认模型。
    更糟的是它**可能成功**，然后你得到一个 384 维的库而查询用 1024 维。

【纪律】`search()` **永远不抛异常**。知识库不可用时返回空结果 + 原因，
由 A-06 如实说"本次未取得知识库依据"，而不是凭常识编规则。
"""

from __future__ import annotations

import pytest

from backend.app.services.knowledge import chunking, retriever, store

# ══════════════════════════════════════════════════════════════════
# 坑 1：metadata 不能放列表
# ══════════════════════════════════════════════════════════════════


class TestTagsSerialization:
    def test_标签序列化成字符串(self):
        assert store.tags_to_str(["报价审核", "增项"]) == "报价审核|增项"

    def test_空标签串成空串(self):
        assert store.tags_to_str([]) == ""
        assert store.tags_to_str(["", None]) == ""  # type: ignore[list-item]

    def test_还原成列表(self):
        assert store.tags_from_str("报价审核|增项") == ["报价审核", "增项"]

    def test_空值还原成空列表(self):
        for v in ("", None, 0):
            assert store.tags_from_str(v) == []

    def test_往返一致(self):
        """读写必须对称，否则检索出来的标签会悄悄变样。"""
        for tags in ([], ["a"], ["a", "b"], ["报价审核", "单价陷阱", "增项"]):
            assert store.tags_from_str(store.tags_to_str(tags)) == tags

    def test_分隔符不出现在标签里(self):
        """
        用 `|` 做分隔符的前提是标签本身不含它。
        从清单里读出来的标签，这里做一次体检 —— 含 `|` 会让标签被切错。
        """
        manifest = chunking.load_manifest(
            chunking.Path(__file__).resolve().parent.parent
            / "seed_data" / "references_manifest.yaml"
        )
        for entry in manifest["ingest"]:
            for tag in entry.get("tags") or []:
                assert "|" not in tag, f"标签 {tag!r} 含分隔符，会被切错"


# ══════════════════════════════════════════════════════════════════
# chunk id 幂等
# ══════════════════════════════════════════════════════════════════


class TestChunkId:
    def test_同内容同id(self):
        """幂等的前提：重复入库是 upsert 覆盖，不是追加。"""
        a = store.chunk_id("src.md", "同样的一段话")
        b = store.chunk_id("src.md", "同样的一段话")
        assert a == b

    def test_内容变id变(self):
        a = store.chunk_id("src.md", "话 A")
        b = store.chunk_id("src.md", "话 B")
        assert a != b

    def test_来源变id变(self):
        """两条不同文件里的相同文字，不该被当成同一条。"""
        a = store.chunk_id("a.md", "同样的一段话")
        b = store.chunk_id("b.md", "同样的一段话")
        assert a != b

    def test_id是合法十六进制(self):
        cid = store.chunk_id("s", "t")
        assert len(cid) == 40
        int(cid, 16)


# ══════════════════════════════════════════════════════════════════
# 切块
# ══════════════════════════════════════════════════════════════════


class TestChunking:
    def test_标题留在正文里(self):
        """
        strip_headers=False —— 标题要留在 chunk 里，检索时才看得见语境。

        注意正文要够长：低于 MIN_CHUNK_CHARS 的碎片会被丢弃
        （"以上都不含"这种半句话无法独立检索）。
        """
        body = "## 小节\n\n" + "这是正文内容，需要足够长才能通过长度下限。" * 6
        chunks = chunking.chunk_files([{
            "path": _tmp_md("# 大标题\n\n" + body),
            "rel": "x/y.md", "doc_type": "avoid_pit", "tags": [],
            "source": "x", "priority": "core",
        }])
        assert chunks, "应当切出 chunk"
        assert any("#" in c["text"] for c in chunks)

    def test_标题路径被抽出(self):
        chunks = chunking.chunk_files([{
            "path": _tmp_md("## 二、11项隐形消费\n\n这是正文内容，足够长以通过下限。" * 3),
            "rel": "x/y.md", "doc_type": "avoid_pit", "tags": [],
            "source": "x", "priority": "core",
        }])
        assert chunks[0]["headings"], "应抽出章节路径，便于溯源定位"

    def test_过短片段被丢弃(self):
        chunks = chunking.chunk_files([{
            "path": _tmp_md("# T\n\n短。"),
            "rel": "x/y.md", "doc_type": "avoid_pit", "tags": [],
            "source": "x", "priority": "core",
        }])
        assert chunks == [], "低于下限的碎片不该入库"

    def test_元数据随文件带出(self):
        chunks = chunking.chunk_files([{
            "path": _tmp_md("## 标题\n\n" + "正文内容。" * 30),
            "rel": "dir/f.md", "doc_type": "regulation",
            "tags": ["环保"], "source": "dir", "priority": "core",
        }])
        assert chunks[0]["doc_type"] == "regulation"
        assert chunks[0]["tags"] == ["环保"]
        assert chunks[0]["source"] == "dir/f.md"


def _tmp_md(text: str):
    """把文本落成临时 md 文件，返回 Path。"""
    import tempfile
    from pathlib import Path

    p = Path(tempfile.mkdtemp()) / "t.md"
    p.write_text(text, encoding="utf-8")
    return p


# ══════════════════════════════════════════════════════════════════
# 检索永不抛异常
# ══════════════════════════════════════════════════════════════════


class TestSearchNeverRaises:
    def test_embedding挂了返回不可用而非抛异常(self, monkeypatch):
        monkeypatch.setattr(
            retriever, "embed_texts",
            lambda *a, **kw: (_ for _ in ()).throw(
                store.KnowledgeUnavailableError("Ollama 没开")),
        )
        r = retriever.search("任意查询")
        assert r.available is False
        assert "Ollama 没开" in r.reason
        assert r.chunks == []

    def test_向量检索挂了返回不可用(self, monkeypatch):
        monkeypatch.setattr(retriever, "embed_texts", lambda *a, **kw: [[0.0] * 1024])
        monkeypatch.setattr(
            retriever, "query_vectors",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("库炸了")),
        )
        r = retriever.search("任意查询")
        assert r.available is False
        assert "库炸了" in r.reason

    def test_空查询安全返回(self):
        r = retriever.search("")
        assert r.chunks == []
        assert r.available is True

    def test_低于相似度下限的被过滤(self, monkeypatch):
        monkeypatch.setattr(retriever, "embed_texts", lambda *a, **kw: [[0.0] * 1024])
        monkeypatch.setattr(retriever, "query_vectors", lambda *a, **kw: [
            {"text": "弱相关", "metadata": {"source": "s.md"}, "similarity": 0.1},
            {"text": "强相关", "metadata": {"source": "s2.md"}, "similarity": 0.9},
        ])
        r = retriever.search("q", min_similarity=0.35)
        assert len(r.chunks) == 1
        assert r.chunks[0].text == "强相关"

    def test_全部被过滤时说明原因(self, monkeypatch):
        monkeypatch.setattr(retriever, "embed_texts", lambda *a, **kw: [[0.0] * 1024])
        monkeypatch.setattr(retriever, "query_vectors", lambda *a, **kw: [
            {"text": "弱", "metadata": {}, "similarity": 0.1},
        ])
        r = retriever.search("q")
        assert r.available is True
        assert "均低于" in r.reason, "要区分'没查到'和'查到了但都不够相关'"


# ══════════════════════════════════════════════════════════════════
# 引用格式
# ══════════════════════════════════════════════════════════════════


class TestCitation:
    def test_引用含文件与章节(self):
        c = retriever.KnowledgeChunk(
            text="x",
            source="zhuangxiu-skills/装修报价审核/references/单价陷阱与隐形消费.md",
            headings="四、装修预算只有单价坑你没商量",
        )
        assert "单价陷阱与隐形消费" in c.citation
        assert "四、装修预算只有单价坑你没商量" in c.citation

    def test_剥掉共同前缀(self):
        """出处要短好读，`zhuangxiu-skills/` 是共同前缀，每条都带就太啰嗦。"""
        c = retriever.KnowledgeChunk(text="x", source="zhuangxiu-skills/a/b.md")
        assert not c.citation.startswith("zhuangxiu-skills/")

    def test_没有章节时只给文件(self):
        c = retriever.KnowledgeChunk(text="x", source="a/b.md")
        assert "§" not in c.citation
        assert "b" in c.citation


# ══════════════════════════════════════════════════════════════════
# 多查询合并
# ══════════════════════════════════════════════════════════════════


class TestSearchMany:
    def test_多查询结果去重合并(self, monkeypatch):
        calls: list[str] = []

        def _search(q, **kw):
            calls.append(q)
            return retriever.RetrievalResult(query=q, chunks=[
                retriever.KnowledgeChunk(text=f"内容-{q}", source=f"{q}.md",
                                         similarity=0.7),
            ])

        monkeypatch.setattr(retriever, "search", _search)
        r = retriever.search_many(["甲", "乙", "丙"], top_k_each=1)

        assert calls == ["甲", "乙", "丙"]
        assert len(r.chunks) == 3

    def test_同一条命中多次只留相似度高的(self, monkeypatch):
        def _search(q, **kw):
            return retriever.RetrievalResult(query=q, chunks=[
                retriever.KnowledgeChunk(text="同一段话", source="s.md",
                                         similarity=0.6 if q == "甲" else 0.9),
            ])

        monkeypatch.setattr(retriever, "search", _search)
        r = retriever.search_many(["甲", "乙"], top_k_each=1)
        assert len(r.chunks) == 1
        assert r.chunks[0].similarity == 0.9

    def test_有一个查询失败则整体标记不可用(self, monkeypatch):
        def _search(q, **kw):
            if q == "乙":
                return retriever.RetrievalResult(query=q, chunks=[],
                                                 available=False, reason="挂了")
            return retriever.RetrievalResult(query=q, chunks=[
                retriever.KnowledgeChunk(text="内容", source="s.md"),
            ])

        monkeypatch.setattr(retriever, "search", _search)
        r = retriever.search_many(["甲", "乙"])
        assert r.available is False
        assert "挂了" in r.reason
        assert r.chunks, "部分成功的结果仍要保留，不能因为一个失败就全丢"
