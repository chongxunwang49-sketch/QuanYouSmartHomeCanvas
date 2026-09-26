import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

/**
 * 路由。`meta.title` / `meta.subtitle` 由各页面自己渲染（页面标题在
 * 内容区而不是顶栏——这是设计稿的结构：顶栏只有搜索和动作）。
 */
const routes: RouteRecordRaw[] = [
  /**
   * 登录页。**唯一免登录的路由，且不套应用外壳。**
   *
   * 放在最前面是有意的：它是"进门"的那一步，读代码的人从上往下看
   * 就知道整个应用的前提是什么。
   *
   * `meta.public` 显式标出免登录 —— 守卫的默认是"需要登录"，
   * 因为漏标一个受保护路由的代价（数据泄漏）远大于漏标一个公开路由
   * （多一次登录）。
   */
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', layout: 'bare', public: true },
  },
  {
    path: '/',
    name: 'dashboard',
    component: () => import('@/views/DashboardView.vue'),
    meta: { title: '工作台总览' },
  },
  /**
   * 户型解析 —— **嵌套路由**。
   *
   * ⚠️ 这里的分层不是为了"路径好看"，是**功能性的**：
   *
   * `ParseView.vue` 持有解析会话（轮询 + 结果，见 `useParseSession`），
   * 而 `useTaskPolling` 有 `onScopeDispose(stop)` —— 谁卸载谁停轮询。
   * 父路由组件在 `/parse` 及其全部子路由下**始终挂载**，所以用户在
   * 「识别总览 → 3D 漫游」之间切换时，正在跑的解析不会被掐断。
   *
   * 拆子路由的动因是另一个：识别结果是重内容（2D 矢量图 ~1000px、
   * 3D 漫游 ~600px、五维诊断 ~700px），全部平铺在一页里纵向超过
   * 3000px，用户要一直滚。拆成"每页只答一个问题"。
   */
  {
    path: '/parse',
    component: () => import('@/views/ParseView.vue'),
    children: [
      {
        path: '',
        name: 'parse',
        component: () => import('@/views/parse/ParseUploadView.vue'),
        meta: { title: '户型图解析与重建' },
      },
      {
        path: 'overview',
        name: 'parse-overview',
        component: () => import('@/views/parse/ParseOverviewView.vue'),
        meta: { title: '识别总览' },
      },
      {
        path: 'drawing',
        name: 'parse-drawing',
        component: () => import('@/views/parse/ParseDrawingView.vue'),
        meta: { title: '户型矢量图' },
      },
      {
        path: 'walkthrough',
        name: 'parse-walkthrough',
        component: () => import('@/views/parse/ParseWalkthroughView.vue'),
        meta: { title: '3D 漫游' },
      },
      {
        path: 'diagnosis',
        name: 'parse-diagnosis',
        component: () => import('@/views/parse/ParseDiagnosisView.vue'),
        meta: { title: '户型诊断' },
      },
    ],
  },
  /**
   * 方案生成 —— **嵌套路由**，与 `/parse` 同一个结构、同一个理由。
   *
   * ⚠️ 2026-09-24 需求方明确要求：
   *   「在「方案生成」模块采用和上面「户型解析」模块一样的子目录形式，
   *     对功能进行细分（**不要为了做子目录而做子目录，每个目录模块
   *     要保证不空洞**）」
   *
   * 拆分的判据因此是"这一页能不能独立回答一个问题"：
   *   生成参数    要生成什么？（户型、风格×档位、需求、材料偏好）
   *   三方案对比  三套方案各是什么、差在哪、我要哪一套
   *   3D 装修漫游 选定那套走进去是什么样
   *
   * 三层都是重内容（表单 ~700px、三张方案卡 ~900px、3D 画布 74vh），
   * 平铺一页超过 3000px —— 与 `/parse` 当初拆分的动因完全一样。
   *
   * ⚠️ 拆分的**功能性前提**同样继承自 `/parse`：生成要跑 90 秒以上，
   *    而 `useTaskPolling` 是"谁卸载谁停轮询"。`GenerateView.vue`
   *    作为父路由组件在 `/generate` 及其全部子路由下始终挂载，
   *    所以用户在子页之间切换不会掐断正在跑的生成。
   */
  {
    path: '/generate',
    component: () => import('@/views/GenerateView.vue'),
    children: [
      {
        path: '',
        name: 'generate',
        component: () => import('@/views/generate/GenerateSetupView.vue'),
        meta: { title: '多方案智能对比与价值评估' },
      },
      {
        path: 'plans',
        name: 'generate-plans',
        component: () => import('@/views/generate/GeneratePlansView.vue'),
        meta: { title: '三方案对比' },
      },
      {
        path: 'walkthrough',
        name: 'generate-walkthrough',
        component: () => import('@/views/generate/GenerateWalkthroughView.vue'),
        meta: { title: '3D 装修漫游' },
      },
    ],
  },
  {
    path: '/review',
    name: 'review',
    component: () => import('@/views/ReviewView.vue'),
    meta: { title: '报价单避坑审查' },
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('@/views/KnowledgeView.vue'),
    // roles 与 `config/nav.ts` 的 `NavItem.roles` 必须一致，见 router.d.ts
    meta: { title: '知识库管理', roles: ['admin'] },
  },
  {
    path: '/analytics',
    name: 'analytics',
    component: () => import('@/views/AnalyticsView.vue'),
    meta: { title: '数据分析', roles: ['admin', 'designer'] },
  },
  /**
   * ⚠️ **「账号管理」与「个人中心」是两个页面**（2026-09-24 需求方明确）。
   *
   * 原来只有一个 `/users`，靠角色判断在同一页里显示不同内容。
   * 那样写的问题不在"能不能实现"，而在**容易漏**：某一行忘了加角色判断，
   * 普通用户就看见了不该看见的控件。拆成两页之后，管理页可以整体
   * `roles: ['admin']` 拦住，个人中心里根本没有管理类控件可漏。
   */
  {
    path: '/admin/users',
    name: 'admin-users',
    component: () => import('@/views/UsersAdminView.vue'),
    meta: { title: '账号管理', roles: ['admin'] },
  },
  {
    path: '/me',
    name: 'me',
    component: () => import('@/views/ProfileView.vue'),
    // 无 roles = 所有已登录角色都能进。管理员也有"自己的账号"。
    meta: { title: '个人中心' },
  },
  {
    path: '/materials',
    name: 'materials',
    component: () => import('@/views/MaterialsView.vue'),
    meta: { title: '材料价格查询' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

/**
 * 登录守卫（AC-01）。
 *
 * ══════════════════════════════════════════════════════════════════
 * ⚠️ 守卫是"体验层"，不是"防线"
 * ══════════════════════════════════════════════════════════════════
 * 它能挡住的是"未登录的人点进来看到空页面"，挡不住任何真正的越权 ——
 * 任何人都可以改 localStorage 里的令牌、或者干脆用 curl 打接口。
 * 真正的判据在后端 `api/deps.py` 的 `current_user`。
 * 这里做守卫的理由只有一条：**让用户在进门时就知道要登录**，
 * 而不是点进某个功能才收到 4003。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么要 await restore()
 * ══════════════════════════════════════════════════════════════════
 * 刷新页面时 localStorage 里有令牌，但 `user` 还是 null（信息不持久化）。
 * 不等这一次求证就判定"未登录"，用户每刷新一次就被踢回登录页 ——
 * 而他的令牌其实完好。
 *
 * `restore()` 内部会缓存 `restored` 标志，所以连续导航不会重复打请求。
 */
router.beforeEach(async (to) => {
  // 必须在守卫**内部**取 store：模块顶层取的话 pinia 还没装，
  // 拿到的是 undefined（`main.ts` 里 `app.use(createPinia())` 在前）。
  const auth = useAuthStore()

  if (to.meta.public) {
    /**
     * 已登录的人不该停在登录页 —— 那会表现为"刷新后回到登录页，
     * 明明已经登录了"。只有在**本地有令牌**时才去求证，
     * 避免每次访问登录页都白打一个 `/auth/me`。
     */
    if (to.name === 'login' && auth.token) {
      await auth.restore()
      if (auth.isLoggedIn) return { path: '/' }
    }
    return true
  }

  await auth.restore()

  if (!auth.isLoggedIn) {
    // 记住原本要去哪 —— 登录成功后直接送过去，而不是一律回工作台。
    // `redirect` 只带路径不带查询串之外的敏感内容，不做解码回跳。
    return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }

  /**
   * 角色门控。
   *
   * ⚠️ 打在"弹回工作台"而不是"渲染一个 403 页面"上，是有意的：
   * 用户是从 ⌘K、从旧书签、或直接敲 URL 到这里的，TA 并没有做错什么，
   * 给一个报错页只会让人以为系统坏了。**但也不能静默** ——
   * 所以落点带一个 `denied` 查询参数，由工作台如实说一句
   * 「「知识库管理」需要管理员权限，已返回工作台」。
   *
   * 这仍是界面层。真正的判据在后端 `api/deps.py`：本页目前没有任何
   * 需要保护的后端资源（知识库是本地文件、数据分析页还没有接口），
   * 所以这里没有可对照的服务端门控。**等那两页接上后端读写接口时，
   * 必须同时加上服务端判定** —— 只留这一层等于没做。
   */
  const allowed = to.meta.roles
  if (allowed && auth.role && !allowed.includes(auth.role)) {
    return { path: '/', query: { denied: String(to.meta.title ?? to.path) } }
  }

  return true
})

export default router
