# 全友·智绘家 —— 前端

Vue 3 + TypeScript + Vite + Pinia + Vue Router + Tailwind + Element Plus + ECharts。

```bash
npm install
npm run dev        # http://localhost:5173，需后端在 127.0.0.1:8000
npm run build      # vue-tsc 类型检查 + 打包
npm run typecheck
```

后端：`python -m uvicorn backend.app.main:app --port 8000`（在仓库根目录跑）。
前端通过 `/api` 前缀反代到后端，**代码里不出现 host**，换环境不用改前端。

---

## 设计系统

配色、字体、间距、组件形态**全部来自 `ui参考/stitch_ai/`**——用户用 Stitch
生成的三份设计稿，加一份 `DESIGN.md` 设计令牌。

设计语言叫 **「Botanical Warmth & Natural Living」**：有机木质 + 植物生命力，
阳光浅色模式。`DESIGN.md` 里明确写了三条禁令 —— **禁纯黑、禁霓虹渐变、
禁科技感冷灰**。这三条是整套配色的立场，不是随手选的。

### 令牌（`tailwind.config.js`）

| 令牌 | 值 | 用在哪 |
|---|---|---|
| `botanical` | `#4A7C59` | 主色·叶绿：主按钮、进度条、选中态 |
| `botanical-light` | `#E8F0E5` | 侧栏选中底、标签底、状态胶囊 |
| `botanical-surface` | `#F3F8F1` | 推荐卡数值条、矩阵高亮 |
| `wood` | `#6B4F3A` | 栗棕：品牌色、次要强调 |
| `wood-dark` | `#2C2418` | **正文标题 —— 不是 `#000`** |
| `wood-muted` | `#7A6E5E` | 次级文字 |
| `warm-bg` / `warm-sidebar` | `#FAF8F3` / `#F5F2EC` | 页面底 / 侧栏与内嵌条 |
| `warm-border` / `warm-grid` | `#EDE8E0` / `#EFEAE2` | 边框 / 表格分隔 |
| `accent-gold` / `accent-red` | `#C9A961` / `#C85A48` | 黄铜点缀 / 警示 |

### 两条容易被忽略的规则

**一、衬线只给标题。** `Newsreader` 只出现在 `h1`、方案名和抽屉标题上；
正文一律 `Manrope`。衬线漫延到正文，界面会从"产品"变成"杂志"。
字号刻意压得小（正文 13px / 说明 11px / 标签 10px），层级靠**字重和颜色**拉，
不靠字号。

**二、阴影全部带暖色调。** 全站没有一处中性黑阴影——设计稿里是
`rgba(44,36,24,...)` 与 `rgba(74,124,89,...)`（推荐卡专用绿色投影）。
往色板里塞高饱和色或 `#000` 会同时破坏十几处对比度。

---

## 目录

```
src/
├── api/            client.ts（拆信封/错误分流/trace_id）· types.ts（后端契约映射）
├── composables/    useTaskPolling.ts —— 轮询节奏（需求文档 2.2.4）
├── components/     AppSidebar · AppHeader · AppIcon · PlanCard · PlanDrawer
│                   DiffMatrix · PhaseProgress · DegradedNotice · EmptyState
├── stores/         task.ts（任务台账）· health.ts（依赖状态）
├── utils/          toast.ts —— **全项目唯一的 Element Plus 接触点**
├── assets/         从 ui参考 精选的图标 / 案例图 / 照片 / 插画
└── views/          8 个页面
```

---

## 几处刻意的工程决定

### 轮询节奏是规定的，不是随手定的

`useTaskPolling.ts` 严格按 2.2.4：

```
前 10 秒  每 1 秒   →  10 秒后  每 2 秒  →  60 秒后  每 5 秒  →  120 秒 超时
```

用**循环**而不是 `setInterval`：后端单次请求超时 10 秒，而早期间隔 1 秒——
`setInterval` 会在上一个请求没回来时就发下一个，请求越堆越多。

**`estimated_seconds` 不参与调速。** 它是后端给的估算区间，不是承诺值；
拿它调速会在任务比预期慢时把轮询拖到超时——而那恰恰最需要看到进度。

### 一个 toast 不值得 938 KB

`utils/toast.ts` 从**深路径**引 Element Plus，不是 `from 'element-plus'`。

桶式导入时 Element Plus 那个 chunk 有 **938 KB**（gzip 301 KB）——而项目只用了
`ElMessage`。根因是它每个组件的 `index.mjs` 在模块顶层调 `withInstall()`，
那是真副作用，Rollup 摇不掉。改深路径后 **938 KB → 28 KB**。

代价是深路径属于内部结构，所以锁在一个文件里，将来升级只改这一处。
ECharts 同理，`echarts/core` + 显式 `use()` 把 1036 KB 降到 456 KB。

### 图片上的"材质热点"这一版没有做

设计稿里图片上浮着叶绿圆点，悬停弹出「选材热点 · 地面 / ¥129 ㎡」。
我们手上只有实景参考图，**没有"这个方案的沙发在图上的第几个像素"这种数据**。
把热点摆在编造坐标上，用户看到的是真材料名配假位置——这种"半真"比全假更坏，
因为它看起来可信。

所以材料清单平铺在图下方（信息不丢），图上明确标注
**「参考实景 · 非本方案渲染图」**。等 M5 的矢量渲染器产出真实热区坐标再接回去。

### 不编数据

- **工作台**没有同比增幅、没有趋势线。数字只来自任务台账与 `/system/health`
- **数据分析**只画本会话真实观测到的耗时；后端没暴露统计计数，
  所以**不显示** Token 消耗与调用次数，而不是估一个数填上
- **用户管理**整页标注"未实现"，并说明 M1 是主动跳过的。
  摆一排假用户能把这页填满，但会让评审者以为这块做过了
- **对比矩阵**不做"推荐"列高亮——系统明确不替用户排序方案，
  高亮第二列等于偷偷塞了一个后端刻意不做的结论。改成高亮**有差异**的项

---

## 素材

`src/assets/` 下是精选自 `ui参考/` 的素材（未精选的不进仓库）：

- `icons/` 119 个 Iconify SVG，构建期内联，**运行时零请求**
- `images/cases/` 14 张全友官网实景案例
- `images/photos/` 12 张 Pexels 家居实拍
- `illustrations/` 18 张 unDraw 插画（空状态用）

字体自托管在 `public/fonts/`（Newsreader + Manrope，952 KB），
**不走 Google Fonts CDN**——断网也能正常渲染。中文字形交给系统字体回落。

> ⚠️ 全友官网素材**版权归全友家居所有**，仅作本项目演示与设计参考，不得商用。
> 详见 `ui参考/README.md` 与 `ui参考/MANIFEST.json`。这些素材**不入库**（见根目录 `.gitignore`）。
