"""
结果一致性（AC-32）：把"两次跑出来的东西一样吗"变成**可计算**的。

═══════════════════════════════════════════════════════════════════
AC-32 的字面要求是没法直接执行的
═══════════════════════════════════════════════════════════════════
原文：「黄金演示路径连续 3 次执行，结果一致性 ≥ 95%」。

这句话缺一个定义：**比什么**。LLM 的输出不是确定性的 ——
同一张图解析两次，墙体数实测是 7 与 8；同一套方案重跑，风险条目的
措辞每次不同。把那些字段也算进分母，就是在用不确定性凑一个好看的
数字：跑出来的"一致性"会长期在门槛附近抖，而它既不上升也不下降，
因为分子里没有一样东西是确定的。

所以本模块把结果切成三层，**分母只取第一层**：

  A 类 · 确定性 —— 纯函数算出来的，给定输入必然逐字节相同。
        **要求 3/3 完全相等，它就是那 95% 的分母。**
        做不到说明有真 bug（本项目的规则引擎与渲染器都承诺了确定性，
        见 services/budget/engine.py 与 services/render/svg.py）。

  B 类 · 结构不变量 —— 内容由 LLM 定，但**形状**必须成立：
        风险类型落在 8 类枚举内、材料 id 都在目录里、环境结论不带浓度数字…
        要求每次成立，**不计入分母**（它没有"比率"可言，只有真假）。

  C 类 · 漂移 —— 会变、且**应该**变的东西（风险条数、文本长度）。
        只记录、只展示，不判失败。它们的用途是让人一眼看到
        "这一版模型比上一版啰嗦了"这类变化，而不是拿来当门槛。

═══════════════════════════════════════════════════════════════════
为什么固定输入是"冻结的解析结果"而不是那张图
═══════════════════════════════════════════════════════════════════
黄金路径的第一步是视觉解析，而它是这条链上**唯一**的不可复现来源。
如果每次重放都从图片开始，那么"结果不一致"就永远说不清是
"下游有 bug"还是"这次解析出的房间数本来就不一样"——
两者混在一个数字里，等于什么都没测出来。

所以重放固定的是 `scripts/fixtures/golden_layout.json`（一份真实解析
产物的存档），从 `stages="generate"` 进入图 —— 也就是 4.3 接口
本来就有的那条入口（它刻意不重新解析，理由见 build_graph 的说明）。

解析本身的不确定性没有被丢掉，它被**单独量**：
`scripts/replay_golden_path.py --reparse` 会跑 N 次解析并报告
房间数/面积的漂移，作为 C 类输出。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, get_args

from ..schemas.risk import RiskType

__all__ = [
    "CLASS_A", "CLASS_B", "CLASS_C",
    "fingerprint", "compare", "consistency_score",
    "RISK_TYPES", "CONCENTRATION_MARKERS",
]

#: 三层的名字。**写在这里而不是散在脚本里** —— 报告、测试、文档
#: 引用的是同一组常量。
CLASS_A = "deterministic"      # 计入一致性分母
CLASS_B = "invariant"          # 必须成立，不占总分
CLASS_C = "drift"              # 只记录

#: 风险类型的合法取值。**从 schema 取，不抄一份。**
#:
#: ⚠️ 第一版在这里**手写了一份英文枚举**（`budget_overrun` / `hidden_cost`…），
#: 而 `schemas/risk.py` 里的 `RiskType` 实际是**中文**的
#: （`增项风险` / `漏项` / `单价异常`…）。于是 B 类那条
#: 「风险类型都在枚举内」会对**每一条真实风险**都判不成立 ——
#: 一个从头到尾只会说"不合格"的检查，看起来在工作，其实什么都没检查。
#:
#: 这类"两份清单各写各的"正是本项目一路在防的漂移。枚举只有一个定义处，
#: 这里引用它。
RISK_TYPES: tuple[str, ...] = get_args(RiskType)

#: 出现这些字样就说明把"推出来的风险档"说成了"量出来的浓度"。
#: 与 `services/environment.py` 的纪律配套 —— 那边不产生浓度，
#: 这边负责确认它没从别处漏进来。
CONCENTRATION_MARKERS: tuple[str, ...] = ("mg/m", "mg／m", "ppm", "µg/m", "ug/m")


def _num(value: Any) -> Any:
    """
    数字归一化。`Decimal` / `int` / `float` 都变 `float` 再比。

    ⚠️ 不归一化的话，`81178.24`（JSON 里回来的 float）与
    `Decimal('81178.24')`（引擎里算出来的）会被判成"不同"，
    而它们在业务上是同一个数 —— 这种假阳性会把一致性分数压下去，
    让人去查一个不存在的 bug。

    ⚠️ **`Decimal` 必须显式列出。** 第一版只写了 `(int, float)`，
    而 `Decimal` 既不是 `int` 也不是 `float`（它继承自 `numbers.Number`
    但不在那两个分支里），于是它会**原样透传** —— 注释说着"都变 float 再比"，
    代码没做。测试当场就红了：`Decimal('81178.24') != 81178.24`。
    这类"文档说了、代码没做"的缺口，正是这条归一化要防的东西本身。

    `bool` 单独拦在数字之前：`True == 1` 在 Python 里成立，
    但那会把两种语义混起来（`quanyou_met: true` 与 `count: 1`）。
    **字符串不转** —— `"88000"` 与 `88000.0` 是不同的表示，
    把它们判等会掩盖真正的类型错误。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return round(float(value), 6)
    return value


