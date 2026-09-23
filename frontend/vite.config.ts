import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 后端地址。默认 127.0.0.1:8000（backend/app/core/config.py 的 BACKEND_PORT）。
// 用 `/api` 前缀反代而不是直连绝对地址，是为了让前端代码里不出现 host ——
// 换环境（本机 / 容器 / nginx）时前端一行都不用改。
const BACKEND = process.env.VITE_BACKEND || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: BACKEND,
        changeOrigin: true,
        // 解析接口实测 33–48s，生成接口约 95s。dev server 的默认超时
        // 会把长请求掐断，而那正是最需要看到的一类请求。
        timeout: 300_000,
        proxyTimeout: 300_000,
      },
      // SSE / 未来的流式接口也走同一后端
      '/static': { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // ⚠️ **不要在这里点名 `element-plus`。**
        //
        // 试过写 `element: ['element-plus']`，结果那个 chunk 有 938 KB ——
        // 而项目里只用了 `ElMessage` 一个东西。原因是 manualChunks 按**包**
        // 指定分块时，Rollup 会把该包入口能到达的模块都算进去，等于把
        // tree-shaking 的结果绕过去了。
        // 让打包器自己按实际引用决定，才是对的。
        manualChunks(id) {
          if (id.includes('node_modules/echarts') || id.includes('node_modules/zrender')) {
            return 'echarts'
          }
          if (id.includes('node_modules/element-plus') || id.includes('node_modules/@element-plus')) {
            return 'element'
          }
          if (
            id.includes('node_modules/vue') ||
            id.includes('node_modules/pinia') ||
            id.includes('node_modules/axios')
          ) {
            return 'vendor'
          }
        },
      },
    },
    chunkSizeWarningLimit: 1200,
  },
})
