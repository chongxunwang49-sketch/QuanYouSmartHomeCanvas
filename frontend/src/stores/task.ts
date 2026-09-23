import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type { TaskKind } from '@/api/types'

/**
 * 任务台账。
 *
 * 存在的理由很实际：**异步任务的结果在页面之间是要接得上的**。
 * 用户在「户型解析」提交后切到「工作台」，再切回来时不该看到一张白纸——
 * 后端的结果在 Redis 里留 1 小时（`RESULT_TTL`），前端这份台账负责把
 * 用户领回去。
 *
 * ⚠️ **不持久化。** 刷新页面后台账清空，但 `task_id` 仍在地址栏的
 * query 里（各页面自己维护），所以刷新不会丢任务——这比把 task_id
 * 塞进 localStorage 更干净：后者会在用户重装/换浏览器后留下死引用。
 */
export interface TaskEntry {
  taskId: string
  kind: TaskKind
  kindLabel: string
  phaseText: string
  status: 'pending' | 'processing' | 'completed' | 'failed'
  degraded: boolean
  createdAt: number
  /** 结束时刻。用来算真实耗时 —— **不编造性能数字**，只用观测到的 */
  finishedAt: number | null
  /** 完成后的一句话摘要，列表里展示 */
  summary: string
  /** 点回去时跳哪 */
  route: string
}

const KIND_LABEL: Record<TaskKind, string> = {
  parse: '户型解析',
  generate: '方案生成',
  review: '避坑审查',
}

const KIND_ROUTE: Record<TaskKind, string> = {
  parse: '/parse',
  generate: '/generate',
  review: '/review',
}

/** 台账上限。超出后丢最旧的——它是"最近用过的"，不是审计日志。 */
const MAX_ENTRIES = 20

export const useTaskStore = defineStore('task', () => {
  const entries = ref<TaskEntry[]>([])

  const recent = computed(() => [...entries.value].sort((a, b) => b.createdAt - a.createdAt))
  const runningCount = computed(
    () => entries.value.filter((e) => e.status === 'processing' || e.status === 'pending').length,
  )
  const completed = computed(() => entries.value.filter((e) => e.status === 'completed'))

  function track(input: {
    taskId: string
    kind: TaskKind
    summary?: string
    route?: string
  }): TaskEntry {
    const entry: TaskEntry = {
      taskId: input.taskId,
      kind: input.kind,
      kindLabel: KIND_LABEL[input.kind] ?? input.kind,
      phaseText: '已接收，正在排队…',
      status: 'pending',
      degraded: false,
      createdAt: Date.now(),
      finishedAt: null,
      summary: input.summary ?? '',
      route: input.route ?? `${KIND_ROUTE[input.kind] ?? '/'}?task=${input.taskId}`,
    }
    const existing = entries.value.findIndex((e) => e.taskId === input.taskId)
    if (existing >= 0) entries.value.splice(existing, 1)
    entries.value.unshift(entry)
    if (entries.value.length > MAX_ENTRIES) entries.value.length = MAX_ENTRIES
    return entry
  }

  /** 轮询每拿到一帧就更新一次，工作台的"进行中"列表才不是静止的。 */
  function update(
    taskId: string,
    patch: Partial<Pick<TaskEntry, 'phaseText' | 'status' | 'degraded' | 'summary'>>,
  ) {
    const e = entries.value.find((x) => x.taskId === taskId)
    if (!e) return
    Object.assign(e, patch)
    // 终态时打上结束时间。只打一次 —— 后续的 update 不该把它往后推，
    // 否则"耗时"会随着界面刷新一直变大。
    if (!e.finishedAt && (e.status === 'completed' || e.status === 'failed')) {
      e.finishedAt = Date.now()
    }
  }

  function remove(taskId: string) {
    const i = entries.value.findIndex((e) => e.taskId === taskId)
    if (i >= 0) entries.value.splice(i, 1)
  }

  function clear() {
    entries.value = []
  }

  return { entries, recent, runningCount, completed, track, update, remove, clear }
})
