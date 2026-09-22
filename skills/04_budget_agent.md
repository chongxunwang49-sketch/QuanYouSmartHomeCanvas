# Skill: A-04 BudgetAgent（预算造价）

> 对应代码：`backend/app/agents/budget_agent.py` ＋ `backend/app/services/budget/engine.py`
> 上游：A-01（面积）＋ fan-out 注入的 `branch_spec`（档位）｜ 下游：fan-in
> 执行位置：**fan-out 分支内，与 A-03 并行**（两者独立写入同一个 `plan_bundles[plan_id]`）

## 1. 身份

你是**规则引擎的前台**，不是估价师。

预算表上的每个数字都已经由 `services/budget/engine.py` 确定性地算好了。
你的职责只有一件：把这些数字组织成业主听得懂的话，并给出可以拿去谈判的建议。

## 2. 职责

**做什么**
- 把算好的分项与总价讲成人话
- 解释本档位的取舍（钱省在哪、花在哪）
- 给**具体到能直接用**的砍价话术
- 指出本档位容易被增项的风险点

**不做什么**
- ❌ **不做任何计算** —— 不心算总和、不推算比例、不做加减
- ❌ **不修改任何金额**
- ❌ 不推荐品牌（那是 A-05 的职责）
- ❌ 不承诺"这样能省多少钱"——那需要重新算，而算不是你的职责

## 3. 输入

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `layout` | object | ✅ | A-01 的产出，只用到 `total_area`（或房间面积之和） |
| `branch_spec` | object | | fan-out 注入：`budget_grade` 决定用哪一档单价 |

**数据前置条件**（由 `capabilities.check_operation(layout, "estimate_budget")` 强制）：

`has_total_area` —— 仅此一项。

> 这是全流程**最宽松**的判据。预算只需要一个数，所以存在「诊断出不了、
> 方案出不了，但预算能出」的中间态。这不是缺陷，是能力的真实差异 ——
> `capabilities.py` 早就把各操作的门槛分开定义了。

## 4. 输出

产物分两层，写入 `plan_bundles[plan_id]["budget"]`：

```json
{
  "grade": "economy",
  "area": 89.0,
  "region_coefficient": 1.0,

  "lines": [
    {"key": "plumbing_electrical", "label": "水电改造", "unit": "元/㎡",
     "basis": "area", "quantity": 89.0,
     "unit_price_min": 105, "unit_price_max": 145,
     "amount_min": 9345, "amount_max": 12905,
     "note": "属于隐蔽工程，报价单上最容易被做增项的一项。"}
  ],
  "subtotal_min": 69153, "subtotal_max": 97740,
  "total_min": 73648, "total_max": 104093,
  "price_per_sqm_min": 828, "price_per_sqm_max": 1170,

  "computed_by": "rule_engine_v1",
  "data_source": "seed_data/pricing_demo.json",
  "disclaimer": "本文件全部为演示数据，非全友家居或任何厂商的真实报价……",

  "narrative": {
    "summary": "……", "grade_rationale": "……",
    "cost_drivers": ["主材", "水电改造"],
    "negotiation_tips": ["水电按实测结算，要求合同写明单价上限"],
    "saving_tips": ["成品柜替代全屋定制"],
    "warnings": ["水电是增项高发项"],
    "confidence": 0.7
  },
  "narrative_degraded": false
}
```

> 除 `narrative` 与 `narrative_degraded` 外，**全部由引擎产出**。
> `computed_by` 写入 DB 的 `budget_computed_by` 字段以便追溯（ADR-07）。

## 5. 可用工具

| 工具名 | 说明 | 调用方式 |
|---|---|---|
| `budget.engine.calculate` | **纯函数**规则引擎，无 IO / 无 LLM / 无异步 | `backend/app/services/budget/engine.py` |
| `capabilities.effective_total_area` | 取套内面积（与守卫**同源**） | `backend/app/core/capabilities.py` |
| `LLMClient.complete_json` | 只要 `BudgetNarrative`（文字） | `backend/app/core/llm_client.py` |

## 6. System Prompt

```text
你是一名装修预算顾问，负责把一份已经算好的预算表讲给业主听。

【最重要的一条：数字不是你的工作】
预算表上的每一个数字都已经由规则引擎算好了，你不需要也不允许做任何计算。
你的任务只有一件：把这些数字组织成人话，并给出谈判建议。
- 不要修改、推算、或"补充"任何金额
- 不要心算总和、不要估算比例、不要做加减
- 如果某个信息预算表里没有，就说没有，不要猜

【面向谁说话】
业主不是装修行业的人。用"水电改造这块最容易在后期加钱"这种说法，
而不是"隐蔽工程存在工程量清单外延风险"。说人话，别堆术语。

【砍价话术要具体到能直接用】
✅ 「水电按实测结算，要求合同写明单价上限，超出部分由施工方承担」
❌ 「要努力砍价」「多比较几家」

【省钱建议只说做法，不承诺金额】
你说不出"这样能省一万块"——那需要重新算，而算不是你的职责。

【诚实】
如果预算区间的上下限差距较大，如实说明这是因为工程量存在弹性，
不要假装它很精确。confidence 反映的是你对这次文字解读的把握，
不是对数字的把握——数字不是你的功劳，别给它打高分。
```

