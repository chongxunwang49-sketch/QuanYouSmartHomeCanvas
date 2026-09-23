"""
文件路径: backend/app/agents/budget_agent.py
模块职责: A-04 预算造价 —— 规则引擎算钱，LLM 只做文字包装
依赖关系: A-01（layout）-> 本 Agent -> A-05 材料 / fan-in
对应 Skill 文档: skills/04_budget_agent.md
上游: A-00 MasterOrchestrator（经 fan-out 注入 branch_spec）
下游: fan_in_aggregate（与 A-03 并行写入同一 plan_bundles[plan_id]）

对应需求文档:
  - 2.2.2 多 Agent 并行方案生成模块（预算造价 Agent）
  - ADR-07：预算数字由规则引擎计算，LLM 不得生成金额
  - AC-05：输出 ≥ 7 个分项，总价区间合理，且由规则引擎计算
  - 4.2 / 5.2：breakdown 结构与 budget_computed_by 字段

═══════════════════════════════════════════════════════════════════
本 Agent 与 A-01~A-03 的关键区别：失败方向是反的
═══════════════════════════════════════════════════════════════════

A-01/A-03 是**生成型**：LLM 失败 = 没有产物。这是合理的，因为产物本身
就是模型生成的。

A-04 是**计算型**，产物分两层：
    第 1 层 —— 数字，由 services/budget/engine.py 确定性地算出来，零延迟、零依赖
    第 2 层 —— 文字（解说 + 砍价话术），由 LLM 生成，是**装饰**

因此本 Agent 的执行顺序刻意是「先算数、再写字」，且写字那一步：
  - 有**独立的、更短的**超时（20s），不与节点总超时（30s）共用
  - 失败时回退到模板文案，**数字照常返回**

换句话说：**LLM 挂了，用户仍然拿得到一份完整、准确的预算表**，
只是少了那段解说词。反过来的设计（先调 LLM 再算）会让一次网络抖动
白白损失掉本来就不需要网络的那部分价值。

这也让 A-04 成为整条链路上唯一「断网可用」的 Agent —— 值得在演示里讲。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..core.capabilities import (
    OperationNotAllowedError,
    check_operation,
    effective_total_area,
)
from ..graph.state import HomeDecoState
from ..schemas.budget import BudgetBreakdown, BudgetNarrative
from ..services.budget import engine
from .base import BaseAgent

#: 文字包装的独立超时。比节点总超时短 —— 让"写字"先于"算数"被放弃。
#:
#: ⚠️ 2026-09-23：20s → 60s。同 A-05/A-06，20s 是 `LLM_MAX_TOKENS=8000`
#:    时代的数字，提到 32000 后不够用。这一路是三个里输出最短的
#:    （只是一段造价说明），所以给的内层预算也最小，但既然实测纯文本
#:    调用基线已经是 24s，保留 20s 就等于**恒定降级**。
NARRATIVE_TIMEOUT = 60.0

SYSTEM_PROMPT = """你是一名装修预算顾问，负责把一份**已经算好的**预算表讲给业主听。

【最重要的一条：数字不是你的工作】
预算表上的每一个数字都已经由规则引擎算好了，**你不需要也不允许做任何计算**。
你的任务只有一件：把这些数字组织成人话，并给出谈判建议。

- 不要修改、推算、或"补充"任何金额
- 不要心算总和、不要估算比例、不要做加减
- 如果某个信息预算表里没有，就说没有，不要猜

【面向谁说话】
业主不是装修行业的人。用"水电改造这块最容易在后期加钱"这种说法，
而不是"隐蔽工程存在工程量清单外延风险"。说人话，别堆术语。

【砍价话术要具体到能直接用】
✅ 好的例子：「水电按实测结算，要求合同写明单价上限，超出部分由施工方承担」
✅ 好的例子：「管理费按施工费的 5% 计取，写进合同，不接受按总价计取」
❌ 差的例子：「要努力砍价」「多比较几家」

【省钱建议只说做法，不承诺金额】
你说不出"这样能省一万块"——那需要重新算，而算不是你的职责。
可以说「成品柜替代全屋定制」这种做法，让业主自己去判断。

