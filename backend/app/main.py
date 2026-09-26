"""
FastAPI 应用入口。

    uvicorn backend.app.main:app --reload --port 8000

═══════════════════════════════════════════════════════════════════
这个文件只做四件事
═══════════════════════════════════════════════════════════════════
1. **生命周期**：启动时校验依赖、停机时**等在飞任务跑完**（AC-31，不打断）
2. **trace_id 中间件**：每个请求一个 trace_id，贯穿 API → Agent → LLM → 审计日志
3. **错误翻译**：把业务异常统一转成 HTTP 200 + `code != 0`（见 api/schemas.py）
4. **挂路由**

**不放任何业务逻辑。** 路由在 `api/routes.py`，任务驱动在 `api/tasks.py`。
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from .api.routes import router
from .api.schemas import ApiError, ApiResponse
from .api.tasks import SHUTDOWN_DRAIN_SECONDS, get_task_manager
from .core.config import settings
from .core import metrics
from .core.logger import setup_logging, with_trace_id

#: trace_id 的请求头名。前端/网关传入则沿用，便于跨服务串联。
TRACE_HEADER = "X-Trace-Id"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    启动与停机。

    停机这段是 **AC-31** 的落地（口径于 2026-09-23 反转）：
    **先停止接收新任务，再等已在飞的自然跑完，不主动打断。**
    先停新任务这一步不能省 —— 反过来的话，停机过程中新到的请求
    仍会创建任务，而那些任务没人管，用户会看到一个永远停在
    processing 的任务。

    为什么不再主动取消（`handle.cancel()`）：实测它会把任务留在
    `processing` 上（`_run` 的 CancelledError 分支当时不写 Redis），
    界面永远"正在分析图片…"。取消权现在归用户，见 `api/tasks.py`。
    """
    # ⚠️ **必须是启动后第一件事。**
    #
    # `setup_logging` 有幂等保护（进程内只生效一次），所以谁先调谁定终身：
    # 在它之前发生的日志不会进审计文件。放在这里之前打过任何一行日志，
    # 那一行就只在控制台上，事后查不到。
    #
    # ⚠️ 这里也是 AC-14 唯一被激活的地方。此前 `log_dir` 参数**全项目
    # 无一处传过** —— 分支写好了、注释标着"供审计（AC-14）"，
    # 而审计文件一个都没产生过。测试里有"文件真的产生了"的用例守着。
    setup_logging(log_dir=settings.LOG_DIR, level=settings.LOG_LEVEL)

    logger.info("═" * 60)
    logger.info("全友·智绘家 后端启动中…")
    logger.info(f"审计日志目录：{settings.LOG_DIR}")

    # 关键配置体检。**只告警不阻断** —— 缺 key 时本地 Ollama 还能兜底，
    # 起不来比"降级运行"更糟（整个演示都做不了）。
    if not settings.DEEPSEEK_API_KEY:
        logger.warning("未配置 DEEPSEEK_API_KEY —— 将走本地 Ollama，能力明显下降")
    try:
        from .services.knowledge import store as knowledge_store

        info = knowledge_store.collection_info()
        if info.available and info.count:
            logger.info(f"知识库就绪：{info.count} 条 chunk")
        else:
            logger.warning(
                f"知识库不可用（{info.reason}）—— A-06 将拿不到引用依据。"
                f"运行 scripts/ingest_knowledge.py 建库。"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"知识库检查失败：{type(e).__name__}: {e}")

    # ── AC-12：先建持久化检查点，再接管上次没跑完的任务 ──
    # ⚠️ 顺序不能反：`resume_orphans` 要靠检查点才能判断"续不续得了"，
    #    检查点没建好时它会把所有悬挂记录都判成"无可恢复"并落成失败 ——
    #    那会**误杀**掉本来能救回来的任务。
    from .graph.workflow import setup_checkpointer, teardown_checkpointer

    await setup_checkpointer()
    resumed = await get_task_manager().resume_orphans()
    if resumed:
        logger.warning(f"启动接管：处理了 {resumed} 个上次未完成的任务")

    # ── 业务数据落库（户型 / 方案 / 审计）────────────────────
    #
    # ⚠️ **不在这里建表。** 建表是部署动作，由 `scripts/init_db.py` 负责，
    #    结果落进 `schema_migrations`。应用启动时顺手 CREATE TABLE 看起来
    #    方便，代价是"表是谁建的、什么时候建的、建的是哪个版本"再也说不清 ——
    #    而容器里跑的应用通常连 DDL 权限都不该有。
    #    这里只**检查**：表在不在，不在就告警并说明怎么修。
    #
    # ⚠️ 起不来的立场与 Redis 相同：库不通**不停机**。业务数据退回
    #    内存 + Redis（TTL 1 小时），也就是落库之前的行为。
    #    "库挂了整个演示做不了"比"库挂了回到旧行为"糟得多。
    try:
        from .db import audit_sink, migrations, pool

        if not settings.ENABLE_DB:
            logger.info("业务落库已关闭（ENABLE_DB=false），户型与方案只进内存 + Redis")
        elif await pool.is_available():
            done = await migrations.applied()
            if not done:
                logger.warning(
                    "数据库已连接，但业务表不存在 —— 户型与方案将无法持久化。"
                    "**在宿主机**执行 `python scripts/init_db.py` 建表"
                    "（PG 端口已映射到 127.0.0.1:5432，宿主机脚本连得上）。"
                    "⚠️ 容器里没有这个脚本，而且这是有意的：应用镜像不该带 "
                    "DDL 能力，建表是部署动作不是运行时动作。"
                )
            else:
                logger.info(f"业务表就绪（迁移版本：{', '.join(sorted(done))}）")
            # 审计的后台刷盘任务在这里启动。**只在库可用时启动** ——
            # 库不通时它每 2 秒醒一次、每批都失败并打一条 warning，
            # 那不是"尽力而为"，那是刷屏。
            await audit_sink.start()
        else:
            logger.warning(
                "数据库连接不可用 —— 户型与方案只进内存 + Redis（重启即失）。"
                f"检查 POSTGRES_HOST={settings.POSTGRES_HOST} 与 `docker compose ps`。"
            )
    except Exception as e:  # noqa: BLE001 —— 落库检查失败不该让服务起不来
        logger.warning(f"落库初始化检查失败（忽略）：{type(e).__name__}: {e}")

    logger.info(f"服务就绪，端口 {settings.BACKEND_PORT}")
    logger.info("═" * 60)

    yield

    logger.info("收到停机信号，开始优雅停机…")
    # ⚠️ 检索线程池要**先收**：池线程是非 daemon 的，解释器退出时会 join，
    #    而一次检索的超时上限是 180 秒 —— 不主动收，停机就可能被它拖住。
    #    放在任务取消之前，是因为在飞的任务正等着检索返回。
    try:
        from .services.knowledge.retriever import shutdown_retrieval_pool

        shutdown_retrieval_pool()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"检索线程池回收失败（不影响停机）：{type(e).__name__}: {e}")

    # ⚠️ 停机上限 150s 是 `TaskManager.shutdown` 的默认值（AC-31），
    #    配套 `docker-compose.yml` 的 `stop_grace_period: 180s` ——
    #    后者必须**大于**前者，否则 Docker 的硬杀照样发生，等于没做。
    #    这里显式写出来，好让"改停机预算"这件事只有一个明显的入口。
    await get_task_manager().shutdown(timeout=SHUTDOWN_DRAIN_SECONDS)
    # ⚠️ 检查点要在任务**之后**关：排空期间在飞任务还在写检查点。
    await teardown_checkpointer()
    try:
        from .core.redis_client import get_redis

        await get_redis().close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"关闭 Redis 连接失败（忽略）：{type(e).__name__}: {e}")

    # ⚠️ 审计刷盘要在**任务排空之后**：排空期间任务会写终态审计事件，
    #    先停刷盘等于把最后那几条丢掉。丢掉的恰好是"这次停机前排空了
    #    几个任务"这类最想查的记录。
    # ⚠️ 连接池最后关：审计刷盘、以及任务排空期间的户型/方案落库都要用它。
    try:
        from .db import audit_sink, pool

        await audit_sink.stop()
        await pool.close_pool()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"落库资源回收失败（忽略）：{type(e).__name__}: {e}")

    logger.info("已停机")


