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
  {
    path: '/parse',
    name: 'parse',
    component: () => import('@/views/ParseView.vue'),
    meta: { title: '户型图解析与重建' },
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
