"""
MCP 客户端封装。

实测记录（mcp SDK 2.2.0）：
- 服务端：`FastMCP` 已更名为 `MCPServer`（from mcp.server.mcpserver import MCPServer），
  用 `@server.tool()` 装饰器注册，`server.run(transport="stdio")` 启动。
- 客户端：`ClientSession` 提供 `list_tools()` / `call_tool()`，
  配合 `stdio_client()` 异步上下文管理器使用；`read_timeout_seconds` 控制单次调用超时。

⚠️ 本机 Windows 注意：子进程必须以 **UTF-8** 启动，否则 MCP 的 JSON-RPC 报文
   在 GBK 默认编码下会乱码。故 StdioServerParameters 显式注入 PYTHONUTF8=1。
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from loguru import logger

from .config import settings

# MCP Server 所在目录（项目根/mcp_servers）
MCP_SERVERS_DIR = settings.PROJECT_ROOT / "mcp_servers"


class MCPToolError(RuntimeError):
    """MCP 工具调用失败。"""


class MCPClient:
    """
    MCP 工具调用客户端。

    每次 call_tool 起一个 stdio 子进程（简单、隔离性好）。
    若后续需要高频调用，可改为长驻会话 + 复用 ClientSession。
    """

    def __init__(self, python_executable: str | None = None) -> None:
        # 默认复用当前解释器，保证依赖一致
        self.python = python_executable or sys.executable

    def _server_params(self, module: str, *, trace_id: str = ""):
        """
        ⚠️ `trace_id` 通过**子进程环境变量**传下去，不走工具入参。

        为什么不塞进 `arguments`：那会污染工具自己的入参 schema（每个 Server
        都要在业务参数里多认一个跟业务无关的字段），而且**参数是要被校验的** ——
        多一个字段就是多一处会因为校验失败而报错的地方。

        环境变量是子进程本来就带着的东西，Server 侧想记就记（
        `mcp_servers/*` 可以用 `os.environ.get("QY_TRACE_ID")`），
        不想记也完全没有副作用。

        ⚠️ 这是 **AC-30「同一请求在 API 响应、审计日志、LLM 调用日志中
        trace_id 一致」在 MCP 这一段上的补齐**：在此之前 `mcp_client.py`
        全文没有任何 `trace_id` 引用，MCP 子进程里发生的事在链路追踪上是
        断开的 —— 主链一路带着 trace，到了工具调用这里就没了。
        """
        from mcp.client.stdio import StdioServerParameters

        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(settings.PROJECT_ROOT),
        }
        if trace_id:
            env["QY_TRACE_ID"] = trace_id
        return StdioServerParameters(
            command=self.python,
            args=["-m", f"mcp_servers.{module}"],
            env=env,
            cwd=str(settings.PROJECT_ROOT),
        )

    @asynccontextmanager
    async def _raw_session(self, module: str, timeout: float = 60.0,
                           *, trace_id: str = ""):
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        params = self._server_params(module, trace_id=trace_id)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                await session.initialize()
                yield session

    @asynccontextmanager
    async def _session(self, module: str, timeout: float = 60.0,
                       *, trace_id: str = ""):
        """
        在 `_raw_session` 外面再包一层，**把 anyio 的 ExceptionGroup 拆开**。

        ⚠️ 这一步是实测补上的（2026-09-24），不是洁癖。

        `stdio_client` 内部用 anyio 的 TaskGroup。从会话体里抛出的任何异常，
        在退出上下文时都会被收集、重新抛成一个 `BaseExceptionGroup`。
        后果是：调用方写 `except MCPToolError` **永远接不住** —— 拿到的是
        `ExceptionGroup`，而 `MCPToolError` 只躺在它的 `.exceptions` 里。

        也就是说，`MCPToolError` 这个类型存在的全部意义（"让调用方能区分
        '工具报错' 和 '进程挂了'"）在修复前是落空的。测试里就是这么发现的：
        工具明明抛了 `ToolError`、客户端明明把它转成了 `MCPToolError`，
        而 `pytest.raises(MCPToolError)` 却报 DID NOT RAISE。
        """
        try:
            async with self._raw_session(module, timeout=timeout,
                                         trace_id=trace_id) as session:
                yield session
        except BaseExceptionGroup as eg:
            raise _unwrap_group(eg) from None

    # ── 公开 API ──────────────────────────────────────────

    async def list_tools(self, module: str) -> list[dict[str, Any]]:
        """列出一个 MCP Server 暴露的工具。"""
        async with self._session(module) as session:
            resp = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": t.description,
                    # mcp 2.x 由 inputSchema 更名为 input_schema（snake_case），
                    # 这里两种都兼容，避免 SDK 小版本来回横跳时炸掉
                    "schema": getattr(t, "input_schema", None)
                    or getattr(t, "inputSchema", None),
                }
                for t in resp.tools
            ]

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any], *, module: str | None = None,
        timeout: float = 60.0, trace_id: str | None = None,
    ) -> Any:
        """
        调用 MCP 工具。

        module 缺省时按工具名前缀推断（如 parse_house_layout -> parse_house_layout）。

        `trace_id` 不给就取**当前上下文里的那个**（`logger.current_trace_id()`）——
        调用方通常不需要显式传：主链已经把它绑在上下文里了。
        它会被写进本进程的日志，并通过环境变量传给子进程（见 `_server_params`）。
        """
        from .logger import current_trace_id

        module = module or tool_name
        tid = trace_id if trace_id is not None else current_trace_id()
        logger.debug(
            f"[MCP] 调用 {module}.{tool_name} "
            f"args={json.dumps(arguments, ensure_ascii=False)[:200]}"
            + (f" trace={tid[:8]}" if tid and tid != "-" else "")
        )

        async with self._session(module, timeout=timeout, trace_id=tid) as session:
            result = await session.call_tool(tool_name, arguments)

            # ⚠️ **两个拼法都要认，这里是实测补上的（2026-09-24）。**
            #
            # SDK 2.x 把 `CallToolResult` 的字段从 camelCase 改成了 snake_case。
            # 上面 `list_tools` 已经为同一场改名做了兼容（`input_schema` /
            # `inputSchema`），但**漏了这里** —— `getattr(result, "isError", False)`
            # 在 2.x 上永远取到默认值 False，于是"工具调用失败"被当成成功返回：
            # 调用方拿到的是字符串 `"Error executing tool calc_budget"`，
            # 而它看起来就像一份正常结果。
            #
            # 这正是本项目一路在防的那类失败：**失败看起来像成功**。
            # 实测（mcp 2.2.0）：成功 → `is_error=False` + `structured_content`；
            # 失败 → `is_error=True` + `structured_content=None`。
            failed = bool(
                getattr(result, "is_error", None)
                or getattr(result, "isError", False)
            )
            if failed:
                raise MCPToolError(
                    f"MCP 工具 {tool_name} 返回错误: {_texts(result)}"
                )

            # SDK 2.x：结构化结果在 structuredContent，纯文本在 content
            structured = (
                getattr(result, "structured_content", None)
                if getattr(result, "structured_content", None) is not None
                else getattr(result, "structuredContent", None)
            )
            if structured is not None:
                return structured

            payload = _texts(result)
            try:
                return json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                return payload


def _texts(result: Any) -> str:
    """把 MCP 返回的 content 列表拼成字符串。"""
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _unwrap_group(exc: BaseException) -> BaseException:
    """
    从异常组里挑出**真正该抛出去的那一个**。

    优先找 `MCPToolError`：它代表"工具真的执行了、但拒绝了这次调用"，
    是调用方最需要看到的结论。找不到就退回组里第一个叶子 ——
    宁可抛一个原始异常（比如子进程起不来），也不要抛一个把原因藏在
    `.exceptions` 里的 `BaseExceptionGroup`。

    递归是必要的：TaskGroup 会嵌套（stdio → ClientSession 各一层），
    实测拿到的是两层嵌套的组。
    """
    if not isinstance(exc, BaseExceptionGroup):
        return exc

    def leaves(e: BaseException):
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                yield from leaves(sub)
        else:
            yield e

    found = list(leaves(exc))
    for e in found:
        if isinstance(e, MCPToolError):
            return e
    return found[0] if found else exc


_client: MCPClient | None = None


def get_mcp_client() -> MCPClient:
    global _client
    if _client is None:
        _client = MCPClient()
    return _client


__all__ = ["MCPClient", "MCPToolError", "get_mcp_client", "MCP_SERVERS_DIR"]
