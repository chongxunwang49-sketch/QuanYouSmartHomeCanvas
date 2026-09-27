"""
接口层的请求/响应模型。

═══════════════════════════════════════════════════════════════════
统一响应外壳：`{code, msg, data}`
═══════════════════════════════════════════════════════════════════

**业务错误一律走 HTTP 200 + code != 0**，不靠 HTTP 状态码表达业务失败。

需求文档 12.4 明确写了这条，理由很实在：前端 axios 拦截器通常按 HTTP 状态码
判断请求成败，用 4xx/5xx 表达"额度用完了""户型数据不足"这类**业务**结果，
会被拦截器当成网络/服务错误弹出通用报错，而用户需要看到的是
「缺少房间面积，建议重新上传更清晰的户型图」这种**可操作**的提示。

所以约定：

    HTTP 200 + code=0     成功
    HTTP 200 + code!=0    业务失败（额度/数据不足/未找到任务…），msg 与 data 说清原因
    HTTP 4xx/5xx          真正的协议/服务错误（参数格式错、未捕获异常）

`ApiError` 用异常抛出、由全局处理器转成 200+code，好处是**业务代码里可以直接
raise 而不用层层返回错误**，且不会漏掉某条分支忘了转格式。
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一响应外壳。"""

    code: int = Field(default=0, description="0 表示成功；非 0 为业务错误码")
    msg: str = Field(default="ok", description="面向用户的说明，前端可直接展示")
    data: T | None = Field(default=None, description="成功时的业务数据")

    @classmethod
    def ok(cls, data: Any = None, msg: str = "ok") -> "ApiResponse":
        return cls(code=0, msg=msg, data=data)

    @classmethod
    def fail(cls, code: int, msg: str, data: Any = None) -> "ApiResponse":
        return cls(code=code, msg=msg, data=data)


#: 业务错误码。与 HTTP 状态码无关，见模块说明。
ErrorCode = Literal[
    4001,  # 参数不合法
    4002,  # 数据不支撑该操作（对应 OperationNotAllowedError，AC-33）
    4003,  # 未登录 / 令牌无效或已过期（AC-01）
    4004,  # 任务不存在
    4009,  # 任务已存在同名
    4005,  # 权限不足 / 需要开通会员（AC-01 + AC-13）
    4006,  # 超出每日额度（AC-13）
    4007,  # 账号已被管理员停用（口令正确时才返回，见 auth.is_disabled_account）
    5001,  # 执行失败
    5002,  # 依赖不可用（Redis / 模型 / 知识库）
]


class ApiError(Exception):
    """
    业务错误。**API 层主动抛它，而不是返回错误响应**。

    由 `main.py` 的全局处理器统一转成 HTTP 200 + 非 0 code（见模块说明）。
    """

    def __init__(self, code: int, msg: str, *, data: Any = None, http_status: int = 200) -> None:
        self.code = code
        self.msg = msg
        self.data = data
        #: 少数情况确实该用非 200（例如路由未匹配）。默认 200，见模块说明。
        self.http_status = http_status
        super().__init__(msg)


# ══════════════════════════════════════════════════════════════════
# 请求体
# ══════════════════════════════════════════════════════════════════


class ParseRequest(BaseModel):
    """4.2 户型解析请求。"""

    image: str = Field(
        description="户型图的 data URI（`data:image/png;base64,...`）或裸 base64 字符串",
    )
    image_media_type: str = Field(default="image/png", description="图片 MIME 类型")
    detail_level: Literal["basic", "full"] = Field(
        default="full", description="解析详细程度。basic 只出房间名，full 出完整结构"
    )
    prefer_local: bool = Field(
        default=False,
        description="隐私模式：True 时图像**绝不出本机**（ADR-12，在提供方选择层强制）",
    )


class GenerateRequest(BaseModel):
    """4.3 方案生成请求。"""

    layout_id: str = Field(description="户型解析返回的 layout_id")
    styles: list[str] = Field(
        default_factory=lambda: ["modern", "nordic", "chinese"],
        description="风格列表，与 budget_grades **按位置配对**",
    )
    budget_grades: list[str] = Field(
        default_factory=lambda: ["economy", "medium", "high"],
        description="预算档位列表，与 styles 按位置配对",
    )
    quanyou_priority: bool = Field(
        default=True,
        description=(
            "同等条件下是否优先推荐全友自有产品（软偏好）。"
            "⚠️ AC-18 的 60% 覆盖率保底是平台级要求，**不受此开关影响**"
        ),
    )
    requirements: dict[str, Any] = Field(
        default_factory=dict,
        description="业主需求：family_size / has_elderly / has_children / pets / smart_home / eco_level",
    )
    # ── AC-19 材料偏好 ────────────────────────────────────────
    # 语义是「偏好与排除」，不是「逐件挑选」：AC-18 的覆盖率是系统的
    # 验收指标，让用户直接把竞品挑满会让它变成一句空话。
    # 详见 services/material/filters.py 的模块说明。
    excluded_categories: list[str] = Field(
        default_factory=list,
        description="不想要的品类 key（见 GET /material/price 返回的 categories）",
    )
    excluded_brands: list[str] = Field(
        default_factory=list, description="不想要的品牌。不能排除「全友」（与 AC-18 冲突，会被 4001 拒绝）",
    )
    preferred_brands: list[str] = Field(
        default_factory=list, description="同等条件下优先的品牌，加分低于平台的全友优先",
    )


