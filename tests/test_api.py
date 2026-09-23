"""
HTTP 接口层测试。

本文件守四组命题：

1. **业务失败必须是 HTTP 200 + code != 0**（4.4 明确要求）。
   用 4xx 表达"任务还没好""数据不支撑该操作"会被前端 axios 拦截器
   当成服务错误弹通用报错，而用户需要看到的是可操作的提示。
2. **异步接口立即返回**，不阻塞在 100 秒的链路上。
3. **能力守卫在入口拦下**，不放进图里让三个分支各撞一次墙（AC-33）。
4. **任务不会永远卡在 processing** —— 后台异常、停机取消都要落到终态。

测试用 `httpx.ASGITransport` 走进程内请求，不建真实连接
（conftest 的绊线会拦住真出网，但放行 ASGI transport）。
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx
import pytest

from backend.app.api import store as layout_store
from backend.app.api.tasks import get_task_manager, reset_task_manager
from backend.app.graph import workflow
from backend.app.main import create_app

# ══════════════════════════════════════════════════════════════════
# 样本
# ══════════════════════════════════════════════════════════════════

#: 1x1 PNG，够小又不至于让 base64 校验出问题
TINY_PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)).decode()

#: 数据完整的户型（能过 generate_plan 守卫）
FULL_LAYOUT: dict[str, Any] = {
    "layout_id": "layout_api_test",
    "mode": "full",
    "rooms": [
        {"name": "客厅", "type": "living_room", "area": 28.5, "bbox": [120, 80, 420, 360]},
        {"name": "主卧", "type": "bedroom", "area": 16.2, "bbox": [440, 80, 680, 300]},
    ],
    "walls": [{"type": "load_bearing", "coords": [[100, 60], [700, 60]]}],
    "doors": [{"position": [250, 380], "width": 0.9}],
    "windows": [{"position": [600, 50], "width": 1.8, "orientation": "south"}],
    "total_area": 44.7,
    "has_north_arrow": True,
    "confidence": 0.85,
}

#: 缺墙体的户型 —— 诊断能做，但方案生成会被守卫拒（AC-33 的分级门槛）
NO_WALLS_LAYOUT = {**FULL_LAYOUT, "walls": []}


@pytest.fixture
def client():
    """进程内 ASGI 客户端。**不用 TestClient**，避免它与绊线纠缠。"""
    app = create_app()

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as c:
            yield c

    return _run


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例都从干净的任务表与户型暂存开始。"""
    reset_task_manager()
    layout_store.clear()
    yield
    reset_task_manager()
    layout_store.clear()


async def _client() -> httpx.AsyncClient:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ══════════════════════════════════════════════════════════════════
# 响应外壳与 trace_id
# ══════════════════════════════════════════════════════════════════


class TestEnvelope:
    async def test_健康检查统一外壳(self):
        async with await _client() as c:
            r = await c.get("/api/v1/system/health")
        assert r.status_code == 200
        body = r.json()
        assert set(body) >= {"code", "msg", "data"}
        assert body["code"] == 0

    async def test_根路径可用(self):
        async with await _client() as c:
            r = await c.get("/")
        assert r.status_code == 200
        assert r.json()["data"]["docs"] == "/docs"

    async def test_trace_id回写响应头(self):
        async with await _client() as c:
            r = await c.get("/api/v1/system/health")
        assert r.headers.get("X-Trace-Id")
        assert r.headers.get("X-Elapsed-Ms") is not None

    async def test_沿用调用方传入的trace_id(self):
        """前端/网关已有链路 id 时要能串起来，而不是各生成各的。"""
        async with await _client() as c:
            r = await c.get("/api/v1/system/health",
                            headers={"X-Trace-Id": "my-own-trace-001"})
        assert r.headers["X-Trace-Id"] == "my-own-trace-001"

    async def test_未知路由是404而非统一外壳(self):
        """
        路由不存在属于**协议层**错误，不是业务失败 ——
        这一层不套外壳是有意的（见 api/schemas.py 的说明）。
        """
        async with await _client() as c:
            r = await c.get("/api/v1/nope")
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════
# 业务失败 = 200 + 非 0 code
# ══════════════════════════════════════════════════════════════════


