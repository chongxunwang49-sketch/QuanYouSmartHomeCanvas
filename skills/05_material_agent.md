# Skill: A-05 MaterialAgent（材料选型）

> 对应代码：`backend/app/agents/material_agent.py` ＋ `backend/app/services/material/catalog.py`
> 上游：A-01（房间）＋ fan-out 注入的 `branch_spec`（档位/风格）｜ 下游：fan-in
> 执行位置：**fan-out 分支内，与 A-03 / A-04 并行**（三者独立写入同一 `plan_bundles[plan_id]`）

## 1. 身份

你是装修材料选配顾问，在一份**给定的候选清单**里替业主挑选材料。

注意"给定"两个字：候选不是你找的，是系统按档位、风格与业主需求筛好并排好序的。
你的价值在于**挑得更有判断力、理由说得更贴切**，而不是"提供候选"。

## 2. 职责

**做什么**
- 逐品类从候选里选一件，并说明为什么
- 给出 0-3 条品牌/档次替代建议
- 说明环保等级上的取舍
- 提示**材料层面**的注意事项

**不做什么**
- ❌ **不编造型号** —— product_id 必须严格取自候选清单
- ❌ **不提及任何金额** —— 价格由代码从目录取用
- ❌ 不做报价单审查、增项漏项分析（那是 A-06 避坑审查的职责）
- ❌ 不为了凑数硬选 —— 候选里没有合适的，跳过那个品类

## 3. 输入

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `layout` | object | ✅ | A-01 的产出。选材只需知道「有哪些房间」 |
| `requirements` | object | | `has_children` / `has_elderly` / `pets` / `eco_level` / `smart_home` / `family_size` |
| `branch_spec` | object | | fan-out 注入：`budget_grade` 决定候选池，`style` 参与排序 |

**数据前置条件**（由 `capabilities.check_operation(layout, "select_materials")` 强制）：

`rooms`（有房间）**且** `has_area`（至少一间有面积）。

> ⚠️ **不要墙体**。选材的判据比 A-03 低一档：一个没识别出墙体、
> 但房间与面积齐全的户型，方案出不了，**选材却是能做的**。
> 用 `generate_plan` 那道门会过度拒绝 —— 这是本项目第一次出现
> "同一个分支里不同 Agent 门槛不同"的情况，是能力的真实差异，不是缺陷。

## 4. 输出

产物分三层，写入 `plan_bundles[plan_id]["materials"]`：

```json
{
  "plan_id": "plan_modern_economy",
  "grade": "economy",
  "style": "modern",

  "items": [
    {
      "category": "floor",
      "reason": "全友自有产品、适配需求（children）、风格匹配（modern）",
      "id": "QY-FL-101", "name": "全友 强化复合地板 云杉系列", "brand": "全友",
      "is_quanyou": true, "spec": "1210x165x12mm",
      "price_range": [89, 159], "eco_level": "E0",
      "search_url": "https://www.quanyou.com.cn/search?keyword=强化地板"
    }
  ],
  "product_ids": ["QY-FL-101"],

  "quanyou_coverage": 1.0,
  "quanyou_met": true,
  "auto_substitutions": [],
  "invented_products": [],

  "substitutions": [],
  "summary": "……", "eco_note": "……",
  "warnings": [], "data_gaps": [], "confidence": 0.7,

  "catalog_version": "catalog_v1",
  "disclaimer": "本文件全部为演示数据……"
}
```

**三层各由谁产出：**

| 层 | 内容 | 产出者 |
|---|---|---|
| ① | `choices`（选了哪个 id、为什么） | **LLM** |
| ② | 商品名称/品牌/价格/链接 | **代码**按 id 从目录回填 |
| ③ | `quanyou_coverage` / `auto_substitutions` / `invented_products` | **代码**计算 |

## 5. 可用工具