class ReviewRequest(BaseModel):
    """4.6 报价单/合同审查请求。"""

    quote_text: str = Field(description="报价单或合同原文")
    requirements: dict[str, Any] = Field(default_factory=dict, description="业主需求")


class KnowledgeUploadRequest(BaseModel):
    """
    4.6 知识库入库请求（2026-09-26 补齐 —— 此前只有文档里写着）。

    ⚠️ **收文本，不收文件。** 这一步是有意的取舍：
      · 收文件要处理 multipart、大小上限、编码嗅探、以及"传上来的
        到底是 md 还是伪装成 md 的二进制" —— 而这些与业务无关。
      · 而知识库的语料本来就是**纯文本 md**（见 `references_manifest.yaml`）。
        让浏览器把文件读成文本再提交，前端一行 `FileReader` 就够了。
    上限由 `MAX_UPLOAD_CHARS` 把关，超了 4001 并说明原因。
    """

    title: str = Field(description="文档标题，作为 `source` 显示在引用里")
    text: str = Field(description="文档正文（markdown 纯文本）")
    doc_type: str = Field(
        default="avoid_pit",
        description="语料类型，取值见 `services/knowledge/chunking.DOC_TYPES`",
    )
    tags: list[str] = Field(default_factory=list, description="标签，用于检索过滤")


class FloorMaterialRequest(BaseModel):
    """
    4.3‴ 换一间房的地面材料（AC-10）。

    `material_id` 传空串 = **还原**成默认房型配色。用空串而不是
    `null` 或一个 `DELETE`：还原和替换是同一个动作的两种取值，
    分成两个接口会让前端在两处各写一遍房间号的校验。
    """

    room_index: int = Field(description="房间下标（与地面热区的 room_index 同一个）")
    material_id: str = Field(
        default="",
        description="材料目录里的 product id；空串表示还原成默认配色",
    )


__all__ = [
    "ApiResponse", "ApiError", "ErrorCode",
    "ParseRequest", "GenerateRequest", "ReviewRequest",
    "KnowledgeUploadRequest",
    "FloorMaterialRequest",
]


class LoginRequest(BaseModel):
    """4.1 登录。"""

    username: str = Field(description="用户名（大小写不敏感）")
    password: str = Field(description="口令")


class MembershipRequest(BaseModel):
    """自助改档位（演示用：普通用户"开通会员"）。"""

    membership: str = Field(description="free 或 paid")


class UserUpdateRequest(BaseModel):
    """
    管理员改**别人**的角色 / 档位 / 启用状态。

    三个字段都可选，只改传了的那个。**取值校验不在这里做** —— 允许的取值
    由 `core/auth.py` 的 `ROLES` / `MEMBERSHIPS` 定义，在那里校验才能保证
    只有一处真源（`set_user_role` 会抛 `ValueError`，API 层转 4001）。

    ⚠️ `role` 允许传，但**不允许传 `admin`** —— 这条策略在路由层拦
    （「管理页不做管理员权限分配」是权限策略，不是领域约束）。
    """

    role: str | None = Field(default=None, description="designer / user（**不接受 admin**）")
    membership: str | None = Field(default=None, description="free / paid")
    is_active: bool | None = Field(default=None, description="false = 封禁（该账号将无法登录）")


__all__ += ["LoginRequest", "MembershipRequest", "UserUpdateRequest",
            "HouseDetailRequest"]  # type: ignore[name-defined]


class HouseDetailRequest(BaseModel):
    """
    屋主补充的「户型详情」（文字）。4.3′ 户型诊断的输入补全。

    ⚠️ **与平面图是两类东西，别混。** 平面图是图（走 `/layout/parse`，
    多模态解析）；这里是**文字资料**（层高、朝向、采光面、通风路径、收纳、
    设备、特殊说明），它补的正是二维平面图上读不出来的那部分 ——
    诊断一直"数据不足"就是因为缺这些。

    前端两个上传模块在文案上必须能一眼分清楚，否则用户会把户型图传到这一栏。
    """

    text: str = Field(description="户型详情正文（纯文本 / Markdown）")
    title: str = Field(default="", description="这份详情的名字，界面上与来源一起显示")
    source: str = Field(default="user", description="来源标记：user / demo / imported")
