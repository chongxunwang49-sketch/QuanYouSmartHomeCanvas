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
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from loguru import logger

from ..core.logger import with_trace_id
from ..core.redis_client import TaskProgressStore, get_redis
from ..graph.state import HomeDecoState, initial_state
from ..graph.workflow import PrecheckError, get_compiled_graph

TaskKind = Literal["parse", "generate", "review"]
TaskStatus = Literal["pending", "processing", "completed", "failed"]

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
    "review_risks": "planning",
    "aggregate_plans": "finalizing",
}


def _phase_for(node: str, kind: TaskKind) -> str | None:
    """
    节点名 → 阶段。**审查任务的措辞要单独处理。**

    同一个 `review_risks` 节点在两种语境下含义不同：
      · `kind="review"` —— 用户提交的是一份报价单，该说「正在审查报价单…」
      · `kind="generate"` —— 它在方案生成链里跑，说「正在生成装修方案…」才对

    实测踩过：走报价单审查时，界面全程显示「正在生成装修方案…」。
    用户看的是一份合同，却被告知系统在生成方案 —— 这正是需求文档 2.2.4
    最在意的那件事（「用户看到的是正在做什么」，而不是进程名）。

    所以映射不能只看节点名，得带上任务类型。
    """
    if node == "review_risks" and kind == "review":
        return "reviewing"
    return _NODE_PHASE.get(node)

#: 结果保留秒数。1 小时支持"刷新页面后重新获取"（4.4）。
RESULT_TTL = 3600


def _new_task_id(kind: TaskKind) -> str:
    """`task_20260922_1a2b3c4d` —— 日期便于人肉排查，随机段避免碰撞。"""
    return f"task_{datetime.now():%Y%m%d}_{uuid.uuid4().hex[:8]}"


@dataclass
class TaskRecord:
    """一个后台任务的运行期状态。"""

    task_id: str
    kind: TaskKind
    trace_id: str
    status: TaskStatus = "pending"
    phase: str = "queued"
    progress: int = 0
    degraded: bool = False
    error: str = ""
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())
    finished_at: float | None = None
    handle: asyncio.Task | None = None

    def to_dict(self) -> dict[str, Any]:
        """给轮询接口的视图。字段与 4.4 的响应对齐。"""
        from ..core.redis_client import PHASE_TEXT

        return {
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "kind": self.kind,
            "status": self.status,
            "phase": self.phase,
            # 前端直接展示这个字段，不必自建映射表（2.2.4）
            "phase_text": PHASE_TEXT.get(self.phase, self.phase),
            "progress": self.progress,
            "degraded": self.degraded,
            "error": self.error or None,
            "result": self.result,
        }


