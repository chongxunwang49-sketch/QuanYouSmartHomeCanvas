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

    def _server_params(self, module: str):
        from mcp.client.stdio import StdioServerParameters

        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(settings.PROJECT_ROOT),
        }
        return StdioServerParameters(
            command=self.python,
            args=["-m", f"mcp_servers.{module}"],
            env=env,
            cwd=str(settings.PROJECT_ROOT),
        )

    @asynccontextmanager
    async def _session(self, module: str, timeout: float = 60.0):
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        params = self._server_params(module)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                await session.initialize()
                yield session

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
        timeout: float = 60.0,
    ) -> Any:
        """
        调用 MCP 工具。

        module 缺省时按工具名前缀推断（如 parse_house_layout -> parse_house_layout）。
        """
        module = module or tool_name
        logger.debug(f"[MCP] 调用 {module}.{tool_name} args={json.dumps(arguments, ensure_ascii=False)[:200]}")

        async with self._session(module, timeout=timeout) as session:
            result = await session.call_tool(tool_name, arguments)

            if getattr(result, "isError", False):
                raise MCPToolError(
                    f"MCP 工具 {tool_name} 返回错误: {_texts(result)}"
                )

            # SDK 2.x：结构化结果在 structuredContent，纯文本在 content
            structured = getattr(result, "structuredContent", None)
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


_client: MCPClient | None = None


def get_mcp_client() -> MCPClient:
    global _client
    if _client is None:
        _client = MCPClient()
    return _client


__all__ = ["MCPClient", "MCPToolError", "get_mcp_client", "MCP_SERVERS_DIR"]
