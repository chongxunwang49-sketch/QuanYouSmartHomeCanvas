"""
MCP Server: parse_house_layout —— 户型图结构化解析。

启动：python -m mcp_servers.parse_house_layout
协议：stdio（由 MCPClient 拉起）

实现说明：
- 使用 mcp SDK 2.x 的 MCPServer（旧名 FastMCP，2.x 已更名）。
  参见 https://py.sdk.modelcontextprotocol.io/v2/migration/
- 服务端与 Agent 共用 LLMClient，因此模型可插拔、降级链一致。
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

# 允许以 `python -m mcp_servers.xxx` 独立启动时找到 backend 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger

from backend.app.core.config import settings
from backend.app.core.llm_client import ImagePart, get_llm_client
from backend.app.core.logging_setup import setup_logging
from backend.app.schemas.layout import LayoutSchema

try:
    from mcp.server.mcpserver import MCPServer
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "需要 mcp >= 2.0（FastMCP 已更名为 MCPServer）。"
        "若需沿用 v1 写法请执行: pip install 'mcp<2'。"
        f"原始错误: {e}"
    ) from e

setup_logging(level=settings.LOG_LEVEL)

server = MCPServer(
    name="parse_house_layout",
    title="户型图解析",
    version="1.0.0",
    instructions="把住宅户型图（JPG/PNG）解析为结构化 JSON，包含房间、墙体、门窗、尺寸与朝向。",
)

_SYSTEM_PROMPT = """你是一名资深的室内设计师与建筑制图工程师，擅长解读住宅户型图。
请观察用户提供的户型图，输出结构化的户型信息。

【必须遵守的规则】
1. 绝对禁止编造。图中没有的内容一律留空，不允许凭经验"补全"。
2. 面积只做估算时，必须在 uncertain_points 中说明"按比例估算，非实测"。
3. 只有明确看到粗实线才判定 load_bearing；不确定一律填 unknown。
4. 不确定的地方必须如实列出，宁可多写不确定项，也不要给自信的错误答案。
5. 置信度要真实反映把握程度。"""


def _load_image(image_path: str | None, image_base64: str | None) -> ImagePart:
    """从路径或 base64 还原图像。"""
    if image_base64:
        if image_base64.startswith("data:"):
            return ImagePart.from_data_uri(image_base64)
        return ImagePart(data_b64=image_base64)

    if not image_path:
        raise ValueError("必须提供 image_path 或 image_base64 之一")

    p = Path(image_path)
    if not p.exists():
        raise FileNotFoundError(f"图像不存在: {image_path}")
    if p.stat().st_size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"图像超过 {settings.MAX_UPLOAD_MB}MB 上限")
    return ImagePart.from_raw(p.read_bytes(), _guess_mime(p.suffix))


def _guess_mime(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }.get(suffix.lower(), "image/png")


@server.tool(
    name="parse_house_layout",
    description=(
        "解析住宅户型图，返回房间/墙体/门窗/尺寸/朝向的结构化 JSON。"
        "detail_level=basic 时只返回房间列表与总面积，可显著降低 token 消耗。"
    ),
)
async def parse_house_layout(
    image_path: str | None = None,
    image_base64: str | None = None,
    detail_level: str = "full",
    prefer_local: bool = False,
) -> dict[str, Any]:
    """
    Args:
        image_path: 图像在服务器本地的绝对路径。
        image_base64: 图像的 base64 内容（或 data URI）。与 image_path 二选一。
        detail_level: basic 或 full。
        prefer_local: True 时强制使用本地 Ollama 模型，图像不离开本机。
    """
    image = _load_image(image_path, image_base64)
    client = get_llm_client()

    hint = ""
    if detail_level == "basic":
        hint = "\n本次只需给出房间列表、总面积与置信度，墙体/门窗/尺寸可留空。"

    parsed, result = await client.complete_json(
        LayoutSchema,
        system=_SYSTEM_PROMPT,
        user=f"请解析这张户型图，输出结构化 JSON。{hint}",
        images=[image],
        agent="MCP:parse_house_layout",
    )

    assert isinstance(parsed, LayoutSchema)
    payload = parsed.model_dump()

    # 安全提示是强制项，MCP 路径与 Agent 路径必须一致，不能有一条漏掉。
    # 承重墙误判会导致用户砸错墙，这是安全事故级别的约束。
    load_bearing = [w for w in parsed.walls if w.type == "load_bearing"]
    payload["safety_notice"] = (
        "图中存在疑似承重墙，任何拆改前必须由具备资质的专业人员现场复核，"
        "切勿依据本系统结论直接施工。"
        if load_bearing
        else "未能明确识别承重墙，如需拆改请务必由专业人员现场确认。"
    )

    payload["model_used"] = result.model_used
    payload["degraded"] = result.degraded
    payload["degrade_reason"] = result.degrade_reason
    payload["elapsed_ms"] = result.elapsed_ms
    payload["token_usage"] = {
        "prompt": result.prompt_tokens,
        "completion": result.completion_tokens,
        "reasoning": result.reasoning_tokens,
    }
    return payload


def main() -> None:
    logger.info("parse_house_layout MCP Server 启动（stdio）")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
