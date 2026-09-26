<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterView, useRoute } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import DegradedNotice from '@/components/DegradedNotice.vue'
import DiffMatrix from '@/components/DiffMatrix.vue'
import PageHeader from '@/components/PageHeader.vue'
import PaidGateNotice from '@/components/PaidGateNotice.vue'
import PhaseProgress from '@/components/PhaseProgress.vue'
import TaskCancelButton from '@/components/TaskCancelButton.vue'
import { statusHeadline } from '@/api/types'
import { NAV, isNavActive } from '@/config/nav'
import { provideGenerateSession } from '@/composables/useGenerateSession'
import { useAuthStore } from '@/stores/auth'
import { useTaskStore } from '@/stores/task'
import { toast } from '@/utils/toast'

/**
 * 方案生成 —— **模块外壳**（父路由组件）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么要拆子路由（需求方 2026-09-24 明确要求）
 * ══════════════════════════════════════════════════════════════════
 * 需求方原话：「我现在的建议是在「方案生成」模块采用和上面「户型解析」
 * 模块一样的子目录形式，对功能进行细分（**不要为了做子目录而做子目录，
 * 每个目录模块要保证不空洞**）」。
 *
 * 所以拆的判据是"这一页能不能独立回答一个问题"，不是"能不能凑三项"：
 *
 *   生成参数   —— 要生成什么？（户型、风格×档位、需求、材料偏好）
 *   三方案对比 —— 三套方案各是什么、差在哪？（含选定）
 *   3D 装修漫游 —— 选定那套走进去是什么样？
 *
 * 三页各自都是重内容（参数表单 ~700px、三张方案卡 ~900px、3D 画布
 * 74vh），平铺在一页里会拉到 3000px 以上 —— 和「户型解析」当初拆分的
 * 动因完全一样（见 ParseView 的文件头）。
 *
 * ⚠️ **`3D 装修漫游` 这一页是需求方这次点名要的**：「在用户拿到 id 后
 *    进入的装修搭配推荐三选一后，可以在该模块再次生成一个包含家具的
 *    3d 界面进行移动」。见 `GenerateWalkthroughView.vue`。
 *
 * ══════════════════════════════════════════════════════════════════
 * 这一层负责的四件事，都不是"顺手放的"
 * ══════════════════════════════════════════════════════════════════
 * ① **持有生成会话。** 生成要跑 90 秒以上，轮询放在子页里的话，
 *    用户切一次子页就把它掐掉了（原因见 useGenerateSession 文件头）。
 *
 * ② **降级提示挂在这一层。** `DegradedNotice` 不折叠、不可关闭，
 *    理由是"一个能被点掉的警告，第一批就会被点掉"。放进子页的话，
 *    切一次页就等于点掉了它。
 *
 * ③ **「方案已产出、审查仍在进行」那条提示也挂在这一层。** 它解释的是
 *    "为什么方案卡上写着缺 risks" —— 用户在任何子页都该看得到，
 *    尤其在他切到 3D 漫游去的时候。
 *
 * ④ **页面头只渲染一次。** 子页再放一个会变成双标题。
 */
const route = useRoute()
const s = provideGenerateSession()
const auth = useAuthStore()
const tasks = useTaskStore()

const generateNav = NAV.find((n) => n.to === '/generate')
const isIndex = computed(() => route.path === '/generate')

/**
 * 当前所在的子页。
 *
 * ⚠️ 子项用 `isNavActive(..., exact=true)` 精确匹配：子项里「生成参数」
 * 的路径就是 `/generate`，它是 `/generate/plans` 等所有子路由的前缀 ——
 * 用 startsWith 的话在子页会同时点亮两项（详见 config/nav 文件头）。
 */
const currentChild = computed(() =>
  isIndex.value
    ? null
    : (generateNav?.children?.find((c) => isNavActive(route.path, c.to, true)) ?? null),
)

const breadcrumb = computed(() =>
  isIndex.value
    ? ['全友·智绘家', '方案中心', '多智能体方案并行对比']
    : ['全友·智绘家', '方案中心', currentChild.value?.label ?? ''],
)

const title = computed(() =>
  isIndex.value ? '多方案智能对比与价值评估' : (currentChild.value?.label ?? ''),
)

/** 付费门控（AC-01）。**只用于界面**：置灰 + 给理由。真正的拦截在后端 4005。 */
const paidBlocked = computed(() => Boolean(auth.user) && !auth.canUsePaid)

const matrixOpen = ref(false)

const headerStatus = computed(() => {
  const snap = s.status.value
  if (!snap) return { icon: 'sparkle', text: '等待生成', tone: 'wood' as const }
  return {
    icon: snap.cancelled ? 'x-circle' : 'brain',
    text: statusHeadline(snap),
    tone: (snap.cancelled ? 'wood' : 'botanical') as 'wood' | 'botanical',
  }
})

/** 用户按了「中断」：停掉轮询并立刻补拉一次状态，否则界面停在上一帧。 */
async function afterCancel() {
  s.poll.stop()
  const snap = await s.poll.refreshOnce()
  if (snap && snap.cancelled) {
    tasks.update(snap.task_id, {
      status: snap.status, phaseText: snap.phase_text, summary: '已中断',
    })
  }
}

/**
 * 「开始生成」的落点。**提示文案在会话层算出来、这里只负责说。**
 *
 * ⚠️ 分开的理由：`submit()` 要在子页之间通用（生成参数页和顶栏按钮都调它），
 * 而 "toast 说什么" 是界面的事。会话层不引 toast，测试与复用都干净。
 */
