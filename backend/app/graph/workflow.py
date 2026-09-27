"""
LangGraph 工作流编排。

═══════════════════════════════════════════════════════════════════
六个 Agent 全通：解析 → 诊断 → 3×3 并发产出 → 汇聚审查 → fan-in
═══════════════════════════════════════════════════════════════════

    START
      │
    parse_layout        A-01 多模态解析（含质量预检与能力匹配降级）
      │  条件路由：无数据 / 降级 => 短路 END
    diagnose_layout     A-02 五维诊断
      │  条件路由：不支撑方案生成 => 短路 END
      │
      │  fan-out（Send）：3 套方案 × 3 个产出者 = 9 个并发任务
      │
      ├─ plan_modern_economy ─┬─ generate_plan    A-03 空间规划
      │                       ├─ estimate_budget  A-04 预算（规则引擎）
      │                       └─ select_materials A-05 选材（代码回填价格）
      ├─ plan_nordic_medium ──┼─ 同上 ×3
      └─ plan_chinese_high ───┴─ 同上 ×3
      │
      │  汇聚：9 个产出任务全部完成后，下一个节点执行**一次**
      │
    review_risks        A-06 避坑审查（RAG + 引用溯源）★ 不是并行分支
      │
      │  fan-in：按 plan_id 汇聚（同一 plan_id 下的产物由深合并 reducer 合并）
      │
    aggregate_plans     三方案汇总 + 对比表（纯代码，无 LLM）
      │
     END

**产出者之间互相独立**：任意一个失败不影响其余两个。
fan-in 按分支规格列出方案，缺哪个产物写进 `missing_artifacts`。

**A-06 为什么不在并行分支里**：你没法审查一份还不存在的预算。
详见 `_REVIEW_NODE` 处的说明。

**加产出者只需改 `_BRANCH_PRODUCERS` 一张表**（节点名 → 产物键），
节点列表、产物列表、fan-in 收集逻辑都从它派生。

⚠️ **这里原先还写着一句「图像与热区节点位于 fan-in 之后」，那是过期描述。**
本工作流**从来没有过图像或热区节点**（`build_graph` 里只有三处 `add_node`：
Agent 注册循环、`precheck_image`、`aggregate_plans`）。出图路径已随 AC-08 作废；
矢量图与热区**不是图节点**，而是 `/layout/{id}/plan.svg` 与 `/hotspots`
两个接口按需计算的纯函数（`services/render/`，实测 0.5ms）——
**渲染不需要 LLM，没必要占一个异步节点。**

**四个入口**（见 `build_graph`）：`full` 跑整条链（e2e 脚本用）、
`parse` 到诊断为止（`/layout/parse`）、`generate` 从诊断进
（`/design/generate`，布局由调用方提供，跳过已做过的视觉解析）、
`review` 只跑「检索 → 审查」（`/avoid-pit/review`，审的是用户给的报价单，与户型无关）。

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

from ..agents.budget_agent import BudgetAgent
from ..agents.layout_diagnoser import LayoutDiagnoserAgent
from ..agents.layout_parser import LayoutParserAgent
from ..agents.material_agent import MaterialAgent
from ..agents.risk_reviewer import RiskReviewAgent
from ..agents.space_planner import SpacePlannerAgent
from ..core.capabilities import check_operation
from ..core.config import settings
from ..services.environment import assess_environment
from .state import HomeDecoState

# ══════════════════════════════════════════════════════════════════
# 分支内的 Agent（每个方案分支里各跑一份）
# ══════════════════════════════════════════════════════════════════

#: ⚡ **加分支 Agent 只需要改这一处**：节点名 -> 它在 plan_bundles 里的产物键。
#:
#: 之前节点名与产物键是两张平行的元组，加第三个 Agent 时我在 aggregate_plans
#: 里漏了一个硬编码的键，直接 KeyError。改成从一张表派生之后，
#: 节点列表、产物列表、fan-in 的收集逻辑都跟着这张表走，不会再各漏一处。
#:
#: ⚠️ 这里只放**互不依赖、可以并行**的产出者。审查者（A-06）不在其中 ——
#: 见下面的 _REVIEW_NODE 说明。
_BRANCH_PRODUCERS: dict[str, str] = {
    "generate_plan": "space_plan",
    "estimate_budget": "budget",
    "select_materials": "materials",
}

#: ⚡ **审查节点：不是产出者，是汇聚点。**
#:
#: A-06 无法与产出者并行 —— **你没法审查一份还不存在的预算**。
#: 所以它在图上是「三个产出者 → 审查 → fan-in」，而不是第四个并行分支。
#:
#: 需求文档早期画的是"4 个 Agent 并行"，那是架构草图；真实的依赖关系是
#: "审查在产出之后"。这与 ADR-08（图像节点移出并行分支）是同一类修正：
#: **并行的前提是互不依赖，而不是"看起来可以并行"。**
#:
#: 实测确认过 LangGraph 的语义：9 个分派任务全部汇聚到一个节点时，
#: 该节点只执行一次，且能看到全部合并后的写入。
_REVIEW_NODE = "review_risks"
_REVIEW_ARTIFACT = "risks"

#: 分支内的产出者节点名（Send 的目标，派生，勿单独维护）
_BRANCH_NODES: tuple[str, ...] = tuple(_BRANCH_PRODUCERS)

#: 一套方案的全部产物键：产出者的 + 审查的
BRANCH_ARTIFACTS: tuple[str, ...] = (*_BRANCH_PRODUCERS.values(), _REVIEW_ARTIFACT)

# ══════════════════════════════════════════════════════════════════
# 节点注册表
# ══════════════════════════════════════════════════════════════════

#: 节点名 -> Agent 实例。后续每加一个 Agent 在此注册即可。
_AGENTS: dict[str, Any] = {
    "parse_layout": LayoutParserAgent(),
    "diagnose_layout": LayoutDiagnoserAgent(),
    # ── 以下三个在 fan-out 分支内并发执行（产出者）──
    "generate_plan": SpacePlannerAgent(),
    "estimate_budget": BudgetAgent(),
    "select_materials": MaterialAgent(),
    # ── 审查者：在产出者全部完成之后执行（汇聚节点，非并行分支）──
    "review_risks": RiskReviewAgent(),
}

NODES = Literal[
    "parse_layout", "diagnose_layout",
    "generate_plan", "estimate_budget", "select_materials",
    "review_risks",
]


def get_agent(node: str):
    """按节点名取 Agent（供测试与调试使用）。"""
    return _AGENTS[node]


# ══════════════════════════════════════════════════════════════════
# 分支规格
# ══════════════════════════════════════════════════════════════════

#: 默认三套方案。与需求文档 11.2.2 的方案 A/B/C 一一对应。
DEFAULT_BRANCH_PAIRS: list[tuple[str, str]] = [
    ("modern", "economy"),
    ("nordic", "medium"),
    ("chinese", "high"),
]

#: 分支数上限。**防的是配置手滑**——请求里写了 20 个风格就会并发 20 路，
#: 每路后续还要接 4 个 Agent。图像节点有串行锁保护，LLM 调用没有。
#:
#: ⚠️ **这个常量就是本项目的并发上限**，任何"再给 LLM 加个全局信号量"的
#: 想法都应当先读这段。它乘以 `_BRANCH_PRODUCERS` 的 3 个产出者，
#: 最坏情况 12 路并发 LLM 调用 —— 这就是全部。默认三套是 9 路。
#:
#: 实测默认三套的 fan-out 段：21.3s，三路分别 21.3 / 20.1 / 19.4s
#: 同时段完成，**确实是真并行**。因此把并发放到 9 以下不是"更安全"，
#: 而是把并行变成排队、直接让这一段变慢一倍。
#: （`AGENT_MAX_CONCURRENCY` 那个从来没被调用过的配置已于 2026-09-24 删除，
#: 原因见 `core/config.py` 里留下的说明。）
MAX_PLAN_BRANCHES = 4

#: 少于这个数量，对比表就没有意义（单列不成表）。
#: 此时仍返回方案本身，只是不声称"可对比"——见 aggregate_plans。
MIN_PLANS_FOR_COMPARISON = 2


def build_branch_specs(state: HomeDecoState) -> list[dict[str, Any]]:
    """
    把 styles / budget_grades 配对成若干分支规格。

    两个数组**按位置配对**：styles[0] 配 budget_grades[0]，以此类推。
    这与 API 契约 12.3 的入参形态一致，也是前端一次提交三套组合的自然表达。

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


