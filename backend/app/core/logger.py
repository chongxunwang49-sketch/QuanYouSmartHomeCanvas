"""
文件路径: backend/app/core/logger.py
模块职责: 日志基础设施 —— loguru 配置 + 全链路 trace_id 上下文
依赖关系: 被 core/ 与 agents/ 全部模块使用；无内部依赖（只依赖标准库与 loguru）

对应需求文档:
  - 4. 四章「API 接口契约」开头：全链路 trace_id 要求
  - AC-30：同一请求在 API 响应、审计日志、LLM 调用日志中 trace_id 一致
  - 附录 B.6：本地部署，日志落盘 E:/quanyou/logs

═══════════════════════════════════════════════════════════════════
两个必须知道的坑
═══════════════════════════════════════════════════════════════════

【坑 1】Windows 控制台默认 GBK，loguru 输出中文/emoji 会抛 UnicodeEncodeError
        并**中断业务流程**（不是只丢日志，是直接崩）。导入期就把标准流重配为 UTF-8。

【坑 2】trace_id 不能只靠 `logger.bind()`，因为异步任务不继承上下文。
        `/layout/parse` 改为异步后，后台任务从数据库取回 task_id 时
        需要**重新绑定** trace_id，否则整条链路断在这里。
        故提供 `bind_trace_id()` 显式重绑接口，见 ADR-13。
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from loguru import logger as _logger

__all__ = [
    "logger", "setup_logging", "reset_logging", "audit",
    "new_trace_id", "bind_trace_id",
    "current_trace_id", "with_trace_id", "trace_context",
]

#: loguru 的 extra 字段名。格式串里通过 {extra[trace_id]} 引用。
_TRACE_KEY = "trace_id"

#: 未绑定 trace_id 时的占位符（保证日志格式对齐，便于 grep）
_NO_TRACE = "-"

_configured = False

#: 显式记录最近一次绑定的 trace_id。
#: loguru 没有公开的"读取当前上下文"接口，而异步任务恢复时需要这个值，
#: 故自己维护一份。单线程/单事件循环下准确；多线程并发请以
#: with_trace_id() 的返回值为准。
_last_trace_id: str = _NO_TRACE


# ══════════════════════════════════════════════════════════════════
# trace_id 上下文
# ══════════════════════════════════════════════════════════════════


def new_trace_id() -> str:
    """生成一个新的 trace_id（标准 UUID4）。"""
    return str(uuid.uuid4())


def bind_trace_id(trace_id: str) -> None:
    """
    把 trace_id 绑到当前执行上下文。

    用于**异步任务恢复**场景：API 层生成 trace_id 后写入 async_tasks 表，
    后台任务从库里取回来时要重新绑定一次——异步上下文不会自动继承。
    """
    global _last_trace_id
    _last_trace_id = trace_id
    _logger.configure(extra={_TRACE_KEY: trace_id})


def current_trace_id() -> str:
    """读取最近绑定的 trace_id；未绑定时返回占位符 '-'。"""
    return _last_trace_id


@contextmanager
def with_trace_id(trace_id: str | None = None) -> Iterator[str]:
    """
    上下文管理器：进入时绑定 trace_id，退出时还原。

    用法：
        with with_trace_id() as tid:
            logger.info("开始解析")        # 日志自动带 tid
    """
    global _last_trace_id
    tid = trace_id or new_trace_id()
    previous = _last_trace_id
    _last_trace_id = tid
    try:
        with _logger.contextualize(**{_TRACE_KEY: tid}):
            yield tid
    finally:
        _last_trace_id = previous


#: 语义化别名，与 with_trace_id 等价（供阅读性更好的调用点使用）
trace_context = with_trace_id


# ══════════════════════════════════════════════════════════════════
# 日志配置
# ══════════════════════════════════════════════════════════════════


def _force_utf8_streams() -> None:
    """
    把标准流切到 UTF-8。

    见模块头「坑 1」：本机控制台是 GBK，不修这个的话
    任何带中文/emoji 的日志都会抛异常并中断业务代码。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 —— 某些被重定向的流不支持
                pass


def _ensure_trace_default(record: dict) -> None:
    """
    loguru patcher：给每条日志补上 trace_id 字段。

    没有这一步，未绑定上下文的日志在格式串里会 KeyError。
    """
    record["extra"].setdefault(_TRACE_KEY, _NO_TRACE)


