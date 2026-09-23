import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/**
 * 路由。`meta.title` / `meta.subtitle` 由各页面自己渲染（页面标题在
 * 内容区而不是顶栏——这是设计稿的结构：顶栏只有搜索和动作）。
 */
const routes: RouteRecordRaw[] = [
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
  {
    path: '/generate',
    name: 'generate',
    component: () => import('@/views/GenerateView.vue'),
    meta: { title: '多方案智能对比与价值评估' },
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
    meta: { title: '知识库管理' },
  },
  {
    path: '/analytics',
    name: 'analytics',
    component: () => import('@/views/AnalyticsView.vue'),
    meta: { title: '数据分析' },
  },
  {
    path: '/users',
    name: 'users',
    component: () => import('@/views/UsersView.vue'),
    meta: { title: '用户管理' },
  },
  {
    path: '/materials',
    name: 'materials',
    component: () => import('@/views/MaterialsView.vue'),
    meta: { title: '材料价格查询' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export default createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})
