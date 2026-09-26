import { computed, inject, onMounted, provide, ref, type ComputedRef, type InjectionKey, type Ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { designGenerate, materialOptions } from '@/api'
import type { GenerateResult, MaterialOptionsData, Plan } from '@/api/types'
import { BizError } from '@/api/types'
import type { TaskPolling } from '@/composables/useTaskPolling'
import { useTaskPolling } from '@/composables/useTaskPolling'
import { useTaskStore } from '@/stores/task'

/**
 * 方案生成会话 —— `/generate` 整棵子树共享的那一份状态。
 *
 * ══════════════════════════════════════════════════════════════════
 * 与 `useParseSession` 是同一个理由，不是"照着抄一个"
 * ══════════════════════════════════════════════════════════════════
 * `useTaskPolling` 里有一句 `onScopeDispose(stop)` —— 谁创建谁卸载谁停轮询。
 * 生成要跑 90 秒以上，如果在子页里创建轮询，用户从「三方案对比」切到
 * 「3D 装修漫游」就会把它掐掉：进度条停住，而任务还在后端跑。
 *
 * 所以轮询由**父路由组件** `GenerateView.vue` 持有，它在 `/generate`
 * 及其全部子路由下始终挂载。子页通过 `useGenerateSession()` 拿同一份。
 *
 * ══════════════════════════════════════════════════════════════════
 * 表单状态也放在这里，不是偷懒
 * ══════════════════════════════════════════════════════════════════
 * 风格、预算档、业主需求、材料偏好这些**用户填了一半的东西**，
 * 如果住在「生成参数」子页里，用户切到「三方案对比」看一眼再切回来，
 * 填的东西就全没了。生成要 90 秒，这期间切页是常态。
 * 放在会话里 = 切页不丢。
 *
 * ══════════════════════════════════════════════════════════════════
 * 需求方对这块模块的结构要求（2026-09-24）
 * ══════════════════════════════════════════════════════════════════
 * 「「户型解析」生成的 id，3d 漫游是后面「方案生成」的三个装修方案和
 *   含家具装修的 3d 漫游的基础；「方案生成」模块的 3d 漫游就是在
 *   「户型解析」3d 漫游的情况下放入家具和装修，而不是自己重新从 0 生成」
 *
 * 这条落在代码上就是：**家具是方案的属性，不是户型的属性**。
 * `SceneViewer` 只给 `layoutId` 时画空房子，给了 `planId` 才画家具 ——
 * 两条路径共用同一份 `/walkable` 几何，不重新建模。
 */

export interface GenerateSession {
  poll: TaskPolling<GenerateResult>
  status: TaskPolling<GenerateResult>['status']
  result: ComputedRef<GenerateResult | null>
  plans: ComputedRef<Plan[]>
  comparison: ComputedRef<GenerateResult['comparison'] | null>

  /** 户型 ID。来自地址栏 `?layout=`，也可以在子页里改。 */
  layoutId: Ref<string>
  /** 当前正在跑的任务 ID（地址栏 `?task=`）。 */
  taskId: ComputedRef<string>

  // ── 生成参数（切页不丢）──
  styles: Ref<string[]>
  grades: Ref<string[]>
  quanyouPriority: Ref<boolean>
  requirements: Ref<Record<string, unknown>>

  // ── AC-19 材料偏好 ──
  materialOpts: Ref<MaterialOptionsData | null>
  excludedCategories: Ref<string[]>
  excludedBrands: Ref<string[]>
  preferredBrands: Ref<string[]>
  /** 服务端退回的过滤冲突（4001）。文案**由后端给**，前端只展示。 */
  filterProblems: Ref<{ code: string; message: string }[]>

  submitting: Ref<boolean>
  /** 用户选定的那一套方案。**必须由用户点，不给默认值**（见 selectPlan）。 */
  selectedPlan: ComputedRef<Plan | null>
  selectedPlanId: Ref<string>

  submit: () => Promise<void>
  selectPlan: (plan: Plan) => void
  problemsOf: (e: unknown) => { code: string; message: string }[]
  goGenerate: (layoutId: string) => void
}

const KEY: InjectionKey<GenerateSession> = Symbol('qy.generate-session')

/** 选中的方案存在这里，键带上户型 —— 换一个户型就不该继承上一个的选择。 */
const SELECTED_PREFIX = 'qy.generate.selected.'

/**
 * 三套预设配对。改这里等于改"一次生成哪三套方案"。
 *
 * ⚠️ **第 3 套曾写 `luxury`，而后端 `PlanStyle` 里没有这个值** ——
 * 后果是那一套方案（高端档）落进 `_STYLE_HINTS.get(style, …)` 的兜底分支，
 * **完全没拿到风格引导**，界面上却仍标着「意式轻奢」。后端只从
 * `?layout=` 或这里拿风格，不会自己纠正，所以是静默失效。
 *
 * 现已与后端 `DEFAULT_BRANCH_PAIRS`（modern/nordic/chinese）对齐。
 * 可选的风格全集见 `@/api/types` 的 `STYLE_LABEL`（同样对齐后端枚举）。
 */
export const PAIRS = [
  { style: 'modern', grade: 'economy' },
  { style: 'nordic', grade: 'medium' },
  { style: 'chinese', grade: 'high' },
] as const

export function provideGenerateSession(): GenerateSession {
  const route = useRoute()
  const router = useRouter()
  const tasks = useTaskStore()
  const poll = useTaskPolling<GenerateResult>('generate')

  const layoutId = ref(String(route.query.layout || ''))
  const submitting = ref(false)

  const styles = ref<string[]>(PAIRS.map((p) => p.style))
  const grades = ref<string[]>(PAIRS.map((p) => p.grade))
  const quanyouPriority = ref(true)
  const requirements = ref<Record<string, unknown>>({
    family_size: 3,
    has_elderly: false,
    has_children: true,
    pets: false,
    smart_home: false,
    eco_level: 'high',
  })

  const materialOpts = ref<MaterialOptionsData | null>(null)
  const excludedCategories = ref<string[]>([])
  const excludedBrands = ref<string[]>([])
  const preferredBrands = ref<string[]>([])
  const filterProblems = ref<{ code: string; message: string }[]>([])

  const result = computed(() => poll.status.value?.result ?? null)
  const plans = computed(() => result.value?.plans ?? [])
  const comparison = computed(() => result.value?.comparison ?? null)

  const selectedPlanId = ref('')
  const selectedPlan = computed(
    () => plans.value.find((p) => p.plan_id === selectedPlanId.value) ?? null,
  )

  /**
   * 选定一套方案。**三选一的动作到这里才真的算发生。**
   *
   * 这是含家具 3D 漫游的前置：`SceneViewer` 靠 `planId` 决定画什么家具
   * （见它 props 的说明）。所以选定必须**存下来**，否则从「三方案对比」
   * 跳到「3D 装修漫游」时那一边不知道用户选了哪套。
   *
   * ⚠️ 存在 `sessionStorage` 而不是 Pinia：它是**这个户型这次会话**的选择，
   *    换一个户型就该重选。键里带 layout_id，正是这个意思。
   */
  function selectPlan(plan: Plan) {
    selectedPlanId.value = plan.plan_id
    if (layoutId.value) {
      try {
        sessionStorage.setItem(SELECTED_PREFIX + layoutId.value, plan.plan_id)
      } catch {
        /* 隐私模式下 sessionStorage 会抛 —— 存不下就只在内存里记着，
           不因为这个把「选定」这个动作弄失败 */
      }
    }
  }

  function restoreSelected() {
    const key = layoutId.value
    if (!key || selectedPlanId.value) return
    try {
      const saved = sessionStorage.getItem(SELECTED_PREFIX + key)
      if (saved && plans.value.some((p) => p.plan_id === saved)) {
        selectedPlanId.value = saved
      }
    } catch {
      /* 同上：读不到就当没选过，用户再点一次 */
    }
  }

  /**
   * 从后端 4001 的响应里取过滤问题。
   *
   * 后端把 `problems: [{code, message}]` 放在 `BizError.data` 里，
   * **文案由后端给**：前端自己组织一套说法，就会和后端的判据分叉。
   * 取不到就返回空数组，由调用方退回通用的 toast —— 不假装知道原因。
   */
  function problemsOf(e: unknown): { code: string; message: string }[] {
    if (!(e instanceof BizError)) return []
    const raw = (e.data as { problems?: unknown } | null)?.problems
    if (!Array.isArray(raw)) return []
    return raw.filter(
      (p): p is { code: string; message: string } =>
        !!p && typeof p === 'object' && typeof (p as { message?: unknown }).message === 'string',
    )
  }

  async function submit() {
    if (!layoutId.value.trim()) return
    submitting.value = true
    filterProblems.value = []
    try {
      const created = await designGenerate({
        layout_id: layoutId.value.trim(),
        styles: styles.value,
        budget_grades: grades.value,
        quanyou_priority: quanyouPriority.value,
        requirements: requirements.value,
        excluded_categories: excludedCategories.value,
        excluded_brands: excludedBrands.value,
        preferred_brands: preferredBrands.value,
      })
      tasks.track({ taskId: created.task_id, kind: 'generate' })
      router.replace({ query: { ...route.query, task: created.task_id } })

      const snap = await poll.start(created.task_id, created.estimated_seconds)
      if (!snap) return

      const n = snap.result?.plans?.length ?? 0
      tasks.update(created.task_id, {
        status: snap.status,
        phaseText: snap.phase_text,
        degraded: snap.degraded,
        summary: n ? `产出 ${n} 套方案` : snap.error || '',
      })
      // 新的一批方案出来了，上一批的选定不再适用
      selectedPlanId.value = ''
      return
    } finally {
      submitting.value = false
    }
  }

  function goGenerate(id: string) {
    layoutId.value = id
    router.push({ path: '/generate', query: id ? { layout: id } : {} })
  }

  // 方案一到就恢复上次的选定（刷新页面、或从别的子页切回来）
  onMounted(async () => {
    try {
      materialOpts.value = await materialOptions()
    } catch {
      // 可选项拿不到不算失败：偏好面板整体不显示即可，生成本身不依赖它。
      // 报一个 toast 会让人以为生成坏了。
      materialOpts.value = null
    }

    const taskId = String(route.query.task || '') || poll.savedTaskId()
    if (!taskId) return
    if (String(route.query.task || '') !== taskId) {
      router.replace({ query: { ...route.query, task: taskId } })
    }
    const snap = await poll.start(taskId)
    if (snap?.result) {
      tasks.track({
        taskId,
        kind: 'generate',
        summary: `产出 ${snap.result.plans?.length ?? 0} 套方案`,
      })
      tasks.update(taskId, { status: snap.status, phaseText: snap.phase_text })
      if (!layoutId.value) layoutId.value = String(snap.result.layout_id ?? '')
      restoreSelected()
    }
  })

  const session: GenerateSession = {
    poll,
    status: poll.status,
    result,
    plans,
    comparison,
    layoutId,
    taskId: computed(() => String(route.query.task || '')),
    styles,
    grades,
    quanyouPriority,
    requirements,
    materialOpts,
    excludedCategories,
    excludedBrands,
    preferredBrands,
    filterProblems,
    submitting,
    selectedPlan,
    selectedPlanId,
    submit,
    selectPlan,
    problemsOf,
    goGenerate,
  }

  provide(KEY, session)
  return session
}

/**
 * 在 `/generate` 的任意子页里调用，拿到父组件提供的那一份。
 *
 * 拿不到就直接抛 —— 这是"组件挂错了位置"的编码错误，
 * 静默返回一个空对象只会让症状延后到某个 computed 里再炸。
 */
export function useGenerateSession(): GenerateSession {
  const session = inject(KEY)
  if (!session) {
    throw new Error(
      'useGenerateSession() 必须在 /generate 的子路由组件里调用 —— ' +
        '会话由 GenerateView.vue 提供（见 useGenerateSession.ts 文件头）。',
    )
  }
  return session
}
