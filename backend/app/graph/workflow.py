"""
LangGraph 工作流编排。

═══════════════════════════════════════════════════════════════════
当前进度：解析 → 诊断 → 3 路 fan-out → fan-in 汇总
═══════════════════════════════════════════════════════════════════

    START
      │
    parse_layout        A-01 多模态解析（含质量预检与能力匹配降级）
      │  条件路由：无数据 / 降级 => 短路 END
    diagnose_layout     A-02 五维诊断
      │  条件路由：不支撑方案生成 => 短路 END
      ├──────────────┬──────────────┐        fan-out（Send）
    generate_plan  generate_plan  generate_plan   A-03 空间规划 ×3
    (现代+经济)     (北欧+中档)     (中式+高端)     同节点、并发三个实例
      └──────────────┴──────────────┘
      │                                    fan-in
    aggregate_plans     三方案汇总 + 对比表（纯代码，无 LLM）
      │
     END

分支内的其余 Agent（A-04 预算 / A-05 材料 / A-06 避坑）在此骨架上增量添加。
图像与热区节点位于 **fan-in 之后**，不在并行分支内 —— 3 路并发调图会打爆显存
（需求文档 3.3 的关键结构调整）。

═══════════════════════════════════════════════════════════════════
三个实测得来的关键约定
═══════════════════════════════════════════════════════════════════

【1】Send 的 payload 会**替换**节点看到的状态，不是合并。
    实测探针：父节点写入 parent_only / seed 后再 fan-out，分支里读到的
    两个字段**全部缺失**，只剩 payload 里的键。

    这与官方 map-reduce 示例不矛盾——那个例子的节点恰好只用 payload 里的字段，
    所以看不出差别。一旦分支需要父状态的其它字段（本例的 layout / diagnosis），
    就必须**显式塞进 payload**，否则等到 Agent 抛 KeyError 才发现。

【2】分支的**回写**走正常 reducer 通道。
    输入是 payload，但节点 return 的 dict 仍然按父图的 reducer 合并：
    plan_bundles 用 MergeDict、trace 用 operator.add。
    因此 `degraded` / `phase` 这类**没有 reducer 的标量**会被三个分支同时写，
    直接抛 `InvalidUpdateError`。已在 state.py 里改为 OrBool / LastWrite。

【3】必须用 `ainvoke` / `astream`，不能用 `invoke`。
    节点声明了 `timeout=`，同步 invoke 会抛
    `ValueError: Node timeouts are only supported for async nodes`。
    本图全程异步节点，生产路径（FastAPI）本就是 async。
"""

from __future__ import annotations

from typing import Any, Literal

from loguru import logger

from ..agents.layout_diagnoser import LayoutDiagnoserAgent
from ..agents.layout_parser import LayoutParserAgent
from ..agents.space_planner import SpacePlannerAgent
from ..core.capabilities import check_operation
from ..core.config import settings
from .state import HomeDecoState

# ══════════════════════════════════════════════════════════════════
# 节点注册表
# ══════════════════════════════════════════════════════════════════

#: 节点名 -> Agent 实例。后续每加一个 Agent 在此注册即可。
_AGENTS: dict[str, Any] = {
    "parse_layout": LayoutParserAgent(),
    "diagnose_layout": LayoutDiagnoserAgent(),
    "generate_plan": SpacePlannerAgent(),
}

NODES = Literal["parse_layout", "diagnose_layout", "generate_plan"]


def get_agent(node: str):
    """按节点名取 Agent（供测试与调试使用）。"""
    return _AGENTS[node]


# ══════════════════════════════════════════════════════════════════
# 分支规格
# ══════════════════════════════════════════════════════════════════

#: 默认三套方案。与需求文档 2.2.2 的方案 A/B/C 一一对应。
DEFAULT_BRANCH_PAIRS: list[tuple[str, str]] = [
    ("modern", "economy"),
    ("nordic", "medium"),
    ("chinese", "high"),
]

#: 分支数上限。**防的是配置手滑**——请求里写了 20 个风格就会并发 20 路，
#: 每路后续还要接 4 个 Agent。图像节点有串行锁保护，LLM 调用没有。
MAX_PLAN_BRANCHES = 4

#: 少于这个数量，对比表就没有意义（单列不成表）。
#: 此时仍返回方案本身，只是不声称"可对比"——见 aggregate_plans。
MIN_PLANS_FOR_COMPARISON = 2