def _a_class(result: dict[str, Any]) -> dict[str, Any]:
    """
    确定性字段。**键是扁平的**（`plan_x.budget.total_min`）——
    比对的输出要能直接说出是哪一个字段对不上，而不是"两个大 dict 不同"。
    """
    out: dict[str, Any] = {}
    plans = result.get("plans") or []

    out["plan_ids"] = sorted(str(p.get("plan_id")) for p in plans)
    out["plan_count"] = len(plans)

    for plan in plans:
        pid = str(plan.get("plan_id"))
        budget = plan.get("budget") or {}
        out[f"{pid}.style"] = plan.get("style")
        out[f"{pid}.budget_grade"] = plan.get("budget_grade")
        out[f"{pid}.budget.total_min"] = _num(budget.get("total_min"))
        out[f"{pid}.budget.total_max"] = _num(budget.get("total_max"))
        out[f"{pid}.budget.subtotal_min"] = _num(budget.get("subtotal_min"))
        out[f"{pid}.budget.subtotal_max"] = _num(budget.get("subtotal_max"))
        out[f"{pid}.budget.computed_by"] = budget.get("computed_by")
        # 分项明细：名目与金额都是 area×grade 的纯函数
        out[f"{pid}.budget.lines"] = [
            [str(ln.get("name")), _num(ln.get("min")), _num(ln.get("max"))]
            for ln in (budget.get("lines") or [])
        ]
        # 材料目录版本：它变了，选材的候选池就变了，属于输入的一部分
        mats = plan.get("materials") or {}
        out[f"{pid}.catalog_version"] = mats.get("catalog_version")

    comparison = result.get("comparison") or {}
    out["comparison.available"] = comparison.get("available")
    out["comparison.rows_order"] = [
        str(r.get("plan_id")) for r in (comparison.get("rows") or [])
    ]
    return out


def _b_class(result: dict[str, Any]) -> dict[str, Any]:
    """
    结构不变量。每条的取值是 **bool + 一句依据**，不是数字 ——
    这些是"成立/不成立"，没有"85% 成立"这回事。
    """
    from ..services.material import catalog

    plans = result.get("plans") or []
    known_types = set(RISK_TYPES)
    known_cats = {c["key"] for c in catalog.categories()}
    known_ids = set(catalog.by_id())
    checks: dict[str, Any] = {}

    checks["至少有一套方案"] = bool(plans)
    checks["无错误条目"] = not (result.get("errors") or [])

    for plan in plans:
        pid = str(plan.get("plan_id"))
        risks = plan.get("risks") or {}
        mats = plan.get("materials") or {}
        env = plan.get("environment") or {}
        items = mats.get("items") or []

        checks[f"{pid}.每项产物都在"] = not (plan.get("missing_artifacts") or [])
        checks[f"{pid}.风险类型都在枚举内"] = all(
            str(r.get("type")) in known_types for r in (risks.get("items") or [])
        )
        checks[f"{pid}.材料品类都在目录内"] = all(
            str(it.get("category")) in known_cats for it in items
        )
        # ⚠️ 这条是 A-05 纪律 1（模型只能指认不能创造）的**运行时复核**：
        #    单元测试验的是"代码会剔除幻觉 id"，这里验的是"真跑一遍之后
        #    产物里确实一个都没有"。
        checks[f"{pid}.没有目录外的商品"] = all(
            str(it.get("id")) in known_ids for it in items
        )
        checks[f"{pid}.全友覆盖率达标"] = bool(mats.get("quanyou_met"))
        checks[f"{pid}.环境结论是允许的档位"] = all(
            str((env.get(k) or {}).get("risk")) in ("low", "medium", "high", "unknown")
            for k in ("formaldehyde", "tvoc")
        )
        text = str(plan.get("space_plan") or "") + str(env)
        checks[f"{pid}.环境结论没混进浓度"] = not any(
            m in text for m in CONCENTRATION_MARKERS
        )
    return checks


