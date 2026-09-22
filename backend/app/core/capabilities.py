"""
业务连续性守卫：判断一份户型解析结果**能支撑哪些后续操作**。

═══════════════════════════════════════════════════════════════════════
为什么需要这个模块
═══════════════════════════════════════════════════════════════════════
降级模式（`mode = "degraded_basic"`）下 `total_area = 0.0`、`walls = []`。
如果没有这道守卫，用户拿着这份结果去触发方案生成会发生什么？

    calc_budget(area=0, ...)  →  返回一个 ¥0 的预算
    空间规划 Agent(无面积)     →  编出一套没有面积依据的方案

**最危险的不是报错，是"成功地"输出一个看起来正常、实际全是垃圾的结果。**
一个 ¥0 的预算比"预算无法计算"糟糕得多——用户会以为那是真的。

因此本模块的职责是：**在入口处拦截，而不是让错误数据流到下游再产生看似合理的产物。**

═══════════════════════════════════════════════════════════════════════
设计原则
═══════════════════════════════════════════════════════════════════════
1. **数据驱动，不是模式驱动**。判断依据是「字段里到底有没有可用数据」，
   而不是「mode 是不是 degraded_basic」。
   这样即使某天完整模式也返回了空面积（模型没估出来），守卫依然生效。
2. **前后端双重拦截**。前端置灰是体验，后端返回 400 是保证。
   只有前端拦截等于没有拦截——直接调 API 就绕过去了。
3. **拒绝时必须说清缺什么、怎么办**，不能只说"不允许"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: 系统支持的操作
Operation = Literal[
    "view_rooms",           # 查看房间列表
    "export_room_list",     # 导出房间名清单
    "diagnose",             # 户型诊断（采光/通风/动线）
    "generate_plan",        # 方案生成
    "estimate_budget",      # 预算估算
    "generate_image",       # 效果图生成
    "generate_hotspots",    # 物品热区
    "edit_layout",          # 局部替换 / 框选编辑
]

#: 每个操作的数据前置条件：字段路径 -> 人类可读的名称
_REQUIREMENTS: dict[str, dict[str, str]] = {
    "diagnose": {
        "rooms": "房间信息",
        "windows": "门窗位置",
        "has_area": "房间面积",
    },
    "generate_plan": {
        "rooms": "房间信息",
        "has_area": "房间面积",
        "walls": "墙体信息",
    },
    "estimate_budget": {
        "has_total_area": "套内总面积",
    },
    "generate_image": {
        "rooms": "房间信息",
        "has_bbox": "房间在图中位置",
    },
    "generate_hotspots": {
        "has_bbox": "房间在图中位置",
    },
    "edit_layout": {
        "has_bbox": "房间在图中位置",
        "has_area": "房间面积",
    },
}

#: 无法在降级模式下提供时，给用户的建议
_DEFAULT_SUGGESTION = "当前结果缺少该操作所需的数据，建议重新上传更清晰的户型图以获取完整解析。"


@dataclass
class Capability:
    """单个操作的可执行性。"""

    allowed: bool
    reason: str = ""
    missing: list[str] = field(default_factory=list)
    suggestion: str = ""


@dataclass
class CapabilityReport:
    """整份结果的可用性报告。前端据此置灰按钮，后端据此返回 400。"""

    operations: dict[str, Capability]
    mode: str
    reason: str

    def allows(self, op: str) -> bool:
        cap = self.operations.get(op)
        return bool(cap and cap.allowed)

    def require(self, op: str) -> Capability:
        """供 API 层调用：不允许时抛出携带原因的异常。"""
        cap = self.operations.get(op)
        if cap is None:
            raise UnknownOperationError(f"未知操作: {op}")
        if not cap.allowed:
            raise OperationNotAllowedError(op, cap)
        return cap

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "operations": {
                op: {
                    "allowed": c.allowed,
                    "reason": c.reason,
                    "missing": c.missing,
                    "suggestion": c.suggestion,
                }
                for op, c in self.operations.items()
            },
        }


class OperationNotAllowedError(RuntimeError):
    """操作所需数据缺失。API 层应转为 HTTP 400 并原样透出 reason。"""

    def __init__(self, operation: str, capability: Capability) -> None:
        self.operation = operation
        self.capability = capability
        super().__init__(f"操作 {operation} 不可执行：{capability.reason}")

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "reason": self.capability.reason,
            "missing": self.capability.missing,
            "suggestion": self.capability.suggestion,
        }


class UnknownOperationError(ValueError):
    """调用了未注册的操作名——属于编码错误，不是用户输入问题。"""


# ══════════════════════════════════════════════════════════════════════
# 数据探测
# ══════════════════════════════════════════════════════════════════════


def _has_rooms(layout: dict) -> bool:
    return bool(layout.get("rooms"))


def _has_area(layout: dict) -> bool:
    """至少一个房间有 > 0 的面积。"""
    return any((r.get("area") or 0) > 0 for r in layout.get("rooms") or [])


def _has_bbox(layout: dict) -> bool:
    """至少一个房间有非空的像素边界框。"""
    return any(bool(r.get("bbox")) for r in layout.get("rooms") or [])


def _effective_total_area(layout: dict) -> float:
    """
    套内总面积。

    优先用 total_area；为 0 时**回退到房间面积之和**——
    这是合理的推导，不是编造。反之若房间面积也全为 0，就真的是没有数据。
    """
    total = layout.get("total_area") or 0.0
    if total > 0:
        return float(total)
    return float(sum((r.get("area") or 0) for r in layout.get("rooms") or []))


def _has_total_area(layout: dict) -> bool:
    return _effective_total_area(layout) > 0


_PROBES = {
    "rooms": _has_rooms,
    "has_area": _has_area,
    "has_bbox": _has_bbox,
    "has_total_area": _has_total_area,
    "walls": lambda d: bool(d.get("walls")),
    "windows": lambda d: bool(d.get("windows")),
}


# ══════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════


def evaluate_capabilities(layout: dict[str, Any]) -> CapabilityReport:
    """
    评估一份户型解析结果能支撑哪些操作。

    Args:
        layout: 户型解析结果的 dict（即 API 响应里的 data 部分）

    Returns:
        CapabilityReport，可直接序列化给前端
    """
    mode = layout.get("mode", "full")
    operations: dict[str, Capability] = {}

    for op in Operation.__args__:  # type: ignore[attr-defined]
        needs = _REQUIREMENTS.get(op)

        # 无数据前置条件的操作（查看/导出）永远允许
        if not needs:
            operations[op] = Capability(allowed=True)
            continue

        missing = [
            label for key, label in needs.items()
            if not _PROBES[key](layout)
        ]

        if missing:
            operations[op] = Capability(
                allowed=False,
                reason=f"缺少{'、'.join(missing)}，无法执行该操作",
                missing=missing,
                suggestion=_DEFAULT_SUGGESTION,
            )
        else:
            operations[op] = Capability(allowed=True)

    reason = (
        "当前为降级解析结果，仅房间名称可用"
        if mode == "degraded_basic"
        else ("解析结果完整" if operations.get("generate_plan", Capability(True)).allowed
              else "解析结果不完整")
    )

    return CapabilityReport(operations=operations, mode=mode, reason=reason)


def attach_capabilities(layout: dict[str, Any]) -> dict[str, Any]:
    """
    把能力报告写入解析结果。

    Agent 层调用，结果会随 API 一起返回给前端——
    前端不需要自己推断"现在能做什么"，后端直接告诉它。
    """
    report = evaluate_capabilities(layout)
    layout["capabilities"] = report.to_dict()
    return layout


def check_operation(layout_data: dict[str, Any], operation: str) -> Capability:
    """
    检查单个操作是否可执行。**这是 API 层最常用的入口。**

    用法（FastAPI 依赖或路由内）：

        cap = check_operation(layout, "generate_plan")
        if not cap.allowed:
            raise HTTPException(400, detail={
                "code": 4002,
                "msg": cap.reason,
                "data": {"operation": "generate_plan",
                         "missing": cap.missing,
                         "suggestion": cap.suggestion},
            })

    注意：判据是**字段里有没有数据**，不是 `mode` 字符串。
    这样即使某天完整模式也返回空面积，守卫依然生效。
    """
    if operation not in _REQUIREMENTS and operation not in Operation.__args__:  # type: ignore[attr-defined]
        raise UnknownOperationError(f"未知操作: {operation}")

    report = evaluate_capabilities(layout_data)
    cap = report.operations.get(operation)
    if cap is None:
        raise UnknownOperationError(f"未知操作: {operation}")
    return cap


__all__ = [
    "Operation", "Capability", "CapabilityReport",
    "OperationNotAllowedError", "UnknownOperationError",
    "evaluate_capabilities", "attach_capabilities", "check_operation",
]
