"""
异步任务管理 —— 创建任务、驱动图执行、写进度、供轮询。

═══════════════════════════════════════════════════════════════════
为什么是异步任务而不是同步请求
═══════════════════════════════════════════════════════════════════
整条链 110 秒（解析接口约 40s，生成接口约 95s）。同步 HTTP 请求挺不过
任何一层超时：浏览器默认 30s、Nginx 默认 60s、uvicorn 默认无限但客户端会放弃。

所以接口一律**立即返回 task_id**，图在后台跑，前端按 2.2.4 的节奏轮询。

═══════════════════════════════════════════════════════════════════
进度从哪来：`astream` 的节点事件，而不是 Agent 自己写
═══════════════════════════════════════════════════════════════════
用 `graph.astream(state, stream_mode="updates")` —— 每完成一个节点产出一片，
键是节点名。**节点名 → phase 的映射表在 runner 里**，Agent 不需要关心
"我该把自己标记成什么阶段"。

这解决了一个真实问题：Agent 是在**返回时**写 phase 的，那时它已经跑完了，
写出来的值天然滞后；而且每个 Agent 各写各的，很容易写出枚举外的值
（实测踩过：`parsed` / `diagnosed` / `reviewed` 三个都不在 `Phase` 里，
前端会看到英文原文）。

═══════════════════════════════════════════════════════════════════
内存 + Redis 双写
═══════════════════════════════════════════════════════════════════
进度**同时**写内存与 Redis，读取时优先 Redis、回退内存。

理由是 Redis 在本项目里是**可选依赖**（`redis_client.py` 的设计是
"Redis 不可用时静默降级，不阻塞主流程"）。但任务状态是**主流程本身** ——
Redis 挂了就查不到任务，接口等于废了。只写内存的话，进程重启任务记录就没了；
只写 Redis 的话，开发机没开 Redis 时接口完全不可用。两边都写，两边都能顶。

═══════════════════════════════════════════════════════════════════
"已用时间 / 剩余时间"只在 `status()` 里算一次
═══════════════════════════════════════════════════════════════════
进度、已用时间、剩余时间**不是存下来的数**，而是 `(kind, phase, 时间)`
的推导结果（模型见 `core/progress.py`）。推导放在 `status()` ——
两条读取路径都经过它，因此不可能出现"Redis 在不在决定了界面显示什么"。

存进 Redis 的只有**事实**：任务类型、当前阶段、开始时间、进入本阶段的时间。

═══════════════════════════════════════════════════════════════════
方案先交出去，风险结论后补（`_publish_partial`）
═══════════════════════════════════════════════════════════════════
实测方案生成链 122.9 秒，其中 **A-06 汇聚审查占 82.4 秒**（67%），
而三套方案在 38 秒时就全部产出了 —— 之后的一分半钟，方案其实一直躺在
checkpointer 里，用户却只能盯着进度条。

所以 runner 在 `review_risks` **开始**的那一刻取一次状态快照，用
`assemble_plans` 把方案先写进结果（`partial=True`，`missing_artifacts`
如实列出 `risks`），审查跑完后由正常路径覆盖成完整结果。

⚠️ 这不是"先给个假结果"：缺失项是显式标出来的，界面也明说风险复核还在进行。
   用户因此可以在第 38 秒开始读方案，而不是第 123 秒。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from loguru import logger

from ..core.logger import audit, with_trace_id
from ..core.progress import build_view, progress_at
from ..core.redis_client import TaskProgressStore, get_redis
from ..graph.state import HomeDecoState, initial_state
from ..graph.workflow import PrecheckError, assemble_plans, get_compiled_graph

TaskKind = Literal["parse", "generate", "review"]
TaskStatus = Literal["pending", "processing", "completed", "failed"]

#: 用户主动中断时的失败原因。**这不是"出错"** —— 是用户自己的选择。
#:
#: ⚠️ 状态仍然落在 `failed` 上，因为 `status` 是**控制流通道**
#: （前端的轮询循环靠"completed / failed 就停"来判断该不该继续），
#: 而 `cancelled` 标志是**措辞通道**。两者分开之后，界面可以说"已中断"
#: 而不是"失败"，同时又不必给状态机加一个新状态。
CANCEL_REASON_USER = "任务已被用户中断"

#: 停机时到点仍未跑完的，用这句话记账（AC-31 的"超期处理"）。
CANCEL_REASON_SHUTDOWN = "服务停机，任务未在上限内完成"

#: 停机排空上限（秒）。见 `TaskManager.shutdown` 里那段推导。
SHUTDOWN_DRAIN_SECONDS = 150.0

#: 取消接口等待协程退出的上限（秒）。**它不决定任务能不能停下**，
#: 只决定接口等多久就返回 —— 返回前 `_finalize` 一定会把终态写上，
#: 所以这里短一点没关系，短了反而不会把接口本身挂住。
CANCEL_JOIN_SECONDS = 5.0


class TaskNotFound(LookupError):
    """任务不存在或已过期。API 层转 4004。"""


class TaskNotOwned(PermissionError):
    """
    不是这个任务的主人。API 层转 4005。

    ⚠️ **必须有这道检查。** 上线登录之后，task_id 是接口里唯一指向别人
    在飞任务的凭据；没有 owner 校验的话，任何人拿到（或猜到）一个
    task_id 就能取消别人的解析。实测的 task_id 形如
    `task_20260924_1a2b3c4d` —— 日期 + 8 位随机，不算不可猜。
    """

#: 节点名 → 语义化阶段。**这张表由 runner 拥有**，见模块说明。
#:
#: ⚠️ 表里的每一项都必须对应一个**真实存在的节点**。
#: 之前 `prechecking` 不在这张表里 —— 它是任务启动时无条件写死的一个阶段，
#: 而"检查图片质量"这件事根本没发生（界面在撒谎）。现在它对应
#: `precheck_image` 节点的真实执行，见 workflow.precheck_image。
_NODE_PHASE: dict[str, str] = {
    "precheck_image": "prechecking",
    "parse_layout": "analyzing",
    "diagnose_layout": "diagnosing",
    "generate_plan": "planning",
    "estimate_budget": "planning",
    "select_materials": "planning",
    "retrieve_knowledge": "retrieving",
    "review_risks": "planning",
    "aggregate_plans": "finalizing",
}

#: `review_risks` 节点的阶段**按任务类型分开**。表里给的是兜底值。
#:
#: ⚠️ 方案链里它**不能**继续叫 `planning`。这是 2026-09-24 改的，起因是
#: 用户反馈"进度条不说明问题，我只看到已等待时间"。查下去发现：
#: 方案链上有两段完全不同的工作共用 `planning` 这一个阶段名 ——
#: 九路并发出方案（21 秒）和 A-06 复核风险（82 秒）。
#: 合并的后果是进度模型没法给它们不同的权重，那 82 秒在条子上无处安放。
#:
#: 拆开之后它们各自成立：出方案 → 复核风险，用户也看得见"方案已经出来了，
#: 正在复核"这个真实发生过的转折（见 `_publish_partial`）。
_REVIEW_PHASE: dict[str, str] = {
    "review": "reviewing",
    "generate": "checking_risks",
}


def _phase_for(node: str, kind: TaskKind) -> str | None:
    """
    节点名 → 阶段。**审查任务的措辞要单独处理。**

    同一个 `review_risks` 节点在两种语境下含义不同：
      · `kind="review"` —— 用户提交的是一份报价单，该说「正在审查报价单…」
      · `kind="generate"` —— 它在方案链里跑，说「正在复核方案风险…」，
        而且此刻三套方案**已经产出**（见 _publish_partial）

    实测踩过：走报价单审查时，界面全程显示「正在生成装修方案…」。
    用户看的是一份合同，却被告知系统在生成方案 —— 这正是需求文档 11.2.4
    最在意的那件事（「用户看到的是正在做什么」，而不是进程名）。

    所以映射不能只看节点名，得带上任务类型。
    """
    if node == "review_risks":
        return _REVIEW_PHASE.get(kind) or _NODE_PHASE["review_risks"]
    return _NODE_PHASE.get(node)

#: 结果保留秒数。1 小时支持"刷新页面后重新获取"（4.4）。
RESULT_TTL = 3600


def _new_task_id(kind: TaskKind) -> str:
    """`task_20260922_1a2b3c4d` —— 日期便于人肉排查，随机段避免碰撞。"""
    return f"task_{datetime.now():%Y%m%d}_{uuid.uuid4().hex[:8]}"


@dataclass
class TaskRecord:
    """
    一个后台任务的运行期状态。

    ⚠️ **时间是 epoch 秒，不是单调时钟。**
    `asyncio.get_event_loop().time()` 那个单调时钟更适合量耗时（不受系统
    改时间影响），但它**跨不了进程** —— 任务状态要落到 Redis 里，好让
    进程重启后仍能查到（4.4 要求结果保留 1 小时），而重启后的新进程
    没法跟旧进程的单调时钟对齐。剩余时间要么在两条路径上给出同一个数，
    要么就不该给。所以统一用挂钟时间。
    """

    task_id: str
    kind: TaskKind
    trace_id: str
    #: 发起人。取消接口靠它做归属校验（见 TaskNotOwned）。
    #: None = 不知道主人（登录上线之前的记录）—— 那种任务**不允许被取消**，
    #: 宁可少一个功能，也不给"谁都能取消"留口子。
    user_id: int | None = None
    status: TaskStatus = "pending"
    phase: str = "queued"
    #: **进入**当前阶段时的百分比。不是"此刻的百分比" ——
    #: 此刻的那个由 `to_dict` 按时间推出来（见模块说明）。
    progress: int = 0
    degraded: bool = False
    #: 是不是被用户主动中断的。`status` 仍是 failed（控制流通道），
    #: 这个标志只用来决定界面措辞（见 CANCEL_REASON_USER）。
    cancelled: bool = False
    error: str = ""
    result: dict[str, Any] | None = None
    started_at: float = field(default_factory=time.time)
    phase_entered_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    handle: asyncio.Task | None = None

    def is_running(self) -> bool:
        return self.handle is not None and not self.handle.done()

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        """给轮询接口的视图。字段与 4.4 的响应对齐。"""
        from ..core.redis_client import PHASE_TEXT

        view = build_view(
            kind=self.kind, phase=self.phase, status=self.status,
            started_at=self.started_at, phase_entered_at=self.phase_entered_at,
            fallback_progress=self.progress, now=now,
        )
        return {
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "kind": self.kind,
            "status": self.status,
            "phase": self.phase,
            # 前端直接展示这个字段，不必自建映射表（2.2.4）
            "phase_text": PHASE_TEXT.get(self.phase, self.phase),
            **view,
            "degraded": self.degraded,
            "cancelled": self.cancelled,
            "error": self.error or None,
            "result": self.result,
        }


class TaskManager:
    """任务注册表 + 图执行驱动。"""

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}
        self._shutting_down = False

    # ── 生命周期 ──────────────────────────────────────────

    def create(self, kind: TaskKind, trace_id: str = "",
               *, user_id: int | None = None) -> TaskRecord:
        if self._shutting_down:
            raise RuntimeError("服务正在停机，不再接收新任务")

        rec = TaskRecord(
            task_id=_new_task_id(kind),
            kind=kind,
            user_id=user_id,
            trace_id=trace_id or uuid.uuid4().hex,
        )
        self._records[rec.task_id] = rec
        logger.info(
            f"[task] 创建 {rec.task_id}（{kind}）trace_id={rec.trace_id[:8]}"
            + (f" user={user_id}" if user_id is not None else "")
        )
        # 审计：AC-14 点名的"解析、生成"就是这两类任务。记在 create 里
        # 而不是三个路由里 —— 加第四个异步接口时不会漏。
        audit("task_created", task_id=rec.task_id, kind=kind,
              user_id=user_id, trace_id=rec.trace_id)
        return rec

    def start(self, rec: TaskRecord, state: HomeDecoState, *, stages: str) -> None:
        """
        把图丢到后台跑。

        用 `asyncio.create_task` 而不是 FastAPI 的 `BackgroundTasks`：
        后者在**响应发出后**才执行，且无法在停机时取消 ——
        而 ADR-13 要求优雅停机时能取消在飞的任务。
        """
        rec.status = "processing"
        rec.handle = asyncio.create_task(self._run(rec, state, stages=stages))
        rec.handle.add_done_callback(lambda t: self._on_done(rec, t))

    def _on_done(self, rec: TaskRecord, handle: asyncio.Task) -> None:
        """
        兜住 runner 自身没接住的异常。

        没有这个回调的话，`asyncio.create_task` 里的异常只会在 GC 时
        以 "Task exception was never retrieved" 的形式出现 ——
        任务会永远停在 processing，前端一直轮询到超时也等不到结果。

        ⚠️ **已经落过终态的一律不动。** 这个回调在"任务被取消"时**也会触发**，
        而取消的两种来路（用户按中断 / 停机超期）都已经由 `_finalize`
        写好了措辞更有信息量的原因。这里再覆盖一次有两个后果：
          · 把"用户中断"或"停机超期未完成"改成笼统的"任务被取消"，信息更少；
          · 本回调**不写 Redis**，而 `_finalize` 写了 ——
            内存与 Redis 的 `error` 当场就不一致，界面显示哪一句
            取决于 Redis 在不在。这正是本项目一直在防的那类问题。
        """
        if rec.status in ("completed", "failed"):
            return

        if handle.cancelled():
            reason = CANCEL_REASON_SHUTDOWN
        else:
            exc = handle.exception()
            if exc is None:
                return
            reason = f"{type(exc).__name__}: {exc}"
            logger.exception(f"[task] {rec.task_id} 未捕获异常: {exc}")

        rec.status = "failed"
        rec.error = reason
        # ⚠️ 内存改了还不够，**Redis 也要落**。这一个回调是"runner 没接住"
         #    的最后一道兜底，而它原来只改内存 —— 于是这类任务在轮询接口
         #    上永远停在 processing（读的是 Redis 那份）。同步回调里发不了
         #    `await`，所以排一个任务出去；排不出去（事件循环正在关）就
         #    只留内存那份，并告警。
        try:
            asyncio.get_running_loop().create_task(
                self._finalize(rec, TaskProgressStore(get_redis()),
                               reason=reason, cancelled=False)
            )
        except RuntimeError as e:  # 事件循环已关闭
            logger.warning(
                f"[task] {rec.task_id} 终态只写进了内存（{e}）：{reason}"
            )

    async def shutdown(self, timeout: float = SHUTDOWN_DRAIN_SECONDS) -> None:
        """
        优雅停机（AC-31）：**不打断在飞任务，等它们自然跑完。**

        ══════════════════════════════════════════════════════════════
        口径在 2026-09-23 反转过，理由记在这里免得再翻一遍
        ══════════════════════════════════════════════════════════════
        原实现是"置停机标志 → `handle.cancel()` 全部取消"。它有一个
        **实测复现过的后果**：被取消的任务停在 `processing` 上，因为
        `_run` 的 `CancelledError` 分支只改了内存、从不写 Redis ——
        进程重启后轮询接口读到的还是 `{'status': 'processing'}`，
        于是界面永远"正在分析图片…"，而那条任务早就没了。
        （`main.py` 的停机注释里恰好声称要避免这件事，它自己没防住。）

        现在的口径：**取消权归用户**（`POST /task/{id}/cancel`），
        停机只负责"不再接新任务 + 等"。

        ⚠️ **上限 150s 是算出来的，不是拍的**：`LLM_TIMEOUT_SECONDS = 120`
        是最内层的单次调用上限，节点层还有自己的熔断（`REVIEW_TIMEOUT=75`
        等），最坏路径约 120s；150s 覆盖它并留 30s 收尾。
        配套：`docker-compose.yml` 的 `stop_grace_period: 180s` **必须大于
        这个值**，否则 Docker 的硬杀会照样发生，等于没做。

        ⚠️ 超期之后**先记账再退出** —— 这不是替用户做决定，
        是进程退出前的记账义务：退出时不允许存在 `processing` 的悬挂记录。
        """
        self._shutting_down = True
        pending = [r for r in self._records.values() if r.is_running()]
        if not pending:
            logger.info("[task] 停机：没有在飞任务")
            return

        logger.info(
            f"[task] 停机：等待 {len(pending)} 个在飞任务自然结束"
            f"（上限 {timeout:.0f}s，不再主动打断）"
        )
        await asyncio.wait(
            [r.handle for r in pending], timeout=timeout,  # type: ignore[list-item]
        )

        stuck = [r for r in pending if r.is_running()]
        if not stuck:
            logger.info("[task] 停机：在飞任务全部已交付")
            return

        logger.warning(
            f"[task] 停机：{timeout:.0f}s 内仍有 {len(stuck)} 个任务未完成"
            f"（{', '.join(r.task_id for r in stuck)}），写入终态记录后退出"
        )
        progress = TaskProgressStore(get_redis())
        for r in stuck:
            await self._finalize(
                r, progress,
                reason=CANCEL_REASON_SHUTDOWN, cancelled=False,
            )
            r.handle.cancel()  # 记录先落地，再让协程退出
        # 给被取消的协程一点时间真的退出。不等的话，`shutdown` 一返回
        # `list_running()` 里还挂着它们 —— 看起来像"停机没停干净"，
        # 而这条恰恰是 AC-31 要防的那种"说不清的状态"。
        await asyncio.wait(
            [r.handle for r in stuck], timeout=5.0,  # type: ignore[list-item]
        )

    # ── 启动接管（AC-12） ──────────────────────────────────

    #: kind → 编译图的 stages。续跑时要用同一张图。
    _STAGES_FOR: dict[str, str] = {
        "parse": "parse", "generate": "generate", "review": "review",
    }

    async def resume_orphans(self) -> int:
        """
        接管上次**没跑完**的任务（AC-12）。**由 lifespan 在启动时调用。**

        ⚠️ **这是 AC-31 那条"退出前不留悬挂记录"的另一半。**
        AC-31 的排空只在**优雅停机**时成立；进程被 SIGKILL、断电、容器 OOM 时
        没有任何代码能执行，那些记录会永远停在 `processing` ——
        轮询接口会一直告诉前端"正在分析图片…"，**一个永远不会自愈的谎**。
        真正把它们救回来的只有两件事：记录能扫出来（`find_unfinished`），
        以及图能从检查点续上（本函数）。

        三种归宿，都要说清：
          · 有检查点 → **续跑**，跑完照常交付（已完成的节点不重跑）
          · 没有检查点 → 落成 `failed`，原因写"服务异常退出且无可恢复的检查点"
          · 认不出的 kind → 同上（不能瞎猜该用哪张图）

        Returns:
            接管（或落终态）的任务条数。
        """
        progress = TaskProgressStore(get_redis())
        try:
            orphans = await progress.find_unfinished()
        except Exception as e:  # noqa: BLE001 —— 扫不出来不该让服务起不来
            logger.warning(f"[task] 启动接管：扫描未完成任务失败（忽略）："
                           f"{type(e).__name__}: {e}")
            return 0

        if not orphans:
            return 0

        logger.warning(f"[task] 启动接管：发现 {len(orphans)} 个未完成任务"
                       f"（上次非正常退出留下的）")
        taken = 0
        for info in orphans:
            task_id = str(info.get("task_id") or "")
            kind = str(info.get("kind") or "")
            if not task_id or task_id in self._records:
                continue

            stages = self._STAGES_FOR.get(kind)
            if stages is None:
                await self._finalize_orphan(
                    task_id, progress,
                    reason=f"服务异常退出，且任务类型 {kind!r} 无法确定该用哪张图续跑",
                )
                taken += 1
                continue

            # 先确认检查点真的在 —— 没有就别硬续，否则图会拿空状态开跑并抛 KeyError
            has_checkpoint = False
            try:
                snap = await get_compiled_graph(stages).aget_state(
                    {"configurable": {"thread_id": task_id}}
                )
                has_checkpoint = bool(snap.values)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[task] {task_id} 读检查点失败：{type(e).__name__}: {e}")

            if not has_checkpoint:
                await self._finalize_orphan(
                    task_id, progress,
                    reason="服务异常退出，且没有可恢复的检查点（检查点未启用时会发生）",
                )
                taken += 1
                continue

            rec = self._revive(task_id, kind, info)
            logger.info(f"[task] {task_id}（{kind}）从检查点续跑")
            rec.handle = asyncio.create_task(
                self._run(rec, None, stages=stages, resume=True)
            )
            rec.handle.add_done_callback(lambda t, r=rec: self._on_done(r, t))
            taken += 1

        if taken:
            logger.warning(f"[task] 启动接管处理了 {taken} 个任务")
        return taken

    def _revive(self, task_id: str, kind: str, info: dict[str, Any]) -> TaskRecord:
        """
        按 Redis 里的事实重建一条 TaskRecord。

        能重建出来的字段全部来自落库的那份（kind / user_id / 两个时间戳 /
        trace_id / phase）—— **不猜**。缺的用默认值，宁可时间显示得粗糙，
        也不要编一个看起来精确的开始时间。
        """
        now = time.time()
        rec = TaskRecord(
            task_id=task_id,
            kind=kind,                                  # type: ignore[arg-type]
            trace_id=str(info.get("trace_id") or ""),
            user_id=info.get("user_id"),
            status="processing",
            phase=str(info.get("phase") or "queued"),
            started_at=float(info.get("started_at") or now),
            phase_entered_at=float(info.get("phase_entered_at") or now),
        )
        self._records[task_id] = rec
        return rec

    async def _finalize_orphan(
        self, task_id: str, progress: TaskProgressStore, *, reason: str,
    ) -> None:
        """把接不回来的悬挂记录落成终态。**没有内存记录，所以直接写 Redis。**"""
        logger.warning(f"[task] {task_id} 无法续跑，落成终态：{reason}")
        try:
            remote = await progress.read(task_id)
            await progress.update(
                task_id,
                kind=str((remote or {}).get("kind") or ""),
                status="failed",                       # type: ignore[arg-type]
                phase=str((remote or {}).get("phase") or "queued"),  # type: ignore[arg-type]
                progress=_as_int((remote or {}).get("progress")),
                started_at=(remote or {}).get("started_at"),
                phase_entered_at=(remote or {}).get("phase_entered_at"),
                degraded=False,
                user_id=(remote or {}).get("user_id"),
                cancelled=False,
                trace_id=(remote or {}).get("trace_id", ""),
                error=reason,
                ttl=RESULT_TTL,
            )
            audit("task_finished", task_id=task_id,
                  kind=str((remote or {}).get("kind") or ""),
                  status="failed", reason="orphan_unrecoverable")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[task] {task_id} 落终态失败：{type(e).__name__}: {e}")

    # ── 取消（AC-31：取消权归用户） ────────────────────────

    async def cancel(self, task_id: str, *, user_id: int | None) -> dict[str, Any]:
        """
        用户主动中断一个在飞任务。

        ⚠️ **有归属校验，且是必须的。** 登录上线之后，`task_id` 是接口里
        唯一指向"别人正在跑的那个任务"的凭据；没有这道检查，任何人
        拿到一个 task_id 就能掐掉别人的解析。实测的 id 形如
        `task_20260924_1a2b3c4d`（日期 + 8 位随机），不算不可猜。

        ⚠️ **已经结束的任务不算错误。** 用户点「中断」到请求到达之间
        任务可能刚好跑完 —— 那是"你赢了"，不是"操作失败"。
        返回 `cancelled=False` 并带上当前快照，让界面显示真实状态。
        （额度不退：它在建任务之前就已经扣了，见 deps.consume_quota。）

        Raises:
            TaskNotFound: 任务不存在或已过期。
            TaskNotOwned: 不是这个任务的主人。
        """
        progress = TaskProgressStore(get_redis())

        rec = self._records.get(task_id)
        if rec is None:
            # 内存里没有，但 Redis 里可能还留着（进程重启过）。
            remote = await self.status(task_id)
            if remote is None:
                raise TaskNotFound(task_id)
            # 只读得到记录、控制不了协程 —— 它早就不在本进程里跑了，
            # 所以不存在"要不要打断"的问题。
            return {**remote, "cancelled": False}

        # ⚠️ 先判归属**再**看是否在跑。反过来的话，"不是你的任务"
        #    和"这个任务已经结束了"会得到同一个响应，
        #    拿它当探测接口就能问出"某个 task_id 存不存在"。
        if rec.user_id is None or rec.user_id != user_id:
            raise TaskNotOwned(task_id)

        if not rec.is_running():
            return {**rec.to_dict(), "cancelled": False}

        logger.info(f"[task] {task_id} 被用户 {user_id} 中断")
        rec.handle.cancel()  # type: ignore[union-attr]

        # 等它真的退出再返回：接口回来时状态必须已经是终态，
        # 否则前端拿到 200 之后立刻轮询还会看到 processing。
        #
        # ⚠️ `shield` 挡住的是 `wait_for` 自己的超时取消 —— 不加的话，
        #    5 秒到点时 `wait_for` 会顺手把 handle 再取消一次，
        #    而它已经在取消中了，语义上说不清。
        # `except BaseException` 覆盖三种情况：超时、取消落地时抛出的
        # `CancelledError`、以及 runner 自己抛出的异常 —— 都不影响下面的记账。
        try:
            await asyncio.wait_for(asyncio.shield(rec.handle), timeout=CANCEL_JOIN_SECONDS)  # type: ignore[arg-type]
        except BaseException:  # noqa: BLE001
            pass

        # ⚠️ **无条件再写一次终态。** `_run` 里的 CancelledError 分支也会写，
        #    但那是"尽力而为"：协程被取消时它的 `await` 可能根本走不完 ——
        #    走不完，Redis 里就永远停在 `processing`，界面上永远"正在分析"。
        #    这里是接口线程，没有这个不确定性，所以以它为准。
        #    重复写同样的值是幂等的；万一任务在这几秒里刚好跑完，
        #    `_finalize` 自己不覆盖已交付结果，下面的 `rec.cancelled`
        #    也就仍是 False —— 如实返回"你赢了"，而不是谎称中断成功。
        await self._finalize(
            rec, progress, reason=CANCEL_REASON_USER, cancelled=True,
        )
        return {**rec.to_dict(), "cancelled": rec.cancelled}

    async def _finalize(
        self, rec: TaskRecord, progress: TaskProgressStore, *,
        reason: str, cancelled: bool,
    ) -> None:
        """
        把一个没跑完的任务落成终态记录。**停机超期与用户中断共用它。**

        两处都要"进程退出前不留悬挂记录"，因此措辞与字段必须一致 ——
        各写一份迟早会漂移，而漂移的表现是"同样是中断，界面有时说失败
        有时说已中断"。

        ⚠️ 阶段**保留在断点上**（不写成 `done`）：一方面进度条要停在
        真正断掉的位置，另一方面 `PHASE_TEXT['done']` 是"完成"，
        一个大红叉配"完成"是本项目踩过的坑。

        ⚠️ **绝不覆盖已经交付的结果。** 用户点「中断」到接口真正执行之间
        任务可能刚好跑完 —— 那时把 `completed` 改成 `failed` 就是**毁掉
        一份已经算出来的方案**，而且用户看到的是"你取消了"，实际是
        "它已经好了"。这个保护放在这里而不是各个调用方，是因为
        "谁能改终态"这件事必须只有一个答案。
        """
        if rec.status == "completed":
            logger.info(f"[task] {rec.task_id} 已完成，{reason} 不覆盖它的结果")
            return

        rec.status = "failed"
        rec.cancelled = cancelled
        rec.error = reason
        rec.finished_at = time.time()
        try:
            await self._write(
                rec, progress, phase=rec.phase, status="failed",
                error=reason,
            )
        except BaseException as e:  # noqa: BLE001 —— 记账失败不能盖住取消本身
            logger.warning(
                f"[task] {rec.task_id} 终态写盘失败（内存已更新）："
                f"{type(e).__name__}: {e}"
            )

    # ── 执行 ──────────────────────────────────────────────

    async def _run(self, rec: TaskRecord, state: HomeDecoState | None, *,
                   stages: str, resume: bool = False) -> None:
        """
        驱动图执行，逐节点写进度。

        `resume=True` 时 `state` 传 `None`：LangGraph 会**从检查点继续**，
        而不是从头跑。已完成的节点不会重跑 —— 这是 AC-12 的核心语义，
        也是"崩溃后接管"能不浪费已经花掉的 LLM 调用的原因。

        ⚠️ 续跑**必须**有检查点。没有检查点时传 `None` 会让图拿着空状态
        开跑，节点抛 KeyError，用户看到的是一个莫名其妙的失败。
        所以 `resume_orphans` 会先 `aget_state` 确认检查点存在，再决定续跑
        还是落终态。
        """
        progress = TaskProgressStore(get_redis())
        graph = get_compiled_graph(stages)  # type: ignore[arg-type]
        cfg = {"configurable": {"thread_id": rec.task_id}}
        #: 阶段 -> 进入时刻。跑完时打一行日志，`core/progress.py` 里那些
        #: 期望耗时就是照着这种日志标的 —— 没有它，下次没人敢动那些数字。
        marks: list[tuple[str, float]] = []

        # trace_id 绑到上下文 —— 之后所有 Agent / LLM 调用 / 审计日志都带上它。
        # 用 with_trace_id（contextvars）而非 bind_trace_id（全局配置）：
        # 多个任务并发时，全局配置会互相覆盖。
        with with_trace_id(rec.trace_id):
            # 只写一个"已接收"的初始态，之后**全部阶段由真实的节点事件驱动**。
            #
            # ⚠️ 这里以前写的是 `phase="prechecking"`（"正在检查图片质量…"），
            # 但那时代码里根本没有图片质检动作 —— 那句文案是假的。
            # 现在 `prechecking` 由 `precheck_image` 节点触发（见 _NODE_PHASE），
            # 界面显示它的时候，检查确实正在发生。
            await self._write(rec, progress, phase="queued", status="processing")
            marks.append(("queued", time.time()))
            try:
                # ⚠️ 用 `astream_events` 而不是 `astream(stream_mode="updates")`。
                #
                # updates 只在节点**跑完之后**才产出那一片 —— 于是阶段永远滞后一格。
                # 实测：解析接口 48 秒里有 21 秒显示的是「正在检查图片质量…」，
                # 而那时模型早就在读图了。用户看到的和他等的事情对不上，
                # 恰是需求文档最在意的那点（「用户看到的是正在做什么」）。
                #
                # events 流会给出 `on_chain_start`，节点**开始**时就能拿到名字，
                # 滞后归零。实测确认事件里的 name 就是节点名（另有
                # tags=['graph:step:N'] 可用作步序）。
                published = False
                stream_input = None if resume else state
                if resume:
                    logger.info(f"[task] {rec.task_id} 从检查点续跑（AC-12）")
                async for ev in graph.astream_events(stream_input, cfg, version="v2"):
                    if ev.get("event") != "on_chain_start":
                        continue
                    node = str(ev.get("name"))
                    phase = _phase_for(node, rec.kind)
                    if not phase:
                        continue
                    if phase != rec.phase:
                        marks.append((phase, time.time()))
                    await self._write(rec, progress, phase=phase,
                                      status="processing")

                    # ⚡ 方案先交出去（见模块说明与 _publish_partial）。
                    # 只在方案链、只在审查**开始**那一刻做一次。
                    if node == "review_risks" and not published:
                        published = True
                        await self._publish_partial(rec, progress, graph, cfg)

                # 跑完后从 checkpointer 取完整状态。
                # events 流不直接给最终状态，用它拿进度、用 aget_state 拿结果。
                snap = await graph.aget_state(cfg)
                final = dict(snap.values)

                rec.result = self._build_result(rec.kind, final)
                rec.degraded = bool(final.get("degraded"))
                rec.status = "completed"

                # 解析任务完成后把户型暂存起来，供 4.3 的 `/design/generate`
                # 按 layout_id 取用（见 store.py 的说明：不能让方案接口重新解析）
                if rec.kind == "parse" and final.get("layout"):
                    from . import store as layout_store

                    # 这些元信息只对**落库**有意义（诊断结果、耗时、降级原因），
                    # 内存与 Redis 那边用不上 —— 它们的签名参数是可选的，
                    # 就是为了让这里能一次把该存的都存了。
                    diag = final.get("diagnosis")
                    await layout_store.save(
                        str(final.get("layout_id") or ""), final["layout"],
                        user_id=rec.user_id,
                        diagnosis=diag if isinstance(diag, dict) else None,
                        confidence=_as_float(final.get("confidence")),
                        model_used=str(final.get("model_used") or "")[:32] or None,
                        degraded=rec.degraded,
                        degrade_reason=_first_degrade_reason(final),
                        parse_elapsed_ms=int(
                            (time.time() - rec.started_at) * 1000
                        ),
                    )

                # 方案链完成后把三套方案落库（design_plans）。
                # ⚠️ 与户型不同，这里**没有 Redis 层** —— 方案此前根本不落盘，
                #    结果只活在任务结果里（TTL 1 小时）。所以这一步是新增能力：
                #    "上周那套北欧中档方案"从此查得到。
                if rec.kind == "generate" and rec.result:
                    await self._persist_plans(rec, final)
                # 降级完成也是完成，但阶段要如实标出来（AC-17）
                await self._write(
                    rec, progress,
                    phase="degraded" if rec.degraded else "done",
                    status="completed", result=rec.result,
                )
                logger.info(
                    f"[task] {rec.task_id} 完成，degraded={rec.degraded}"
                )
                # 审计：**`degraded` 必须带上**（AC-14 原文要求含该字段）。
                # 它也是 AC-17 那"三处可见"里的第三处 —— 另外两处是
                # API 响应与前端 UI，早就有了，缺的一直是这一处。
                audit("task_finished", task_id=rec.task_id, kind=rec.kind,
                      user_id=rec.user_id, status="completed",
                      degraded=rec.degraded,
                      elapsed_seconds=round(time.time() - rec.started_at, 1))
                self._log_phase_durations(rec, marks)

            except asyncio.CancelledError:
                # ⚠️ **这一支必须写 Redis。**
                #
                # 它原来只改内存、直接 raise，于是被取消的任务在 Redis 里
                # **永远停在 `processing`** —— 实测复现过：容器重启后轮询
                # 接口读到 `{'status': 'processing', 'phase_text': '正在分析图片…'}`，
                # 界面永远"正在分析"，而那条任务早就不存在了。
                # 这正是 AC-31 那句"进程退出前不允许存在 processing 的悬挂记录"。
                #
                # ⚠️ 这里**只是尽力而为**：协程已经在取消中，`await` 有可能
                #    走不完。权威是接口层那次 `_finalize`（它没有这个不确定性）。
                #
                # ⚠️ 已经被记过账的（停机超期处理会先 `_finalize` 再取消），
                #    不要在这里改写成"被用户中断" —— 那会把停机原因覆盖掉，
                #    而"为什么停的"正是这条记录唯一有价值的信息。
                if rec.status == "failed":
                    logger.info(
                        f"[task] {rec.task_id} 已被记过终态（{rec.error}），"
                        f"取消落地时不再改写"
                    )
                    raise
                rec.status = "failed"
                rec.cancelled = True
                rec.error = CANCEL_REASON_USER
                rec.finished_at = time.time()
                logger.warning(f"[task] {rec.task_id} 被取消，写入终态记录")
                try:
                    await self._write(rec, progress, phase=rec.phase,
                                      status="failed", error=rec.error)
                except BaseException as e:  # noqa: BLE001
                    logger.warning(
                        f"[task] {rec.task_id} 取消时写终态失败（接口层会补）："
                        f"{type(e).__name__}: {e}"
                    )
                raise

            except PrecheckError as e:
                # 图片不合格**不是系统故障**，是用户需要处理的一件事。
                # 所以只把可操作的那句话交出去，不带异常类名 ——
                # 用户要读的是「图片分辨率过低，请换一张更清晰的」，
                # 不是「PrecheckError: ...」。零 Token 消耗。
                rec.status = "failed"
                rec.error = e.advice
                rec.result = {"precheck": e.result.to_dict()}
                logger.info(f"[task] {rec.task_id} 图片未通过预检：{e.result.rejections}")
                await self._write(rec, progress, phase=rec.phase, status="failed",
                                  error=rec.error, result=rec.result)

            except Exception as e:  # noqa: BLE001 —— 任务级兜底，转成可轮询的失败态
                rec.status = "failed"
                rec.error = f"{type(e).__name__}: {e}"
                logger.exception(f"[task] {rec.task_id} 执行失败: {e}")
                # ⚠️ 失败时**保留当前阶段**，不再把它落成 `done`。
                #
                # 落成 `done` 有两个连带后果：`PHASE_TEXT['done']` 是"完成"
                # （界面上一个大红叉配"完成"），而且进度会按"阶段已走完"
                # 算成 100%。停在断点上才能说清"走到了哪一步断的"。
                await self._write(rec, progress, phase=rec.phase, status="failed",
                                  error=rec.error)
                audit("task_finished", task_id=rec.task_id, kind=rec.kind,
                      user_id=rec.user_id, status="failed",
                      degraded=rec.degraded, error=rec.error[:200])

    async def _publish_partial(
        self,
        rec: TaskRecord,
        progress: TaskProgressStore,
        graph: Any,
        cfg: dict[str, Any],
    ) -> None:
        """
        审查开始的那一刻，把已经产出的方案先写进结果。

        ⚠️ **失败一律吞掉。** 这是纯粹的"提前给一点"，任何异常都不能
        变成任务的失败原因 —— 拿不到快照顶多是回到改动前的体验
        （等审查一起出来），而抛出去会让一个本来能成功的任务失败。

        实测依据（2026-09-24）：整链 122.9s，方案在 38s 就齐了。
        """
        if rec.kind != "generate":
            return
        try:
            snap = await graph.aget_state(cfg)
            values = dict(snap.values)
            assembled = assemble_plans(values)
            if not assembled.get("plans"):
                return          # 方案都还没出来，提前发没有意义

            # 用**同一个** `_build_result` 塑形，只是先把汇总结果并进快照。
            # 手写一份 partial 的字段表迟早会和完整结果的字段表漂移 ——
            # 而那种漂移表现为"前端偶尔读到 undefined"，最难查的一类。
            partial = self._build_result("generate", {**values, **assembled})
            partial["partial"] = True
            await self._write(rec, progress, phase=rec.phase, status="processing",
                              result=partial)
            logger.info(
                f"[task] {rec.task_id} 方案已提前交付"
                f"（{len(assembled['plans'])} 套，风险复核仍在进行）"
            )
        except Exception as e:  # noqa: BLE001 —— 见 docstring
            logger.warning(
                f"[task] {rec.task_id} 提前交付方案失败（不影响任务）："
                f"{type(e).__name__}: {e}"
            )

    async def _persist_plans(self, rec: TaskRecord, final: dict[str, Any]) -> None:
        """
        把方案链的产出落进 `design_plans`（AC-19 之后新加的能力）。

        ⚠️ **失败只记 warning，不影响任务成功。** 与落库的整体立场一致
        （见 `db/pool.py`）：库是加固不是依赖。用户拿到的方案来自 API 响应，
        落库是为了"下次还查得到"，不是为了让这次成功。

        ⚠️ 用 `rec.result` 而不是 `final` 取方案：前者是**已经裁剪过的
        接口返回体**（`_build_result`），后者是整条链的 state。
        这里要的字段（plans 的六项产物）两者都有，但走 `rec.result`
        能保证"进了库的东西"与"用户看到的东西"是同一份 ——
        两边各取一次，迟早会出现库里多一个字段、界面上没有。
        """
        from ..db import repository

        plans = (rec.result or {}).get("plans") or []
        layout_id = str((rec.result or {}).get("layout_id") or "")
        if not plans or not layout_id:
            return
        try:
            written = await repository.save_plans(
                layout_id, plans,
                hotspots_by_plan=final.get("hotspots") or {},
            )
        except Exception as e:  # noqa: BLE001 —— 落库失败不该让任务算失败
            logger.warning(f"[task] 方案落库异常 {rec.task_id}：{type(e).__name__}: {e}")
            return

        if written == len(plans):
            logger.info(f"[task] {rec.task_id} 方案已落库 {written} 套（{layout_id}）")
        else:
            # 部分成功要如实说：0 套与 2/3 套是两种不同的故障，
            # 一句"落库完成"会把两者都盖住。
            logger.warning(
                f"[task] 方案落库不完整：{written}/{len(plans)} 套"
                f"（{layout_id}）—— 常见原因是户型不在库里（外键）或数据库不通"
            )

    @staticmethod
    def _log_phase_durations(rec: TaskRecord, marks: list[tuple[str, float]]) -> None:
        """
        把各阶段实际耗时打成一行。

        `core/progress.py` 里的期望耗时全部来自这种日志。没有它，
        那条模型就是一组没有出处的数字 —— 下一个人既不敢改，也不知道
        它是不是还在生效。这是**标定入口**，不是调试残留。
        """
        if len(marks) < 2:
            return
        parts: list[str] = []
        for i, (phase, at) in enumerate(marks):
            end = marks[i + 1][1] if i + 1 < len(marks) else rec.finished_at or time.time()
            parts.append(f"{phase}={end - at:.1f}s")
        logger.info(f"[task] {rec.task_id} 阶段耗时：{' '.join(parts)}")

    @staticmethod
    def _build_result(kind: TaskKind, final: dict[str, Any]) -> dict[str, Any]:
        """
        按任务类型裁剪返回体。

        **不做全量返回**：整条链的 state 里有 `image_ref`（base64，可能上 MB）、
        `trace`、`plan_bundles`（含全部中间产物）—— 全丢给前端既浪费带宽，
        也把内部结构暴露成了接口契约。只挑该给的。
        """
        if kind == "parse":
            return {
                "layout_id": final.get("layout_id"),
                "layout": final.get("layout"),
                # 图片质量预检结果（AC-27）。前端可以据此显示
                # "1920×1080 · 清晰度 412" 这类信息，以及未阻断的提醒。
                "precheck": final.get("precheck"),
                # 米制场景（几何内核）。2D 矢量渲染与 3D 漫步共用这一份坐标，
                # 避免两个渲染器各自换算导致"热点和墙对不上"。
                #
                # 在这里算而不是在图里加节点：它是 layout 的**纯函数**，
                # 不产生新的状态语义，加节点只会让图更难读。
                "scene": _scene_of(final.get("layout")),
                "diagnosis": final.get("diagnosis"),
                "capabilities": (final.get("layout") or {}).get("capabilities"),
                "degraded": bool(final.get("degraded")),
                "degrade_reasons": final.get("degrade_reasons") or [],
                "errors": final.get("errors") or [],
                "trace": final.get("trace") or [],
            }

        if kind == "review":
            return {
                "review": final.get("risk_review"),
                "degraded": bool(final.get("degraded")),
                "trace": final.get("trace") or [],
            }

        return {
            "layout_id": final.get("layout_id"),
            "diagnosis": final.get("diagnosis"),
            "plans": final.get("plans") or [],
            "comparison": final.get("comparison"),
            # ⚠️ `partial` 是**显式契约**，不能让前端靠"有没有 risks"去猜。
            # 审查还没跑完时方案就已经交出去了（见 _publish_partial），
            # 此时 result 长得和完整结果很像 —— 差一个真假难辨的字段
            # 就是"看起来合理的错误结果"。这里写死 False，让"完整"
            # 也是一个被明确断言的结论。
            "partial": False,
            "degraded": bool(final.get("degraded")),
            "degrade_reasons": final.get("degrade_reasons") or [],
            "errors": final.get("errors") or [],
            "trace": final.get("trace") or [],
        }

    # ── 进度读写 ──────────────────────────────────────────

    async def _write(
        self,
        rec: TaskRecord,
        progress: TaskProgressStore,
        *,
        phase: str,
        status: TaskStatus,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        """
        双写：内存（权威）+ Redis（供跨进程/重启后读取）。

        内存写失败要炸（那是代码 bug），Redis 写失败只告警 ——
        Redis 是可选依赖，它挂了不该让任务失败。
        """
        if phase != rec.phase:
            # ⚠️ 进入新阶段的时刻必须记下来：剩余时间要算"这个阶段已经走了多久"，
            # 而它只能从这个时刻推。漏记一期，整条链的倒计时都会偏早。
            rec.phase_entered_at = time.time()
        rec.phase = phase
        rec.status = status
        if status in ("completed", "failed"):
            rec.finished_at = time.time()

        # ⚠️ `progress` 必须在这里跟着 phase 一起更新。
        #
        # 它原来**从来没有被赋值过** —— 一直是 dataclass 默认的 0。
        # Redis 可用时看不出来（`status()` 优先读 Redis，那边是
        # `TaskProgressStore` 按阶段算的）；**Redis 一挂就露馅**：
        # 回退到内存的 `rec.to_dict()`，进度条从头到尾钉在 0，
        # 而阶段文案一直在变。用户看到的是"卡死了"，实际一直在跑。
        #
        # 本机的 Redis 默认就是没起的（health 里 redis 一直是 false），
        # 所以这条路径才是**常态**，不是兜底。
        entry = progress_at(rec.kind, phase)
        rec.progress = rec.progress if entry is None else entry
        if error:
            rec.error = error
        if result is not None:
            rec.result = result

        try:
            await progress.update(
                rec.task_id,
                kind=rec.kind,
                status=status,          # type: ignore[arg-type]
                phase=phase,            # type: ignore[arg-type]
                progress=rec.progress,
                started_at=rec.started_at,
                phase_entered_at=rec.phase_entered_at,
                degraded=rec.degraded,
                user_id=rec.user_id,
                cancelled=rec.cancelled,
                trace_id=rec.trace_id,
                result=result,
                error=error or None,
                ttl=RESULT_TTL,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[task] 进度写入 Redis 失败（不影响任务）："
                           f"{type(e).__name__}: {e}")

    async def status(self, task_id: str) -> dict[str, Any] | None:
        """
        读任务状态。**优先 Redis，回退内存。**

        Redis 优先是为了支持"进程重启后仍能查到已完成的任务"（4.4 要求结果保留 1 小时）。
        回退内存是为了 Redis 没开时接口仍然可用 —— 见模块说明。

        ⚠️ **两条路径都从这里出去，所以进度/剩余时间只在这里算一次。**
        以前两边各算各的（各查各的表），只要有人改了一边，界面就会随
        "Redis 在不在"而变 —— 那种 bug 在演示现场没法排查。
        """
        from ..core.redis_client import PHASE_TEXT

        rec = self._records.get(task_id)

        try:
            remote = await TaskProgressStore(get_redis()).read(task_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[task] 读 Redis 进度失败，回退内存：{type(e).__name__}: {e}")
            remote = None

        if remote:
            phase = remote.get("phase", "queued")
            status = remote.get("status", "pending")
            # kind 落库是为了这里：进程重启后 `rec` 没了，但剩余时间还要能给 ——
            # 而它是 `(kind, phase)` 的函数。老记录（改动前写下的）没有这个字段，
            # 此时退回内存里的 rec；两者都没有就按"估不出来"处理。
            kind = remote.get("kind") or (rec.kind if rec else "")
            started_at = remote.get("started_at")
            if started_at is None and rec is not None:
                started_at = rec.started_at
            phase_entered_at = remote.get("phase_entered_at")
            if phase_entered_at is None:
                phase_entered_at = rec.phase_entered_at if rec else started_at

            stored_progress = _as_int(remote.get("progress"))
            view = build_view(
                kind=kind, phase=phase, status=status,
                started_at=started_at, phase_entered_at=phase_entered_at,
                fallback_progress=stored_progress,
            )
            return {
                "task_id": task_id,
                "trace_id": remote.get("trace_id", ""),
                "kind": kind,
                "status": status,
                "phase": phase,
                "phase_text": remote.get("phase_text") or PHASE_TEXT.get(phase, phase),
                **view,
                "degraded": remote.get("degraded") in ("1", "true", True),
                "cancelled": remote.get("cancelled") in ("1", "true", True),
                "error": remote.get("error") or None,
                "result": remote.get("result"),
            }

        return rec.to_dict() if rec else None

    def list_running(self) -> list[str]:
        """在飞任务的 id。给 `/system/health` 用。"""
        return [r.task_id for r in self._records.values()
                if r.handle and not r.handle.done()]


def _scene_of(layout: Any) -> dict[str, Any] | None:
    """
    把户型归一化成米制场景。失败返回 None，**不抛**。

    几何内核有自己的测试覆盖（tests/test_geometry.py），但它面对的是
    模型输出的、质量参差的真实数据。一旦它在这里抛异常，用户连
    「户型已经解析出来了」这个事实都看不到 —— 而解析本身是成功的。
    所以失败只降级为"这次没有场景"，解析结果照常返回。
    """
    if not isinstance(layout, dict) or not layout:
        return None
    try:
        from ..services.geometry import normalize_layout

        return normalize_layout(layout).to_dict()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[geometry] 场景归一化失败，本次不含 scene：{type(e).__name__}: {e}")
        return None


def _as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _as_float(v: Any) -> float | None:
    """给落库用。**取不到返回 None 而不是 0.0** —— 置信度 0 与"没算出来"是两件事。"""
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _first_degrade_reason(final: dict[str, Any]) -> str | None:
    """
    取第一条降级原因，给 `house_layouts.degrade_reason` 用（列宽 VARCHAR(128)）。

    ⚠️ **只取第一条，不拼接。** 数据库里这一列是"降级的一句话摘要"，
    而完整的降级原因列表在任务结果与审计里都有。把多条拼进一个有长度
    上限的列，会在某个长度上突然开始截断 —— 那时丢的是哪一条完全随机。
    """
    reasons = final.get("degrade_reasons") or []
    for r in reasons:
        text = str(r).strip()
        if text:
            return text[:128]
    return None


_manager: TaskManager | None = None


def get_task_manager() -> TaskManager:
    global _manager
    if _manager is None:
        _manager = TaskManager()
    return _manager


def reset_task_manager() -> None:
    """仅测试用：丢弃全部任务记录。"""
    global _manager
    _manager = None


__all__ = [
    "TaskKind", "TaskStatus", "TaskRecord", "TaskManager",
    "TaskNotFound", "TaskNotOwned",
    "CANCEL_REASON_USER", "CANCEL_REASON_SHUTDOWN",
    "SHUTDOWN_DRAIN_SECONDS", "CANCEL_JOIN_SECONDS",
    "get_task_manager", "reset_task_manager", "RESULT_TTL",
]
