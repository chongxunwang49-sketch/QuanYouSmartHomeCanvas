"""
业务数据的读写。**这是唯一碰 SQL 的地方**（除了 migrations/）。

调用方只看见「存一份户型」「取一份户型」「存一套方案」「记一批审计事件」，
不看见表名与列名 —— 表结构要改的时候，改动落在这个文件里。

═══════════════════════════════════════════════════════════════════
全部写操作都是 upsert —— 为什么
═══════════════════════════════════════════════════════════════════
两类写入都会重复发生：

  · 户型：同一个 `layout_id` 可能被解析两次（用户重传同一张图）；
  · 方案：同一个户型重跑一次生成，`plan_id` 与上次完全相同
    （它是 `plan_{style}_{grade}`，不含随机成分）。

用 `ON CONFLICT DO UPDATE` 而不是 INSERT，重跑就是**覆盖**而不是报错。
覆盖是有意的：这张表存的是"当前这份数据长什么样"，不是变更历史。
想要历史的话，`audit_logs` 里有每一次任务的时间线 —— 两张表的分工不同。

⚠️ 覆盖的前提是**同一份数据**。`layout_id` 生成的规则里带日期与随机段
（`layout_YYYYMMDD_xxxxxx`），所以"同一个 id 指向不同数据"这件事不会发生。

═══════════════════════════════════════════════════════════════════
审计事件是唯一一处"失败必须被看见"的写入
═══════════════════════════════════════════════════════════════════
其余写操作失败就失败了（记一条 warning，界面照常）—— 户型丢了有 Redis
兜着，方案丢了用户重跑一次。但审计不同：**它的全部意义就是事后能查**。
一条悄悄没写进去的降级记录，会让"昨天有几次降级"这个问题的答案是错的，
而错得没有任何迹象。所以审计的丢弃计数会被 `/system/metrics` 暴露出来。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from . import pool

__all__ = [
    "save_layout", "load_layout", "save_plans", "load_plan",
    "insert_audit_events", "count_audit_events",
    "count_rows", "audit_counts_by_action",
    "save_floor_materials", "load_floor_materials",
]

# ── 户型 ──────────────────────────────────────────────────────────

_UPSERT_LAYOUT = """
INSERT INTO house_layouts (
    id, user_id, parsed_data, diagnosis_data, confidence, model_used,
    degraded, degrade_reason, parse_elapsed_ms, updated_at
) VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, NOW())
ON CONFLICT (id) DO UPDATE SET
    user_id          = COALESCE(EXCLUDED.user_id, house_layouts.user_id),
    parsed_data      = EXCLUDED.parsed_data,
    diagnosis_data   = COALESCE(EXCLUDED.diagnosis_data, house_layouts.diagnosis_data),
    confidence       = EXCLUDED.confidence,
    model_used       = EXCLUDED.model_used,
    degraded         = EXCLUDED.degraded,
    degrade_reason   = EXCLUDED.degrade_reason,
    parse_elapsed_ms = EXCLUDED.parse_elapsed_ms,
    updated_at       = NOW()
"""


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


async def save_layout(
    layout_id: str,
    layout: dict[str, Any],
    *,
    user_id: int | None = None,
    diagnosis: dict[str, Any] | None = None,
    confidence: float | None = None,
    model_used: str | None = None,
    degraded: bool = False,
    degrade_reason: str | None = None,
    parse_elapsed_ms: int | None = None,
) -> bool:
    """
    存/更新一份户型。返回是否写成功。

    `degrade_reason` 会**截断到列宽**（VARCHAR(128)）—— 不截的话，
    一条长句子会让整条写入失败，而丢的是一整份户型。
    截断比丢失好，且截断本身记在日志里。
    """
    if not layout_id:
        return False
    reason = (degrade_reason or "")[:128] or None
    return await pool.execute(_UPSERT_LAYOUT, (
        layout_id, user_id, _json(layout), _json(diagnosis),
        confidence, (model_used or "")[:32] or None,
        bool(degraded), reason, parse_elapsed_ms,
    )) is not None


async def load_layout(layout_id: str) -> dict[str, Any] | None:
    """
    取一份户型。**只返回 `parsed_data`**，与 `store.load()` 的形状对齐 ——
    调用方拿到的是"户型本身"，不是数据库行。

    返回 None 有两种可能（数据库不通 / 确实没有），调用方按同一种方式处理：
    回退到 Redis 或报"户型已过期"。这是有意的简化：在这个读取路径上，
    两种原因对应的动作完全相同。
    """
    if not layout_id:
        return None
    rows = await pool.fetch_all(
        "SELECT parsed_data FROM house_layouts WHERE id = %s", (layout_id,)
    )
    if not rows:
        return None
    data = rows[0]["parsed_data"]
    # psycopg 会把 jsonb 解成 Python 对象；万一驱动配置变了给了字符串，
    # 这里兜一下，免得下游拿到一个 str 然后开始拼字符串。
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None
    return data


# ── 方案 ──────────────────────────────────────────────────────────

_UPSERT_PLAN = """
INSERT INTO design_plans (
    layout_id, plan_id, style, budget_grade, total_budget_min, total_budget_max,
    breakdown, budget_computed_by, risks, environment, materials,
    space_plan, quanyou_products, hotspots, status, updated_at
) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb,
          %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, NOW())
