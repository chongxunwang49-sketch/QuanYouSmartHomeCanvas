"""
检查点与启动接管（AC-12）。

═══════════════════════════════════════════════════════════════════
这条验收此前**连前提都不成立**
═══════════════════════════════════════════════════════════════════
需求写的是「Redis 会话恢复」，而 `langgraph-checkpoint-redis` 依赖
RediSearch —— 启动要发 `FT.INFO`，而 compose 钉死的 `redis:7-alpine`
**没有任何模块**：

    redisvl.exceptions.RedisSearchError: unknown command 'FT.INFO'

换 `redis/redis-stack-server` 能解但镜像明显更重，而 Docker VM 余量本就
紧张。改用旁边的 `qy-postgres`（一直在跑、完全空闲）。
⚠️ 这是对字面口径的偏离，语义没变，已记 V2.4 修订。

═══════════════════════════════════════════════════════════════════
为什么"崩溃续跑"那条必须用子进程
═══════════════════════════════════════════════════════════════════
两个原因，缺一不可：

① **要真的崩。** 用 `os._exit()` 硬退，不走任何清理钩子 —— 这才是断电 /
   OOM / SIGKILL 的样子。在同一个进程里"模拟中断"是测不出东西的：
   真正的风险是"进程没了，状态还在不在"。

② **Windows 的事件循环。** `psycopg` 的异步模式不能用默认的
   `ProactorEventLoop`（`Psycopg cannot use the 'ProactorEventLoop'`），
   要切 Selector。而切策略必须在**建循环之前**，且**不能全局切** ——
   `tests/test_mcp_servers.py` 要起子进程，而 `create_subprocess_*`
   在 Windows 的 Selector 循环上不支持。所以策略只在子进程里切。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.core.config import settings
from backend.app.core.redis_client import RedisClient, TaskProgressStore

ROOT = Path(__file__).resolve().parents[1]

#: 子进程脚本。`mode=crash` 跑到第二个节点就硬退；`mode=resume` 从检查点续跑。
#: 每个节点执行时往 `QY_TRACE` 追加一行 —— 父进程靠它判断"有没有重跑"。
_CHILD = r"""
import asyncio, os, sys
sys.path.insert(0, os.environ["QY_ROOT"])
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from typing import Annotated, TypedDict
from operator import add
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

TRACE = os.environ["QY_TRACE"]
DSN = os.environ["QY_DSN"]
THREAD = os.environ["QY_THREAD"]
MODE = os.environ["QY_MODE"]

class S(TypedDict, total=False):
    log: Annotated[list, add]

def mk(tag):
    def f(state):
        with open(TRACE, "a", encoding="utf-8") as fh:
            fh.write(tag + "\n")
        return {"log": [tag]}
    return f

builder = StateGraph(S)
for name in ("n1", "n2", "n3", "n4"):
    builder.add_node(name, mk(name))
builder.add_edge(START, "n1")
builder.add_edge("n1", "n2")
builder.add_edge("n2", "n3")
builder.add_edge("n3", "n4")
builder.add_edge("n4", END)

async def main():
    async with AsyncPostgresSaver.from_conn_string(DSN) as saver:
        await saver.setup()
        graph = builder.compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": THREAD}}
        if MODE == "crash":
            async for ev in graph.astream_events({}, cfg, version="v2"):
                if ev.get("event") == "on_chain_start" and ev.get("name") == "n3":
                    break
            # ⚠️ 硬退：不走 atexit / 不关连接 / 不 flush —— 断电长这样
            sys.stdout.flush()
            os._exit(9)
        else:
            snap = await graph.ainvoke(None, cfg)
            with open(TRACE + ".final", "w", encoding="utf-8") as fh:
                fh.write(json.dumps(snap))
            print(json.dumps(snap))