def build_branch_specs(state: HomeDecoState) -> list[dict[str, Any]]:
    """
    把 styles / budget_grades 配对成若干分支规格。

    两个数组**按位置配对**：styles[0] 配 budget_grades[0]，以此类推。
    这与 API 契约 4.3 的入参形态一致，也是前端一次提交三套组合的自然表达。

    长度不等时 zip 会静默截断——这里显式告警，因为那多半是调用方写错了，
    而"少出一套方案"这种问题不告警很难被发现。
    """
    styles = [s for s in (state.get("styles") or []) if s]
    grades = [g for g in (state.get("budget_grades") or []) if g]

    if styles and grades and len(styles) != len(grades):
        logger.warning(
            f"[fan-out] styles({len(styles)}) 与 budget_grades({len(grades)}) "
            f"长度不一致，按较短的一方截断，实际出 {min(len(styles), len(grades))} 套方案"
        )

    pairs = list(zip(styles, grades)) or list(DEFAULT_BRANCH_PAIRS)

    if len(pairs) > MAX_PLAN_BRANCHES:
        logger.warning(
            f"[fan-out] 请求 {len(pairs)} 套方案，超过上限 {MAX_PLAN_BRANCHES}，"
            f"已截断。如需更多请调整 MAX_PLAN_BRANCHES 并评估并发成本"
        )
        pairs = pairs[:MAX_PLAN_BRANCHES]

    return [
        {
            "plan_id": f"plan_{style}_{grade}",
            "style": style,
            "budget_grade": grade,
            "index": i,
        }
        for i, (style, grade) in enumerate(pairs)
    ]


# ══════════════════════════════════════════════════════════════════
# Checkpointer
# ══════════════════════════════════════════════════════════════════


def _build_checkpointer():
    """
    构造 checkpointer。

    优先 RedisSaver（支持中断恢复，AC-12）；不可用时降级为内存版并告警。
    实测：RedisSaver.from_conn_string 是**上下文管理器**（返回 Iterator），
    因此这里取了底层的 __enter__ 结果并在进程存活期内保持。
    """
    if not settings.ENABLE_REDIS_CHECKPOINTER:
        logger.info("已禁用 Redis checkpointer，使用内存版")
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()

    try:
        from langgraph.checkpoint.redis import RedisSaver

        cm = RedisSaver.from_conn_string(settings.REDIS_URL)
        saver = cm.__enter__()
        saver.setup()  # 建索引，幂等
        # 保持引用防止被 GC，同时记录以便进程退出时清理
        saver._qy_cm = cm  # type: ignore[attr-defined]
        logger.info(f"Redis checkpointer 就绪: {settings.REDIS_URL}")
        return saver
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"Redis checkpointer 不可用（{type(e).__name__}: {e}），"
            f"降级为内存 checkpointer —— 进程重启后状态丢失，仅适用于开发期"
        )
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()


# ══════════════════════════════════════════════════════════════════
# 条件路由
# ══════════════════════════════════════════════════════════════════


def _route_after_parse(state: HomeDecoState) -> str:
    """
    A-01 之后的路由：有可用的解析结果才继续诊断。

    两条都不继续的路径：
    - `layout is None`：A-01 彻底失败（已在 errors 中记录）
    - `mode == "degraded_basic"`：只有房间名，没有面积/窗户/朝向。
      诊断会被 A-02 内部的能力守卫拒绝（AC-33），这里提前短路，
      省掉一次注定失败的 LLM 调用。

    注意：路由只做"要不要继续"的判断，**不在这里抛错**——
    失败信息由上游节点写入 errors，前端从那里读。
    """
    layout = state.get("layout")
    if not layout:
        logger.warning("[workflow] 解析结果为空，跳过诊断")
        return "end"
    if layout.get("mode") == "degraded_basic":
        logger.warning("[workflow] 降级解析结果无法支撑诊断，跳过（AC-33）")
        return "end"
    return "diagnose"


def _route_after_diagnosis(state: HomeDecoState) -> str | list[Any]:
    """
    A-02 之后的分支展开：能出方案就 fan-out，否则短路。

    这里**提前用 generate_plan 守卫拦一道**，而不是让 3 个分支各自去撞墙。
    generate_plan 的门槛比 diagnose 高（还要求墙体信息），所以存在
    "诊断能跑但方案不能出"的中间态——例如完整模式下模型没识别出任何墙体。

    那种情况下 3 个分支会各自抛一次 OperationNotAllowedError，
    最终得到 0 套方案 + 3 条重复错误。不如在入口拦一次，给出干净的原因。
    这与 capabilities.py 的立场一致：**在入口处拦截，不让错误数据流到下游**。

    返回值可能是字符串（节点名）或 Send 列表——LangGraph 两者都接受。
    """
    layout = state.get("layout")
    if not layout:
        return "end"

    cap = check_operation(layout, "generate_plan")
    if not cap.allowed:
        logger.warning(f"[workflow] 户型数据不支撑方案生成（{cap.reason}），跳过 fan-out")
        return "end"
    return _fan_out_plans(state)