【诚实】
如果预算区间的上下限差距较大，如实说明这是因为工程量存在弹性，
不要假装它很精确。confidence 反映的是**你对这次文字解读的把握**，
不是对数字的把握——数字不是你的功劳，别给它打高分。"""


class BudgetAgent(BaseAgent):
    """A-04：户型 + 分支规格 → 预算明细（数字由规则引擎，文字由 LLM）。"""

    code = "A-04"
    name = "BudgetAgent"
    requires_vision = False
    # ⚠️ 同 A-03：30s 是在 `LLM_MAX_TOKENS=8000` 时代定的。
    #    提到 32000 之后调用变慢，30s 会让方案生成整条链超时
    #    （完整分析见 space_planner.py 里那段注释）。
    #    本 Agent 与 A-03 在同一个 fan-out 里并行，预算应当一致。
    timeout = 90.0

    async def run(self, state: HomeDecoState) -> dict[str, Any]:
        layout = state.get("layout")
        if not layout:
            raise ValueError(
                "状态中缺少 layout，A-04 无法执行。"
                "上游 A-01 可能已失败——请检查 errors 字段"
            )

        # ══ 业务连续性守卫 ══════════════════════════════════════
        # 预算只需要一个数：套内面积。判据比 diagnose/generate_plan 都宽松，
        # 所以完全可能出现「方案出不了但预算能出」——这是合理的能力差异，
        # 不是缺陷。capabilities.py 早就把它们分成了不同的门槛。
        cap = check_operation(layout, "estimate_budget")
        if not cap.allowed:
            raise OperationNotAllowedError("estimate_budget", cap)

        spec = self._branch_spec(state)

        # ══ 第 1 层：算数（纯函数，失败即代码有 bug，不降级）══════
        # 与守卫**共用同一个面积函数**，避免"守卫放行但引擎拿到 0"。
        area = effective_total_area(layout)
        breakdown = engine.calculate(area=area, grade=spec["budget_grade"])

        self.log.info(
            f"预算 {spec['plan_id']} 算得 "
            f"{breakdown.total_min:,.0f}-{breakdown.total_max:,.0f} 元 "
            f"({breakdown.price_per_sqm_min:.0f}-{breakdown.price_per_sqm_max:.0f} 元/㎡, "
            f"{len(breakdown.lines)} 个分项)"
        )

        # ══ 第 2 层：写字（可选，有独立超时，失败即回退模板）══════
        narrative, degraded, reasons = await self._narrate(breakdown, spec)

        payload = {
            **breakdown.model_dump(),
            "narrative": narrative.model_dump(),
            "narrative_degraded": degraded,
        }

        return {
            # 与 A-03 并行写入同一个 plan_id。
            # MergeDict 必须深合并一层，否则两边会互相顶掉（见 state.py）。
            "plan_bundles": {spec["plan_id"]: {"budget": payload}},
            "degraded": degraded,
            "degrade_reasons": (
                [f"[A-04/{spec['plan_id']}] {r}" for r in reasons] if reasons else []
            ),
            "phase": "planning",
            "_llm_meta": getattr(self, "_last_llm_meta", None),
        }

    # ── 分支规格 ──────────────────────────────────────────

    @staticmethod
    def _branch_spec(state: HomeDecoState) -> dict[str, Any]:
        """同 A-03：正常由 fan-out 注入，直调时回退到首项，保证可单跑。"""
        spec = state.get("branch_spec") or {}
        if spec.get("plan_id"):
            return spec

        styles = state.get("styles") or ["modern"]
        grades = state.get("budget_grades") or ["economy"]
        return {
            "plan_id": f"plan_{styles[0]}_{grades[0]}",
            "style": styles[0],
            "budget_grade": grades[0],
            "index": 0,
        }

    # ── 文字包装 ──────────────────────────────────────────

    async def _narrate(
        self, breakdown: BudgetBreakdown, spec: dict[str, Any]
    ) -> tuple[BudgetNarrative, bool, list[str]]:
        """
        让 LLM 把算好的数字讲成人话。

        Returns:
            (narrative, degraded, reasons)

        **任何失败都不向上抛** —— 数字已经拿到了，不能因为写不出解说词
        就让整个预算节点失败。这是本 Agent 与 A-03 最大的差别。
        """
        try:
            parsed, result = await asyncio.wait_for(
                self._call_llm(
                    system=SYSTEM_PROMPT,
                    user=self._build_prompt(breakdown, spec),
                    schema=BudgetNarrative,
                ),
                timeout=NARRATIVE_TIMEOUT,
            )
            assert isinstance(parsed, BudgetNarrative)
            return parsed, result.degraded, (
                [f"预算解说由本地文本模型完成：{result.degrade_reason}"]
                if result.degrade_reason else []
            )

        except asyncio.TimeoutError:
            reason = f"解说生成超时（>{NARRATIVE_TIMEOUT}s），已回退模板文案（数字不受影响）"
            self.log.warning(reason)
            return self._fallback_narrative(breakdown), True, [reason]

        except Exception as e:  # noqa: BLE001 —— 写字失败不该拖垮算数
            reason = (
                f"解说生成失败（{type(e).__name__}: {e}），"
                f"已回退模板文案（数字不受影响）"
            )
            self.log.warning(reason)
            return self._fallback_narrative(breakdown), True, [reason]

    @staticmethod
    def _fallback_narrative(breakdown: BudgetBreakdown) -> BudgetNarrative:
        """
        不依赖任何模型的兜底文案。

        刻意保持朴素：它不假装有观点，只陈述引擎已经确定的事实。
        这样"降级"在用户眼里是"少了一段解读"，而不是"多了一段胡说"。
        """
        labels = "、".join(
            ln.label for ln in breakdown.lines if ln.basis != "subtotal_pct"
        )
        return BudgetNarrative(
            summary=(
                f"本方案为{_GRADE_CN[breakdown.grade]}档，按套内面积 "
                f"{breakdown.area:g}㎡ 估算，施工项目包含：{labels}，"
                f"另按施工费比例计取管理费与设计费。"
            ),
            grade_rationale=(
                "预算文字解读未能生成（模型不可用），此处仅提供由规则引擎"
                "直接给出的数字结果。"
            ),
            cost_drivers=[],
            negotiation_tips=[],
            saving_tips=[],
            warnings=["预算解说部分缺失，请以分项明细中的数字为准。"],
            confidence=0.0,
        )

    @staticmethod
    def _build_prompt(breakdown: BudgetBreakdown, spec: dict[str, Any]) -> str:
        """
        把算好的预算表转成模型易读的文本。

        注意这里**只给数字的文本呈现**，不给任何"请计算"的措辞，
        更不给它金额字段的填写位置（BudgetNarrative 里本来也没有）。

        带上 plan_id 与风格，是为了让三份解说词**各自贴合自己的方案**——
        否则同户型的三个分支只差一个档位，解说词会高度雷同。
        """
        lines = []
        for ln in breakdown.lines:
            if ln.basis == "subtotal_pct":
                lines.append(
                    f"  - {ln.label}：按施工费的 {ln.quantity:g}%，"
                    f"约 {ln.amount_min:,.0f} - {ln.amount_max:,.0f} 元"
                )
            else:
                lines.append(
                    f"  - {ln.label}：{ln.quantity:g} {ln.unit} × "
                    f"{ln.unit_price_min:g}-{ln.unit_price_max:g} 元"
                    f" = {ln.amount_min:,.0f} - {ln.amount_max:,.0f} 元"
                )

        return f"""请把下面这份**已算好**的预算讲给业主听。

