"""
HTTP 路由。

对应需求文档第十二章的接口契约。当前实现 **25 条**：

    4.1 认证与账号（AC-01）
      POST /auth/login                   登录（唯一不需要令牌的写接口）
      GET  /auth/me                      当前用户
      GET  /auth/quota                   我今日的额度（只读，不消耗）
      POST /auth/membership              自助改档位（演示开关）
      GET  /me/devices                   我的登录设备
      GET  /users                        全部账号（**仅管理员**）
      POST /users/{user_id}              改角色/档位/启停（**仅管理员**）

    4.2 / 4.3 解析与生成（AC-02…AC-06）
      POST /layout/parse                 户型解析（异步，扣配额）
      POST /design/generate              方案生成（异步，需会员 + 扣配额）

    4.6 避坑审查
      POST /avoid-pit/review             报价单/合同审查（异步，需会员，**不扣配额**）

    4.4 任务（AC-31 / AC-36）
      GET  /task/{task_id}/status        状态轮询
      POST /task/{task_id}/cancel        中断在飞任务（取消权归本人）

    4.5 材料
      GET  /material/price               材料价格查询（同步，纯查表）
      GET  /material/options             品类/品牌可选项（AC-19）

    4.3′ / 4.3″ 矢量图、热区与 3D（AC-07 / AC-09 / AC-21）
      GET  /layout/{id}/plan.svg         矢量户型图
      GET  /layout/{id}/hotspots         物品热区 + 价格与链接
      GET  /layout/{id}/walkable         3D 漫游几何（第一人称 / 自由视角）
      GET  /layout/{id}/furniture        3D 家具摆放（纯规则算）

    4.3‴ 地面材质替换（AC-10）
      GET  /layout/{id}/floor-materials  当前替换 + 可选清单
      POST /layout/{id}/floor-material   把某间房地面换成一种材料（空串 = 还原）

    4.6′ 知识库与统计（2026-09-26 补齐）
      GET  /knowledge/list               知识库现状（**仅管理员**）
      POST /knowledge/upload             入库一段文本（**仅管理员**）
      GET  /dashboard/stats              跨会话累计统计

    4.6 系统
      GET  /system/health                健康检查（**免登录**，永不 503）
      GET  /system/metrics               性能指标（AC-23，**需登录**）

⚠️ **这份清单要与代码同步 —— 而它已经漏过两次了。**

   第一次（2026-09-26 之前）：只列了 13 条，实际注册 20 条。漏掉的 7 条
   （`/auth/quota`、`/auth/membership`、`/me/devices`、`GET /users`、
   `POST /users/{user_id}`、`/material/options`、`/layout/{id}/furniture`）
   在文档里等于不存在。

   第二次（同日，就在修完第一次之后）：加了地面材质替换两条路由，**清单没跟着改** ——
   写 23、实际 25。而下面这句话当时就写在这里：

       清单靠"记得改"维持的话，它迟早会变成一份**看起来完整、实际过期的**说明。

   **它果然又变成了一次。** 所以这条警告留在这里，连同它应验的记录 ——
   一份会自我应验的警告比一句口号有用。

   判据（可随时复核）：

       grep -cE '^@router\.' backend/app/api/routes.py     # → 25

**四个异步接口的形状是一致的**：立即返回 task_id，客户端轮询 4.4。
差别只在 `kind` 与起始 `stages`。

**门控分布**（AC-01 + AC-13，判定逻辑见 `api/deps.py`）：

    接口              免费用户            会员            管理员/设计师
    layout/parse      可用（5 次/日）      可用（5 次/日）  不限
    design/generate   **需开通**（4005）   可用（3 次/日）  不限
    avoid-pit/review  **需开通**（4005）   可用            不限
    knowledge/*       —                  —               仅管理员
    dashboard/stats   可用               可用            可用

⚠️ 会员**照样受配额约束** —— 配额防的是跑飞，与是否付费无关。
⚠️ `avoid-pit/review` **不扣配额**是有意的：配额分 parse / generate 两个桶
   （见 `TaskType`），审查是反复用的动作，扣在生成桶上会让"多想几次"
   变成有代价的行为。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from datetime import datetime, timezone
from uuid import uuid4

from collections.abc import AsyncIterator
from typing import Any, get_args

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from loguru import logger

from ..core import auth
from ..core.auth import User
from ..core.logger import audit
from ..core.capabilities import OperationNotAllowedError
from ..core import metrics
from ..core.llm_client import LLMError
from ..db import repository
from ..core.config import settings
from ..core.progress import total_seconds
from ..core.redis_client import get_quota_limiter
from ..graph.state import initial_state
from ..graph.workflow import get_compiled_graph
from ..schemas.plan import BudgetGrade, PlanStyle
from ..agents.layout_diagnoser import HOUSE_DETAIL_MAX_CHARS
from ..services.chat import build_context, build_history, stream_answer
from ..services.furniture import scaling
from ..services.geometry import build_walkable
from ..services.material import catalog
from ..services.render import hotspot_payload, render_plan_for
from . import store as layout_store
from .deps import consume_quota, current_user, require_paid
from .schemas import (
    ApiError,
    ApiResponse,
    ChatAskRequest,
    ChatConversationPatch,
    ChatConversationRequest,
    FloorMaterialRequest,
    GenerateRequest,
    HouseDetailRequest,
    KnowledgeUploadRequest,
    LoginRequest,
    MembershipRequest,
    ParseRequest,
    ReviewRequest,
    UserUpdateRequest,
)
from .tasks import get_task_manager

router = APIRouter(prefix="/api/v1")


def _trace_id(request: Request) -> str:
    """取中间件写进 request.state 的 trace_id。"""
    return getattr(request.state, "trace_id", "") or ""


# ══════════════════════════════════════════════════════════════════
# 4.1 认证（AC-01）
# ══════════════════════════════════════════════════════════════════
#
# ⚠️ 这三个接口的约定与其它接口**不同**，写清楚免得后来的人改错：
#
#   · `/auth/login` 是唯一**不需要**令牌的写接口（否则死锁）。
#   · 认证失败是 **HTTP 200 + code 4003**，不是 401 ——
#     理由见 `api/deps.py` 模块说明。前端拿到 4003 就跳 /login。
#   · 口令错误**不区分**"用户不存在"与"口令错误"，两种情况返回同一句话。
#     区分开等于给了一个枚举用户名的接口。


@router.post("/auth/login", response_model=ApiResponse)
async def auth_login(req: LoginRequest, request: Request) -> ApiResponse:
    """
    登录，返回访问令牌与用户信息。

    ⚠️ **没有 refresh token。** 需求文档 11.3.2 要求双令牌，这里有意偏离：
    演示场景下 12 小时的有效期足够，双令牌的复杂度换不来演示价值。
    偏离理由写在 `core/auth.py` 的文件头。
    """
    user = auth.verify_credentials(req.username, req.password)
    if user is None:
        # ⚠️ 顺序有讲究：**先看是不是"口令对但账号被停用"**，
        #    再回落到那句合并的 4001。
        #
        #    合并 4001 的理由是"不区分用户是否存在"（区分开就是用户名枚举接口）。
        #    而"账号被停用"这条只在**口令正确**时才说（见 auth.is_disabled_account），
        #    所以它不泄漏"用户名是否存在" —— 只对已经拿到口令的人多说一句，
        #    而那个人本来就能进，只是被管理员停了。
        #
        #    不说这一句的代价很具体：演示时刚在管理页点了「停用」，
        #    回头登录只看到"用户名或口令不正确"，会以为自己操作错了、
        #    或者口令记错了 —— 排查方向整个偏掉。
        if auth.is_disabled_account(req.username, req.password):
            logger.info(f"[auth] 账号已停用 username={req.username!r} trace={_trace_id(request)}")
            # 审计：账号被停用后仍尝试登录 —— 这类事件事后最需要查
            audit("login_failed", username=req.username, reason="account_disabled")
            raise ApiError(
                4007,
                "该账号已被管理员停用，无法登录。请联系管理员在「账号管理」里启用。",
                data={"action": "contact_admin"},
            )
        logger.info(f"[auth] 登录失败 username={req.username!r} trace={_trace_id(request)}")
        # ⚠️ 只记用户名与原因，**绝不记口令** —— 审计日志会落盘 180 天，
        #    而且（按设计）不含变量快照正是为了不把口令写进去
        #    （见 logger.setup_logging 里 diagnose=False 的说明）。
        audit("login_failed", username=req.username, reason="bad_credentials")
        # ⚠️ 不区分"用户不存在"与"口令错误" —— 区分开就是一个用户名枚举接口
        raise ApiError(4001, "用户名或口令不正确")

    token, expires_in = auth.issue_token(user)

    # 记一次登录设备（个人中心的"登录设备"就是它）。**失败不影响登录** ——
    # 见 auth.record_login 的说明：它是锦上添花，不该成为进门的门槛。
    auth.record_login(
        user.id,
        request.headers.get("user-agent", ""),
        request.client.host if request.client else "",
    )

    logger.info(f"[auth] 登录成功 user={user.username} role={user.role} trace={_trace_id(request)}")
    audit("login", username=user.username, user_id=user.id,
          role=user.role, membership=user.membership)
    return ApiResponse.ok({
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "user": user.to_dict(),
    })


@router.get("/me/devices", response_model=ApiResponse)
async def my_devices(user: User = Depends(current_user)) -> ApiResponse:
    """
    我的登录设备（个人中心）。

    ⚠️ **这是"从登录请求推断出的设备"，不是密码学意义的设备绑定。**
    依据只有 User-Agent 与来源 IP，两者都可伪造 —— 所以它只回答
    "我自己看看有哪些地方登录过"，**不构成任何访问控制**。
    接口与界面都必须这么写，不要把它说成"设备锁"。
    """
    return ApiResponse.ok({
        "devices": auth.login_records(user.id),
        "note": "依据登录请求的 User-Agent 与来源 IP 推断，可被伪造；仅用于自查，不构成访问控制。",
    })


@router.get("/auth/me", response_model=ApiResponse)
async def auth_me(user: User = Depends(current_user)) -> ApiResponse:
    """
    当前登录用户。

    前端刷新页面后靠它恢复登录态 —— 令牌存在 localStorage，
    但**用户信息不做本地持久化**，每次重新问一次：
    管理员刚改了某人的角色/档位，刷新就该看到新的，
    而不是从 localStorage 里读出一个过期快照。
    """
    return ApiResponse.ok({"user": user.to_dict()})


@router.get("/auth/quota", response_model=ApiResponse)
async def auth_quota(user: User = Depends(current_user)) -> ApiResponse:
    """
    我今日的额度（AC-13）。**只读，不消耗。**

    为什么要有这个接口：界面上"今日已用 2/5"这种显示，如果不给查询口，
    前端就只能自己记 —— 而它是**按用户在后端按天算的**，前端记的那个数
    在换设备、刷新、并发提交后全是错的。宁可多一个只读接口，
    也不要摆一个**猜出来的**数字（本项目最忌讳"看起来合理的错误"）。

    ⚠️ 管理员与设计师的 `limit` 是 `null`（不限量），但 `used` **照样是真实计数** ——
    见 `deps.consume_quota` 的说明：次数对谁都累计，只是对管理员不设上限。
    """
    limiter = get_quota_limiter()
    quota: dict[str, Any] = {}
    for task_type in ("parse", "generate", "review"):
        result = await limiter.peek(user_id=user.id, role=user.role, task_type=task_type)
        quota[task_type] = result.to_dict()
    return ApiResponse.ok({
        "quota": quota,
        "user_id": user.id,
        "role": user.role,
        "unlimited": user.is_unlimited,
    })


@router.post("/auth/membership", response_model=ApiResponse)
async def set_my_membership(
    req: MembershipRequest,
    user: User = Depends(current_user),
) -> ApiResponse:
    """
    自助改自己的会员档位（演示用"开通会员"）。

    ⚠️ **这是演示开关，不是支付流程。** 没有任何扣费、没有订单、
    没有有效期 —— 点了就是点了，并且**落盘到 `data/users_override.json`**
    （`.gitignore` 里），所以重启后仍然有效，且不会污染入库的种子文件。

    ⚠️ 只对 `role == user` 开放。管理员与设计师**不受档位限制**
    （`User.is_unlimited`），给他们改档位是没有意义的一件操作 ——
    与其让界面上多一个点了没用的按钮，不如明确拒绝并说清原因。
    """
    if user.role != auth.ROLE_USER:
        raise ApiError(
            4002,
            f"当前角色是{user.role_label}，不受会员档位限制，"
            f"因此没有可开通或取消的档位。",
            data={"role": user.role, "unlimited": True},
        )
    if req.membership not in auth.MEMBERSHIPS:
        raise ApiError(
            4001,
            f"档位取值不合法：{req.membership}；允许 {list(auth.MEMBERSHIPS)}",
            data={"allowed": list(auth.MEMBERSHIPS)},
        )
    updated = auth.set_user_membership(user.id, req.membership)
    logger.info(f"[auth] {user.username} 自助改档位 → {req.membership}")
    return ApiResponse.ok({"user": updated.to_dict()})


@router.get("/users", response_model=ApiResponse)
async def list_users(user: User = Depends(current_user)) -> ApiResponse:
    """
    全部账号。**仅管理员。**

    ⚠️ 这是本项目**第一个按角色而非"登录与否"判定的接口**。
    在设计师那里返回 4002 而不是空列表：空列表会被界面渲染成
    "一个账号都没有"，那是**看起来合理但错误**的一种表现（本项目三原则之一）。
    """
    if user.role != auth.ROLE_ADMIN:
        raise ApiError(
            4002,
            f"查看全部账号需要管理员权限。当前角色：{user.role_label}。",
            data={"role": user.role, "required_role": auth.ROLE_ADMIN},
        )
    return ApiResponse.ok({
        "users": [u.to_dict() for u in auth.users()],
        "roles": list(auth.ROLES),
        "memberships": list(auth.MEMBERSHIPS),
    })


@router.post("/users/{user_id}", response_model=ApiResponse)
async def update_user(
    user_id: int,
    req: UserUpdateRequest,
    user: User = Depends(current_user),
) -> ApiResponse:
    """
    管理员改**别人**的角色 / 档位 / 启用状态。**仅管理员。**

    ⚠️ 三条**刻意的拒绝**，每一条都是"能让操作者把自己锁在门外"的形状：

      ① 不能改自己 —— 演示时若管理员把自己降成普通用户，就再也没有账号
         能改回来（改回来需要管理员），只能手删 `data/users_override.json`。
      ② **不能把别人设成管理员** ——「管理页做的是权限分配，但不含管理员权限」
         （需求方 2026-09-24 明确）。管理员账号的增减是一条不该出现在
         演示界面上的路径：它一旦可点，一个误操作就能造出一个权限对等的
         账号，而且没有任何地方能看出"谁造了它"。
      ③ **不能封禁管理员账号**（含自己）—— 封了管理员就没人能解封。
         与 ② 同源：管理员账号整体在这个管理页的**管辖范围之外**。

    想试角色切换，用 demo / vip 那两个账号登进去看。
    """
    if user.role != auth.ROLE_ADMIN:
        raise ApiError(
            4002,
            f"修改账号需要管理员权限。当前角色：{user.role_label}。",
            data={"role": user.role, "required_role": auth.ROLE_ADMIN},
        )
    if user_id == user.id:
        raise ApiError(
            4002,
            "不能修改自己的账号 —— 演示时一旦把自己降级或封禁，就没有账号能改回来了。"
            "想验证角色差异，请用 demo / vip 账号登录查看。",
            data={"self": True},
        )
    if req.role is None and req.membership is None and req.is_active is None:
        raise ApiError(4001, "role / membership / is_active 至少要传一个")
    if req.role == auth.ROLE_ADMIN:
        raise ApiError(
            4002,
            "本页不提供管理员权限的分配。把账号提升为管理员会造出一个权限对等的账号，"
            "而这条路径不该出现在演示界面上（要加管理员请直接改 "
            "seed_data/users.json 并重启）。",
            data={"forbidden_role": auth.ROLE_ADMIN, "allowed_roles": [auth.ROLE_DESIGNER, auth.ROLE_USER]},
        )

    target = auth.find_by_id(user_id)
    if target is None:
        raise ApiError(4004, f"用户不存在：id={user_id}")

    # ②/③ 的另一半：不许动管理员账号。**要在改之前判断**，
    # 否则"把管理员先降级再封禁"会被拆成两步绕过去。
    if target.role == auth.ROLE_ADMIN:
        raise ApiError(
            4002,
            f"账号 {target.username} 是管理员，不在本页的管辖范围内 —— "
            f"管理员账号不能被改角色、改档位或封禁。",
            data={"target_role": target.role},
        )

    try:
        updated = target
        if req.role is not None:
            updated = auth.set_user_role(user_id, req.role)
        if req.membership is not None:
            updated = auth.set_user_membership(user_id, req.membership)
        if req.is_active is not None:
            updated = auth.set_user_active(user_id, req.is_active)
    except ValueError as e:
        # auth 层抛的取值校验错误 → 4001，并带上允许的取值
        raise ApiError(
            4001,
            str(e),
            data={"allowed_roles": [auth.ROLE_DESIGNER, auth.ROLE_USER],
                  "allowed_memberships": list(auth.MEMBERSHIPS)},
        ) from e

    logger.info(f"[auth] {user.username} 修改用户 id={user_id} → "
                f"role={req.role} membership={req.membership} is_active={req.is_active}")
    return ApiResponse.ok({"user": updated.to_dict()})


# ══════════════════════════════════════════════════════════════════
# 4.2 户型解析
# ══════════════════════════════════════════════════════════════════


@router.post("/layout/parse", response_model=ApiResponse)
async def parse_layout(
    req: ParseRequest,
    request: Request,
    user: User = Depends(current_user),
) -> ApiResponse:
    """
    提交户型图解析。**立即返回 task_id**，结果经 `/task/{id}/status` 轮询。

    只跑「解析 → 诊断」这一段（实测 33–48 秒）。方案生成是另一个接口 ——
    在这里顺带跑完会多花 90 秒、多调十几次 LLM，而调用方只要那份户型 JSON。

    解析是**免费功能**（不受会员档位限制），但受每日配额约束（AC-13）。
    """
    if not req.image.strip():
        raise ApiError(4001, "image 不能为空")

    # ⚠️ 顺序是有意的：**先校验入参、再扣额度**。
    #    反过来的话，一个拼错的请求也会白扣用户一次配额。
    await consume_quota(user, "parse")

    tm = get_task_manager()
    rec = tm.create("parse", trace_id=_trace_id(request), user_id=user.id)

    state = initial_state(
        task_id=rec.task_id,
        trace_id=rec.trace_id,
        image_ref=req.image,
        image_media_type=req.image_media_type,
        detail_level=req.detail_level,
        prefer_local=req.prefer_local,
    )
    tm.start(rec, state, stages="parse")

    return ApiResponse.ok({
        "task_id": rec.task_id,
        "trace_id": rec.trace_id,
        "status": "processing",
        "poll": f"/api/v1/task/{rec.task_id}/status",
        # 前端据此设计轮询节奏（2.2.4）；给个参考值省得它自己猜。
        # ⚠️ 这是**实测区间**的中位估计，不是承诺值 ——
        # 两次真实解析分别是 33s 与 48s，差异来自模型侧延迟波动。
        # 轮询策略仍要按 2.2.4 的 2 秒节奏走，前端不能依赖这个数。
        #
        # 这个数**不在这里手填**：它和轮询接口返回的剩余时间同源
        # （`core/progress.py` 的阶段模型）。手填的话，改了一处忘了另一处，
        # 就会出现"一开始说 40 秒，第一轮轮询立刻改口说 90 秒"。
        "estimated_seconds": round(total_seconds("parse")),
    })


# ══════════════════════════════════════════════════════════════════
# 4.3 方案生成
# ══════════════════════════════════════════════════════════════════


@router.post("/design/generate", response_model=ApiResponse)
async def design_generate(
    req: GenerateRequest,
    request: Request,
    user: User = Depends(current_user),
) -> ApiResponse:
    """
    按 `layout_id` 生成方案。**立即返回 task_id**。

    生成是**付费功能**：免费用户在入口就被 4005 拦下。

    ⚠️ **不重新解析户型图。** 视觉解析约 16 秒，且模型有随机性 ——
    同一个 `layout_id` 重新解析可能得到不同的房间数。用户会发现自己看的户型
    和生成方案用的不是同一份，而界面上写着同一个 id。
    **同一个 id 必须指同一份数据。**

    所以这里从暂存里取户型，图从 `diagnose_layout` 进（跳过已做过的解析）。
    """
    # ⚠️ 授权检查放在**读户型之前** —— 先答"你能不能做这件事"，
    #    再答"这份数据在不在"。反过来等于给了一个探测 layout_id 的接口。
    require_paid(user, "装修方案生成")

    layout = await layout_store.load(req.layout_id)
    if layout is None:
        raise ApiError(
            4004,
            f"找不到 layout_id={req.layout_id} 的户型。"
            f"可能已过期（暂存 1 小时），请重新解析。",
        )

    # 能力守卫：数据不支撑方案生成时**在入口就拦下**，不放进图里
    # 让三个分支各撞一次墙（capabilities.py 的立场）
    from ..core.capabilities import check_operation

    cap = check_operation(layout, "generate_plan")
    if not cap.allowed:
        raise ApiError(4002, cap.reason,
                       data={"missing": cap.missing, "suggestion": cap.suggestion})

    if not req.styles or not req.budget_grades:
        raise ApiError(4001, "styles 与 budget_grades 都不能为空")

    # ⚠️ **必须校验取值，不能只校验非空。**
    #
    # 2026-09-23 实测踩到：前端「生成参数」的第 3 套发的是 `luxury`，
    # 而后端 `PlanStyle` 里没有这个值。下游 `space_planner.py` 用
    # `_STYLE_HINTS.get(style, '按该风格的通行做法处理')` 兜底 ——
    # 那一套方案**完全没拿到风格引导**，界面上却仍显示「意式轻奢」。
    # 全程零报错，正是本项目最防的那类「看起来成功、实际没做」。
    #
    # 拦在入口的理由与能力守卫同理（capabilities.py 的立场）：
    # 与其让三个分支各静默降级一次，不如在这里说清哪个值不认、允许哪些。
    allowed_styles = set(get_args(PlanStyle))
    allowed_grades = set(get_args(BudgetGrade))
    bad_styles = [s for s in req.styles if s not in allowed_styles]
    bad_grades = [g for g in req.budget_grades if g not in allowed_grades]
    if bad_styles or bad_grades:
        detail = "".join(
            [
                f"不认识的风格 {bad_styles}；" if bad_styles else "",
                f"不认识的预算档位 {bad_grades}；" if bad_grades else "",
            ]
        )
        raise ApiError(
            4001,
            f"风格或预算档位取值不合法：{detail}",
            data={
                "allowed_styles": sorted(allowed_styles),
                "allowed_budget_grades": sorted(allowed_grades),
            },
        )

    # ── AC-19：材料偏好必须在**提交任务之前**校验 ──────────────
    #
    # 排除到候选池为空这件事，晚一步发现会有两种代价，都很难看：
    #   · 整个池子空了 → A-05 抛错 → 任务失败，而 A-02 诊断 + 九路并发的
    #     40 秒和一次付费额度**已经花掉了**（`consume_quota` 在下面）；
    #   · 只有某个档位空了 → 那套方案照出，只是**静默少一个品类**，
    #     用户拿到一份"看起来正常、短了一行"的清单。这正是本项目
    #     最防的那类「看起来合理的错误」。
    #
    # 所以这里一次把「品类 × 档位」全试一遍，把矛盾在提交前说清。
    # 与上面 styles/budget_grades 的取值校验同一个立场（capabilities.py）：
    # 与其让三个分支各静默降级一次，不如在入口说清哪里不对、允许什么。
    from ..services.material.filters import MaterialFilters, validate_filters

    filters = MaterialFilters.from_payload({
        "excluded_categories": req.excluded_categories,
        "excluded_brands": req.excluded_brands,
        "preferred_brands": req.preferred_brands,
        "quanyou_priority": req.quanyou_priority,
    })
    problems = validate_filters(
        filters,
        grades=req.budget_grades,
        room_names=[
            str((r or {}).get("name") or "")
            for r in (layout.get("rooms") or [])
            if isinstance(r, dict)
        ],
    )
    if problems:
        raise ApiError(
            4001,
            "材料偏好与可选范围冲突：" + "；".join(p.message for p in problems),
            data={
                "problems": [p.as_dict() for p in problems],
                "allowed_categories": [
                    {"key": c["key"], "label": c["label"]}
                    for c in catalog.categories()
                ],
                "allowed_brands": sorted({p.brand for p in catalog.all_products()}),
            },
        )

    await consume_quota(user, "generate")

    tm = get_task_manager()
    rec = tm.create("generate", trace_id=_trace_id(request), user_id=user.id)

    state = initial_state(
        task_id=rec.task_id,
        trace_id=rec.trace_id,
        # 关键：把已解析的户型直接放进状态，图从诊断进
        layout=layout,
        layout_id=req.layout_id,
        styles=req.styles,
        budget_grades=req.budget_grades,
        quanyou_priority=req.quanyou_priority,
        requirements=req.requirements or {},
        # 与 quanyou_priority 同源（filters 里那份是权威，见 state.py 注释）
        material_filters=filters.to_dict(),
    )
    tm.start(rec, state, stages="generate")

    return ApiResponse.ok({
        "task_id": rec.task_id,
        "trace_id": rec.trace_id,
        "status": "processing",
        "poll": f"/api/v1/task/{rec.task_id}/status",
        "plan_count": min(len(req.styles), len(req.budget_grades)),
        # ⚠️ 需求文档 12.3 的示例写的是 45 秒，那是**实现前**的估计。
        # 实测整条链 122.9s（A-02 15.8 + 九路并发 21.3 + A-06 审查 82.4），
        # 与文档的差异在此标注（文档是决策记录，不改）。
        "estimated_seconds": round(total_seconds("generate")),
    })


# ══════════════════════════════════════════════════════════════════
# 4.6 报价单/合同审查
# ══════════════════════════════════════════════════════════════════


@router.post("/avoid-pit/review", response_model=ApiResponse)
async def avoid_pit_review(
    req: ReviewRequest,
    request: Request,
    user: User = Depends(current_user),
) -> ApiResponse:
    """
    审查一份报价单/合同。**立即返回 task_id**。

    走的是 A-06 的 quote 模式（唯一模式 —— 分支内的 plan 模式由链上自动触发）。

    审查是**付费功能**：免费用户在入口就被 4005 拦下。
    ⚠️ 它**不占用解析/生成的配额** —— `TaskType` 只有 parse 与 generate
    两个桶（见 `redis_client.py`），审查没有自己的计数器。
    这不是遗漏：审查成本约 19 秒、一次 LLM 调用，与生成不是一个量级。
    """
    if not req.quote_text.strip():
        raise ApiError(4001, "quote_text 不能为空")

    require_paid(user, "报价单避坑审查")

    tm = get_task_manager()
    rec = tm.create("review", trace_id=_trace_id(request), user_id=user.id)

    state = initial_state(
        task_id=rec.task_id,
        trace_id=rec.trace_id,
        quote_text=req.quote_text,
        requirements=req.requirements or {},
    )
    # 审查不依赖户型，独立跑 A-06 一个节点（复用完整图的话会缺 layout 而失败）
    tm.start(rec, state, stages="review")

    return ApiResponse.ok({
        "task_id": rec.task_id,
        "trace_id": rec.trace_id,
        "status": "processing",
        "poll": f"/api/v1/task/{rec.task_id}/status",
        # 单次 A-06 实测约 19s（`REVIEW_TIMEOUT=75s` 是上限，不是期望值）。
        # 同上：与剩余时间同源，不在这里手填。
        "estimated_seconds": round(total_seconds("review")),
    })


# ══════════════════════════════════════════════════════════════════
# 4.4 任务状态轮询
# ══════════════════════════════════════════════════════════════════


@router.get("/task/{task_id}/status", response_model=ApiResponse)
async def task_status(task_id: str, request: Request) -> ApiResponse:
    """
    查询任务状态。

    ⚠️ **未完成时同样返回 200 + code=0**，只是 `status` / `phase` / `progress` 不同。
    用 4xx 表示"还没好"会被前端 axios 拦截器误判为错误（4.4 明确要求）。

    响应标 `Cache-Control: no-store` —— 进度必须实打实地拿到最新值，
    中间层缓存会让界面卡在某个阶段不动（4.4）。
    """
    from fastapi.responses import JSONResponse

    data = await get_task_manager().status(task_id)
    if data is None:
        raise ApiError(4004, f"任务不存在或已过期（结果保留 1 小时）：{task_id}")

    resp = JSONResponse(content=ApiResponse.ok(data).model_dump())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/task/{task_id}/cancel", response_model=ApiResponse)
async def cancel_task(
    task_id: str,
    user: User = Depends(current_user),
) -> ApiResponse:
    """
    中断一个在飞任务（AC-31）。

    **取消权归用户本人** —— 停机不再主动打断任何任务，见
    `TaskManager.shutdown` 的口径说明。

    ⚠️ 三个"不是错误"的情况，都在这里按正常响应返回（`code=0`）：
      · 任务已经跑完了 —— 用户点的时候它刚好结束，那是"你赢了"
      · 任务早就不在本进程里（重启过）—— 没东西可打断
      · 重复点两次 —— 第二次看到的是终态
    把它们做成错误，界面就得为"其实什么都没发生"弹一个红框。

    ⚠️ 额度**不退**：`consume_quota` 在建任务之前就执行了，token 此时
    已经产生（见 deps.consume_quota）。所以前端的确认框里必须提前写明，
    不能等用户点完再说。
    """
    from .tasks import TaskNotFound, TaskNotOwned

    try:
        data = await get_task_manager().cancel(task_id, user_id=user.id)
    except TaskNotFound:
        raise ApiError(4004, f"任务不存在或已过期（结果保留 1 小时）：{task_id}")
    except TaskNotOwned:
        # ⚠️ 措辞刻意模糊：既不确认"这个 id 存在"，也不透露它属于谁。
        raise ApiError(4005, "只能中断自己发起的任务")

    return ApiResponse.ok(data)


# ══════════════════════════════════════════════════════════════════
# 4.5 材料价格查询（同步，纯查表）
# ══════════════════════════════════════════════════════════════════


@router.get("/material/price", response_model=ApiResponse)
async def material_price(
    category: str = "",
    brand: str = "",
    quanyou_first: bool = True,
) -> ApiResponse:
    """
    材料价格查询。

    **同步接口**：纯查演示目录，不调模型、不查向量库，毫秒级返回。

    返回结构对齐 4.5 的契约：`quanyou_recommended`（全友自有）+ `items`（其它品牌）。
    **一定要带上 `disclaimer`** —— 需求文档 13.3 与 R-09 要求演示数据必须显著标注，
    而这正是最容易被前端"顺手美化"掉的地方。
    """
    cats = {c["key"]: c for c in catalog.categories()}
    if category and category not in cats:
        raise ApiError(4001, f"未知品类 {category}；可用：{sorted(cats)}")

    products = [
        p for p in catalog.all_products()
        if (not category or p.category == category)
        and (not brand or brand in p.brand)
    ]

    def row(p: catalog.Product) -> dict[str, Any]:
        return {
            "id": p.id, "name": p.name, "brand": p.brand,
            "category": p.category,
            "category_label": cats.get(p.category, {}).get("label", p.category),
            "spec": p.spec,
            "price_range": list(p.price_range),
            "unit": cats.get(p.category, {}).get("unit", ""),
            "eco_level": p.eco_level,
            "search_url": p.search_url,
        }

    qy = sorted([row(p) for p in products if p.is_quanyou],
                key=lambda r: r["price_range"][0])
    other = sorted([row(p) for p in products if not p.is_quanyou],
                   key=lambda r: r["price_range"][0])

    return ApiResponse.ok({
        "category": category or "all",
        "category_name": cats.get(category, {}).get("label", "全部品类"),
        # quanyou_first=False 时只给市场对照，不推自有品牌
        "quanyou_recommended": qy if quanyou_first else [],
        "items": other,
        "total": len(qy) + len(other),
        "catalog_version": catalog.catalog_version(),
        "disclaimer": catalog.disclaimer(),
    })


@router.get("/material/options", response_model=ApiResponse)
async def material_options() -> ApiResponse:
    """
    材料偏好的可选项（AC-19）。

    存在的理由很窄：**前端不该把品类与品牌硬编码一份**。
    硬编码的清单会随目录变更而漂移，而且漂移是静默的 ——
    界面照常显示一个后端已经不认的品类，用户勾了，请求 4001 回来，
    看起来像后端的故障。所以清单从目录里来，跟 `catalog.py` 单点一致。

    `constants` 里的两个数字同理：AC-18 的 60% 底线与偏好品牌加分
    都定义在 `catalog.py`，前端引用它们，而不是各写一遍。
    """
    return ApiResponse.ok({
        "categories": [
            {"key": c["key"], "label": c["label"], "unit": c.get("unit", "")}
            for c in catalog.categories()
        ],
        "brands": sorted({p.brand for p in catalog.all_products()}),
        "quanyou_brand": "全友",
        "constants": {
            "min_quanyou_coverage": catalog.MIN_QUANYOU_COVERAGE,
            "preferred_brand_bonus": catalog.PREFERRED_BRAND_BONUS,
            "quanyou_preference_bonus": catalog.QUANYOU_PREFERENCE_BONUS,
        },
        "catalog_version": catalog.catalog_version(),
    })


# ══════════════════════════════════════════════════════════════════
# 4.3′ 矢量图与热区（AC-07 / AC-09 / AC-21）
# ══════════════════════════════════════════════════════════════════
#
# 为什么是**两个**接口而不是把 SVG 塞进解析结果里：
#
#   · 解析接口的响应已经带了 layout + scene + diagnosis + trace。
#     再塞一张 30–60KB 的 SVG，每次轮询都要传一遍 ——
#     而轮询是每 1–5 秒一次的（需求文档 11.2.4）。
#   · SVG 要能单独被 <img src> / 新窗口打开 / 下载，这些都需要一个 URL。
#     需求文档 12.4 的契约里写的就是 `"vector": {"url": ...}`，是 URL 不是内容。
#
# ⚠️ 两个接口都必须走 `render_plan_for` —— 见该函数的说明。


@router.get("/layout/{layout_id}/plan.svg")
async def layout_plan_svg(layout_id: str) -> Response:
    """
    矢量户型图（AC-07）。

    **同步接口**：纯 CPU、纯字符串拼接，实测 0.5ms 量级 ——
    AC-07 的"< 3s"有四个数量级的余量。所以不需要走异步任务那一套。

    返回裸 `image/svg+xml` 而不是 `ApiResponse` 信封：它的消费者是
    `<img>` / `<iframe>` / 浏览器窗口，不是我们的前端代码。
    """
    layout = await layout_store.load(layout_id)
    if not layout:
        raise ApiError(4004, f"户型 {layout_id} 不存在或已过期（保留 1 小时）")

    # AC-10 的「局部替换」：用户换过的地面材料要体现在这张图上。
    # **颜色在服务端定**（材料目录里的 `swatch`），前端不参与配色 ——
    # 否则同一台户型在不同端会画出不同的颜色。
    floor_fills = await _floor_fills(layout_id)

    try:
        _, plan = render_plan_for(layout, floor_fills=floor_fills)
    except Exception as e:  # noqa: BLE001 —— 渲染不该失败，失败要留下原因
        logger.exception(f"[render] 矢量图渲染失败 layout_id={layout_id}")
        raise ApiError(5003, f"矢量图渲染失败：{type(e).__name__}") from e

    return Response(
        content=plan.svg,
        media_type="image/svg+xml",
        headers={
            # 户型属于用户数据，别让中间层缓存
            "Cache-Control": "private, max-age=300",
            "X-Plan-Width": str(plan.width_px),
            "X-Plan-Height": str(plan.height_px),
            # 画不准的地方必须能传到调用方，不能只留在服务端日志里
            "X-Plan-Warnings": str(len(plan.warnings)),
        },
    )


# ══════════════════════════════════════════════════════════════════
# 4.3‴ 地面材质替换（AC-10）
# ══════════════════════════════════════════════════════════════════
#
# ⚠️ **AC-10 原文是「框选地板 → 输入"深色胡桃木" → 局部重绘成功」**，
#    而它的载体是 AI 出图路径 —— 那条路径已随 AC-08 作废（2026-09-23，
#    产出的是俯视家具平面图，不是 3D 等轴测渲染图）。
#
#    2026-09-26 需求方把它**重新定性**为：**在矢量图上换地面材质**。
#    为什么这个重定性是合理的：
#      · 交付物里本来就有矢量户型图（AC-07），而**材料价格就挂在这张图上**
#        （AC-09/21 的热区）—— 所以"在这张图上换材料"和它是一套的
#      · 换材质要**反映到造价上**才有业务价值，而规则引擎算造价是本项目
#        一直在做的事（ADR-07）
#      · 它可测、可复现，不依赖任何生成模型
#
# 一句话：**点一块地面 → 换一种材料 → 图上换色 + 这间房的造价跟着变。**

#: 能作为地面材料的品类。**只有这两个**：它们的单位都是元/㎡，
#: 且都是地面铺装。灯具（元/套）、室内门（元/樘）没法铺在地上。
FLOOR_CATEGORIES: tuple[str, ...] = ("floor", "tile")


async def _floor_fills(layout_id: str) -> dict[int, str]:
    """
    房间下标 → 材料代表色。**渲染层要的那一份**。

    单独抽出来是因为 `plan.svg` 要用它，而它必须**永不抛异常**：
    一张画不出来的户型图比"颜色没生效"严重得多。
    """
    from ..services.material import catalog as mat_catalog

    try:
        subs = await layout_store.load_floor_materials(layout_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[floor-material] 读替换记录失败（按未替换渲染）：{e}")
        return {}
    if not subs:
        return {}

    by_id = mat_catalog.by_id()
    out: dict[int, str] = {}
    for room_index, product_id in subs.items():
        p = by_id.get(product_id)
        # ⚠️ 材料被下架（目录改过）时**跳过而不是崩** —— 换过的那间房
        #    退回默认房型配色，图上少一处替换，而不会整张图画不出来。
        if p and p.swatch:
            out[int(room_index)] = p.swatch
    return out


def _floor_material_options() -> list[dict[str, Any]]:
    """
    可选的地面材料。**清单从目录来**，前端不硬编码 ——
    硬编码的那份会随目录变更静默漂移，用户选中一个后端已经不认的
    product id，请求 4001 回来，在用户看来像后端坏了。

    ⚠️ 筛的是 `surface`（能铺在哪儿），**不是只看 category**：
    `QY-TL-102` 的 category 是 `tile`，但它名字里写着「釉面**内墙**砖」——
    只按 category 筛会把它当成地面材料推荐出来。
    """
    from ..services.material import catalog as mat_catalog

    out = []
    for p in mat_catalog.all_products():
        if p.category not in FLOOR_CATEGORIES:
            continue
        if p.surface not in ("floor", "both"):
            continue
        out.append({
            "id": p.id, "name": p.name, "brand": p.brand,
            "is_quanyou": p.is_quanyou, "spec": p.spec,
            "price_range": list(p.price_range),
            "swatch": p.swatch,
            "search_url": p.search_url,
        })
    return sorted(out, key=lambda x: (x["price_range"][0], x["id"]))


def _substitution_view(
    subs: dict[int, str], floor_hotspots: dict[int, tuple[str, float]],
) -> list[dict[str, Any]]:
    """
    把「房间 → product_id」摊成给人看的清单：材料、面积、造价区间。

    `floor_hotspots` 是 `{room_index: (房间名, 面积)}`，由热区层给 ——
    **面积不在这一层重新算**：热区那里已经算过一次（`quantity`），
    两处各算一遍迟早分叉，而分叉的表现是"图上标的面积和造价用的面积不一样"。
    """
    from ..services.material import catalog as mat_catalog

    by_id = mat_catalog.by_id()
    out: list[dict[str, Any]] = []
    for room_index, product_id in sorted(subs.items()):
        p = by_id.get(product_id)
        name, area = floor_hotspots.get(int(room_index), ("", 0.0))
        if p is None:
            # 材料下架了：如实说，不假装这间房没换过
            out.append({
                "room_index": int(room_index), "room_name": name, "area_m2": area,
                "material": None, "cost": None,
                "note": f"材料 {product_id} 已不在目录里（演示目录改过？），"
                        f"图上按默认配色渲染",
            })
            continue
        lo, hi = p.price_range
        out.append({
            "room_index": int(room_index),
            "room_name": name,
            "area_m2": round(area, 2),
            "material": {
                "id": p.id, "name": p.name, "brand": p.brand,
                "is_quanyou": p.is_quanyou, "spec": p.spec,
                "price_range": [lo, hi], "swatch": p.swatch,
                "search_url": p.search_url,
            },
            # 造价 = 面积 × 单价区间。**不取中位数当"一个数"** ——
            # 那会变成一个看起来很确定的数字，而它其实是区间。
            #
            # ⚠️ `unit` 是**「元」不是「元/㎡」**：这是这间房地面的总价，
            #    乘过面积了。实测踩过一次 —— 我原来写的是 `p.unit_hint`
            #    （元/㎡），于是界面上显示「地面造价 3817.69–5667.09 元/㎡」，
            #    而那是 13.21㎡ 的总价。**一个把总额标成单价的界面在说谎。**
            #    单价区间在 `material.price_range` 里，各归各位。
            "cost": {
                "min": round(area * lo, 2),
                "max": round(area * hi, 2),
                "unit": "元",
            },
            # ⚠️ **正常路径也带这个键（空串），不要只在异常时才有。**
            #    契约测试逐键比对时当场发现：`note` 只在"材料下架"那条
            #    分支里出现，于是**响应的形状取决于数据**。
            #    这种接口很难消费 —— 调用方得先判断"这次有没有这个键"，
            #    而漏判一处就是 `undefined`。
            "note": "",
        })
    return out


async def _floor_hotspot_index(layout_id: str, layout: dict
                               ) -> dict[int, tuple[str, float]]:
    """`{房间下标: (「XX地面」的标签, 面积)}`，来自热区层。"""
    from ..services.render import hotspot_payload

    _scene, plan = render_plan_for(layout)
    payload = hotspot_payload(_scene, plan.projection)
    out: dict[int, tuple[str, float]] = {}
    for h in payload["hotspots"]:
        if h["category"] == "floor" and h.get("room_index") is not None:
            out[int(h["room_index"])] = (
                str(h["label"]).removesuffix("地面"), float(h["quantity"] or 0.0),
            )
    return out


def _floor_payload(layout_id: str,
                   floor_hotspots: dict[int, tuple[str, float]],
                   subs: dict[int, str]) -> dict[str, Any]:
    return {
        "layout_id": layout_id,
        # 可替换的房间清单。**前端不必再调 /hotspots 自己筛一遍** ——
        # 两处各筛一次的话，"哪些房间能换地面"就有了两个来源。
        "rooms": [
            {"room_index": i, "room_name": n, "area_m2": round(a, 2)}
            for i, (n, a) in sorted(floor_hotspots.items())
        ],
        "substitutions": _substitution_view(subs, floor_hotspots),
        "eligible": _floor_material_options(),
        "notes": [
            "图上填的是材料的**代表色**，不是效果图 —— 真实纹理要看实物或官网。",
            "造价 = 这间房的地面面积 × 单价区间。面积由房间几何算出，"
            "**未扣除固定家具占位**（与热区同一口径）。",
            "替换记录与户型同样保留 1 小时，并落库；解析产物本身不受影响。",
        ],
    }


@router.get("/layout/{layout_id}/floor-materials", response_model=ApiResponse)
async def floor_materials(layout_id: str) -> ApiResponse:
    """
    这台户型当前的地面材质替换 + 可选材料清单（AC-10）。

    前端打开矢量图页时调它一次：拿到"当前换成什么样了"（`substitutions`）
    与"可以换成什么"（`eligible`）。**清单由后端给**，理由同
    `/material/options` —— 前端存一份会静默漂移。
    """
    layout = await layout_store.load(layout_id)
    if not layout:
        raise ApiError(4004, f"户型 {layout_id} 不存在或已过期（保留 1 小时）")

    hotspots = await _floor_hotspot_index(layout_id, layout)
    subs = await layout_store.load_floor_materials(layout_id)
    return ApiResponse.ok(_floor_payload(layout_id, hotspots, subs))


@router.post("/layout/{layout_id}/floor-material", response_model=ApiResponse)
async def set_floor_material(
    layout_id: str,
    req: FloorMaterialRequest,
) -> ApiResponse:
    """
    把某间房的地面换成一种材料。**`material_id` 传空串 = 还原成默认配色。**

    ⚠️ 校验顺序：**先户型、再房间、再材料**。反过来的话，
    一个不存在的房间会拿到"材料不存在"，排查方向整个偏掉。

    ⚠️ 这一步**不需要会员、也不扣配额**：它是查看户型时的交互，
    与"生成方案"不是一回事（AC-13 的配额只分 parse / generate 两个桶）。
    """
    layout = await layout_store.load(layout_id)
    if not layout:
        raise ApiError(4004, f"户型 {layout_id} 不存在或已过期（保留 1 小时）")

    hotspots = await _floor_hotspot_index(layout_id, layout)
    room_index = int(req.room_index)
    if room_index not in hotspots:
        raise ApiError(
            4001,
            f"房间下标 {room_index} 不是这台户型里可替换地面的房间。"
            f"可选的是：{sorted(hotspots)}（来自地面热区）。",
        )

    subs = await layout_store.load_floor_materials(layout_id)
    material_id = (req.material_id or "").strip()

    if not material_id:
        # 还原：**要真的删掉这个键并写下去**，不能只在内存里抹掉
        subs.pop(room_index, None)
        await layout_store.save_floor_materials(layout_id, subs)
        logger.info(f"[floor-material] 还原房间 {room_index} 的地面 layout={layout_id}")
        return ApiResponse.ok(_floor_payload(layout_id, hotspots, subs))

    options = {o["id"]: o for o in _floor_material_options()}
    if material_id not in options:
        raise ApiError(
            4001,
            f"材料 {material_id!r} 不能用作地面 —— 下拉里的可选值是 "
            f"{sorted(options)}（品类限 {list(FLOOR_CATEGORIES)}，"
            f"且 `surface` 必须是 floor 或 both）。",
        )

    subs[room_index] = material_id
    await layout_store.save_floor_materials(layout_id, subs)
    chosen = options[material_id]
    logger.info(
        f"[floor-material] 房间 {room_index} 换成 {material_id}"
        f"（{chosen['name']}）layout={layout_id}"
    )
    audit("floor_material", resource_type="layout", resource_id=layout_id,
          room_index=room_index, material_id=material_id)
    return ApiResponse.ok(_floor_payload(layout_id, hotspots, subs))


# ══════════════════════════════════════════════════════════════════
# 智友问答（对话式 RAG）—— 见 `services/chat/answer.py`
# ══════════════════════════════════════════════════════════════════


@router.get("/chat/conversations", response_model=ApiResponse)
async def list_chat_conversations(user: User = Depends(current_user)) -> ApiResponse:
    """
    我的会话列表（置顶优先、其次最近更新）。

    ⚠️ **库不通时返回 5003，不返回空列表。** 空列表在界面上就是
    "你还没有对话" —— 用户会以为历史丢了，而其实只是连不上。
    """
    rows = await repository.list_conversations(user.id)
    if rows is None:
        raise ApiError(5003, "读不到会话列表（数据库不可用）—— 稍后重试，历史没有丢")
    return ApiResponse.ok({"conversations": rows})


@router.post("/chat/conversations", response_model=ApiResponse)
async def create_chat_conversation(
    req: ChatConversationRequest,
    user: User = Depends(current_user),
) -> ApiResponse:
    """新建一个会话。标题可以空着 —— 第一条提问会自动成为标题。"""
    cid = f"chat_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid4().hex[:10]}"
    ok = await repository.create_conversation(
        cid, user.id,
        title=(req.title or "新对话"),
        layout_id=(req.layout_id or "").strip() or None,
        plan_id=(req.plan_id or "").strip() or None,
    )
    if not ok:
        raise ApiError(5003, "新建会话失败（数据库不可用）")
    row = await repository.get_conversation(cid)
    return ApiResponse.ok({"conversation": row or {"conversation_id": cid}})


@router.patch("/chat/conversations/{conversation_id}", response_model=ApiResponse)
async def patch_chat_conversation(
    conversation_id: str,
    req: ChatConversationPatch,
    user: User = Depends(current_user),
) -> ApiResponse:
    """
    重命名 / 置顶 / 取消置顶。

    ⚠️ **先查归属再改**：不查的话，改别人会话的标题是可以成功的
    （只要猜得到 id）—— 那是越权，不是"用不到的功能"。
    """
    row = await repository.get_conversation(conversation_id)
    if not row:
        raise ApiError(4004, f"会话 {conversation_id} 不存在")
    if int(row.get("user_id") or -1) != user.id:
        raise ApiError(4002, "这个会话不属于当前账号，不能修改")
    ok = await repository.update_conversation(
        conversation_id, title=req.title, pinned=req.pinned,
    )
    if not ok:
        raise ApiError(5003, "更新会话失败（数据库不可用）")
    return ApiResponse.ok({"conversation": await repository.get_conversation(conversation_id)})


@router.delete("/chat/conversations/{conversation_id}", response_model=ApiResponse)
async def delete_chat_conversation(
    conversation_id: str,
    user: User = Depends(current_user),
) -> ApiResponse:
    """删除会话（消息随外键级联删除）。归属校验同 PATCH。"""
    row = await repository.get_conversation(conversation_id)
    if not row:
        raise ApiError(4004, f"会话 {conversation_id} 不存在")
    if int(row.get("user_id") or -1) != user.id:
        raise ApiError(4002, "这个会话不属于当前账号，不能删除")
    if not await repository.delete_conversation(conversation_id):
        raise ApiError(5003, "删除会话失败（数据库不可用）")
    audit("chat_conversation_deleted", resource_type="chat",
          resource_id=conversation_id)
    return ApiResponse.ok({"deleted": conversation_id})


@router.get("/chat/conversations/{conversation_id}/messages", response_model=ApiResponse)
async def list_chat_messages(
    conversation_id: str,
    user: User = Depends(current_user),
) -> ApiResponse:
    """一条会话的全部消息（含每条回答当时的引用来源）。"""
    row = await repository.get_conversation(conversation_id)
    if not row:
        raise ApiError(4004, f"会话 {conversation_id} 不存在")
    if int(row.get("user_id") or -1) != user.id:
        raise ApiError(4002, "这个会话不属于当前账号")
    rows = await repository.list_messages(conversation_id)
    if rows is None:
        raise ApiError(5003, "读不到消息（数据库不可用）")
    return ApiResponse.ok({"conversation": row, "messages": rows})


@router.post("/chat/ask")
async def chat_ask(
    req: ChatAskRequest,
    user: User = Depends(current_user),
) -> StreamingResponse:
    """
    提问并**流式**拿回答（SSE）。

    ⚠️ 这条路由**不用 `ApiResponse` 包装**：SSE 是一行行的 `data:`，
    不是一次性 JSON。错误也用同一条流报（`{"error": "..."}`），
    这样前端只需要处理一种解析路径。

    事件顺序（固定）：
        {"type": "meta",   ...}     ← 先告诉前端"这次看到了哪些资料"
        {"type": "delta",  "text": "…"} × N
        {"type": "sources", "sources": [...]}  ← 引用清单：**由代码给，不由模型写**
        {"type": "done",   ...}
        出错时：{"type": "error", "message": "…"}
    """
    question = (req.question or "").strip()
    if not question:
        raise ApiError(4001, "问题不能为空")

    # 会话：给了 id 就用它，没给就现建一个（前端第一次提问时不用先调一次创建）
    conversation_id = (req.conversation_id or "").strip()
    if conversation_id:
        row = await repository.get_conversation(conversation_id)
        if not row:
            raise ApiError(4004, f"会话 {conversation_id} 不存在")
        if int(row.get("user_id") or -1) != user.id:
            raise ApiError(4002, "这个会话不属于当前账号")
    else:
        conversation_id = f"chat_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid4().hex[:10]}"
        # 标题取问题前 24 字：用户不用先想标题，列表里也认得出来
        title = question[:24] + ("…" if len(question) > 24 else "")
        await repository.create_conversation(
            conversation_id, user.id, title=title,
            layout_id=(req.layout_id or "").strip() or None,
            plan_id=(req.plan_id or "").strip() or None,
        )

    # 依据：户型 + 屋主详情 + 方案（方案从库里按 plan_id 取）
    layout_id = (req.layout_id or "").strip() or str(
        (await repository.get_conversation(conversation_id) or {}).get("layout_id") or ""
    )
    plan_id = (req.plan_id or "").strip() or str(
        (await repository.get_conversation(conversation_id) or {}).get("plan_id") or ""
    )
    layout = await layout_store.load(layout_id) if layout_id else None
    plan = (await repository.load_plan(layout_id, plan_id)
            if (layout_id and plan_id) else None)

    ctx = build_context(question, layout=layout, plan=plan)
    history_rows = await repository.list_messages(conversation_id) or []
    history = build_history(history_rows)

    # 用户这句话先落库：即使模型随后失败，对话里也留得下"我问了什么"
    await repository.append_message(conversation_id, "user", question)

    async def event_stream() -> AsyncIterator[bytes]:
        def sse(payload: dict[str, Any]) -> bytes:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

        yield sse({
            "type": "meta",
            "conversation_id": conversation_id,
            "knowledge_available": ctx.knowledge_available,
            "knowledge_reason": ctx.knowledge_reason,
            "has_layout": ctx.has_layout,
            "has_house_detail": ctx.has_detail,
            "has_plan": ctx.has_plan,
            "knowledge_count": len(ctx.knowledge),
        })

        parts: list[str] = []
        try:
            async for piece in stream_answer(question, ctx, history=history,
                                             prefer_local=req.prefer_local):
                # ⚠️ 顺手抹掉 `**`：界面按纯文本渲染，星号会**原样显示**出来。
                #    提示词里已经要求模型别写 Markdown，但那是要求、不是保证 ——
                #    实测第一次回答就带了一堆 `**…**`。项目里本来就有测试
                #    （test_frontend_contract）禁止界面出现 Markdown 强调，
                #    所以在这里兜一道，且**流出去的与存下来的用同一份文本**
                #    （否则用户看到的与翻历史看到的会不一样）。
                piece = piece.replace("**", "")
                parts.append(piece)
                yield sse({"type": "delta", "text": piece})
        except LLMError as e:
            logger.warning(f"[chat] 回答失败：{e}")
            yield sse({"type": "error", "message": f"模型调用失败：{str(e)[:200]}"})
        except Exception as e:  # noqa: BLE001
            logger.exception("[chat] 回答异常")
            yield sse({"type": "error", "message": f"{type(e).__name__}: {str(e)[:200]}"})

        answer = "".join(parts).strip()
        sources = ctx.citations()
        if answer:
            await repository.append_message(
                conversation_id, "assistant", answer, sources=sources,
            )
        else:
            yield sse({"type": "error", "message": "模型没有返回任何内容"})

        yield sse({"type": "sources", "sources": sources})
        yield sse({"type": "done", "conversation_id": conversation_id,
                   "chars": len(answer)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # SSE 必须禁缓存：中间层缓存住第一条事件，界面就永远不动
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",     # nginx 不缓冲（否则流式会攒成一坨）
        },
    )


@router.get("/layout/{layout_id}/house-detail", response_model=ApiResponse)
async def get_house_detail(layout_id: str) -> ApiResponse:
    """
    屋主补充的「户型详情」（文字）。

    ══════════════════════════════════════════════════════════════════
    为什么要有这个接口
    ══════════════════════════════════════════════════════════════════
    平面图是**二维**的：层高、朝向、采光面、通风路径、收纳位置这些它都没有。
    于是户型诊断只能反复写"数据不足、置信度下调" —— 实测就是这个现象。
    屋主补一份文字说明就把这些补齐了，而且这份说明比从图上推断更权威。

    它存在户型记录里（`layout["house_detail"]`），与户型同寿命（1 小时），
    也随户型一起落库 —— 不需要第二套存储。
    """
    layout = await layout_store.load(layout_id)
    if not layout:
        raise ApiError(4004, f"户型 {layout_id} 不存在或已过期（保留 1 小时）")
    return ApiResponse.ok({"layout_id": layout_id,
                           "house_detail": layout.get("house_detail")})


@router.post("/layout/{layout_id}/house-detail", response_model=ApiResponse)
async def save_house_detail(
    layout_id: str,
    req: HouseDetailRequest,
) -> ApiResponse:
    """
    保存屋主补充的户型详情。**保存后要重新跑一次诊断才有意义** ——
    见 `POST /layout/{id}/diagnose`（前端是"保存并重新诊断"一个动作）。
    """
    layout = await layout_store.load(layout_id)
    if not layout:
        raise ApiError(4004, f"户型 {layout_id} 不存在或已过期（保留 1 小时）")

    text = (req.text or "").strip()
    if not text:
        raise ApiError(4001, "户型详情不能为空 —— 要去掉这份详情请用 DELETE（本演示里留空即清空）")
    if len(text) > HOUSE_DETAIL_MAX_CHARS * 2:
        raise ApiError(
            4001,
            f"户型详情太长了（{len(text)} 字符）。请精简到 "
            f"{HOUSE_DETAIL_MAX_CHARS * 2} 字符以内 —— 超出的部分不会进诊断提示词。",
        )

    layout["house_detail"] = {
        "text": text,
        "title": (req.title or "屋主提供的户型详情").strip(),
        "source": (req.source or "user").strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # ⚠️ 走同一个 store：内存 + Redis + 落库三处一起更新，
    #    否则会出现"这次能看到、刷新就没了"。
    await layout_store.save(layout_id, layout)
    logger.info(f"[house-detail] 保存 {len(text)} 字符 layout={layout_id}")
    audit("house_detail", resource_type="layout", resource_id=layout_id,
          chars=len(text))
    return ApiResponse.ok({"layout_id": layout_id,
                           "house_detail": layout["house_detail"]})


@router.get("/layout/{layout_id}/house-detail/sample", response_model=ApiResponse)
async def sample_house_detail(layout_id: str) -> ApiResponse:
    """
    给当前户型**推荐一份演示用的户型详情**（按面积最接近的那份）。

    ⚠️ 这是**演示辅助**，不是产品能力：`seed_data/demo_house_details/` 里放着
    三份与演示户型图配套的详情文档。前端把它填进输入框，**由用户点保存** ——
    不是后端自动替他填上。区别很重要：数据是"屋主提供的"，得由人确认一次。
    """
    layout = await layout_store.load(layout_id)
    if not layout:
        raise ApiError(4004, f"户型 {layout_id} 不存在或已过期（保留 1 小时）")

    samples = _demo_house_details()
    if not samples:
        raise ApiError(4004, "本机没有演示用户型详情（seed_data/demo_house_details/ 为空）")

    area = float(layout.get("total_area") or 0)
    best = min(samples, key=lambda s: abs(s["area"] - area)) if area > 0 else samples[0]
    return ApiResponse.ok({
        "title": best["title"],
        "text": best["text"],
        "matched_by": (
            f"套内总面积最接近：本户型 {area:.1f} ㎡ / 该样例 {best['area']:.1f} ㎡"
            if area > 0 else "本地演示样例（本户型未给出面积，取第一份）"
        ),
        "note": "这是**演示样例**。真实使用时请填你自己房子的实际情况。",
    })


#: 演示详情文档的缓存（读一次就够，文件不会变）。
_DEMO_DETAIL_CACHE: list[dict[str, Any]] = []


def _demo_house_details() -> list[dict[str, Any]]:
    """
    读 `seed_data/demo_house_details/*.md`，并从正文里取"建筑面积"用于匹配。

    ⚠️ 面积是从文档里**用正则读出来的**，不是在代码里再写一份 ——
    写两份的话，改了文档忘了改代码，匹配就会静默错位。
    """
    if _DEMO_DETAIL_CACHE:
        return _DEMO_DETAIL_CACHE
    root = pathlib.Path(__file__).resolve().parents[3] / "seed_data" / "demo_house_details"
    if not root.is_dir():
        return []
    for f in sorted(root.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        m = re.search(r"建筑面积[：:]\s*\**\s*([0-9]+(?:\.[0-9]+)?)", text)
        _DEMO_DETAIL_CACHE.append({
            "title": f.stem,
            "text": text,
            "area": float(m.group(1)) if m else 0.0,
        })
    return _DEMO_DETAIL_CACHE


@router.get("/layout/{layout_id}/diagnosis", response_model=ApiResponse)
async def get_diagnosis(layout_id: str) -> ApiResponse:
    """
    户型诊断（五维）。**有存下来的就直接给，没有就现跑一次。**

    为什么要单开一条读接口，而不是只用解析结果里那份：
    屋主补了户型详情之后需要**重新诊断**，而解析任务早已结束 ——
    诊断必须能脱离那次任务独立取用（刷新页面、隔一会儿再看，都还在）。
    """
    layout = await layout_store.load(layout_id)
    if not layout:
        raise ApiError(4004, f"户型 {layout_id} 不存在或已过期（保留 1 小时）")
    diagnosis = layout.get("diagnosis")
    if not diagnosis:
        diagnosis = await _run_diagnosis(layout_id, layout)
    return ApiResponse.ok({"layout_id": layout_id, "diagnosis": diagnosis})


@router.post("/layout/{layout_id}/diagnose", response_model=ApiResponse)
async def rerun_diagnosis(layout_id: str) -> ApiResponse:
    """
    强制重跑一次五维诊断（用于补完户型详情之后）。

    ⚠️ 与首次解析时的诊断**走同一个 agent**（A-02），只是输入多了那份详情 ——
    没有任何"演示专用"的旁路，评分高是因为依据齐了，不是因为走了别的路。
    """
    layout = await layout_store.load(layout_id)
    if not layout:
        raise ApiError(4004, f"户型 {layout_id} 不存在或已过期（保留 1 小时）")
    diagnosis = await _run_diagnosis(layout_id, layout)
    return ApiResponse.ok({"layout_id": layout_id, "diagnosis": diagnosis})


async def _run_diagnosis(layout_id: str, layout: dict[str, Any]) -> dict[str, Any]:
    """
    跑一次 A-02，并把结果写回户型记录。

    两个调用方（首次取用 / 重新诊断）共用同一份实现 —— 免得两条路走出两个结果。
    """
    from ..agents.layout_diagnoser import LayoutDiagnoserAgent

    first_room = ((layout.get("rooms") or [{}])[0].get("name") if layout.get("rooms")
                  else "未命名")
    state = initial_state(task_id=f"diag-{layout_id}", layout=layout)
    try:
        out = await LayoutDiagnoserAgent().execute(state)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[diagnose] 诊断失败 layout={layout_id} 房间={first_room}")
        raise ApiError(5003, f"诊断失败：{type(e).__name__}") from e

    diagnosis = out.get("diagnosis")
    if not diagnosis:
        raise ApiError(5003, "诊断没有产出结果（agent 返回空）")

    layout["diagnosis"] = diagnosis
    await layout_store.save(layout_id, layout)
    logger.info(
        f"[diagnose] overall={diagnosis.get('overall_score')} "
        f"confidence={diagnosis.get('confidence')} "
        f"gaps={len(diagnosis.get('data_gaps') or [])} layout={layout_id}"
    )
    return diagnosis


@router.get("/layout/{layout_id}/walkable", response_model=ApiResponse)
async def layout_walkable(layout_id: str, plan_id: str = "") -> ApiResponse:
    """
    返回碰撞线段（门洞已切开）、房间净空、门连通图、出生点。

    ⚠️ **`mode` 字段决定前端走哪条路**：

        "walk"  可以做第一人称贴地行走（有碰撞、只能从门进出）
        "fly"   降级为自由视角（可穿墙飞行）

    降级的判据在 `services/geometry/walkable.py`：墙不闭合、房间站不下人、
    门过窄、有房间走不到 —— 任一不成立就走 fly。**硬做第一人称会把用户
    关在房间里**，那比功能少一点更糟。

    `issues` 里逐条写明了为什么（`ok=false` 时必定非空）—— 静默降级
    是欺骗用户（AC-17）。

    Args:
        plan_id: 给了就按**那套方案**算 3D 场景要不要等比放大（见 `scaling.py`）。
                 ⚠️ **必须传**：家具是跟着方案走的，不传的话几何按真实尺寸给、
                 家具按放大后的尺寸摆，两者会分属两个尺度 —— 家具整体偏移，
                 而画面上看起来只是"摆歪了"，很难归因。
                 两个接口拿到的是**同一个缓存值**，不会各算各的。
    """
    layout = await layout_store.load(layout_id)
    if not layout:
        raise ApiError(4004, f"户型 {layout_id} 不存在或已过期（保留 1 小时）")

    try:
        scene, plan = render_plan_for(layout)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[render] 场景归一化失败 layout_id={layout_id}")
        raise ApiError(5003, f"场景归一化失败：{type(e).__name__}") from e

    walk = build_walkable(scene)
    payload = {
        "layout_id": layout_id,
        # 3D 场景本体：直接给米制场景，前端挤出墙体用
        "scene": scene.to_dict(),
        "walkable": walk.to_dict(),
        # 画布像素坐标（2D 图与热区）。3D 用不到，但前端要在同一页
        # 同时显示平面图和 3D 时用得上 —— 不给的话它会自己算，
        # 而自己算就是"两套坐标"的开端。
        "plan_transform": plan.projection.as_dict(),
    }

    # 家具摆不下时把 3D 场景等比放大（需求方口径）。**只放大 3D** ——
    # 平面图、热区、造价仍按真实尺寸。见 `services/furniture/scaling.py`。
    k = 1.0
    if plan_id:
        k = await _scene_scale(
            layout_id, layout, plan_id, "modern",
            await _phrases_from_plan(layout_id, plan_id),
        )
    scaling.scale_walkable_payload(payload, k)
    payload["scene_scale"] = k
    payload["scene_scale_note"] = _scale_note(k)
    return ApiResponse.ok(payload)


@router.get("/layout/{layout_id}/furniture", response_model=ApiResponse)
async def layout_furniture(
    layout_id: str, style: str = "modern", plan_id: str = "",
) -> ApiResponse:
    """
    3D 场景里的家具摆放（"3D 带装修"）。

    **纯规则算，不问模型** —— 与 ADR-07（预算）、`environment.py`（环保）
    同一条纪律：坐标要站得住，就得能同时满足"在房间里 / 不堵门 /
    不压别的家具"，而这三条是可计算的。交给模型只会变成祈祷它算对了。

    Args:
        style: 方案风格，决定配色（`seed_data/furniture_catalog.json` 的三套）。
        plan_id: 给了就用**方案里说的那些家具**（`space_plan.zones[].furniture`
                 经 `catalog.match` 匹配成目录条目）；不给则按房间名从目录挑。
                 ⚠️ 给了但查不到那个方案时**不会静默退回默认**，而是 4004 ——
                 退回默认会让用户以为"3D 是按我这套方案摆的"。

    ⚠️ **摆不下的家具在 `rejected` 里，带原因**，不是静默丢弃。
       每间房最多 3 件、同类只摆一件 —— 前者是"不塞满"，后者是因为
       实测出现过客厅摆三张沙发（沙发是最大的三件，连着占满名额）。

    ⚠️ 坐标与 `/layout/{id}/walkable` **同一套场景坐标系（米）**，
       前端可直接用 `three/coords.ts` 转换，不需要第二套换算。
    """
    layout = await layout_store.load(layout_id)
    if not layout:
        raise ApiError(4004, f"户型 {layout_id} 不存在或已过期（保留 1 小时）")

    try:
        scene, _plan = render_plan_for(layout)
        walkable = build_walkable(scene)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[furniture] 场景归一化失败 layout_id={layout_id}")
        raise ApiError(5003, f"场景归一化失败：{type(e).__name__}") from e

    room_phrases = await _phrases_from_plan(layout_id, plan_id)

    from ..services.furniture import placement

    payload = {
        "scene": scene.to_dict(),
        "walkable": walkable.to_dict(),
    }
    # ⚠️ **先定放大倍数，再按放大后的房子摆家具。** 顺序反了的话，
    #    家具会按真实尺寸的房间摆，然后被画进一个更大的房子里 ——
    #    全部挤在左上角。
    k = await _scene_scale(layout_id, layout, plan_id, style, room_phrases)
    scaling.scale_walkable_payload(payload, k)

    result = placement.place_all(
        walkable=payload["walkable"], scene=payload["scene"],
        style=style, room_phrases=room_phrases,
    )
    result["layout_id"] = layout_id
    result["plan_id"] = plan_id or None
    #: 3D 场景被等比放大了多少倍。**前端必须显示它** ——
    #: 不显示的话用户会拿 3D 目测房间大小，而那是个错的数。
    result["scene_scale"] = k
    result["scene_scale_note"] = _scale_note(k)
    return ApiResponse.ok(result)


def _scale_note(k: float) -> str:
    """放大倍数的说明文案。`k == 1` 时为空 —— 没放大就没什么要说的。"""
    if abs(k - 1.0) < 1e-9:
        return ""
    return (
        f"本户型的 3D 场景已等比放大 {k:g} 倍，以容纳全部家具。"
        f"平面图、热区与造价仍按真实尺寸，未放大。"
    )


#: 场景放大倍数的缓存。键 → 倍数。
#:
#: ⚠️ **必须缓存，因为两个接口要拿到同一个值。**
#: `/walkable` 与 `/furniture` 是两次独立请求，各自算一遍摆放
#: （纯 Python，几十毫秒）不算贵，但**必须得到同一个 k** ——
#: 不一致的话墙和家具会分属两个尺度的空间，家具整体偏移。
#: 缓存同时保证了两件事：同一个 k，且只算一次。
_SCENE_SCALE_CACHE: dict[tuple[str, str, str], float] = {}


async def _scene_scale(
    layout_id: str, layout: dict, plan_id: str, style: str,
    room_phrases: dict[str, list[str]] | None,
) -> float:
    """
    3D 场景要不要等比放大、放大多少倍。

    取**阶梯里第一个"几何上全部摆得下"的倍数**。放得下就是 1.0（不放大）。

    ⚠️ 判据只看 `kind == "space"` 的拒绝 —— 「同类已有一件」与
    「占地达上限」是政策，放大房间消不掉它们（见 `scaling.py` 的说明）。
    混进来数的话，这个循环会一路加到上限，白白把房子放大 2.5 倍。

    ⚠️ 试探本身可能抛异常（目录被改坏等）。**抛了就退回 1.0 不放大** ——
    放大是为了好看，不该因为它把整个 3D 拖垮。
    """
    key = (layout_id, plan_id or "", style)
    if key in _SCENE_SCALE_CACHE:
        return _SCENE_SCALE_CACHE[key]

    from ..services.furniture import placement, scaling

    scene, _plan = render_plan_for(layout)
    base = {"scene": scene.to_dict(), "walkable": build_walkable(scene).to_dict()}

    chosen = 1.0
    try:
        for k in scaling.SCALE_LADDER:
            payload = json.loads(json.dumps(base))       # 深拷贝，别把 base 改脏
            scaling.scale_walkable_payload(payload, k)
            res = placement.place_all(
                walkable=payload["walkable"], scene=payload["scene"],
                style=style, room_phrases=room_phrases,
            )
            if scaling.space_rejections(res) == 0:
                chosen = k
                break
        else:
            chosen = scaling.MAX_SCENE_SCALE
            logger.warning(
                f"[furniture] 放到 {chosen:g} 倍仍有家具摆不下 layout_id={layout_id}"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[furniture] 放大倍数试探失败，按 1.0 处理：{type(e).__name__}: {e}")
        chosen = 1.0

    if abs(chosen - 1.0) > 1e-9:
        logger.info(f"[furniture] 3D 场景等比放大 ×{chosen:g} layout_id={layout_id}")

    _SCENE_SCALE_CACHE[key] = chosen
    # 缓存键里有 layout_id，而户型会在 Redis 里过期 —— 顺手防它无限增长
    if len(_SCENE_SCALE_CACHE) > 256:
        _SCENE_SCALE_CACHE.clear()
    return chosen


async def _phrases_from_plan(
    layout_id: str, plan_id: str,
) -> dict[str, list[str]] | None:
    """
    从库里取那套方案，把每个房间的家具短语抽出来。

    查不到就 4004 —— **不退回默认摆放**。退回去的话，用户看到的是一套
    与他选的方案无关的家具，而界面上写着那个 plan_id。
    """
    if not plan_id:
        return None
    from ..db import pool, repository

    rows = await repository.load_plan(layout_id, plan_id)
    if rows is None:
        # ⚠️ **"库不通"和"确实没有"必须分开说。**
        #
        # `load_plan` 把"查询失败"和"没查到这一行"都表示成 `None`
        # （因为 `pool.fetch_all` 的契约是"失败返回 None"）。原来那句话
        # 只好把两种可能一起列出来 —— 读起来像在推卸责任，而且**当真正的
        # 原因是第三种（SQL 写错了）时，它会把水搅浑**：实测 2026-09-24，
        # `load_plan` 的 SELECT 引用了一个不存在的列 `budget`，方案明明在库里，
        # 用户看到的却是"可能是还没生成过、或数据库暂时不可用"。
        #
        # 现在用 `pool.is_available()` 把两种情形**分开**，各说各的 ——
        # 如果真的查不到，那就是真的没有，不必再提数据库。
        if not await pool.is_available():
            raise ApiError(
                4004,
                f"数据库暂时不可用，读不到方案 {plan_id}（户型 {layout_id}）。"
                f"这不是「没有这个方案」—— 请稍后重试。"
                f"此时不会退回默认摆放，因为那会让你以为这套 3D 是"
                f"按你选的方案摆的。",
            )
        raise ApiError(
            4004,
            f"找不到方案 {plan_id}（户型 {layout_id}）—— 数据库是通的，"
            f"所以确实没有这一套（可能还没生成过，或结果已过期）。"
            f"此时不会退回默认摆放，因为那会让你以为这套 3D 是按你选的"
            f"方案摆的。",
        )
    space = rows.get("space_plan") or {}
    out: dict[str, list[str]] = {}
    for zone in space.get("zones") or []:
        name = str(zone.get("room_name") or "")
        items = [str(x) for x in (zone.get("furniture") or []) if str(x).strip()]
        if name and items:
            out.setdefault(name, []).extend(items)
    return out or None


@router.get("/layout/{layout_id}/hotspots", response_model=ApiResponse)
async def layout_hotspots(layout_id: str) -> ApiResponse:
    """
    物品热区（AC-09 的物品热区 / AC-21 的价格与链接）。

    ⚠️ 热区坐标是**画布像素**，必须和 `/plan.svg` 画出来的图配套使用。
    两个接口共用 `render_plan_for`，同一份 layout 必然得到同一套变换 ——
    这是"悬停位置和画面对得上"的全部保证。

    `precision` 一律是 `exact`：坐标是从几何**量出来**的，不是从生成图猜的。
    AI 图那条路径上的 `room_level` / `none` 见 `render/geo_check.py`。
    """
    layout = await layout_store.load(layout_id)
    if not layout:
        raise ApiError(4004, f"户型 {layout_id} 不存在或已过期（保留 1 小时）")

    try:
        scene, plan = render_plan_for(layout)
        payload = hotspot_payload(scene, plan.projection)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[render] 热区生成失败 layout_id={layout_id}")
        raise ApiError(5003, f"热区生成失败：{type(e).__name__}") from e

    return ApiResponse.ok({
        "layout_id": layout_id,
        "image": {
            "url": f"/api/v1/layout/{layout_id}/plan.svg",
            "width": plan.width_px,
            "height": plan.height_px,
        },
        "transform": plan.projection.as_dict(),
        "warnings": plan.warnings,
        **payload,
    })


# ══════════════════════════════════════════════════════════════════
# 4.6 健康检查
# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
# 4.6′ 知识库管理 + 工作台统计（2026-09-26 补齐）
# ══════════════════════════════════════════════════════════════════
#
# ⚠️ 这两组接口**此前只存在于需求文档 §12.6 的表里，代码里一条都没有** ——
#    没有占位、没有 stub、没有 501。后果是「知识库管理」页只能做成只读的
#    （页面上一向如实写着这件事，没有摆假按钮），而工作台拿不到任何
#    跨会话的真实数字。
#
# ⚠️ 知识库两条**要求管理员**。语料是全站共享的：往里面塞内容的人会影响
#    所有人的审查结论，这不是设计师该做的事 —— 与 `config/nav.ts` 里
#    「知识库管理」标着 `roles:['admin']` 是同一条理由。
#    ⚠️ 但这**不是防线**（同 nav.ts 的说明）：真正的判据在下面两处 `if`。


@router.get("/knowledge/list", response_model=ApiResponse)
async def knowledge_list(user: User = Depends(current_user)) -> ApiResponse:
    """
    知识库现状：按来源聚合的文档清单 + collection 状态。

    ⚠️ **不可用时返回 `available: false` + 原因，而不是报错。**
    与 `/system/health` 同一个立场：依赖挂了不是"接口坏了"，
    而是"这个能力现在用不了，因为 X"。前端据此显示状态，
    而不是把它渲染成一片空白（空白会被读成"知识库是空的"）。
    """
    from ..services.knowledge import chunking, store

    if user.role != auth.ROLE_ADMIN:
        raise ApiError(
            4002,
            f"查看知识库需要管理员权限。当前角色：{user.role_label}。",
            data={"role": user.role, "required_role": auth.ROLE_ADMIN},
        )

    info = store.collection_info()
    docs, reason = store.list_documents()
    return ApiResponse.ok({
        "available": info.available,
        "reason": reason,
        "collection": info.name,
        "path": info.path,
        "chunk_count": info.count,
        "document_count": len(docs),
        "documents": docs,
        # 可选类型由后端给 —— 前端硬编码一份会随语料类型变更静默漂移
        "doc_types": list(chunking.DOC_TYPES),
        "limits": {
            "max_upload_chars": chunking.MAX_UPLOAD_CHARS,
            "min_chunk_chars": chunking.MIN_CHUNK_CHARS,
        },
        "notes": [
            "文档清单按 `source` 聚合，只读元数据，不读正文。",
            "入库是幂等的：chunk id 是内容的 sha1，同一份文档再传一次是覆盖。",
        ],
    })


@router.post("/knowledge/upload", response_model=ApiResponse)
async def knowledge_upload(
    req: KnowledgeUploadRequest,
    user: User = Depends(current_user),
) -> ApiResponse:
    """
    把一段文本切块、向量化、写进知识库。

    ⚠️ **这里是同步等 embedding 的**（一次上传几十条 chunk、约几秒），
    所以有长度上限：超过 `MAX_UPLOAD_CHARS` 一律 4001，并指向离线脚本。
    没有上限的话，一次上传一本书会把事件循环占住几分钟 ——
    而 `/task/{id}/status` 的轮询是同一台机器上的（本项目在 A-06 的检索上
    量过一次同样的错：28~30 秒里轮询完全没响应）。

    ⚠️ 校验顺序：**先参数、再依赖**。反过来的话，Ollama 没起来时
    用户拿到的是"依赖不可用"，而他真正的问题是标题为空。
    """
    from ..services.knowledge import chunking, store

    if user.role != auth.ROLE_ADMIN:
        raise ApiError(
            4002,
            f"维护知识库需要管理员权限。当前角色：{user.role_label}。",
            data={"role": user.role, "required_role": auth.ROLE_ADMIN},
        )

    title = (req.title or "").strip()
    text = (req.text or "").strip()
    if not title:
        raise ApiError(4001, "标题不能为空 —— 它会作为引用里的「出处」显示。")
    if len(title) > 120:
        raise ApiError(4001, f"标题过长（{len(title)} 字），上限 120。")
    if not text:
        raise ApiError(4001, "正文不能为空。")
    if len(text) > chunking.MAX_UPLOAD_CHARS:
        raise ApiError(
            4001,
            f"正文 {len(text)} 字符，超过单次上限 "
            f"{chunking.MAX_UPLOAD_CHARS}。请拆成多篇分批上传，"
            f"或走离线入库：`python scripts/ingest_knowledge.py`。",
        )
    if req.doc_type not in chunking.DOC_TYPES:
        raise ApiError(
            4001,
            f"doc_type={req.doc_type!r} 不在允许的取值里："
            f"{list(chunking.DOC_TYPES)}。",
        )
    tags = [str(t).strip() for t in (req.tags or []) if str(t).strip()]
    if len(tags) > 12:
        raise ApiError(4001, f"标签最多 12 个，收到 {len(tags)} 个。")

    source = f"用户上传/{title}.md"
    try:
        chunks = chunking.chunk_text(
            text, source=source, doc_type=req.doc_type, tags=tags,
            source_dir="用户上传", priority="support",
        )
    except ImportError as e:
        # ⚠️ **这条分支是真踩出来的，不是防御性编程。**
        #
        # `chunking.build_splitters()` 是懒加载 —— 缺包时 ModuleNotFoundError
        # 在**请求处理中途**才抛，被全局处理器兜成 5001「服务内部错误」。
        # 界面上只说"出错了"，一个字都没提缺什么。实测就是这么发生的：
        # 容器里没装 `langchain-text-splitters`（它原来只被离线脚本用），
        # 而 2026-09-26 补的上传接口把它变成了**运行时依赖**。
        #
        # 依赖现在已经装上了，但这条分支留着：**懒加载的导入失败必须
        # 说清是哪个包** —— 否则下次再有"功能搬家、依赖没跟上"，
        # 排查又会从"服务内部错误"这四个字开始。
        raise ApiError(
            5002,
            f"切块所需的依赖没有装：{e}。"
            f"入库要用 langchain-text-splitters（见 backend/requirements.txt）。"
            f"本次没有写入任何内容。",
        ) from e
    if not chunks:
        raise ApiError(
            4001,
            f"切不出任何 chunk —— 正文太短或没有可切的结构。"
            f"单条 chunk 至少要 {chunking.MIN_CHUNK_CHARS} 字符，"
            f"当前正文 {len(text)} 字符。",
        )

    try:
        vectors = store.embed_texts([c["text"] for c in chunks])
    except Exception as e:  # noqa: BLE001
        # 不吞：embedding 依赖不可用要说清是哪一步、怎么修
        raise ApiError(
            5002,
            f"文本向量化失败（{type(e).__name__}: {e}）。"
            f"入库需要本机 Ollama 提供 embedding 服务，"
            f"检查 `ollama serve` 与 OLLAMA_EMBEDDING_MODEL="
            f"{settings.OLLAMA_EMBEDDING_MODEL}。本次没有写入任何内容。",
        ) from e

    written = store.upsert_chunks(chunks, embeddings=vectors)
    info = store.collection_info()
    logger.info(
        f"[knowledge] 入库 {source}：{written} 条 chunk，"
        f"库内共 {info.count} 条（操作者 {user.username}）"
    )
    audit("knowledge_upload", user_id=user.id, resource_type="knowledge",
          resource_id=source, chunks=written)
    return ApiResponse.ok({
        "source": source,
        "written": written,
        "chunk_count": info.count,
        "available": info.available,
        "notes": [
            "入库是幂等的：同一份内容再传一次是覆盖，不会重复。",
            f"库内共 {info.count} 条 chunk。",
        ],
    })


@router.get("/dashboard/stats", response_model=ApiResponse)
async def dashboard_stats(user: User = Depends(current_user)) -> ApiResponse:
    """
    工作台用的跨会话真实统计。

    ⚠️ **与 `/system/metrics` 是两个不同的东西，不要合并：**
      · 这个回答"攒了多少东西"—— 户型、方案、审计事件、语料条数
      · `metrics` 回答"跑得怎么样"—— 各阶段 P95、降级率、性能基线
    前者给工作台看，后者给性能报告看。混成一个接口的话，
    工作台会被迫去解析一堆它不用的百分位数。

    ⚠️ **库不通时给 `available: false` + 原因，不报错、也不给 0。**
    给 0 的话界面上会显示"0 户型"—— 而真相是"读不到"，
    两者对用户的含义完全相反（本项目三原则之一：不产出看起来合理的错误）。
    """
    from ..db import pool, repository
    from ..services.knowledge import store

    kb_info = store.collection_info()
    kb: dict[str, Any] = {
        "available": kb_info.available,
        "reason": kb_info.reason,
        "collection": kb_info.name,
        "chunks": kb_info.count,
    }
    if kb_info.available:
        docs, _reason = store.list_documents()
        kb["documents"] = len(docs)

    if not await pool.is_available():
        return ApiResponse.ok({
            "available": False,
            "reason": (
                "数据库不可用 —— 户型与方案只在内存 + Redis 里（重启即失），"
                "所以这里给不出累计数字。"
                f"检查 POSTGRES_HOST={settings.POSTGRES_HOST} 与 `docker compose ps`。"
            ),
            "layouts": None,
            "plans": None,
            "audit_events": None,
            "by_action": {},
            "kb": kb,
            "notes": ["`null` 表示读不到，不是 0 —— 两者含义相反。"],
        })

    by_action = await repository.audit_counts_by_action() or {}
    return ApiResponse.ok({
        "available": True,
        "reason": "",
        "layouts": await repository.count_rows("house_layouts"),
        "plans": await repository.count_rows("design_plans"),
        "audit_events": await repository.count_rows("audit_logs"),
        "by_action": by_action,
        "kb": kb,
        "notes": [
            "户型与方案是**累计**值，不受任务结果 1 小时过期影响。",
            "审计事件数含登录，所以它比任务数大是正常的。",
        ],
    })


@router.get("/system/health", response_model=ApiResponse)
async def system_health() -> ApiResponse:
    """
    健康检查。**每个依赖单独报告，且整体永远返回 200。**

    为什么不整体返回 503：这是一个**演示与开发用**的服务，
    知识库没起来时前端仍应能打开页面、看到"知识库不可用"的提示，
    而不是拿到一个连不上的服务。哪个依赖挂了由 `checks` 说清楚。
    """
    from ..core.redis_client import get_redis
    from ..services.knowledge import store as knowledge_store

    tm = get_task_manager()
    checks: dict[str, Any] = {}

    # ── LLM 主模型 ──
    checks["deepseek"] = {
        "ok": bool(settings.DEEPSEEK_API_KEY),
        "detail": "已配置" if settings.DEEPSEEK_API_KEY else "未配置 DEEPSEEK_API_KEY，将走本地 Ollama",
    }

    # ── Redis ──
    try:
        ok = await get_redis().ping()
        checks["redis"] = {"ok": bool(ok),
                           "detail": "可用" if ok else "不可用（进度退化为内存）"}
    except Exception as e:  # noqa: BLE001
        checks["redis"] = {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    # ── 知识库 ──
    try:
        info = knowledge_store.collection_info()
        checks["knowledge"] = {
            "ok": info.available and info.count > 0,
            "detail": (f"{info.count} 条 chunk"
                       if info.available else info.reason),
        }
    except Exception as e:  # noqa: BLE001
        checks["knowledge"] = {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    # ── 材料目录 ──
    try:
        n = len(catalog.all_products())
        checks["material_catalog"] = {"ok": n > 0, "detail": f"{n} 个商品"}
    except Exception as e:  # noqa: BLE001
        checks["material_catalog"] = {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    return ApiResponse.ok({
        "status": "ok" if all(c["ok"] for c in checks.values()) else "degraded",
        "checks": checks,
        "running_tasks": tm.list_running(),
        "version": settings.APP_VERSION if hasattr(settings, "APP_VERSION") else "dev",
    })


# ══════════════════════════════════════════════════════════════════
# 4.6′ 性能指标（AC-23）
# ══════════════════════════════════════════════════════════════════

#: 第 2.3.1 节的目标。**在这里抄一份是刻意的** —— 接口要能自己回答
#: "达标了没有"，而"达标"只有在有目标值时才成立。文档改了这里也要改，
#: 所以每一项都标了它出自哪一节。
_TARGETS = {
    "parse": {"p95_s": 20, "label": "户型解析", "doc": "2.3.1"},
    "generate": {"p95_s": 60, "label": "方案生成", "doc": "2.3.1"},
    "http": {"p95_ms": 2000, "label": "API 响应（排除 LLM）", "doc": "2.3.1"},
    "svg_render": {"p95_ms": 3000, "label": "矢量图渲染", "doc": "2.3.1"},
}


async def _overview(*, days: int = 1) -> dict[str, Any]:
    return await asyncio.to_thread(_build_overview, days)


def _build_overview(days: int) -> dict[str, Any]:
    """同步聚合。**由 `asyncio.to_thread` 调用** —— 见 metrics 模块的说明。"""
    hot = metrics.snapshot()
    sweep = metrics.sweep_audit_files(settings.LOG_DIR, days=days)

    metrics_out: dict[str, Any] = {}
    notes: list[str] = []

    # ── 长任务：解析 / 生成（来自审计文件，重启后仍在）──
    for kind, target in (("parse", _TARGETS["parse"]),
                         ("generate", _TARGETS["generate"])):
        summary = metrics.summarize(sweep.task_seconds.get(kind, []))
        if summary is None:
            metrics_out[kind] = {
                "label": target["label"],
                "available": False,
                # ⚠️ **不给 0**。"没采到"和"0 秒"是两件事，混起来就是谎。
                "reason": (f"最近 {days} 天的审计文件里没有 {kind} 任务的完成记录"
                           f"（{sweep.errors[0] if sweep.errors else '样本不足 2 条'}）"),
            }
            continue
        metrics_out[kind] = {
            "label": target["label"],
            "available": True,
            "unit": "s",
            **summary,
            "target_p95": target["p95_s"],
            "meets_target": summary["p95"] <= target["p95_s"],
            "source": "audit_file",
        }

    # ── 热路径：HTTP 响应 / 矢量图渲染（进程内，重启即清零）──
    for name, target in (("http", _TARGETS["http"]),
                         ("svg_render", _TARGETS["svg_render"])):
        summary = hot.get(name)
        if summary is None:
            metrics_out[name] = {
                "label": target["label"],
                "available": False,
                "reason": "进程内样本不足 2 条（环形缓冲重启即清零）",
            }
            continue
        metrics_out[name] = {
            "label": target["label"],
            "available": True,
            "unit": "ms",
            **summary,
            "target_p95": target["p95_ms"],
            "meets_target": summary["p95"] <= target["p95_ms"],
            "source": "in_process_ring",
        }

    # ── 各 Agent 的 LLM 调用（审计文件里的 llm_call 事件）──
    llm: dict[str, Any] = {}
    for agent, values in sorted(sweep.llm_ms.items()):
        summary = metrics.summarize(values)
        if summary is None:
            continue
        tokens = sweep.llm_tokens.get(agent, [])
        llm[agent] = {
            "unit": "ms",
            **summary,
            "avg_total_tokens": round(sum(tokens) / len(tokens)) if tokens else None,
        }

    notes.append(
        "长任务（解析/生成）与各 Agent 的耗时来自审计文件，进程重启后仍在；"
        "HTTP 与矢量图渲染来自进程内环形缓冲（上限 5000 条），重启即清零。"
    )
    notes.append(
        "第 2.3.1 节里未采集的两项：热区悬停响应（要前端 Performance API）、"
        "AI 效果图 P95（该交付物已于 V2.4 作废，见修订说明第三节）。"
    )
    if sweep.errors:
        notes.extend(sweep.errors)

    return {
        "window_days": days,
        "metrics": metrics_out,
        "llm_by_agent": llm,
        "running_tasks": get_task_manager().list_running(),
        "source": sweep.to_source(),
        "notes": notes,
    }


@router.get("/system/metrics", response_model=ApiResponse)
async def system_metrics(days: int = 1,
                         user: User = Depends(current_user)) -> ApiResponse:
    """
    性能基线（AC-23）。**只读，不改任何状态。**

    ⚠️ **这个接口要登录，`/system/health` 不要** —— 两者有意区别。

    健康检查是"服务活着吗"，给监控探针用，公开无害。而指标响应里带
    `running_tasks`（在飞任务的 id），而 AC-31 之后 **task_id 本身就是
    "能取消那个任务"的凭据**。把它挂在一个不需要登录的接口上，等于
    开了一条本地越权的路 —— 虽然本项目只绑回环（AC-37），但"因为没人
    能访问所以不用鉴权"正是那种会在换部署时反咬一口的假设。
    登录门槛很低（任何角色都行），代价与收益完全不成比例。

    ⚠️ **"没采到"与"0"是两件事。** 任何一个指标样本不足时会带
    `available: false` 与一句 `reason`，而不是给一个 0 —— 性能报告里
    一个假的 0 会被当成"快到无法测量"，而真相是"根本没数据"。

    ⚠️ 聚合是**同步文件 IO**（审计文件可能几 MB），所以走
    `asyncio.to_thread`。今天刚在 A-06 的检索上踩过同步 IO 卡住事件循环
    28 秒的坑，不想在指标接口上再踩一次。
    """
    if days < 1 or days > 30:
        # 上限 30 天：审计文件保留 180 天，全扫一遍要几秒 —— 那是接口该
        # 拒绝的量级，不是该默默承受的。
        raise ApiError(4001, f"days 取值需在 1..30 之间，收到 {days}")

    overview = await _overview(days=days)
    # ⚠️ 落库那一块**不能放进 `_build_overview`**：它是同步函数、跑在
    #    `to_thread` 里，而连库是异步的（psycopg 的异步模式）。
    #    在这里 await 之后再合并 —— 顺便也让"线程里不碰数据库"这件事
    #    保持成一个简单的事实。
    overview["persistence"] = await _persistence_block()
    return ApiResponse.ok(overview)


async def _persistence_block() -> dict[str, Any]:
    """
    落库状况。**这是"数据库到底有没有在工作"的唯一权威回答。**

    在此之前，唯一能确认审计落库的办法是手动 `psql` 数行数 ——
    而"落库失败了但没人知道"正是审计最怕的失败模式（见 db/audit_sink.py）。

    三个计数各有各的含义，都**不合并**：
      · `written`      真的写进去了
      · `failed`       尝试写但失败了（数据库不通、约束冲突……）
      · `dropped_full` 队列满被挤掉的（数据库长时间不通时的表现）
    `failed + dropped_full > 0` 就是"审计有缺口"，而不是"一切正常"。
    """
    from ..db import audit_sink, migrations, pool, repository

    available = await pool.is_available()
    block: dict[str, Any] = {
        "enabled": settings.ENABLE_DB,
        "available": available,
        "sink": audit_sink.stats(),
        "pool": pool.stats(),
    }

    if not available:
        block["available"] = False
        block["reason"] = (
            "数据库连接不可用 —— 户型与方案只进内存 + Redis（重启即失），"
            "审计事件只落文件。"
            f"检查 POSTGRES_HOST={settings.POSTGRES_HOST} 与 `docker compose ps`。"
        )
        return block

    block["migrations"] = sorted(await migrations.applied() or [])
    block["audit_rows"] = await repository.count_audit_events()
    block["note"] = (
        "`audit_rows` 是库里的累计行数；它与审计文件里的行数**不要求相等** —— "
        "文件是进程外日志、库是结构化存储，各自保留策略不同。"
    )
    return block


__all__ = ["router"]