#: 启动时建好的持久化 checkpointer。**在 lifespan 里准备好**，见 `setup_checkpointer`。
#:
#: ⚠️ 为什么要"先建好再取"，而不是像以前那样在这里现建：
#: `AsyncPostgresSaver` 的构造与 `setup()` 都是 **async** 的，而这个函数是同步的
#: （`get_compiled_graph()` 在 `_run` 里同步调用）。以前那个 `RedisSaver` 分支
#: 用 `cm.__enter__()` 硬掰成同步 —— 那条路径**从未真正跑过**，
#: 所以这个取巧写法也从未被验证。
_persistent_saver: Any = None


async def setup_checkpointer() -> Any:
    """
    建好持久化 checkpointer（AC-12）。**由 lifespan 在启动时调用一次。**

    不可用时**降级为内存版并告警** —— 与项目一贯的降级纪律一致：
    状态丢失可以接受，但必须看得见。

    ⚠️ **为什么是 PostgreSQL 而不是 Redis**（口径变更，2026-09-24 实测撞墙）：
    `langgraph-checkpoint-redis` 依赖 RediSearch，启动要发 `FT.INFO`，
    而 §9.2 钉死的 `redis:7-alpine` **没有任何模块**：

        redisvl.exceptions.RedisSearchError: unknown command 'FT.INFO'

    换 `redis/redis-stack-server` 能解，但镜像明显更重，而本机 Docker VM
    的内存余量本就紧张。改用旁边的 `qy-postgres`：它一直在跑、完全空闲。
    **语义没变**（中断后状态可恢复），偏离的是"用哪个存储"。

    ⚠️ **Windows 开发注意**：`psycopg` 的异步模式不支持 Windows 默认的
    `ProactorEventLoop`，会在连接时抛
    `Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。
    所以本机开发要用 `scripts/run_server.py` 起服务（它先切 Selector 事件循环）。
    容器里是 Linux，不受影响。测试同理 —— pytest 跑在 Proactor 上，
    所以检查点相关的用例用子进程 + 显式切换（见 tests/test_checkpoint.py）。
    """
    global _persistent_saver

    if not settings.ENABLE_CHECKPOINTER:
        logger.info("已禁用持久化 checkpointer，使用内存版（重启后状态丢失）")
        return None

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        cm = AsyncPostgresSaver.from_conn_string(settings.checkpoint_dsn)
        saver = await cm.__aenter__()
        await saver.setup()          # 建表，幂等
        saver._qy_cm = cm            # 保持引用，退出时好关
        _persistent_saver = saver
        # ⚠️ 必须让已编译图的缓存失效：`get_compiled_graph` 会把**编译结果**
        #    连同当时的 checkpointer 一起缓存。若在持久化 saver 就绪之前
        #    有谁编译过一次（拿到的是内存版），那份图会被一直用下去 ——
        #    表现为"配置明明开了，检查点却没落库"，而且没有任何报错。
        reset_compiled_graphs()
        logger.info("PostgreSQL checkpointer 就绪 —— 中断后可从检查点续跑（AC-12）")
        return saver
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"PostgreSQL checkpointer 不可用（{type(e).__name__}: {e}），"
            f"降级为内存 checkpointer —— 进程重启后状态丢失，"
            f"崩溃的任务**无法续跑**，只能落成终态"
        )
        _persistent_saver = None
        return None