【本方案的定位】
  方案 ID：{spec.get('plan_id')}
  设计风格：{_STYLE_CN.get(spec.get('style', ''), spec.get('style') or '（未指定）')}
  预算档位：{_GRADE_CN[breakdown.grade]}（{breakdown.grade}）

【预算概况】
  套内面积：{breakdown.area:g} ㎡
  施工费小计：{breakdown.subtotal_min:,.0f} - {breakdown.subtotal_max:,.0f} 元
  **总价**：{breakdown.total_min:,.0f} - {breakdown.total_max:,.0f} 元
  折合单价：{breakdown.price_per_sqm_min:,.0f} - {breakdown.price_per_sqm_max:,.0f} 元/㎡

【分项明细】
{chr(10).join(lines)}

【数据来源声明】
{breakdown.disclaimer}

请基于以上内容，给出整体说明、成本驱动项、砍价话术与省钱建议。
**再次强调**：不要做任何计算，不要修改任何金额，只负责组织语言。
若上面的数据不足以支撑某条建议，就不要写那一条。"""


_GRADE_CN: dict[str, str] = {
    "economy": "经济",
    "medium": "中档",
    "high": "高端",
}

#: 风格中文名。与 A-03 的 _STYLE_HINTS 同源，这里只取短名供提示词使用。
_STYLE_CN: dict[str, str] = {
    "modern": "现代简约",
    "nordic": "北欧/奶油风",
    "chinese": "现代中式/侘寂",
    "cream": "奶油风",
    "japandi": "日式侘寂+北欧",
    "industrial": "工业风",
}


__all__ = ["BudgetAgent", "NARRATIVE_TIMEOUT"]