ON CONFLICT (layout_id, plan_id) DO UPDATE SET
    style              = EXCLUDED.style,
    budget_grade       = EXCLUDED.budget_grade,
    total_budget_min   = EXCLUDED.total_budget_min,
    total_budget_max   = EXCLUDED.total_budget_max,
    breakdown          = EXCLUDED.breakdown,
    budget_computed_by = EXCLUDED.budget_computed_by,
    risks              = EXCLUDED.risks,
    environment        = EXCLUDED.environment,
    materials          = EXCLUDED.materials,
    space_plan         = EXCLUDED.space_plan,
    quanyou_products   = EXCLUDED.quanyou_products,
    hotspots           = EXCLUDED.hotspots,
    status             = EXCLUDED.status,
    updated_at         = NOW()
"""


def _plan_row(
    layout_id: str, plan: dict[str, Any], hotspots: Any = None,
) -> tuple[Any, ...]:
    """
    把一套方案摊成一行。

    ⚠️ **金额取的是规则引擎算出来的那几个字段**，不是模型写的字。
    这与 ADR-07 是同一条纪律：能进库的数字必须是可复现的。

    ⚠️ 两处字段名与表列名不同，都是有意为之，不要"顺手对齐"：
      · 表列 `breakdown` ← 运行时的 `budget.lines`（`BudgetBreakdown` 里
        分项明细的字段名就是 `lines`）。改列名会与文档 §13.2 对不上，
        改运行时字段名会牵动 A-04 与前端。
      · 表列 `quanyou_products` ← 材料清单里**只要全友的那部分**，
        而不是整份 `materials.items`。整份已经在 `materials` 列里了，
        这里再存一遍全量等于同一份数据存两处。
    """
    budget = plan.get("budget") or {}
    materials = plan.get("materials") or {}
    items = materials.get("items") or []
    return (
        layout_id,
        str(plan.get("plan_id") or "")[:64],
        str(plan.get("style") or "")[:32],
        str(plan.get("budget_grade") or "")[:16],
        budget.get("total_min"), budget.get("total_max"),
        _json(budget.get("lines")),
        (str(budget.get("computed_by") or "")[:32]) or None,
        _json(plan.get("risks")),
        _json(plan.get("environment")),
        _json(materials),
        # ⚠️ `space_plan` 是 003 补上的 —— 001 漏了它（见那个迁移的说明：
        #    列清单是照着已有的表写的，而不是照着方案的产物写的，
        #    于是"表本身少一格"就看不见）。
        _json(plan.get("space_plan")),
        _json([it for it in items if it.get("is_quanyou")]),
        _json(hotspots),
        str(plan.get("status") or "completed")[:16],
    )


async def save_plans(
    layout_id: str,
    plans: Iterable[dict[str, Any]],
    *,
    hotspots_by_plan: dict[str, Any] | None = None,
) -> int:
    """
    批量存方案。返回成功写入的条数（0 表示一条都没写成）。

    **一条一条写，不包在一个事务里。** 理由：三套方案是三个独立分支的产物，
    一套写失败不该把另外两套一起回滚 —— 部分成功比全无更接近真实情况，
    而"写进去几套"这个数字本身就是要报告给调用方的。

    ⚠️ `hotspots_by_plan` 是**单独传进来的**，不在 `plans[]` 里：
    热区（如果 state 里有）挂在 `state["hotspots"]` 这个 MergeDict 下、
    按 plan_id 分键，而 `plans[]` 是 `assemble_plans` 从 `plan_bundles`
    汇出来的 —— 两者在 state 里根本不在同一个地方。第一版在
    `plan.get("hotspots")` 上取值，那**永远是 None**，而且不会报错，
    只会安静地存一列空值。

    ⚠️ 但要如实说清楚这个参数**目前收不到数据**：实测（2026-09-24，
    `grep -rn '"hotspots"' backend/app`）**没有任何图节点写
    `state["hotspots"]`** —— 它只在 `state.py` 里被声明并初始化成 `{}`。
    热区是 `services/render/hotspots.py` **按需现算**的，由
    `GET /layout/{id}/hotspots` 直接返回。所以 `design_plans.hotspots`
    在这一版流程里**恒为 NULL**（端到端跑完实测确认）。

    保留这个参数不是"留个念想"：它的**形状**是对的（值在 state 的另一处），
    将来真要在终态持久化热区时，改的是调用点而不是这里；
    而如果按第一版那样从 `plan` 里取，那个错误会随代码一起留着。

    ⚠️ 前提是户型必须先在库里：`design_plans.layout_id` 有外键。
    正常流程里解析总是先于生成，所以成立；不成立时这一批会全部写失败，
    返回 0，调用方记 warning。**不做隐式的自动补写** —— 那会掩盖
    "方案比户型先到"这个真实的异常。
    """
    spots = hotspots_by_plan or {}
    rows = [
        _plan_row(layout_id, p, spots.get(str(p.get("plan_id") or "")))
        for p in plans if p
    ]
    ok = 0
    for row in rows:
        if await pool.execute(_UPSERT_PLAN, row) is not None:
            ok += 1
    return ok


# ── 审计 ──────────────────────────────────────────────────────────

#: 列名 → 事件字段名。**这一层映射是刻意的**：表结构稳定，事件字段可以加。
_COLUMN_SOURCES: tuple[tuple[str, str], ...] = (
    ("user_id", "user_id"),
    ("resource_type", "kind"),
    ("resource_id", "task_id"),
    ("model_used", "model_used"),
    ("degrade_reason", "reason"),
    ("ip_address", "ip_address"),
    ("action", "event"),
)

_INSERT_AUDIT = """
INSERT INTO audit_logs (
    action, user_id, resource_type, resource_id, model_used,
    degraded, degrade_reason, token_usage, latency_ms, ip_address, payload
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
"""


def _audit_row(event: str, fields: dict[str, Any]) -> tuple[Any, ...]:
    def pick(name: str) -> Any:
        for column, source in _COLUMN_SOURCES:
            if column == name:
                return fields.get(source)
        return None

    latency = fields.get("latency_ms")
    if latency is None and fields.get("elapsed_seconds") is not None:
        latency = int(float(fields["elapsed_seconds"]) * 1000)

    limit = {"resource_type": 32, "resource_id": 64, "model_used": 32,
             "degrade_reason": 128, "ip_address": 45, "action": 64}

    def cut(name: str, value: Any) -> Any:
        if value is None:
            return None
        text = str(value)
        return text[: limit[name]] or None

    return (
        cut("action", event) or "unknown",
        pick("user_id"),
        cut("resource_type", pick("resource_type")),
        cut("resource_id", pick("resource_id")),
        cut("model_used", pick("model_used")),
        bool(fields.get("degraded", False)),
        cut("degrade_reason", pick("degrade_reason")),
        _json(fields.get("token_usage")),
        latency,
        cut("ip_address", pick("ip_address")),
        # 其余字段原样收进来（见 002 迁移的说明）：username / status 这些
        # 在现有列里没有位置，但它们恰恰是查询时要用的。
        _json(fields or None),
    )


async def insert_audit_events(events: Sequence[tuple[str, dict[str, Any]]]) -> int:
    """
    批量插入审计事件，**一条多值 INSERT**（不是循环单条）。

    审计是异步批量刷盘的（见 `audit_sink.py`），每批几十条。循环单条
    会让每批变成几十次往返；多值 INSERT 只有一次。
    """
    if not events:
        return 0
    rows = [_audit_row(event, fields) for event, fields in events]
    values = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)"] * len(rows))
    flat = [v for row in rows for v in row]
    sql = _INSERT_AUDIT.replace(
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)",
        f"VALUES {values}",
    )
    result = await pool.execute(sql, flat)
    return len(rows) if result is not None else 0


async def load_plan(layout_id: str, plan_id: str) -> dict[str, Any] | None:
    """
    取一套方案。返回一个 dict（不是原始行）——
    调用方要的是"方案长什么样"，不是列名。

    返回 None 有**两种**可能（数据库不通 / 确实没有那个方案），
    而调用方（`/layout/{id}/furniture`）对这两种的处理**相同**：
    4004 并说明不会退回默认。这与"库不通就静默退回旧行为"的总体立场
    不同，是本接口特有的 —— 3D 摆错家具比没有 3D 更糟。
    """
    if not layout_id or not plan_id:
        return None
    # ⚠️ **列名必须与 `design_plans` 的真实列逐字一致。**
    #
    # 这里原来写的是 `SELECT …, space_plan, budget, risks, …` —— 而
    # `design_plans` **没有 `budget` 这一列**（真正的列是
    # `total_budget_min` / `total_budget_max` / `breakdown` /
    # `budget_computed_by`，下面几行 pop 的正是它们）。
    #
    # 后果不是报错，而是**一个看起来很合理的错误**：
    #   psycopg 抛 UndefinedColumn → `pool.fetch_all` 返回 None（它的契约
    #   是"失败返回 None"）→ 这里 `not rows` → 返回 None →
    #   接口回 4004「找不到方案…可能是还没生成过、或数据库暂时不可用」。
    #   而 4004 那句话里还写着"不会退回默认摆放" —— 语气笃定，位置也合理，
    #   于是没有人会去怀疑它。实测 2026-09-24：方案明明在库里，
    #   3D 页面却一直说"找不到方案"，界面上一间空房子。
    #
    # 雪上加霜的是连接池的熔断：一次坏查询会让**整个持久化层停 30 秒**
    # （`_COOLDOWN_SECONDS`），所以这一个字符的错误会连带影响审计落库。
    #
    # 同类的错这是第二次了（上一次是 001 迁移漏了 `space_plan`）。
    # 所以除了改这里，还加了 `tests/test_db_sql_columns.py`：
    # 把本文件里所有 SQL 的列名抽出来，与迁移 DDL 逐字对 —— 这类错误
    # 静态就该抓住，不该等到有人点开 3D 才发现。
    rows = await pool.fetch_all(
        "SELECT plan_id, style, budget_grade, space_plan, risks, "
        "environment, materials, hotspots, total_budget_min, total_budget_max, "
        "breakdown, budget_computed_by FROM design_plans "
        "WHERE layout_id = %s AND plan_id = %s",
        (layout_id, plan_id),
    )
    if not rows:
        return None
    row = dict(rows[0])
    # `budget` 在库里是拆开的几列，拼回运行时那个形状 —— 调用方认得的是
    # 运行时的形状，不是表的形状。两张皮的转换**只在这里做一次**。
    lines = row.pop("breakdown", None)
    row["budget"] = {
        "total_min": float(row.pop("total_budget_min") or 0),
        "total_max": float(row.pop("total_budget_max") or 0),
        "lines": lines or [],
        "computed_by": row.pop("budget_computed_by", None),
    }
    return row


async def count_audit_events(*, action: str | None = None) -> int | None:
    """
    数一下审计事件。**这是"落库到底有没有在工作"的判据** ——
    在此之前 AC-14 的"可查询"只能 grep 文件。

    返回 None 表示数据库不通（与 0 不同：0 是"通了但确实没有"）。
    """
    if action:
        rows = await pool.fetch_all(
            "SELECT count(*) AS n FROM audit_logs WHERE action = %s", (action,)
        )
    else:
        rows = await pool.fetch_all("SELECT count(*) AS n FROM audit_logs")
    if not rows:
        return None
    return int(rows[0]["n"])


#: 表名 → 允许被 `count_rows` 计数的白名单。
#:
#: ⚠️ **白名单不是洁癖。** `count_rows` 的表名要拼进 SQL（表名没法用 %s 占位），
#:    所以它必须有这道门；否则调用方传什么就查什么，那是注入面。
#:    这里只有三张业务表，本来也只需要这三张。
_COUNTABLE_TABLES: frozenset[str] = frozenset({
    "house_layouts", "design_plans", "audit_logs",
})


async def count_rows(table: str) -> int | None:
    """
    数一张业务表的行数。**返回 None = 数据库不通**（与 0 不同）。

    ⚠️ 表名走白名单而不是参数化 —— SQL 的表名位置不能占位。
       白名单之外的名字直接拒绝，而不是拼进去再指望数据库报错。
    """
    if table not in _COUNTABLE_TABLES:
        raise ValueError(
            f"表 {table!r} 不在可计数白名单里：{sorted(_COUNTABLE_TABLES)}"
        )
    rows = await pool.fetch_all(f"SELECT count(*) AS n FROM {table}")  # noqa: S608
    if not rows:
        return None
    return int(rows[0]["n"])


async def audit_counts_by_action() -> dict[str, int] | None:
    """
    审计事件按 `action` 分组计数。返回 None = 数据库不通。

    一次 GROUP BY 拿全，而不是对每个 action 各查一次 ——
    后者在"新加了一个 action"时会静默漏掉它（清单是写死的）。
    """
    rows = await pool.fetch_all(
        "SELECT action, count(*) AS n FROM audit_logs GROUP BY action"
    )
    if rows is None:
        return None
    return {str(r["action"]): int(r["n"]) for r in rows}


# ══════════════════════════════════════════════════════════════════
# 地面材质替换（AC-10）
# ══════════════════════════════════════════════════════════════════
#
# ⚠️ 这一列**不与 `parsed_data` 同路**：那是解析产物（可重放，AC-32 的
#    黄金路径冻结的就是它），这是**用户的选择**。混在一起的话，
#    "重放的输入"与"人选过的东西"就分不开了。理由写在
#    `migrations/004_house_layouts_floor_materials.sql` 的头部。


async def save_floor_materials(layout_id: str, mapping: dict[int, str]) -> bool:
    """
    写一份「房间 → 地面材料」的映射。**整份覆盖**，不做增量合并 ——
    调用方给的就是当前完整状态（见 `store.save_floor_materials`）。

    返回是否写成功。**户型不存在时也返回 False** —— 那说明 layout_id
    是错的，而不是"写成功但没人看"。
    """
    if not layout_id:
        return False
    # JSON 的键只能是字符串；读回来那一侧负责转回 int（见 load）
    payload = json.dumps({str(k): str(v) for k, v in mapping.items()},
                         ensure_ascii=False)
    rowcount = await pool.execute(
        "UPDATE house_layouts SET floor_materials = %s::jsonb, updated_at = NOW() "
        "WHERE id = %s",
        (payload, layout_id),
    )
    # ⚠️ `pool.execute` 的契约里 rowcount 对 DDL 是 -1 —— 这里是 UPDATE，
    #    所以 >0 才代表真的命中了一行。
    return bool(rowcount and rowcount > 0)


async def load_floor_materials(layout_id: str) -> dict[int, str] | None:
    """
    读回「房间 → 地面材料」。**键一定是 int**（见 004 迁移的说明）。

    返回 `None` = 数据库不通；返回 `{}` = 通了但这台户型没有替换记录。
    **两者不能混**：前者调用方要保留缓存里的那份，后者才是"确实没有"。
    """
    if not layout_id:
        return None
    rows = await pool.fetch_all(
        "SELECT floor_materials FROM house_layouts WHERE id = %s", (layout_id,)
    )
    if rows is None:
        return None
    if not rows:
        return {}
    raw = rows[0].get("floor_materials") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[int, str] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = str(v)
        except (TypeError, ValueError):
            # 坏键（理论上不会出现，因为写的时候是我们自己转的字符串）
            # 跳过而不是整份丢弃 —— 丢一份好的比留一个坏键糟得多
            continue
    return out
