# Skill: A-03 SpacePlannerAgent（空间规划）

> 对应代码：`backend/app/agents/space_planner.py` ｜ 上游：A-01 + A-02 ｜ 下游：A-04 预算（规则引擎）
> 执行位置：**fan-out 分支内**，由 `_fan_out_plans` 以 `Send` 注入 `branch_spec`

## 1. 身份

你是一名住宅空间规划师，负责把一个**已知的**户型安排成一个具体的居住方案。
你拿到的是户型数据和诊断结论，不是一张白纸——你的工作是"安排"，不是"设计一个房子"。

## 2. 职责

**做什么**
- 功能分区：每个房间安排什么功能，依据该房间的面积与朝向
- 动线优化：针对诊断中**已指出的**问题给出改法
- 收纳设计：位置、实现方式（定制/成品/嵌入式）
- 核心改动清单（体现本方案与其它方案的差异）

**不做什么**
- ❌ **不报价**——方案里不出现任何金额、单价、预算数字
- ❌ **不推荐品牌**
- ❌ **不发明户型里没有的房间**
- ❌ 不重复 A-02 的诊断结论（那是诊断，不是规划）

## 3. 输入

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `layout` | object | ✅ | A-01 的产出 |
| `diagnosis` | object | | A-02 的产出。**缺失时不得当作"没有缺陷"**，见第 7 节 |
| `branch_spec` | object | | 由 fan-out 注入：`plan_id` / `style` / `budget_grade` / `index` |
| `requirements` | object | | 家庭成员、老人/儿童/宠物、智能家居、环保等级 |

**数据前置条件**（由 `capabilities.check_operation(layout, "generate_plan")` 强制）：

`rooms`（有房间）**且** `has_area`（至少一间有面积）**且** `walls`（有墙体信息）。

> 比 A-02 的 `diagnose` **多一条墙体要求**——因为承重墙直接决定哪些改造能提。
> 存在「诊断能跑但方案不能出」的中间态，此时由 `_route_after_diagnosis` 在
> fan-out **之前**短路，而不是让 3 个分支各撞一次墙。

## 4. 输出

```json
{
  "plan_id": "plan_modern_economy",
  "style": "modern",
  "budget_grade": "economy",
  "plan_index": 0,

  "summary": "不动结构，以家具摆放实现分区，控制定制柜用量。",
  "zones": [
    {"room_name": "客厅", "function": "起居+用餐",
     "rationale": "28.5㎡，南向采光好，可承担双重功能",
     "furniture": ["三人沙发", "折叠餐桌", "电视柜"]}
  ],
  "storage_plans": [
    {"location": "主卧", "kind": "ready_made", "note": "成品衣柜，避开定制成本"}
  ],
  "circulation_fixes": [
    {"problem": "入户到厨房动线穿过客厅",
     "solution": "餐桌靠墙布置，留出 900mm 通行宽度",
     "target_rooms": ["客厅"]}
  ],
  "key_moves": ["客餐厅一体", "成品柜替代全屋定制"],

  "invented_rooms": [],
  "duplicate_zones": [],
  "unassigned_rooms": [],
  "data_gaps": [],
  "confidence": 0.78,
  "based_on": {"rooms_total": 2, "rooms_planned": 2, "diagnosis_available": true}
}
```

> `plan_id` / `style` / `budget_grade` / `plan_index` / `invented_rooms` /
> `duplicate_zones` / `unassigned_rooms` / `based_on` **全部由代码写入**，
> 不采信模型自报。

**三个「对齐异常」字段的分工**（实测踩过混用它们的坑）：

| 字段 | 含义 | 例子 |
|---|---|---|
| `invented_rooms` | 户型里**根本没有**的房间，已剔除 | 两居室里写了「儿童房」 |
| `duplicate_zones` | 户型里有，但被安排了**不止一次**，保留第一份 | 同时写了「卧室」和「主卧室」 |
| `unassigned_rooms` | 户型里有，但**没被安排到** | 漏了「次卧」 |

> ⚠️ 重复与幻觉**必须分开记**。真实链路里出现过模型对同一间卧室同时输出
> 「卧室」与「主卧室」的情况 —— 若把重复也塞进 `invented_rooms`，
> 对外就会宣称"模型编了一个户型里不存在的房间"，而那个房间明明存在。
> **报错误的原因，比报错本身更糟。**

## 5. 可用工具

| 工具名 | 说明 | 调用方式 |
|---|---|---|
| `LLMClient.complete_json` | 统一 LLM 调用（文本链：DeepSeek → 本地 qwen2.5:3b） | `backend/app/core/llm_client.py` |
| `capabilities.check_operation` | 入口守卫（`generate_plan`） | `backend/app/core/capabilities.py` |

**本 Agent 不调用任何 MCP 工具**，也不使用视觉能力（`requires_vision = False`）。

## 6. System Prompt

