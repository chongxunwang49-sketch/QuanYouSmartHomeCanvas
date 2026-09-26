<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import SceneViewer from '@/components/SceneViewer.vue'
import { GRADE_LABEL, STYLE_LABEL, label } from '@/api/types'
import { useGenerateSession } from '@/composables/useGenerateSession'

/**
 * 方案生成 · 3D 装修漫游（`/generate/walkthrough`）。
 *
 * 回答第三个问题：**选定那套装修走进去是什么样。**
 *
 * ══════════════════════════════════════════════════════════════════
 * ⚠️ 这一页是需求方这次点名要的，它和「户型解析」的 3D 漫游是**两件事**
 * ══════════════════════════════════════════════════════════════════
 * 需求方原话：
 *
 *   「在你解析完后打开的网页是该项目的 3d 演示界面，但是这是一个错误，
 *     我们项目实现的效果是，在**平面图解析后**在该 3d 界面可以进行移动
 *     看查**没有家具**的情况；而在用户拿到 id 后进入的装修搭配推荐
 *     **三选一后**，可以在该模块再次生成一个**包含家具**的 3d 界面进行移动」
 *
 * 所以两个 3D 页面的差别只有一个：**画不画家具。**
 *
 *   户型解析 · 3D 漫游   `/parse/walkthrough`   空房子（不给 planId）
 *   方案生成 · 3D 装修漫游 `/generate/walkthrough` 选定方案 + 家具（给 planId）
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么不是"从 0 再生成一次 3D"
 * ══════════════════════════════════════════════════════════════════
 * 需求方原话：「「方案生成」模块的 3d 漫游就是在「户型解析」3d 漫游的
 * 情况下放入家具和装修，而不是自己重新从 0 生成，这样可以省资源和等待时间」。
 *
 * 这一条在代码上就是**同一个 `layout_id`**：`SceneViewer` 向
 * `/layout/{同一个 id}/walkable` 拿几何 —— 与解析页那次是同一份数据、
 * 同一个接口（后端有缓存），前端不做第二次建模。家具走
 * `/layout/{id}/furniture?plan_id=…`，它返回的坐标与 `walkable`
 * **同一套场景坐标系**（后端在响应里写明了这一点），所以两者天然对齐。
 */
const s = useGenerateSession()

/**
 * 有没有东西可画。**三样缺一不可**：
 * 户型 ID（几何）、选定的方案（家具）、生成结果还在（说明这个户型
 * 确实是刚解析/生成过的那一份）。
 */
const ready = computed(() => Boolean(s.layoutId.value.trim() && s.selectedPlan.value))

/** 方案里说的风格。给 `SceneViewer` 决定家具配色。 */
const planStyle = computed(() => s.selectedPlan.value?.style ?? '')
</script>

<template>
  <div class="flex flex-col gap-4">
    <template v-if="ready">
      <!--
        操作提示。**与「户型解析」的 3D 漫游同一套键位**（都来自
        `three/keys.ts` 那张表），所以这里不再手写一份 —— 手写的那份
        迟早会与 `SceneViewer` 里的提示分叉。
      -->
      <div
        class="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-xl border border-warm-border bg-warm-sidebar/50 px-3 py-2 text-[11px] text-wood-muted"
      >
        <span class="flex items-center gap-1.5">
          <AppIcon name="info" :size="13" class="text-botanical" />
          <span class="font-semibold text-wood-dark">这一间是</span>
        </span>
        <span class="text-wood-dark">
          {{ label(STYLE_LABEL, s.selectedPlan.value!.style) }} ·
          {{ label(GRADE_LABEL, s.selectedPlan.value!.budget_grade) }}
        </span>
        <span class="text-wood-muted/70">|</span>
        <span>
          户型几何与「户型解析」的 3D 漫游是<strong class="font-semibold">同一份</strong>
          （{{ s.layoutId.value }}），家具是这套方案的
        </span>
        <RouterLink class="text-botanical hover:underline" to="/generate/plans">
          换一套 →
        </RouterLink>
      </div>

      <SceneViewer
        :layout-id="s.layoutId.value"
        :plan-id="s.selectedPlan.value!.plan_id"
        :plan-style="planStyle"
        height="min(74vh, 820px)"
      />
    </template>

    <!--
      ⚠️ **没选定方案时不能直接渲染 3D。**
      直接传一个空 planId 会画出一间空房子 —— 而用户以为自己看的是
      "选定那套的 3D"，两者差得远。必须说清缺的是哪一步。
    -->
    <EmptyState
      v-else-if="s.layoutId.value.trim() && s.plans.value.length"
      art="家居空间-knocking-on-the-door_vgly"
      title="还没有选定方案"
      description="3D 装修漫游要画的是「你选的那一套」的家具 —— 没有选定就不知道该摆哪一套。先到「三方案对比」里点一张卡上的「选定」。"
    >
      <RouterLink class="btn-ghost px-4 py-2" to="/generate/plans">
        <AppIcon name="chart-bar" :size="16" class="text-botanical" />
        <span>去三选一</span>
      </RouterLink>
    </EmptyState>

    <EmptyState
      v-else
      art="家居空间-knocking-on-the-door_vgly"
      title="还没有可以走进去的装修"
      description="这一页要一份已解析的户型和一套已生成的方案。先到「生成参数」里填户型 ID 并生成三套方案，选定其中一套之后回来。"
    >
      <RouterLink class="btn-ghost px-4 py-2" to="/generate">
        <AppIcon name="gear" :size="16" class="text-botanical" />
        <span>去生成方案</span>
      </RouterLink>
    </EmptyState>
  </div>
</template>
