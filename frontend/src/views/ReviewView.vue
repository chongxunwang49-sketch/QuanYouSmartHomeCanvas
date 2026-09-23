<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import DegradedNotice from '@/components/DegradedNotice.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import PhaseProgress from '@/components/PhaseProgress.vue'
import { avoidPitReview } from '@/api'
import { toast } from '@/utils/toast'
import { messageOf } from '@/api/client'
import type { ReviewResult } from '@/api/types'
import { SEVERITY_LABEL, label } from '@/api/types'
import { useTaskPolling } from '@/composables/useTaskPolling'
import { useTaskStore } from '@/stores/task'

/**
 * 报价单避坑审查（A-06）。
 *
 * ⚠️ 这一页的核心不是"找出问题"，而是**每条结论都要能溯源**。
 * 需求文档 AC-06 要求引用必须真实存在于知识库中；模型编造的引用会被
 * 后端剔出并列进 `invented_citations`。那一块要展示出来——
 * 它证明的是"系统在拦模型胡说"，而不是"系统不会胡说"。
 */
const route = useRoute()
const router = useRouter()
const tasks = useTaskStore()
const poll = useTaskPolling<ReviewResult>()

const quoteText = ref('')
const submitting = ref(false)

const result = computed(() => poll.status.value?.result ?? null)
const review = computed(() => result.value?.review ?? null)

const findings = computed(() => review.value?.findings ?? [])

/** 按严重度分组统计，用于顶部的概览条 */
const bySeverity = computed(() => {
  const acc: Record<string, number> = { high: 0, medium: 0, low: 0 }
  for (const f of findings.value) acc[f.severity] = (acc[f.severity] ?? 0) + 1
  return acc
})

const SEVERITY_TONE: Record<string, string> = {
  high: 'border-accent-red/30 bg-accent-red/5 text-accent-red',
  medium: 'border-accent-gold/40 bg-wood-light/60 text-accent-gold',
  low: 'border-warm-border bg-warm-sidebar text-wood-muted',
}
const tone = (s: string) => SEVERITY_TONE[s] ?? SEVERITY_TONE.low

/** 一个演示用的报价单样例，方便直接试。**明确标注为演示样本。** */
const SAMPLE = `XX装饰工程有限公司 报价单
项目：三室两厅 128㎡ 全包

一、基础工程
1. 墙面基层处理及乳胶漆  128㎡ × 68元/㎡ = 8,704元
2. 地面找平              128㎡ × 45元/㎡ = 5,760元
3. 水电改造（按实结算）  预收 12,000元  ※ 最终以实际发生为准

二、主材
4. 强化复合地板          95㎡ × 129元/㎡ = 12,255元
5. 瓷砖（厨卫墙地）      62㎡ × 88元/㎡ = 5,456元
6. 木门                  5樘 × 1,200元/樘 = 6,000元

三、定制
7. 橱柜（含台面）        4.2延米 × 2,800元/延米 = 11,760元
8. 衣柜（含五金）        18㎡ × 950元/㎡ = 17,100元

四、其他
9. 管理费                按总价 8% 计取
10. 设计费               免收（签单赠送）
11. 拆改及垃圾清运       3,500元  ※ 不含外墙拆改

备注：本合同最终解释权归本公司所有。
付款方式：签订合同预付 60%，开工后付 35%，验收后付 5%。`

function useSample() {
  quoteText.value = SAMPLE
  toast.info('已填入演示报价单样本（非真实合同）')
}