def _fan_out_plans(state: HomeDecoState) -> list[Any]:
    """
    展开成 N 个并发的 A-03 实例。

    ⚠️ 每个 Send 的 payload 必须**自带分支所需的一切**——
    payload 会替换掉节点看到的状态（见文件头「关键约定 1」）。
    layout / diagnosis 不塞进去，A-03 里 `state.get("layout")` 就是 None。
    """
    from langgraph.types import Send

    specs = build_branch_specs(state)

    # 分支共用的只读输入。**只放分支真正会读的字段**——
    # payload 会随 checkpointer 一起序列化落盘，塞整份 state 会白白放大 3 倍。
    shared = {
        "layout": state.get("layout"),
        "diagnosis": state.get("diagnosis"),
        "requirements": state.get("requirements") or {},
        "task_id": state.get("task_id", ""),
        "trace_id": state.get("trace_id", ""),
        "detail_level": state.get("detail_level", "full"),
    }

    logger.info(
        f"[fan-out] 展开 {len(specs)} 套方案："
        f"{', '.join(s['plan_id'] for s in specs)}"
    )
    return [Send("generate_plan", {**shared, "branch_spec": spec}) for spec in specs]


# ══════════════════════════════════════════════════════════════════
# fan-in 汇总
# ══════════════════════════════════════════════════════════════════

#: 对比表要展示的维度。顺序即前端列顺序。
_COMPARISON_FIELDS: list[tuple[str, str]] = [
    ("style", "风格"),
    ("budget_grade", "预算档"),
    ("summary", "设计思路"),
    ("key_moves", "核心改动"),
    ("storage_count", "收纳处数"),
    ("circulation_fix_count", "动线优化项"),
]


def _comparison_row(plan: dict[str, Any]) -> dict[str, Any]:
    """把一个方案压成对比表的一行。"""
    return {
        "plan_id": plan.get("plan_id"),
        "style": plan.get("style"),
        "budget_grade": plan.get("budget_grade"),
        "summary": plan.get("summary", ""),
        "key_moves": plan.get("key_moves") or [],
        "storage_count": len(plan.get("storage_plans") or []),
        "circulation_fix_count": len(plan.get("circulation_fixes") or []),
        "zone_count": len(plan.get("zones") or []),
        "confidence": plan.get("confidence", 0.0),
        # 数据质量信号一并带出，便于前端标灰或提示
        "unassigned_rooms": plan.get("unassigned_rooms") or [],
        "invented_rooms": plan.get("invented_rooms") or [],
    }