class TestBusinessErrors:
    async def test_任务不存在返回200加4004(self):
        async with await _client() as c:
            r = await c.get("/api/v1/task/task_不存在/status")
        assert r.status_code == 200, "业务失败不能用 4xx，前端拦截器会误判"
        body = r.json()
        assert body["code"] == 4004
        assert "不存在" in body["msg"]

    async def test_layout_id未知返回4004(self):
        async with await _client() as c:
            r = await c.post("/api/v1/design/generate",
                             json={"layout_id": "layout_没有这个"})
        assert r.status_code == 200
        assert r.json()["code"] == 4004

    async def test_数据不支撑方案生成返回4002(self):
        """
        AC-33：缺墙体的户型能诊断、不能出方案 —— 守卫要在**入口**拦下，
        而不是放进图里让三个分支各撞一次墙。
        """
        await layout_store.save("layout_no_walls", NO_WALLS_LAYOUT)
        async with await _client() as c:
            r = await c.post("/api/v1/design/generate",
                             json={"layout_id": "layout_no_walls"})
        body = r.json()
        assert r.status_code == 200
        assert body["code"] == 4002
        # 拒绝必须说清缺什么、怎么办（capabilities.py 的立场）
        assert body["data"]["missing"] == ["墙体信息"]
        assert body["data"]["suggestion"]

    async def test_报价单为空返回4001(self):
        async with await _client() as c:
            r = await c.post("/api/v1/avoid-pit/review", json={"quote_text": "   "})
        assert r.status_code == 200
        assert r.json()["code"] == 4001

    async def test_图片为空返回4001(self):
        async with await _client() as c:
            r = await c.post("/api/v1/layout/parse", json={"image": ""})
        assert r.status_code == 200
        assert r.json()["code"] == 4001

    async def test_未知品类返回4001(self):
        async with await _client() as c:
            r = await c.get("/api/v1/material/price?category=不存在的品类")
        assert r.status_code == 200
        assert r.json()["code"] == 4001

    async def test_styles为空返回4001(self):
        await layout_store.save("layout_ok", FULL_LAYOUT)
        async with await _client() as c:
            r = await c.post("/api/v1/design/generate",
                             json={"layout_id": "layout_ok", "styles": []})
        assert r.json()["code"] == 4001


# ══════════════════════════════════════════════════════════════════
# 材料价格（同步接口）
# ══════════════════════════════════════════════════════════════════


class TestMaterialPrice:
    async def test_返回全友与竞品两组(self):
        async with await _client() as c:
            r = await c.get("/api/v1/material/price?category=floor")
        data = r.json()["data"]
        assert data["quanyou_recommended"], "应返回全友推荐"
        assert data["items"], "应返回其它品牌对照"
        assert all(p["brand"] == "全友" for p in data["quanyou_recommended"])
        assert all(p["brand"] != "全友" for p in data["items"])

    async def test_必须带演示数据声明(self):
        """
        R-09 / 5.3 要求演示数据必须显著标注。
        这是最容易被前端"顺手美化"掉的地方，所以后端每次都带上。
        """
        async with await _client() as c:
            r = await c.get("/api/v1/material/price")
        d = r.json()["data"]
        assert d["disclaimer"].strip()
        assert "演示" in d["disclaimer"]
        assert d["catalog_version"]

    async def test_按品牌过滤(self):
        async with await _client() as c:
            r = await c.get("/api/v1/material/price?category=floor&brand=圣象")
        d = r.json()["data"]
        assert d["total"] >= 1
        assert all("圣象" in p["brand"] for p in d["items"])

    async def test_quanyou_first为假时不推自有品牌(self):
        async with await _client() as c:
            r = await c.get("/api/v1/material/price?quanyou_first=false")
        assert r.json()["data"]["quanyou_recommended"] == []

    async def test_价格区间来自目录而非模型(self):
        from backend.app.services.material import catalog

        async with await _client() as c:
            r = await c.get("/api/v1/material/price?category=floor")
        index = catalog.by_id()
        for p in r.json()["data"]["quanyou_recommended"] + r.json()["data"]["items"]:
            assert p["price_range"] == list(index[p["id"]].price_range)


# ══════════════════════════════════════════════════════════════════
# 异步任务：立即返回 + 可轮询
# ══════════════════════════════════════════════════════════════════


