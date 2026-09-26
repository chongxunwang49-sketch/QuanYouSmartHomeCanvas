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
三层：内存 → Redis → PostgreSQL
═══════════════════════════════════════════════════════════════════
2026-09-24 起，户型也落库了（`house_layouts`，见 `db/migrations/`）。
三层各有各的职责，**不是"三份备份"**：

    内存   最快，进程内。命中率最高 —— 解析完紧接着生成就是这条路径。
    Redis  跨进程、带 TTL。**TTL 是特性不是缺陷**：它是"暂存"，
           过期即消失，演示环境不留垃圾。
    PG     **持久真相**。没有 TTL，进程重启/容器重建都还在。
           查询走它，才知道"这个 layout_id 到底有没有过"。

读的顺序是 内存 → Redis → PG，每层命中都**回填上一层** ——
所以第一次从库里捞出来之后，后面的读取都是内存速度。

写则是三层都写：PG 先写（它决定数据"存在过"），Redis 与内存尽力而为。
⚠️ PG 写失败**不阻止**解析任务成功：这套设计里 PG 是**加固**而不是依赖，
写不进去时行为退回落库之前 —— 户型活 1 小时，重启就没了。
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

#: 「房间 → 地面材料」的替换记录（AC-10），与户型同样的三层。
#:
#: ⚠️ 键要转成字符串存：JSON 的键只能是字符串，而**房间下标是 int**。
#:    读回来必须转回 int，否则调用方 `floor_fills[idx]` 永远取不到值 ——
#:    而那是"换了材质但图上没变化"这种不报错的失败。
_floor_memory: dict[str, dict[int, str]] = {}


#: 户型暂存自己的前缀。
#:
#: ⚠️ **2026-09-24 从 `qy:task:layout:<id>` 改到这里**（原来共用
#: `REDIS_TASK_PREFIX`）。起因是 AC-12 的启动接管去 SCAN `qy:task:*`
#: 找没跑完的任务，结果一头撞上这些户型暂存的 **STRING** 键：
#:
#:     redis.exceptions.ResponseError: WRONGTYPE Operation against
#:     a key holding the wrong kind of value
#:
#: 两个完全不同的数据族共用一个前缀，**平时看不出来**（各读各的键），
#: 一旦有人按前缀扫就会炸 —— 而"按前缀扫"正是恢复类功能的常规做法。
#: 换前缀没有迁移成本：暂存本来就带 1 小时 TTL、只服务于开发期。
LAYOUT_PREFIX = f"{settings.REDIS_TASK_PREFIX}:layout"


def _key(layout_id: str) -> str:
    # 注意仍是 `qy:task:layout:*` 的形状 —— 变的是**归属**：
    # 它现在由本模块自己的常量描述，而不是借任务那套前缀。
    return f"{LAYOUT_PREFIX}:{layout_id}"


