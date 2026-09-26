"""
接口层的依赖注入 —— 当前用户、会员门控、额度消耗。

══════════════════════════════════════════════════════════════════════
⚠️ 这是本项目**第一次**用 FastAPI 的 `Depends`
══════════════════════════════════════════════════════════════════════
在 AC-01 之前所有路由都是无状态的纯函数，不需要依赖注入。引入它之后有两条
约定必须写死，否则后来的人会各自发挥：

 ① **未登录不是 HTTP 401，是 200 + code 4003。**
    与本项目其它业务失败同一条规矩（见 `api/schemas.py` 模块说明）：
    前端 axios 拦截器按 HTTP 状态码分流，4xx 会被归一成"服务错误"弹通用
    报错；而"请重新登录"是**可操作的提示**，必须走业务码这条路。
    前端拿到 4003 的唯一动作是跳 `/login`。

 ② **门控做三层，缺一层都不算数。**
       · 界面层：不可用的入口置灰并说明原因（前端）
       · 接口层：本模块（真正拦得住 curl）
       · 额度层：`core/redis_client.py` 的计数（按天，AC-13）
    只做界面层等于没做 —— 打开 DevTools 就能绕过，
    这正是本项目「不产出看起来合理但其实没做的事」那条原则的反面。

══════════════════════════════════════════════════════════════════════
角色 × 会员：两个正交维度
══════════════════════════════════════════════════════════════════════
    role        admin / designer / user     —— 决定**能看什么**
    membership  free / paid                 —— 决定**用多少**

  · `admin` / `designer`：`is_unlimited=True`，**不受会员档位限制**
  · `user` + `free`：解析 5 次/日、生成 3 次/日；付费功能一律 4005
  · `user` + `paid`：不限功能，但仍受每日配额保护（防跑飞）

额度值来自 `core/config.py` 的 `QUOTA_USER_*_PER_DAY`，
判定与 `core/auth.py` 的 `User.is_unlimited` 必须一致
（`tests/test_auth.py` 有跨模块断言钉着）。
"""

from __future__ import annotations

from fastapi import Header
from loguru import logger

from ..core import auth
from ..core.auth import TokenError, User
from ..core.redis_client import TaskType, get_quota_limiter
from .schemas import ApiError

#: 前端"需要开通会员"的提示要指向哪一页。放在响应里，避免前端再写一份。
#:
#: ⚠️ 2026-09-24 改：原来是「用户管理 → 我的套餐」，而**用户管理页现在只有
#:    管理员能看见**（普通用户的对应页面是「个人中心」）。不改的话，
#:    这句提示会把免费用户指向一扇他进不去的门 —— 那句话本身是对的，
#:    只是门换了地方。
UPGRADE_HINT = "在「个人中心 → 我的套餐」里开通演示会员即可解锁（演示环境，不会真实扣费）。"


def _bearer(authorization: str | None) -> str:
    """从 `Authorization: Bearer xxx` 里取令牌。取不到返回空串。"""
    raw = (authorization or "").strip()
    if not raw:
        return ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    # 容忍直接塞令牌（不带 Bearer 前缀）—— 用 curl 手测时少写一段
    return raw


async def current_user(authorization: str | None = Header(default=None)) -> User:
    """
    **要求已登录。** 未登录 / 令牌无效 / 已过期一律 `4003`。

    ⚠️ 无效的原因（签名不匹配 / 已过期 / 用户被停用）**不回给客户端**，
    只放进 `data.reason` 供排查 —— 告诉攻击者"签名对了但过期了"
    本身就是信息泄漏，虽然在这个演示系统里无所谓，但形状要对。
    """
    token = _bearer(authorization)
    if not token:
        raise ApiError(4003, "未登录，请先登录", data={"action": "login"})
    try:
        return auth.decode_token(token)
    except TokenError as exc:
        raise ApiError(
            4003,
            "登录已失效，请重新登录",
            data={"action": "login", "reason": exc.reason},
        ) from exc


async def optional_user(authorization: str | None = Header(default=None)) -> User | None:
    """**不要求登录。** 有令牌且有效就返回用户，否则 None。用于读接口。"""
    token = _bearer(authorization)
    if not token:
        return None
    try:
        return auth.decode_token(token)
    except TokenError:
        return None


def require_paid(user: User, feature: str) -> None:
    """
    付费功能门控。不满足抛 `4005`。

    ⚠️ 拒绝时必须说清**缺什么、怎么办**（`core/capabilities.py` 的立场，
    AC-33 已把它定成全项目的规矩）：所以 `data` 里带上 feature 名、
    当前档位、以及去哪开通。
    """
    if user.can_use_paid_features:
        return
    raise ApiError(
        4005,
        f"「{feature}」需要开通会员后使用。当前档位：免费版。",
        data={
            "feature": feature,
            "membership": user.membership,
            "required_membership": auth.MEMBERSHIP_PAID,
            "upgrade_hint": UPGRADE_HINT,
        },
    )


async def consume_quota(user: User, task_type: TaskType) -> None:
    """
    消耗一次额度。超额抛 `4006`。

    ⚠️ **管理员/设计师也会走这里**，只是 `_role_limit` 对他们返回 `None`
    （不限量）—— 这样计数仍然累计，界面上"今日已用"对谁都真实，
    而不是管理员那里永远显示 0。

    ⚠️ Redis 不可用时 `check_and_consume_quota` **放行并标 degraded**
    （见 `redis_client.py` 的降级策略）。这里沿用那个决定：
    演示时 Redis 挂了不该把功能一起锁死，但会在响应里留下痕迹。
    """
    role_limit = await get_quota_limiter().consume(
        user_id=user.id, role=user.role, task_type=task_type
    )
    if role_limit.allowed:
        # ⚠️ **降级放行必须留下痕迹。** 这一支原来直接 return，
        #    于是"Redis 挂了、额度没算"和"额度够用"在日志与响应里
        #    完全一样。放行可以（见上面的降级策略），但不能看不出来。
        if role_limit.degraded:
            logger.warning(
                f"[quota] Redis 不可用，本次未计数即放行："
                f"user={user.username} task={task_type}"
            )
        return
    raise ApiError(
        4006,
        f"今日「{task_type}」额度已用完（{role_limit.limit} 次/日）。"
        f"{role_limit.reset_at} 后重置。",
        data={
            "task_type": task_type,
            "limit": role_limit.limit,
            "used": role_limit.used,
            "role": user.role,
            "reset_at": role_limit.reset_at,
            "upgrade_hint": UPGRADE_HINT,
        },
    )


__all__ = [
    "UPGRADE_HINT",
    "consume_quota",
    "current_user",
    "optional_user",
    "require_paid",
]