| 工具名 | 说明 | 调用方式 |
|---|---|---|
| `material.catalog.candidates` | 按档位/风格/需求筛出候选并排序（**确定性**） | `backend/app/services/material/catalog.py` |
| `material.catalog.coverage` | 计算全友覆盖率（AC-18） | 同上 |
| `material.catalog.find_quanyou_alternative` | 找同品类全友替代品（AC-18 兜底） | 同上 |
| `LLMClient.complete_json` | 只要 `MaterialPlan`（选择与理由） | `backend/app/core/llm_client.py` |

**本 Agent 不使用 embedding / ChromaDB / Reranker。** 理由见第 7 节。

## 6. System Prompt

```text
你是一名装修材料选配顾问，为业主在一份给定的候选清单里挑选材料。

【最重要的一条：你只能从候选清单里挑】
每个品类都会给你一份候选商品列表，每项带 id、名称、品牌、规格、价格区间，
以及"匹配理由"。你只能从这些候选里选，不得自行编造型号或品牌。
候选里确实没有合适的，就把那个品类留空不选 —— 少选一项，
比编一个搜不到的型号强得多。系统会用代码核对你的每一个 id，
对不上的会被剔除。

【优先全友自有产品】
这是全友的平台，优先推荐自有产品是明确的业务要求。
在同样合适的前提下选全友；如果某品类的全友产品明显不匹配
（规格不符、档位不符），可以选竞品，但要在 reason 里说清为什么。

【理由要引用给定的匹配信息】
候选清单里已经给出了每件商品的匹配理由。你的 reason 应当基于这些已有信息，
不要自己另编依据，也不要声称候选清单里没有的参数。

【不要谈钱】
价格由系统从目录里直接取用，你不需要也不允许计算、估算或提及任何金额。

【边界】
你只负责选材。报价单审查、增项漏项、合同风险都不属于你 ——
那是避坑审查 Agent 的职责，不要写进 warnings。
```

## 7. 约束与边界

**为什么不用向量检索**

需求文档把本 Agent 的实现写成「RAG + 全友产品知识库」，听起来该上 embedding。
但看数据形态就会发现**用不上**：用户偏好是结构化的（`has_children` 是布尔），
商品也是打标好的（`suitable_for` / `style_fit` / `budget_grade`）。
两边都是标签，匹配就是集合运算。

引入向量的代价却是实打实的：多一个 Ollama 依赖、多一次网络往返、结果不确定、
**无法断言"给定输入必然得到给定输出"**。在结构化能解决的地方引入向量，
是用可靠性换一个用不上的能力。

真正的语义检索留给 **A-06 避坑审查** —— 那边是装修规范与避坑文档，
自由文本、无标签，那才是 RAG 不可替代的地方（也在路线图 M4 范围内）。

**AC-18 是一条真门槛**

「全友产品覆盖率 ≥ 60%」要成立，前提是**候选集里有竞品**。
目录里每个品类刻意保持 2 个全友 + 2 个竞品（整体 50%）——
随机选择的期望覆盖率是 50%，**低于 60% 的线**，必须真的优先选全友才能过。

`tests/test_material_catalog.py` 里有一组测试专门盯这件事：
`test_目录里必须有竞品`、`test_随机选的期望覆盖率低于AC18门槛`。
**如果哪天有人往目录里塞满全友产品，那两条会报红** ——
提醒他 AC-18 正在被稀释。测数据构成而不只测代码，在这种场景下是必要的。

**AC-18 的代码兜底**

覆盖率由代码统计（模型自报没有意义）。不达标时，代码在同品类里
换成全友的等价物，并逐条记入 `auto_substitutions`。
找不到可替换的就**保留原选择并如实上报未达标** —— 宁可覆盖率不达标，
也不为了凑指标换一个规格不符的东西。

**模型失败时仍产出选材**

候选排序本身就编码了档位过滤、风格匹配、需求适配与全友优先，**不需要模型参与**。
所以：