class TaskManager:
    """任务注册表 + 图执行驱动。"""

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}
        self._shutting_down = False

    # ── 生命周期 ──────────────────────────────────────────

    def create(self, kind: TaskKind, trace_id: str = "") -> TaskRecord:
        if self._shutting_down:
            raise RuntimeError("服务正在停机，不再接收新任务")

        rec = TaskRecord(
            task_id=_new_task_id(kind),
            kind=kind,
            trace_id=trace_id or uuid.uuid4().hex,
        )
        self._records[rec.task_id] = rec
        logger.info(f"[task] 创建 {rec.task_id}（{kind}）trace_id={rec.trace_id[:8]}")
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
        """
        if handle.cancelled():
            rec.status = "failed"
            rec.error = "任务被取消（服务停机）"
            return
        exc = handle.exception()
        if exc is not None:
            rec.status = "failed"
            rec.phase = "done"
            rec.error = f"{type(exc).__name__}: {exc}"
            logger.exception(f"[task] {rec.task_id} 未捕获异常: {exc}")

    async def shutdown(self, timeout: float = 10.0) -> None:
        """
        优雅停机（ADR-13）：取消在飞任务，等它们真的退出。

        先置标志位再取消 —— 顺序反过来的话，停机过程中新到的请求
        仍会创建任务，那些任务没人管。
        """
        self._shutting_down = True
        pending = [r for r in self._records.values() if r.handle and not r.handle.done()]
        if not pending:
            return

        logger.info(f"[task] 停机：取消 {len(pending)} 个在飞任务")
        for r in pending:
            r.handle.cancel()  # type: ignore[union-attr]

        try:
            await asyncio.wait_for(
                asyncio.gather(*(r.handle for r in pending), return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[task] 停机：{timeout}s 内仍有任务未退出，强制放弃")

    # ── 执行 ──────────────────────────────────────────────

    async def _run(self, rec: TaskRecord, state: HomeDecoState, *, stages: str) -> None:
        """驱动图执行，逐节点写进度。"""
        progress = TaskProgressStore(get_redis())
        graph = get_compiled_graph(stages)  # type: ignore[arg-type]
        cfg = {"configurable": {"thread_id": rec.task_id}}

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
                async for ev in graph.astream_events(state, cfg, version="v2"):
                    if ev.get("event") != "on_chain_start":
                        continue
                    phase = _phase_for(str(ev.get("name")), rec.kind)
                    if phase:
                        await self._write(rec, progress, phase=phase,
                                          status="processing")

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

                    await layout_store.save(
                        str(final.get("layout_id") or ""), final["layout"]
                    )
                # 降级完成也是完成，但阶段要如实标出来（AC-17）
                await self._write(
                    rec, progress,
                    phase="degraded" if rec.degraded else "done",
                    status="completed", result=rec.result,
                )
                logger.info(
                    f"[task] {rec.task_id} 完成，degraded={rec.degraded}"
                )

            except asyncio.CancelledError:
                rec.status = "failed"
                rec.error = "任务被取消"
                logger.warning(f"[task] {rec.task_id} 被取消")
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
                await self._write(rec, progress, phase="done", status="failed",
                                  error=rec.error, result=rec.result)

            except Exception as e:  # noqa: BLE001 —— 任务级兜底，转成可轮询的失败态
                rec.status = "failed"
                rec.error = f"{type(e).__name__}: {e}"
                logger.exception(f"[task] {rec.task_id} 执行失败: {e}")
                await self._write(rec, progress, phase="done", status="failed",
                                  error=rec.error)

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
        rec.phase = phase
        rec.status = status
        # ⚠️ `progress` 必须在这里跟着 phase 一起更新。
        #
        # 它原来**从来没有被赋值过** —— 一直是 dataclass 默认的 0。
        # Redis 可用时看不出来（`status()` 优先读 Redis，那边是
        # `TaskProgressStore` 按 PHASE_PROGRESS 算的）；**Redis 一挂就露馅**：
        # 回退到内存的 `rec.to_dict()`，进度条从头到尾钉在 0，
        # 而阶段文案一直在变。用户看到的是"卡死了"，实际一直在跑。
        #
        # 本机的 Redis 默认就是没起的（health 里 redis 一直是 false），
        # 所以这条路径才是**常态**，不是兜底。
        from ..core.redis_client import PHASE_PROGRESS

        rec.progress = PHASE_PROGRESS.get(phase, rec.progress)
        if error:
            rec.error = error
        if result is not None:
            rec.result = result

        try:
            await progress.update(
                rec.task_id,
                status=status,          # type: ignore[arg-type]
                phase=phase,            # type: ignore[arg-type]
                degraded=rec.degraded,
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
            return {
                "task_id": task_id,
                "trace_id": remote.get("trace_id", ""),
                "kind": (rec.kind if rec else ""),
                "status": remote.get("status", "pending"),
                "phase": phase,
                "phase_text": remote.get("phase_text") or PHASE_TEXT.get(phase, phase),
                "progress": _as_int(remote.get("progress")),
                "degraded": remote.get("degraded") in ("1", "true", True),
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
    "get_task_manager", "reset_task_manager", "RESULT_TTL",
]
