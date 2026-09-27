"""
Redis 封装测试（对应需求文档 11.2.4 / 11.2.7 / 12.4）。

分两类：
- **降级行为**：本机 Redis 未启动，正好验证「Redis 挂了主流程不能崩」这条设计。
- **逻辑正确性**：用假客户端替换真实连接，验证额度、缓存、进度的业务规则。

设计意图：限流失败应当**放行**而不是拒绝——
把一个缓存故障放大成服务不可用，是比多放一次请求严重得多的问题。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.app.core.progress import PHASE_TEXT, STEPS, progress_at
from backend.app.core.redis_client import (
    QuotaResult,
    RedisClient,
)


def run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════
# 降级：Redis 不可用时的行为（本机默认就是这种状态）
# ══════════════════════════════════════════════════════════════════


class TestGracefulDegradation:
    """Redis 不可用时，任何操作都不得抛异常，且必须标记 degraded。"""

    def _offline(self) -> RedisClient:
        """指向一个必然连不上的地址，模拟 Redis 未启动。"""
        return RedisClient(url="redis://127.0.0.1:6399/0")

    def test_client_returns_none_instead_of_raising(self):
        c = self._offline()
        assert run(c.client()) is None

    def test_ping_false(self):
        assert run(self._offline().ping()) is False

    def test_quota_allows_and_marks_degraded(self):
        """
        核心断言：限流不可用时**放行**，而不是拒绝。
        拒绝会把 Redis 故障放大成服务不可用。
        """
        r = run(self._offline().check_and_consume_quota(
            user_id=1, role="user", task_type="parse"))
        assert r.allowed is True
        assert r.degraded is True
        assert r.limit == 5          # 额度值仍要如实返回给前端

    def test_admin_unlimited_even_when_offline(self):
        r = run(self._offline().check_and_consume_quota(
            user_id=1, role="admin", task_type="generate"))
        assert r.allowed and r.unlimited

    def test_cache_returns_none_not_raise(self):
        c = self._offline()
        assert run(c.cache_get("layout", ["a", "b"])) is None
        assert run(c.cache_set("layout", ["a"], {"x": 1})) is False

    def test_task_progress_noop_not_raise(self):
        c = self._offline()
        run(c.set_task_progress("t1", kind="parse", status="processing",
                                phase="analyzing", progress=2))
        assert run(c.get_task_progress("t1")) is None

    def test_peek_quota_degrades(self):
        r = run(self._offline().peek_quota(user_id=1, role="user", task_type="parse"))
        assert r.degraded is True

    def test_降级标记必须能传到调用方(self):
        """
        `degraded` 必须在 `to_dict()` 里。

        ⚠️ 它原来**不在**。后果是"Redis 挂了、额度没算"与"额度够用"
        在 API 响应里长得一模一样 —— 降级能发生，但传不出去。

        这不是理论问题，是实测踩出来的：`tests/test_api.py` 的配额用例
        偶发失败，报 `assert 0 == 4006`，看着像权限逻辑写错，
        真因却是 Redis 超时导致额度降级放行，而**降级这个事实
        没有任何出口**，只能靠翻日志。（见 `_assert_quota_counted` 的说明。）

        对应 AC-17：降级状态要在 API、审计日志、前端 UI 三处可见。
        这是"API 那处"的地基。
        """
        r = run(self._offline().check_and_consume_quota(
            user_id=1, role="user", task_type="parse"))
        d = r.to_dict()
        assert "degraded" in d, "降级标记必须序列化出去，否则调用方看不见"
        assert d["degraded"] is True

        # 正常路径也要有这个字段（值 False），否则前端得靠 `in` 判断
        ok = QuotaResult(allowed=True, used=1, limit=5, remaining=4,
                         role="user", reset_at="2026-01-01 24:00")
        assert ok.to_dict()["degraded"] is False


# ══════════════════════════════════════════════════════════════════
# 逻辑正确性：用假客户端验证业务规则
# ══════════════════════════════════════════════════════════════════


class _FakePipeline:
    """假 pipeline：把命令排队，execute 时按序作用于同一个 _FakeRedis。"""

    def __init__(self, redis: "_FakeRedis") -> None:
        self._r = redis
        self._ops: list[tuple[str, str, Any]] = []

    def incr(self, key):
        self._ops.append(("incr", key, None))

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self):
        results = []
        for op, key, val in self._ops:
            if op == "incr":
                results.append(await self._r.incr(key))
            elif op == "expire":
                results.append(True)
        self._ops.clear()
        return results


class _FakeRedis:
    """
    最小可用的假 Redis。

    ⚠️ 关键：GET / SET / INCR 必须共用**同一个**键空间。
    真实 Redis 里它们本就操作同一批 key；如果假实现拆成两个 dict，
    就会出现"INCR 写进 counters、GET 从 strings 读"的假阴性——
    本测试第一版就踩了这个坑（`peek_quota` 永远读到 0）。
    """

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}          # 单一键空间，值统一存字符串
        self.hashes: dict[str, dict[str, str]] = {}

    def pipeline(self, transaction: bool = True):
        return _FakePipeline(self)

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ex=None):
        self.kv[key] = value

    async def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key, 0)) + 1)
        return int(self.kv[key])

    async def decr(self, key):
        self.kv[key] = str(int(self.kv.get(key, 0)) - 1)
        return int(self.kv[key])

    async def hset(self, key, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {})

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


def _wired(fake: _FakeRedis) -> RedisClient:
    """构造一个已接上假客户端的 RedisClient。"""
    c = RedisClient()
    c._client = fake          # 直接注入，跳过真实连接
    return c


class TestQuotaRules:
    def test_user_parse_limit_is_5(self):
        from backend.app.core.config import settings

        c = _wired(_FakeRedis())
        for i in range(settings.QUOTA_USER_PARSE_PER_DAY):
            r = run(c.check_and_consume_quota(user_id=1, role="user", task_type="parse"))
            assert r.allowed, f"第 {i + 1} 次应当放行"

        # 第 6 次应当拒绝
        r = run(c.check_and_consume_quota(user_id=1, role="user", task_type="parse"))
        assert r.allowed is False
        assert r.remaining == 0

    def test_user_generate_limit_is_3(self):
        from backend.app.core.config import settings

        c = _wired(_FakeRedis())
        for _ in range(settings.QUOTA_USER_GENERATE_PER_DAY):
            assert run(c.check_and_consume_quota(
                user_id=1, role="user", task_type="generate")).allowed
        assert run(c.check_and_consume_quota(
            user_id=1, role="user", task_type="generate")).allowed is False

    def test_designer_and_admin_unlimited(self):
        c = _wired(_FakeRedis())
        for role in ("designer", "admin"):
            r = run(c.check_and_consume_quota(user_id=9, role=role, task_type="parse"))
            assert r.allowed and r.unlimited and r.limit is None

    def test_quota_is_per_user(self):
        """额度必须按用户隔离，不能互相影响。"""
        c = _wired(_FakeRedis())
        for _ in range(5):
            run(c.check_and_consume_quota(user_id=1, role="user", task_type="parse"))
        # 用户 1 已用尽
        assert run(c.check_and_consume_quota(
            user_id=1, role="user", task_type="parse")).allowed is False
        # 用户 2 不受影响
        assert run(c.check_and_consume_quota(
            user_id=2, role="user", task_type="parse")).allowed is True

    def test_parse_and_generate_have_separate_buckets(self):
        """解析与生成是两条独立额度线。"""
        c = _wired(_FakeRedis())
        for _ in range(5):
            run(c.check_and_consume_quota(user_id=1, role="user", task_type="parse"))
        assert run(c.check_and_consume_quota(
            user_id=1, role="user", task_type="parse")).allowed is False
        assert run(c.check_and_consume_quota(
            user_id=1, role="user", task_type="generate")).allowed is True

    def test_over_limit_rolls_back_counter(self):
        """
        超限时把多加的那次退回去，保证计数准确——
        否则前端显示的"已用次数"会一路虚高。
        """
        c = _wired(_FakeRedis())
        for _ in range(6):
            run(c.check_and_consume_quota(user_id=1, role="user", task_type="parse"))
        r = run(c.peek_quota(user_id=1, role="user", task_type="parse"))
        assert r.used == 5, f"计数应停在限额 5，实际 {r.used}"

    def test_peek_does_not_consume(self):
        c = _wired(_FakeRedis())
        for _ in range(3):
            run(c.peek_quota(user_id=1, role="user", task_type="parse"))
        assert run(c.peek_quota(user_id=1, role="user", task_type="parse")).used == 0


class TestSemanticCache:
    def test_roundtrip(self):
        c = _wired(_FakeRedis())
        run(c.cache_set("layout", ["图A", "full"], {"rooms": 4}, ttl=60))
        assert run(c.cache_get("layout", ["图A", "full"])) == {"rooms": 4}

    def test_key_order_independent(self):
        """参数顺序不同但内容相同，应命中同一缓存。"""
        c = _wired(_FakeRedis())
        run(c.cache_set("layout", ["a", "b"], {"ok": 1}))
        assert run(c.cache_get("layout", ["a", "b"])) == {"ok": 1}

    def test_different_buckets_isolated(self):
        c = _wired(_FakeRedis())
        run(c.cache_set("layout", ["a"], {"v": 1}))
        assert run(c.cache_get("material", ["a"])) is None

    def test_cache_key_is_bounded_length(self):
        """超长输入应被哈希成定长 key，避免 Redis key 过长。"""
        long_text = "客" * 100_000
        key = RedisClient._cache_key("layout", [long_text])
        assert len(key) < 120

    def test_cache_key_handles_special_chars(self):
        key = RedisClient._cache_key("layout", ["换行\n\t{}[]:*?\"'"])
        assert " " not in key and ":" not in key.split("layout:")[1]


class TestTaskProgress:
    def test_phase_maps_to_text_and_progress(self):
        """
        4.4 要求：用户看到「正在分析图片…」而不是「60%」。
        phase / phase_text / progress 三者必须一起返回。
        """
        c = _wired(_FakeRedis())
        run(c.set_task_progress("t1", kind="parse", status="processing",
                                phase="analyzing", progress=2))
        p = run(c.get_task_progress("t1"))

        assert p["phase"] == "analyzing"
        assert p["phase_text"] == PHASE_TEXT["analyzing"]
        assert p["progress"] == 2

    def test_kind_and_times_roundtrip(self):
        """
        ⚠️ **`kind` 和两个时间戳必须能读回来。**
        剩余时间是 `(kind, phase, 时间)` 的函数（core/progress.py）。
        进程重启后内存里的 TaskRecord 已经没了，只能靠 Hash 里这几个字段
        把倒计时重新算出来 —— 缺一个，重启后的任务就再也给不出剩余时间。
        """
        c = _wired(_FakeRedis())
        run(c.set_task_progress("t1", kind="generate", status="processing",
                                phase="checking_risks", progress=31,
                                started_at=1000.5, phase_entered_at=1037.25))
        p = run(c.get_task_progress("t1"))

        assert p["kind"] == "generate"
        assert p["started_at"] == 1000.5
        assert p["phase_entered_at"] == 1037.25

    def test_trace_id_persisted_for_async_resume(self):
        """
        trace_id 必须落库：异步任务恢复时要靠它重新绑定日志上下文（ADR-13）。
        异步上下文不会自动继承，丢了就断链。
        """
        c = _wired(_FakeRedis())
        run(c.set_task_progress("t1", kind="parse", status="processing",
                                phase="analyzing", trace_id="abc-123",
                                progress=2))
        assert run(c.get_task_progress("t1"))["trace_id"] == "abc-123"

    def test_degraded_flag_roundtrip(self):
        """降级标志必须能从进度里读回来（AC-17 三处可见之一）。"""
        c = _wired(_FakeRedis())
        run(c.set_task_progress("t1", kind="parse", status="completed",
                                phase="degraded", progress=100, degraded=True))
        p = run(c.get_task_progress("t1"))
        assert p["degraded"] is True and p["phase"] == "degraded"

    def test_result_serialized(self):
        c = _wired(_FakeRedis())
        run(c.set_task_progress("t1", kind="generate", status="completed",
                                phase="done", progress=100,
                                result={"plans": [{"id": "a"}]}))
        assert run(c.get_task_progress("t1"))["result"] == {"plans": [{"id": "a"}]}

    def test_unknown_task_returns_none(self):
        assert run(_wired(_FakeRedis()).get_task_progress("nope")) is None

    def test_每个阶段的进度都能算出来(self):
        """
        模型里的每个阶段都必须有文案，且能算出一个百分比 ——
        缺文案前端显示空白，算不出百分比则进度条会跳回 0（比不动更让人困惑）。
        """
        for kind, steps in STEPS.items():
            for step in steps:
                assert PHASE_TEXT.get(step.phase, "").strip(), \
                    f"{kind}/{step.phase} 缺文案"
                assert progress_at(kind, step.phase) is not None, \
                    f"{kind}/{step.phase} 算不出进度"
                assert step.seconds > 0, f"{kind}/{step.phase} 的期望耗时必须为正"

    def test_未知阶段不给进度而不是给0(self):
        """
        ⚠️ `0` 和"不知道"必须分得开。

        未知阶段若回 0，进度条会**跳回起点**；回 None 则调用方保留上一次的值。
        曾经那张全局表用 `.get(phase, 0)` 兜底，就是这个行为。
        """
        assert progress_at("generate", "不存在的阶段") is None