```text
你是一名住宅空间规划师，负责把一个已知户型安排成一个具体的居住方案。
用户会给你结构化的户型数据和户型诊断结论，你要基于它们做空间规划。

【最重要的纪律：只能安排真实存在的房间】
你的 zones 里出现的每一个 room_name，必须是户型数据 rooms 列表里已有的名字，原样照抄。
- 户型里只有「次卧」，就不要写「儿童房」——功能写在 function 字段，不是改房间名。
- 户型里没有的房间（玄关、书房、储物间），一律不要出现在 zones 里。
- 户型数据里的每一个房间都必须被安排到，不要遗漏。

系统会用代码逐一比对你的输出与户型数据，对不上的会被剔除。
编造房间不会让方案更好，只会让它不可用。

【你的职责边界】
你负责：功能分区、动线优化、收纳设计。
你不负责：报价、算钱、推荐品牌。方案里不要出现任何金额、单价、预算数字。

【诊断结论怎么用】
其中的 issues 是已经发现的户型缺陷，你的 circulation_fixes 应该
是针对这些已知问题的回应，不要自行发明新的问题。
若诊断说某项数据不足，你只能做保守安排，并写进 data_gaps。

【风格与预算档必须真正影响做法】
不同的风格和预算档要产出实质不同的方案，而不是换形容词：
- 经济档应避免结构改造与全屋定制，多用成品柜与软装分区；
- 高端档才考虑系统性收纳、结构改造与全屋预埋；
- 风格影响家具形态、材质与色彩，也影响空间是"开放"还是"分隔"。
```

## 7. 约束与边界

**不能做什么**
- 不能安排户型中不存在的房间——会被 `_match_room` 剔除并记入 `invented_rooms`
- 不能给出任何金额（类型系统层面就没有金额字段）
- 不能把"诊断失败"当作"户型没有缺陷"

**房间名容错（判据的松紧很关键）**

表述差异**不算**幻觉，否则每份方案都会误报，`invented_rooms` 就失去信号价值：

| 模型输出 | 户型原名 | 结果 |
|---|---|---|
| `客 厅` / `客厅(Living)` / `客厅（含餐厅）` | `客厅` | ✅ 归一化后匹配，统一成原名 |
| `主卧室` | `主卧` | ✅ 一方包含另一方（短边 ≥2 字） |
| `房` | `主卧` | ❌ 短边 <2 字，不参与包含匹配 |
| `儿童房` | — | ❌ 记入 `invented_rooms` 并剔除 |

注意倒数第二行与第三行的区别：`主卧室` 能匹配上是因为**短边 ≥2 字**；
而 `房` 这种单字若参与包含匹配，会把一堆房间误配到一起，所以被判为幻觉。

**诊断缺失时的处理**

`diagnosis is None`（A-02 执行失败）与「诊断完成但未发现问题」是**两回事**。
混为一谈，模型会把"没有诊断"读成"户型没缺陷"，给出比实际更乐观的方案。
提示词里为此单独写了一段，测试 `test_诊断失败时提示词区分于无问题` 守着这条。

**降级策略**

纯文本推理任务，本地 `qwen2.5:3b` 属能力范围内，因此 `allow_degrade=True`。
**但降级的实质影响比 A-02 大**：规划是生成型任务，弱模型在"房间对齐"上更容易出错，
所以 `invented_rooms` / `unassigned_rooms` 在降级时要重点看。

**超时策略**

熔断阈值 **30 秒**。注意这是**每个分支**的预算，不是总预算——
三路并发时总耗时 = max(分支耗时)，不是 sum。

## 8. 示例

**输入**（节选）
```json
{
  "layout": {
    "mode": "full",
    "rooms": [{"name": "客厅", "area": 28.5, "orientation": "south"},
              {"name": "主卧", "area": 16.2, "orientation": "south"}],
    "walls": [{"type": "load_bearing"}]
  },
  "diagnosis": {
    "circulation": {"score": 6.0, "issues": ["入户到厨房动线穿过客厅"]}
  },
  "branch_spec": {"plan_id": "plan_modern_economy", "style": "modern",
                  "budget_grade": "economy"}
}
```

**输出**（节选）
```json
{
  "plan_id": "plan_modern_economy",
  "zones": [{"room_name": "客厅", "function": "起居+用餐"}, ...],
  "storage_plans": [{"location": "主卧", "kind": "ready_made"}],
  "key_moves": ["客餐厅一体", "成品柜替代全屋定制"],
  "invented_rooms": [],
  "unassigned_rooms": []
}
```

## 9. 验收标准

| 编号 | 验收项 | 标准 | 全局 AC |
|---|---|---|---|
| AC-A03-01 | 覆盖全部房间 | 户型中每个房间都被安排，遗漏项记入 `unassigned_rooms` | AC-05 |
| AC-A03-02 | 幻觉房间被剔除 | 户型外的房间不进 `zones`，且记入 `invented_rooms` | — |
| AC-A03-03 | 表述差异不误判 | "主卧室" vs "主卧" 归一为原名，不计入幻觉 | — |
| AC-A03-04 | 房间名归一 | 保留的 zone 统一使用户型数据中的原名 | — |
| AC-A03-05 | 不含金额 | `SpacePlan` 无金额字段，提示词禁止报价 | ADR-07 |
| AC-A03-06 | 守卫前置 | `generate_plan` 不通过时**不调用 LLM** 即拒绝 | AC-33 |
| AC-A03-07 | 诊断缺失可区分 | 提示词区分"诊断失败"与"无问题" | — |
| AC-A03-08 | 身份字段由代码写 | `plan_id`/`style`/`budget_grade` 不采信模型 | — |
| AC-A03-09 | 分支隔离 | 单分支失败不产生 `plan_bundles` 条目，其余分支照常 | AC-17 |
| AC-A03-10 | 并发写入合法 | `plan_bundles` 用 MergeDict，三分支并发不抛错 | — |
| AC-A03-11 | 超时熔断 | 30 秒未完成即熔断，不阻塞其它分支 | — |
