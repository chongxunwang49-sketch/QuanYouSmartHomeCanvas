"""
HTTP 路由。

对应需求文档第四章的接口契约。当前实现：

    POST /api/v1/layout/parse          4.2 户型解析（异步）
    POST /api/v1/design/generate       4.3 方案生成（异步）
    POST /api/v1/avoid-pit/review      4.6 报价单/合同审查（异步）
    GET  /api/v1/task/{task_id}/status 4.4 任务状态轮询
    GET  /api/v1/material/price        4.5 材料价格查询（同步，纯查表）
    GET  /api/v1/system/health         4.6 健康检查

**四个异步接口的形状是一致的**：立即返回 task_id，客户端轮询 4.4。
差别只在 `kind` 与起始 `stages`。

未实现（依赖 M1 认证与本机服务）：4.1 登录、4.6 的 knowledge/upload、
/system/metrics。见 README 的「未开始」。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from loguru import logger

from ..core.capabilities import OperationNotAllowedError
from ..core.config import settings
from ..graph.state import initial_state
from ..graph.workflow import get_compiled_graph
from ..services.material import catalog
from . import store as layout_store
from .schemas import (
    ApiError,
    ApiResponse,
    GenerateRequest,
    ParseRequest,
    ReviewRequest,
)
from .tasks import get_task_manager

router = APIRouter(prefix="/api/v1")


def _trace_id(request: Request) -> str:
    """取中间件写进 request.state 的 trace_id。"""
    return getattr(request.state, "trace_id", "") or ""


# ══════════════════════════════════════════════════════════════════
# 4.2 户型解析
# ══════════════════════════════════════════════════════════════════


@router.post("/layout/parse", response_model=ApiResponse)
async def parse_layout(req: ParseRequest, request: Request) -> ApiResponse:
    """
    提交户型图解析。**立即返回 task_id**，结果经 `/task/{id}/status` 轮询。

    只跑「解析 → 诊断」这一段（约 40 秒）。方案生成是另一个接口 ——
    在这里顺带跑完会多花 90 秒、多调十几次 LLM，而调用方只要那份户型 JSON。
    """
    if not req.image.strip():
        raise ApiError(4001, "image 不能为空")

    tm = get_task_manager()
    rec = tm.create("parse", trace_id=_trace_id(request))

    state = initial_state(
        task_id=rec.task_id,
        trace_id=rec.trace_id,
        image_ref=req.image,
        image_media_type=req.image_media_type,
        detail_level=req.detail_level,
        prefer_local=req.prefer_local,
    )
    tm.start(rec, state, stages="parse")

    return ApiResponse.ok({
        "task_id": rec.task_id,
        "trace_id": rec.trace_id,
        "status": "processing",
        "poll": f"/api/v1/task/{rec.task_id}/status",
        # 前端据此设计轮询节奏（2.2.4）；给个参考值省得它自己猜
        "estimated_seconds": 40,
    })


# ══════════════════════════════════════════════════════════════════
# 4.3 方案生成
# ══════════════════════════════════════════════════════════════════


@router.post("/design/generate", response_model=ApiResponse)
async def design_generate(req: GenerateRequest, request: Request) -> ApiResponse:
    """
    按 `layout_id` 生成方案。**立即返回 task_id**。

    ⚠️ **不重新解析户型图。** 视觉解析约 16 秒，且模型有随机性 ——
    同一个 `layout_id` 重新解析可能得到不同的房间数。用户会发现自己看的户型
    和生成方案用的不是同一份，而界面上写着同一个 id。
    **同一个 id 必须指同一份数据。**

    所以这里从暂存里取户型，图从 `diagnose_layout` 进（跳过已做过的解析）。
    """
    layout = await layout_store.load(req.layout_id)
    if layout is None:
        raise ApiError(
            4004,
            f"找不到 layout_id={req.layout_id} 的户型。"
            f"可能已过期（暂存 1 小时），请重新解析。",
        )

    # 能力守卫：数据不支撑方案生成时**在入口就拦下**，不放进图里
    # 让三个分支各撞一次墙（capabilities.py 的立场）
    from ..core.capabilities import check_operation

    cap = check_operation(layout, "generate_plan")
    if not cap.allowed:
        raise ApiError(4002, cap.reason,
                       data={"missing": cap.missing, "suggestion": cap.suggestion})

    if not req.styles or not req.budget_grades:
        raise ApiError(4001, "styles 与 budget_grades 都不能为空")

    tm = get_task_manager()
    rec = tm.create("generate", trace_id=_trace_id(request))

    state = initial_state(
        task_id=rec.task_id,
        trace_id=rec.trace_id,
        # 关键：把已解析的户型直接放进状态，图从诊断进
        layout=layout,
        layout_id=req.layout_id,
        styles=req.styles,
        budget_grades=req.budget_grades,
        quanyou_priority=req.quanyou_priority,
        requirements=req.requirements or {},
    )
    tm.start(rec, state, stages="generate")

    return ApiResponse.ok({
        "task_id": rec.task_id,
        "trace_id": rec.trace_id,
        "status": "processing",
        "poll": f"/api/v1/task/{rec.task_id}/status",
        "plan_count": min(len(req.styles), len(req.budget_grades)),
        "estimated_seconds": 100,
    })


# ══════════════════════════════════════════════════════════════════
# 4.6 报价单/合同审查
# ══════════════════════════════════════════════════════════════════


@router.post("/avoid-pit/review", response_model=ApiResponse)
async def avoid_pit_review(req: ReviewRequest, request: Request) -> ApiResponse:
    """
    审查一份报价单/合同。**立即返回 task_id**。

    走的是 A-06 的 quote 模式（唯一模式 —— 分支内的 plan 模式由链上自动触发）。
    """
    if not req.quote_text.strip():
        raise ApiError(4001, "quote_text 不能为空")

    tm = get_task_manager()
    rec = tm.create("review", trace_id=_trace_id(request))

    state = initial_state(
        task_id=rec.task_id,
        trace_id=rec.trace_id,
        quote_text=req.quote_text,
        requirements=req.requirements or {},
    )
    # 审查不依赖户型，独立跑 A-06 一个节点（复用完整图的话会缺 layout 而失败）
    tm.start(rec, state, stages="review")

    return ApiResponse.ok({
        "task_id": rec.task_id,
        "trace_id": rec.trace_id,
        "status": "processing",
        "poll": f"/api/v1/task/{rec.task_id}/status",
        "estimated_seconds": 25,
    })


# ══════════════════════════════════════════════════════════════════
# 4.4 任务状态轮询
# ══════════════════════════════════════════════════════════════════


@router.get("/task/{task_id}/status", response_model=ApiResponse)
async def task_status(task_id: str, request: Request) -> ApiResponse:
    """
    查询任务状态。

    ⚠️ **未完成时同样返回 200 + code=0**，只是 `status` / `phase` / `progress` 不同。
    用 4xx 表示"还没好"会被前端 axios 拦截器误判为错误（4.4 明确要求）。

    响应标 `Cache-Control: no-store` —— 进度必须实打实地拿到最新值，
    中间层缓存会让界面卡在某个阶段不动（4.4）。
    """
    from fastapi.responses import JSONResponse

    data = await get_task_manager().status(task_id)
    if data is None:
        raise ApiError(4004, f"任务不存在或已过期（结果保留 1 小时）：{task_id}")

    resp = JSONResponse(content=ApiResponse.ok(data).model_dump())
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ══════════════════════════════════════════════════════════════════
# 4.5 材料价格查询（同步，纯查表）
# ══════════════════════════════════════════════════════════════════


@router.get("/material/price", response_model=ApiResponse)
async def material_price(
    category: str = "",
    brand: str = "",
    quanyou_first: bool = True,
) -> ApiResponse:
    """
    材料价格查询。

    **同步接口**：纯查演示目录，不调模型、不查向量库，毫秒级返回。

    返回结构对齐 4.5 的契约：`quanyou_recommended`（全友自有）+ `items`（其它品牌）。
    **一定要带上 `disclaimer`** —— 需求文档 5.3 与 R-09 要求演示数据必须显著标注，
    而这正是最容易被前端"顺手美化"掉的地方。
    """
    cats = {c["key"]: c for c in catalog.categories()}
    if category and category not in cats:
        raise ApiError(4001, f"未知品类 {category}；可用：{sorted(cats)}")

    products = [
        p for p in catalog.all_products()
        if (not category or p.category == category)
        and (not brand or brand in p.brand)
    ]

    def row(p: catalog.Product) -> dict[str, Any]:
        return {
            "id": p.id, "name": p.name, "brand": p.brand,
            "category": p.category,
            "category_label": cats.get(p.category, {}).get("label", p.category),
            "spec": p.spec,
            "price_range": list(p.price_range),
            "unit": cats.get(p.category, {}).get("unit", ""),
            "eco_level": p.eco_level,
            "search_url": p.search_url,
        }

    qy = sorted([row(p) for p in products if p.is_quanyou],
                key=lambda r: r["price_range"][0])
    other = sorted([row(p) for p in products if not p.is_quanyou],
                   key=lambda r: r["price_range"][0])

    return ApiResponse.ok({
        "category": category or "all",
        "category_name": cats.get(category, {}).get("label", "全部品类"),
        # quanyou_first=False 时只给市场对照，不推自有品牌
        "quanyou_recommended": qy if quanyou_first else [],
        "items": other,
        "total": len(qy) + len(other),
        "catalog_version": catalog.catalog_version(),
        "disclaimer": catalog.disclaimer(),
    })


# ══════════════════════════════════════════════════════════════════
# 4.6 健康检查
# ══════════════════════════════════════════════════════════════════


@router.get("/system/health", response_model=ApiResponse)
async def system_health() -> ApiResponse:
    """
    健康检查。**每个依赖单独报告，且整体永远返回 200。**

    为什么不整体返回 503：这是一个**演示与开发用**的服务，
    知识库没起来时前端仍应能打开页面、看到"知识库不可用"的提示，
    而不是拿到一个连不上的服务。哪个依赖挂了由 `checks` 说清楚。
    """
    from ..core.redis_client import get_redis
    from ..services.knowledge import store as knowledge_store

    tm = get_task_manager()
    checks: dict[str, Any] = {}

    # ── LLM 主模型 ──
    checks["deepseek"] = {
        "ok": bool(settings.DEEPSEEK_API_KEY),
        "detail": "已配置" if settings.DEEPSEEK_API_KEY else "未配置 DEEPSEEK_API_KEY，将走本地 Ollama",
    }

    # ── Redis ──
    try:
        ok = await get_redis().ping()
        checks["redis"] = {"ok": bool(ok),
                           "detail": "可用" if ok else "不可用（进度退化为内存）"}
    except Exception as e:  # noqa: BLE001
        checks["redis"] = {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    # ── 知识库 ──
    try:
        info = knowledge_store.collection_info()
        checks["knowledge"] = {
            "ok": info.available and info.count > 0,
            "detail": (f"{info.count} 条 chunk"
                       if info.available else info.reason),
        }
    except Exception as e:  # noqa: BLE001
        checks["knowledge"] = {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    # ── 材料目录 ──
    try:
        n = len(catalog.all_products())
        checks["material_catalog"] = {"ok": n > 0, "detail": f"{n} 个商品"}
    except Exception as e:  # noqa: BLE001
        checks["material_catalog"] = {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    return ApiResponse.ok({
        "status": "ok" if all(c["ok"] for c in checks.values()) else "degraded",
        "checks": checks,
        "running_tasks": tm.list_running(),
        "version": settings.APP_VERSION if hasattr(settings, "APP_VERSION") else "dev",
    })


__all__ = ["router"]
