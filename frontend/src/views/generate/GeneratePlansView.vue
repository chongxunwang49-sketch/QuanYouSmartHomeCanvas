<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import PlanCard from '@/components/PlanCard.vue'
import PlanDrawer from '@/components/PlanDrawer.vue'
import { imageAt } from '@/assets/images/pool'
import type { Plan } from '@/api/types'
import { GRADE_LABEL, STYLE_LABEL, label, statusHeadline } from '@/api/types'
import { useGenerateSession } from '@/composables/useGenerateSession'
import { toast } from '@/utils/toast'

/**
 * 方案生成 · 三方案对比与三选一（`/generate/plans`）。
 *
 * 回答第二个问题：**三套方案各是什么、差在哪、我要哪一套。**
 *
 * ══════════════════════════════════════════════════════════════════
 * ⚠️「选定」不再是一句 toast —— 它是含家具 3D 漫游的前置
 * ══════════════════════════════════════════════════════════════════
 * 初版 `selectPlan` 只弹一句"已选定（演示环境未落库）"，选定这件事
 * 什么也没发生。需求方这次把流程说清楚了：
 *
 *   「在用户拿到 id 后进入的装修搭配推荐**三选一**后，可以在该模块
 *     再次生成一个包含家具的 3d 界面进行移动」
 *
 * 也就是"三选一"是一个**真实的步骤**，它决定了后面那间房子里摆哪套
 * 家具。所以选定必须记下来（存进会话 + sessionStorage），
 * `3D 装修漫游` 那一页才有东西可画。
 *
 * 选定的动作在会话里（`selectPlan`），不在这里 —— 因为
 * `3D 装修漫游` 页也要能显示用户选的是哪一套。
 */
const s = useGenerateSession()

/** 推荐位：中间那套（中档）。**这是产品位，不是系统推荐的"最优"** ——
    系统明确不给方案排序，这里高亮的是"中间档"这个展示逻辑。 */
const FEATURED_INDEX = 1

const drawerOpen = ref(false)
const activePlan = ref<Plan | null>(null)

/** 按方案序号稳定取图 —— 同一套方案每次刷新看到的是同一张 */
const imageFor = imageAt

function choose(plan: Plan) {
  s.selectPlan(plan)
  toast.success(
    `已选定「${label(STYLE_LABEL, plan.style)} · ${label(GRADE_LABEL, plan.budget_grade)}」。` +
      '去「3D 装修漫游」可以看到这套家具摆进房子的样子。',
  )
}

function openDetail(plan: Plan) {
  activePlan.value = plan
  drawerOpen.value = true
}
</script>