def _c_class(result: dict[str, Any]) -> dict[str, Any]:
    """会变、且应该变的东西。**只记录**。"""
    out: dict[str, Any] = {}
    for plan in result.get("plans") or []:
        pid = str(plan.get("plan_id"))
        risks = plan.get("risks") or {}
        space = plan.get("space_plan") or {}
        out[f"{pid}.风险条数"] = len(risks.get("items") or [])
        out[f"{pid}.风险类型数"] = int(
            risks.get("distinct_type_count") or 0
        )
        # 只记长度不记内容：内容每次都不同，记下来只会淹没真正的变化
        out[f"{pid}.规划文本长度"] = len(str(space.get("summary") or ""))
    return out


def fingerprint(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把一份结果折成三层指纹。"""
    return {"a": _a_class(result), "b": _b_class(result), "c": _c_class(result)}


def compare(
    baseline: dict[str, Any], current: dict[str, Any],
) -> dict[str, Any]:
    """
    比对两份 `fingerprint()` 的产物。

    Returns:
        `{"a": {...}, "b": {...}, "c": {...}, "score": float, "identical": bool}`

        只有 A 类参与 `score`（见模块说明）。B 类不成立会进 `b.failed`
        并**让 `identical` 为假** —— 它不进分母，但它错了就是错了，
        不能因为"没算进比率"而被忽略。
    """
    a_diffs = {
        k: {"baseline": baseline["a"].get(k), "current": current["a"].get(k)}
        for k in sorted(set(baseline["a"]) | set(current["a"]))
        if baseline["a"].get(k) != current["a"].get(k)
    }
    b_failed = {
        k: False for k in sorted(current["b"]) if not current["b"][k]
    }
    b_new = sorted(set(current["b"]) - set(baseline["b"]))
    c_drift = {
        k: {"baseline": baseline["c"].get(k), "current": current["c"].get(k)}
        for k in sorted(set(baseline["c"]) | set(current["c"]))
        if baseline["c"].get(k) != current["c"].get(k)
    }

    total = len(set(baseline["a"]) | set(current["a"]))
    matched = total - len(a_diffs)
    return {
        "a": {"total": total, "matched": matched, "diffs": a_diffs},
        "b": {"failed": b_failed, "new_keys": b_new},
        "c": {"drift": c_drift},
        "score": (matched / total) if total else 1.0,
        # A 类全等 **且** B 类全部成立 —— 两个条件都要
        "identical": not a_diffs and not b_failed and not b_new,
    }


def consistency_score(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    把 N 次重放的指纹折成一个分数。以**第一次**为基线。

    ⚠️ 基线取第一次而不是"两两比"：两两比会把 N 次放大成 N² 次比对，
    而且报出来的分数没有单一含义（是平均？是最差？）。
    取第一次当基线还有个演示上的好处 —— **第一次跑出来的东西就是
    基线**，不需要预先维护一份"正确答案"文件，也就不会过期。
    """
    if not runs:
        return {"runs": 0, "score": 0.0, "identical": False,
                "reason": "没有可比的运行结果"}
    base = runs[0]
    reports = [compare(base, r) for r in runs[1:]]
    if not reports:
        return {"runs": 1, "score": 1.0, "identical": True, "reports": []}

    # 总分数用"匹配字段数 / 总字段数"而不是"各次分数的平均"：
    # 后者在分母不同的运行之间做平均，含义是含混的。
    total = sum(r["a"]["total"] for r in reports)
    matched = sum(r["a"]["matched"] for r in reports)
    return {
        "runs": len(runs),
        "baseline_keys": len(base["a"]),
        "compared_fields": total,
        "score": (matched / total) if total else 1.0,
        "identical": all(r["identical"] for r in reports),
        "reports": reports,
    }
