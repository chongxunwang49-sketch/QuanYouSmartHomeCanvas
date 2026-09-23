# 全友·智绘家 QuanYou Smart HomeCanvas

**内江市全友家居 · 户型多模态装修推荐系统**

上传一张户型图 → 多模态解析 → 多 Agent 并行生成 3 套装修方案 → 预算估算 + 避坑审查 + 效果图 + 物品热区交互。

---

## 这个项目最值得看的是什么

不是"用了 LangGraph 和 MCP"，而是**它记录了三轮把自己前期假设推翻的过程**。

绝大多数个人项目的技术选型是纸面推演：看到某个模型/框架流行就写进架构图，直到交付前才发现跑不起来。
本项目反过来——**每一项技术决策都带本机实测数据，凡是实测推翻的就在文档里写明推翻理由**。

三轮评审共修正 **26 处**设计问题，其中 **10 处是阻断级**：

| 曾计划 | 实测结论 | 改为 |
|---|---|---|
| 本地 SDXL + ControlNet | 底模 fp16 6.9GB，本机可用显存仅 **3.22GB** | SD1.5 + ControlNet + **矢量图常驻兜底** |
| ~~用 SD1.5 生成「室内效果图」~~ | **任务错配**——输入是俯视平面图，输出只能是灰块或噪声 | 交付物重新定义为「**3D 户型渲染图**」，实测 0.97GB / 12.2s |
| ~~LCM-LoRA 4–8 步加速~~ | **跨种子极不稳定**：同一提示词 seed=7 出 3D 图、seed=42 出噪声图 | **默认关闭**，改标准采样 20 步。用 4.5s 换确定性 |
| ~~`offload` 会把出图拖到分钟级~~ | 实测**既省 3GB 显存又更快**（6.58s → 5.33s），全量上卡时分配器在颠簸 | `enable_model_cpu_offload()` 设为默认 |
| Qwen3-VL 主 → DeepSeek 备 | 本机**无 DashScope key**；实测 DeepSeek 可读图 | **主备对调** |
| YOLOv8 检测生成图物品 | **COCO 80 类里没有地板、墙纸、空调** | 户型 JSON 坐标映射（零检测模型） |
| Docker 6 服务全部 Healthy | Docker VM 上限 **7.63GB**，本机已有多条 `exit(137)` OOM 记录 | **4 容器** + Chroma 内嵌 + Ollama 走宿主机 |
| 让本地小模型承接降级解析 | 实测它**编造越界 bbox、虚构墙体、JSON 语法本身是坏的** | 降级只问房间名 + **质量闸门** |
| 预设热区 IoU 阈值 0.70 | conditioning 边缘图与生成图 Canny 结果**天生不可比**，0.70 会让功能整体失效 | **默认关闭**，待实测标定 |

> 完整修订记录见 [`docs/需求/需求文档.md`](docs/需求/需求文档.md) 开头四张「本次修订说明」表。

---

## 当前进度

**已可用（M0 + M2 前半，已实测跑通）**

- ✅ 多模态户型图解析（A-01）—— 真实调用 DeepSeek，输出结构化 JSON
- ✅ 户型诊断（A-02）—— 采光/通风/动线/利用率/环保五维评分
- ✅ 空间规划（A-03）—— 功能分区 / 动线优化 / 收纳设计，幻觉房间由代码剔除
- ✅ **预算造价（A-04）—— 规则引擎算钱，LLM 只管文字**（ADR-07）
- ✅ **材料选型（A-05）—— 模型只能指认候选，价格由代码回填**（AC-18 / AC-21）
- ✅ **避坑审查（A-06）—— RAG 检索 + 引用必须可溯源**（AC-06，实测召回 93%）
- ✅ **fan-out / fan-in + 汇聚审查** —— 9 个并发产出任务 + 1 次审查
- ✅ 三级降级链 —— DeepSeek → 本地 Ollama，断网自动切换（已实测）
- ✅ 本地隐私模式 —— `prefer_local=true` 时图像不出本机（**抓包验证**）
- ✅ 业务连续性守卫 —— 数据不达标时禁止触发下游操作，拒绝时说明缺什么
- ✅ MCP Server —— `parse_house_layout` 可被外部 MCP 客户端调用
- ✅ **AI 出图基准跑通** —— SD1.5 + ControlNet @512，连续 10/10 张不 OOM
  ⚠️ **但产出不是它名字里说的那个东西。** 2026-09-23 复测：显存与耗时达标，
  产出却是**俯视家具平面图**，不是「3D 等轴测户型渲染图」。根因见下方「出图复测」。
  方案封面因此改用 3D 场景的等轴测截图（真实几何、确定性、毫秒级）。
