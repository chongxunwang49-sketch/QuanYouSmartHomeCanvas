import { onScopeDispose, readonly, ref, watch, type DeepReadonly, type Ref } from 'vue'

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

/**
 * 到点放弃的下限。需求文档：「120 秒：停止轮询，提示超时并提供 trace_id」。
 *
 * ⚠️ 它是**下限**，不是固定值。实测方案生成链要 122.9 秒 ——
 * 比这个下限还长，于是"轮询先放弃了、结果两秒后才准备好"。
 * 用户看到的是"超时，任务可能仍在后台执行"，而其实马上就有结果了。
 *
 * 所以真正的期限取 `max(这个下限, 后端估算 × 2)`。用后端自己的估算来定，
 * 是因为它是同一套阶段模型算出来的（`core/progress.py`）；
 * 乘 2 是留给"模型标定偏慢"的余量 —— 模型本来就会被实际速度修正，
 * 但没有义务修正到分毫不差。
 */
export const POLL_TIMEOUT_MS = 120_000

/** 超出后端估算多少倍就放弃 */
const POLL_DEADLINE_FACTOR = 2

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
  /**
   * 预计剩余秒数。`null` = 估不出来（含"已超出预期"与"还没拿到第一次响应"）。
   *
   * 它按本地时钟**逐秒递减**，不是每轮轮询才跳一次 —— 后端 2 秒才回一次，
   * 直接显示服务端那个数的话，倒计时会"卡住两秒、跳两秒"。
   */
  etaSeconds: DeepReadonly<Ref<number | null>>
  /** 已超出预期。界面改口说"比预期久"，不再报数字 */
  overrun: DeepReadonly<Ref<boolean>>
  /** 本次轮询多少秒后放弃。用于超时提示里的那句话，避免各处硬编码"120 秒" */
  timeoutSeconds: DeepReadonly<Ref<number>>
  /**
   * 正在轮询的 task_id（空串 = 没有）。
   *
   * ⚠️ 单独给一个 ref，而不是让调用方调 `savedTaskId()`：那个是**函数**，
   * 在模板里调不会跟着任务变化重新求值 —— 而「中断」按钮要拿它去发请求，
   * 拿到上一个任务的 id 就会去中断一个已经结束的任务。
   */
  activeTaskId: DeepReadonly<Ref<string>>
  error: Ref<string>
  /** 超时结束（区别于失败） */
  timedOut: DeepReadonly<Ref<boolean>>
  /** 阶段文案的变化历史，用来在界面上留下"做过什么"的痕迹 */
  phaseLog: DeepReadonly<Ref<PhaseLogEntry[]>>
  start: (taskId: string, estimatedSeconds?: number) => Promise<TaskStatusData<R> | null>
  stop: () => void
  /**
   * 补拉一次状态，把 `status` 刷新到最新。**不启动轮询。**
   *
   * 给"中断"用：`stop()` 之后界面还停在中断前那一帧（"正在分析图片…"），
   * 而用户明明刚按了中断 —— 中间最多要等一个轮询间隔才自愈。
   * 这里主动拉一次，按钮按下去界面就跟着变。
   */
  refreshOnce: () => Promise<TaskStatusData<R> | null>
  /** 上次这个页面跑过的 task_id（进程内存优先，其次 sessionStorage） */
  savedTaskId: () => string
}

export interface TaskPollingOptions {
  sessionKey?: string
}

/**
 * ══════════════════════════════════════════════════════════════════
 * 会话记忆：**离开页面再回来，任务不能丢**
 * ══════════════════════════════════════════════════════════════════
 * 这是一条实测出来的用户反馈（2026-09-23）：
 *
 *   「上传平面图解析时不能浏览其它网页，一点开其它网页再回来
 *     进度完全消失，还要重新上传。更糟的是解析完成后点开别的窗口，
 *     回来所有结果也没了。」
 *
 * 成因是两处都只在内存里：
 *   ① 轮询状态（status / phaseLog）是**组件内的 ref**，卸载即销毁
 *   ② `task_id` 只写在地址栏的 query 里 —— 而从侧栏点回「户型解析」
 *      走的是 `router.push('/parse')`，**query 没有了**
 *
 * 结果就是：任务其实一直在后端跑（结果留 1 小时），但前端把它忘了。
 *
 * 修法分两层：
 *   · 模块级 `SESSIONS`：组件卸载不销毁，路由切回来立刻能显示
 *   · `sessionStorage`：只存 task_id（几十字节），**整页刷新也能接回**
 *
 * ⚠️ 只存 id、不存结果。结果是上百 KB 的 JSON，塞进 sessionStorage
 * 有配额风险；而且后端的 `RESULT_TTL` 本来就是 1 小时 ——
 * 回来时重新拉一次状态就能拿到，没必要在前端也备一份。
 */
