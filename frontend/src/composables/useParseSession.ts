import { computed, inject, onMounted, provide, type ComputedRef, type InjectionKey } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useTaskStore } from '@/stores/task'
import { useTaskPolling, type TaskPolling } from '@/composables/useTaskPolling'
import type { ParseResult } from '@/api/types'

/**
 * 解析会话 —— `/parse` 整棵子树共享的那一份状态。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么必须由父路由持有，而不是各子页自己 useTaskPolling
 * ══════════════════════════════════════════════════════════════════
 * `useTaskPolling` 里有一句 `onScopeDispose(stop)` —— **谁创建、谁卸载、
 * 谁停轮询**。如果把轮询放在子页（比如「3D 漫游」）里，用户在解析进行中
 * 从「识别总览」切到「3D 漫游」，上一个子页卸载就会把轮询掐掉：
 * 进度条停在原地，而任务其实还在后端跑。
 *
 * 这正是本项目之前修过一次的同一类 bug（见 useTaskPolling 的会话记忆
 * 注释）。所以这里把轮询放在**父路由组件** `ParseView.vue` 的作用域里 ——
 * 它在 `/parse` 及其全部子路由下始终挂载，子页之间怎么切都不影响它。
 *
 * 子页通过 `useParseSession()` 拿同一份结果，不需要 props 透传、
 * 也不需要新建 store。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么用 provide/inject 而不是 Pinia
 * ══════════════════════════════════════════════════════════════════
 * 因为这份状态**有明确的生命周期**：它属于 `/parse` 这棵子树，
 * 而不属于整个应用。放进 Pinia 会变成全局单例，别的页面也能拿到
 * 一个"上次解析的结果"，那不是我们想要的语义。
 * （`stores/` 里现有两个 store —— health 和 task —— 都是真的全局。）
 */

export interface ParseSession {
  /** 轮询句柄。上传页调 `start()`，其它页只读。 */
  poll: TaskPolling<ParseResult>
  /** 最近一次任务状态（含 result / phase / progress / error）。 */
  status: TaskPolling<ParseResult>['status']
  /** 解析结果。null 表示还没有结果（未开始 / 进行中 / 失败）。 */
  result: ComputedRef<ParseResult | null>
  layout: ComputedRef<ParseResult['layout'] | null>
  diagnosis: ComputedRef<ParseResult['diagnosis'] | null>
  /** 图片质量预检结果（AC-27）。不合格时任务失败，但 result 里仍带着它。 */
  precheck: ComputedRef<ParseResult['precheck'] | null>

  /**
   * 被预检拦下的情况：这时 `result` 只有 `precheck`，`layout`/`diagnosis`
   * 全是 null，照常渲染会得到一屏空框。
   */
  rejectedByPrecheck: ComputedRef<boolean>
  /** `mode === 'degraded_basic'`：只有 `rooms[].name` 有效（需求 2.2.4）。 */
  isDegradedBasic: ComputedRef<boolean>

  /** 能力守卫（后端在入口就拦，前端据此置灰 —— 两道都要有）。 */
  capabilities: ComputedRef<ParseResult['capabilities'] | null | undefined>
  canGenerate: ComputedRef<boolean>
  blockedReason: ComputedRef<string>

  /** 有没有可以展示的结果（含"被预检拦下"这种失败态）。 */
  hasResult: ComputedRef<boolean>

  roomType: (t: string) => string
  scoreTone: (score: number) => string
  goGenerate: () => void
}

const KEY: InjectionKey<ParseSession> = Symbol('qy.parse-session')

const ROOM_TYPE_LABEL: Record<string, string> = {
  living_room: '客厅',
  bedroom: '卧室',
  kitchen: '厨房',
  bathroom: '卫生间',
  dining_room: '餐厅',
  study: '书房',
  balcony: '阳台',
  storage: '储藏',
  other: '其它',
}

/**
 * 在 `ParseView.vue`（父路由组件）里调用一次，创建并向下提供会话。
 */
