"""
审计事件的异步落库（AC-14 的「可查询」）。

═══════════════════════════════════════════════════════════════════
为什么是队列 + 后台任务，而不是在 audit() 里直接写
═══════════════════════════════════════════════════════════════════
`audit()` 是**同步函数**，而它被从异步上下文里调用（登录处理、任务终态）。
在同步函数里做一次阻塞的数据库写入，就是**在事件循环上阻塞** ——
这个项目在 A-06 上已经量过一次同样的错（`asyncio.to_thread` 那次，
见 `risk_reviewer.py`），代价是 28~30 秒的事件循环空洞。

所以分工是：

    audit()          → 构造字段 → `put_nowait()` → 立刻返回（微秒级）
    后台消费任务      → 攒批 → 一条多值 INSERT → 落库

审计不是关键路径上的数据：晚几十毫秒落库没有任何影响，
而把主链路拖慢是有影响的。

═══════════════════════════════════════════════════════════════════
丢弃是要被看见的
═══════════════════════════════════════════════════════════════════
队列**有界**（`MAXSIZE`）。数据库长时间不通时，队列会满，此时：

  · 丢**最老的**（`put_nowait` 在满时抛 `QueueFull`，我们手动 `get_nowait`
    腾位），因为最近的审计更可能被查；
  · **计数**。`stats()` 里的 `dropped_*` 会被 `/system/metrics` 暴露出来。

为什么不像别的写入那样"静默失败就算了"：审计的全部意义是事后可查。
一条悄悄没写进去的降级记录，会让"昨天有几次降级"的答案是**错的**，
而错得没有任何迹象 —— 这比没有审计更危险。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from . import repository

__all__ = ["enqueue", "start", "stop", "stats", "reset"]

#: 队列上限。按"数据库停 30 秒、每秒几十条事件"的量级留的余量。
MAXSIZE = 500

#: 一批最多合并多少条。太大则延迟可见，太小则批量失去意义。
BATCH = 100

#: 消费循环的等待上限：即使没有新事件，也定期醒来刷一次，
#: 免得最后一条事件一直躺在队列里等到下一次有流量才落库。
FLUSH_SECONDS = 2.0

_queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
_task: asyncio.Task | None = None

_stats = {
    "enqueued": 0,
    "written": 0,
    "failed": 0,        # 落库失败（数据库不通等）
    "dropped_full": 0,  # 队列满被挤掉的
}


def stats() -> dict[str, int]:
    return dict(_stats)


def _get_queue() -> asyncio.Queue[tuple[str, dict[str, Any]]]:
    """
    惰性建队列。

    ⚠️ **不在 import 期建。** `asyncio.Queue()` 在 3.10+ 虽然不绑循环，
    但这个模块会被 `core/logger.py` 导入，而 logger 又会被几乎一切导入 ——
    在 import 期碰任何 asyncio 对象都是在赌"第一次用的时候有循环"。
    懒建没有这个赌注。
    """
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=MAXSIZE)
    return _queue


def enqueue(event: str, fields: dict[str, Any]) -> bool:
    """
    把一条审计事件放进队列。**同步、绝不抛、绝不阻塞。**

    返回是否入队成功。调用方（`logger.audit`）**不看返回值** ——
    审计失败不该影响它所在的那件事，但计数在 `stats()` 里留着。
    """
    try:
        q = _get_queue()
        if q.full():
            # 丢最老的：最近的审计更可能被查（见模块说明）
            try:
                q.get_nowait()
                _stats["dropped_full"] += 1
            except asyncio.QueueEmpty:  # pragma: no cover —— 竞态，忽略
                pass
        q.put_nowait((event, fields))
        _stats["enqueued"] += 1
        return True
    except Exception as e:  # noqa: BLE001 —— 审计不能拖垮调用方
        logger.debug(f"[audit-sink] 入队失败（忽略）：{type(e).__name__}: {e}")
        return False


async def _consume() -> None:
    """
    后台消费循环。**任何异常都不能让它退出** —— 它退出了，后面所有
    审计事件都会静静堆在队列里直到被挤掉。
    """
    q = _get_queue()
    while True:
        try:
            first = await asyncio.wait_for(q.get(), timeout=FLUSH_SECONDS)
        except asyncio.TimeoutError:
            continue                      # 只是没事件，不是错误
        except asyncio.CancelledError:
            raise

        batch = [first]
        while len(batch) < BATCH:
            try:
                batch.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break

        try:
            written = await repository.insert_audit_events(batch)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[audit-sink] 落库异常：{type(e).__name__}: {e}")
            written = 0

        if written:
            _stats["written"] += written
        else:
            _stats["failed"] += len(batch)
            logger.warning(
                f"[audit-sink] {len(batch)} 条审计事件未落库"
                f"（累计失败 {_stats['failed']} 条）"
            )


async def start() -> None:
    """在 lifespan 里启动后台消费任务。重复调用是安全的。"""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_consume(), name="audit-sink")
    logger.debug("[audit-sink] 后台任务已启动")


async def stop() -> None:
    """
    停掉消费任务，并**尽力把队列里剩下的刷完**。

    为什么要有超时：停机流程本身可能是被取消/超时驱动的（AC-31 的
    150 秒排空），这里如果无上限地等，就等于给停机加了一个不确定的尾巴。
    刷不完的会在 `stats()` 里体现为没落库 —— 如实记着，不假装写完了。
    """
    global _task
    if _task is None:
        return
    task, _task = _task, None
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass

    q = _get_queue()
    leftover: list[tuple[str, dict[str, Any]]] = []
    while not q.empty():
        try:
            leftover.append(q.get_nowait())
        except asyncio.QueueEmpty:  # pragma: no cover
            break
    if not leftover:
        return
    try:
        written = await asyncio.wait_for(
            repository.insert_audit_events(leftover), timeout=5.0
        )
        _stats["written"] += written
        if written < len(leftover):
            _stats["failed"] += len(leftover) - written
    except Exception as e:  # noqa: BLE001
        _stats["failed"] += len(leftover)
        logger.warning(f"[audit-sink] 停机刷盘失败，{len(leftover)} 条未落库：{e}")


def reset() -> None:
    """仅测试用。"""
    global _queue, _task
    _queue = None
    _task = None
    for k in _stats:
        _stats[k] = 0