async def save(
    layout_id: str,
    layout: dict[str, Any],
    *,
    user_id: int | None = None,
    diagnosis: dict[str, Any] | None = None,
    confidence: float | None = None,
    model_used: str | None = None,
    degraded: bool = False,
    degrade_reason: str | None = None,
    parse_elapsed_ms: int | None = None,
) -> None:
    """
    存一份户型。内存必写，PG 与 Redis 尽力而为。

    多余的关键字参数只给 PG 用（诊断结果、耗时、降级原因这些是**落库才有
    意义**的信息：内存里存了也没人读，Redis 里存了还占空间）。
    签名保持向后兼容 —— 老调用方 `save(id, layout)` 照常工作。
    """
    if not layout_id:
        return
    _memory[layout_id] = layout

    # ── PG：持久真相，先写 ─────────────────────────────────
    #
    # ⚠️ 先写 PG 而不是最后写：它决定"这份户型存在过"。Redis 有 TTL，
    #    内存随进程消失，只有它不会 —— 所以它该是第一个被写下的。
    #    写失败不阻止后两步：见模块说明，PG 是加固不是依赖。
    try:
        from ..db import repository

        await repository.save_layout(
            layout_id, layout,
            user_id=user_id, diagnosis=diagnosis, confidence=confidence,
            model_used=model_used, degraded=degraded,
            degrade_reason=degrade_reason, parse_elapsed_ms=parse_elapsed_ms,
        )
    except Exception as e:  # noqa: BLE001 —— 落库失败不该让解析任务失败
        logger.warning(f"[layout-store] 写库失败（已存内存）：{type(e).__name__}: {e}")

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

    最后一层是 PG —— 走到那里说明内存没有、Redis 也过期了（或不通），
    也就是"1 小时前解析的那份、而进程重启过"。这正是落库要解决的问题，
    所以这一层命中时**必须回填** Redis 与内存，否则下一次读又要落回来。

    ⚠️ 回填 Redis 时**重新给满 TTL**。这会让"一小时前"的数据再活一小时，
    看起来像延长了保留期 —— 但这是对的：用户此刻正在用它，
    读一次就过期会让"刚打开旧户型、点了生成、任务跑着、"变成失败。
    """
    if not layout_id:
        return None
    if layout_id in _memory:
        return _memory[layout_id]

    try:
        client = await get_redis().client()
        if client is not None:
            raw = await client.get(_key(layout_id))
            if raw:
                layout = json.loads(raw)
                _memory[layout_id] = layout   # 回填，下次走内存
                return layout
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[layout-store] 读 Redis 失败：{type(e).__name__}: {e}")

    # ── PG：最后一道，也是最持久的一道 ──────────────────────
    try:
        from ..db import repository

        layout = await repository.load_layout(layout_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[layout-store] 读库失败：{type(e).__name__}: {e}")
        return None

    if layout:
        logger.info(f"[layout-store] 从数据库捞回户型 {layout_id}（内存与缓存都没有）")
        _memory[layout_id] = layout
        try:
            client = await get_redis().client()
            if client is not None:
                await client.set(_key(layout_id),
                                 json.dumps(layout, ensure_ascii=False),
                                 ex=LAYOUT_TTL)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[layout-store] 回填 Redis 失败（不影响本次读取）：{e}")
    return layout


# ══════════════════════════════════════════════════════════════════
# 地面材质替换（AC-10）
# ══════════════════════════════════════════════════════════════════
#
# 走的是与户型**同一套三层**（内存 → Redis → PG），TTL 也一致：
# 替换记录属于"这台户型这次会话里的探索"，户型没了它也没有意义。
#
# ⚠️ 读的顺序与户型相反吗？不，一样（内存优先）。
#    理由也相同：这是**用户刚做的选择**，内存里那份一定最新。


def _floor_key(layout_id: str) -> str:
    # 与户型键**同一前缀、不同后缀** —— 共用前缀是有意的（同一个数据族：
    # 都属于这份户型），而 `:floors` 后缀把它们分开，不会互相覆盖。
    return f"{_key(layout_id)}:floors"


async def save_floor_materials(layout_id: str, mapping: dict[int, str]) -> None:
    """
    整份覆盖「房间 → 地面材料」。**永不抛异常**（与 `save` 同一条纪律）：
    换地板这件交互不该因为库挂了而失败，最差是内存里记着。

    `mapping` 为空 = 全部还原成默认房型配色，这也要真的写下去
    （不是"空就不写"—— 那样用户清空之后刷新页面又回来了）。
    """
    if not layout_id:
        return
    _floor_memory[layout_id] = dict(mapping)

    try:
        from ..db import repository

        await repository.save_floor_materials(layout_id, mapping)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[layout-store] 写地面材质失败（已存内存）：{type(e).__name__}: {e}")

    try:
        client = await get_redis().client()
        if client is None:
            return
        await client.set(_floor_key(layout_id),
                         json.dumps({str(k): v for k, v in mapping.items()},
                                    ensure_ascii=False),
                         ex=LAYOUT_TTL)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[layout-store] 写地面材质到 Redis 失败（已存内存）：{type(e).__name__}: {e}")


async def load_floor_materials(layout_id: str) -> dict[int, str]:
    """
    读回「房间 → 地面材料」。**键一律是 int**（见 `_floor_memory` 的说明）。

    ⚠️ 与户型的 `load` 有一处**有意的不一样**：这里读不到就返回空 dict，
       **不区分"没有"与"库不通"**。因为对调用方（渲染）来说两者动作相同：
       用默认配色画。而户型读不到是"整份都没有"，那要报 4004 —— 差别在这。
    """
    if not layout_id:
        return {}
    if layout_id in _floor_memory:
        return dict(_floor_memory[layout_id])

    try:
        client = await get_redis().client()
        if client is not None:
            raw = await client.get(_floor_key(layout_id))
            if raw:
                got = _as_int_keyed(json.loads(raw))
                _floor_memory[layout_id] = got
                return dict(got)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[layout-store] 读地面材质 Redis 失败：{type(e).__name__}: {e}")

    try:
        from ..db import repository

        got = await repository.load_floor_materials(layout_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[layout-store] 读地面材质库失败：{type(e).__name__}: {e}")
        return {}

    if got:
        _floor_memory[layout_id] = dict(got)
        # 回填 Redis，下次少走一层（与 `load` 同一个理由）
        try:
            client = await get_redis().client()
            if client is not None:
                await client.set(_floor_key(layout_id),
                                 json.dumps({str(k): v for k, v in got.items()},
                                            ensure_ascii=False),
                                 ex=LAYOUT_TTL)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[layout-store] 回填地面材质 Redis 失败（不影响本次）：{e}")
    return dict(got) if got else {}


def _as_int_keyed(raw: Any) -> dict[int, str]:
    """JSON 的键是字符串 → 转成 int 键。坏键跳过而不是丢整份。"""
    out: dict[int, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
    return out


def clear() -> None:
    """仅测试用。"""
    _memory.clear()
    _floor_memory.clear()


__all__ = ["save", "load", "clear", "LAYOUT_TTL",
           "save_floor_materials", "load_floor_materials"]
