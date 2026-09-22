"""
接口层的请求/响应模型。

═══════════════════════════════════════════════════════════════════
统一响应外壳：`{code, msg, data}`
═══════════════════════════════════════════════════════════════════

**业务错误一律走 HTTP 200 + code != 0**，不靠 HTTP 状态码表达业务失败。

需求文档 4.4 明确写了这条，理由很实在：前端 axios 拦截器通常按 HTTP 状态码
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
    4004,  # 任务不存在
    4009,  # 任务已存在同名
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
    quanyou_priority: bool = Field(default=True, description="是否优先推荐全友自有产品（AC-18）")
    requirements: dict[str, Any] = Field(
        default_factory=dict,
        description="业主需求：family_size / has_elderly / has_children / pets / smart_home / eco_level",
    )


class ReviewRequest(BaseModel):
    """4.6 报价单/合同审查请求。"""

    quote_text: str = Field(description="报价单或合同原文")
    requirements: dict[str, Any] = Field(default_factory=dict, description="业主需求")


__all__ = [
    "ApiResponse", "ApiError", "ErrorCode",
    "ParseRequest", "GenerateRequest", "ReviewRequest",
]