- ✅ **FastAPI 接口层** —— 6 个接口，异步任务 + 语义化进度轮询（`/docs` 可交互）
- ✅ **Vue3 前端** —— 8 个页面，严格按用户提供的设计系统实现（见下）
- ✅ **图片质量预检**（AC-27）—— 本地零 Token 拦下不合格图片，`prechecking` 阶段是真的
- ✅ **517 个自动化测试全绿**（34s，全程不联网）

**未开始**

- ⬜ 矢量图渲染与热区（M5）—— 图片上的材质热点要等真实热区坐标
- ⬜ 国标语料（GB 50327 / GB 18580 / GB-T 39600 摘要）—— 让合规类结论有法条可引
- ⬜ 数据库落库（目前户型暂存在内存 + Redis，进程重启即丢）
- ⬜ M1 认证 —— **主动跳过**，单机演示系统不对外暴露（前端"用户管理"页如实标注未实现）

### 真实链路一次完整跑通（`scripts/e2e_smoke.py`）

```
A-01 解析       ~16s    3 房间 / 2 窗 / 3 门
A-02 诊断       ~18s    五维评分，部分维度标记「数据不足」
  ↓ fan-out：9 个并发任务
A-03 规划      22.5s   [17684, 16287, 22478]ms   3 套方案
A-04 预算      11.6s   [11556,  9504,  8778]ms   3 份预算（规则引擎）
A-05 选材      12.4s   [ 8339, 12405,  8407]ms   3 份选材（代码回填价格）
  ↓ 汇聚
A-06 避坑      38.8s   1 次审查 × 3 套方案（**串行，无影子可躲**）
fan-in          ~0s    对比表 3/3 可用
总墙钟         111.3s
```

> ⚠️ **A-06 是唯一拖慢墙钟的 Agent。** A-03/A-04/A-05 互相并行，
> 新加一个只要不慢过最慢的那个就"零成本"；但 A-06 是**汇聚节点** ——
> 审查必须在产出之后，它的 38.8s 直接加到总时间上。
> 这是"依赖关系决定架构"的代价，也是下一步最值得优化的地方。

**三套方案的产出**（本轮 e2e，折合单价全部落在文档区间内）：

| 方案 | 档位 | 总价区间 | 折合单价 | 全友覆盖 | 避坑审查 |
|---|---|---|---|---|---|
| plan_modern_economy | 经济 | ¥33,100 – 46,783 | 828–1170 元/㎡ | 100% ✅ | 7 条 / 5 类 |
| plan_nordic_medium | 中档 | ¥60,155 – 79,488 | 1504–1987 元/㎡ | 100% ✅ | 5 条 / 3 类 |
| plan_chinese_high | 高端 | ¥101,962 – 139,261 | 2549–3482 元/㎡ | 100% ✅ | 6 条 / 4 类 |

> **A-04 与 A-05 躲在 A-03 的影子里**（最慢 11.6s / 12.4s vs A-03 的 22.5s），
> 所以产出者从 2 个加到 3 个，墙钟没有因此增长。
>
> ⚠️ 但**分支内的避坑审查只有 3-5 类，达不到 AC-06 的 5 类线** ——
> 因为它审的是我们自己用规则引擎生成的预算表，那张表本来就没那么多坑。
> **AC-06 是靠 quote 模式达标的**（审真实报价单，实测 8 类、召回 93%）。
> 两个模式是两种能力，不该混为一谈。

> ⚠️ 材料价格**全部是演示数据**（全友无公开结构化价格接口，见 R-09）。
> 目录文件与每次 API 响应里都带 `disclaimer`，前端必须原样展示。

### M0 出图基准实测结果

```
级别：L0（期望状态，无需任何降级）
连续 10/10 张不 OOM
峰值显存 0.968GB   ← 阈值 3.2GB，用不到三成
均耗时   12.23s    ← P95 目标 25s，不到一半
```

**输出的是「3D 等轴测户型渲染图」，不是「装修效果图」。** 这个措辞差别必须守住——
输入是俯视平面图，让人眼视角实景从平面图里长出来是研究级任务，本机做不到；
而把平面"立起来"做 3D 渲染正是 SD1.5 擅长的。

