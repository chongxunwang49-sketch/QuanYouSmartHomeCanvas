"""
知识库管理两条接口 + 工作台统计（2026-09-26 补齐）。

══════════════════════════════════════════════════════════════════
这三条接口此前**只存在于需求文档 §4.6 的表里**
══════════════════════════════════════════════════════════════════
`GET /knowledge/list`、`POST /knowledge/upload`、`GET /dashboard/stats`
在代码里一条都没有 —— 没有占位、没有 stub、没有 501。后果是「知识库管理」
页只能做成只读的（页面上一向如实写着这件事，没有摆假按钮），
而工作台拿不到任何跨会话的真实数字。

补它们的时候有两处**必须守住**的性质，各有一条用例：

  · **权限门是服务端判的，不是靠菜单藏起来** —— 与 `nav.ts` 的说明一致：
    藏菜单不构成防线。所以设计师调用要拿到 4002。
  · **入库失败时不许"写了一半"** —— embedding 挂了要在写库**之前**就返回，
    而不是写完一部分再报错（那种"部分成功"在知识库里极难清理，
    而且它会静默改变检索结果）。

⚠️ embedding 走 Ollama（本机服务）。为了让这个文件在**没有 Ollama 的机器上
也能跑**，涉及真实推理的那一步一律 monkeypatch —— 这不是"为了测试而改代码"，
而是把"接口的接线"与"依赖能不能用"分开验。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.api import routes
from backend.app.api.schemas import ApiError, KnowledgeUploadRequest
from backend.app.core import auth
from backend.app.services.knowledge import chunking, store


def _run(coro):
    """Windows 上 psycopg 的异步模式用不了 ProactorEventLoop（见 conftest）。"""
    return asyncio.run(coro)


def _user(role: str):
    return next(u for u in auth.users() if u.role == role)


ADMIN = _user(auth.ROLE_ADMIN)
DESIGNER = _user("designer")

DOC = "# 测试文档\n\n" + ("这是一条用于测试的规则，内容重复到足够切成一块。" * 12)


# ══════════════════════════════════════════════════════════════════
# 权限：菜单藏起来不算防线
# ══════════════════════════════════════════════════════════════════


class TestOnlyAdmin:
    def test_设计师不能看知识库(self):
        with pytest.raises(ApiError) as e:
            _run(routes.knowledge_list(DESIGNER))
        assert e.value.code == 4002, (
            f"拿到 {e.value.code} —— 非管理员必须被服务端拒绝。"
            f"「知识库管理」在侧栏只对管理员可见，但**藏菜单不是防线**"
        )
        assert "管理员" in str(e.value)

    def test_设计师不能入库(self):
        with pytest.raises(ApiError) as e:
            _run(routes.knowledge_upload(
                KnowledgeUploadRequest(title="x", text=DOC), DESIGNER))
        assert e.value.code == 4002

    def test_普通用户也不能(self):
        with pytest.raises(ApiError) as e:
            _run(routes.knowledge_list(_user(auth.ROLE_USER)))
        assert e.value.code == 4002


# ══════════════════════════════════════════════════════════════════
# 入库的参数校验
# ══════════════════════════════════════════════════════════════════


class TestUploadValidation:
    @pytest.mark.parametrize(
        "req,keyword",
        [
            (KnowledgeUploadRequest(title="   ", text=DOC), "标题"),
            (KnowledgeUploadRequest(title="t", text="   "), "正文"),
            # ⚠️ 必须**真的**超过上限：第一版写 DOC * 200 只有约 7.6 万字符，
            #    压根没越线，于是这条用例测的是"合法输入"—— 而它照样"失败"了，
            #    因为报错里没有"上限"两个字。测试数据算错比断言写错更难看出来。
            (KnowledgeUploadRequest(
                title="t", text="x" * (chunking.MAX_UPLOAD_CHARS + 1)), "上限"),
            # ⚠️ 2026-09-27 改：这两条的关键词原来分别是 `doc_type`（字段名）与
            #    `chunk`（内部术语）—— 报错正文会显示在知识库管理页上，
            #    所以正文改成了中文说法，断言跟着改。
            (KnowledgeUploadRequest(title="t", text=DOC, doc_type="乱写"), "内容类型"),
            (KnowledgeUploadRequest(title="t", text="太短"), "可入库"),
        ],
    )
    def test_参数不合法一律4001并说清哪里不对(self, req, keyword):
        with pytest.raises(ApiError) as e:
            _run(routes.knowledge_upload(req, ADMIN))
        assert e.value.code == 4001
        assert keyword in str(e.value), (
            f"报错里没提到 {keyword!r}，收到：{e.value}"
        )

    def test_标签过多被拒(self):
        with pytest.raises(ApiError) as e:
            _run(routes.knowledge_upload(
                KnowledgeUploadRequest(title="t", text=DOC,
                                       tags=[f"t{i}" for i in range(20)]), ADMIN))
        assert e.value.code == 4001 and "标签" in str(e.value)


# ══════════════════════════════════════════════════════════════════
# 入库的接线（不碰真实 Ollama）
# ══════════════════════════════════════════════════════════════════


class TestUploadWiring:
    def test_切块用的是同一个实现(self, monkeypatch):
        """
        ⚠️ 守着"只有一份切块逻辑"这件事。

        上一版 `chunk_files` 自己写了一遍切块，而上传接口如果再写一遍，
        两边迟早分叉（分块大小、标题抽取、最小长度任一处不同），
        检索出来的"来源"就不可比 —— **而没有任何东西会报错**。
        所以这里断言上传走的是 `chunking.chunk_text`。
        """
        seen: dict = {}
        # ⚠️ **先把原函数抓出来再 monkeypatch。**
        #    spy 里再写 `chunking.chunk_text(...)` 的话，那个名字已经被换成
        #    spy 自己了 —— 无限递归。第一版就是这样报的
        #    `RecursionError: maximum recursion depth exceeded`。
        original = chunking.chunk_text

        def spy(text, **kw):
            seen.update(kw)
            return original(text, **kw)

        monkeypatch.setattr(chunking, "chunk_text", spy)
        monkeypatch.setattr(store, "embed_texts", lambda texts: [[0.0] * 8 for _ in texts])
        monkeypatch.setattr(store, "upsert_chunks", lambda chunks, **kw: len(chunks))
        monkeypatch.setattr(store, "collection_info",
                            lambda: store.CollectionInfo(name="t", count=1, path=""))

        _run(routes.knowledge_upload(
            KnowledgeUploadRequest(title="接线测试", text=DOC,
                                   doc_type="regulation", tags=["a", "b"]), ADMIN))
        assert seen.get("doc_type") == "regulation", f"参数没传下去：{seen}"
        assert seen.get("tags") == ["a", "b"]
        assert seen.get("source") == "用户上传/接线测试.md", (
            f"source 的形状不对：{seen.get('source')!r} —— "
            f"它会作为「出处」显示在审查结论里"
        )

    def test_embedding挂掉时不写任何东西(self, monkeypatch):
        """
        ⚠️ **"写了一半"比"完全没写"难处理得多。**

        embedding 失败必须发生在写库**之前** —— 否则知识库里会留下
        一批没有向量的 chunk，而 Chroma 的检索靠向量，
        这些行等于永远查不到却占着 `count`（清单里显得入库成功了）。
        """
        wrote = []

        def boom(texts):
            raise RuntimeError("ollama 连不上")

        monkeypatch.setattr(store, "embed_texts", boom)
        monkeypatch.setattr(store, "upsert_chunks",
                            lambda chunks, **kw: wrote.append(chunks) or len(chunks))

        with pytest.raises(ApiError) as e:
            _run(routes.knowledge_upload(
                KnowledgeUploadRequest(title="失败测试", text=DOC), ADMIN))
        assert e.value.code == 5002, (
            f"依赖不可用应当是 5002（与「参数不合法」分开），收到 {e.value.code}"
        )
        assert "没有写入任何内容" in str(e.value), (
            "报错必须明确说「没写进去」 —— 否则用户不知道要不要重试"
        )
        assert not wrote, "embedding 失败之后仍然调了写库 —— 会留下没有向量的孤儿 chunk"


# ══════════════════════════════════════════════════════════════════
# 清单
# ══════════════════════════════════════════════════════════════════


class TestKnowledgeList:
    def test_不可用时给原因而不是报错(self, monkeypatch):
        """
        ⚠️ 依赖挂了不是"接口坏了"，而是"这个能力现在用不了，因为 X"。
        与 `/system/health` 同一立场。报错的话，「知识库管理」页
        只会显示一片空白 —— 而空白会被读成"知识库是空的"。
        """
        monkeypatch.setattr(
            store, "collection_info",
            lambda: store.CollectionInfo(name="t", count=0, path="",
                                         available=False, reason="ollama 没起来"),
        )
        d = _run(routes.knowledge_list(ADMIN)).data
        assert d["available"] is False
        assert "ollama" in d["reason"]
        assert d["documents"] == []

    def test_清单只读元数据不读正文(self, monkeypatch):
        """
        ⚠️ Chroma 的 `get()` 会把正文一起返回，几百条 chunk 有好几 MB。
        清单只需要元数据 —— 所以 `include` 里只能有 `metadatas`。
        这条用例直接盯住那个参数。
        """
        seen: dict = {}

        class FakeCol:
            def get(self, **kw):
                seen.update(kw)
                return {"metadatas": [
                    # ⚠️ tags 走**真的编码器**（`tags_to_str`）生成。
                    #    手写 "x,y" 是错的：真正的分隔符是 `|`（见 store.py
                    #    的说明），而手写的那份不会有人告诉你它不对。
                    {"source": "a.md", "doc_type": "avoid_pit",
                     "tags": store.tags_to_str(["x", "y"]),
                     "headings": "H1", "priority": "support"},
                    {"source": "a.md", "doc_type": "avoid_pit",
                     "tags": store.tags_to_str(["x", "y"]),
                     "headings": "H2", "priority": "support"},
                    {"source": "b.md", "doc_type": "regulation", "tags": "",
                     "headings": "", "priority": "core"},
                ]}

        monkeypatch.setattr(store, "collection_info",
                            lambda: store.CollectionInfo(name="t", count=3, path=""))
        monkeypatch.setattr(store, "get_collection", lambda create=True: FakeCol())

        d = _run(routes.knowledge_list(ADMIN)).data
        assert "documents" not in seen, "`get()` 取了正文 —— 清单只需要元数据"
        assert seen.get("include") == ["metadatas"], (
            f"include 写的是 {seen.get('include')!r}，应当只有 metadatas"
        )
        assert d["document_count"] == 2
        # 条数多的排前面；同一 source 聚合成一条
        assert [x["source"] for x in d["documents"]] == ["a.md", "b.md"]
        assert d["documents"][0]["chunks"] == 2
        assert d["documents"][0]["tags"] == ["x", "y"], "tags 没从字符串还原成列表"

    def test_可选类型由后端给(self):
        """前端硬编码一份 doc_type 会随语料类型变更静默漂移。"""
        d = _run(routes.knowledge_list(ADMIN)).data
        assert d["doc_types"] == list(chunking.DOC_TYPES)
        assert "max_upload_chars" in d["limits"]


# ══════════════════════════════════════════════════════════════════
# 工作台统计
# ══════════════════════════════════════════════════════════════════


class TestDashboardStats:
    def test_库不通时给null而不是0(self, monkeypatch):
        """
        ⚠️ **`null` 与 `0` 的含义完全相反。**
        给 0 的话界面上显示"0 个户型" —— 而真相是"读不到"。
        这正是本项目三原则里那条「不产出看起来合理的错误」。
        """
        from backend.app.db import pool

        async def no():
            return False

        monkeypatch.setattr(pool, "is_available", no)
        d = _run(routes.dashboard_stats(ADMIN)).data
        assert d["available"] is False
        assert d["layouts"] is None and d["plans"] is None
        assert d["audit_events"] is None
        assert d["reason"], "库不通时必须说明原因与怎么修"

    def test_库通时给真实计数(self, monkeypatch):
        from backend.app.db import pool, repository

        async def yes():
            return True

        monkeypatch.setattr(pool, "is_available", yes)

        async def rows(table):
            return {"house_layouts": 7, "design_plans": 21,
                    "audit_logs": 99}[table]

        async def by_action():
            return {"login": 5, "task_created": 3}

        monkeypatch.setattr(repository, "count_rows", rows)
        monkeypatch.setattr(repository, "audit_counts_by_action", by_action)
        d = _run(routes.dashboard_stats(ADMIN)).data
        assert (d["layouts"], d["plans"], d["audit_events"]) == (7, 21, 99)
        assert d["by_action"] == {"login": 5, "task_created": 3}
        assert d["available"] is True

    def test_表名白名单挡住注入面(self):
        """
        ⚠️ 表名要拼进 SQL（没法用 `%s` 占位），所以它必须有白名单。
        白名单之外一律拒绝 —— 而不是拼进去再指望数据库报错。
        """
        from backend.app.db import repository

        for bad in ("users; drop table x", "pg_shadow", "information_schema.tables"):
            with pytest.raises(ValueError):
                _run(repository.count_rows(bad))
