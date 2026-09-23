import { onScopeDispose, readonly, ref, type DeepReadonly, type Ref } from 'vue'

import { taskStatus } from '@/api'
import { BizError, type TaskStatusData } from '@/api/types'
import { NetErrorCode, messageOf } from '@/api/client'

/**
 * 任务状态轮询。
 *
 * ══════════════════════════════════════════════════════════════════
 * 节奏是需求文档 2.2.4 规定的，不是随手定的
 * ══════════════════════════════════════════════════════════════════
 *
 *   前 10 秒   每 1 秒   —— 解析前期阶段变化快，要跟得上
 *   10 秒后    每 2 秒   —— 进入长尾阶段，降低频率
 *   60 秒后    每 5 秒   —— 接近超时，进一步降频
 *   120 秒     停止，提示超时并提供 trace_id
 *
 * 这条曲线解决的是「打爆后端」与「更新不流畅」的两难：固定 1 秒轮询在
 * 95 秒的生成任务上要打 95 次，固定 5 秒又会让前期的阶段跳变看起来卡顿。
 *
 * ⚠️ **`estimated_seconds` 不参与节奏计算。** 它是后端给的**估算区间**
 * （实测 33–48s 的中位值），不是承诺值。拿它去动态调速，会在任务比预期
 * 慢的时候把轮询拖到超时——而那恰恰是最需要看到进度的场景。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么用循环而不是 setInterval
 * ══════════════════════════════════════════════════════════════════
 * 后端一次请求要 10 秒超时，而早期间隔是 1 秒。setInterval 会在上一个
 * 请求还没回来时就发下一个，请求越堆越多（在长任务上实测过这个问题）。
 * 循环是「等上一次回来再睡」，天然串行，不会堆积。
 */

interface PollStage {
  /** 从任务开始算起的时间上限 */
  untilMs: number
  /** 该段内的轮询间隔 */
  intervalMs: number
}

/** 需求文档 2.2.4 的节奏表。改这里之前先回去读那一段。 */
const POLL_STAGES: readonly PollStage[] = [
  { untilMs: 10_000, intervalMs: 1_000 },
  { untilMs: 60_000, intervalMs: 2_000 },
  { untilMs: 120_000, intervalMs: 5_000 },
] as const

/** 到点放弃。需求文档：「120 秒：停止轮询，提示超时并提供 trace_id」 */
export const POLL_TIMEOUT_MS = 120_000

function intervalFor(elapsedMs: number): number {
  for (const stage of POLL_STAGES) {
    if (elapsedMs < stage.untilMs) return stage.intervalMs
  }
  return POLL_STAGES[POLL_STAGES.length - 1].intervalMs
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

export interface PhaseLogEntry {
  phase: string
  text: string
  /** 进入该阶段时，距离任务开始过了多少毫秒 */
  atMs: number
}

export interface TaskPolling<R> {
  /** 最新一次状态。null 表示还没拿到第一次响应。 */
  status: Ref<TaskStatusData<R> | null>
  running: DeepReadonly<Ref<boolean>>
  /** 从开始到现在的毫秒数，供界面显示"已等待 42s" */
  elapsedMs: DeepReadonly<Ref<number>>
  /** 已等待的时间是否超出了后端给的估算，用于把"预计 40 秒"换成"比预期久" */
  overEstimate: DeepReadonly<Ref<boolean>>
  error: Ref<string>
  /** 超时结束（区别于失败） */
  timedOut: DeepReadonly<Ref<boolean>>
  /** 阶段文案的变化历史，用来在界面上留下"做过什么"的痕迹 */
  phaseLog: DeepReadonly<Ref<PhaseLogEntry[]>>
  start: (taskId: string, estimatedSeconds?: number) => Promise<TaskStatusData<R> | null>
  stop: () => void
}

export function useTaskPolling<R = unknown>(): TaskPolling<R> {
  // `as Ref<...>`：泛型 T 经过 Vue 的 UnwrapRef 之后类型会漂，这是 Vue 3
  // 对"泛型 ref"的已知限制。断言在这里是安全的——ref 里存的确实就是这个类型。
  const status = ref<TaskStatusData<R> | null>(null) as Ref<TaskStatusData<R> | null>
  const running = ref(false)
  const elapsedMs = ref(0)
  const overEstimate = ref(false)
  const error = ref('')
  const timedOut = ref(false)
  const phaseLog = ref<PhaseLogEntry[]>([])

  let cancelled = false
  let abort: AbortController | null = null
  let timer: ReturnType<typeof setInterval> | null = null

  const stop = () => {
    cancelled = true
    abort?.abort()
    abort = null
    if (timer) {
      clearInterval(timer)
      timer = null
    }
    running.value = false
  }

  // 组件卸载时一定要停 —— 否则用户离开页面后轮询还在跑，
  // 后台一堆孤儿请求，而它们的结果没人看。
  onScopeDispose(stop)

  async function start(
    taskId: string,
    estimatedSeconds?: number,
  ): Promise<TaskStatusData<R> | null> {
    stop()
    cancelled = false
    timedOut.value = false
    error.value = ''
    phaseLog.value = []
    running.value = true

    const beganAt = Date.now()
    // 估算值只用来判断"是不是比预期久了"，不参与调速
    const estimateMs = estimatedSeconds ? estimatedSeconds * 1000 : 0
    if (timer) clearInterval(timer)
    timer = setInterval(() => {
      elapsedMs.value = Date.now() - beganAt
      if (estimateMs && elapsedMs.value > estimateMs) overEstimate.value = true
    }, 250)

    try {
      // 先立刻打一次：用户点完按钮马上就能看到「已接收，正在排队…」，
      // 而不是对着一个空进度条等满一个间隔。
      for (;;) {
        if (cancelled) return null

        const elapsed = Date.now() - beganAt
        if (elapsed >= POLL_TIMEOUT_MS) {
          timedOut.value = true
          return null
        }

        abort = new AbortController()
        let snap: TaskStatusData<R>
        try {
          snap = await taskStatus<R>(taskId, abort.signal)
        } catch (e) {
          if (cancelled || (e instanceof BizError && e.code === NetErrorCode.CANCELED)) {
            return null
          }
          // 单次轮询失败不终止整条链 —— 网络抖一下就放弃，用户得从头再来。
          // 但连续失败会由超时兜底，不会无限重试。
          error.value = messageOf(e)
          await sleep(intervalFor(Date.now() - beganAt))
          continue
        }
        if (cancelled) return null

        error.value = ''
        const prevPhase = status.value?.phase
        status.value = snap
        if (snap.phase !== prevPhase) {
          phaseLog.value.push({
            phase: snap.phase,
            text: snap.phase_text,
            atMs: Date.now() - beganAt,
          })
        }

        if (snap.status === 'completed' || snap.status === 'failed') return snap

        await sleep(intervalFor(Date.now() - beganAt))
        if (cancelled) return null
      }
    } finally {
      running.value = false
      if (timer) {
        clearInterval(timer)
        timer = null
      }
      elapsedMs.value = Date.now() - beganAt
    }
  }

  return {
    status,
    // 只读暴露：这几个字段由轮询驱动，让调用方误写会破坏时序
    running: readonly(running),
    elapsedMs: readonly(elapsedMs),
    overEstimate: readonly(overEstimate),
    error,
    timedOut: readonly(timedOut),
    phaseLog: readonly(phaseLog),
    start,
    stop,
  }
}
