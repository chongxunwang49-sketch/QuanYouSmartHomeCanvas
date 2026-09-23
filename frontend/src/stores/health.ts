import { defineStore } from 'pinia'
import { ref } from 'vue'

import { systemHealth } from '@/api'
import type { HealthCheck } from '@/api/types'

/**
 * 后端依赖健康状态。
 *
 * `/system/health` **永远返回 200**，哪个依赖挂了由 `checks` 说清楚 ——
 * 这是刻意的设计：知识库没起来时前端仍应能打开页面、看到"知识库不可用"，
 * 而不是拿到一个连不上的服务直接白屏。
 *
 * 所以这里把 `status === 'degraded'` 当作**正常情况**处理：界面照常渲染，
 * 只在侧栏和设置页如实标出哪一项挂了。**不要**把它做成一个红色报错弹窗。
 */
export const useHealthStore = defineStore('health', () => {
  const status = ref<'ok' | 'degraded' | 'unknown'>('unknown')
  const checks = ref<Record<string, HealthCheck>>({})
  const version = ref('')
  const runningTasks = ref<string[]>([])
  const loaded = ref(false)
  const error = ref('')

  async function refresh() {
    try {
      const data = await systemHealth()
      status.value = data.status
      checks.value = data.checks
      version.value = data.version
      runningTasks.value = data.running_tasks
      error.value = ''
    } catch (e) {
      // 连不上后端是"未知"，不是"降级" —— 两者对用户的含义完全不同
      status.value = 'unknown'
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loaded.value = true
    }
  }

  return { status, checks, version, runningTasks, loaded, error, refresh }
})