<template>
  <div class="flex flex-col gap-4">
    <!-- ══ 空状态 ══ -->
    <div v-if="!s.plans.value.length" class="card">
      <EmptyState
        v-if="s.poll.running.value"
        art="设计与户型-design-components_c2hs"
        title="三套方案正在并行生成"
        description="每套方案都要排出空间规划、算出造价明细、选定材料，最后再统一复核一遍。约需 95 秒 —— 这期间可以切到别的子页，生成不会中断。"
      >
        <p class="text-[11px] text-wood-muted">
          当前阶段：{{ statusHeadline(s.status.value!) }}
        </p>
      </EmptyState>
      <EmptyState
        v-else
        art="设计与户型-design-components_c2hs"
        title="还没有可对比的方案"
        description="这一页要拿至少一套方案才能对比。先到「生成参数」里选好户型与风格档位，再按页面右上角的「开始生成」。"
      >
        <RouterLink class="btn-ghost px-4 py-2" to="/generate">
          <AppIcon name="gear" :size="16" class="text-botanical" />
          <span>去填生成参数</span>
        </RouterLink>
      </EmptyState>
    </div>

    <!-- ══ 方案卡 ══ -->
    <section v-if="s.plans.value.length" class="grid grid-cols-1 items-stretch gap-5 lg:grid-cols-3">
      <PlanCard
        v-for="(p, i) in s.plans.value"
        :key="p.plan_id"
        :plan="p"
        :featured="i === FEATURED_INDEX && s.plans.value.length >= 3"
        :selected="p.plan_id === s.selectedPlanId.value"
        :image="imageFor(i)"
        :image-index="i"
        @detail="openDetail(p)"
        @select="choose(p)"
      />
    </section>

    <!-- ══ 选定之后的下一步 ══ -->
    <section
      v-if="s.plans.value.length"
      class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warm-border bg-warm-sidebar/50 p-3.5"
    >
      <div class="min-w-0">
        <p class="text-[12px] font-semibold text-wood-dark">
          <template v-if="s.selectedPlan.value">
            已选定「{{ label(STYLE_LABEL, s.selectedPlan.value.style) }} ·
            {{ label(GRADE_LABEL, s.selectedPlan.value.budget_grade) }}」
          </template>
          <template v-else>还没有选定方案</template>
        </p>
        <p class="mt-0.5 text-[11px] leading-relaxed text-wood-muted">
          <template v-if="s.selectedPlan.value">
            3D 装修漫游会在这份户型上放入<strong class="font-semibold">这一套</strong>方案的家具 ——
            房子与「户型解析」里的 3D 漫游是同一套，看到的就是这个户型。
          </template>
          <template v-else>
            在卡片上点「选这套」。选定之后才能进 3D 装修漫游 ——
            房子画哪一套家具，取决于这里的这个选择。
          </template>
        </p>
      </div>
      <RouterLink
        v-if="s.selectedPlan.value"
        class="btn-primary shrink-0 px-3.5 py-2"
        to="/generate/walkthrough"
      >
        <AppIcon name="cube" :size="16" />
        <span>走进这套装修</span>
      </RouterLink>
    </section>

    <!-- ══ 汇总说明 ══ -->
    <section v-if="s.plans.value.length && s.comparison.value?.notes?.length" class="card p-4">
      <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
        <AppIcon name="info" :size="14" class="text-wood-muted" />
        <span>汇总说明</span>
      </p>
      <ul class="mt-1.5 space-y-1">
        <li
          v-for="(n, i) in s.comparison.value.notes"
          :key="i"
          class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
        >
          <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent-gold" />
          <span>{{ n }}</span>
        </li>
      </ul>
    </section>

    <!--
      ══ 未能完成的环节 ══

      ⚠️ 原来这里叫「执行中的错误」，逐条打印 `[A-04] LLM 调用失败: [deepseek] HTTP 500: …`
         —— 内部环节代号加异常原文，是给开发者看的，不该出现在业主面前的界面上。

      但**这块不能整块删掉**：生成确实可能缺东西，"什么都不显示"就是静默失败，
      而本项目的第一条纪律是不许静默降级。所以留着，只把话说成人话：
      去掉环节代号，去掉等宽字体（那是"原始日志"的视觉暗示），
      句子本身由后端给（后端已改成用户向文案）。
    -->
    <section
      v-if="s.result.value?.errors?.length"
      class="rounded-xl border border-accent-red/30 bg-accent-red/5 p-3.5"
    >
      <p class="flex items-center gap-1.5 text-[12px] font-bold text-wood-dark">
        <AppIcon name="warning-circle" :size="14" class="text-accent-red" />
        <span>有 {{ s.result.value.errors.length }} 个环节没能完成</span>
      </p>
      <ul class="mt-1.5 space-y-1">
        <li
          v-for="(e, i) in s.result.value.errors"
          :key="i"
          class="break-words text-[11px] leading-relaxed text-wood"
        >
          {{ e.message }}
        </li>
      </ul>
      <p class="mt-1.5 text-[11px] leading-relaxed text-wood-muted">
        缺的内容会在对应方案上标注出来。稍后重试通常能补上。
      </p>
    </section>

    <PlanDrawer :plan="activePlan" :open="drawerOpen" @close="drawerOpen = false" />
  </div>
</template>