def aggregate_plans(state: HomeDecoState) -> dict[str, Any]:
    """
    fan-in：把各分支写入的 plan_bundles 汇总成 plans 与对比表。

    **纯函数节点，不调用 LLM。** 对比表是结构化数据的重排，
    没有任何需要"生成"的内容；交给模型反而会引入不一致
    （例如把三套方案的风格说反）。这与预算必须由规则引擎算是同一个原则。

    三条设计要点：
    1. **分支可能失败，且不拖垮整体**。BaseAgent.execute 已保证单分支异常
       不会中断图；这里只需容忍 plan_bundles 里少于预期份数。
       "3 套里出了 2 套"是可用结果，"0 套"才是失败。
    2. **不排优劣**。没有业主的优先级信息（更看重预算还是环保？），
       任何"推荐方案"都是无依据的。只给数据质量信号，把选择权交回用户。
    3. 这是普通函数节点，异常不会被 BaseAgent 兜住，因此整体包了 try。
    """
    try:
        bundles = state.get("plan_bundles") or {}

        plans: list[dict[str, Any]] = []
        broken: list[str] = []
        for plan_id, bundle in bundles.items():
            space_plan = (bundle or {}).get("space_plan")
            if space_plan:
                plans.append(space_plan)
            else:
                broken.append(plan_id)

        # 按分支顺序排，而不是按 plan_id 字典序——
        # 字典序会得到「中式、现代、北欧」这种与请求顺序无关的排列。
        plans.sort(key=lambda p: (p.get("plan_index", 99), p.get("plan_id") or ""))

        expected = len(build_branch_specs(state))
        got = len(plans)

        notes: list[str] = []
        if got < expected:
            notes.append(
                f"请求生成 {expected} 套方案，实际产出 {got} 套"
                + (f"（未产出：{'、'.join(broken)}）" if broken else "")
                + "，可能是个别分支失败或超时"
            )

        comparison: dict[str, Any] = {
            "available": got >= MIN_PLANS_FOR_COMPARISON,
            "plan_count": got,
            "requested_count": expected,
            "fields": [{"key": k, "label": v} for k, v in _COMPARISON_FIELDS],
            "rows": [_comparison_row(p) for p in plans],
            "notes": notes,
        }

        if got < MIN_PLANS_FOR_COMPARISON:
            reason = (
                "全部方案分支均未产出结果" if got == 0
                else "仅产出 1 套方案，无法构成横向对比"
            )
            comparison["unavailable_reason"] = reason
            logger.warning(f"[fan-in] {reason}")

        if got >= 1:
            # 方案优劣取决于业主优先级（预算 vs 环保 vs 风格），
            # 系统不代为排序——这与「不编造依据」是同一条纪律。
            comparison["recommendation"] = None
            comparison["recommendation_note"] = (
                "本系统不对方案做优劣排序：哪套更合适取决于您的实际优先级"
                "（预算、居住人数、风格偏好）。请结合各方案的核心改动自行选择。"
            )
            logger.info(
                f"[fan-in] 汇总 {got} 套方案，对比表"
                f"{'可用' if comparison['available'] else '不可用'}"
            )

        result: dict[str, Any] = {
            "plans": plans,
            "comparison": comparison,
            "phase": "finalizing",
            "progress": 90,
        }

        if got == 0:
            result["degraded"] = True
            result["degrade_reasons"] = ["[fan-in] 三套方案分支均未产出可用方案"]
            result["errors"] = [{
                "agent": "fan-in",
                "type": "error",
                "message": "所有方案分支失败，fan-in 无内容可汇总",
            }]
        return result

    except Exception as e:  # noqa: BLE001 —— 汇总失败不能连带丢掉已产出的方案
        logger.exception(f"[fan-in] 汇总异常：{type(e).__name__}: {e}")
        return {
            "plans": [],
            "comparison": {
                "available": False,
                "plan_count": 0,
                "unavailable_reason": f"汇总过程出错：{type(e).__name__}",
            },
            "phase": "finalizing",
            "degraded": True,
            "degrade_reasons": [f"[fan-in] 汇总异常：{type(e).__name__}: {e}"],
            "errors": [{"agent": "fan-in", "type": "error", "message": str(e)}],
        }


# ══════════════════════════════════════════════════════════════════
# 图构建
# ══════════════════════════════════════════════════════════════════


def build_graph(*, checkpointer: Any = None, with_checkpointer: bool = True):
    """
    构建并编译工作流。

    Args:
        checkpointer: 显式传入（测试用，通常传 InMemorySaver）。
        with_checkpointer: False 时不挂载 checkpointer，纯粹跑一次无状态图。
    """
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(HomeDecoState)

    # ── 注册节点 ──────────────────────────────────────────
    for node_name, agent in _AGENTS.items():
        builder.add_node(
            node_name,
            agent.execute,
            timeout=agent.timeout,
        )

    # 汇总节点是纯函数，不走 BaseAgent —— 它没有超时熔断的必要（无 IO），
    # 内部的 try/except 已覆盖异常隔离。
    builder.add_node("aggregate_plans", aggregate_plans)

    # ── 连边 ──────────────────────────────────────────────
    builder.add_edge(START, "parse_layout")

    # 解析失败就没有可诊断的数据，直接结束而不是让 A-02 抛错。
    builder.add_conditional_edges(
        "parse_layout",
        _route_after_parse,
        {"diagnose": "diagnose_layout", "end": END},
    )

    # ⚡ fan-out 点：返回 list[Send] 时 LangGraph 并发执行三个 generate_plan
    # 实例；返回 "end" 时正常结束。两种返回类型混用是允许的。
    builder.add_conditional_edges(
        "diagnose_layout",
        _route_after_diagnosis,
        {"end": END},
    )

    # ⚡ fan-in 点：三个分支都指向 aggregate_plans，LangGraph 自动等待
    # 全部完成（或全部失败）后才执行它。
    builder.add_edge("generate_plan", "aggregate_plans")
    builder.add_edge("aggregate_plans", END)

    if not with_checkpointer:
        return builder.compile()

    cp = checkpointer if checkpointer is not None else _build_checkpointer()
    return builder.compile(checkpointer=cp)


_compiled: Any = None


def get_compiled_graph():
    """进程级单例编译图（避免每次请求重建 checkpointer 连接）。"""
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


__all__ = [
    "build_graph", "get_compiled_graph", "get_agent", "NODES",
    "build_branch_specs", "aggregate_plans",
    "DEFAULT_BRANCH_PAIRS", "MAX_PLAN_BRANCHES", "MIN_PLANS_FOR_COMPARISON",
]
