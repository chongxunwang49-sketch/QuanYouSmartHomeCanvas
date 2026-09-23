/**
 * 导航配置 —— **菜单的唯一真源**。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么要有这个文件
 * ══════════════════════════════════════════════════════════════════
 * 在此之前菜单有两份，分别在 `AppSidebar.vue` 和 `AppHeader.vue`：
 *
 *   AppSidebar   `NAV`    —— { to, icon, label }
 *   AppHeader    `routes` —— { label, hint, icon, run }   ← run 是闭包不是路径
 *
 * 两份字段不同、各自维护，靠"改完记得改另一边"维持一致。这在 6 个
 * 平级菜单项时勉强能忍，但**加了子列表之后就不行了**：子项只在侧栏
 * 有、⌘K 搜不到，用户会认为是 bug（"我明明看见这个页面"）。
 *
 * 所以抽到这里，两边都从这里读，`run` 由 `to` 现推。
 *
 * ══════════════════════════════════════════════════════════════════
 * 子列表（children）的选中判定
 * ══════════════════════════════════════════════════════════════════
 * ⚠️ 顶层项和子项的判定规则**不一样**，这是刻意的：
 *
 *   顶层项  `startsWith`  —— 在 `/parse/overview` 时，「户型解析」也要亮
 *   子项    `===`         —— 否则 `/parse` 会把自己所有的子路由都点亮
 *
 * 后者不是理论问题：子项里的「上传解析」路由就是 `/parse`，而它是
 * `/parse/overview` 等所有子路由的前缀。用 startsWith 的话，进任意
 * 子页面都会看到「上传解析」和真实所在项**同时高亮**。
 */

export interface NavLeaf {
  /** 路由路径。也是 ⌘K 面板跳转的目标。 */
  to: string
  label: string
  icon: string
  /** ⌘K 面板的副标题。侧栏不显示（240px 宽塞不下）。 */
  hint: string
}

export interface NavItem extends NavLeaf {
  /** 二级列表。有子项的顶层项在侧栏可展开。 */
  children?: NavLeaf[]
}

/**
 * 核心工作台。
 *
 * 顺序即优先级：工作台 → 解析 → 生成 → 知识库 → 数据 → 用户。
 * 注意 `/review`（避坑审查）与 `/materials`（材料价格）**不在这里** ——
 * 它们没有独立入口，从工作台卡片和解析结果的「下一步」跳过去。
 */
export const NAV: readonly NavItem[] = [
  {
    to: '/',
    icon: 'squares-four',
    label: '工作台',
    hint: '总览与最近任务',
  },
  {
    to: '/parse',
    icon: 'blueprint',
    label: '户型解析',
    hint: '上传户型图并识别',
    /**
     * 识别结果是**重内容**（2D 矢量图 ~1000px、3D 漫游 ~600px、
     * 五维诊断 ~700px），全平铺在一页里会拉到 3000px 以上。
     * 拆成子页，每页只答一个问题。
     */
    children: [
      { to: '/parse', label: '上传解析', icon: 'upload-simple', hint: '上传户型图，开始识别' },
      { to: '/parse/overview', label: '识别总览', icon: 'house-line', hint: '房间清单、面积与数据缺口' },
      { to: '/parse/drawing', label: '户型矢量图', icon: 'layout', hint: '2D 矢量图与物品热区' },
      { to: '/parse/walkthrough', label: '3D 漫游', icon: 'cube', hint: '第一人称走进户型' },
      { to: '/parse/diagnosis', label: '户型诊断', icon: 'chart-bar', hint: '五维评分与执行轨迹' },
    ],
  },
  {
    to: '/generate',
    icon: 'sparkle',
    label: '方案生成',
    hint: '多方案对比与明细',
  },
  {
    to: '/knowledge',
    icon: 'book-open',
    label: '知识库管理',
    hint: 'RAG 语料与检索',
  },
  {
    to: '/analytics',
    icon: 'chart-line',
    label: '数据分析',
    hint: '调用与性能指标',
  },
  {
    to: '/users',
    icon: 'users',
    label: '用户管理',
    hint: '账号与权限',
  },
]

/** 展平成一维，供 ⌘K 面板搜索用（父项 + 全部子项）。 */
export function flattenNav(): NavLeaf[] {
  return NAV.flatMap((item) => [item, ...(item.children ?? [])])
}

/**
 * 侧栏高亮判定。顶层与子项规则不同，见文件头说明。
 *
 * `hasChildren` 由调用方传入而不是这里推断 —— 因为同一个路径可能既是
 * 顶层项（`/parse`）又出现在子项里（「上传解析」），判定要看**它出现在
 * 哪一层**，光看路径分不出来。
 */
export function isNavActive(path: string, to: string, exact: boolean): boolean {
  if (to === '/') return path === '/'
  return exact ? path === to : path.startsWith(to)
}
