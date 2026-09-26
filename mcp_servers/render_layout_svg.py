"""
MCP Server: render_layout_svg —— 户型 JSON → 矢量户型图（SVG）。

启动：python -m mcp_servers.render_layout_svg
协议：stdio（由 MCPClient 拉起）

═══════════════════════════════════════════════════════════════════
走接口层那**唯一**的渲染入口，不自己拼参数
═══════════════════════════════════════════════════════════════════
渲染链路是 `layout JSON → normalize_layout → Scene → render_scene_svg(尺寸/边距)`
三步，其中尺寸与边距有一组"接口层认定的规范值"（`CANONICAL_MIN_SIDE_PX`
/ `CANONICAL_PAD_PX`）。`services/render/__init__.py` 的 `render_plan_for()`
把它们收成了一个函数，并在注释里写明了理由：

    约定成"同一个函数"，比约定成"记得用同样的参数"可靠 ——
    后者是纪律，前者是结构。

所以本工具**只调 `render_plan_for`，不暴露尺寸旋钮**。给了旋钮就等于
开了一条"外部的图和站内的图不一样大、边距不一样"的路 —— 而那种差异
没人会发现，直到有人把两张图并排看。

═══════════════════════════════════════════════════════════════════
为什么把 SVG 原文直接返回（而不是存文件给路径）
═══════════════════════════════════════════════════════════════════
实测一份 4 房的户型 SVG 是 3.7 KB，9 房的量级也就十几 KB —— 对 MCP 的
JSON-RPC 报文完全不是负担。存文件反而引入"路径在哪、谁来清理、
外部调用方能不能读到那个目录"三个新问题，而返回值还得再加一个路径字段。

⚠️ `warnings` 必须跟着 SVG 一起返回。它是"哪里画不准/没画"的**唯一**说明
   （墙未闭合、门窗落不到墙上、房间轮廓无效……）。只给图不给它，等于让
   调用方把一张有缺口的图当成完整的 —— 那正是本项目一路在防的事。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 允许以 `python -m mcp_servers.xxx` 独立启动时找到 backend 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger

from backend.app.core.config import settings
from backend.app.core.logging_setup import setup_logging
from backend.app.services.render import render_plan_for

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "需要 mcp >= 2.0（FastMCP 已更名为 MCPServer）。"
        f"原始错误: {e}"
    ) from e

setup_logging(level=settings.LOG_LEVEL)

server = MCPServer(
    name="render_layout_svg",
    title="户型矢量图渲染",
    version="1.0.0",
    instructions=(
        "把结构化户型 JSON 渲染成矢量户型图（SVG）。"
        "纯函数：同一份户型永远得到逐字节相同的 SVG。"
        "返回值里的 warnings 说明哪些地方画不出来或画不准。"
    ),
)


@server.tool(
    name="render_layout_svg",
    description=(
        "把户型 JSON 渲染成 SVG 矢量图，返回 svg 原文、像素尺寸、"
        "未绘制/未闭合等 warnings，以及场景假设清单。"
        "纯计算，不调用任何模型，无外部依赖。"
    ),
)
def render_layout_svg(layout: dict[str, Any]) -> dict[str, Any]:
    """
    Args:
        layout: 户型 JSON（A-01 解析产物：rooms / walls / doors / windows / total_area）。
    """
    if not isinstance(layout, dict) or not layout.get("rooms"):
        # 用 `ToolError` 而不是 `ValueError`：前者调用方能看到这句话，
        # 后者会被 SDK 当成崩溃、把消息替换成 "Error executing tool ..."。
        # 见 calc_budget.py 里那段说明。
        raise ToolError(
            "户型数据为空或没有 rooms —— 没有房间就画不出图。"
            "请先解析户型图拿到完整结构。"
        )

    scene, plan = render_plan_for(layout)
    logger.info(
        f"[render_layout_svg] {len(scene.rooms)} 个房间 → "
        f"{plan.width_px}×{plan.height_px}，{len(plan.svg)} 字符，"
        f"{len(plan.warnings)} 条 warning"
    )
    return {
        "svg": plan.svg,
        "width_px": plan.width_px,
        "height_px": plan.height_px,
        # 画不准的地方。**不许静默** —— 调用方要么展示，要么记日志。
        "warnings": plan.warnings,
        # 场景的假设清单（如"层高按 2.8m 假定"）。SVG 会被单独导出/分享，
        # 假设必须跟着图走，否则一张标着层高的图离开系统就没人知道那是猜的。
        "assumptions": list(scene.assumptions),
        "room_count": len(scene.rooms),
    }


def main() -> None:
    logger.info("render_layout_svg MCP Server 启动（stdio）")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
