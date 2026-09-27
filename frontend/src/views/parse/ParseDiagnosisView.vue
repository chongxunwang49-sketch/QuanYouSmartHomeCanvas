<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import { layoutDiagnosis } from '@/api'
import { useParseSession } from '@/composables/useParseSession'
import type { Diagnosis, DiagnosisItem } from '@/api/types'

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
 *
 * ⚠️⚠️ **诊断优先看后端存下来的那一份，不是只看本次会话里的。**
 *    原因：屋主可以在「上传解析」页补一份户型详情，补完会**重新诊断** ——
 *    而那次解析任务早就结束了。只看会话里的结果，就会"上传了详情、
 *    通知栏说分数变了、点进这一页还是旧的"。所以这里从
 *    `/layout/{id}/diagnosis` 取一次，取不到再退回会话里那份。
 */
const s = useParseSession()

const layoutId = computed(() => s.layout.value?.layout_id ?? '')
/** 从后端取回的最新诊断（补完详情重新诊断过的那种）。 */
const stored = ref<Diagnosis | null>(null)

async function refresh() {
  const id = layoutId.value
  if (!id) return
  try {
    const d = await layoutDiagnosis(id)
    stored.value = d.diagnosis
  } catch {
    /* 取不到就用手上的那份 —— 这一页不该因为一次请求失败而空掉 */
  }
}
onMounted(refresh)
watch(layoutId, refresh)

/** 后端那份优先；没有就用会话里的。 */
const diag = computed(() => stored.value ?? s.diagnosis.value ?? null)

const DIMS: { key: string; label: string; icon: string }[] = [
  { key: 'lighting', label: '采光', icon: 'sparkle' },
  { key: 'ventilation', label: '通风', icon: 'leaf' },
  { key: 'circulation', label: '动线', icon: 'arrow-right' },
  { key: 'space_utilization', label: '空间利用率', icon: 'layout' },
  { key: 'green_score', label: '环保', icon: 'plant' },
]

function dimOf(key: string): DiagnosisItem | null {
  const d = diag.value as unknown as Record<string, DiagnosisItem> | null
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

    <template v-else-if="diag">
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
              :class="s.scoreTone(diag.overall_score)"
            >
              {{ diag.overall_score.toFixed(1) }}
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
          v-if="diag.summary"
          class="mt-3 rounded-xl border border-warm-border bg-warm-sidebar/50 p-3 text-[12px] leading-relaxed text-wood"
        >
          {{ diag.summary }}
        </p>

        <!-- 承重墙警告：这条不能折叠 -->
        <div
          v-if="diag.load_bearing_warning?.length"
          class="mt-3 rounded-xl border border-accent-red/30 bg-accent-red/5 p-3"
        >
          <p class="flex items-center gap-1.5 text-[12px] font-bold text-wood-dark">
            <AppIcon name="warning" :size="14" class="text-accent-red" />
            <span>承重结构提示</span>
          </p>
          <ul class="mt-1.5 space-y-1">
            <li
              v-for="(w, i) in diag.load_bearing_warning"
              :key="i"
              class="text-[11px] leading-relaxed text-wood"
            >
              {{ w }}
            </li>
          </ul>
        </div>

        <!--
          依据来源 + 置信度。
          ⚠️ 放在最下面但**不能省**：补了「户型详情」之后分数会变高，
          不把来源列出来，那个变化就是个黑箱（"凭什么从 6.4 变 8.1"）。
          置信度同理 —— 它现在能到 0.9 以上，正是因为依据齐了。
        -->
        <div class="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-warm-border pt-3">
          <span v-if="diag.confidence != null" class="flex items-center gap-1.5 text-[11px] text-wood-muted">
            <AppIcon name="shield-check" :size="13" class="text-botanical" />
            <span>置信度</span>
            <span class="num font-semibold text-wood-dark">
              {{ (diag.confidence * 100).toFixed(0) }}%
            </span>
          </span>
          <span class="flex items-center gap-1.5 text-[11px] text-wood-muted">
            <AppIcon name="check-circle" :size="13" class="text-botanical" />
            <span>数据缺口</span>
            <span class="num font-semibold text-wood-dark">{{ diag.data_gaps?.length ?? 0 }} 项</span>
          </span>
          <RouterLink
            v-if="!diag.evidence_sources?.some((x) => x.includes('户型详情'))"
            class="inline-flex items-center gap-1 text-[11px] text-botanical hover:underline"
            to="/parse"
          >
            <AppIcon name="file-plus" :size="13" />
            <span>补一份「户型详情」可以让评分更可靠</span>
          </RouterLink>
        </div>

        <ul v-if="diag.evidence_sources?.length" class="mt-2 space-y-0.5">
          <li
            v-for="(src, i) in diag.evidence_sources"
            :key="i"
            class="text-[10px] leading-relaxed text-wood-muted/80"
          >
            · 依据 {{ i + 1 }}：{{ src }}
          </li>
        </ul>
      </div>

    </template>

    <!-- 有结果但没有诊断（本次解析信息不完整时可能发生） -->
    <div v-else class="card">
      <EmptyState
        art="审查与风控-blocked_ldel"
        title="这次解析没有产出诊断"
        description="诊断要拿墙体、门窗与面积来判断，这次这几项没读全。换一张更清晰的户型图重试通常能解决。"
      >
        <RouterLink class="btn-ghost px-4 py-2" to="/parse">
          <AppIcon name="upload-simple" :size="16" class="text-botanical" />
          <span>重新解析</span>
        </RouterLink>
      </EmptyState>
    </div>
  </div>
</template>
