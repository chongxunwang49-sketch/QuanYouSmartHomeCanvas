"""
业务数据的 PostgreSQL 连接池。

═══════════════════════════════════════════════════════════════════
为什么是裸 psycopg，而不是 SQLAlchemy + alembic
═══════════════════════════════════════════════════════════════════
`requirements.txt` 里早就用实测依据排除了这两个（当时全仓零 import）。
现在真要落库了，重新算一次这笔账 —— 结论没变：

  · **表就 3 张，写入方就 1 个进程。** ORM 的价值在"大量表的映射与关系
    导航"，这里没有关系导航可言（唯二的外键还都建不了，见 schema.sql）。
  · **psycopg 已经在了。** `psycopg[binary]==3.3.6` 是检查点（AC-12）的
    依赖，连接池 `psycopg_pool` 随它一起来。加 SQLAlchemy 等于为了 3 张表
    多引入一套运行时。
  · **SQL 本来就是这门语言的正确表达。** 本模块的查询全是单表、
    按主键读写、JSONB 存整块 —— 用 ORM 写出来不会更清楚，只会多一层
    "这个 filter 最终生成什么 SQL"的推理。

迁移没有用 alembic，代价要写明：**没有自动降级路径**。
`backend/app/db/migrations/` 下是带序号的纯 SQL，`migrate()` 幂等执行、
记录已应用的版本（见 migrations.py）。改表要么加一个新的 `00N_*.sql`
（向前兼容），要么手写降级 SQL —— 这对一个演示项目是合适的取舍，
但它确实是取舍，不是"不需要迁移"。

═══════════════════════════════════════════════════════════════════
不可用时的立场：尽力而为，绝不阻塞主链路
═══════════════════════════════════════════════════════════════════
与 Redis 同一立场（见 `redis_client.py`）：**PG 挂了不该让接口挂**。
所以：

  · 连不上 → 记一条 warning，返回 None，调用方自行决定要不要回退；
  · 连着但报错 → 同样吞掉并记录，**不向上抛**；
  · 熔断 → 连续失败后短暂停止重试（`_COOLDOWN_SECONDS`），
    否则每次请求都要等一次连接超时，把"DB 慢"放大成"接口慢"。

⚠️ 但这条立场有**一个例外**，写在 `repository.py` 里：审计事件
（AC-14）落库失败必须能被看见，不能静默 —— 审计的全部意义就是事后能查。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Sequence

from loguru import logger

from ..core.config import settings

__all__ = ["get_pool", "close_pool", "execute", "fetch_all", "is_available", "reset"]

#: 连续失败后暂停重试的秒数。见模块说明的"熔断"。
_COOLDOWN_SECONDS = 30.0

_pool: Any = None
_pool_lock = asyncio.Lock()
_last_failure_at: float = 0.0
_consecutive_failures = 0

#: 观测用：累计成功的写入/查询次数，供 /system/metrics 回答"落库到底有没有在工作"。
_stats = {"executed": 0, "failed": 0, "skipped_unavailable": 0}


def stats() -> dict[str, int]:
    return dict(_stats)


def _cooling_down() -> bool:
    return (
        _consecutive_failures > 0
        and (time.monotonic() - _last_failure_at) < _COOLDOWN_SECONDS
    )


def _record_failure(where: str, exc: BaseException) -> None:
    global _last_failure_at, _consecutive_failures
    _last_failure_at = time.monotonic()
    _consecutive_failures += 1
    _stats["failed"] += 1
    # 第一次失败说清楚，之后降成 debug —— 否则数据库长时间不通的时候，
    # 日志会被同一句话刷满，真正有用的那几条反而找不到了。
    level = logger.warning if _consecutive_failures == 1 else logger.debug
    level(
        f"[db] {where} 失败（连续 {_consecutive_failures} 次，"
        f"{_COOLDOWN_SECONDS:.0f}s 内不再重试）：{type(exc).__name__}: {exc}"
    )


async def get_pool() -> Any | None:
    """
    取连接池，连不上返回 None。

    ⚠️ **不在 import 期建池。** 连接池要绑事件循环，而模块是在导入时
    执行的 —— 那时不一定有循环，有也不是将来跑请求的那个。所以池在
    lifespan 里显式 `open_pool()`，这里只做惰性兜底。
    """
    global _pool
    if not settings.ENABLE_DB:
        _stats["skipped_unavailable"] += 1
        return None
    if _cooling_down():
        _stats["skipped_unavailable"] += 1
        return None
    if _pool is not None:
        return _pool

    async with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            from psycopg_pool import AsyncConnectionPool

            pool = AsyncConnectionPool(
                settings.checkpoint_dsn,
                min_size=1,
                max_size=4,
                # 连不上时不阻塞：宁可这一条写不进去，也不要请求卡在这里。
                timeout=5.0,
                open=False,
            )
            await pool.open(wait=False)
            _pool = pool
            logger.info("[db] 连接池已建立")
            return _pool
        except Exception as e:  # noqa: BLE001 —— 建池失败不该让调用方崩
            _record_failure("建立连接池", e)
            return None


async def close_pool() -> None:
    global _pool
    if _pool is None:
        return
    try:
        await _pool.close()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[db] 关闭连接池出错（忽略）：{type(e).__name__}: {e}")
    finally:
        _pool = None


def reset() -> None:
    """仅测试用：清掉熔断状态与缓存的池。"""
    global _pool, _last_failure_at, _consecutive_failures
    _pool = None
    _last_failure_at = 0.0
    _consecutive_failures = 0
    for k in _stats:
        _stats[k] = 0


async def execute(sql: str, params: Sequence[Any] | None = None) -> int | None:
    """
    执行一条写语句，返回受影响行数。**失败返回 None，绝不抛。**

    返回 None 与返回 0 含义不同：0 是"执行了，没有行受影响"，
    None 是"根本没执行成"。调用方靠这个区分"写成功了但没变化"
    "数据库没通"、"语句本身有问题"。

    ⚠️ **`rowcount` 对 DDL 是 `-1`**（实测：`CREATE TABLE` / `DROP TABLE`
    都返回 -1）——psycopg 对没有行数的语句用它表示"不适用"。
    所以调用方**不能**把 `rowcount >= 0` 当成"语句有效"的判据。

    ⚠️ `get_pool()` 也被包在 try 里。它自己的契约是"绝不抛"，但那是它的
    内部纪律 —— 这个函数的契约是"绝不抛"，两条契约不能互相依赖：
    实测过（把 get_pool 换成会抛的桩），原来那一版会直接把异常漏出去。
    边界上兜一次，成本是一个 `try`。
    """
    try:
        pool = await get_pool()
        if pool is None:
            return None
        async with pool.connection() as conn:
            cur = await conn.execute(sql, params)
            _stats["executed"] += 1
            return cur.rowcount
    except Exception as e:  # noqa: BLE001
        _record_failure("写入", e)
        return None


async def fetch_all(
    sql: str, params: Sequence[Any] | None = None,
) -> list[dict[str, Any]] | None:
    """查询。失败返回 None（而不是空列表 —— 两者含义不同，见 `execute`）。"""
    try:
        pool = await get_pool()
        if pool is None:
            return None
        from psycopg.rows import dict_row

        async with pool.connection() as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(sql, params)
            rows = await cur.fetchall()
            _stats["executed"] += 1
            return list(rows)
    except Exception as e:  # noqa: BLE001
        _record_failure("查询", e)
        return None


async def is_available() -> bool:
    """给健康检查用。**不触发重试**（冷却期内直接返回 False）。"""
    rows = await fetch_all("SELECT 1 AS ok")
    return bool(rows)