class TestAsyncTask:
    async def test_解析接口立即返回task_id(self):
        """整条链 100 秒，同步 HTTP 挺不过任何一层超时。"""
        async with await _client() as c:
            r = await c.post("/api/v1/layout/parse",
                             json={"image": TINY_PNG, "detail_level": "full"})
        body = r.json()
        assert r.status_code == 200
        assert body["code"] == 0
        assert body["data"]["task_id"].startswith("task_")
        assert body["data"]["status"] == "processing"
        assert body["data"]["estimated_seconds"] > 0

    async def test_新建任务立刻可轮询(self):
        async with await _client() as c:
            r = await c.post("/api/v1/layout/parse", json={"image": TINY_PNG})
            tid = r.json()["data"]["task_id"]
            s = await c.get(f"/api/v1/task/{tid}/status")
        d = s.json()["data"]
        assert d["task_id"] == tid
        assert d["status"] in ("pending", "processing")
        # phase_text 必须是中文可展示文案，不是枚举原文
        assert d["phase_text"]
        assert not d["phase_text"].isascii(), f"phase_text 是英文原文：{d['phase_text']}"

    async def test_未完成也返回200(self):
        """4.4：用 4xx 表示"还没好"会被前端拦截器误判为错误。"""
        async with await _client() as c:
            r = await c.post("/api/v1/layout/parse", json={"image": TINY_PNG})
            tid = r.json()["data"]["task_id"]
            s = await c.get(f"/api/v1/task/{tid}/status")
        assert s.status_code == 200
        assert s.json()["code"] == 0

    async def test_响应带no_store(self):
        """进度必须拿到最新值，中间层缓存会让界面卡住（4.4）。"""
        async with await _client() as c:
            r = await c.post("/api/v1/layout/parse", json={"image": TINY_PNG})
            tid = r.json()["data"]["task_id"]
            s = await c.get(f"/api/v1/task/{tid}/status")
        assert "no-store" in s.headers.get("Cache-Control", "")

    async def test_任务id带日期便于排查(self):
        from datetime import datetime

        async with await _client() as c:
            r = await c.post("/api/v1/layout/parse", json={"image": TINY_PNG})
        tid = r.json()["data"]["task_id"]
        assert datetime.now().strftime("%Y%m%d") in tid


# ══════════════════════════════════════════════════════════════════
# 后台执行：走假 Agent 跑通整条轮询流程
# ══════════════════════════════════════════════════════════════════


