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
三处与需求文档 11.3.2 的有意偏离（都在代码里标注，不改文档）
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

#: 角色。与需求文档 11.2.7 的三级模型一致。
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

        依据需求文档 11.2.7：**admin 与 designer 都不限量**，
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
            # 封禁 / 启用。**必须给前端** —— 管理页要显示"已停用"，
            # 否则被封的账号在表里和正常账号长得一样，管理员看不出自己做了什么。
            "is_active": self.is_active,
        }


@lru_cache(maxsize=1)
def _raw_users() -> list[dict[str, Any]]:
    path: Path = PROJECT_ROOT / "seed_data" / "users.json"
    return json.loads(path.read_text(encoding="utf-8"))["users"]


#: 运行期改动的落盘位置。
#:
#: ⚠️ **刻意不写回 `seed_data/users.json`。** 那是**入库的种子文件**，
#: 演示时点一下"开通会员"就会在工作区留下未提交改动，下一次
#: `git status` 看到一片脏文件 —— 而它其实不是代码变更。
#: `data/` 在 `.gitignore` 里，演示怎么折腾都不影响仓库。
OVERRIDE_PATH: Path = PROJECT_ROOT / "data" / "users_override.json"


@lru_cache(maxsize=1)
def _overrides() -> dict[int, dict[str, str]]:
    """
    读取运行期覆盖。**只覆盖 `role` / `membership` / `is_active`**，
    不覆盖口令与身份信息。

    文件损坏、不存在、格式不对一律**当作"没有覆盖"**并告警 ——
    这些东西不该让登录整个挂掉。宁可回到种子数据，也不要一个打不开的门。
    """
    try:
        raw = json.loads(OVERRIDE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[auth] 读取 {OVERRIDE_PATH.name} 失败，按无覆盖处理：{type(e).__name__}: {e}")
        return {}

    out: dict[int, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return {}
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        try:
            uid = int(key)
        except (TypeError, ValueError):
            continue
        out[uid] = {str(k): str(v) for k, v in val.items()}
    return out


def _as_bool(text: str, default: bool) -> bool:
    """覆盖文件里存的是字符串。认不出来的值**回落到默认**而不是当成 False ——
    把 `"yes"` 之类误判成"停用"会让一个账号莫名其妙登不进来。"""
    low = (text or "").strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    return default


def reset_user_cache() -> None:
    """丢弃用户缓存。**改完用户必须调它**，否则旧角色会一直被认到进程结束。"""
    _overrides.cache_clear()
    _raw_users.cache_clear()


def _to_user(raw: dict[str, Any]) -> User:
    """种子条目 + 运行期覆盖 → `User`。覆盖只认 role / membership / is_active。"""
    uid = int(raw["id"])
    ov = _overrides().get(uid, {})
    seed_active = bool(raw.get("is_active", True))
    return User(
        id=uid,
        username=str(raw["username"]),
        display_name=str(raw.get("display_name") or raw["username"]),
        title=str(raw.get("title") or ""),
        avatar_text=str(raw.get("avatar_text") or raw["username"][:1]),
        role=ov.get("role", str(raw["role"])),
        membership=ov.get("membership", str(raw["membership"])),
        is_active=_as_bool(ov["is_active"], seed_active) if "is_active" in ov else seed_active,
    )


def users() -> tuple[User, ...]:
    """
    全部账号（种子 + 运行期覆盖）。

    ⚠️ **覆盖是在这里合并的，不是在 `_raw_users()` 里** —— 后者是种子文件的
    原样缓存，把覆盖混进去会让"写入 + 清缓存"变成一场竞态：
    清了一半的缓存会读到一个既不是种子、也不是最终值的中间态。
    分开之后每次 `users()` 都是当前真值，`reset_user_cache()` 一把清干净。
    """
    return tuple(_to_user(raw) for raw in _raw_users())


#: 角色 / 档位的中文名。**这句报错会弹在界面上**（`routes.update_user`
#: 把 `ValueError` 的原文转成 4001 的 message），所以不能印 `['admin', …]`
#: 这种枚举原值。
_ROLE_CN: dict[str, str] = {
    ROLE_ADMIN: "管理员",
    ROLE_DESIGNER: "设计师",
    ROLE_USER: "普通用户",
}
_MEMBERSHIP_CN: dict[str, str] = {
    MEMBERSHIP_FREE: "免费版",
    MEMBERSHIP_PAID: "演示会员",
}


def set_user_role(user_id: int, role: str) -> User:
    """改角色。取值非法抛 `ValueError`（API 层转成 4001）。"""
    if role not in ROLES:
        raise ValueError(
            f"角色取值不合法：{role}；可选的是"
            f"{'、'.join(_ROLE_CN[r] for r in ROLES)}。"
        )
    return _write_override(user_id, "role", role)


def set_user_active(user_id: int, active: bool) -> User:
    """启用 / 封禁一个账号。被封禁的账号**登不进来**（`verify_credentials` 会返回 None）。"""
    return _write_override(user_id, "is_active", "true" if active else "false")


def set_user_membership(user_id: int, membership: str) -> User:
    """改会员档位。取值非法抛 `ValueError`。"""
    if membership not in MEMBERSHIPS:
        raise ValueError(
            f"档位取值不合法：{membership}；可选的是"
            f"{'、'.join(_MEMBERSHIP_CN[m] for m in MEMBERSHIPS)}。"
        )
    return _write_override(user_id, "membership", membership)


def _write_override(user_id: int, field: str, value: str) -> User:
    """
    写一条覆盖并落盘。

    ⚠️ **每次都重新读一遍文件再改**，而不是拿 `_overrides()` 的缓存去改 ——
    缓存是"我这个进程看到的"，文件是"大家看到的"。演示时同时开了
    两个终端改同一个账号的话，基于缓存写回会把对方的改动抹掉。
    """
    if find_by_id(user_id) is None:
        raise ValueError(f"用户不存在：id={user_id}")

    # 从磁盘读最新，绕开缓存
    _overrides.cache_clear()
    merged = {str(k): dict(v) for k, v in _overrides().items()}
    merged.setdefault(str(user_id), {})[field] = value

    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再替换：中途崩溃不会留下半个 JSON（那会让所有人登录失败）
    tmp = OVERRIDE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OVERRIDE_PATH)

    reset_user_cache()
    user = find_by_id(user_id)
    assert user is not None  # 上面已校验存在性；这里只为让类型收敛
    logger.info(f"[auth] 用户改动 id={user_id} {field}={value}（已落盘 {OVERRIDE_PATH.name}）")
    return user


def find_by_username(username: str) -> User | None:
    u = (username or "").strip().lower()
    return next((x for x in users() if x.username.lower() == u), None)


def find_by_id(user_id: int) -> User | None:
    return next((x for x in users() if x.id == user_id), None)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((_PASSWORD_NAMESPACE + salt + password).encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════
# 登录设备（个人中心）
# ══════════════════════════════════════════════════════════════════
#
# ⚠️ **用词要准：这是"从登录请求推断出来的设备"，不是密码学意义的设备绑定。**
#    没有指纹、没有证书、没有二次验证 —— 依据只有 User-Agent 与来源 IP。
#    两者都可以被伪造，所以它**只用于"我自己看看有哪些地方登录过"**，
#    不构成任何访问控制。界面上也必须这么写。
#
#    之所以还要做：个人中心如果只有"账号名 + 会员情况"两行，页面会空得
#    像没做完（用户明确说过"清新不代表空洞"）。登录记录是**真实可查**的
#    一个信息，比摆几行编出来的"绑定设备：iPhone 15 Pro"诚实得多。

#: 设备记录的落盘位置。与 `users_override.json` 同理，放 `data/`（已 gitignore）。
LOGIN_RECORD_PATH: Path = PROJECT_ROOT / "data" / "login_records.json"

#: 每个账号最多保留多少台设备。超出的按"最近登录"淘汰 ——
#: 演示机上来来回回登录，不设上限文件会一直长。
MAX_DEVICES_PER_USER = 10

_BROWSERS = (
    ("Edg", "Edge"),
    ("OPR", "Opera"),
    ("Chrome", "Chrome"),
    ("Firefox", "Firefox"),
    ("Safari", "Safari"),
)


def describe_device(user_agent: str) -> str:
    """
    从 User-Agent 里抽一个人看得懂的设备名。**是启发式，不保证准确。**

    刻意只做"浏览器 · 系统"这一层，不做型号识别：UA 里的型号字符串
    （尤其是国产浏览器的）乱七八糟，硬解析只会得到"iPhone 15 Pro"这种
    看起来精确其实猜的东西 —— 那正是本项目最忌讳的「看起来合理的错误」。

    认不出来就如实说「未知设备」，不要编。
    """
    ua = user_agent or ""
    if not ua:
        return "未知设备"

    browser = next((name for key, name in _BROWSERS if key in ua), "未知浏览器")

    if "Windows" in ua:
        os_name = "Windows"
    elif "Android" in ua:
        os_name = "Android"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "macOS"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = "未知系统"

    return f"{browser} · {os_name}"


def _device_key(user_agent: str, ip: str) -> str:
    """同一台设备（UA + IP 相同）算一条，不重复堆记录。"""
    return hashlib.sha256(f"{user_agent}|{ip}".encode("utf-8")).hexdigest()[:12]


def _read_login_records() -> dict[str, Any]:
    try:
        raw = json.loads(LOGIN_RECORD_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[auth] 读取 {LOGIN_RECORD_PATH.name} 失败，按空处理：{type(e).__name__}: {e}")
        return {}
    return raw if isinstance(raw, dict) else {}


def record_login(user_id: int, user_agent: str, ip: str) -> None:
    """
    记一次登录。**失败只告警，绝不让登录本身失败** ——
    这是个锦上添花的功能，不该成为进门的门槛。
    """
    try:
        data = _read_login_records()
        key = str(user_id)
        devices: list[dict[str, Any]] = list(data.get(key) or [])
        dk = _device_key(user_agent, ip)

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        hit = next((d for d in devices if d.get("key") == dk), None)
        if hit:
            hit["last_seen"] = now
            hit["count"] = int(hit.get("count", 1)) + 1
        else:
            devices.append({
                "key": dk,
                "name": describe_device(user_agent),
                "ip": ip or "—",
                "first_seen": now,
                "last_seen": now,
                "count": 1,
            })

        # 最近的排前面，超出上限的丢掉
        devices.sort(key=lambda d: str(d.get("last_seen", "")), reverse=True)
        data[key] = devices[:MAX_DEVICES_PER_USER]

        LOGIN_RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = LOGIN_RECORD_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(LOGIN_RECORD_PATH)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[auth] 记录登录设备失败（不影响登录）：{type(e).__name__}: {e}")


def login_records(user_id: int) -> list[dict[str, Any]]:
    """某个账号的登录设备，最近的在前。"""
    return list(_read_login_records().get(str(user_id)) or [])


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


def is_disabled_account(username: str, password: str) -> bool:
    """
    口令**正确**、但账号被停用 —— 用来把"账号已停用"和"口令不对"区分开。

    ⚠️ 两个条件缺一不可，这是个有意的安全形状：

      · 只有**口令正确**才告诉他"账号被停用了" —— 说给一个口令都输错的人听，
        等于免费告诉他"这个用户名存在"，那就是一个用户名枚举接口
        （`auth_login` 的注释里为同一件事刻意合并了 4001 的两句话）。
      · 但口令对了还必须**说得清楚** —— 否则被封的人会一直以为自己记错了口令，
        反复重试到怀疑人生。演示时尤其明显：刚在管理页点了「停用」，
        回头登录只看到"用户名或口令不正确"，会以为是自己操作错了。

    这个取舍是：**已知口令的人不怕被告知账号状态**（他本来就进得去，
    只是被管理员停了），而不知道口令的人什么也问不出来。
    """
    u = (username or "").strip().lower()
    raw = next((r for r in _raw_users() if str(r["username"]).lower() == u), None)
    if raw is None:
        return False
    expected = str(raw.get("password_hash") or "")
    actual = _hash_password(password or "", str(raw.get("salt") or ""))
    if not hmac.compare_digest(expected, actual):
        return False
    user = find_by_id(int(raw["id"]))
    return bool(user and not user.is_active)


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
    "LOGIN_RECORD_PATH",
    "MAX_DEVICES_PER_USER",
    "MEMBERSHIP_FREE",
    "MEMBERSHIP_PAID",
    "MEMBERSHIPS",
    "OVERRIDE_PATH",
    "ROLES",
    "ROLE_ADMIN",
    "ROLE_DESIGNER",
    "ROLE_USER",
    "TOKEN_TTL_SECONDS",
    "TokenError",
    "User",
    "decode_token",
    "describe_device",
    "find_by_id",
    "find_by_username",
    "is_disabled_account",
    "issue_token",
    "login_records",
    "record_login",
    "reset_secret_cache",
    "reset_user_cache",
    "set_user_active",
    "set_user_membership",
    "set_user_role",
    "users",
    "verify_credentials",
]