async function onGenerate() {
  await s.submit()
  // 材料偏好冲突（4001）不走 toast —— 那些问题由「生成参数」子页
  // 贴在选项旁边（用户需要一边看着标红的选项一边改，弹窗一闪而过帮不上忙）。
  if (s.filterProblems.value.length) return

  const snap = s.status.value
  if (!snap) return
  if (snap.status === 'failed') {
    toast.error(snap.error || '生成失败')
    return
  }
  if (s.poll.timedOut.value) {
    // 期限是按后端估算算出来的（见 useTaskPolling），所以这里报实际值，
    // 不写死"120 秒" —— 实测整链 122.9 秒，写死的话提示本身就是错的。
    toast.warning(`轮询超时（${s.poll.timeoutSeconds.value} 秒）。任务可能仍在后台执行。`)
    return
  }
  const n = snap.result?.plans?.length ?? 0
  const want = s.styles.value.length
  if (n && n < want) {
    // 个别分支失败是真实会发生的事，如实提示而不是报"成功"
    toast.warning(`请求 ${want} 套，实际产出 ${n} 套`)
  } else if (n) {
    toast.success(`已产出 ${n} 套方案`)
  }
}

// 地址栏的 layout 变了（从工作台或解析结果跳过来）就跟上
watch(
  () => route.query.layout,
  (v) => {
    const id = String(v || '')
    if (id && id !== s.layoutId.value) s.layoutId.value = id
  },
)
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6 surface-dots">
    <PageHeader
      :breadcrumb="breadcrumb"
      :title="title"
      :status="headerStatus"
      :code="s.layoutId.value ? `LAYOUT: ${s.layoutId.value}` : ''"
    >
      <template #actions>
        <button
          class="btn-ghost px-3 py-1.5"
          type="button"
          :disabled="!s.plans.value.length"
          @click="matrixOpen = true"
        >
          <AppIcon name="chart-bar" :size="16" class="text-botanical" />
          <span>参数对比矩阵</span>
        </button>
        <button
          class="btn-primary px-3.5 py-1.5"
          type="button"
          :disabled="!s.layoutId.value.trim() || s.submitting.value || s.poll.running.value || paidBlocked"
          @click="onGenerate"
        >
          <AppIcon :name="s.poll.running.value ? 'spinner' : 'sparkle'" :size="16" />
          <span>{{
            s.poll.running.value ? '生成中…' : s.plans.value.length ? '重新生成' : '开始生成'
          }}</span>
        </button>
      </template>
    </PageHeader>

    <!-- 会员门控（AC-01）。放在"缺户型 ID"那条之前 —— 它是更前置的一个
         问题：连"能不能做这件事"都不成立时，先答那个。 -->
    <PaidGateNotice feature="GENERATE" />

    <!--
      ── 降级提示：故意放在子路由**之上**，切页也在 ──
      见文件头 ②。它不折叠、不可关闭，这是刻意继承的约束。
    -->
    <DegradedNotice v-if="s.result.value?.degraded" :reasons="s.result.value.degrade_reasons" />

    <!--
      ── 方案已交付、风险复核仍在进行 ──
      ⚠️ 这条提示不是装饰：此时方案卡片的 `missing_artifacts` 里写着 `risks`。
      实测方案 38 秒产出、审查还要 82 秒 —— 不说的话，那 82 秒里用户
      看到的是一份"缺东西"的结果，而且切到 3D 页也仍然看不到解释。
    -->
    <div
      v-if="s.poll.running.value && s.result.value?.partial && s.plans.value.length"
      class="flex items-start gap-2.5 rounded-xl border border-botanical/30 bg-botanical/5 p-3"
    >
      <AppIcon name="info" :size="17" class="mt-0.5 shrink-0 text-botanical" />
      <p class="text-[11px] leading-relaxed text-wood">
        <span class="font-semibold">
          方案已产出（{{ s.plans.value.length }} 套），可以开始看了。
        </span>
        避坑审查仍在进行，完成后会自动补上每套方案的风险结论 ——
        <span class="text-wood-muted">
          在此之前，方案卡片上的「缺少产物」会写明缺的是哪一项。
        </span>
      </p>
    </div>

    <!-- ── 子页 ── -->
    <RouterView />

    <!--
      进度卡放在外壳层，**每个子页下面都能看到**。
      生成要跑 90 秒以上，这期间用户可能已经切到「三方案对比」去看
      （那里此刻还是空的）—— 进度必须跟着他。
    -->
    <PhaseProgress
      v-if="s.status.value"
      :text="s.status.value.phase_text"
      :progress="s.status.value.progress"
      :elapsed-ms="s.poll.elapsedMs.value"
      :eta-seconds="s.poll.etaSeconds.value"
      :overrun="s.poll.overrun.value"
      :log="s.poll.phaseLog.value"
      :status="s.status.value.status"
      :error="s.status.value.error ?? ''"
      :cancelled="s.status.value.cancelled"
    >
      <template #actions>
        <TaskCancelButton
          v-if="s.poll.running.value"
          :task-id="s.poll.activeTaskId.value"
          @settled="afterCancel"
        />
      </template>
    </PhaseProgress>

    <DiffMatrix :comparison="s.comparison.value" :open="matrixOpen" @close="matrixOpen = false" />
  </main>
</template>
