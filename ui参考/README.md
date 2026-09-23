# ui参考 —— 前端素材库

**396 个素材，44.4 MB，五个来源。** 由 `scripts/collect_ui_assets.py` 采集，
可重跑、可续跑、可审计。

```
python scripts/collect_ui_assets.py                    # 全量
python scripts/collect_ui_assets.py --only undraw      # 只补某个源
python scripts/collect_ui_assets.py --max-per-section 45
```

---

## 目录

| 目录 | 数量 | 体积 | 用途 |
|---|---:|---:|---|
| `00-UI参考截图/` | — | — | 用户自备的界面参考 |
| `01-官网实拍/` | 171 | 34 MB | 全友官网真实素材，**最高优先** |
| `02-插画-unDraw/` | 56 | 0.7 MB | 空状态、功能说明插画（SVG） |
| `03-图标-Iconify/` | 94 | 153 KB | 界面图标（SVG，Phosphor + Tabler） |
| `04-照片-Pexels/` | 35 | 7.6 MB | 家居实拍，补官网没有的场景 |
| `05-占位图-Picsum/` | 40 | 3.5 MB | 开发期占位 |
| `stitch_ai/` | — | — | 用户自备的 UI 稿（见下） |

### 01-官网实拍 细分

| 子目录 | 数量 | 说明 |
|---|---:|---|
| `装修案例/` | 63 | **实景案例，最有价值的一批**——真实装修完成的房间 |
| `站内Logo与图标/` | 61 | 全友 Logo、导航图标、吉祥物 |
| `产品-卫浴/` | 16 | |
| `产品-窗帘软装/` | 16 | |
| `产品-热销/` | 11 | |
| `产品-定制橱柜/` | 3 | 官网橱柜列表页是 JS 渲染的，静态页只剩 3 张 |
| `品牌视觉/` | 1 | 品牌页以文字和站点 chrome 为主 |

---

## 两类注意事项

### 1. 版权 —— 用之前请先读这段

| 来源 | 许可 |
|---|---|
| **官网实拍** | **全友家居版权所有。** 本项目是 全友 品牌命题作品，作演示与设计参考可以；**不得商用、不得二次分发**，上线前须替换或取得授权 |
| unDraw | 开放许可，可商用可修改，无需署名 |
| Iconify | 各图标集合许可不同（Phosphor / Tabler 均为 MIT） |
| Pexels | Pexels 许可，免费商用无需署名 |
| Picsum | 取自 Unsplash，**仅作占位**，上线前必须替换 |

`MANIFEST.json` 里每条素材都带 `license` 字段和 `source_url`，审计时以它为准。

### 2. 采集过程踩到的四个坑（都已修在脚本里）

- **Windows 系统代理是坏的，而 `requests` 默认会去吃它**（`trust_env=True`）。
  症状极隐蔽：一部分请求静默失败、另一部分正常，同一时刻 `curl` 访问同一个
  URL 却是 200 —— 第一轮据此误判成"对端在封我"。已置 `_SESSION.trust_env = False`。
- **Pexels 的 `www` 全站被 Cloudflare 挡**（403，区域子域与 Googlebot UA 同样被挡），
  但 `images.pexels.com` 直连可用。所以脚本**只按 ID 下载、不按关键词搜索**。
  想用关键词搜，需要一个免费的 Pexels API key。
- **undraw.co 的 `/api/illustrations` 已随改版失效**，现走 Next.js 的
  `/_next/data/{buildId}/illustrations/{page}.json`。buildId 每次现抓，不硬编码。
- **Iconify 公开 API 限流很紧**，逐张下载会成片 429（加退避后一轮要跑半小时）。
  改用批量接口后 114 次请求压到 6 次，约 30 秒完成。

---

## `stitch_ai/` —— 本项目的 UI 稿

Stitch 导出的三份设计稿 + 一份设计系统。**`screen.png` 是坏的**（导出时
预览图没带上，三个文件都是 28 字节的占位文本），但 `code.html` 是完整的，
设计令牌可以直接从 CSS 里读。

| 文件 | 内容 |
|---|---|
| `code.html` | 工作台总览：户型图解析与重建、最近方案列表 |
| `agent/code.html` | 多方案智能对比与价值评估、Diff Matrix、方案深度报告 |
| `cad_tab/code.html` | 户型智能识别与空间拓扑重构（CV-BIM SYNC）、空间拓扑房间清单 |
| `botanical_warmth_natural_living/DESIGN.md` | **设计系统全量令牌** |

### 设计系统要点（`DESIGN.md`）

**「Botanical Warmth & Natural Living」** —— 有机木质 + 植物生命力，
浅色阳光模式，明确**禁用纯黑、禁用霓虹渐变、禁用科技感冷灰**。

```css
--primary:            #513825;  /* 深栗棕 · 主色 */
--primary-container:  #6b4f3a;  /* 栗棕容器 */
--secondary:          #376847;  /* 叶绿 · 正向/确认 */
--secondary-container:#b6edc2;  /* 浅鼠尾草绿 */
--tertiary:           #4f3a00;  /* 黄铜 · 指标高亮 */
--background:         #fff8f3;  /* 蛋壳画布 */
--surface-container:  #fcebd8;  /* 暖亚麻面 */
--on-surface:         #221a0e;  /* 深暖咖啡 —— 不是 #000 */
--on-surface-variant: #4f453e;  /* 次级文字 */
--outline-variant:    #d3c4ba;  /* 分隔线 */
```

- **字体**：`Newsreader`（衬线，标题/展示） + `Manrope`（无衬线，正文/UI）
- **圆角**：卡片 `1rem`–`1.5rem`；输入控件 `0.5rem`–`0.75rem`；标签/状态胶囊 `9999px`
- **阴影**：全部用栗棕色调，不用中性黑
  - Level 1 卡片 `0 4px 20px rgba(107, 79, 58, .04)`
  - Level 2 浮层 `0 12px 32px rgba(107, 79, 58, .08)`
- **间距**：`xs .25 / sm .5 / md 1 / lg 1.5 / xl 2.5 / 2xl 4 rem`
- **栅格**：桌面 12 列 24px 沟槽 · 平板 8 列 20px · 移动 4 列 16px

### 信息架构（三份稿子的共同导航）

```
工作台 · 户型解析 · 方案生成 · 知识库管理 · 数据分析 · 用户管理
```

---

## `MANIFEST.json`

396 条素材的完整台账。每条含：

```json
{
  "path": "01-官网实拍/装修案例/image_1789550343_IMBp6hpG.jpg",
  "source": "quanyou",
  "source_url": "https://www.quanyou.com.cn/attachment/images/2026/09/17/image_1789550343_IMBp6hpG.jpg",
  "source_page": "https://www.quanyou.com.cn/cases.html",
  "bytes": 2501185,
  "sha1": "…",
  "license": "全友家居官网素材，版权归全友家居所有；仅作本项目演示与设计参考，不得商用"
}
```

**`sha1` 可用来验证文件未被改动**；`source_page` 记录它是从哪个页面抠出来的
（同一张图可能出现在多个页面，这里记的是首次采集到它的那个）。
