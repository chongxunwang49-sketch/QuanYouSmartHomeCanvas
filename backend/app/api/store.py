"""
解析结果暂存 —— 让 `/design/generate` 只凭 `layout_id` 就能开工。

═══════════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════════
4.2 的解析接口产出 `layout_id`，4.3 的方案接口**接收** `layout_id` ——
两个接口之间必须有个地方把户型存下来。

**为什么不让方案接口重新解析一遍**：视觉解析约 16 秒，而且**模型有随机性** ——
同一个 `layout_id` 重新解析可能得到不同的房间数、不同的面积。
用户会发现自己看的那份户型和生成方案用的不是同一份，而界面上写着同一个 id。
**同一个 id 必须指同一份数据**，这是标识符的基本要求。

═══════════════════════════════════════════════════════════════════
为什么是内存 + Redis 双写
═══════════════════════════════════════════════════════════════════
与 `tasks.py` 同一个理由：Redis 在本项目是**可选依赖**，
但它挂了这个接口就没法用了。两边都写，两边都能顶。

⚠️ **这是临时方案**。设计稿里户型落在 PostgreSQL（5.2 的 `house_layouts` 表），
当前项目还没接数据库 —— 进程重启后暂存的户型就没了。
接库时把 `save` / `load` 换成 SQL 即可，调用方不用改。
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from ..core.config import settings
from ..core.redis_client import get_redis

#: 户型暂存 TTL。与任务结果一致（1 小时），够用户"解析完歇一会儿再生成"。
LAYOUT_TTL = 3600

_memory: dict[str, dict[str, Any]] = {}


def _key(layout_id: str) -> str:
    return f"{settings.REDIS_TASK_PREFIX}:layout:{layout_id}"


async def save(layout_id: str, layout: dict[str, Any]) -> None:
    """存一份户型。内存必写，Redis 尽力而为。"""
    if not layout_id:
        return
    _memory[layout_id] = layout

    try:
        client = await get_redis().client()
        if client is None:
            return
        await client.set(_key(layout_id), json.dumps(layout, ensure_ascii=False),
                         ex=LAYOUT_TTL)
    except Exception as e:  # noqa: BLE001 —— 存 Redis 失败不该让解析任务失败
        logger.warning(f"[layout-store] 写 Redis 失败（已存内存）：{type(e).__name__}: {e}")


async def load(layout_id: str) -> dict[str, Any] | None:
    """
    取一份户型。**内存优先**，与 `tasks.py` 的读取顺序相反。

    顺序不同的理由：这里存的是**输入数据**，进程活着时内存里的那份一定是最新的；
    而任务状态是**结果**，Redis 里的那份可能在别的进程写过。
    实际上两边内容一致，差别只在谁先命中、少一次序列化。
    """
    if not layout_id:
        return None
    if layout_id in _memory:
        return _memory[layout_id]

    try:
        client = await get_redis().client()
        if client is None:
            return None
        raw = await client.get(_key(layout_id))
        if raw:
            layout = json.loads(raw)
            _memory[layout_id] = layout   # 回填，下次走内存
            return layout
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[layout-store] 读 Redis 失败：{type(e).__name__}: {e}")
    return None


def clear() -> None:
    """仅测试用。"""
    _memory.clear()


__all__ = ["save", "load", "clear", "LAYOUT_TTL"]
