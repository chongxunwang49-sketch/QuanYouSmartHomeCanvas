import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { setUnauthorizedHandler } from './api/client'
import router from './router'
import { useAuthStore } from './stores/auth'
import './style.css'

/**
 * 入口。
 *
 * ⚠️ **没有全量注册 Element Plus。** 只按需引入用到的组件
 * （见各页面里的 `el-upload` / `el-drawer` 等），因为全量引入会把
 * 整个组件库打进首屏 chunk —— 而这个界面 90% 是自绘的，
 * Element 只用来兜住上传、抽屉、表格这几个复杂交互。
 *
 * 样式变量仍然整体覆盖（style.css 里改的是 CSS 变量），
 * 所以按需引入不会导致"只有用到的组件才是新皮肤"这种割裂。
 */
const app = createApp(App)
app.use(createPinia())
app.use(router)

/**
 * 登录失效的统一出口。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么在这里注册，而不是写在 `api/client.ts` 里
 * ══════════════════════════════════════════════════════════════════
 * `client.ts` 直接 import 路由会形成 `client → router → 页面 → api/index
 * → client` 的环。所以它只暴露一个注册口，由**组合根**（本文件）把
 * 「拿到 4003 该干什么」注入进去 —— 依赖方向是单向的。
 *
 * ══════════════════════════════════════════════════════════════════
 * 三件事的顺序
 * ══════════════════════════════════════════════════════════════════
 * ① 清本地登录态 —— 令牌已经无效了，留着只会让下一次请求再撞一次 4003
 * ② 跳登录页并带上 `redirect` —— 登录成功后回到被打断的地方
 * ③ **已经在登录页就不再跳** —— 否则用户输错口令、或页面刚加载时的
 *    一次失败请求，都会把 `redirect` 覆盖成 `/login`，登录成功后
 *    跳回登录页，看起来像"登录按钮没反应"
 */
setUnauthorizedHandler((message) => {
  const auth = useAuthStore()
  auth.logout()

  const current = router.currentRoute.value
  if (current.name === 'login') return

  router.replace({
    name: 'login',
    query: current.fullPath === '/' ? {} : { redirect: current.fullPath },
  })
  console.warn('[auth] 登录已失效，已跳转登录页：', message)
})

// 全局兜底：组件里漏掉的异常不至于让整页白屏。
// 生产环境这里应该上报，演示环境打日志就够。
app.config.errorHandler = (err, _instance, info) => {
  console.error('[未捕获的组件异常]', info, err)
}

app.mount('#app')