export function provideParseSession(): ParseSession {
  const router = useRouter()
  const route = useRoute()
  const tasks = useTaskStore()

  /** 会话键：轮询状态跨路由切换保留，见 useTaskPolling 的说明 */
  const poll = useTaskPolling<ParseResult>('parse')

  const result = computed(() => poll.status.value?.result ?? null)
  const layout = computed(() => result.value?.layout ?? null)
  const diagnosis = computed(() => result.value?.diagnosis ?? null)
  const precheck = computed(() => result.value?.precheck ?? null)

  const rejectedByPrecheck = computed(
    () => poll.status.value?.status === 'failed' && precheck.value != null && !precheck.value.ok,
  )

  const isDegradedBasic = computed(() => layout.value?.mode === 'degraded_basic')

  const capabilities = computed(() => {
    const fromResult = result.value?.capabilities
    const fromLayout = layout.value?.capabilities
    return fromResult ?? fromLayout ?? null
  })

  const canGenerate = computed(() => {
    if (isDegradedBasic.value) return false // 降级时结构字段全空，生成无意义
    if (!layout.value) return false
    const c = capabilities.value
    if (c && typeof c.can_generate_plan === 'boolean') return c.can_generate_plan
    // 后端没给能力报告时，按"有没有房间和面积"自行判断——
    // 这不是替后端做决定，只是避免界面出现一个必然失败的可点按钮。
    return (layout.value.rooms?.length ?? 0) > 0 && (layout.value.total_area ?? 0) > 0
  })

  const blockedReason = computed(() => {
    if (isDegradedBasic.value) return '当前为降级解析结果，结构与面积字段不可用'
    const c = capabilities.value
    if (c?.suggestion) return String(c.suggestion)
    if (!(layout.value?.total_area ?? 0)) return '缺少户型总面积，无法估算造价'
    return ''
  })

  const hasResult = computed(() => result.value != null || rejectedByPrecheck.value)

  const roomType = (t: string) => ROOM_TYPE_LABEL[t] ?? t

  /** 分数配色。不做"低于 60 就是红的"这种武断判断——只是视觉分档。 */
  function scoreTone(score: number) {
    if (score >= 80) return 'text-botanical'
    if (score >= 60) return 'text-wood'
    return 'text-accent-gold'
  }

  function goGenerate() {
    const id = result.value?.layout_id
    if (!id) return
    router.push({ path: '/generate', query: { layout: id } })
  }

  /**
   * 恢复上一次的任务。
   *
   * ⚠️ 光看地址栏的 query **不够** —— 从侧栏点回「户型解析」走的是
   * `router.push('/parse')`，query 是空的。而任务其实还在后端跑
   * （结果留 1 小时），前端却把它忘了：用户回来看到的是一张白纸。
   *
   * 三级兜底：
   *   ① 地址栏 query（同一个标签页内刷新）
   *   ② 轮询会话表（路由切走再切回，进程内存里还有）
   *   ③ sessionStorage（整页刷新、甚至关了标签页重开）
   */
  onMounted(async () => {
    const taskId = String(route.query.task || '') || poll.savedTaskId()
    if (!taskId) return

    // 把 id 写回地址栏：刷新、分享链接都还能用
    if (String(route.query.task || '') !== taskId) {
      router.replace({ query: { ...route.query, task: taskId } })
    }

    const snap = await poll.start(taskId)
    if (!snap) return
    tasks.track({
      taskId,
      kind: 'parse',
      summary: snap.result?.layout
        ? `${snap.result.layout.rooms?.length ?? 0} 个房间 · ${snap.result.layout.total_area ?? 0} ㎡`
        : '',
    })
    tasks.update(taskId, { status: snap.status, phaseText: snap.phase_text })
  })

  const session: ParseSession = {
    poll,
    status: poll.status,
    result,
    layout,
    diagnosis,
    precheck,
    rejectedByPrecheck,
    isDegradedBasic,
    capabilities,
    canGenerate,
    blockedReason,
    hasResult,
    roomType,
    scoreTone,
    goGenerate,
  }

  provide(KEY, session)
  return session
}

/**
 * 在 `/parse` 的任意子页里调用，拿到父组件提供的那一份。
 *
 * 拿不到就直接抛 —— 这属于"组件挂错了位置"的编码错误，
 * 静默返回一个空对象只会让症状延后到某个 computed 里再炸。
 */
export function useParseSession(): ParseSession {
  const session = inject(KEY)
  if (!session) {
    throw new Error(
      'useParseSession() 必须在 /parse 的子路由组件里调用 —— ' +
        '会话由 ParseView.vue 提供（见 useParseSession.ts 文件头）。',
    )
  }
  return session
}
