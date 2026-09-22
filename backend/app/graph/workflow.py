"""
LangGraph 工作流编排。

═══════════════════════════════════════════════════════════════════
当前进度：M0 垂直切片
═══════════════════════════════════════════════════════════════════
本版本只打通「户型解析」一段（parse_layout → END），目的是先把地基跑通：

    真实户型图 → DeepSeek 多模态 → 结构化 JSON → Pydantic 校验 → 状态合并

后续节点（diagnose / 3 路 fan-out / fan-in / 图像 / 热区）在 M1-M5 逐步接上。
**先把一条链路跑通再铺开**，避免一次写 12 个节点、出问题时无从定位。

═══════════════════════════════════════════════════════════════════
设计要点（现已实现的部分）
═══════════════════════════════════════════════════════════════════
1. **Checkpointer 自动降级**：Redis 不可用时回退到内存，开发期不阻塞。
   （V2.0 文档 AC-12 要求 Redis 恢复能力，但开发机 Redis 未必常开。）
2. **节点超时**：LangGraph 1.2.x 的 add_node 原生支持 timeout 参数，
   与 BaseAgent 内部的 asyncio.wait_for 形成双重保险。
3. **所有节点都是 BaseAgent.execute**，天然具备异常隔离能力。

⚠️ **必须用 `ainvoke` / `astream` 调用本图，不能用 `invoke`。**
   实测（langgraph 1.2.11）：节点若声明了 `timeout=`，同步 `invoke()` 会直接抛
   `ValueError: Node timeouts are only supported for async nodes because sync
   Python execution cannot be safely cancelled in-process.`
   本项目的图全程异步节点，生产路径（FastAPI）本就是 async，无需同步调用。
"""

from __future__ import annotations

from typing import Any, Literal

from loguru import logger

from ..agents.layout_parser import LayoutParserAgent
from ..core.config import settings
from .state import HomeDecoState

# ══════════════════════════════════════════════════════════════════
# 节点注册表
# ══════════════════════════════════════════════════════════════════

#: 节点名 -> Agent 实例。后续每加一个 Agent 在此注册即可。
_AGENTS: dict[str, Any] = {
    "parse_layout": LayoutParserAgent(),
}

NODES = Literal["parse_layout"]


def get_agent(node: str):
    """按节点名取 Agent（供测试与调试使用）。"""
    return _AGENTS[node]


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

    # ── 连边（当前为线性单节点；fan-out/fan-in 在后续里程碑接入）──
    builder.add_edge(START, "parse_layout")
    builder.add_edge("parse_layout", END)

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


__all__ = ["build_graph", "get_compiled_graph", "get_agent", "NODES"]
