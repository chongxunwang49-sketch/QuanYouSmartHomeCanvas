"""
本机开发用的服务启动器（**Windows 必须用它**）。

    python scripts/run_server.py                 # 默认 127.0.0.1:8000
    python scripts/run_server.py --port 8001

═══════════════════════════════════════════════════════════════════
为什么需要这个文件（不能直接 `uvicorn backend.app.main:app`）
═══════════════════════════════════════════════════════════════════
AC-12 的检查点用 **PostgreSQL**（`AsyncPostgresSaver`），而 `psycopg` 的
异步模式**不支持 Windows 默认的 Proactor 事件循环**，连接时直接抛：

    psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop'
    to run in async mode.

要换事件循环，只能**在事件循环被创建之前**换 —— 而
`uvicorn backend.app.main:app` 这个写法里，应用的 import 发生在
`asyncio.run()` **内部**（uvicorn 在 `serve()` 里才 load 应用），
所以写在 `main.py` 里的任何 `set_event_loop_policy` 都已经晚了。
`uvicorn.run(app)` 同样晚：它自己 `asyncio.run()`。

**必须由调用方在 `uvicorn.run()` 之前切好策略**，那就是本文件的全部作用。

═══════════════════════════════════════════════════════════════════
两个"知道就好"的点
═══════════════════════════════════════════════════════════════════
① **容器里不受影响。** Docker 里是 Linux，事件循环是 epoll，
   psycopg 异步开箱可用 —— 所以 `deploy/Dockerfile` 的 CMD 保持
   `uvicorn ...` 不变，本文件只服务本机开发。

② **为什么不在测试里也切策略。** 测试跑在 Proactor 上，而
   `tests/test_mcp_servers.py` 要起子进程（MCP 走 stdio），
   `asyncio.create_subprocess_*` 在 Windows 的 Selector 循环上
   **不支持**（NotImplementedError）。全切会打断 MCP 那组用例。
   所以策略只在**服务进程**里切；检查点相关的用例自己起子进程 +
   显式切换（见 `tests/test_checkpoint.py`）。

   一句话：**这条约束必须在"起服务"这一层解决，不能在库或应用里解决。**
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _force_selector_loop_on_windows() -> bool:
    """
    把 Windows 的事件循环换成 Selector。返回是否真的换了。

    ⚠️ 必须在 `uvicorn.run()` **之前**调用（见模块说明）。
    """
    if sys.platform != "win32":
        return False
    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is None:      # 非 Windows 或未来版本没有这个类
        return False
    asyncio.set_event_loop_policy(policy())
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="本机开发服务启动器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="改动自动重启（会慢一些）")
    args = parser.parse_args()

    switched = _force_selector_loop_on_windows()

    import uvicorn

    from backend.app.core.config import settings

    print(
        f"[run_server] 事件循环：{'WindowsSelector' if switched else '默认'}"
        f"（psycopg 异步模式要求非 Proactor；容器里是 Linux，不受影响）"
    )
    print(f"[run_server] 检查点："
          f"{'启用（PostgreSQL）' if settings.ENABLE_CHECKPOINTER else '关闭'}"
          f" → {settings.checkpoint_dsn.split('@')[-1]}")

    # ⚠️ `reload=True` 时 uvicorn 会**另起一个子进程**跑应用，而策略是
    #    进程级设置 —— 子进程不继承。所以带 --reload 时这里必须显式提示，
    #    免得有人开了热重载之后发现检查点又连不上了，还以为是配置问题。
    if args.reload and switched:
        print("[run_server] ⚠️ --reload 下应用跑在子进程里，需要子进程也切策略；"
              "本启动器通过 uvicorn 的 reload 钩子做不到这一点。"
              "调试检查点请先不要开 --reload。")

    uvicorn.run(
        "backend.app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