async def teardown_checkpointer() -> None:
    """停机时关掉连接（AC-31 的收尾）。"""
    global _persistent_saver
    cm = getattr(_persistent_saver, "_qy_cm", None)
    _persistent_saver = None
    # 同理：缓存里的图还绑着那个已经关掉的 saver，必须一起丢掉
    reset_compiled_graphs()
    if cm is not None:
        try:
            await cm.__aexit__(None, None, None)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"关闭 checkpointer 失败（忽略）：{type(e).__name__}: {e}")


def _build_checkpointer():
    """
    取 checkpointer。**优先用启动时建好的那个**，没有就退回内存版。

    内存版不是"另一个选择"，而是**降级**：进程重启后状态全丢，
    所以它只适用于开发期与测试。降级的原因在 `setup_checkpointer` 里告警过。
    """
    if _persistent_saver is not None:
        return _persistent_saver

    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


# ══════════════════════════════════════════════════════════════════
# 图片质量预检节点（AC-27）
# ══════════════════════════════════════════════════════════════════


class PrecheckError(RuntimeError):
    """
    图片未通过质量预检。

    单独一个类型是为了让上层能把 `advice`（给人看的那句话）**原样**交给用户，
    而不是套上 `PrecheckError: ` 这种类名前缀 —— 用户要读的是
    「图片分辨率过低，请换一张更清晰的」，不是异常类名。
    """

    def __init__(self, advice: str, result: Any) -> None:
        self.advice = advice
        self.result = result
        super().__init__(advice)