class TestBackgroundExecution:
    """
    这组用一个**极简的桩图**替换掉真图。

    目的是验证 runner 本身：进度写入、终态、结果组装、异常兜底 ——
    而不是再测一遍 Agent（那有各自专门的测试文件）。
    """

    @pytest.fixture
    def stub_graph(self, monkeypatch):
        import backend.app.api.tasks as tasks_mod

        class _StubGraph:
            def __init__(self, updates: list[dict], final: dict, boom: bool = False):
                self.updates = updates
                self.final = final
                self.boom = boom

            async def astream_events(self, state, cfg, version=None):
                """
                模拟 `astream_events` 的节点开始事件。

                runner 现在靠 `on_chain_start` 拿节点名（这样进度不滞后），
                所以桩也要按事件流来 —— 只模拟 astream(updates) 的话，
                runner 会一个阶段都写不出来。
                """
                for u in self.updates:
                    for node in u:
                        yield {"event": "on_chain_start", "name": node}
                    await asyncio.sleep(0)   # 让出控制权，模拟真实节点
                if self.boom:
                    raise RuntimeError("模拟图执行失败")

            async def aget_state(self, cfg):
                class _Snap:
                    values = self.final
                return _Snap()

        def _install(updates, final, boom=False):
            g = _StubGraph(updates, final, boom)
            monkeypatch.setattr(tasks_mod, "get_compiled_graph", lambda stages: g)
            return g

        return _install

    async def _wait_done(self, c, tid, timeout=5.0):
        """轮询直到终态。异步测试里可以真的 await，不必 sleep 硬等。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            r = await c.get(f"/api/v1/task/{tid}/status")
            d = r.json()["data"]
            if d["status"] in ("completed", "failed"):
                return d
            await asyncio.sleep(0.02)
        raise AssertionError(f"任务 {tid} 在 {timeout}s 内未结束")

    async def test_成功流程走到completed(self, stub_graph, stub_knowledge):
        stub_graph(
            updates=[{"parse_layout": {}}, {"diagnose_layout": {}}],
            final={
                "layout_id": "layout_stub", "layout": FULL_LAYOUT,
                "diagnosis": {"overall_score": 7.0},
                "degraded": False, "trace": [], "errors": [],
            },
        )
        async with await _client() as c:
            r = await c.post("/api/v1/layout/parse", json={"image": TINY_PNG})
            d = await self._wait_done(c, r.json()["data"]["task_id"])

        assert d["status"] == "completed"
        assert d["phase"] == "done"
        assert d["result"]["layout_id"] == "layout_stub"
        assert d["result"]["layout"]["rooms"]

    async def test_降级完成标成degraded(self, stub_graph, stub_knowledge):
        """降级完成也是完成，但阶段要如实标出来（AC-17）。"""
        stub_graph(
            updates=[{"parse_layout": {}}],
            final={"layout_id": "l", "layout": {"mode": "degraded_basic"},
                   "degraded": True, "degrade_reasons": ["主模型不可用"],
                   "trace": [], "errors": []},
        )
        async with await _client() as c:
            r = await c.post("/api/v1/layout/parse", json={"image": TINY_PNG})
            d = await self._wait_done(c, r.json()["data"]["task_id"])

        assert d["status"] == "completed"
        assert d["phase"] == "degraded"
        assert d["degraded"] is True
        assert d["phase_text"] != "degraded", "phase_text 应是中文文案"

    async def test_图执行失败落到终态而非卡住(self, stub_graph, stub_knowledge):
        """
        ⚠️ 没有兜底的话，任务会永远停在 processing，
        前端一直轮询到超时也等不到结果。
        """
        stub_graph(updates=[{"parse_layout": {}}], final={}, boom=True)
        async with await _client() as c:
            r = await c.post("/api/v1/layout/parse", json={"image": TINY_PNG})
            d = await self._wait_done(c, r.json()["data"]["task_id"])

        assert d["status"] == "failed"
        assert "模拟图执行失败" in (d["error"] or "")

    async def test_解析完成后户型被暂存(self, stub_graph, stub_knowledge):
        """4.3 只凭 layout_id 取户型，所以解析完必须存下来。"""
        stub_graph(
            updates=[{"parse_layout": {}}],
            final={"layout_id": "layout_saved", "layout": FULL_LAYOUT,
                   "degraded": False, "trace": [], "errors": []},
        )
        async with await _client() as c:
            r = await c.post("/api/v1/layout/parse", json={"image": TINY_PNG})
            await self._wait_done(c, r.json()["data"]["task_id"])

        assert await layout_store.load("layout_saved") is not None

    async def test_结果不含原始图片(self, stub_graph, stub_knowledge):
        """
        结果体**不做全量返回**：state 里有 image_ref（base64，可能上 MB）、
        全部中间产物。全丢给前端既浪费带宽，也把内部结构变成了接口契约。
        """
        stub_graph(
            updates=[{"parse_layout": {}}],
            final={"layout_id": "l", "layout": FULL_LAYOUT,
                   "image_ref": "data:image/png;base64," + "A" * 10000,
                   "plan_bundles": {"x": {"y": "z"}},
                   "degraded": False, "trace": [], "errors": []},
        )
        async with await _client() as c:
            r = await c.post("/api/v1/layout/parse", json={"image": TINY_PNG})
            d = await self._wait_done(c, r.json()["data"]["task_id"])

        assert "image_ref" not in d["result"]
        assert "plan_bundles" not in d["result"]


# ══════════════════════════════════════════════════════════════════
# 优雅停机
# ══════════════════════════════════════════════════════════════════


class TestGracefulShutdown:
    async def test_停机取消在飞任务(self):
        """ADR-13：停机时要能取消在飞任务，且任务落到终态而非悬着。"""
        tm = get_task_manager()

        class _SlowGraph:
            async def astream_events(self, state, cfg, version=None):
                await asyncio.sleep(30)   # 模拟长跑
                yield {"event": "on_chain_start", "name": "parse_layout"}

            async def aget_state(self, cfg):
                class _S:
                    values: dict = {}
                return _S()

        import backend.app.api.tasks as tasks_mod

        original = tasks_mod.get_compiled_graph
        tasks_mod.get_compiled_graph = lambda stages: _SlowGraph()  # type: ignore
        try:
            rec = tm.create("parse")
            from backend.app.graph.state import initial_state

            tm.start(rec, initial_state(task_id=rec.task_id), stages="parse")
            await asyncio.sleep(0.05)
            assert tm.list_running() == [rec.task_id]

            await tm.shutdown(timeout=3)
            assert tm.list_running() == []
            assert rec.status == "failed"
        finally:
            tasks_mod.get_compiled_graph = original  # type: ignore

    async def test_停机后拒绝新任务(self):
        tm = get_task_manager()
        await tm.shutdown(timeout=1)
        with pytest.raises(RuntimeError, match="停机"):
            tm.create("parse")


# ══════════════════════════════════════════════════════════════════
# 三个入口共用一张图的节点表
# ══════════════════════════════════════════════════════════════════


class TestGraphStages:
    def test_三种stages都能编译(self):
        for stages in ("full", "parse", "generate", "review"):
            assert workflow.build_graph(with_checkpointer=False,
                                        stages=stages) is not None  # type: ignore

    def test_review图不需要户型也能跑(self, stub_knowledge, monkeypatch):
        """
        报价单审查**与户型无关** —— 用户上传的是装修公司的报价单，
        不是自己的房子。硬塞进完整图会因缺 layout 而在 A-03 炸掉。

        断言写成**行为**而不是"节点表里有没有 parse_layout"：
        `build_graph` 会把所有 Agent 都注册成节点，只是边不同 ——
        不看边而看节点表，会得出"完整图也在 review 图里"这种错误结论。
        """
        from backend.app.agents.risk_reviewer import RiskReviewAgent
        from backend.app.core.llm_client import LLMResult
        from backend.app.graph.state import initial_state

        class _FakeRiskLLM:
            async def complete_json(self, schema, **kw):
                return schema.model_validate({
                    "summary": "未发现明显问题。", "findings": [],
                    "overall_risk": "low", "negotiation_points": [],
                    "data_gaps": [], "confidence": 0.6,
                }), LLMResult(text="{}", provider="fake", model_used="fake",
                              prompt_tokens=1, completion_tokens=1, elapsed_ms=1)

            async def complete(self, **kw):  # pragma: no cover
                raise AssertionError("不应走纯文本路径")

        monkeypatch.setitem(workflow._AGENTS, "review_risks",
                            RiskReviewAgent(llm=_FakeRiskLLM()))

        g = workflow.build_graph(with_checkpointer=False, stages="review")
        # 只给报价单，**没有任何户型数据**
        out = asyncio.run(g.ainvoke(
            initial_state(task_id="t", quote_text="| 2-1 | 电路改造 | 米 | —— | 15 |")
        ))

        assert out["risk_review"] is not None
        assert [t["agent"] for t in out["trace"]] == ["A-06"]


class TestPhaseWording:
    """
    阶段措辞要跟**用户正在等的那件事**对上。

    需求文档 2.2.4 的核心不是"显示进度"，是「用户看到的是正在做什么」。
    同一个 `review_risks` 节点在两种语境下含义完全不同：
      · 独立跑报价单审查 —— 用户在看一份合同
      · 方案生成链内部审查 —— 用户在等方案

    实测踩过：走报价单审查时，界面全程显示「正在生成装修方案…」。
    用户看的是合同，却被告知系统在生成方案。
    """

    def test_审查任务的措辞不是生成方案(self):
        from backend.app.api.tasks import _phase_for
        from backend.app.core.redis_client import PHASE_TEXT

        phase = _phase_for("review_risks", "review")
        assert phase == "reviewing", f"报价单审查拿到了 {phase!r}"
        assert PHASE_TEXT[phase] == "正在审查报价单…"
        # 最关键的一条：不能说"在生成方案"
        assert "方案" not in PHASE_TEXT[phase]

    def test_方案链内的审查仍然说生成方案(self):
        """同一个节点在 generate 语境下不该被改成"审查报价单"。"""
        from backend.app.api.tasks import _phase_for
        from backend.app.core.redis_client import PHASE_TEXT

        phase = _phase_for("review_risks", "generate")
        assert phase == "planning"
        assert PHASE_TEXT[phase] == "正在生成装修方案…"

    def test_每个节点的每个任务类型都有文案(self):
        """
        穷举一遍，防止将来加了节点却忘了给文案 ——
        那种情况下 `PHASE_TEXT.get(phase, phase)` 会把英文 key 原样吐给前端，
        而需求文档明确告诉前端"不必自己维护映射表"，前端没有任何机会发现。
        """
        from backend.app.api.tasks import _NODE_PHASE, _phase_for
        from backend.app.core.redis_client import PHASE_TEXT

        for node in _NODE_PHASE:
            for kind in ("parse", "generate", "review"):
                phase = _phase_for(node, kind)  # type: ignore[arg-type]
                if phase is None:
                    continue
                assert phase in PHASE_TEXT, f"{node}/{kind} -> 未登记阶段 {phase!r}"
                assert PHASE_TEXT[phase].strip(), f"{phase} 文案为空"

    def test_阶段都有进度参考值(self):
        """`PHASE_PROGRESS` 缺项时前端进度条会跳回 0，比不动更让人困惑。"""
        from backend.app.core.redis_client import PHASE_PROGRESS, PHASE_TEXT

        missing = [p for p in PHASE_TEXT if p not in PHASE_PROGRESS]
        assert not missing, f"以下阶段没有 progress 参考值：{missing}"
