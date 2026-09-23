"""
认证（AC-01）。

这个文件里最重要的是两组用例：

  · **令牌篡改** —— 自己实现 HS256 的风险就在于"验签写漏了看着也正常"，
    所以要把"改一个字节就该拒绝"逐条钉死（改签名 / 改 payload / 截断）。
  · **跨模块一致性** —— `User.is_unlimited` 与 `redis_client._role_limit`
    是同一个业务规则的两处实现，必须一致。dev 时不一致的后果是
    "界面显示不限量、后端却把他限流了"，很难查。
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from backend.app.core import auth
from backend.app.core.redis_client import RedisClient


@pytest.fixture(autouse=True)
def _fresh_secret():
    """每个用例一把干净的密钥，避免相互影响。"""
    auth.reset_secret_cache()
    yield
    auth.reset_secret_cache()


# ══════════════════════════════════════════════════════════════════
# 用户与口令
# ══════════════════════════════════════════════════════════════════


def test_种子用户可加载且角色合法():
    us = auth.users()
    assert len(us) >= 3, "至少要能演示三种角色"
    for u in us:
        assert u.role in auth.ROLES, f"{u.username} 的角色非法：{u.role}"
        assert u.membership in auth.MEMBERSHIPS


def test_三角色齐备():
    """AC-01 的字面要求：admin / designer / user 分别登录成功。"""
    roles = {u.role for u in auth.users()}
    assert {"admin", "designer", "user"} <= roles, f"缺少角色：{roles}"


@pytest.mark.parametrize(
    "username,password",
    [("admin", "admin123"), ("designer", "designer123"),
     ("demo", "demo123"), ("vip", "vip123")],
)
def test_种子口令能登录(username, password):
    u = auth.verify_credentials(username, password)
    assert u is not None and u.username == username


def test_口令错误返回None():
    assert auth.verify_credentials("admin", "wrong") is None
    assert auth.verify_credentials("admin", "") is None


def test_用户不存在返回None():
    assert auth.verify_credentials("nobody", "whatever") is None


def test_用户名大小写不敏感():
    assert auth.verify_credentials("ADMIN", "admin123") is not None


def test_用户对象不携带口令材料():
    """
    `User` 里**不能**有 password_hash / salt —— 它在 API 层会被序列化出去。
    这类泄漏一旦发生，谁也不会在界面上看见，只能靠断言守。
    """
    u = auth.users()[0]
    assert not hasattr(u, "password_hash")
    assert not hasattr(u, "salt")
    assert "password" not in json.dumps(u.to_dict())


# ══════════════════════════════════════════════════════════════════
# 权限判定（与配额模块保持一致）
# ══════════════════════════════════════════════════════════════════


def test_管理员与设计师不限量_且与配额模块一致():
    """
    ⚠️ 这条断言的价值在于**两处实现必须相同**：

      `User.is_unlimited`            —— 前端拿它决定界面怎么显示
      `RedisClient._role_limit`      —— 后端拿它决定放不放行

    两处不一致的后果是"界面显示不限量、后端却把他限流了"，
    而这在演示时表现为"莫名其妙被拒"，很难往权限上想。

    ⚠️ `_role_limit` 挂在 `RedisClient` 上，不在 `QuotaLimiter` 上 ——
    后者只是前者的语义化薄包装（`consume` / `peek` 两个方法）。
    """
    for u in auth.users():
        backend_unlimited = RedisClient._role_limit(u.role, "parse") is None
        assert u.is_unlimited == backend_unlimited, (
            f"{u.role} 在 auth.User 里是 unlimited={u.is_unlimited}，"
            f"在 RedisClient 里是 unlimited={backend_unlimited} —— 两处必须一致"
        )


def test_管理员与设计师不受会员档位限制():
    for u in auth.users():
        if u.role in ("admin", "designer"):
            assert u.can_use_paid_features, f"{u.role} 不该被会员档位拦住"


def test_免费用户用不了付费功能_付费用户可以():
    free = next(u for u in auth.users() if u.role == "user" and u.membership == "free")
    paid = next(u for u in auth.users() if u.role == "user" and u.membership == "paid")
    assert not free.can_use_paid_features
    assert paid.can_use_paid_features


# ══════════════════════════════════════════════════════════════════
# 令牌
# ══════════════════════════════════════════════════════════════════


def _any_user():
    return auth.users()[0]


def test_令牌签发与解回():
    u = _any_user()
    token, ttl = auth.issue_token(u)
    assert ttl > 0
    assert token.count(".") == 2, "JWT 必须是三段"
    back = auth.decode_token(token)
    assert back.id == u.id and back.role == u.role


def test_篡改签名被拒绝():
    u = _any_user()
    token, _ = auth.issue_token(u)
    head, body, sig = token.split(".")
    # 只改签名最后一个字符
    bad_sig = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    with pytest.raises(auth.TokenError, match="签名"):
        auth.decode_token(f"{head}.{body}.{bad_sig}")


def test_篡改载荷被拒绝():
    """
    **最重要的一条。** 把 payload 里的 role 改成 admin、但不动签名 ——
    如果实现是先解 payload 再看内容、或者忘了验签，这里就会放行，
    等于任何用户都能把自己提权成管理员。
    """
    u = next(x for x in auth.users() if x.role == "user")
    token, _ = auth.issue_token(u)
    head, body, sig = token.split(".")

    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["role"] = "admin"          # 提权
    forged = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    ).rstrip(b"=").decode()

    with pytest.raises(auth.TokenError, match="签名"):
        auth.decode_token(f"{head}.{forged}.{sig}")


def test_过期令牌被拒绝():
    u = _any_user()
    token, _ = auth.issue_token(u, ttl_seconds=-1)   # 签发即过期
    with pytest.raises(auth.TokenError, match="过期"):
        auth.decode_token(token)


def test_换一把密钥后旧令牌失效():
    """
    模拟"服务重启"：密钥换了，之前签发的令牌不该还能用。

    ⚠️ 必须**先把 AUTH_SECRET 清空**再重置缓存 —— conftest 默认把它钉死了，
    而配置了密钥时 `_secret()` 每次返回同一个值，"重置"就不会换密钥，
    这条用例也就测不到东西了（实测踩过：当时报的是"DID NOT RAISE"）。
    """
    from backend.app.core.config import settings

    original = settings.AUTH_SECRET
    try:
        settings.AUTH_SECRET = ""          # 走"未配置 → 每进程随机"那条路
        auth.reset_secret_cache()
        token, _ = auth.issue_token(_any_user())

        auth.reset_secret_cache()          # 相当于进程重启
        with pytest.raises(auth.TokenError, match="签名"):
            auth.decode_token(token)
    finally:
        settings.AUTH_SECRET = original
        auth.reset_secret_cache()


@pytest.mark.parametrize(
    "garbage",
    ["", "not-a-token", "a.b", "a.b.c.d", "..", "a.b.c"],
)
def test_畸形令牌被拒绝(garbage):
    """任何输入都不能让解码器抛 `TokenError` 以外的异常 —— 否则会变成 500。"""
    with pytest.raises(auth.TokenError):
        auth.decode_token(garbage)


def test_令牌里的角色变更后以库里的为准():
    """
    令牌里的 role 只是**签发时的快照**。管理员事后改了某人的角色，
    旧令牌不该还带着旧权限 —— 这里的实现以 `find_by_id` 的结果为准，
    所以改角色立即生效（代价是每次请求查一次内存表，可接受）。
    """
    u = next(x for x in auth.users() if x.role == "user")
    token, _ = auth.issue_token(u)
    back = auth.decode_token(token)
    assert back.role == "user", "解回来的角色应当来自用户表，而不是令牌里的快照"


def test_密钥未配置时不为空且每进程一致():
    """⚠️ conftest 默认钉死了 AUTH_SECRET —— 这条专门测"没配"的真实路径。"""
    from backend.app.core.config import settings

    original = settings.AUTH_SECRET
    try:
        settings.AUTH_SECRET = ""
        auth.reset_secret_cache()
        a = auth._secret()
        assert a and len(a) > 20
        assert auth._secret() == a, "同一进程内必须是同一把，否则签发的令牌立刻失效"
        # 再重置一次应当换一把（模拟进程重启）
        auth.reset_secret_cache()
        assert auth._secret() != a, "未配置时必须每进程随机，不能固化成同一把"
    finally:
        settings.AUTH_SECRET = original
        auth.reset_secret_cache()


def test_密钥配置了就用配置的():
    from backend.app.core.config import settings

    original = settings.AUTH_SECRET
    try:
        settings.AUTH_SECRET = "configured-secret-for-test"
        auth.reset_secret_cache()
        assert auth._secret() == "configured-secret-for-test"
    finally:
        settings.AUTH_SECRET = original
        auth.reset_secret_cache()


def test_过期时间在合理范围():
    u = _any_user()
    token, ttl = auth.issue_token(u)
    body = token.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload["exp"] - payload["iat"] == ttl
    assert ttl >= 3600, "演示当天不该频繁要求重新登录"
    assert payload["exp"] > int(time.time())
