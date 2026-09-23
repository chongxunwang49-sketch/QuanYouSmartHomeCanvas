"""
认证 —— 身份的建立与校验（AC-01）。

══════════════════════════════════════════════════════════════════════
⚠️ 这个模块是一次**决策反转**的产物
══════════════════════════════════════════════════════════════════════
本项目的 M1（认证模块）原本是**主动跳过**的 —— 理由是"单机演示系统不对外
暴露"。2026-09-23 决定反转：面试演示需要真实的多角色登录，且 AC-01
是验收清单第一条。详见 `docs/需求/需求文档.md` 的 V2.4 修订说明。

所以这里的定位是：**够真实到能演示，且把"离生产还差什么"写清楚**，
而不是假装它是一个可以直接上线的认证层。

══════════════════════════════════════════════════════════════════════
三处与需求文档 2.3.2 的有意偏离（都在代码里标注，不改文档）
══════════════════════════════════════════════════════════════════════
 ① **令牌是自己实现的 HS256，不是 python-jose / pyjwt。**
    项目 requirements.txt 明确不装这两个包（见该文件【C】段）。
    为了一个演示登录引入新依赖会牵连容器重建，而 HS256 的结构本身很简单
    （`base64url(header).base64url(payload).base64url(hmac-sha256(...))`），
    实现出来反而比引依赖更好审。**验证与签名的测试见
    `tests/test_auth.py`，包含篡改检测与过期检测。**
    ⚠️ 生产不该自己写 —— 这里能做是因为只用它认自己签的令牌。

 ② **口令哈希是 `sha256(命名空间 + salt + 口令)`，不是 bcrypt。**
    同样因为没有 passlib/bcrypt 依赖。SHA-256 太快、抗不住离线爆破，
    这是**已知且被接受的**弱点 —— 反正演示口令公开写在 README 里。

 ③ **没有 refresh token。** 需求文档要求 access + refresh 双令牌。
    演示场景下一次登录用 12 小时足够，双令牌的复杂度换不来演示价值。
    令牌过期就是重新登录。

══════════════════════════════════════════════════════════════════════
⚠️ 密钥
══════════════════════════════════════════════════════════════════════
`AUTH_SECRET` 没有配时**每个进程随机生成**，并打一条警告。
这样做的理由：在代码里写一个默认密钥，等于把"所有人共用一把钥匙"
固化成默认行为；而随机密钥的代价只是"进程重启后需要重新登录"，
对一个演示系统完全可接受。

要跨重启保持登录，在 `.env` 里设 `AUTH_SECRET`（compose 会从环境读）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

from .config import PROJECT_ROOT, settings

#: 与 seed_data/users.json 里的哈希口径一致。**不是密钥**，只是命名空间。
_PASSWORD_NAMESPACE = "quanyou-smart-homecanvas-demo"

#: 令牌有效期。12 小时 —— 演示当天不用重复登录。
TOKEN_TTL_SECONDS = 12 * 3600

#: 角色。与需求文档 2.2.7 的三级模型一致。
ROLE_ADMIN = "admin"
ROLE_DESIGNER = "designer"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_DESIGNER, ROLE_USER)

#: 会员档位。与 `CoreConfig` 的免费/付费区分对应。
MEMBERSHIP_FREE = "free"
MEMBERSHIP_PAID = "paid"
MEMBERSHIPS = (MEMBERSHIP_FREE, MEMBERSHIP_PAID)

_secret_cache: str | None = None


def _secret() -> str:
    """
    签名密钥。配了就用配的，没配就**每进程随机**。

    ⚠️ 这里刻意**不设默认值常量** —— 一个写死在源码里的默认密钥，
    会让人以为"不配也行"，而它的实际后果是所有部署共用同一把钥匙。
    """
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    configured = (settings.AUTH_SECRET or "").strip()
    if configured:
        _secret_cache = configured
    else:
        _secret_cache = secrets.token_urlsafe(48)
        logger.warning(
            "AUTH_SECRET 未配置，已生成随机密钥 —— 进程重启后所有登录态失效。"
            "演示当天不受影响；要跨重启保持登录，请在 .env 里设 AUTH_SECRET。"
        )
    return _secret_cache


# ══════════════════════════════════════════════════════════════════
# 用户
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class User:
    """一个账号。`password_hash` / `salt` **不在这里** —— 它们不该流到 API 层。"""

    id: int
    username: str
    display_name: str
    title: str
    avatar_text: str
    role: str
    membership: str
    is_active: bool = True

    @property
    def is_unlimited(self) -> bool:
        """
        是否不受额度限制。

        依据需求文档 2.2.7：**admin 与 designer 都不限量**，
        只有 `user` 受每日配额约束。这与
        `core/redis_client.py` 的 `_role_limit()` 是同一个判断 ——
        两处必须一致，`tests/test_auth.py` 有断言钉着。
        """
        return self.role in (ROLE_ADMIN, ROLE_DESIGNER)

    @property
    def can_use_paid_features(self) -> bool:
        """付费功能是否可用。管理员/设计师不受会员档位限制。"""
        return self.is_unlimited or self.membership == MEMBERSHIP_PAID

    @property
    def role_label(self) -> str:
        return {"admin": "管理员", "designer": "设计师", "user": "普通用户"}.get(self.role, self.role)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "title": self.title,
            "avatar_text": self.avatar_text,
            "role": self.role,
            "role_label": self.role_label,
            "membership": self.membership,
            "is_unlimited": self.is_unlimited,
            "can_use_paid_features": self.can_use_paid_features,
        }


@lru_cache(maxsize=1)
def _raw_users() -> list[dict[str, Any]]:
    path: Path = PROJECT_ROOT / "seed_data" / "users.json"
    return json.loads(path.read_text(encoding="utf-8"))["users"]


def users() -> tuple[User, ...]:
    out: list[User] = []
    for raw in _raw_users():
        out.append(
            User(
                id=int(raw["id"]),
                username=str(raw["username"]),
                display_name=str(raw.get("display_name") or raw["username"]),
                title=str(raw.get("title") or ""),
                avatar_text=str(raw.get("avatar_text") or raw["username"][:1]),
                role=str(raw["role"]),
                membership=str(raw["membership"]),
                is_active=bool(raw.get("is_active", True)),
            )
        )
    return tuple(out)


def find_by_username(username: str) -> User | None:
    u = (username or "").strip().lower()
    return next((x for x in users() if x.username.lower() == u), None)


def find_by_id(user_id: int) -> User | None:
    return next((x for x in users() if x.id == user_id), None)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((_PASSWORD_NAMESPACE + salt + password).encode("utf-8")).hexdigest()


def verify_credentials(username: str, password: str) -> User | None:
    """
    校验口令。成功返回用户，失败返回 None。

    ⚠️ 用 `hmac.compare_digest` 而不是 `==`：前者是常数时间比较，
    不会因为"前几位对上了"而提前返回，从而不泄漏哈希的逐位信息。
    演示系统里这不算威胁，但**写成正确的形状**没有额外成本。
    """
    raw = next(
        (r for r in _raw_users() if str(r["username"]).lower() == (username or "").strip().lower()),
        None,
    )
    if raw is None:
        # ⚠️ 用户不存在时也走一遍哈希，避免"响应快=用户不存在"的时序旁路。
        hmac.compare_digest(_hash_password(password, "x" * 16), "0" * 64)
        return None

    expected = str(raw.get("password_hash") or "")
    actual = _hash_password(password or "", str(raw.get("salt") or ""))
    if not hmac.compare_digest(expected, actual):
        return None

    user = find_by_id(int(raw["id"]))
    return user if (user and user.is_active) else None


# ══════════════════════════════════════════════════════════════════
# 令牌（最小 HS256，标准库实现）
# ══════════════════════════════════════════════════════════════════


class TokenError(Exception):
    """令牌无效。`reason` 是给日志用的短原因，**不回给客户端**。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(signing_input: str) -> str:
    mac = hmac.new(_secret().encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
    return _b64e(mac.digest())


def issue_token(user: User, *, ttl_seconds: int = TOKEN_TTL_SECONDS) -> tuple[str, int]:
    """签发令牌。返回 `(token, expires_in_seconds)`。"""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user.id),
        "usr": user.username,
        "role": user.role,
        "mem": user.membership,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    # separators 去掉空格 —— 同一份 claims 每次都得到同一个字符串，
    # 便于测试里逐字比对，也让令牌短一点。
    head = _b64e(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signing_input = f"{head}.{body}"
    return f"{signing_input}.{_sign(signing_input)}", ttl_seconds


def decode_token(token: str) -> User:
    """
    校验令牌并取回用户。任何问题都抛 `TokenError`。

    检查顺序是有意的：**先验签、再看内容**。先解 payload 再验签的话，
    等于让未经验证的数据进入了代码路径 —— 那是这类实现最常见的错。
    """
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise TokenError("令牌格式不是三段")
    head, body, sig = parts

    if not hmac.compare_digest(_sign(f"{head}.{body}"), sig):
        raise TokenError("签名不匹配")

    try:
        payload = json.loads(_b64d(body))
    except Exception as exc:  # noqa: BLE001 —— 任何解码问题都归为"令牌无效"
        raise TokenError(f"payload 无法解析：{type(exc).__name__}") from exc

    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise TokenError("缺少 exp")
    if int(time.time()) >= exp:
        raise TokenError("已过期")

    user = find_by_id(int(payload.get("sub") or 0))
    if user is None or not user.is_active:
        raise TokenError("用户不存在或已停用")
    return user


def reset_secret_cache() -> None:
    """仅供测试：清掉密钥缓存，让下一次调用重新取。"""
    global _secret_cache
    _secret_cache = None


__all__ = [
    "MEMBERSHIP_FREE",
    "MEMBERSHIP_PAID",
    "MEMBERSHIPS",
    "ROLES",
    "ROLE_ADMIN",
    "ROLE_DESIGNER",
    "ROLE_USER",
    "TOKEN_TTL_SECONDS",
    "TokenError",
    "User",
    "decode_token",
    "find_by_id",
    "find_by_username",
    "issue_token",
    "reset_secret_cache",
    "users",
    "verify_credentials",
]