复现：`python scripts/bench_image.py`，报告落在 `E:/quanyou/outputs/bench/bench_report.json`。

#### ⚠️ 出图复测（2026-09-23）：显存达标 ≠ 画对了

上面那份基准量的是**显存与耗时**，这两项确实达标。但 AC-08 的完整表述是
「生成 **3D 等轴测**户型渲染图，≥512×512，P95 < 25s」——**前半句没有兑现**。

打开基准产出的那张图看：它是**俯视的家具平面图**，不是等轴测 3D 渲染。
把真实户型（3 房间 / 45.9㎡）喂进去，结果一样。

做了四组对照实验定位原因（产出存于 `E:/quanyou/outputs/render_ablation/`）：

| 配置 | 结果 |
|---|---|
| 俯视条件图 scale=0.5 | 俯视抽象色块 |
| **等比条件图 scale=0.5** | **俯视家具平面图** ← 当前最好的 |
| 等比条件图 scale=0.35 | 完全跑偏（生成了一栋房子外景，带车） |
| 等轴测条件图 scale=0.7 | 倾斜但几何破碎 |

**根因是结构性的**：`controlnet_sd15_canny` 会把条件图的结构逐边复刻，
而条件图是俯视的 → 输出必然是俯视。约束强度是把刀锋：强了变平面复刻，
弱了模型自己编。这个窄带里，最好的结果也只是俯视家具图。

要真正拿到等轴测，需要换 ControlNet 类型（depth / 专用 layout 模型）
或引入户型 LoRA —— **都超出 M0 的范围**。

> 顺带修掉一个真 bug：`render_conditioning_image` 原先对 x / y 各用一个
> 缩放因子，等于把非正方形户型**强行拉成正方形**（685×505 的三居室被压扁
> 1.36 倍，ControlNet 照着变形结构生成）。已改为等比缩放 + 居中，
> 修复后内容比例 1.359 vs 源 1.356，居中误差 ≤1px。回归测试见
> `tests/test_image_conditioning.py`（11 条，全部从像素里量，不靠肉眼）。

---

## 本机实测基线

> 这套数字是本项目所有技术决策的依据。**换个机器跑，结论可能完全不同。**

| 项 | 实测值 | 直接影响 |
|---|---|---|
| CPU | i7-11800H 8C/16T | 充足 |
| 内存 | 15.74 GB（Docker Desktop 占用后剩 ~3.3GB） | 偏紧 |
| **GPU 可用显存** | **3.22 GB**（驱动口径，非标称 4GB） | 否决 SDXL、否决全部视频生成 |
| **Docker VM** | **7.63 GB 上限** | 只能跑 4 容器 |
| Python | **3.11.15**（conda env `pytorch`） | base 是 3.13 但**无 CUDA torch** |
| 磁盘 | E: 121GB / D: 29GB | 模型与数据统一放 E 盘 |
| `huggingface.co` | **直连不通**（12s 超时） | 必须走 `hf-mirror.com` |
| GitHub | 可达但 **9.9s** | 不 clone 大仓库 |
| DeepSeek 多模态 | ✅ 已实测可读图（240 tokens / 320×240 图） | 主模型 |
| Ollama 本地 | `minicpm-v4.6`(vision) / `qwen2.5:3b` / `bge-large-zh-v1.5` | 兜底 + Embedding |

> ⚠️ 显存测量的一个陷阱：`torch.cuda.memory_allocated()` **看不到 CUDA context**
> （实测它显示 0MB）。显存预算必须从 `torch.cuda.mem_get_info()` 或 `nvidia-smi`
> 的**驱动口径**读，否则会系统性低估 300–500MB。

---

## 快速开始

