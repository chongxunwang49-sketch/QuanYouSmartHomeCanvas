<script setup lang="ts">
import { RouterLink } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import { useParseSession } from '@/composables/useParseSession'
import type { DiagnosisItem } from '@/api/types'

/**
 * 户型解析 · 户型诊断（`/parse/diagnosis`）。
 *
 * 回答第四个问题：**这套户型好不好。**
 *
 * 含三块：五维评分（采光/通风/动线/空间利用率/环保）、诊断综述与
 * 承重结构提示、执行轨迹（工程细节，原来挤在首屏，现在归到这一页底部）。
 *
 * ⚠️ 「数据不足」是合法结论，**不能渲染成 0 分**（见模板里的分支）。
 *    这是需求文档明确要求的诚实原则：识别不出就说识别不出。
 */
const s = useParseSession()

const DIMS: { key: keyof NonNullable<typeof s.diagnosis.value>; label: string; icon: string }[] = [
  { key: 'lighting', label: '采光', icon: 'sparkle' },
  { key: 'ventilation', label: '通风', icon: 'leaf' },
  { key: 'circulation', label: '动线', icon: 'arrow-right' },
  { key: 'space_utilization', label: '空间利用率', icon: 'layout' },
  { key: 'green_score', label: '环保', icon: 'plant' },
]

function dimOf(key: string): DiagnosisItem | null {
  const d = s.diagnosis.value as unknown as Record<string, DiagnosisItem> | null
  return d?.[key] ?? null
}
</script>

<template>
  <div class="flex flex-col gap-4">
    <EmptyState
      v-if="!s.result.value"
      art="AI与数据-data-segmentation_3n3n"
      title="还没有可诊断的户型"
      description="先在「上传解析」里交一张户型图。这一页会按采光、通风、动线、空间利用率、环保五个维度打分，并给出可执行的改进建议。"
    >
      <RouterLink class="btn-ghost px-4 py-2" to="/parse">
        <AppIcon name="upload-simple" :size="16" class="text-botanical" />
        <span>去上传户型图</span>
      </RouterLink>
    </EmptyState>

    <template v-else-if="s.diagnosis.value">
      <div class="card p-4">
        <div class="mb-3 flex items-center justify-between">
          <h2 class="flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
            <AppIcon name="chart-bar" :size="17" class="text-botanical" />
            <span>户型诊断</span>
          </h2>
          <div class="flex items-center gap-2">
            <span class="text-[11px] text-wood-muted">综合</span>
            <span
              class="num text-[18px] font-bold"
              :class="s.scoreTone(s.diagnosis.value.overall_score)"
            >
              {{ s.diagnosis.value.overall_score.toFixed(1) }}
            </span>
          </div>
        </div>

        <div class="flex flex-col gap-2">
          <div
            v-for="d in DIMS"
            :key="d.key"
            class="rounded-xl border border-warm-border bg-white p-3"
          >
            <div class="flex items-center justify-between">
              <span class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                <AppIcon :name="d.icon" :size="14" class="text-botanical" />
                <span>{{ d.label }}</span>
              </span>
              <!-- "数据不足"是合法结论，不能渲染成 0 分 -->
              <span
                v-if="dimOf(d.key)?.insufficient_data"
                class="rounded border border-warm-border bg-warm-sidebar px-2 py-0.5 text-[10px] font-semibold text-wood-muted"
              >
                数据不足
              </span>
              <span
                v-else
                class="num text-[14px] font-bold"
                :class="s.scoreTone(dimOf(d.key)?.score ?? 0)"
              >
                {{ (dimOf(d.key)?.score ?? 0).toFixed(1) }}
              </span>
            </div>

            <div class="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-warm-border">
              <div
                class="h-full rounded-full bg-botanical transition-[width] duration-500"
                :style="{ width: `${dimOf(d.key)?.insufficient_data ? 0 : (dimOf(d.key)?.score ?? 0)}%` }"
              />
            </div>

            <ul v-if="dimOf(d.key)?.issues?.length" class="mt-2 flex flex-col gap-0.5">
              <li
                v-for="(it, i) in dimOf(d.key)?.issues"
                :key="i"
                class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
              >
                <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-wood-muted/50" />
                <span>{{ it }}</span>
              </li>
            </ul>
          </div>
        </div>

        <p
          v-if="s.diagnosis.value.summary"
          class="mt-3 rounded-xl border border-warm-border bg-warm-sidebar/50 p-3 text-[12px] leading-relaxed text-wood"
        >
          {{ s.diagnosis.value.summary }}
        </p>

        <!-- 承重墙警告：这条不能折叠 -->
        <div
          v-if="s.diagnosis.value.load_bearing_warning?.length"
          class="mt-3 rounded-xl border border-accent-red/30 bg-accent-red/5 p-3"
        >
          <p class="flex items-center gap-1.5 text-[12px] font-bold text-wood-dark">
            <AppIcon name="warning" :size="14" class="text-accent-red" />
            <span>承重结构提示</span>
          </p>
          <ul class="mt-1.5 space-y-1">
            <li
              v-for="(w, i) in s.diagnosis.value.load_bearing_warning"
              :key="i"
              class="text-[11px] leading-relaxed text-wood"
            >
              {{ w }}
            </li>
          </ul>
        </div>
      </div>

      <!-- 执行轨迹：工程细节，不占首屏 -->
      <div v-if="s.result.value?.trace?.length" class="card p-4">
        <h2 class="mb-2 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
          <AppIcon name="list-checks" :size="16" class="text-botanical" />
          <span>执行轨迹</span>
          <span class="tag">{{ s.result.value.trace.length }} 步</span>
        </h2>
        <ul class="flex flex-col gap-1">
          <li
            v-for="(t, i) in s.result.value.trace"
            :key="i"
            class="flex items-center gap-2 rounded-lg bg-warm-sidebar/40 px-2.5 py-1.5 font-mono text-[11px]"
          >
            <span class="text-wood-muted">{{ String(i + 1).padStart(2, '0') }}</span>
            <span class="min-w-0 flex-1 truncate text-wood">
              {{ t.agent || t.step || '—' }}
            </span>
            <span v-if="t.elapsed_ms" class="num text-wood-muted">{{ t.elapsed_ms }}ms</span>
          </li>
        </ul>
      </div>
    </template>

    <!-- 有结果但没有诊断（降级路径下可能发生） -->
    <div v-else class="card">
      <EmptyState
        art="审查与风控-blocked_ldel"
        title="这次解析没有产出诊断"
        description="诊断依赖墙体、门窗与面积数据。本次解析可能走了降级路径，结构字段不可用 —— 换一张更清晰的户型图重试通常能解决。"
      >
        <RouterLink class="btn-ghost px-4 py-2" to="/parse">
          <AppIcon name="upload-simple" :size="16" class="text-botanical" />
          <span>重新解析</span>
        </RouterLink>
      </EmptyState>
    </div>
  </div>
</template>
