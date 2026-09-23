<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import DegradedNotice from '@/components/DegradedNotice.vue'
import DiffMatrix from '@/components/DiffMatrix.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import PhaseProgress from '@/components/PhaseProgress.vue'
import PlanCard from '@/components/PlanCard.vue'
import PlanDrawer from '@/components/PlanDrawer.vue'
import { designGenerate } from '@/api'
import { imageAt } from '@/assets/images/pool'
import { toast } from '@/utils/toast'
import { messageOf } from '@/api/client'
import type { GenerateResult, Plan } from '@/api/types'
import { GRADE_LABEL, STYLE_LABEL, label } from '@/api/types'
import { useTaskPolling } from '@/composables/useTaskPolling'
import { useTaskStore } from '@/stores/task'

/**
 * 方案生成与横向对比。
 *
 * 三种风格 × 三个预算档，**按位置配对**（styles[0] 配 budget_grades[0]），
 * 产出数量取两者较短的。这个配对规则是后端的契约，前端不重算。
 */
const route = useRoute()
const router = useRouter()
const tasks = useTaskStore()
const poll = useTaskPolling<GenerateResult>()

const layoutId = ref(String(route.query.layout || ''))
const submitting = ref(false)

/** 三套预设配对。改这里等于改"一次生成哪三套方案"。 */
const PAIRS = [
  { style: 'modern', grade: 'economy' },
  { style: 'nordic', grade: 'medium' },
  { style: 'luxury', grade: 'high' },
] as const

const styles = ref<string[]>(PAIRS.map((p) => p.style))
const grades = ref<string[]>(PAIRS.map((p) => p.grade))

const quanyouPriority = ref(true)
const requirements = ref({
  family_size: 3,
  has_elderly: false,
  has_children: true,
  pets: false,
  smart_home: false,
  eco_level: 'high',
})

const result = computed(() => poll.status.value?.result ?? null)
const plans = computed(() => result.value?.plans ?? [])
const comparison = computed(() => result.value?.comparison ?? null)

/** 推荐位：中间那套（中档）。**这是产品位，不是系统推荐的"最优"** ——
    系统明确不给方案排序，这里高亮的是"中间档"这个展示逻辑。 */
const FEATURED_INDEX = 1

const drawerOpen = ref(false)
const matrixOpen = ref(false)
const activePlan = ref<Plan | null>(null)

/** 按方案序号稳定取图 —— 同一套方案每次刷新看到的是同一张 */
const imageFor = imageAt

async function submit() {
  if (!layoutId.value.trim()) {
    toast.warning('请先填写或从「户型解析」带过来 layout_id')
    return
  }
  submitting.value = true
  try {
    const created = await designGenerate({
      layout_id: layoutId.value.trim(),
      styles: styles.value,
      budget_grades: grades.value,
      quanyou_priority: quanyouPriority.value,
      requirements: requirements.value,
    })
    tasks.track({ taskId: created.task_id, kind: 'generate' })
    router.replace({ query: { ...route.query, task: created.task_id } })

    const snap = await poll.start(created.task_id, created.estimated_seconds)
    if (poll.timedOut.value) {
      toast.warning('轮询超时（120 秒）。任务可能仍在后台执行。')
      return
    }
    if (!snap) return

    const n = snap.result?.plans?.length ?? 0
    tasks.update(created.task_id, {
      status: snap.status,
      phaseText: snap.phase_text,
      degraded: snap.degraded,
      summary: n ? `产出 ${n} 套方案` : snap.error || '',
    })

    if (snap.status === 'failed') toast.error(snap.error || '生成失败')
    else if (n < (created.plan_count ?? 3)) {
      // 个别分支失败是真实会发生的事，如实提示而不是报"成功"
      toast.warning(`请求 ${created.plan_count ?? 3} 套，实际产出 ${n} 套`)
    } else toast.success(`已产出 ${n} 套方案`)
  } catch (e) {
    toast.error(messageOf(e))
  } finally {
    submitting.value = false
  }
}

function openDetail(plan: Plan) {
  activePlan.value = plan
  drawerOpen.value = true
}

function selectPlan(plan: Plan) {
  toast.success(
    `已选定「${label(STYLE_LABEL, plan.style)} · ${label(GRADE_LABEL, plan.budget_grade)}」。` +
      '（演示环境：未真的落库，选定动作到此为止。）',
  )
}

