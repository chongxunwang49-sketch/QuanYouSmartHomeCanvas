"""
预算规则引擎 —— ADR-07 的落地实现。

    total = Σ(工程量 × 单价) × 地区系数 + Σ(施工费小计 × 比例项)

═══════════════════════════════════════════════════════════════════
本模块的三条硬约束
═══════════════════════════════════════════════════════════════════

【1】不导入任何 LLM 相关模块。
    这不是代码风格要求，是 ADR-07（「LLM 不得生成金额」）的执行方式：
    如果这个模块连 `llm_client` 都导不进来，它就不可能在某次"顺手优化"里
    悄悄调用模型。`tests/test_budget_engine.py` 里有一条测试直接读本文件的
    源码断言这件事 —— **用不变量代替自觉**。

【2】面积无效时**拒绝计算**，绝不返回 0。
    `calc_budget(area=0)` 返回一个 ¥0 的预算是本项目最危险的失败形态之一：
    它不报错、HTTP 200、看起来正常，而用户会拿它去和装修公司谈。
    这与 capabilities.py 的立论完全一致，所以在引擎内部**再兜一道** ——
    即使调用方忘了先过守卫，也拿不到那个假数字。

【3】所有数字都是确定性的。
    同样的入参永远得到同样的结果，不引入随机、不读时间、不依赖网络。
    AC-05 要求「预算由规则引擎计算（非 LLM 生成）」，可测性正是它的回报。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from ...schemas.budget import BudgetBreakdown, BudgetGrade, BudgetLine

#: 项目根目录：engine.py → budget → services → app → backend → ROOT
_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_PRICING = _ROOT / "seed_data" / "pricing_demo.json"

#: 分项数下限。AC-05 要求「输出 ≥ 7 个分项」，这里把它变成可执行的断言。
MIN_BUDGET_LINES = 7

_VALID_GRADES: tuple[str, ...] = ("economy", "medium", "high")


class PricingDataError(RuntimeError):
    """价格表缺失或格式不对。属于部署问题，不是用户输入问题。"""


class InvalidAreaError(ValueError):
    """
    面积无效，拒绝计算。

    单独定义一个异常类型，是为了让调用方能把它和"参数写错了"区分开：
    这个异常代表**业务上就不该算**，应转成面向用户的 400 + 说明，
    而不是 500。
    """


# ══════════════════════════════════════════════════════════════════
# 价格表加载
# ══════════════════════════════════════════════════════════════════


@lru_cache(maxsize=4)
def load_pricing(path: str | None = None) -> dict[str, Any]:
    """
    读取价格表。带缓存 —— 引擎会被每个方案分支调一次，不必反复读盘。

    缓存键是路径字符串（可哈希）。传 None 时用默认路径。
    """
    target = Path(path) if path else _DEFAULT_PRICING
    if not target.exists():
        raise PricingDataError(f"价格表不存在: {target}")

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PricingDataError(f"价格表不是合法 JSON: {target} —— {e}") from e

    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise PricingDataError(f"价格表缺少 items 数组: {target}")

    missing = [g for g in _VALID_GRADES if not any(
        g in (it.get("unit_price") or {}) for it in items
    )]
    if missing:
        raise PricingDataError(f"价格表缺少档位 {missing} 的单价: {target}")

    return data


def grade_ranges(path: str | None = None) -> dict[str, tuple[float, float]]:
    """
    取文档定义的三档总价区间（元/㎡）。

    供测试断言"按本表算出的总价是否仍在文档口径内"。把它做成公开函数，
    是为了让那条断言不必去猜价格表的结构。
    """
    meta = load_pricing(path).get("_meta", {})
    raw = meta.get("grade_ranges_cny_per_sqm", {})
    return {g: (float(v[0]), float(v[1])) for g, v in raw.items() if len(v) == 2}


# ══════════════════════════════════════════════════════════════════
# 主计算
# ══════════════════════════════════════════════════════════════════


def _round(x: float) -> float:
    """统一保留 2 位小数，与 DB 的 DECIMAL(12,2) 对齐。"""
    return round(x + 0.0, 2)


def calculate(
    *,
    area: float,
    grade: str,
    pricing_path: str | None = None,
    wall_area_factor: float | None = None,
    region_coefficient: float | None = None,
) -> BudgetBreakdown:
    """
    按面积与档位计算预算。**纯函数，无 IO、无副作用、无 LLM。**

    Args:
        area: 套内面积（㎡）。必须 > 0，否则抛 InvalidAreaError。
        grade: economy / medium / high。
        pricing_path: 覆盖价格表路径（测试用）。
        wall_area_factor: 覆盖墙面面积折算系数，默认取价格表。
        region_coefficient: 覆盖地区系数，默认取价格表。

    Returns:
        BudgetBreakdown，含逐项明细与总价区间。
    """
    # ── 约束 2：面积无效就拒绝，不给 ¥0 ──────────────────
    if not isinstance(area, (int, float)) or area <= 0:
        raise InvalidAreaError(
            f"套内面积无效（{area!r}），拒绝估算预算。"
            "一个 ¥0 的预算比「预算无法计算」危险得多——用户会当真。"
        )

    if grade not in _VALID_GRADES:
        raise ValueError(f"未知预算档位 {grade!r}，应为 {_VALID_GRADES} 之一")

    data = load_pricing(pricing_path)
    meta = data.get("_meta", {})

    wall_factor = (
        float(wall_area_factor) if wall_area_factor is not None
        else float(meta.get("wall_area_factor", 2.6))
    )
    region = (
        float(region_coefficient) if region_coefficient is not None
        else float(meta.get("region_coefficient", 1.0))
    )

    # ── 第一阶段：按工程量计价的分项 ──────────────────────
    lines: list[BudgetLine] = []
    pending_pct: list[dict[str, Any]] = []

    for item in data["items"]:
        basis = item.get("basis")
        if basis == "subtotal_pct":
            pending_pct.append(item)
            continue

        if basis == "area":
            quantity = float(area)
        elif basis == "wall_area":
            quantity = float(area) * wall_factor
        else:
            raise PricingDataError(f"分项 {item.get('key')} 的 basis 未知: {basis!r}")

        lo, hi = (float(x) for x in item["unit_price"][grade])
        lines.append(BudgetLine(
            key=item["key"],
            label=item["label"],
            unit=item["unit"],
            basis=basis,
            quantity=_round(quantity),
            unit_price_min=lo,
            unit_price_max=hi,
            amount_min=_round(quantity * lo * region),
            amount_max=_round(quantity * hi * region),
            note=item.get("note", ""),
        ))

    subtotal_min = _round(sum(ln.amount_min for ln in lines))
    subtotal_max = _round(sum(ln.amount_max for ln in lines))

    # ── 第二阶段：按施工费小计的比例项 ────────────────────
    # 比例项在**小计之后**算，且彼此不叠加（管理费不以"含设计费"的金额为基数）。
    # 若按后者算会出现"管理费吃设计费"的复利效应，与装修公司实际计法不符。
    for item in pending_pct:
        pct_lo, pct_hi = (float(x) for x in item["unit_price"][grade])
        lines.append(BudgetLine(
            key=item["key"],
            label=item["label"],
            unit=item["unit"],
            basis="subtotal_pct",
            quantity=_round(pct_lo if pct_lo == pct_hi else (pct_lo + pct_hi) / 2),
            unit_price_min=pct_lo,
            unit_price_max=pct_hi,
            amount_min=_round(subtotal_min * pct_lo / 100),
            amount_max=_round(subtotal_max * pct_hi / 100),
            note=item.get("note", ""),
        ))

    total_min = _round(subtotal_min + sum(ln.amount_min for ln in lines if ln.basis == "subtotal_pct"))
    total_max = _round(subtotal_max + sum(ln.amount_max for ln in lines if ln.basis == "subtotal_pct"))

    return BudgetBreakdown(
        grade=grade,  # type: ignore[arg-type]
        area=_round(float(area)),
        region_coefficient=region,
        lines=lines,
        subtotal_min=subtotal_min,
        subtotal_max=subtotal_max,
        total_min=total_min,
        total_max=total_max,
        price_per_sqm_min=_round(total_min / area),
        price_per_sqm_max=_round(total_max / area),
        computed_by=str(meta.get("engine_version", "rule_engine_v1")),
        data_source="seed_data/pricing_demo.json",
        disclaimer=str(meta.get("disclaimer", "")),
    )


def estimate_unit_price(area: float, grade: str) -> tuple[float, float]:
    """便捷函数：只要折合单价区间（元/㎡），供需要快速比对的场景使用。"""
    result = calculate(area=area, grade=grade)
    return result.price_per_sqm_min, result.price_per_sqm_max


__all__ = [
    "calculate", "load_pricing", "grade_ranges", "estimate_unit_price",
    "MIN_BUDGET_LINES", "PricingDataError", "InvalidAreaError",
]
