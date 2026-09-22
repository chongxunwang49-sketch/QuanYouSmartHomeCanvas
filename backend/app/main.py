"""
FastAPI 应用入口。

    uvicorn backend.app.main:app --reload --port 8000

═══════════════════════════════════════════════════════════════════
这个文件只做四件事
═══════════════════════════════════════════════════════════════════
1. **生命周期**：启动时校验依赖、停机时取消在飞任务（ADR-13）
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
from .api.tasks import get_task_manager
from .core.config import settings
from .core.logger import with_trace_id

#: trace_id 的请求头名。前端/网关传入则沿用，便于跨服务串联。
TRACE_HEADER = "X-Trace-Id"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    启动与停机。

    停机这段是 ADR-13 的落地：**先停止接收新任务，再取消在飞的**。
    顺序反过来的话，停机过程中新到的请求仍会创建任务，而那些任务没人管 ——
    用户会看到一个永远停在 processing 的任务。
    """
    logger.info("═" * 60)
    logger.info("全友·智绘家 后端启动中…")

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

    logger.info(f"服务就绪，端口 {settings.BACKEND_PORT}")
    logger.info("═" * 60)

    yield

    logger.info("收到停机信号，开始优雅停机…")
    await get_task_manager().shutdown()
    try:
        from .core.redis_client import get_redis

        await get_redis().close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"关闭 Redis 连接失败（忽略）：{type(e).__name__}: {e}")
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
    # ⚠️ 需求文档 5.5 的部署约束是「端口一律绑 127.0.0.1，全部演示在本地完成」，
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