```bash
# 1. 环境（本机唯一带 CUDA torch 的环境）
PY="D:/AI damoxing/Anaconda3/envs/pytorch/python.exe"

# 2. 配置
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY（留空则自动走本地 Ollama）

# 3. 跑测试
$PY -m pytest tests/ -v                          # 单元测试（不联网）
$PY -m pytest tests/ -m integration -s           # 集成测试（真实调用）

# 4. 起后端（打开 http://127.0.0.1:8000/docs 可交互调试）
$PY -m uvicorn backend.app.main:app --reload --port 8000

# 5. 调用 MCP 工具
$PY -m mcp_servers.parse_house_layout
```

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/layout/parse` | 户型解析（异步，实测 33–48s） |
| POST | `/api/v1/design/generate` | 方案生成（异步，实测 ~100s） |
| POST | `/api/v1/avoid-pit/review` | 报价单/合同审查（异步，实测 ~19s） |
| GET | `/api/v1/task/{task_id}/status` | 任务状态轮询 |
| GET | `/api/v1/material/price` | 材料价格（同步） |
| GET | `/api/v1/system/health` | 健康检查 |

**约定**：业务失败一律 **HTTP 200 + `code != 0`**（需求文档 4.4 的要求）。
前端 axios 拦截器按状态码判断成败，用 4xx 表达"数据不支撑该操作"会被当成
服务错误弹通用报错，而用户需要看到的是可操作的提示。

**运行已实测**：真实户型图 → DeepSeek → 结构化 JSON，端到端 6.1s。

---

## 架构

```
┌────────────────────────────────────────────────────────────┐
│ 接入层  Vue3 + TypeScript + Tailwind + Element Plus         │
└─────────────────────────┬──────────────────────────────────┘
┌─────────────────────────▼──────────────────────────────────┐
│ 服务层  FastAPI（异步） + LangGraph 状态机                  │
│   auth / layout / design / budget / material / avoid_pit    │
│   image（串行锁）/ quanyou / MCP 工具中心                    │
└─────────────────────────┬──────────────────────────────────┘
┌─────────────────────────▼──────────────────────────────────┐
│ 数据层  PostgreSQL 15 · Redis 7 · ChromaDB（嵌入式）        │
│         本地文件 E:/quanyou/                                │
└─────────────────────────┬──────────────────────────────────┘
┌─────────────────────────▼──────────────────────────────────┐
│ 模型层  多模态：DeepSeek → minicpm-v4.6（本地）             │
│         文本：deepseek-flash → qwen2.5:3b（本地）           │
│         Embedding：bge-large-zh-v1.5（本地，1024 维）        │
│         图像：VectorRenderer（必出）→ SD1.5（尽力）          │
└────────────────────────────────────────────────────────────┘
```

**关键设计：LangGraph 工作流（六个 Agent 全通，含 RAG 审查）**

```
START
  │
precheck_image      本地图片质量预检（零 Token，AC-27）
  │  不合格 => 直接失败，A-01 一次都不会被调用
parse_layout        A-01 多模态解析（含能力匹配降级）
  │  条件路由：无数据 / 降级 => 短路 END
diagnose_layout     A-02 五维诊断
  │  条件路由：不支撑方案生成 => 短路 END
  │
  │  fan-out（Send）：3 套方案 × 3 个产出者 = 9 个并发任务
  │
  ├─ plan_modern_economy ─┬─ generate_plan    A-03 空间规划
  │                       ├─ estimate_budget  A-04 预算（规则引擎）
  │                       └─ select_materials A-05 选材（代码回填价格）
  ├─ plan_nordic_medium ──┼─ 同上 ×3
  └─ plan_chinese_high ───┴─ 同上 ×3
  │
  │  汇聚：9 个产出任务全部完成后，下面的节点执行**一次**
  │
review_risks        A-06 避坑审查（RAG + 引用溯源）★ **不是并行分支**
  │
  │  fan-in：按 plan_id 汇聚（深合并 reducer）
  │
aggregate_plans     三方案汇总 + 对比表（纯代码，无 LLM）
  │
 END
```

**A-06 为什么不在并行分支里**：**你没法审查一份还不存在的预算。**
需求文档 3.3 画的"4 个 Agent 并行"是架构草图；真实依赖是"审查在产出之后"。
这与 ADR-08（图像节点移出并行分支）是同一类修正 ——
**并行的前提是互不依赖，而不是"看起来可以并行"**。

**产出者之间互相独立**：任意一个失败不影响其余两个。
fan-in 按分支规格列出方案，缺哪个产物写进 `missing_artifacts`。

**加产出者只需改 `workflow.py` 里的一张表** `_BRANCH_PRODUCERS`
（节点名 → 产物键），节点列表、产物列表、fan-in 收集逻辑都从它派生。
审查者单独登记在 `_REVIEW_NODE`，不混进产出者列表 —— 有测试守着这条。
图像与热区节点位于 **fan-in 之后**，不在并行分支内 —— 9 路并发调图会打爆显存。

**关键设计：能力匹配的降级链**

```
主模型全量解析（allow_degrade=False，禁止自动降级）
    │ 失败 / 用户选本地模式
    ▼