import json
asyncio.run(main())
"""


def _env(tmp: Path, thread: str, mode: str) -> dict[str, str]:
    return {
        **os.environ,
        "QY_ROOT": str(ROOT),
        "QY_TRACE": str(tmp / "trace.txt"),
        "QY_TRACE_FINAL": str(tmp / "trace.txt.final"),
        "QY_DSN": settings.checkpoint_dsn,
        "QY_THREAD": thread,
        "QY_MODE": mode,
        "PYTHONIOENCODING": "utf-8",
    }


@pytest.fixture(autouse=True)
def _need_postgres():
    """
    PG 不在就跳过 —— 而不是失败。它是可选依赖（不在就降级内存版）。

    ⚠️ 驱动本身缺席时同样要**跳过**，所以走 `importorskip` ——
    裸 import 会把它变成 ERROR，而"红着的原因与代码无关"这件事
    看久了就会被当成常态（`tests/test_db.py` 里踩的是同一个坑）。
    """
    psycopg = pytest.importorskip(
        "psycopg", reason="本地环境没有 psycopg3（容器里有），跳过检查点用例"
    )

    try:
        with psycopg.connect(settings.checkpoint_dsn, connect_timeout=3):
            pass
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用（{type(e).__name__}），跳过检查点用例")


class TestCrashAndResume:
    """
    AC-12 的核心：**崩了之后能续上，而且已完成的节点不重跑。**

    "不重跑"是这条验收真正的价值所在 —— 重跑意味着白花一次 LLM 调用的钱
    和几十秒等待。只断言"最终能跑完"是不够的：那用"从头再跑一遍"也能满足。
    """

    def test_崩溃后从检查点续跑且不重跑已完成节点(self, tmp_path: Path):
        trace = tmp_path / "trace.txt"
        thread = f"pytest-ac12-{os.getpid()}"

        # ① 跑到 n2 之后硬退
        crash = subprocess.run(
            [sys.executable, "-c", _CHILD], env=_env(tmp_path, thread, "crash"),
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        assert crash.returncode == 9, (
            f"子进程没有按预期硬退（rc={crash.returncode}）：\n"
            f"STDOUT={crash.stdout[-1500:]}\nSTDERR={crash.stderr[-2000:]}"
        )
        first = trace.read_text(encoding="utf-8").split()
        assert first == ["n1", "n2"], f"崩溃前应当只跑过 n1/n2，实际 {first}"

        # ② 另起一个进程，从检查点续跑
        resume = subprocess.run(
            [sys.executable, "-c", _CHILD], env=_env(tmp_path, thread, "resume"),
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        assert resume.returncode == 0, (
            f"续跑失败：\nSTDOUT={resume.stdout[-1500:]}\nSTDERR={resume.stderr[-2500:]}"
        )

        all_runs = trace.read_text(encoding="utf-8").split()
        assert all_runs == ["n1", "n2", "n3", "n4"], (
            f"续跑后各节点的执行序列是 {all_runs} —— "
            f"期望 n1/n2 各一次（不重跑）+ n3/n4 补齐"
        )
        assert all_runs.count("n1") == 1 and all_runs.count("n2") == 1, (
            "已完成的节点被重跑了 —— 那就等于从头再来，检查点白开"
        )

        # ③ 最终状态是完整的（两个进程各跑一半，合起来才是全部产物）
        final = json.loads((tmp_path / "trace.txt.final").read_text(encoding="utf-8"))
        assert final["log"] == ["n1", "n2", "n3", "n4"], (
            f"续跑后的状态不完整：{final} —— "
            f"说明跨进程的状态合并是坏的（这正是 AC-12 要证明的事）"
        )


class TestUnfinishedScan:
    """启动接管的第一半：**悬挂记录能被扫出来。** 扫不出来就什么都谈不上。"""

    async def _store(self) -> TaskProgressStore:
        client = RedisClient()
        if not await client.ping():
            pytest.skip("Redis 不可用，跳过启动接管用例")
        return TaskProgressStore(client)

    async def test_只捞没跑完的(self):
        store = await self._store()
        marker = f"pytest_orphan_{os.getpid()}"

        await store.update(f"{marker}_running", kind="parse", status="processing",
                           phase="analyzing", progress=2)
        await store.update(f"{marker}_done", kind="parse", status="completed",
                           phase="done", progress=100)
        await store.update(f"{marker}_failed", kind="parse", status="failed",
                           phase="analyzing", progress=2)

        found = {t["task_id"] for t in await store.find_unfinished()}
        mine = {t for t in found if t.startswith(marker)}

        assert f"{marker}_running" in mine, "没跑完的任务没被扫出来"
        assert f"{marker}_done" not in mine, "已完成的任务不该被当成接管对象"
        assert f"{marker}_failed" not in mine, "已落终态的任务不该被重复处理"

    async def test_扫出来的记录带着续跑要用的字段(self):
        """
        `kind` / `started_at` / `user_id` 必须一起捞出来 ——
        启动接管要靠它们重建 TaskRecord。缺 `kind` 就不知道该用哪张图。
        """
        store = await self._store()
        marker = f"pytest_orphan_fields_{os.getpid()}"
        await store.update(marker, kind="generate", status="processing",
                           phase="planning", progress=13,
                           started_at=1000.5, phase_entered_at=1010.5,
                           user_id=7, trace_id="trace-abc")

        row = next(t for t in await store.find_unfinished() if t["task_id"] == marker)
        assert row["kind"] == "generate"
        assert row["user_id"] == 7
        assert row["started_at"] == 1000.5
        assert row["trace_id"] == "trace-abc"