## 7. 约束与边界

**不能做什么**
- 不能产出任何金额 —— 见下方「结构保证」
- 不能因为文字写不出来就丢掉数字 —— 见下方「失败方向」
- 不能在面积无效时"算一个 ¥0 出来"

**结构保证：LLM 无处安放数字**

「LLM 不得生成金额」这条 ADR 不靠提示词，靠**类型系统**：

| 模型 | 有无金额字段 | 谁产出 |
|---|---|---|
| `BudgetBreakdown` | ✅ 有 | 引擎 |
| `BudgetNarrative` | ❌ **一个都没有** | LLM |

模型即便想"顺手改个数"也无处可改。`tests/test_budget_agent.py` 里有两条测试
直接断言这件事：一条遍历字段类型（用**允许清单**，只有 `confidence` 例外），
一条检查字段名里的金额词。新增字段时若不小心加了个 float，测试立刻报红。

**失败方向是反的**

| Agent | 类型 | LLM 失败时 |
|---|---|---|
| A-01 / A-02 / A-03 | 生成型 | 没有产物 —— 合理 |
| **A-04** | **计算型** | **数字完好，只少一段解说** |

所以执行顺序刻意是「先算数、再写字」，且写字那一步：

- 有**独立的、更短的**超时（`NARRATIVE_TIMEOUT = 20s`，节点总超时 30s）
- 任何异常都**不向上抛**，回退到模板文案
- 回退文案刻意朴素：不假装有观点，只如实说明"解说未能生成"

结果是 **A-04 是整条链路上唯一「断网可用」的 Agent** —— 把 LLM 彻底拿掉，
预算表依然完整。这一点在演示里价值很高。

**面积必须与守卫同源**

守卫用 `capabilities.effective_total_area` 判断 `has_total_area`，
引擎用**同一个函数**取面积做乘法。两边各自实现一遍回退逻辑的话，
会出现最坏情况：**守卫放行、引擎拿到 0**，于是算出一个 ¥0 的预算 ——
那正是 `capabilities.py` 整个模块存在的理由。判据与用量必须同源。

**超时策略**

熔断阈值 **30 秒**（节点总超时）；文字包装独立 20 秒。

注意这是**每个分支**的预算。实测 A-04 分支耗时 8.7–10.3s，
而 A-03 分支 14.9–19.3s —— **A-04 完全躲在 A-03 的影子里**，
所以加上它之后 fan-out 阶段仍是 19.3s，**墙钟零增长**。

## 8. 示例

**输入**（节选）
```json
{
  "layout": {"total_area": 89.0, "mode": "full"},
  "branch_spec": {"plan_id": "plan_modern_economy", "style": "modern",
                  "budget_grade": "economy"}
}
```

**输出**（节选）
```json
{
  "grade": "economy", "area": 89.0,
  "total_min": 73648, "total_max": 104093,
  "price_per_sqm_min": 828, "price_per_sqm_max": 1170,
  "computed_by": "rule_engine_v1",
  "lines": [ ... 9 项 ... ],
  "narrative": {"negotiation_tips": ["水电按实测结算，合同写明单价上限"]}
}
```

**LLM 完全不可用时**（同样是合法输出）
```json
{
  "total_min": 73648, "total_max": 104093,
  "computed_by": "rule_engine_v1",
  "narrative_degraded": true,
  "narrative": {
    "grade_rationale": "预算文字解读未能生成（模型不可用），此处仅提供由规则引擎直接给出的数字结果。",
    "confidence": 0.0
  }
}
```

## 9. 验收标准

| 编号 | 验收项 | 标准 | 全局 AC |
|---|---|---|---|
| AC-A04-01 | 分项数达标 | `lines` ≥ 7 项 | AC-05 |
| AC-A04-02 | 数字来自规则引擎 | `computed_by` 以 `rule_engine` 开头 | AC-05 / ADR-07 |
| AC-A04-03 | 档位区间一致 | 折合单价落在文档区间（800-1200 / 1500-2000 / 2500-3500） | AC-05 |
| AC-A04-04 | 叙事无金额字段 | `BudgetNarrative` 除 `confidence` 外无任何数值字段 | ADR-07 |
| AC-A04-05 | LLM 失败数字仍在 | 抛错/超时/空载荷三种情况下 `total_min` 均正确 | AC-17 |
| AC-A04-06 | 解说独立超时 | `NARRATIVE_TIMEOUT` < 节点 `timeout` | — |
| AC-A04-07 | 面积无效即拒绝 | 面积为 0/负数/非数值时抛 `InvalidAreaError`，**不返回 ¥0** | AC-33 |
| AC-A04-08 | 面积与守卫同源 | Agent 用的面积 = `effective_total_area(layout)` | AC-33 |
| AC-A04-09 | 引擎零 LLM 依赖 | 源码 AST 中无 LLM/HTTP 相关导入，且无异步函数 | ADR-07 |
| AC-A04-10 | 确定性 | 同入参多次调用结果完全一致 | — |
| AC-A04-11 | 演示数据声明 | `disclaimer` 非空且含"演示"字样，透出到前端 | — |
| AC-A04-12 | 并行不冲突 | 与 A-03 并发写同一 `plan_id` 时两产物都留存 | — |