本地降级解析（force_local=True，只问房间名）→ 质量闸门
    │ 通过                        │ 不通过
    ▼                            ▼
mode=degraded_basic          整条链失败
结构字段全部留空，           建议重新上传
能力守卫禁止下游操作         不返回空壳
```

---

## 三条贯穿全项目的原则

### 1. 禁止编造

LLM 不得生成预算数字（`calc_budget` 是纯函数规则引擎）；承重墙判断必须提示人工复核；
识别不出就说识别不出——`uncertain_points` 越诚实，系统越可靠。

### 2. 降级必须显式

`degraded` 标志贯穿 **API 响应 → 审计日志 → 前端 UI** 三处。
**静默降级视为缺陷**——用户有权知道这次结果来自本地小模型。

### 3. 不产出"看起来合理的错误"

这是本项目最独特的一条。降级结果 `total_area = 0` 时若放任流入下游：

```python
calc_budget(area=0)  →  {"total": 0, "breakdown": {...}}   # 不报错，但是垃圾
```

**一个 ¥0 的装修预算比"预算无法计算"糟糕得多**——用户会拿它去和装修公司谈。
因此设计了 [`capabilities.py`](backend/app/core/capabilities.py) **业务连续性守卫**，
在数据层判断每份结果能支撑哪些操作，前后端双重拦截。

---

## 目录结构

```
backend/app/
  api/         routes.py  tasks.py  schemas.py  store.py   ← HTTP 接口层
  main.py      FastAPI 应用入口（生命周期 / trace_id / 错误翻译）
  core/        config.py  llm_client.py  mcp_client.py
               capabilities.py  logger.py  redis_client.py
  agents/      base.py  layout_parser.py         ← A-01
               layout_diagnoser.py                ← A-02
               space_planner.py                   ← A-03（fan-out 分支内）
               budget_agent.py                    ← A-04（fan-out 分支内）
               material_agent.py                  ← A-05（fan-out 分支内）
               risk_reviewer.py                   ← A-06（汇聚节点，非并行）
  schemas/     layout.py  plan.py  budget.py  material.py  risk.py
  graph/       state.py  workflow.py              ← fan-out / fan-in 编排
  services/
    image/     base.py  local_sd15.py             ← 出图 Provider（M0 已验证）
    budget/    engine.py                          ← 预算规则引擎（纯函数，零 LLM 依赖）
    material/  catalog.py                         ← 材料检索（确定性，无向量/无网络）
    knowledge/ chunking.py  store.py  retriever.py ← RAG（Chroma + Ollama embedding）
seed_data/     pricing_demo.json                  ← 价格表（演示数据，文件内已声明）
               material_catalog.json              ← 材料目录（演示数据，含竞品）
               references_manifest.yaml           ← 语料白名单（含**排除理由**）
               sample_quotes/                     ← 带标注的样本报价单（AC-06 的答案）
mcp_servers/   parse_house_layout.py
scripts/       e2e_smoke.py        真实链路端到端（会花钱，慎跑）
               review_sample_quote.py  A-06 验收：对样本报价单算召回率
               validate_references.py  语料离线验证（入 Chroma 前）
               ingest_knowledge.py     语料切块向量化入库
               bench_image.py     M0 出图基准（放行门槛）
               warmup.py          演示前预热（必须，避免 40s 冷启动）
               download_models.py / fetch_sd15_files.py
frontend/src/
  api/         client.ts（拆信封 / 错误分流 / trace_id）· types.ts
  composables/ useTaskPolling.ts                  ← 轮询节奏（需求文档 2.2.4）
  stores/      task.ts（任务台账）· health.ts（依赖状态）
  utils/       toast.ts                           ← 全项目唯一的 Element Plus 接触点
  components/  AppSidebar · AppHeader · AppIcon · PlanCard · PlanDrawer
               DiffMatrix · PhaseProgress · DegradedNotice · EmptyState
  views/       工作台 · 户型解析 · 方案生成 · 避坑审查
               材料价格 · 知识库 · 数据分析 · 用户管理
  assets/      精选素材（图标 / 案例图 / 照片 / 插画）
  tailwind.config.js                              ← 设计令牌的唯一落点
