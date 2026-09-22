"""
LangGraph 全局状态定义。

设计要点：
1. **并行分支的合并语义**由 Annotated reducer 决定。凡是在 fan-out 后
   被多个分支同时写入的字段，必须用 `Annotated[list, operator.add]`，
   否则并发写入会互相覆盖（LangGraph 会直接报 InvalidUpdateError）。
2. 状态必须可序列化 —— 因为要进 Redis checkpointer（AC-12 中断恢复）。
   因此图像以 base64 字符串存放，不放 ImagePart 对象。
3. 所有 Agent 的产出都是原生 dict（Pydantic 模型 .model_dump() 后的结果），
   便于 JSON 落库与前端直出。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict


def _merge_dict(left: dict, right: dict) -> dict:
    """dict 字段的合并 reducer：浅合并，右侧覆盖同名键。"""
    return {**(left or {}), **(right or {})}


def _or_bool(left: bool, right: bool) -> bool:
    """
    布尔字段的「或」reducer。

    fan-out 后 3 条分支会**同时**写 `degraded`。LangGraph 对没有 reducer 的
    通道只允许单次写入，并发写会抛 `InvalidUpdateError`。
    语义上「任一分支降级即整体降级」，用 OR 正好。
    """
    return bool(left) or bool(right)


def _last_write(left: str, right: str) -> str:
    """
    字符串字段的「后写覆盖」reducer。

    目前只用于 `phase`：三条并行分支都写 `"planning"`，取值相同，
    因此谁最后写不影响结果。若将来各分支写入**不同的** phase，
    这里的语义就站不住了，届时应改为按阶段顺序取最大（phase 只应前进）。
    """
    return right or left


# 会被多个并行分支写入的字段用 operator.add（列表拼接）
AddList = Annotated[list[Any], operator.add]
# 单值字段用「后写覆盖」，但需要在并行分支中显式声明才合法
MergeDict = Annotated[dict[str, Any], _merge_dict]
# 并发分支共同写入的标量
OrBool = Annotated[bool, _or_bool]
LastWrite = Annotated[str, _last_write]


class HomeDecoState(TypedDict, total=False):
    """
    全流程状态。字段按生命周期分组。

    total=False 表示所有键可选 —— 图执行过程中状态是逐步填充的。
    """

    # ══ 输入 ══════════════════════════════════════════════
    task_id: str
    user_id: int | None
    image_ref: str                    # data URI 或裸 base64
    image_media_type: str
    detail_level: Literal["basic", "full"]
    prefer_local: bool                # True = 强制本地模型，户型图不出本机（AC-24）
    requirements: dict[str, Any]      # 家庭成员、老人/儿童/宠物、智能家居、环保等级
    styles: list[str]
    budget_grades: list[str]
    quanyou_priority: bool

    # ══ A-01 户型解析 ═════════════════════════════════════
    layout: dict[str, Any] | None
    layout_id: str

    # ══ A-02 户型诊断 ═════════════════════════════════════
    diagnosis: dict[str, Any] | None

    # ══ fan-out：3 套方案（每个分支写自己的 key）═══════════
    # 每个方案的子 Agent 产出统一挂到 plan_bundles 下，键为 plan_id
    plan_bundles: MergeDict

    # 单个分支的规格，由 Send 注入 —— 分支内的 Agent 从这里读
    # 「我是哪套方案、什么风格、什么预算档」。
    # **不给它配 reducer**：它是只读的输入，若三个分支都写它会直接报
    # InvalidUpdateError。分支内的 Agent 只读不写。
    branch_spec: dict[str, Any]

    # ══ fan-in 汇总 ═══════════════════════════════════════
    plans: AddList                    # 汇总后的方案列表
    comparison: dict[str, Any] | None
    final_report: dict[str, Any] | None

    # ══ 图像与热区 ════════════════════════════════════════
    images: MergeDict                 # {plan_id: {"vector": {...}, "ai": {...}}}
    hotspots: MergeDict               # {plan_id: [...]}

    # ══ 跨切面：可观测性与降级 ════════════════════════════
    # degraded / phase 会被并行分支同时写入，必须带 reducer（见文件头说明）
    degraded: OrBool                  # 任一分支降级 => 整体降级
    degrade_reasons: AddList          # 所有降级原因，逐条累积
    errors: AddList                   # 各 Agent 的失败记录
    trace: AddList                    # 每个节点的耗时/token 记录
    progress: int                     # 0-100，供前端轮询（Redis Hash）

    # 语义化阶段。**不要只把 progress 当数字体重给前端** ——
    # 用户看到「正在提取尺寸…」比看到「60%」有用得多。
    # 取值：queued / prechecking / analyzing / detecting_rooms /
    #       extracting_dimensions / diagnosing / planning / finalizing / done / degraded
    phase: LastWrite
    trace_id: str                     # 全链路追踪 ID，贯穿 API → Agent → LLM → 审计日志


def initial_state(**overrides: Any) -> HomeDecoState:
    """构造一份填充了默认值的初始状态。"""
    base: HomeDecoState = {
        "task_id": "",
        "user_id": None,
        "image_ref": "",
        "image_media_type": "image/png",
        "detail_level": "full",
        "prefer_local": False,
        "requirements": {},
        "styles": ["modern", "nordic", "chinese"],
        "budget_grades": ["economy", "medium", "high"],
        "quanyou_priority": True,
        "layout": None,
        "layout_id": "",
        "diagnosis": None,
        "plan_bundles": {},
        "branch_spec": {},
        "plans": [],
        "comparison": None,
        "final_report": None,
        "images": {},
        "hotspots": {},
        "degraded": False,
        "degrade_reasons": [],
        "errors": [],
        "trace": [],
        "progress": 0,
        "phase": "queued",
        "trace_id": "",
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


__all__ = [
    "HomeDecoState", "initial_state",
    "AddList", "MergeDict", "OrBool", "LastWrite",
]
