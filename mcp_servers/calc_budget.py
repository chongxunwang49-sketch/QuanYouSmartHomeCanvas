"""
MCP Server: calc_budget —— 按户型与档位算装修预算。

启动：python -m mcp_servers.calc_budget
协议：stdio（由 MCPClient 拉起）

═══════════════════════════════════════════════════════════════════
它是一层壳，而且是**刻意的**一层壳
═══════════════════════════════════════════════════════════════════
真正的算法在 `services/budget/engine.py`（纯函数、无 IO、无 LLM）。
本文件**不重算任何数字**，只做三件事：把 MCP 的 JSON 入参翻译成引擎的
参数、调它、把 `BudgetBreakdown` 原样序列化回去。

这条纪律来自 `parse_house_layout.py`（同一个目录）踩过的教训：
MCP 路径与 Agent 路径**必须共用同一个实现**。各写一份的话，两边会随
调优逐渐分叉 —— 而预算数字是**拿去跟装修公司砍价**的，分叉出来的是
"用户按 A 路径看到 8 万、按 B 路径看到 10 万"，比崩溃更糟（ADR-07）。

⚠️ 面积用 `capabilities.effective_total_area`，与守卫和 A-04 **同源**。
   自己再写一遍 `total_area or sum(rooms)` 的话，会出现最坏的情况：
   守卫放行、而这里拿到 0，于是算出一份 ¥0 的预算。
   那个函数的注释就是为这件事写的。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 允许以 `python -m mcp_servers.xxx` 独立启动时找到 backend 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger

from backend.app.core.capabilities import effective_total_area
from backend.app.core.config import settings
from backend.app.core.logging_setup import setup_logging
from backend.app.schemas.budget import BudgetGrade
from backend.app.services.budget import engine

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
    name="calc_budget",
    title="装修预算测算（规则引擎）",
    version="1.0.0",
    instructions=(
        "按户型面积与预算档位算出分项明细与总价区间。"
        "数字由规则引擎计算，不由模型估算 —— 同一个户型同一个档位永远得到同一份预算。"
    ),
)

#: 与 `PriorityPlan` / 前端 `GRADE_LABEL` 同源。写死在这里会让三处漂移，
#: 所以直接读 schema 的 Literal —— 它是唯一真源。
GRADES: tuple[str, ...] = BudgetGrade.__args__  # type: ignore[attr-defined]


# ⚠️ **本文件里所有可预期的失败一律 `raise ToolError`，不要 `ValueError`。**
#
# SDK 的契约（`mcp/server/mcpserver/exceptions.py:43` 起，逐字读过）分两类：
#   · `ToolError` —— "一个你预料到的失败"：调用方拿到 `is_error=True`
#     **和你的原话**，服务端按 INFO 记一行、不带堆栈；
#   · 其它**任何**异常 —— 当**崩溃**处理：调用方只看到
#     `Error executing tool <name>`，**原始信息全部丢失**，服务端按 ERROR 记堆栈。
#
# 实测确认：`ValueError` 走的是后者，调用方拿到的是一句无信息量的英文。
# 这条区分还顺带保住了日志的语义 —— 参数写错不该和系统故障记成同一个级别。


@server.tool(
    name="calc_budget",
    description=(
        "按户型 JSON 与预算档位计算装修预算，返回 ≥7 个分项明细与总价区间（元）。"
        "档位取 economy（经济）/ medium（中档）/ high（高端）。"
        "面积取自户型数据，缺失时回退为房间面积之和。"
    ),
)
def calc_budget(
    layout: dict[str, Any],
    grade: str = "medium",
    wall_area_factor: float | None = None,
    region_coefficient: float | None = None,
) -> dict[str, Any]:
    """
    Args:
        layout: 户型 JSON（A-01 解析产物，含 total_area 或 rooms[].area）。
        grade: economy / medium / high。
        wall_area_factor: 墙面面积相对地面的倍数，覆盖价格表默认值。
        region_coefficient: 地区调整系数，覆盖价格表默认值（本表按内江标定 1.0）。
    """
    if grade not in GRADES:
        # 拒绝要说清允许哪些值，而不是只说"不合法"
        raise ToolError(f"档位取值不合法：{grade!r}；允许 {list(GRADES)}")

    area = effective_total_area(layout)
    if area <= 0:
        # 与 engine.InvalidAreaError 同一个立场：**业务上就不该算**。
        # 让它在这里带一句可操作的话，而不是把 0 喂进引擎再抛一个数字异常。
        raise ToolError(
            "户型里没有可用面积（total_area 与 rooms[].area 都是 0）—— "
            "没有面积就算不出预算。请先解析户型图，或补上 total_area。"
        )

    breakdown = engine.calculate(
        area=area,
        grade=grade,
        wall_area_factor=wall_area_factor,
        region_coefficient=region_coefficient,
    )
    logger.info(
        f"[calc_budget] {area:.1f}㎡ / {grade} → "
        f"{breakdown.total_min:,.0f}-{breakdown.total_max:,.0f} 元"
    )
    return breakdown.model_dump()


def main() -> None:
    logger.info("calc_budget MCP Server 启动（stdio）")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