| 情况 | 结果 |
|---|---|
| 模型正常 | 按模型选择，理由更有针对性 |
| 模型抛错/超时/返回空 | **回退确定性排序**，取各品类首选，理由复用评分算出的 `reasons` |

兜底不是"随便填一个"：它标明了"模型不可用，按匹配度自动选出"，
降级在用户眼里是"少了点针对性"，而不是"多了一段胡说"。
这与 A-04「先算数、再写字」是同一条纪律 —— **把不依赖模型的那部分产物保护起来**。

`SELECT_TIMEOUT = 22s` 独立于节点总超时 30s，让兜底先于熔断发生。

**数据来源纪律**

全友产品价格库**无真实数据源**（R-09）。目录里的品类与价格区间是按公开官网
信息与行业均价量级构造的演示数据，文件内与每次响应里都**显著标注**。
**严禁在文档或 UI 中把演示数据表述为真实报价。**

## 8. 示例

**输入**（节选）
```json
{
  "layout": {"rooms": [{"name": "客厅"}, {"name": "卫生间"}]},
  "requirements": {"has_children": true, "eco_level": "E0"},
  "branch_spec": {"plan_id": "plan_modern_economy", "style": "modern",
                  "budget_grade": "economy"}
}
```

**模型返回值**（不含任何价格）
```json
{
  "summary": "经济档以常规材料为主，环保等级优先保证。",
  "choices": [
    {"category": "floor", "product_id": "QY-FL-101",
     "reason": "全友自有产品、适配需求（children）、风格匹配（modern）"}
  ],
  "substitutions": [],
  "eco_note": "地板与涂料均为 E0 级。",
  "warnings": ["强化地板脚感偏硬"],
  "data_gaps": [],
  "confidence": 0.7
}
```

**代码产出**（价格与链接由目录回填）
```json
{
  "items": [{"id": "QY-FL-101", "price_range": [89, 159],
             "search_url": "https://www.quanyou.com.cn/search?keyword=强化地板",
             "is_quanyou": true, "reason": "全友自有产品、适配需求（children）…"}],
  "quanyou_coverage": 1.0,
  "quanyou_met": true,
  "invented_products": [],
  "disclaimer": "本文件全部为演示数据……"
}
```

## 9. 验收标准

| 编号 | 验收项 | 标准 | 全局 AC |
|---|---|---|---|
| AC-A05-01 | 只能指认不能创造 | 不在候选集里的 `product_id` 被剔除并记入 `invented_products` | — |
| AC-A05-02 | 价格由代码回填 | `items[].price_range` 与目录完全一致 | AC-21 |
| AC-A05-03 | 链接可溯源 | `items[].search_url` 非空且来自目录 | AC-21 |
| AC-A05-04 | 选材模型无金额字段 | `MaterialPlan` 除 `confidence` 外无数值字段 | ADR-07 |
| AC-A05-05 | 全友覆盖率达标 | `quanyou_coverage ≥ 60%`（**代码统计，非模型自报**） | AC-18 |
| AC-A05-06 | 覆盖率可兜底 | 不达标时由代码替换为同品类全友产品并记录 | AC-18 |
| AC-A05-07 | 换不掉就如实说 | 无替代品时保留原选择且 `quanyou_met=false` | — |
| AC-A05-08 | 档位硬过滤 | 经济档方案不出现仅属于高端档的商品 | — |
| AC-A05-09 | 模型失败仍产出 | 抛错/超时/空载荷时回退确定性排序，选材不消失 | AC-17 |
| AC-A05-10 | 选材独立超时 | `SELECT_TIMEOUT` < 节点 `timeout` | — |
| AC-A05-11 | 演示数据声明 | `disclaimer` 非空且含"演示"，透出到前端 | R-09 |
| AC-A05-12 | 工具层零模型依赖 | `catalog.py` 无 LLM/网络/向量相关导入 | — |
| AC-A05-13 | 并行不冲突 | 与 A-03 / A-04 并发写同一 `plan_id` 时三份产物都留存 | — |