def retrieve_knowledge(state: HomeDecoState) -> dict[str, Any]:
    """
    审查链的第一跳：**先把知识库依据查出来**，交给 `review_risks` 用。

    ══════════════════════════════════════════════════════════════════
    为什么把这一步从审查节点里拆出来
    ══════════════════════════════════════════════════════════════════
    两个理由，都不是"为了让进度条好看"：

    1. **AC-36 在这条链上原本达不到。** 要求是"一次执行中 `phase` 至少
       3 个不同取值"，而审查链只有一个节点 —— 实测只可能看到
       `queued → reviewing` 两个。检索本来就真实发生（十几个查询、
       约几秒），拆成节点之后 `on_chain_start` 会真的在那一刻触发，
       阶段是**观测到的**而不是补上去的。

    2. **检索与调用模型是两件可分别失败的事。** 知识库挂了应该只影响
       "有没有引用依据"，而模型挂了是整条审查失败。拆开之后，
       第一件失败的**位置**在阶段就看得见（停在 `retrieving`）。

    ⚠️ **失败不抛**：检索层自己已经把"库挂了"降级成
    `available=False` + `reason`，A-06 会把它如实写进 `data_gaps`
    （"本次未取得知识库依据，风险判断主要基于常识"）。
       在这里抛出去等于把"没有引用"升级成"审查失败"，那是两回事。

    ⚠️ 结果**存成 dict**（`to_dict()`），因为它要穿过图状态、被
    checkpointer 序列化 —— 自定义类过不去。返回体里的
    `"phase"` 是给 `astream_events` 之外的一条兜底路径用的
    （状态里也留一份阶段，便于离线排查）。
    """
    from ..agents.risk_reviewer import RETRIEVAL_TOP_K, review_queries_for_quote
    from ..services.knowledge import retriever

    quote = (state.get("quote_text") or "").strip()
    if not quote:
        # 没有报价单就没有可检索的东西。不报错 —— 让 review_risks
        # 去报它那句"状态里既没有 quote_text 也没有 plan_bundles"。
        return {}

    queries = review_queries_for_quote(quote)
    result = retriever.search_many(queries, top_k_each=3,
                                   max_total=RETRIEVAL_TOP_K)
    logger.info(
        f"[graph] 检索完成：{len(queries)} 个查询 → {len(result.chunks)} 条依据"
        f"{'（知识库不可用）' if not result.available else ''}"
    )
    return {"review_sources": result.to_dict(), "phase": "retrieving"}