def setup_logging(log_dir: Path | str | None = None, level: str = "INFO") -> None:
    """
    配置 loguru。幂等，可重复调用。

    Args:
        log_dir: 日志目录。传入时额外写一份 JSON 结构化日志（供审计与性能分析）。
                 默认从 settings 读，但本模块不 import settings 以避免循环依赖，
                 故由调用方传入。
        level: 控制台日志级别。
    """
    global _configured
    if _configured:
        return

    _force_utf8_streams()
    _logger.remove()
    _logger.configure(patcher=_ensure_trace_default)

    # ── 控制台：带颜色，人类可读 ──────────────────────
    # {extra[trace_id]:.8} 只显示前 8 位，避免刷屏；完整值在文件日志里
    _logger.add(
        sys.stderr,
        level=level,
        colorize=True,
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<magenta>{extra[trace_id]:.8}</magenta> | "
            "<cyan>{name}:{function}</cyan> - <level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
    )

    # ── 文件：完整 JSON，供审计（AC-14）与性能基线（AC-23）──
    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        _logger.add(
            log_dir / "app_{time:YYYY-MM-DD}.log",
            level="DEBUG",
            rotation="00:00",
            retention="180 days",     # 与 2.3.2 审计日志保留期一致
            encoding="utf-8",
            enqueue=True,             # 多进程安全
            serialize=True,           # JSON 结构化
            backtrace=True,
            diagnose=False,           # 关闭变量快照，避免把密钥写进日志
        )

    _configured = True


def reset_logging() -> None:
    """
    清掉"已配置"标志。**仅供测试**，与 `reset_task_manager` / `reset_redis_client`
    是同一个模式。

    ⚠️ 为什么测试需要它：`setup_logging` 有幂等保护（进程内只生效一次），
    而"文件 sink 到底有没有写、写出来长什么样"这件事**只能靠真的配一次、
    真的读文件**来验证 —— 而那条路径在生产里只在 lifespan 启动时走一次，
    测试进程里根本碰不到。没有这个复位函数，AC-14 就只有"代码看起来写了"，
    没有"文件真的产生了"。
    """
    global _configured
    _configured = False


def audit(event: str, **fields: Any) -> None:
    """
    写一条**结构化**审计事件（AC-14）。

    ⚠️ **字段走 `extra`，不是拼进消息字符串。**

    审计日志的用途是"事后可查询"（AC-14 原文），而查询靠的是字段：
    想回答"昨天有多少次生成是降级的"就得能按 `degraded=true` 过滤。
    把同样的信息拼成一句中文再让下游去正则，等于把结构化的工作推给了
    每一个查询方 —— 而且拼字符串的格式会随人改。

    落到文件里长这样（`serialize=True`，JSON Lines，一天一个文件）：
        {"text": "AUDIT task_finished ...", "record": {"extra": {
            "audit": true, "event": "task_finished", "trace_id": "...",
            "task_id": "...", "kind": "generate", "status": "completed",
            "degraded": false, "elapsed_seconds": 112, ...}}}

    `event` 取值是**约定**，不是枚举类：`login` / `login_failed` /
    `task_created` / `task_finished` / `membership_changed` / `user_updated`。
    加新事件时在调用点写清楚即可，但**不要复用已有名字表达别的意思** ——
    查询方是按名字聚合的。
    """
    _logger.bind(audit=True, event=event, **fields).info(
        "AUDIT " + " ".join(f"{k}={v}" for k, v in fields.items())
    )

    # ── 同时投递到数据库（AC-14 的「可查询」）──────────────────
    #
    # ⚠️ **文件与数据库两条腿都留着，不是过渡期。**
    #    文件那条在数据库不通时仍然可查，而且它是"进程外"的 ——
    #    连不上库、连日志文件都被轮转掉的时候，这里至少还有一份。
    #    库那条提供文件给不了的东西：按时间范围聚合、按字段过滤。
    #
    # ⚠️ 这里是**同步**函数，被异步上下文调用（登录、任务终态）。
    #    所以只能入队，不能写库 —— 在事件循环上做阻塞 IO 的代价，
    #    本项目在 A-06 上量过一次（28~30 秒的空洞）。
    #    队列是同步、非阻塞、绝不抛的，详见 db/audit_sink.py。
    try:
        from ..db import audit_sink

        audit_sink.enqueue(event, dict(fields))
    except Exception as e:  # noqa: BLE001 —— 审计落库不能拖垮它所在的那件事
        _logger.debug(f"[audit] 投递到数据库失败（已写文件）：{type(e).__name__}: {e}")


def log_llm_call(
    *,
    agent: str,
    provider: str,
    model: str,
    degraded: bool,
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
    elapsed_ms: int,
    degrade_reason: str | None = None,
    **extra: Any,
) -> None:
    """
    LLM 调用的结构化日志。

    统一从一处输出，保证审计日志字段一致（AC-14 要求含 degraded 字段）。
    降级时用 WARNING 级别，便于 `grep WARNING` 快速定位所有降级事件。
    """
    payload = {
        "event": "llm_call",
        "agent": agent,
        "provider": provider,
        "model": model,
        "degraded": degraded,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "elapsed_ms": elapsed_ms,
        **extra,
    }
    if degraded:
        payload["degrade_reason"] = degrade_reason
        _logger.bind(**payload).warning(f"[{agent}] 降级调用 {provider}/{model}")
    else:
        _logger.bind(**payload).debug(
            f"[{agent}] {provider}/{model} tokens="
            f"{prompt_tokens + completion_tokens} ({elapsed_ms}ms)"
        )


#: 全局唯一的 logger 实例。所有模块统一 `from ..core.logger import logger`
logger = _logger
