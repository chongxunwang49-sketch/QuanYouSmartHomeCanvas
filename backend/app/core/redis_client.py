"""
文件路径: backend/app/core/redis_client.py
模块职责: Redis 封装 —— 承载需求文档 11.2.4 定义的四个角色
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
| 会话状态   | langgraph-checkpoint-**postgres** | **不在本文件**，见 graph/workflow.py。2026-09-24 由 Redis 改来（Redis 镜像没有 RediSearch，跑不了）|
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
from .progress import PHASE_TEXT, Phase, progress_at

__all__ = [
    "RedisClient", "get_redis", "QuotaExceeded", "QuotaResult",
    "PHASE_TEXT", "Phase", "TaskProgressStore", "SemanticCache",
]

TaskType = Literal["parse", "generate"]

# 阶段词汇表、文案、进度/剩余时间模型都在 `core/progress.py`。
# ⚠️ 这里**只导入、不重定义**。曾经 `PHASE_PROGRESS` 就住在本文件，
# 而它是"一张全局表"，用在方案链路上会让进度条先冲到 85% 再倒回 60% ——
# 见 progress.py 模块说明。进度现在是 `(kind, phase)` 的函数，不是一张表。


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
        """
        ⚠️ **`degraded` 必须在这里面。**

        它原来没被序列化出去，后果是：Redis 挂掉时额度走"放行 + 标降级"，
        而**降级这个事实传不到任何调用方** —— API 层只看 `allowed`，
        于是"额度检查没做成"和"额度够用"在响应里长得一模一样。

        实测的连带后果（2026-09-23）：`tests/test_api.py` 的
        `test_超额响应说清额度与重置时间` 在跑完 8 个用例之后会**偶发失败**，
        报 `assert 0 == 4006` —— 因为那时 Redis 已经被前面那些用例拖到
        报错，额度静默放行了。断言看不出是"权限逻辑错了"还是"Redis 累了"。

        这正是本项目「降级必须显式」那条原则（AC-17）要防的情形：
        降级本身可以接受，**静默降级不行**。
        """
        return {
            "allowed": self.allowed,
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "role": self.role,
            "unlimited": self.unlimited,
            "reset_at": self.reset_at,
            "degraded": self.degraded,
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
                # 连接超时 3s：连不上就是"Redis 不在"，短一点让它早点进降级。
                socket_connect_timeout=3,
                # ⚠️ **读超时 10s（原来 3s），这是 2026-09-23 实测调大的。**
                #
                # 原来 3s 对**本机** Redis 看着很宽松（实际往返 <1ms），
                # 但它衡量的是**事件循环的调度延迟**，不是网络。实测：
                # 测试进程中累积了多个在飞的图任务（`generate` fan-out 九路）
                # 之后，Redis 往返被挤过 3s —— 报
                #     redis.exceptions.TimeoutError: Timeout reading from localhost:6379
                # 而额度检查一旦超时就**放行且不计数**（见下面的降级策略），
                # 于是"配额上限"在演示中会**静默失效**。
                #
                # 10s 仍然远小于 `LLM_TIMEOUT_SECONDS`(120s) 与节点超时，
                # 不破坏三层超时不变式（内层 < 节点 < 全局）；它只是把
                # "事件循环忙了一下"和"Redis 真的挂了"区分开。
                socket_timeout=10,
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
        kind: str,
        status: Literal["pending", "processing", "completed", "failed"],
        phase: Phase,
        progress: int,
        started_at: float | None = None,
        phase_entered_at: float | None = None,
        degraded: bool = False,
        user_id: int | None = None,
        cancelled: bool = False,
        trace_id: str | None = None,
        result: Any | None = None,
        error: str | None = None,
        ttl: int | None = None,
    ) -> None:
        """
        写入任务进度到 Hash。

        **`phase` 是必须的**：用户看到「正在识别房间…」比看到「60%」有用得多（2.2.4）。

        Args:
            kind: 任务类型。**必须落库**：进度与剩余时间都是 `(kind, phase)`
                的函数（见 `core/progress.py`），而 `status()` 可能在一个
                刚重启的进程里被调用 —— 那时内存里的 TaskRecord 已经没了，
                kind 只能从 Hash 里读。少了它，重启后的任务会失去剩余时间。
            progress: 进入该阶段时的百分比。**由调用方算**，不在这里查表 ——
                只有 runner 知道这个任务是什么类型。
            started_at / phase_entered_at: epoch 秒。剩余时间要用它们算
                "已经走了多久、这个阶段走了多久"。不存的话重启后只能从零算。
            ttl: 结果保留秒数。默认 1 小时 —— 支持页面刷新后重新获取（4.4）。
        """
        c = await self.client()
        if c is None:
            return

        key = f"{settings.REDIS_TASK_PREFIX}:{task_id}"
        mapping: dict[str, str] = {
            "task_id": task_id,
            "kind": kind,
            "status": status,
            "phase": phase,
            "phase_text": PHASE_TEXT.get(phase, phase),
            "progress": str(progress),
            "degraded": "1" if degraded else "0",
            # ⚠️ `cancelled` 也要落库：取消之后前端要能说出"已中断"而不是
            #    "失败"，而轮询接口可能正走 Redis 这条路径。
            "cancelled": "1" if cancelled else "0",
            "updated_at": date.today().isoformat(),
        }
        # user_id 落库是为了归属审计：进程重启后 `rec` 没了，
        # 但"这个任务是谁的"仍然查得到。
        if user_id is not None:
            mapping["user_id"] = str(user_id)
        for name, value in (("started_at", started_at), ("phase_entered_at", phase_entered_at)):
            if value is not None:
                mapping[name] = f"{value:.3f}"
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
        读任务进度。返回的字段足以让 `TaskManager.status()` 拼出完整响应。

        未完成任务同样返回 200（用 status/phase 表达），
        不用 HTTP 状态码表示"还没好"——那会让前端拦截器误判为错误（4.4）。

        ⚠️ 这里**只做反序列化，不算进度**。进度/剩余时间是 `(kind, phase, 时间)`
        的函数，计算统一放在 `TaskManager.status()` —— 两条读取路径
        （Redis / 内存）都过那里，才不会出现"Redis 在不在决定了界面显示什么"。
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
            "kind": raw.get("kind", ""),
            "status": raw.get("status", "pending"),
            "phase": raw.get("phase", "queued"),
            "phase_text": raw.get("phase_text", PHASE_TEXT["queued"]),
            "progress": int(raw.get("progress", 0)),
            "degraded": raw.get("degraded") == "1",
            "cancelled": raw.get("cancelled") == "1",
        }
        if raw.get("user_id"):
            try:
                out["user_id"] = int(raw["user_id"])
            except ValueError:
                pass
        for name in ("started_at", "phase_entered_at"):
            raw_value = raw.get(name)
            if raw_value:
                try:
                    out[name] = float(raw_value)
                except ValueError:
                    pass          # 脏数据当作不知道，别让整个响应挂掉
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

    async def find_unfinished(self) -> list[dict[str, Any]]:
        """
        扫出所有**没跑完**的任务记录（`pending` / `processing`）。AC-12 的启动接管用它。

        ⚠️ **为什么必须要它**：AC-31 的"退出前不留悬挂记录"只在**优雅停机**
        （SIGTERM → 排空 → 记账）时成立。进程被 SIGKILL、断电、容器 OOM 时
        没有任何代码能有机会执行 —— 那些记录会永远停在 `processing`，
        而轮询接口会一直告诉前端"正在分析图片…"。**一个谎，而且永远不会自愈。**

        用 `SCAN` 而不是 `KEYS`：`KEYS` 在大 keyspace 上是阻塞的，
        而它要在**启动路径**上跑（那正是最不该卡住的时候）。
        """
        c = await self._c.client()
        if c is None:
            return []

        out: list[dict[str, Any]] = []
        try:
            async for key in c.scan_iter(
                match=f"{settings.REDIS_TASK_PREFIX}:*", count=200
            ):
                # ⚠️ **每个键单独兜底。** 原来这一句没有 try，于是任何一个
                #    类型不对的键都会让**整次恢复**返回空 —— 而恢复是"越早越好"
                #    的功能，静默失效最糟。实测踩过：户型暂存曾与任务共用
                #    `qy:task:` 前缀（存的是 STRING），扫描一头撞上 WRONGTYPE，
                #    结果所有悬挂任务都捞不出来，日志里只有一行 warn_once。
                #    （那一处前缀已在 store.py 里改掉；这里的兜底是第二道防线。）
                try:
                    raw = await c.hgetall(key)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[task] 跳过无法读取的键 {key}："
                                 f"{type(e).__name__}: {e}")
                    continue
                if not raw:
                    continue
                if raw.get("status") not in ("pending", "processing"):
                    continue
                data = {
                    "task_id": raw.get("task_id") or str(key).split(":")[-1],
                    "kind": raw.get("kind", ""),
                    "status": raw.get("status"),
                    "phase": raw.get("phase", "queued"),
                    "trace_id": raw.get("trace_id", ""),
                }
                for name in ("started_at", "phase_entered_at"):
                    try:
                        data[name] = float(raw[name]) if raw.get(name) else None
                    except (TypeError, ValueError):
                        data[name] = None
                try:
                    data["user_id"] = int(raw["user_id"]) if raw.get("user_id") else None
                except (TypeError, ValueError):
                    data["user_id"] = None
                out.append(data)
        except Exception as e:  # noqa: BLE001 —— 扫不出来不该让启动失败
            self._c._warn_once("未完成任务扫描", e)
            return []
        return out


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


_quota: QuotaLimiter | None = None


def get_quota_limiter() -> QuotaLimiter:
    """
    额度限流的进程级单例（AC-13）。

    与 `get_redis()` 同一个形状。单独开一个访问器而不是让调用方自己
    `QuotaLimiter(get_redis())` —— 后者每次请求都新建一个包装对象，
    虽然它没有状态、代价可以忽略，但**每次 new 一个"限流器"读起来
    像是每次都在重置限流**，容易被误读。
    """
    global _quota
    if _quota is None:
        _quota = QuotaLimiter(get_redis())
    return _quota


def reset_redis_client() -> None:
    """
    丢掉进程级单例。**仅供测试**，与 `api/tasks.py` 的 `reset_task_manager()`
    是同一个模式。

    ⚠️ 为什么测试必须调它：`redis.asyncio` 的客户端**绑定创建它的事件循环**。
    而 pytest-asyncio 默认**每个用例一个新循环** —— 单例一旦被第一个用例
    创建，后续用例拿到的是"绑定在已关闭循环上的客户端"，
    报 `RuntimeError: Event loop is closed`。症状是"文件里第一个碰 Redis 的
    用例通过、后面的全挂"，很容易被误读成业务代码有并发 bug。

    生产环境只有一个循环，不存在这个问题 —— 所以这是纯粹的测试隔离需求，
    不是缺陷修补。
    """
    global _redis, _quota
    _redis = None
    _quota = None
