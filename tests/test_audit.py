"""
审计日志（AC-14）。

═══════════════════════════════════════════════════════════════════
这条验收在 2026-09-24 之前是**假的**
═══════════════════════════════════════════════════════════════════
`core/logger.py` 里写 JSON 审计文件的 `log_dir` 分支**一直存在**，
注释还写着"供审计（AC-14）"，但——**全项目无一处以 `log_dir=` 调用它**。
那条分支从未被激活，`logs/` 里从来没出现过 `app_*.log`。

所以本文件最重要的一条不是"字段对不对"，而是**"文件到底有没有产生"**。
那件事只有在真的起一次进程、真的配一次、真的读文件之后才成立 ——
在测试进程里做不到（`setup_logging` 有幂等保护，而且它一跑就会
`logger.remove()` 掉测试自己的 sink）。因此下面有一条**子进程用例**。

═══════════════════════════════════════════════════════════════════
为什么审计事件是结构化字段，不是拼好的中文句子
═══════════════════════════════════════════════════════════════════
AC-14 的原文是"可查询"。查询靠字段：想回答"昨天有多少次生成是降级的"，
就得能按 `degraded=true` 过滤。把同样的信息拼成一句话再让下游正则，
等于把结构化的工作推给每一个查询方，而且拼串格式会随人改。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.core.logger import audit, with_trace_id

#: 子进程里跑的脚本。**用子进程是必须的**，理由见模块头。
#: 它走完整链路：LOG_DIR 环境变量 → settings → lifespan 里 setup_logging
#: → 真的登录一次 → 退出。父进程再去读文件。
_CHILD = r"""
import asyncio, os, sys
sys.path.insert(0, os.environ["QY_ROOT"])
import httpx
from backend.app.core.config import settings
from backend.app.main import create_app

async def main():
    app = create_app()
    # ⚠️ 手动跑 lifespan —— ASGITransport **不会**触发 startup/shutdown，
    #    而 setup_logging 正是在 lifespan 里被激活的。
    from backend.app.main import lifespan
    async with lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/auth/login",
                             json={"username": "admin", "password": "admin123"})
            assert r.json()["code"] == 0, r.text
            r = await c.post("/api/v1/auth/login",
                             json={"username": "admin", "password": "错的"})
            assert r.json()["code"] == 4001, r.text
    print("OK")