def precheck_image(state: HomeDecoState) -> dict[str, Any]:
    """
    解析链路的第一跳：**本地**图片质量预检。

    ══════════════════════════════════════════════════════════════════
    这个节点不是装饰 —— 它兑现两件事
    ══════════════════════════════════════════════════════════════════
    1. **AC-27**：不合格图片在进入 LLM 前被拒，Token 消耗为 0。
       不合格时直接抛错，`parse_layout` 一次都不会被调用。

    2. 让 `prechecking` 这个阶段**变成真的**。在此之前，任务启动时会
       无条件写一个 `prechecking`（"正在检查图片质量…"），而代码里
       根本没有检查动作 —— 界面在撒谎。现在它是一次真实的节点执行，
       阶段由 `_NODE_PHASE` 从节点名推导（见 api/tasks.py），
       和其它阶段一样是数据驱动的。

    纯计算，无 IO、无网络、无模型。失败也不该拖垮整条链 ——
    预检自身的异常在 `precheck_ref` 里已被兜成"放行 + 提醒"。
    """
    from ..services.image.precheck import precheck_ref

    result = precheck_ref(str(state.get("image_ref") or ""))

    if not result.ok:
        logger.warning(f"[precheck] 图片未通过预检：{result.rejections}")
        raise PrecheckError(result.advice, result)

    if result.warnings:
        logger.info(f"[precheck] 通过，但有提醒：{result.warnings}")

    return {"precheck": result.to_dict()}


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
    展开成 N 套方案 × M 个分支内 Agent 的并发任务。

    当前 N=3（三套方案）、M=2（A-03 空间规划 + A-04 预算），共 6 个并发任务。

    ⚠️ 每个 Send 的 payload 必须**自带分支所需的一切**——
    payload 会替换掉节点看到的状态（见文件头「关键约定 1」）。
    layout / diagnosis 不塞进去，分支里 `state.get("layout")` 就是 None。
    """
    from langgraph.types import Send

    specs = build_branch_specs(state)

    # 分支共用的只读输入。**只放分支真正会读的字段**——
    # payload 会随 checkpointer 一起序列化落盘，塞整份 state 会白白放大 N×M 倍。
    shared = {
        "layout": state.get("layout"),
        "diagnosis": state.get("diagnosis"),
        "requirements": state.get("requirements") or {},
        # ⚠️ AC-19：不加这两行，A-05 就永远读不到用户的材料偏好 ——
        #    这正是 `quanyou_priority` 之前变成死开关的原因：
        #    字段从 API 一路接到了 state，**却没人把它放进 Send 的 payload**，
        #    分支里 `state.get("quanyou_priority")` 恒为 None。
        #    接线漏在 fan-out 这一跳是最容易发生的，因为类型系统看不见它。
        "material_filters": state.get("material_filters") or {},
        "quanyou_priority": state.get("quanyou_priority", True),
        "task_id": state.get("task_id", ""),
        "trace_id": state.get("trace_id", ""),
        "detail_level": state.get("detail_level", "full"),
    }

    sends: list[Any] = []
    for spec in specs:
        for node in _BRANCH_NODES:
            # 每个 Send 用**独立的 dict**：共享同一个对象会在 LangGraph
            # 内部处理时产生意料之外的别名问题，而复制一份的成本可以忽略。
            sends.append(Send(node, {**shared, "branch_spec": spec}))

    logger.info(
        f"[fan-out] 展开 {len(specs)} 套方案 × {len(_BRANCH_NODES)} 个 Agent "
        f"= {len(sends)} 个并发任务：{', '.join(s['plan_id'] for s in specs)}"
    )
    return sends




# ══════════════════════════════════════════════════════════════════
# fan-in 汇总
# ══════════════════════════════════════════════════════════════════

#: 对比表要展示的维度。顺序即前端列顺序。
_COMPARISON_FIELDS: list[tuple[str, str]] = [
    ("style", "风格"),
    ("budget_grade", "预算档"),
    ("budget_total_min", "预算下限"),
    ("budget_total_max", "预算上限"),
    ("quanyou_coverage", "全友覆盖率"),
    ("summary", "设计思路"),
    ("key_moves", "核心改动"),
    ("storage_count", "收纳处数"),
    ("circulation_fix_count", "动线优化项"),
]


def _comparison_row(plan: dict[str, Any]) -> dict[str, Any]:
    """
    把一个方案包压成对比表的一行。

    两个产物都用 `or {}` 兜住 —— 分支内各 Agent 是**互相独立**的，
    完全可能一个成功一个失败。缺的那个在这里表现为该组字段为空，
    并由 `missing_artifacts` 显式标出，而不是让整行消失。
    """
    sp = plan.get("space_plan") or {}
    bg = plan.get("budget") or {}
    mt = plan.get("materials") or {}
    rk = plan.get("risks") or {}

    return {
        "plan_id": plan.get("plan_id"),
        "style": plan.get("style"),
        "budget_grade": plan.get("budget_grade"),
        # ── 来自 A-03 ──
        "summary": sp.get("summary", ""),
        "key_moves": sp.get("key_moves") or [],
        "storage_count": len(sp.get("storage_plans") or []),
        "circulation_fix_count": len(sp.get("circulation_fixes") or []),
        "zone_count": len(sp.get("zones") or []),
        "confidence": sp.get("confidence"),
        "unassigned_rooms": sp.get("unassigned_rooms") or [],
        "invented_rooms": sp.get("invented_rooms") or [],
        "duplicate_zones": sp.get("duplicate_zones") or [],
        # ── 来自 A-04 ──
        "budget_total_min": bg.get("total_min"),
        "budget_total_max": bg.get("total_max"),
        "budget_per_sqm_min": bg.get("price_per_sqm_min"),
        "budget_per_sqm_max": bg.get("price_per_sqm_max"),
        "budget_computed_by": bg.get("computed_by"),
        # ── 来自 A-05 ──
        "material_count": len(mt.get("items") or []),
        "quanyou_coverage": mt.get("quanyou_coverage"),
        "quanyou_met": mt.get("quanyou_met"),
        # ── 来自 A-06 ──
        "risk_count": rk.get("finding_count"),
        "risk_type_count": rk.get("distinct_type_count"),
        "overall_risk": rk.get("overall_risk"),
        "risk_ac06_met": rk.get("ac06_met"),
        # ── 数据完整性 ──
        "missing_artifacts": plan.get("missing_artifacts") or [],
    }


def assemble_plans(state: HomeDecoState) -> dict[str, Any]:
    """
    把各分支写入的 plan_bundles 汇总成 plans 与对比表。

    **纯函数，不调用 LLM。** 对比表是结构化数据的重排，
    没有任何需要"生成"的内容；交给模型反而会引入不一致
    （例如把三套方案的风格说反）。这与预算必须由规则引擎算是同一个原则。

    四条设计要点：
    1. **方案的身份来自分支规格，而不是"碰巧活下来的产物"。**
       分支内各 Agent 独立成败，可能只出了预算没出方案。若按产物反推计划列表，
       这种分支就会被整个丢掉，而它其实是有部分价值的。
       改为遍历 `build_branch_specs()`，顺序天然正确，也不必再排序。
    2. **缺哪个产物要显式标出**（`missing_artifacts`），而不是让字段静默为空。
       前端据此标灰，用户据此知道"这套方案的预算还没算出来"。
    3. **分支可能失败，且不拖垮整体**。BaseAgent.execute 已保证单分支异常
       不会中断图；这里只需容忍 plan_bundles 里少于预期份数。
       "3 套里出了 2 套"是可用结果，"0 套"才是失败。
    4. **不排优劣**。没有业主的优先级信息（更看重预算还是环保？），
       任何"推荐方案"都是无依据的。只给数据质量信号，把选择权交回用户。

    ⚠️ **为什么它单独成一个函数，而不是留在 `aggregate_plans` 里。**

    `aggregate_plans` 位于图的最后，**在 A-06 审查之后**（边是
    产出者 → review_risks → aggregate_plans）。而九路产出实际上早在审查
    开始前就齐了 —— 实测：三套方案 38 秒就齐，A-06 还要再跑 82 秒。

    于是用户要等满 123 秒才第一次看到方案，尽管方案已经躺在 checkpointer 里
    一分半钟。runner 现在在 `review_risks` **开始**的那一刻取一次状态快照，
    用本函数把方案先交出去（见 api/tasks.py 的 `_publish_partial`）——
    用户 38 秒就能开始读方案，风险结论随后补上。

    所以本函数必须**只有一种行为**：无论谁在什么时刻调用它，同样的状态
    给同样的结果。快照里没有 `risks`，`missing_artifacts` 就会如实列出
    `risks` —— 这正是我们要的"先给有的，并说清缺什么"。
    """
    bundles = state.get("plan_bundles") or {}
    specs = build_branch_specs(state)

    plans: list[dict[str, Any]] = []
    empty_branches: list[str] = []

    for spec in specs:
        plan_id = spec["plan_id"]
        bundle = bundles.get(plan_id) or {}

        # 按 _BRANCH_PRODUCERS 逐项取值 —— 加分支 Agent 时这里自动跟上，
        # 不会出现"新产物忘了收集"的漏洞。
        artifacts = {key: bundle.get(key) for key in BRANCH_ARTIFACTS}
        if not any(artifacts.values()):
            empty_branches.append(plan_id)
            continue

        plans.append({
            "plan_id": plan_id,
            "plan_index": spec["index"],
            "style": spec["style"],
            "budget_grade": spec["budget_grade"],
            **artifacts,
            # ⚠️ 室内环境风险（AC-20）**在这里算**，不在诊断里：
            #    A-02 诊断时还没有材料（选材在并行分支里同时跑），
            #    而这条判据的输入正是材料环保等级。放在这里还有个好处：
            #    三套方案档位不同、选材不同，风险本来就不该一样。
            #    纯规则、无 LLM —— 见 services/environment.py 的说明。
            #    ⚠️ 传的是**选材产物**（`artifacts["materials"]`），不是整个 bundle。
            #       第一版传了 `bundle`，于是它在
            #       `{"space_plan":…, "materials":{…}}` 上找 `items` 找不到，
            #       判成"没有相关材料"→ unknown。用例当场就红了 ——
            #       这类"参数传错一层"的错误不抛异常，只会安静地少一块数据。
            "environment": assess_environment(
                state.get("layout"), artifacts.get("materials")
            ),
            "missing_artifacts": [
                key for key, value in artifacts.items() if not value
            ],
        })

    expected = len(specs)
    got = len(plans)

    notes: list[str] = []
    if got < expected:
        # ⚠️ `notes` 会**上屏**（对比表顶部那段说明），所以这里不列
        #    `plan_modern_economy` 这类主键 —— 只说套数。
        notes.append(
            f"请求生成 {expected} 套方案，实际产出 {got} 套"
            + (f"（有 {len(empty_branches)} 套完全没有产出）" if empty_branches else "")
            + "，可能是个别分支失败或超时"
        )
    missing = [p["plan_id"] for p in plans if p["missing_artifacts"]]
    if missing:
        notes.append(
            f"有 {len(missing)} 套方案只产出了部分内容，"
            f"缺哪几项已在对应方案的卡片上逐条标明"
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

    return {"plans": plans, "comparison": comparison}


def aggregate_plans(state: HomeDecoState) -> dict[str, Any]:
    """
    fan-in 节点：调用 `assemble_plans`，再补上图状态的相位与降级信息。

    这是普通函数节点，异常不会被 BaseAgent 兜住，因此整体包了 try ——
    **汇总失败不能连带丢掉已经产出的方案**。
    """
    try:
        assembled = assemble_plans(state)
        got = len(assembled["plans"])
        logger.info(
            f"[fan-in] 汇总 {got} 套方案，对比表"
            f"{'可用' if assembled['comparison']['available'] else '不可用'}"
        )

        result: dict[str, Any] = {
            **assembled,
            "phase": "finalizing",
            "progress": 90,
        }

        if got == 0:
            result["degraded"] = True
            # ⚠️ 这几条都会上屏（降级提示条 + 「有 N 个环节没能完成」清单）。
            #    `[fan-in]` 是内部节点名，`str(e)` / 异常类名也只进日志。
            result["degrade_reasons"] = ["三套方案都没有产出可用的结果"]
            result["errors"] = [{
                "agent": "fan-in",
                "type": "error",
                "message": "三套方案都没有产出结果，没有可汇总的内容。"
                           "稍后重试通常能补上。",
            }]
        return result

    except Exception as e:  # noqa: BLE001 —— 汇总失败不能连带丢掉已产出的方案
        logger.exception(f"[fan-in] 汇总异常：{type(e).__name__}: {e}")
        return {
            "plans": [],
            "comparison": {
                "available": False,
                "plan_count": 0,
                "unavailable_reason": "方案汇总没能完成，请稍后重试",
            },
            "phase": "finalizing",
            "degraded": True,
            "degrade_reasons": ["方案汇总没能完成，请稍后重试"],
            "errors": [{"agent": "fan-in", "type": "error",
                        "message": "方案汇总没能完成，请稍后重试"}],
        }


# ══════════════════════════════════════════════════════════════════
# 图构建
# ══════════════════════════════════════════════════════════════════


def _route_after_diagnosis_parse_only(state: HomeDecoState) -> str:
    """
    只做解析链路时的终点：诊断完就结束。

    4.2 的 `/layout/parse` 与 4.3 的 `/design/generate` 是两个接口 ——
    前者只要「户型 + 诊断」，后者才要方案。共用一张图跑全链的话，
    一次解析会顺带触发 10 次 LLM 调用、耗掉 100 秒，**而调用方并不需要**。

    所以解析链路是完整链路的一个**前缀**：同一批节点，不同的终点。
    """
    return "end"


def build_graph(
    *,
    checkpointer: Any = None,
    with_checkpointer: bool = True,
    stages: Literal["full", "parse", "generate", "review"] = "full",
):
    """
    构建并编译工作流。

    Args:
        checkpointer: 显式传入（测试用，通常传 InMemorySaver）。
        with_checkpointer: False 时不挂载 checkpointer，纯粹跑一次无状态图。
        stages: 从哪进、到哪出。四个入口对应四个使用场景 ——

            "full"     解析 → 诊断 → fan-out → 汇聚审查 → fan-in
                       （`scripts/e2e_smoke.py` 用；一次跑完整条链）

            "parse"    解析 → 诊断，到此为止
                       （4.2 `/layout/parse`：调用方只要户型 JSON）

            "generate" 诊断 → fan-out → 汇聚审查 → fan-in
                       （4.3 `/design/generate`：**已持有 layout，跳过视觉解析**）

            "review"   只跑 review_risks 一个节点
                       （4.6 `/avoid-pit/review`：审报价单，**与户型无关**）

    **为什么要有三个入口**：解析与方案生成是两个独立 API，且代价差一个数量级 ——
    视觉解析约 16s、整条链约 110s。

    - 让 `/layout/parse` 顺带跑完整条链：白白多花 10 次 LLM 调用、多等 90 秒。
    - 让 `/design/generate` 重新解析一遍：白白再等 16 秒，而且**可能解析出
      不一样的结果**（模型有随机性）—— 用户会发现自己看到的那份户型
      和生成方案用的不是同一份。

    所以 "generate" 从 `diagnose_layout` 进：布局由调用方提供，
    跳过已经做过的那一步。"诊断"不跳过 —— 它是规划的依据，且相对便宜。
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

    # 这两个是纯函数节点，不走 BaseAgent —— 它们没有超时熔断的必要
    # （预检是本地计算，汇总是内存操作），内部的 try/except 已覆盖异常隔离。
    builder.add_node("precheck_image", precheck_image)
    builder.add_node("aggregate_plans", aggregate_plans)

    # ── 连边 ──────────────────────────────────────────────
    # 只跑审查一个节点：报价单审查**与户型无关**（用户上传的是装修公司的报价单，
    # 不是自己的房子）。硬塞进完整图的话，会因为缺 layout 而在 A-03 就炸掉。
    if stages == "review":
        # 审查链两跳：先检索（真实 IO，约几秒），再审查。
        # 见 `retrieve_knowledge` 的说明 —— 拆开是描述事实，不是为了凑阶段数。
        builder.add_node("retrieve_knowledge", retrieve_knowledge)
        builder.add_edge(START, "retrieve_knowledge")
        builder.add_edge("retrieve_knowledge", _REVIEW_NODE)
        builder.add_edge(_REVIEW_NODE, END)
        if not with_checkpointer:
            return builder.compile()
        cp = checkpointer if checkpointer is not None else _build_checkpointer()
        return builder.compile(checkpointer=cp)

    if stages == "generate":
        # 已持有 layout，跳过视觉解析这一步（见 build_graph 的 stages 说明）
        builder.add_edge(START, "diagnose_layout")
    else:
        # 解析链路的第一跳是**本地**图片预检（AC-27）。
        # 不合格的图在这里就断了，`parse_layout` 一次都不会被调用 ——
        # Token 消耗为 0。这也让 `prechecking` 阶段从"写死的假进度"
        # 变成一次真实的节点执行，见 precheck_image 的说明。
        builder.add_edge(START, "precheck_image")
        builder.add_edge("precheck_image", "parse_layout")

        # 解析失败就没有可诊断的数据，直接结束而不是让 A-02 抛错。
        builder.add_conditional_edges(
            "parse_layout",
            _route_after_parse,
            {"diagnose": "diagnose_layout", "end": END},
        )

    # 只跑解析链路：诊断完就收工（4.2 的 `/layout/parse`）
    if stages == "parse":
        builder.add_edge("diagnose_layout", END)
        if not with_checkpointer:
            return builder.compile()
        cp = checkpointer if checkpointer is not None else _build_checkpointer()
        return builder.compile(checkpointer=cp)

    # ⚡ fan-out 点：返回 list[Send] 时 LangGraph 并发执行 N×M 个分支任务；
    # 返回 "end" 时正常结束。两种返回类型混用是允许的。
    builder.add_conditional_edges(
        "diagnose_layout",
        _route_after_diagnosis,
        {"end": END},
    )

    # ⚡ 汇聚点：三个产出者的全部分派任务都指向 review_risks。
    # LangGraph 会等它们**全部**结束后，把审查节点执行**一次**
    # （实测确认：9 个任务汇聚，该节点执行 1 次且看到全部合并后的写入）。
    for node in _BRANCH_NODES:
        builder.add_edge(node, _REVIEW_NODE)

    # ⚡ fan-in 点：审查完成后再汇总。加新产出者时只需登记 _BRANCH_PRODUCERS，
    # 这里的两条边都会自动跟上。
    builder.add_edge(_REVIEW_NODE, "aggregate_plans")
    builder.add_edge("aggregate_plans", END)

    if not with_checkpointer:
        return builder.compile()

    cp = checkpointer if checkpointer is not None else _build_checkpointer()
    return builder.compile(checkpointer=cp)