onMounted(async () => {
  const taskId = String(route.query.task || '')
  if (!taskId) return
  const snap = await poll.start(taskId)
  if (snap?.result) {
    tasks.track({
      taskId,
      kind: 'generate',
      summary: `产出 ${snap.result.plans?.length ?? 0} 套方案`,
    })
    tasks.update(taskId, { status: snap.status, phaseText: snap.phase_text })
  }
})
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6">
    <PageHeader
      :breadcrumb="['全友·智绘家', '方案中心', '多智能体方案并行对比']"
      title="多方案智能对比与价值评估"
      :status="
        poll.status.value
          ? { icon: 'brain', text: poll.status.value.phase_text }
          : { icon: 'sparkle', text: '等待生成', tone: 'wood' }
      "
      :code="layoutId ? `LAYOUT: ${layoutId}` : ''"
    >
      <template #actions>
        <button
          class="btn-ghost px-3 py-1.5"
          type="button"
          :disabled="!plans.length"
          @click="matrixOpen = true"
        >
          <AppIcon name="chart-bar" :size="16" class="text-botanical" />
          <span>参数对比矩阵</span>
        </button>
        <button
          class="btn-primary px-3.5 py-1.5"
          type="button"
          :disabled="!layoutId.trim() || submitting || poll.running.value"
          @click="submit"
        >
          <AppIcon :name="poll.running.value ? 'spinner' : 'sparkle'" :size="16" />
          <span>{{ poll.running.value ? '生成中…' : plans.length ? '重新生成' : '开始生成' }}</span>
        </button>
      </template>
    </PageHeader>

    <!--
      按钮置灰必须说明原因。
      「开始生成」在没填 layout_id 时是 disabled —— 如果不解释，
      用户点一下发现没反应，只会认为界面坏了。
    -->
    <div
      v-if="!layoutId.trim() && !plans.length"
      class="flex items-start gap-2.5 rounded-xl border border-accent-gold/40 bg-wood-light/50 p-3"
    >
      <AppIcon name="info" :size="17" class="mt-0.5 shrink-0 text-accent-gold" />
      <p class="text-[11px] leading-relaxed text-wood">
        <span class="font-semibold">「开始生成」当前不可点，因为还没有户型 ID。</span>
        生成方案要用一份**已解析的户型**——先到「户型解析」上传一张户型图，
        解析完成后点「生成装修方案」会自动带着 ID 跳过来。
      </p>
    </div>

    <!-- ══ 生成参数 ══ -->
    <section v-if="!plans.length" class="card p-4">
      <h2 class="mb-3 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
        <AppIcon name="gear" :size="16" class="text-botanical" />
        <span>生成参数</span>
      </h2>

      <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div class="flex flex-col gap-3">
          <div>
            <label class="mb-1.5 block text-[12px] font-semibold text-wood-dark">
              户型 ID
              <span class="ml-1 font-normal text-wood-muted">（来自「户型解析」）</span>
            </label>
            <div class="flex gap-2">
              <input
                v-model="layoutId"
                class="field px-3 py-2 font-mono"
                placeholder="layout_20260923_xxxxxx"
              />
              <button class="btn-ghost shrink-0 px-3 py-2" type="button" @click="router.push('/parse')">
                <AppIcon name="blueprint" :size="15" class="text-botanical" />
                <span>去解析</span>
              </button>
            </div>
          </div>

          <div>
            <p class="mb-1.5 text-[12px] font-semibold text-wood-dark">
              风格 × 预算档（按位置配对）
            </p>
            <div class="flex flex-col gap-2">
              <div
                v-for="(p, i) in PAIRS"
                :key="p.style"
                class="flex items-center gap-2 rounded-xl border border-warm-border bg-white p-2.5"
              >
                <span class="font-mono text-[11px] text-wood-muted">
                  {{ String(i + 1).padStart(2, '0') }}
                </span>
                <select v-model="styles[i]" class="field flex-1 px-2.5 py-1.5">
                  <option v-for="(v, k) in STYLE_LABEL" :key="k" :value="k">{{ v }}</option>
                </select>
                <AppIcon name="arrow-right" :size="14" class="shrink-0 text-wood-muted" />
                <select v-model="grades[i]" class="field flex-1 px-2.5 py-1.5">
                  <option v-for="(v, k) in GRADE_LABEL" :key="k" :value="k">{{ v }}</option>
                </select>
              </div>
            </div>
          </div>
        </div>

        <div class="flex flex-col gap-3">
          <div>
            <p class="mb-1.5 text-[12px] font-semibold text-wood-dark">业主需求</p>
            <div class="grid grid-cols-2 gap-2">
              <label class="flex items-center gap-2 rounded-xl border border-warm-border bg-white px-3 py-2">
                <span class="text-[12px] text-wood-muted">常住人数</span>
                <input
                  v-model.number="requirements.family_size"
                  class="field w-14 px-2 py-1 text-center"
                  type="number"
                  min="1"
                  max="10"
                />
              </label>
              <label
                v-for="opt in [
                  { k: 'has_children', label: '有儿童' },
                  { k: 'has_elderly', label: '有老人' },
                  { k: 'pets', label: '养宠物' },
                  { k: 'smart_home', label: '要智能家居' },
                ]"
                :key="opt.k"
                class="flex cursor-pointer items-center gap-2 rounded-xl border border-warm-border bg-white px-3 py-2"
              >
                <input
                  v-model="(requirements as Record<string, unknown>)[opt.k]"
                  class="accent-[#4A7C59]"
                  type="checkbox"
                />
                <span class="text-[12px] text-wood-dark">{{ opt.label }}</span>
              </label>
            </div>
          </div>

          <label
            class="flex cursor-pointer items-start gap-3 rounded-xl border border-warm-border bg-white p-3"
          >
            <input v-model="quanyouPriority" class="mt-0.5 accent-[#4A7C59]" type="checkbox" />
            <span>
              <span class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                <AppIcon name="leaf" :size="14" class="text-botanical" />
                <span>优先推荐全友自有产品</span>
              </span>
              <span class="mt-0.5 block text-[11px] leading-relaxed text-wood-muted">
                开启后选材会优先命中全友自有商品，覆盖率目标 ≥60%（AC-18）。
                未达标时后端会自动做同价位替代，替代记录可见于材料明细。
              </span>
            </span>
          </label>

          <div class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3">
            <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
              <AppIcon name="info" :size="14" class="text-wood-muted" />
              <span>关于耗时</span>
            </p>
            <p class="mt-1 text-[11px] leading-relaxed text-wood-muted">
              生成会并行跑 3 套方案 × 3 个 Agent，加一次汇聚审查，共 10 个产出任务。
              实测约 95 秒。期间可以切到别的页面，任务在后台继续。
            </p>
          </div>
        </div>
      </div>
    </section>

    <PhaseProgress
      v-if="poll.status.value"
      :text="poll.status.value.phase_text"
      :progress="poll.status.value.progress"
      :elapsed-ms="poll.elapsedMs.value"
      :over-estimate="poll.overEstimate.value"
      :log="poll.phaseLog.value"
      :status="poll.status.value.status"
    />

    <!-- ══ 空状态 ══ -->
    <div v-if="!plans.length && !poll.running.value" class="card">
      <EmptyState
        art="设计与户型-design-components_c2hs"
        title="还没有生成方案"
        description="填入一个已解析的户型 ID，选好风格与预算档，系统会并行产出空间规划、造价明细与材料选型，再做一次汇聚审查。"
      >
        <button class="btn-ghost px-4 py-2" type="button" @click="router.push('/parse')">
          <AppIcon name="blueprint" :size="16" class="text-botanical" />
          <span>先去解析户型图</span>
        </button>
      </EmptyState>
    </div>

    <!-- ══ 降级提示 ══ -->
    <DegradedNotice
      v-if="result?.degraded"
      :reasons="result.degrade_reasons"
    />

    <!-- ══ 方案卡 ══ -->
    <section v-if="plans.length" class="grid grid-cols-1 items-stretch gap-5 lg:grid-cols-3">
      <PlanCard
        v-for="(p, i) in plans"
        :key="p.plan_id"
        :plan="p"
        :featured="i === FEATURED_INDEX && plans.length >= 3"
        :image="imageFor(i)"
        :image-index="i"
        @detail="openDetail(p)"
        @select="selectPlan(p)"
      />
    </section>

    <!-- ══ 汇总说明 ══ -->
    <section
      v-if="plans.length && comparison?.notes?.length"
      class="card p-4"
    >
      <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
        <AppIcon name="info" :size="14" class="text-wood-muted" />
        <span>汇总说明</span>
      </p>
      <ul class="mt-1.5 space-y-1">
        <li
          v-for="(n, i) in comparison.notes"
          :key="i"
          class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
        >
          <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent-gold" />
          <span>{{ n }}</span>
        </li>
      </ul>
    </section>

    <!-- ══ 错误 ══ -->
    <section
      v-if="result?.errors?.length"
      class="rounded-xl border border-accent-red/30 bg-accent-red/5 p-3.5"
    >
      <p class="flex items-center gap-1.5 text-[12px] font-bold text-wood-dark">
        <AppIcon name="bug" :size="14" class="text-accent-red" />
        <span>执行中的错误（{{ result.errors.length }}）</span>
      </p>
      <ul class="mt-1.5 space-y-1">
        <li
          v-for="(e, i) in result.errors"
          :key="i"
          class="break-words font-mono text-[11px] leading-relaxed text-wood"
        >
          [{{ e.agent || '—' }}] {{ e.message }}
        </li>
      </ul>
    </section>

    <PlanDrawer :plan="activePlan" :open="drawerOpen" @close="drawerOpen = false" />
    <DiffMatrix :comparison="comparison" :open="matrixOpen" @close="matrixOpen = false" />
  </main>
</template>
