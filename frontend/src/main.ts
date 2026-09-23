import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
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

// 全局兜底：组件里漏掉的异常不至于让整页白屏。
// 生产环境这里应该上报，演示环境打日志就够。
app.config.errorHandler = (err, _instance, info) => {
  console.error('[未捕获的组件异常]', info, err)
}

app.mount('#app')