interface PollSession {
  taskId: string
  status: unknown
  phaseLog: PhaseLogEntry[]
  beganAt: number
  estimatedSeconds?: number
}

/** 进程内会话表。key 是任务类型（parse / generate / review）。 */
const SESSIONS = new Map<string, PollSession>()

const SS_PREFIX = 'qy.poll.'
function ssGet(key: string): string {
  try {
    return sessionStorage.getItem(SS_PREFIX + key) || ''
  } catch {
    return ''      // 隐私模式下 sessionStorage 可能不可用 —— 不能因此崩掉
  }
}
function ssSet(key: string, taskId: string) {
  try {
    sessionStorage.setItem(SS_PREFIX + key, taskId)
  } catch {
    /* 存不下就只靠内存，功能不退化到不可用 */
  }
}

export function useTaskPolling<R = unknown>(sessionKey = 'default'): TaskPolling<R> {
  // `as Ref<...>`：泛型 T 经过 Vue 的 UnwrapRef 之后类型会漂，这是 Vue 3
  // 对"泛型 ref"的已知限制。断言在这里是安全的——ref 里存的确实就是这个类型。
  const status = ref<TaskStatusData<R> | null>(null) as Ref<TaskStatusData<R> | null>
  const running = ref(false)
  const elapsedMs = ref(0)
  const etaSeconds = ref<number | null>(null)
  const overrun = ref(false)
  const error = ref('')
  const timedOut = ref(false)
  const phaseLog = ref<PhaseLogEntry[]>([])

  /**
   * ══════════════════════════════════════════════════════════════════
   * 计时锚点：本地时钟只负责"走秒"，数值以服务端为准
   * ══════════════════════════════════════════════════════════════════
   * 之前是 `elapsedMs = Date.now() - beganAt`，`beganAt` 在 `start()` 里取，
   * **每次调用都重新取一次当时的时间**。于是两个场景都会归零：
   *   · 整页刷新 —— `beganAt` 是组件内变量，重新挂载就没了
   *   · 从别的页面切回来 —— 走的还是 `start()`
   *
   * 而任务一直在后端跑。用户看到"已等待 0 秒"配一个 60% 的进度条，
   * 只会认为这个页面坏了。（这条路径实测触发过：用户反馈"
   * 一点开其它网页再回来进度完全消失"。）
   *
   * 现在改成：每轮轮询都拿服务端的 `elapsed_seconds` 重新锚一次，
   * 两次轮询之间由本地时钟平滑推进。
   */
  let anchorElapsedMs = 0
  let anchorAt = Date.now()
  /** 最近一次服务端给的剩余秒数，及其对应的本地已用时间 */
  let etaAnchor: number | null = null
  let etaAnchorElapsedMs = 0
  /** 本次轮询的放弃期限（毫秒）。见 POLL_TIMEOUT_MS 的说明 */
  let deadlineMs = POLL_TIMEOUT_MS
  const timeoutSeconds = ref(Math.round(POLL_TIMEOUT_MS / 1000))
  const activeTaskId = ref('')

  const localElapsedMs = () => anchorElapsedMs + (Date.now() - anchorAt)

  function reanchor(elapsedFromServer: number | null) {
    if (elapsedFromServer === null) return    // 服务端也不知道，就继续用本地的
    anchorElapsedMs = elapsedFromServer * 1000
    anchorAt = Date.now()
  }

  // ── 恢复上一次的会话 ──
  // 组件重新挂载时先把结果填回去，用户立刻看到上次的内容；
  // 随后 `start()` 会向后端再拉一次，拿到最新的（可能已经完成了）。
  const restored = SESSIONS.get(sessionKey)
  if (restored) {
    status.value = restored.status as TaskStatusData<R> | null
    phaseLog.value = restored.phaseLog
  }

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

  const savedTaskId = () => SESSIONS.get(sessionKey)?.taskId || ssGet(sessionKey)

  async function refreshOnce(): Promise<TaskStatusData<R> | null> {
    const id = activeTaskId.value || savedTaskId()
    if (!id) return null
    try {
      const snap = await taskStatus<R>(id)
      reanchor(snap.elapsed_seconds)
      status.value = snap
      etaAnchor = snap.overrun ? null : snap.eta_seconds
      etaAnchorElapsedMs = localElapsedMs()
      etaSeconds.value = etaAnchor
      overrun.value = snap.overrun
      return snap
    } catch {
      // 拉不到就保持原样 —— 这是"锦上添花"的一次刷新，
      // 失败不该在界面上留下任何痕迹（轮询下一次自然会同步）。
      return null
    }
  }

  async function start(
    taskId: string,
    estimatedSeconds?: number,
  ): Promise<TaskStatusData<R> | null> {
    const sameTask = SESSIONS.get(sessionKey)?.taskId === taskId
    stop()
    cancelled = false
    timedOut.value = false
    error.value = ''
    // ⚠️ **同一个任务不要清空阶段轨迹。** 从别的页面切回来会再调一次
    //    `start()`，清掉的话"系统做过什么"那条记录就没了 ——
    //    而那正是用户回来最想看到的东西。
    if (!sameTask) phaseLog.value = []
    running.value = true
    activeTaskId.value = taskId

    // ⚠️ 别在重进同一个任务时把计时重置。任务一直在后端跑，本地归零
    //    会显示成"刚开始"（见锚点说明）。新任务才重新起表。
    if (!sameTask) {
      anchorElapsedMs = 0
      anchorAt = Date.now()
      elapsedMs.value = 0
      overrun.value = false
      // 后端在创建任务时给的粗估（`estimated_seconds`）。它只用来**填第一帧**，
      // 免得用户点完按钮看到一个空的倒计时；第一次轮询回来就被真实值覆盖。
      etaAnchor = estimatedSeconds ?? null
      etaAnchorElapsedMs = 0
      etaSeconds.value = etaAnchor
    }

    // 期限同样不能因为"又点了一次 start"而重置 —— 重进同一个任务时
    // 任务已经跑了一会儿，重置等于给它续命，轮询就永远不会超时了。
    if (!sameTask) {
      deadlineMs = Math.max(
        POLL_TIMEOUT_MS,
        (estimatedSeconds ?? 0) * 1000 * POLL_DEADLINE_FACTOR,
      )
      timeoutSeconds.value = Math.round(deadlineMs / 1000)
    }

    SESSIONS.set(sessionKey, {
      taskId,
      status: status.value as unknown,
      phaseLog: phaseLog.value,
      beganAt: SESSIONS.get(sessionKey)?.beganAt ?? Date.now(),
      estimatedSeconds,
    })
    ssSet(sessionKey, taskId)

    if (timer) clearInterval(timer)
    timer = setInterval(() => {
      elapsedMs.value = localElapsedMs()
      // 倒计时跟着本地时钟走。只在两秒一次的轮询里更新的话，
      // 它会"卡住两秒、跳两秒"，看着像坏了。
      if (etaAnchor !== null) {
        const since = (elapsedMs.value - etaAnchorElapsedMs) / 1000
        etaSeconds.value = Math.max(0, etaAnchor - since)
      }
    }, 250)

    try {
      // 先立刻打一次：用户点完按钮马上就能看到「已接收，正在排队…」，
      // 而不是对着一个空进度条等满一个间隔。
      for (;;) {
        if (cancelled) return null

        if (localElapsedMs() >= deadlineMs) {
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
          await sleep(intervalFor(localElapsedMs()))
          continue
        }
        if (cancelled) return null

        error.value = ''

        // ── 先把服务端的时间锚下来 ──
        // ⚠️ 顺序有讲究：`reanchor` 必须在算 `atMs` 之前，否则阶段轨迹里的
        //    时间戳与"已用时间"会来自两个不同的钟，轨迹上出现负的间隔。
        reanchor(snap.elapsed_seconds)
        const atMs = localElapsedMs()

        const prevPhase = status.value?.phase
        status.value = snap
        etaAnchor = snap.overrun ? null : snap.eta_seconds
        etaAnchorElapsedMs = atMs
        etaSeconds.value = etaAnchor
        overrun.value = snap.overrun
        elapsedMs.value = atMs

        if (snap.phase !== prevPhase) {
          phaseLog.value.push({
            phase: snap.phase,
            text: snap.phase_text,
            atMs,
          })
        }

        if (snap.status === 'completed' || snap.status === 'failed') return snap

        await sleep(intervalFor(localElapsedMs()))
        if (cancelled) return null
      }
    } finally {
      running.value = false
      if (timer) {
        clearInterval(timer)
        timer = null
      }
      elapsedMs.value = localElapsedMs()
    }
  }

  // ── 状态一变就回写会话 ──
  // 卸载之后 `status` 这个 ref 就没了，会话表是唯一还留着它的地方。
  watch(status, (v) => {
    const cur = SESSIONS.get(sessionKey)
    if (cur) cur.status = v as unknown
  })
  watch(
    phaseLog,
    (v) => {
      const cur = SESSIONS.get(sessionKey)
      if (cur) cur.phaseLog = [...v]
    },
    { deep: true },
  )

  return {
    status,
    // 只读暴露：这几个字段由轮询驱动，让调用方误写会破坏时序
    running: readonly(running),
    elapsedMs: readonly(elapsedMs),
    etaSeconds: readonly(etaSeconds),
    overrun: readonly(overrun),
    timeoutSeconds: readonly(timeoutSeconds),
    activeTaskId: readonly(activeTaskId),
    error,
    timedOut: readonly(timedOut),
    phaseLog: readonly(phaseLog),
    start,
    stop,
    refreshOnce,
    savedTaskId,
  }
}
