"""
文件路径: backend/app/core/redis_client.py
模块职责: Redis 封装 —— 承载需求文档 2.2.4 定义的四个角色
依赖关系: core/config.py（连接串与额度阈值）、core/logger.py（日志）

对应需求文档:
  - 2.2.4 Redis 的四个角色
  - 2.2.7 三角色权限模型（额度：user 解析 5 次/日、生成 3 次/日；designer/admin 不限）
  - 2.2.4 末尾：任务进度改为语义化 `phase` 字段（V2.2）
  - 4.4 任务状态查询接口：未完成任务返回 HTTP 200，用 phase/progress 表达

═══════════════════════════════════════════════════════════════════
四个角色与实现方式的对应
═══════════════════════════════════════════════════════════════════
| 角色       | 实现                              | 说明 |
|-----------|-----------------------------------|------|
| 会话状态   | langgraph-checkpoint-redis        | **不在本文件**，见 graph/workflow.py |
| 语义缓存   | STRING + TTL                      | 本文件 SemanticCache |
| 额度限流   | INCR + EXPIRE（原子）              | 本文件 QuotaLimiter |
| 任务进度   | HASH + TTL                        | 本文件 TaskProgressStore |

═══════════════════════════════════════════════════════════════════
降级设计（本机 Redis 未必常开）
═══════════════════════════════════════════════════════════════════
开发期 Redis 可能没起。**任何 Redis 故障都不得让主流程崩掉**，
但必须显式记录 —— 与 ADR-09「降级必须显式」的原则一致：

- 额度限流失败 -> **放行**并告警（宁可多放一次，不可误伤正常用户）
- 语义缓存失败 -> 当作未命中（退化到直调 LLM）
- 任务进度失败 -> 任务照跑，只是前端轮询拿不到进度

反之如果限流失败就拒绝请求，会把一个缓存故障放大成服务不可用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from loguru import logger

from .config import settings

__all__ = [
    "RedisClient", "get_redis", "QuotaExceeded", "QuotaResult",
    "Phase", "PHASE_TEXT", "TaskProgressStore", "SemanticCache",
]

TaskType = Literal["parse", "generate"]


# ══════════════════════════════════════════════════════════════════
# 语义化阶段（4.4 / V2.2）
# ══════════════════════════════════════════════════════════════════

Phase = Literal[
    "queued", "prechecking", "analyzing", "detecting_rooms",
    "extracting_dimensions", "diagnosing", "planning",
    "finalizing", "done", "degraded",
]

#: 阶段 -> 中文文案。**前端不必自己维护映射表**，直接展示 phase_text。
PHASE_TEXT: dict[str, str] = {
    "queued": "已接收，正在排队…",
    "prechecking": "正在检查图片质量…",
    "analyzing": "正在分析图片…",
    "detecting_rooms": "正在识别房间…",
    "extracting_dimensions": "正在提取尺寸与朝向…",
    "diagnosing": "正在生成户型诊断…",
    "planning": "正在生成装修方案…",
    "finalizing": "即将完成…",
    "done": "完成",
    "degraded": "已完成（降级模式）",
}

#: 阶段 -> progress 参考值（前端进度条用）
PHASE_PROGRESS: dict[str, int] = {
    "queued": 0, "prechecking": 5, "analyzing": 15, "detecting_rooms": 40,
    "extracting_dimensions": 65, "diagnosing": 85, "planning": 60,
    "finalizing": 95, "done": 100, "degraded": 100,
}


# ══════════════════════════════════════════════════════════════════
# 额度
# ══════════════════════════════════════════════════════════════════


class QuotaExceeded(RuntimeError):
    """额度用尽。API 层应转为 HTTP 429 并返回 reset_at。"""

    def __init__(self, task_type: str, limit: int, role: str, reset_at: str) -> None:
        self.task_type = task_type
        self.limit = limit
        self.role = role
        self.reset_at = reset_at
        super().__init__(
            f"{role} 角色今日 {task_type} 额度已用尽（{limit} 次/日），"
            f"将于 {reset_at} 重置"
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "limit": self.limit,
            "role": self.role,
            "reset_at": self.reset_at,
        }


@dataclass
class QuotaResult:
    """一次额度检查的结果。"""

    allowed: bool
    used: int
    limit: int | None       # None = 不限量
    remaining: int | None
    role: str
    reset_at: str
    unlimited: bool = False
    degraded: bool = False  # Redis 不可用时放行，标记为降级

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "role": self.role,
            "unlimited": self.unlimited,
            "reset_at": self.reset_at,
        }


# ══════════════════════════════════════════════════════════════════
# 客户端
# ══════════════════════════════════════════════════════════════════


class RedisClient:
    """
    Redis 统一入口。

    连接懒加载；Redis 不可用时所有方法退化为「安全默认值」并告警，
    绝不向上抛连接异常。
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url or settings.REDIS_URL
        self._client: Any = None
        self._broken = False        # 连接已失败过，后续直接走降级路径
        self._warned = False

    # ── 连接 ──────────────────────────────────────────────

    async def client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._broken:
            return None
        try:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(
                self._url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
            )
            await self._client.ping()
            logger.info(f"Redis 已连接: {self._url}")
            return self._client
        except Exception as e:  # noqa: BLE001
            self._broken = True
            self._client = None
            logger.warning(
                f"Redis 不可用（{type(e).__name__}: {e}），"
                f"额度/缓存/进度功能降级 —— 主流程不受影响"
            )
            return None

    async def close(self) -> None:
        """优雅停机时调用（AC-31）。"""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def ping(self) -> bool:
        c = await self.client()
        if c is None:
            return False
        try:
            await c.ping()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _warn_once(self, what: str, err: Exception) -> None:
        if not self._warned:
            logger.warning(f"Redis {what} 失败，走降级路径: {type(err).__name__}: {err}")
            self._warned = True

    # ══════════════════════════════════════════════════════
    # 角色二：额度限流
    # ══════════════════════════════════════════════════════

    @staticmethod
    def _role_limit(role: str, task_type: TaskType) -> int | None:
        """
        角色额度。None 表示不限量。

        见 2.2.7：admin / designer 不限量；user 解析 5 次/日、生成 3 次/日。
        """
        if role in ("admin", "designer"):
            return None
        return (
            settings.QUOTA_USER_PARSE_PER_DAY
            if task_type == "parse"
            else settings.QUOTA_USER_GENERATE_PER_DAY
        )

    @staticmethod
    def _seconds_to_midnight() -> int:
        from datetime import datetime, timedelta

        now = datetime.now()
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        return max(int((tomorrow - now).total_seconds()), 1)

    async def check_and_consume_quota(
        self, *, user_id: int, role: str, task_type: TaskType,
    ) -> QuotaResult:
        """
        检查并**原子消耗**一次额度。

        用 INCR + EXPIRE 原子计数。首次 INCR 时设过期到次日零点，
        保证按自然日重置（而不是滑动窗口）。

        Redis 不可用时放行并标记 degraded —— 宁可多放一次，
        不可把缓存故障放大成服务不可用。
        """
        today = date.today().isoformat()
        reset_at = f"{today} 24:00"
        limit = self._role_limit(role, task_type)

        if limit is None:
            return QuotaResult(
                allowed=True, used=0, limit=None, remaining=None,
                role=role, reset_at=reset_at, unlimited=True,
            )

        key = f"{settings.REDIS_QUOTA_PREFIX}:{task_type}:{user_id}:{today}"
        c = await self.client()

        if c is None:
            return QuotaResult(
                allowed=True, used=0, limit=limit, remaining=limit,
                role=role, reset_at=reset_at, degraded=True,
            )

        try:
            # pipeline 保证 INCR 与 EXPIRE 一起提交，避免计数无过期时间
            async with c.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, self._seconds_to_midnight())
                used, _ = await pipe.execute()

            used = int(used)
            if used > limit:
                # 超限时把多加的那次退回去，保持计数准确
                await c.decr(key)
                return QuotaResult(
                    allowed=False, used=limit, limit=limit, remaining=0,
                    role=role, reset_at=reset_at,
                )

            return QuotaResult(
                allowed=True, used=used, limit=limit,
                remaining=max(limit - used, 0), role=role, reset_at=reset_at,
            )

        except Exception as e:  # noqa: BLE001
            self._warn_once("额度检查", e)
            return QuotaResult(
                allowed=True, used=0, limit=limit, remaining=limit,
                role=role, reset_at=reset_at, degraded=True,
            )

    async def peek_quota(self, *, user_id: int, role: str, task_type: TaskType) -> QuotaResult:
        """只读查询额度，不消耗。供前端展示剩余次数。"""
        today = date.today().isoformat()
        reset_at = f"{today} 24:00"
        limit = self._role_limit(role, task_type)

        if limit is None:
            return QuotaResult(True, 0, None, None, role, reset_at, unlimited=True)

        key = f"{settings.REDIS_QUOTA_PREFIX}:{task_type}:{user_id}:{today}"
        c = await self.client()
        if c is None:
            return QuotaResult(True, 0, limit, limit, role, reset_at, degraded=True)

        try:
            used = int(await c.get(key) or 0)
        except Exception as e:  # noqa: BLE001
            self._warn_once("额度查询", e)
            used = 0

        return QuotaResult(
            allowed=used < limit, used=used, limit=limit,
            remaining=max(limit - used, 0), role=role, reset_at=reset_at,
        )

    # ══════════════════════════════════════════════════════
    # 角色三：语义缓存
    # ══════════════════════════════════════════════════════

    async def cache_get(self, bucket: str, key_parts: list[Any]) -> Any | None:
        """
        读缓存。key_parts 会做哈希，避免超长 key 与特殊字符。
        """
        c = await self.client()
        if c is None:
            return None
        try:
            raw = await c.get(self._cache_key(bucket, key_parts))
            return json.loads(raw) if raw else None
        except Exception as e:  # noqa: BLE001
            self._warn_once("缓存读", e)
            return None

    async def cache_set(
        self, bucket: str, key_parts: list[Any], value: Any, *, ttl: int = 3600,
    ) -> bool:
        c = await self.client()
        if c is None:
            return False
        try:
            await c.set(
                self._cache_key(bucket, key_parts),
                json.dumps(value, ensure_ascii=False),
                ex=ttl,
            )
            return True
        except Exception as e:  # noqa: BLE001
            self._warn_once("缓存写", e)
            return False

    @staticmethod
    def _cache_key(bucket: str, key_parts: list[Any]) -> str:
        """把任意参数序列化成定长 key，避免超长与特殊字符问题。"""
        raw = json.dumps(key_parts, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
        return f"{settings.REDIS_CACHE_PREFIX}:{bucket}:{digest}"

    # ══════════════════════════════════════════════════════
    # 角色四：任务进度
    # ══════════════════════════════════════════════════════

    async def set_task_progress(
        self,
        task_id: str,
        *,
        status: Literal["pending", "processing", "completed", "failed"],
        phase: Phase,
        progress: int | None = None,
        degraded: bool = False,
        trace_id: str | None = None,
        result: Any | None = None,
        error: str | None = None,
        ttl: int | None = None,
    ) -> None:
        """
        写入任务进度到 Hash。

        **`phase` 是必须的**：用户看到「正在识别房间…」比看到「60%」有用得多（2.2.4）。

        Args:
            ttl: 结果保留秒数。默认 1 小时 —— 支持页面刷新后重新获取（4.4）。
        """
        c = await self.client()
        if c is None:
            return

        key = f"{settings.REDIS_TASK_PREFIX}:{task_id}"
        mapping: dict[str, str] = {
            "task_id": task_id,
            "status": status,
            "phase": phase,
            "phase_text": PHASE_TEXT.get(phase, phase),
            "progress": str(progress if progress is not None
                            else PHASE_PROGRESS.get(phase, 0)),
            "degraded": "1" if degraded else "0",
            "updated_at": date.today().isoformat(),
        }
        # trace_id 必须落库：异步任务恢复时要靠它重新绑定日志上下文（ADR-13）
        if trace_id:
            mapping["trace_id"] = trace_id
        if result is not None:
            mapping["result"] = json.dumps(result, ensure_ascii=False, default=str)
        if error:
            mapping["error"] = error[:2000]

        try:
            await c.hset(key, mapping=mapping)
            await c.expire(key, ttl or 3600)
        except Exception as e:  # noqa: BLE001
            self._warn_once("任务进度写", e)

    async def get_task_progress(self, task_id: str) -> dict[str, Any] | None:
        """
        读任务进度。返回的 dict 可直接作为 API 响应的 data 部分。

        未完成任务同样返回 200（用 status/phase 表达），
        不用 HTTP 状态码表示"还没好"——那会让前端拦截器误判为错误（4.4）。
        """
        c = await self.client()
        if c is None:
            return None
        try:
            raw = await c.hgetall(f"{settings.REDIS_TASK_PREFIX}:{task_id}")
        except Exception as e:  # noqa: BLE001
            self._warn_once("任务进度读", e)
            return None

        if not raw:
            return None

        out: dict[str, Any] = {
            "task_id": raw.get("task_id", task_id),
            "status": raw.get("status", "pending"),
            "phase": raw.get("phase", "queued"),
            "phase_text": raw.get("phase_text", PHASE_TEXT["queued"]),
            "progress": int(raw.get("progress", 0)),
            "degraded": raw.get("degraded") == "1",
        }
        if raw.get("trace_id"):
            out["trace_id"] = raw["trace_id"]
        if raw.get("result"):
            try:
                out["result"] = json.loads(raw["result"])
            except json.JSONDecodeError:
                out["result"] = None
        if raw.get("error"):
            out["error"] = raw["error"]
        return out


# ══════════════════════════════════════════════════════════════════
# 便捷别名（让业务代码读起来更自然）
# ══════════════════════════════════════════════════════════════════


class QuotaLimiter:
    """`RedisClient` 额度能力的语义化包装。"""

    def __init__(self, client: RedisClient) -> None:
        self._c = client

    async def consume(self, *, user_id: int, role: str, task_type: TaskType) -> QuotaResult:
        return await self._c.check_and_consume_quota(
            user_id=user_id, role=role, task_type=task_type
        )

    async def peek(self, *, user_id: int, role: str, task_type: TaskType) -> QuotaResult:
        return await self._c.peek_quota(user_id=user_id, role=role, task_type=task_type)


class TaskProgressStore:
    """`RedisClient` 任务进度能力的语义化包装。"""

    def __init__(self, client: RedisClient) -> None:
        self._c = client

    async def update(self, task_id: str, **kwargs: Any) -> None:
        await self._c.set_task_progress(task_id, **kwargs)

    async def read(self, task_id: str) -> dict[str, Any] | None:
        return await self._c.get_task_progress(task_id)


class SemanticCache:
    """`RedisClient` 语义缓存能力的语义化包装。"""

    def __init__(self, client: RedisClient) -> None:
        self._c = client

    async def get(self, bucket: str, key_parts: list[Any]) -> Any | None:
        return await self._c.cache_get(bucket, key_parts)

    async def set(self, bucket: str, key_parts: list[Any], value: Any, *, ttl: int = 3600) -> bool:
        return await self._c.cache_set(bucket, key_parts, value, ttl=ttl)


# ══════════════════════════════════════════════════════════════════
# 进程级单例
# ══════════════════════════════════════════════════════════════════

_redis: RedisClient | None = None


def get_redis() -> RedisClient:
    global _redis
    if _redis is None:
        _redis = RedisClient()
    return _redis
