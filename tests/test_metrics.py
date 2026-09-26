"""
性能指标（AC-23）。

本文件守的核心是一句话：**"没采到"不许变成"0"。**

性能报告里一个假的 0 是最危险的那种错 —— 它读起来像"快到测不出来"，
而真相是"根本没数据"。所以 `percentile` / `summarize` 在样本不足时
返回 `None` 而不是 0，接口把它翻成 `available: false` + 一句 `reason`。
下面每一条"没数据"的用例都在钉这个。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.core import metrics


class TestPercentile:
    def test_最近秩不是插值(self):
        """
        ⚠️ P95 必须来自**真实观测到过**的那个数。

        插值算法（numpy 默认的 linear）会算出两个样本之间的值 ——
        性能报告里报一个从未发生过的耗时，是这类工具最容易犯的错。
        """
        values = [float(i) for i in range(1, 101)]     # 1..100
        assert metrics.percentile(values, 95) == 95.0
        assert metrics.percentile(values, 50) == 50.0
        assert metrics.percentile(values, 100) == 100.0

    def test_结果一定是样本里的某个值(self):
        values = [3.1, 7.7, 12.5, 40.2, 99.9]
        for p in (10, 50, 90, 95, 99):
            assert metrics.percentile(values, p) in values

    def test_样本不足时返回None而不是0(self):
        """一条样本算不出分布。返回 0 会被当成"没有耗时"。"""
        assert metrics.percentile([5.0], 95) is None
        assert metrics.percentile([], 95) is None
        assert metrics.summarize([5.0]) is None
        assert metrics.summarize([]) is None

    def test_概要字段齐全(self):
        s = metrics.summarize([1.0, 2.0, 3.0, 4.0, 100.0])
        assert s is not None
        assert s["n"] == 5
        assert s["p50"] == 3.0
        assert s["p95"] == 100.0      # 最近秩：第 5 个
        assert s["max"] == 100.0


class TestRingBuffer:
    def setup_method(self):
        metrics.reset()

    def teardown_method(self):
        metrics.reset()

    def test_记了就能取到(self):
        for ms in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 100.0):
            metrics.record("http", ms)
        snap = metrics.snapshot()
        assert snap["http"]["n"] == 10
        assert snap["http"]["p95"] == 100.0

    def test_环形缓冲有上限(self):
        """
        ⚠️ **有界是刻意的。** 不限长度的话，一个跑几天不重启的进程会把
        样本攒成内存泄漏 —— 而它长得不像泄漏，看起来只是"指标越来越多"。
        """
        for i in range(metrics._RING + 500):
            metrics.record("http", float(i))
        snap = metrics.snapshot()
        assert snap["http"]["n"] == metrics._RING

    def test_按名字分成不同的桶(self):
        metrics.record("http", 10.0)
        metrics.record("svg_render", 5.0)
        metrics.record("http", 20.0)
        metrics.record("svg_render", 15.0)
        snap = metrics.snapshot()
        assert snap["http"]["p50"] == 15.0
        assert snap["svg_render"]["p50"] == 10.0

    def test_snapshot可以只取指定的几个(self):
        # 每桶记两条 —— 只有一条时 summarize 会返回 None（样本不足），
        # 那样这条用例测的就成了"过滤"以外的事
        metrics.record("http", 1.0)
        metrics.record("http", 2.0)
        metrics.record("other", 3.0)
        metrics.record("other", 4.0)
        assert set(metrics.snapshot(["http"])) == {"http"}
        assert set(metrics.snapshot()) == {"http", "other"}


class TestSweepAuditFiles:
    """从审计文件里读长任务耗时。"""

    @staticmethod
    def _write(tmp: Path, records: list[dict], name: str = "app_2026-09-24.log"):
        tmp.mkdir(parents=True, exist_ok=True)
        path = tmp / name
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _task(kind: str, seconds: float, **extra):
        return {"record": {"extra": {
            "audit": True, "event": "task_finished", "kind": kind,
            "elapsed_seconds": seconds, **extra}}}

    @staticmethod
    def _llm(agent: str, ms: int, prompt: int, completion: int):
        return {"record": {"extra": {
            "agent": agent, "elapsed_ms": ms,
            "prompt_tokens": prompt, "completion_tokens": completion}}}

    def test_目录不存在时说清为什么而不是给空指标(self, tmp_path: Path):
        sweep = metrics.sweep_audit_files(tmp_path / "并不存在")
        assert sweep.task_seconds == {}
        assert sweep.errors, "目录不存在必须记进 errors —— 空指标会被读成 0"
        assert "不存在" in sweep.errors[0]

    def test_目录里没有审计文件时也说清(self, tmp_path: Path):
        sweep = metrics.sweep_audit_files(tmp_path)
        assert sweep.errors and "app_*.log" in sweep.errors[0]

    def test_抽出任务耗时与LLM耗时(self, tmp_path: Path):
        self._write(tmp_path, [
            self._task("parse", 39.2),
            self._task("generate", 110.3, degraded=False),
            self._task("parse", 41.0),
            self._llm("A-04", 7000, 1200, 800),
            self._llm("A-06", 40700, 7000, 1600),
            {"record": {"extra": {"event": "login", "username": "admin"}}},
        ])
        sweep = metrics.sweep_audit_files(tmp_path)

        assert sorted(sweep.task_seconds) == ["generate", "parse"]
        assert sweep.task_seconds["parse"] == [39.2, 41.0]
        assert sweep.llm_ms["A-06"] == [40700.0]
        assert sweep.llm_tokens["A-06"] == [8600]
        assert sweep.lines == 6

    def test_坏行跳过而不是整个失败(self, tmp_path: Path):
        """进程被杀时可能留下半行 JSON —— 一个坏行不该让指标接口整体失败。"""
        tmp_path.mkdir(parents=True, exist_ok=True)
        bad_lines = "\n".join([
            json.dumps(self._task("parse", 30.0)),
            '{"record": {"extra": {"event": "task_fi',      # 半行 JSON
            json.dumps(self._task("parse", 50.0)),
        ])
        (tmp_path / "app_2026-09-24.log").write_text(bad_lines + "\n",
                                                     encoding="utf-8")
        sweep = metrics.sweep_audit_files(tmp_path)
        assert sweep.task_seconds["parse"] == [30.0, 50.0]
        assert not sweep.errors

    def test_只读最近N天(self, tmp_path: Path):
        self._write(tmp_path, [self._task("parse", 1.0)], "app_2026-09-22.log")
        self._write(tmp_path, [self._task("parse", 2.0)], "app_2026-09-23.log")
        self._write(tmp_path, [self._task("parse", 3.0)], "app_2026-09-24.log")

        assert metrics.sweep_audit_files(tmp_path).task_seconds["parse"] == [3.0]
        two = metrics.sweep_audit_files(tmp_path, days=2)
        assert two.task_seconds["parse"] == [2.0, 3.0]
        assert two.files == ["app_2026-09-23.log", "app_2026-09-24.log"]


class TestMetricsEndpoint:
    """接口层：`available: false` + `reason`，而不是 0。"""

    async def test_没有数据时如实说没有(self, monkeypatch, tmp_path: Path):
        metrics.reset()
        monkeypatch.setattr(
            "backend.app.api.routes.settings.LOG_DIR", tmp_path / "空的"
        )

        from tests.test_api import _client

        async with await _client() as c:
            r = await c.get("/api/v1/system/metrics")
        body = r.json()

        assert r.status_code == 200 and body["code"] == 0
        data = body["data"]

        # ⚠️ 四项都必须 available=False **且带 reason** —— 不能是 0
        for key in ("parse", "generate", "http", "svg_render"):
            m = data["metrics"][key]
            assert m["available"] is False, f"{key} 没有数据却报成可用"
            assert m["reason"], f"{key} 没数据却不给原因"
            assert "p95" not in m, f"{key} 没数据却报了 p95 —— 那就是一个编的数"
        assert data["notes"], "没采到的原因要写在 notes 里让调用方看见"

    async def test_有数据时算出达标与否(self, monkeypatch, tmp_path: Path):
        metrics.reset()
        # 造一份审计文件：两次解析（20s / 40s）、一次生成（50s）
        tmp_path.mkdir(parents=True, exist_ok=True)
        records = [
            {"record": {"extra": {"event": "task_finished", "kind": "parse",
                                  "elapsed_seconds": 20.0}}},
            {"record": {"extra": {"event": "task_finished", "kind": "parse",
                                  "elapsed_seconds": 40.0}}},
            {"record": {"extra": {"event": "task_finished", "kind": "generate",
                                  "elapsed_seconds": 50.0, "degraded": False}}},
            # ⚠️ 每类都要 ≥2 条：一条样本算不出分布，summarize 会返回 None，
            #    接口就报 available=False —— 那是**正确**行为，
            #    但这条用例要测的是"有数据时怎么算"
            {"record": {"extra": {"event": "task_finished", "kind": "generate",
                                  "elapsed_seconds": 58.0, "degraded": True}}},
            {"record": {"extra": {"agent": "A-04", "elapsed_ms": 7000,
                                  "prompt_tokens": 100, "completion_tokens": 50}}},
            {"record": {"extra": {"agent": "A-04", "elapsed_ms": 9000,
                                  "prompt_tokens": 200, "completion_tokens": 100}}},
        ]
        (tmp_path / "app_2026-09-24.log").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

        for _ in range(4):
            metrics.record("http", 12.0)
        metrics.record("svg_render", 4.0)
        metrics.record("svg_render", 6.0)

        monkeypatch.setattr("backend.app.api.routes.settings.LOG_DIR", tmp_path)

        from tests.test_api import _client

        async with await _client() as c:
            r = await c.get("/api/v1/system/metrics")
        data = r.json()["data"]

        parse = data["metrics"]["parse"]
        assert parse["available"] and parse["n"] == 2
        assert parse["p95"] == 40.0
        # P95 = 40s，而 2.3.1 的目标是 < 20s → **必须如实说没达标**
        assert parse["target_p95"] == 20
        assert parse["meets_target"] is False

        gen = data["metrics"]["generate"]
        assert gen["available"] and gen["n"] == 2
        assert gen["p95"] == 58.0
        assert gen["meets_target"] is True, "58s < 60s 的生成目标，应当达标"

        assert data["metrics"]["http"]["available"] is True
        assert data["metrics"]["svg_render"]["available"] is True
        assert data["llm_by_agent"]["A-04"]["p95"] == 9000.0
        assert data["llm_by_agent"]["A-04"]["avg_total_tokens"] == 225

    async def test_days超出范围时拒绝(self):
        from tests.test_api import _client

        async with await _client() as c:
            for bad in (0, -1, 31, 999):
                r = await c.get(f"/api/v1/system/metrics?days={bad}")
                assert r.status_code == 200, "业务失败不能用 4xx"
                assert r.json()["code"] == 4001

    async def test_需要登录(self, monkeypatch, tmp_path: Path):
        """
        ⚠️ `/system/health` 公开，`/system/metrics` 不公开 —— 有意区别。

        指标里带 `running_tasks`（在飞任务 id），而 AC-31 之后 task_id
        本身就是"能取消那个任务"的凭据。
        """
        from tests.test_api import _client_with

        async with await _client_with(None) as anon:
            r = await anon.get("/api/v1/system/metrics")
        assert r.json()["code"] == 4003


class TestHotPathWiring:
    """热路径的埋点真的接上了（HTTP 中间件 / 渲染入口）。"""

    def setup_method(self):
        metrics.reset()

    def teardown_method(self):
        metrics.reset()

    async def test_每次请求都记进环形缓冲(self, tmp_path: Path):
        from tests.test_api import _client

        async with await _client() as c:
            await c.get("/api/v1/system/health")
            await c.get("/api/v1/system/health")
            await c.get("/")

        snap = metrics.snapshot(["http"])
        assert snap, "HTTP 耗时没有被记录 —— 中间件的埋点断了"
        assert snap["http"]["n"] >= 3

    async def test_渲染入口自己计时(self):
        """
        计时点放在 `render_plan_for` 里而不是三个调用方 ——
        漏掉一个的表现是"这项指标时有时无"，很难发现。
        """
        from backend.app.services.render import render_plan_for

        layout = {
            "layout_id": "l", "mode": "full",
            "rooms": [{"name": "客厅", "type": "living_room", "area": 28.5,
                       "bbox": [120, 80, 420, 360]}],
            "walls": [{"type": "load_bearing", "coords": [[100, 60], [700, 60]]}],
            "doors": [{"position": [250, 380], "width": 0.9}],
            "windows": [{"position": [600, 50], "width": 1.8,
                         "orientation": "south"}],
            "total_area": 28.5, "has_north_arrow": True, "confidence": 0.85,
        }
        for _ in range(3):
            render_plan_for(layout)

        snap = metrics.snapshot(["svg_render"])
        assert snap and snap["svg_render"]["n"] == 3
        # 实测这份最小户型是毫秒级；给一个宽松上限，防的是"计时单位写错"
        assert snap["svg_render"]["p95"] < 3000