asyncio.run(main())
"""


class TestAuditEventShape:
    """`audit()` 本身的契约：字段进 extra，不是拼进消息。"""

    def test_字段进extra而不是消息(self):
        import loguru

        seen: list[dict] = []
        sink_id = loguru.logger.add(lambda m: seen.append(m.record), level="INFO")
        try:
            audit("task_finished", task_id="t1", kind="generate", degraded=True)
        finally:
            loguru.logger.remove(sink_id)

        assert seen, "审计事件没有写出去"
        extra = seen[-1]["extra"]
        assert extra["audit"] is True
        assert extra["event"] == "task_finished"
        # 查询方要按这些字段过滤，所以它们必须是字段而不是句子的一部分
        assert extra["task_id"] == "t1"
        assert extra["kind"] == "generate"
        assert extra["degraded"] is True

    def test_带上当前trace_id(self):
        """
        AC-30 要求"同一请求在 API 响应、审计日志、LLM 调用日志中 trace_id 一致"。
        审计这一腿靠的就是 loguru 的 contextualize 自动补 extra ——
        这里钉住"它真的补上了"，而不是假设。
        """
        import loguru

        seen: list[dict] = []
        sink_id = loguru.logger.add(lambda m: seen.append(m.record), level="INFO")
        try:
            with with_trace_id("trace-audit-001"):
                audit("login", username="admin")
        finally:
            loguru.logger.remove(sink_id)

        assert seen[-1]["extra"]["trace_id"] == "trace-audit-001"


class TestAuditFileReallyAppears:
    """
    ⚠️ **AC-14 的核心那条：文件真的会产生。**

    这条以前无法成立 —— `setup_logging(log_dir=...)` 全项目没人调用。
    """

    def test_走完整链路后审计文件里能查到登录事件(self, tmp_path: Path):
        log_dir = tmp_path / "audit"
        env = {
            **os.environ,
            "QY_ROOT": str(Path(__file__).resolve().parents[1]),
            "LOG_DIR": str(log_dir),
            "PYTHONIOENCODING": "utf-8",
        }
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD],
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        assert proc.returncode == 0, (
            f"子进程失败：\nSTDOUT={proc.stdout[-2000:]}\nSTDERR={proc.stderr[-3000:]}"
        )

        files = sorted(log_dir.glob("app_*.log"))
        assert files, (
            f"审计文件没有产生（{log_dir} 下有：{list(log_dir.iterdir())}）—— "
            f"说明 setup_logging 又没被以 log_dir 调用"
        )

        lines = [json.loads(ln) for ln in
                 files[0].read_text(encoding="utf-8").splitlines() if ln.strip()]
        events = {ln["record"]["extra"].get("event") for ln in lines}

        assert "login" in events, f"审计文件里没有登录事件，实际有：{events}"
        assert "login_failed" in events, (
            f"登录失败没有被审计 —— 那恰恰是最需要能查的一类，实际：{events}"
        )

        # 逐条检查登录事件的字段，顺带确认**口令没有被写进去**
        login = next(ln for ln in lines
                     if ln["record"]["extra"].get("event") == "login")
        extra = login["record"]["extra"]
        assert extra["username"] == "admin"
        assert extra["role"] in ("admin", "designer", "user")
        assert extra["trace_id"], "审计事件没带 trace_id（AC-30 要三处一致）"

        raw = files[0].read_text(encoding="utf-8")
        assert "admin123" not in raw, (
            "口令被写进了审计文件 —— 它会落盘 180 天。"
            "setup_logging 里 diagnose=False 正是为了防这个"
        )


class TestAuditCallSites:
    """
    三个被点名的操作（登录 / 解析 / 生成）在代码里真的发了审计事件。

    这里只断言"事件发出去了"，文件那条链路由上面那组守 ——
    分成两层是因为**失败原因不同**：一种是"忘了调用"，另一种是"配错了".
    """

    @staticmethod
    def _capture():
        import loguru

        seen: list[dict] = []
        sink_id = loguru.logger.add(lambda m: seen.append(m.record), level="INFO")
        return seen, sink_id

    async def test_登录成功与失败都发事件(self):
        import loguru

        from tests.test_api import _client

        seen, sink_id = self._capture()
        try:
            async with await _client() as c:
                await c.post("/api/v1/auth/login",
                             json={"username": "admin", "password": "admin123"})
                await c.post("/api/v1/auth/login",
                             json={"username": "admin", "password": "错的"})
        finally:
            loguru.logger.remove(sink_id)

        events = [r["extra"].get("event") for r in seen]
        assert "login" in events
        assert "login_failed" in events

    async def test_建任务与任务结束都发事件(self, stub_graph):
        import loguru

        from tests.test_api import _client, TINY_PNG

        stub_graph(updates=[{"parse_layout": {}}],
                   final={"layout_id": "l", "degraded": True})

        seen, sink_id = self._capture()
        try:
            async with await _client() as c:
                r = await c.post("/api/v1/layout/parse",
                                 json={"image": TINY_PNG,
                                       "image_media_type": "image/png"})
                tid = r.json()["data"]["task_id"]
                import asyncio
                for _ in range(100):
                    d = (await c.get(f"/api/v1/task/{tid}/status")).json()["data"]
                    if d["status"] in ("completed", "failed"):
                        break
                    await asyncio.sleep(0.02)
        finally:
            loguru.logger.remove(sink_id)

        created = [r["extra"] for r in seen if r["extra"].get("event") == "task_created"]
        finished = [r["extra"] for r in seen if r["extra"].get("event") == "task_finished"]

        assert created and created[0]["kind"] == "parse"
        assert created[0]["task_id"] == tid
        assert finished, "任务结束没有发审计事件"
        # ⚠️ AC-14 明确要求含 degraded 字段；它也是 AC-17「三处可见」的第三处
        assert finished[0]["degraded"] is True
        assert finished[0]["status"] == "completed"


class TestLogDirConfig:
    def test_默认落在仓库根的logs且被gitignore(self):
        """
        审计日志含用户操作与 trace_id —— **不该进公开仓库**。
        这条断言的是"默认值指向的位置在 .gitignore 里"，
        免得哪天有人把它改成 `data/audit/` 而那里没被忽略。
        """
        import subprocess as sp

        from backend.app.core.config import PROJECT_ROOT, settings

        assert settings.LOG_DIR == PROJECT_ROOT / "logs"
        r = sp.run(["git", "check-ignore", str(settings.LOG_DIR)],
                   cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        assert r.returncode == 0, (
            f"{settings.LOG_DIR} 没有被 .gitignore 忽略 —— 审计日志会被提交"
        )