def create_app() -> FastAPI:
    app = FastAPI(
        title="全友·智绘家 QuanYou Smart HomeCanvas",
        description=(
            "内江市全友家居 · 户型多模态装修推荐系统。\n\n"
            "上传户型图 → 多模态解析 → 多 Agent 生成 3 套方案 → 预算 + 避坑审查。\n\n"
            "**接口约定**：业务错误一律 HTTP 200 + `code != 0`（见 4.4 的说明），"
            "前端 axios 拦截器不必为业务失败弹通用报错。"
        ),
        version="2.3.0",
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────
    # 前端是本地起的 Vue dev server，端口不定，所以开发期放开。
    # ⚠️ 需求文档 10.6 的部署约束是「端口一律绑 127.0.0.1，全部演示在本地完成」，
    # 所以这里放开 CORS 不会造成公网暴露 —— 服务本身就不对外。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                       "http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[TRACE_HEADER, "X-Elapsed-Ms"],
    )

    # ── trace_id 中间件 ────────────────────────────────────
    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        """
        给每个请求一个 trace_id，绑到日志上下文，并回写响应头。

        沿用调用方传入的值（`X-Trace-Id`）—— 这样前端/网关已有链路 id 时
        能直接串起来，而不是各生成各的。

        ⚠️ **必须用 `with_trace_id`，不能用 `bind_trace_id`。**
        前者走 `logger.contextualize()`（基于 contextvars，**并发安全**）；
        后者是 `logger.configure(extra=...)`，改的是**全局**配置 ——
        多个请求并发时 trace_id 会互相覆盖，日志里出现别人的 id
        （而"日志里的 trace_id 不可信"会让整条链路追踪失去意义）。
        `bind_trace_id` 只适用于**异步任务恢复**那种上下文已断的场景。

        另外 contextvars 在 `await call_next(...)` 前设置、能传播进路由处理函数，
        所以这里的上下文管理器写法是有效的。
        """
        trace_id = request.headers.get(TRACE_HEADER) or uuid.uuid4().hex
        request.state.trace_id = trace_id

        started = time.perf_counter()
        with with_trace_id(trace_id):
            response = await call_next(request)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response.headers[TRACE_HEADER] = trace_id
        response.headers["X-Elapsed-Ms"] = str(elapsed_ms)
        # 记进进程内环形缓冲，供 /system/metrics 报 P95（AC-23）。
        # 一次 append，没有 IO —— 热路径上不能有别的动作。
        metrics.record("http", elapsed_ms)

        # 演示期把慢请求记下来，便于发现"哪个接口又变慢了"
        if elapsed_ms > 3000:
            logger.warning(
                f"[slow] {request.method} {request.url.path} {elapsed_ms}ms"
            )
        return response

    # ── 错误翻译 ──────────────────────────────────────────

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        """
        业务错误 → HTTP 200 + 非 0 code（见 api/schemas.py 的模块说明）。

        日志级别按错误码分：4xxx 是**调用方的问题**（参数/数据不足/找不到），
        用 INFO；5xxx 是**服务的问题**，用 ERROR —— 后者才需要在告警里看。
        """
        log = logger.info if exc.code < 5000 else logger.error
        log(f"[api] {exc.code} {exc.msg}")
        return JSONResponse(
            status_code=exc.http_status,
            content=ApiResponse.fail(exc.code, exc.msg, exc.data).model_dump(),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        """
        未捕获异常 → 500，但**仍然套统一外壳**。

        不套外壳的话前端拿到的是 FastAPI 默认的 `{"detail": "Internal Server Error"}`，
        与成功响应的结构完全不同，拦截器要写两套解析逻辑。
        trace_id 一并带出 —— 用户报障时凭它就能定位到日志。
        """
        trace_id = getattr(request.state, "trace_id", "")
        logger.exception(f"[api] 未捕获异常 {request.method} {request.url.path}: {exc}")
        return JSONResponse(
            status_code=500,
            content=ApiResponse.fail(
                5001, "服务内部错误，请携带 trace_id 反馈",
                {"trace_id": trace_id},
            ).model_dump(),
        )

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def root():
        """根路径给个可点的入口，免得打开就 404。"""
        return ApiResponse.ok({
            "name": "全友·智绘家 QuanYou Smart HomeCanvas",
            "docs": "/docs",
            "health": "/api/v1/system/health",
        })

    return app


app = create_app()


__all__ = ["app", "create_app"]