_compiled: dict[str, Any] = {}


def get_compiled_graph(stages: Literal["full", "parse", "generate", "review"] = "full"):
    """
    进程级单例编译图（避免每次请求重建 checkpointer 连接）。

    ⚠️ **四种 stages 各缓存一份**（`full` / `parse` / `generate` / `review`）——
    解析接口、方案接口与报价单审查会同时存在，不能互相覆盖。

    ⚠️ 这里的标注原先只写了三种，漏了 `review` —— 而 `/avoid-pit/review`
    确实在用它（见 `api/tasks.py`）。标注漏一种不会报错，只会让类型检查
    与自动补全在那条路径上失效，**而那正是"清单在手，代码在脚"的另一种形态**。
    """
    if stages not in _compiled:
        _compiled[stages] = build_graph(stages=stages)
    return _compiled[stages]


def reset_compiled_graphs() -> None:
    """
    清掉编译图缓存。

    **给 checkpointer 留的接口**：`_build_checkpointer` 会在 Redis 不可用时
    静默降级到内存版。若 Redis 后来恢复，缓存的图仍挂着内存 checkpointer ——
    此时需要显式重建。测试里也用它来隔离用例之间的缓存。
    """
    _compiled.clear()


__all__ = [
    "build_graph", "get_compiled_graph", "reset_compiled_graphs", "get_agent", "NODES",
    "setup_checkpointer", "teardown_checkpointer",
    "build_branch_specs", "assemble_plans", "aggregate_plans", "precheck_image",
    "PrecheckError",
    "DEFAULT_BRANCH_PAIRS", "MAX_PLAN_BRANCHES", "MIN_PLANS_FOR_COMPARISON",
    "BRANCH_ARTIFACTS",
]