async function submit() {
  if (quoteText.value.trim().length < 20) {
    toast.warning('请粘贴完整的报价单或合同文本')
    return
  }
  submitting.value = true
  try {
    const created = await avoidPitReview({ quote_text: quoteText.value })
    tasks.track({ taskId: created.task_id, kind: 'review' })
    router.replace({ query: { ...route.query, task: created.task_id } })

    const snap = await poll.start(created.task_id, created.estimated_seconds)
    if (poll.timedOut.value) {
      toast.warning('轮询超时（120 秒）。任务可能仍在后台执行。')
      return
    }
    if (!snap) return

    const n = snap.result?.review?.findings?.length ?? 0
    tasks.update(created.task_id, {
      status: snap.status,
      phaseText: snap.phase_text,
      degraded: snap.degraded,
      summary: n ? `发现 ${n} 项风险` : '未发现风险项',
    })

    if (snap.status === 'failed') toast.error(snap.error || '审查失败')
    else toast.success(n ? `发现 ${n} 项风险` : '审查完成，未发现风险项')
  } catch (e) {
    toast.error(messageOf(e))
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  const taskId = String(route.query.task || '')
  if (!taskId) return
  const snap = await poll.start(taskId)
  if (snap?.result) {
    tasks.track({
      taskId,
      kind: 'review',
      summary: `${snap.result.review?.findings?.length ?? 0} 项风险`,
    })
  }
})
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6">
    <PageHeader
      :breadcrumb="['全友·智绘家', '避坑审查']"
      title="报价单与合同审查"
      :status="
        poll.status.value
          ? { icon: 'shield-check', text: poll.status.value.phase_text }
          : { icon: 'shield-check', text: '等待提交', tone: 'wood' }
      "
    />

    <div class="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
      <!-- ══ 左：输入 ══ -->
      <section class="flex flex-col gap-4">
        <div class="card p-4">
          <div class="mb-2 flex items-center justify-between">
            <h2 class="flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
              <AppIcon name="file-text" :size="16" class="text-botanical" />
              <span>报价单 / 合同原文</span>
            </h2>
            <button class="btn-ghost px-2.5 py-1 !text-[11px]" type="button" @click="useSample">
              <AppIcon name="floppy-disk" :size="13" />
              <span>填入演示样本</span>
            </button>
          </div>

          <textarea
            v-model="quoteText"
            class="field h-80 resize-y px-3 py-2.5 font-mono !text-[12px] leading-relaxed"
            placeholder="把报价单或合同全文粘贴到这里。条款越完整，能查出的问题越多。"
          />

          <div class="mt-2 flex items-center justify-between">
            <span class="text-[11px] text-wood-muted">
              {{ quoteText.length }} 字
              <span v-if="quoteText.length" class="ml-1 text-wood-muted/70">
                · 内容只用于本次审查，不落库
              </span>
            </span>
            <button
              class="btn-primary px-4 py-2"
              type="button"
              :disabled="submitting || poll.running.value || quoteText.trim().length < 20"
              @click="submit"
            >
              <AppIcon :name="poll.running.value ? 'spinner' : 'magnifying-glass'" :size="16" />
              <span>{{ poll.running.value ? '审查中…' : '开始审查' }}</span>
            </button>
          </div>
        </div>

        <PhaseProgress
          v-if="poll.status.value"
          :text="poll.status.value.phase_text"
          :progress="poll.status.value.progress"
          :elapsed-ms="poll.elapsedMs.value"
          :over-estimate="poll.overEstimate.value"
          :log="poll.phaseLog.value"
          :status="poll.status.value.status"
        />

        <div class="card p-4">
          <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
            <AppIcon name="info" :size="14" class="text-wood-muted" />
            <span>这一页能查出什么</span>
          </p>
          <ul class="mt-2 space-y-1.5">
            <li
              v-for="t in [
                '明显高于或低于市场区间的材料单价',
                '按实结算、暂估、最终解释权之类的开口条款',
                '付款节奏失衡（预付比例过高）',
                '重复计费、漏项与以次充好的表述',
                '与环保标准相关的模糊承诺',
              ]"
              :key="t"
              class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
            >
              <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-botanical" />
              <span>{{ t }}</span>
            </li>
          </ul>
          <p class="mt-2.5 rounded-lg border border-warm-border bg-warm-sidebar/60 p-2 text-[10px] leading-relaxed text-wood-muted">
            审查结论由 RAG 检索 + 模型判断给出，**每条风险都必须带可溯源的引用**。
            找不到依据的结论会被标成"无引用"，模型编造的引用会被剔除并单列。
          </p>
        </div>
      </section>

      <!-- ══ 右：结果 ══ -->
      <section class="flex flex-col gap-4">
        <div v-if="!review" class="card">
          <EmptyState
            art="审查与风控-document-review_lfir"
            title="等待审查结果"
            description="提交后，系统会检索知识库中的规范与常见套路，逐条比对这份报价单，给出带引用来源的风险清单。"
          >
            <button class="btn-ghost px-4 py-2" type="button" @click="useSample">
              <AppIcon name="file-text" :size="16" class="text-botanical" />
              <span>先填一份演示样本</span>
            </button>
          </EmptyState>
        </div>

        <template v-else>
          <DegradedNotice v-if="result?.degraded" />

          <!-- 概览 -->
          <div class="card flex flex-wrap items-center justify-between gap-3 p-4">
            <div class="flex items-center gap-3">
              <span
                class="flex h-11 w-11 items-center justify-center rounded-xl border"
                :class="tone(review.overall_risk)"
              >
                <AppIcon
                  :name="review.overall_risk === 'high' ? 'warning' : 'shield-check'"
                  :size="22"
                />
              </span>
              <div>
                <p class="text-[11px] text-wood-muted">整体风险等级</p>
                <p class="font-serif text-[18px] font-bold text-wood-dark">
                  {{ label(SEVERITY_LABEL, review.overall_risk) }}
                </p>
              </div>
            </div>

            <div class="flex items-center gap-2">
              <span
                v-for="(n, s) in bySeverity"
                :key="s"
                class="rounded-full border px-2.5 py-1 text-[11px] font-semibold"
                :class="tone(String(s))"
              >
                {{ label(SEVERITY_LABEL, String(s)) }} {{ n }}
              </span>
            </div>
          </div>

          <p
            v-if="review.summary"
            class="card p-3.5 text-[12px] leading-relaxed text-wood"
          >
            {{ review.summary }}
          </p>

          <!-- 风险清单 -->
          <div
            v-for="(f, i) in findings"
            :key="i"
            class="card p-4"
          >
            <div class="flex items-start justify-between gap-3">
              <div class="flex min-w-0 items-start gap-2">
                <span
                  class="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-warm-sidebar font-mono text-[11px] font-bold text-wood"
                >
                  {{ i + 1 }}
                </span>
                <h3 class="text-[13px] font-bold leading-snug text-wood-dark">{{ f.title }}</h3>
              </div>
              <span
                class="shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold"
                :class="tone(f.severity)"
              >
                {{ label(SEVERITY_LABEL, f.severity) }}
              </span>
            </div>

            <p class="mt-1.5 font-mono text-[10px] text-wood-muted">位置：{{ f.where }}</p>
            <p class="mt-1.5 text-[12px] leading-relaxed text-wood">{{ f.detail }}</p>

            <p
              class="mt-2.5 rounded-lg border border-botanical/20 bg-botanical-surface p-2.5 text-[11px] leading-relaxed text-wood-dark"
            >
              <span class="font-semibold text-botanical">建议：</span>{{ f.suggestion }}
            </p>

            <div v-if="f.citations?.length" class="mt-2.5">
              <p class="mb-1 flex items-center gap-1 text-[10px] font-semibold text-wood-muted">
                <AppIcon name="book-open" :size="11" />
                <span>依据（{{ f.citations.length }} 条 · 来自知识库）</span>
              </p>
              <ul class="space-y-1">
                <li
                  v-for="(c, j) in f.citations"
                  :key="j"
                  class="rounded-lg border border-warm-border bg-white px-2.5 py-1.5"
                >
                  <p class="truncate text-[11px] font-medium text-wood-dark">{{ c.title }}</p>
                  <p class="truncate font-mono text-[10px] text-wood-muted">{{ c.source }}</p>
                </li>
              </ul>
            </div>
            <p
              v-else
              class="mt-2.5 rounded-lg border border-accent-gold/30 bg-wood-light/50 px-2.5 py-1.5 text-[10px] leading-relaxed text-wood"
            >
              这一项**没有检索到可引用的依据**，属于模型的判断。建议人工复核后再采纳。
            </p>
          </div>

          <!-- 被剔除的引用 -->
          <div
            v-if="review.invented_citations?.length"
            class="rounded-xl border border-accent-gold/40 bg-wood-light/50 p-3.5"
          >
            <p class="flex items-center gap-1.5 text-[12px] font-bold text-wood-dark">
              <AppIcon name="shield-check" :size="14" class="text-accent-gold" />
              <span>已拦截的编造引用（{{ review.invented_citations.length }}）</span>
            </p>
            <p class="mt-0.5 text-[11px] leading-relaxed text-wood-muted">
              模型在结论中引用了下列文献，但它们在知识库中并不存在。这些引用已被剔除，
              未参与上面的任何结论——**这一块存在的意义是证明系统在拦模型胡说**。
            </p>
            <ul class="mt-1.5 space-y-1">
              <li
                v-for="(c, i) in review.invented_citations"
                :key="i"
                class="break-words rounded bg-white/70 px-2 py-1 font-mono text-[10px] text-wood"
              >
                {{ c }}
              </li>
            </ul>
          </div>

          <!-- 议价要点 -->
          <div v-if="review.negotiation_points?.length" class="card p-4">
            <h3 class="mb-2 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
              <AppIcon name="currency-cny" :size="16" class="text-botanical" />
              <span>议价要点</span>
            </h3>
            <ul class="space-y-1.5">
              <li
                v-for="(p, i) in review.negotiation_points"
                :key="i"
                class="flex items-start gap-1.5 text-[12px] leading-relaxed text-wood"
              >
                <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-botanical" />
                <span>{{ p }}</span>
              </li>
            </ul>
          </div>

          <!-- 数据缺口 -->
          <div
            v-if="review.data_gaps?.length"
            class="card p-3.5"
          >
            <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
              <AppIcon name="info" :size="14" class="text-wood-muted" />
              <span>本次审查的局限</span>
            </p>
            <ul class="mt-1.5 space-y-1">
              <li
                v-for="(g, i) in review.data_gaps"
                :key="i"
                class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
              >
                <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-wood-muted/50" />
                <span>{{ g }}</span>
              </li>
            </ul>
          </div>
        </template>
      </section>
    </div>
  </main>
</template>