skills/        Skill 文档（Agent 的 System Prompt + 边界定义）
tests/         517 个测试（conftest.py 有网络绊线，禁止测试打真实 API）
ui参考/        设计稿与素材库（**素材不入库**，见 .gitignore；采集脚本可重建）
docs/          非代码文档（与功能文件分开存放）
  需求/         需求文档.md          2400+ 行需求与决策记录（含 4 轮修订说明）
  参考/         开源项目链接.md       开源项目逐条核实清单
  面试/         面试亮点.md          可讲事件与追问预案
```

### 前端怎么跑

```bash
cd frontend && npm install && npm run dev     # http://localhost:5173
# 另开一个终端，在仓库根目录：
python -m uvicorn backend.app.main:app --port 8000
```

设计与工程说明见 [`frontend/README.md`](frontend/README.md)。

---

## 已知限制

本项目**主动标注局限**，而不是等评审发现：

| 限制 | 原因 | 处理 |
|---|---|---|
| **只做 3D 户型渲染图，不做室内实景效果图** | 输入是俯视平面图，转人眼视角是任务错配，实测失败 | 交付物定义收窄；UI/文档措辞统一（AC-39） |
| AI 图默认无热区 | 几何非精确对齐，IoU 指标未经标定 | 热区全部走矢量图，AI 图仅作视觉呈现 |
| 本地兜底只能读房间名 | 实测小模型结构化能力不足 | 质量闸门 + 能力守卫，不假装完整 |
| 全友产品为演示数据 | 无公开结构化价格接口 | 显著标注为演示数据，不表述为真实报价 |
| 户型识别准确率未标定 | 尚无标注测试集 | M2 用 ResPlan 子集构建 |
| 出图未做批量并发 | 全局串行锁，禁止并发调图 | 单张 12.2s，3 套方案约 37s，在方案生成总时长内可接受 |

---

## 开发规范

- 所有 Agent 继承 `BaseAgent`，`execute()` 提供超时熔断与异常隔离——**单个 Agent 失败不拖垮主流程**
- 结构化输出用「schema 注入 + 宽松抽取 + Pydantic 校验重试」（**不用** LangChain 的 `with_structured_output`，实测 DeepSeek 三条路全堵）
- 价格、规范、材料数据必须可溯源，禁止编造
- 端口一律绑 `127.0.0.1`，**全部演示在本地完成**，不做公网暴露

### 推送与同步（双远端）

仓库有两个远端，**但只需手动推其中一个**：

```
本机 ──git push gitee main──▶ Gitee ◀──定时拉取── GitHub Actions ──▶ GitHub
     (国内直连，不需要代理)        (都在境外，互访无障碍)
```

**为什么要这么绕**：本机**直连不到 github.com**（实测 `curl https://github.com`
返回 `000`）。直接推 GitHub 必须先确保代理软件在跑，忘了就报
`Failed to connect to github.com port 443 after 21s` —— 而那个报错看起来像
"网络坏了"，不像"少开了个软件"。

把推送方向反过来之后，同步动作发生在 **GitHub 的服务器上**，
本机一个代理都不需要。

| 远端 | 用途 | 需要代理吗 |
|---|---|---|
| `gitee` | **日常就推这个**，源 | 不需要 |
| `origin`（GitHub） | 镜像，由 Action 自动跟进 | 不需要（不用手动推） |

`.github/workflows/sync-from-gitee.yml` 每 30 分钟检查一次 Gitee；
也可以在 GitHub 网页的 Actions 页手动点 `Run workflow` 立即同步。

⚠️ **不要直接往 GitHub 推**。两边一旦分叉，工作流会**故意失败**而不是强推覆盖
（强推会静默销毁 GitHub 上别人可能已经拉走的提交）。


## 相关文档

非代码文档统一放在 `docs/` 下，与功能文件分开：

- [docs/需求/需求文档.md](docs/需求/需求文档.md) —— 完整需求、架构决策记录（ADR）、验收清单、风险登记册
- [docs/参考/开源项目链接.md](docs/参考/开源项目链接.md) —— 逐条核实过的开源项目清单
- [docs/面试/面试亮点.md](docs/面试/面试亮点.md) —— 开发过程中可讲给面试官听的事件与追问预案
- [skills/](skills/) —— 各 Agent 的能力边界与 System Prompt（属功能文件，不随文档移动）

---

*内江市全友家居 · 创造美好家居生活*
