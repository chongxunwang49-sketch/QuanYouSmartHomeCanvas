"""
文件路径: backend/app/core/logging_setup.py
模块职责: **兼容外壳** —— 日志实现已迁移至 `backend/app/core/logger.py`

保留本文件的原因：`mcp_servers/` 下的独立进程以
`from backend.app.core.logging_setup import setup_logging` 引用本模块，
更名时一并改动会引入无谓的回归风险。

新代码请直接使用：

    from backend.app.core.logger import logger, with_trace_id, setup_logging

迁移背景（V2.3）：
    加入全链路 trace_id 支持（AC-30）时，模块职责从"仅配置日志"
    扩展为"配置 + 请求级上下文"，故更名以反映实际职责。
"""

from __future__ import annotations

from .logger import (  # noqa: F401 —— 全部为对外转发
    bind_trace_id,
    current_trace_id,
    log_llm_call,
    logger,
    new_trace_id,
    setup_logging,
    trace_context,
    with_trace_id,
)

__all__ = [
    "logger",
    "setup_logging",
    "new_trace_id",
    "bind_trace_id",
    "current_trace_id",
    "with_trace_id",
    "trace_context",
    "log_llm_call",
]
