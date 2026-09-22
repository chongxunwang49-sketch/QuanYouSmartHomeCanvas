"""
日志初始化。

踩坑记录（本机 Windows 实测）：控制台默认编码为 GBK，loguru 输出中文/emoji
会抛 UnicodeEncodeError 并**中断业务流程**（不是只丢日志，是直接崩）。
因此在导入期就把 stdout/stderr 重配为 UTF-8，并设 errors="replace" 兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_configured = False


def _force_utf8_streams() -> None:
    """把标准流切到 UTF-8，避免 GBK 编码崩溃。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # 某些被重定向的流不支持 reconfigure
                pass


def setup_logging(log_dir: Path | None = None, level: str = "INFO") -> None:
    """配置 loguru。幂等，可重复调用。"""
    global _configured
    if _configured:
        return

    _force_utf8_streams()
    logger.remove()

    # 控制台：带颜色，精简
    logger.add(
        sys.stderr,
        level=level,
        colorize=True,
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}:{function}</cyan> - <level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
    )

    # 文件：完整 JSON，供审计与性能分析（对应 AC-14 / AC-23）
    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / "app_{time:YYYY-MM-DD}.log",
            level="DEBUG",
            rotation="00:00",
            retention="180 days",  # 与审计日志保留期一致
            encoding="utf-8",
            enqueue=True,
            serialize=True,
            backtrace=True,
            diagnose=False,
        )

    _configured = True


__all__ = ["setup_logging", "logger"]
