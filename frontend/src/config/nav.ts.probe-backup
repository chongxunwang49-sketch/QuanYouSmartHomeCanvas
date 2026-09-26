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

/**
 * 角色。与后端 `core/auth.py` 的 `ROLE_*` 一一对应。
 *
 * ⚠️ 这里**不要**加"会员档位"这一维。菜单可见性由**角色**决定，
 * 功能可用性由**角色 × 档位**决定（见 `stores/auth.ts`）——
 * 两者混在一起的话，"会员能看见知识库"这种组合会被写成
 * `role === 'vip'` 那种把两个维度压扁的判断。
 */
export type NavRole = 'admin' | 'designer' | 'user'

export interface NavLeaf {
  /** 路由路径。也是 ⌘K 面板跳转的目标。 */
  to: string
  label: string
  icon: string
  /** ⌘K 面板的副标题。侧栏不显示（240px 宽塞不下）。 */
  hint: string
  /**
   * 哪些角色**能看见**这一项。缺省 = 所有角色都能看见。
   *
   * ⚠️ **这是"看不见"，不是"不能用"。** 两者的选择标准是：
   *
   *   看不见   这个模块对 TA 根本没有意义（语料维护、系统性能指标）
   *   看得见但置灰  这个功能对 TA 有意义但当前用不了（免费用户的方案生成）
   *
   * 后者**必须**看得见 —— 本项目一贯的立场是「拒绝必须说清缺什么、
   * 怎么办」（`core/capabilities.py`，AC-33）。把方案生成整个藏起来，
   * 免费用户就永远不知道有这个东西、也不知道开通能解锁什么。
   *
   * ⚠️ 再强调一次：**删菜单不构成防线。** 手敲 URL 一样进得去，
   * 真正拦得住的是后端 `api/deps.py`。这里的 roles 只解决"界面该显示什么"。
   */
  roles?: readonly NavRole[]
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
    /**
     * 与 `/parse` 同一个拆分理由（见 `ParseView.vue` 文件头），
     * 也是需求方 2026-09-24 点名要求的：
     * 「采用和上面「户型解析」模块一样的子目录形式，对功能进行细分
     *   （不要为了做子目录而做子目录，每个目录模块要保证不空洞）」。
     *
     * 三项各自独立回答一个问题，不是把一个页面切成三块：
     *   生成参数    要生成什么？
     *   三方案对比  三套各是什么、差在哪、我选哪套
     *   3D 装修漫游 选定那套走进去是什么样（含家具）
     *
     * 第三项是这次新做的：**家具是方案的属性，不是户型的属性**。
     * 「户型解析」的 3D 漫游是空房子，这一页才有家具。
     */
    children: [
      { to: '/generate', label: '生成参数', icon: 'gear', hint: '户型、风格档位、业主需求与材料偏好' },
      { to: '/generate/plans', label: '三方案对比', icon: 'chart-bar', hint: '三套方案横向对比与选定' },
      { to: '/generate/walkthrough', label: '3D 装修漫游', icon: 'cube', hint: '走进选定方案的装修效果' },
    ],
  },
  {
    to: '/knowledge',
    icon: 'book-open',
    label: '知识库管理',
    hint: 'RAG 语料与检索',
    /**
     * **只有管理员。** 这一页做的是语料维护（建库、重检索、看待审条目），
     * 是**管理动作**，不是设计动作 —— 设计师需要的是"引用可溯源"，
     * 那在避坑审查的结果里已经给了，不需要他来管语料本身。
     */
    roles: ['admin'],
  },
  {
    to: '/analytics',
    icon: 'chart-line',
    label: '数据分析',
    hint: '调用与性能指标',
    /** 管理员与设计师：都要拿性能数据给自己的方案做判断；普通业主不关心 */
    roles: ['admin', 'designer'],
  },
  /**
   * ⚠️ **「账号管理」与「个人中心」是两个页面，不是同一个页面的两种档位。**
   *
   * 2026-09-24 需求方明确：「只有管理员拥有用户管理界面，其它权限的账号
   * 对应的界面应该是个人中心」，且两者**排布与功能都要分开**。
   *
   * 所以这里不再是"一个页面对不同角色显示不同内容"，而是：
   *
   *   账号管理（仅管理员）  别人列表 · 权限分配 · 封禁/启用
   *   个人中心（所有角色）  我自己的账号 · 套餐 · 登录设备 · 今日额度
   *
   * 这个切分比原来的做法好在**职责单一**：管理页不需要在"我"和"他们"
   * 之间来回切换视角，个人中心也不需要为"要不要显示下拉框"做角色判断。
   * 原来那种"同页不同内容"的写法，最容易漏的就是某一行忘了加角色判断，
   * 结果普通用户看见了不该看见的控件。
   */
  {
    to: '/admin/users',
    icon: 'users',
    label: '账号管理',
    hint: '账号列表、权限分配、封禁启用',
    /** **只有管理员**。与路由 `meta.roles` 必须一致，见 router.d.ts */
    roles: ['admin'],
  },
  {
    to: '/me',
    icon: 'user-circle',
    label: '个人中心',
    hint: '我的账号、套餐与登录设备',
    /**
     * **所有角色都能看见**（含管理员）。
     *
     * 管理员也有"自己的账号"，而且后端 4005 的提示指向这里
     * （`deps.UPGRADE_HINT` 写的是「个人中心 → 我的套餐」）——
     * 藏起来会让那句提示指向一扇进不去的门。
     */
  },
]

/** 展平成一维，供 ⌘K 面板搜索用（父项 + 全部子项）。 */
export function flattenNav(): NavLeaf[] {
  return NAV.flatMap((item) => [item, ...(item.children ?? [])])
}

/**
 * 按角色过滤出可见的菜单。
 *
 * ⚠️ **`role === null`（还没登录 / 还没求证完）返回空数组**，而不是全给。
 * 给全的话，刷新页面时侧栏会先渲染出管理员才有的菜单、再收回去 ——
 * 那一瞬间的闪动比慢半拍更让人怀疑系统坏了。空数组由调用方渲染骨架。
 */
export function navForRole(role: NavRole | null): NavItem[] {
  if (!role) return []
  return NAV.filter((item) => !item.roles || item.roles.includes(role))
}

/** 展平 + 按角色过滤。⌘K 面板用这个，否则搜得到进不去的页面。 */
export function flattenNavForRole(role: NavRole | null): NavLeaf[] {
  return navForRole(role).flatMap((item) => [item, ...(item.children ?? [])])
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
