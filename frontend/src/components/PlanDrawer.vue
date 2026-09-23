<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import AppIcon from './AppIcon.vue'
import DegradedNotice from './DegradedNotice.vue'
import type { Plan } from '@/api/types'
import { GRADE_LABEL, SEVERITY_LABEL, STYLE_LABEL, label } from '@/api/types'

/**
 * 方案深度报告抽屉。480px，从右侧滑出。
 *
 * 三个标签页对应三个 Agent 的产出：
 *   预算明细(BOM) ← A-04 规则引擎（注释里反复强调：金额不经过 LLM）
 *   风险预警      ← A-06 审查（引用必须可溯源）
 *   全友材料清单  ← A-05 选型（价格由代码回填）
 */
const props = defineProps<{ plan: Plan | null; open: boolean }>()
const emit = defineEmits<{ close: [] }>()

type Tab = 'bom' | 'risks' | 'materials'
const tab = ref<Tab>('bom')

// 换方案时回到第一个标签 —— 否则用户点开方案 B 会看到停在"风险"页，
// 而那是上一个方案的阅读位置。
watch(
  () => props.plan?.plan_id,
  () => (tab.value = 'bom'),
)

const budget = computed(() => props.plan?.budget ?? null)
const risks = computed(() => props.plan?.risks ?? null)
const materials = computed(() => props.plan?.materials ?? null)

const wan = (v: number) => (v / 10000).toFixed(1)

/** BOM 占比条。用金额中值算，不四舍五入到整数——否则细项会全变 0%。 */
const bomRows = computed(() => {
  const b = budget.value
  if (!b?.lines?.length) return []
  const totalMid = b.lines.reduce((s, l) => s + (l.amount_min + l.amount_max) / 2, 0) || 1
  return b.lines.map((l) => {
    const mid = (l.amount_min + l.amount_max) / 2
    return {
      key: l.key,
      label: l.label,
      unit: l.unit,
      basis: l.basis,
      quantity: l.quantity,
      amount: `¥${wan(l.amount_min)} - ${wan(l.amount_max)}万`,
      pct: Math.round((mid / totalMid) * 100),
      unitPrice: `¥${l.unit_price_min} - ${l.unit_price_max}`,
    }
  })
})

const SEVERITY_TONE: Record<string, string> = {
  high: 'border-accent-red/30 bg-accent-red/5 text-accent-red',
  medium: 'border-accent-gold/40 bg-wood-light/60 text-accent-gold',
  low: 'border-warm-border bg-warm-sidebar text-wood-muted',
}

const risktone = (s: string) => SEVERITY_TONE[s] ?? SEVERITY_TONE.low

const TABS: { key: Tab; label: string; icon: string }[] = [
  { key: 'bom', label: '预算明细 (BOM)', icon: 'receipt' },
  { key: 'risks', label: '风险预警', icon: 'shield-check' },
  { key: 'materials', label: '全友材料清单', icon: 'plant' },
]
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div
        v-if="open"
        class="fixed inset-0 z-[90] bg-wood-dark/30 backdrop-blur-xs"
        @click="emit('close')"
      />
    </Transition>

    <Transition
      enter-active-class="transition-transform duration-300 ease-out"
      leave-active-class="transition-transform duration-200 ease-in"
      enter-from-class="translate-x-full"
      leave-to-class="translate-x-full"
    >
      <aside
        v-if="open && plan"
        class="fixed right-0 top-0 z-[95] flex h-full w-[480px] max-w-full flex-col border-l border-warm-border bg-white shadow-lg"
      >
        <!-- ── 头部 ── -->
        <div class="flex items-center justify-between border-b border-warm-border bg-warm-sidebar/80 p-5">
          <div class="flex min-w-0 flex-col pr-2">
            <div class="flex items-center gap-2">
              <span
                class="rounded bg-botanical-light px-2 py-0.5 font-mono text-[10px] font-bold text-botanical"
              >
                REPORT
              </span>
              <span class="font-mono text-[11px] text-wood-muted">
                PLAN-{{ String(plan.plan_index + 1).padStart(3, '0') }}
              </span>
            </div>
            <h3 class="mt-0.5 truncate font-serif text-[17px] font-bold text-wood-dark">
              {{ label(STYLE_LABEL, plan.style) }} · {{ label(GRADE_LABEL, plan.budget_grade) }}
              完整深度报告
            </h3>
          </div>
          <button
            class="rounded-lg p-1.5 text-wood-muted transition-colors hover:bg-white hover:text-wood-dark"
            type="button"
            aria-label="关闭"
            @click="emit('close')"
          >
            <AppIcon name="x" :size="20" />
          </button>
        </div>

        <!-- ── 标签 ── -->
        <div class="flex border-b border-warm-border bg-white px-1">
          <button
            v-for="t in TABS"
            :key="t.key"
            class="flex flex-1 items-center justify-center gap-1.5 border-b-2 py-3 text-[13px] transition-colors"
            :class="
              tab === t.key
                ? 'border-botanical font-semibold text-wood'
                : 'border-transparent font-medium text-wood-muted hover:text-wood'
            "
            type="button"
            @click="tab = t.key"
          >
            <AppIcon :name="t.icon" :size="15" />
            <span>{{ t.label }}</span>
          </button>
        </div>

        <!-- ── 内容 ── -->
        <div class="flex flex-1 flex-col gap-4 overflow-y-auto scroll-thin bg-warm-bg/40 p-5">
          <!-- ══ 预算明细 ══ -->
          <template v-if="tab === 'bom'">
            <template v-if="budget">
              <div class="card flex items-center justify-between p-3.5">
                <div>
                  <span class="text-[11px] text-wood-muted">核算工程总预算</span>
                  <div class="num mt-0.5 text-[20px] font-bold text-wood-dark">
                    ¥ {{ wan(budget.total_min) }} - {{ wan(budget.total_max) }}
                    <span class="text-[12px] font-semibold text-wood">万元</span>
                  </div>
                </div>
                <span
                  class="rounded-full bg-botanical-light px-2.5 py-1 font-mono text-[11px] font-semibold text-botanical"
                >
                  {{ budget.lines.length }} 个分项
                </span>
              </div>

              <!-- 单价定位 -->
              <div class="card flex items-center justify-between p-3">
                <span class="text-[12px] text-wood-muted">单方造价</span>
                <span class="num text-[13px] font-semibold text-wood-dark">
                  ¥{{ Math.round(budget.price_per_sqm_min) }} - {{ Math.round(budget.price_per_sqm_max) }} / ㎡
                  <span class="ml-1 text-[11px] font-normal text-wood-muted">
                    （按 {{ budget.area.toFixed(1) }} ㎡）
                  </span>
                </span>
              </div>

              <div class="card p-4">
                <p class="mb-2 text-[12px] font-semibold text-wood-dark">分项工程金额与比重</p>
                <div class="flex flex-col gap-2">
                  <div
                    v-for="r in bomRows"
                    :key="r.key"
                    class="flex flex-col gap-1 rounded-lg bg-warm-sidebar/50 p-2"
                  >
                    <div class="flex justify-between gap-2 text-[12px]">
                      <span class="min-w-0 truncate font-medium text-wood-dark" :title="r.label">
                        {{ r.label }}
                      </span>
                      <span class="num shrink-0 text-wood">{{ r.amount }} ({{ r.pct }}%)</span>
                    </div>
                    <div class="h-1.5 w-full overflow-hidden rounded-full bg-warm-border">
                      <div
                        class="h-full rounded-full bg-botanical transition-[width] duration-500"
                        :style="{ width: `${r.pct}%` }"
                      />
                    </div>
                    <p class="font-mono text-[10px] text-wood-muted">
                      {{ r.quantity }} {{ r.unit }} × {{ r.unitPrice }} · {{ r.basis }}
                    </p>
                  </div>
                  <p v-if="!bomRows.length" class="py-4 text-center text-[12px] text-wood-muted">
                    没有分项数据
                  </p>
                </div>
              </div>

              <!-- 文字解读（LLM 产出，无金额字段） -->
              <div v-if="budget.narrative" class="card p-4">
                <div class="mb-2 flex items-center gap-2">
                  <AppIcon name="brain" :size="15" class="text-botanical" />
                  <p class="text-[12px] font-semibold text-wood-dark">造价解读</p>
                  <span
                    v-if="budget.narrative_degraded"
                    class="rounded border border-accent-gold/30 bg-wood-light px-1.5 text-[10px] text-accent-gold"
                  >
                    模板回退
                  </span>
                </div>
                <p class="text-[12px] leading-relaxed text-wood">
                  {{ budget.narrative.summary }}
                </p>
                <p class="mt-2 text-[11px] leading-relaxed text-wood-muted">
                  {{ budget.narrative.grade_rationale }}
                </p>

                <div v-if="budget.narrative.saving_tips?.length" class="mt-3">
                  <p class="mb-1 text-[11px] font-semibold text-botanical">省钱建议</p>
                  <ul class="space-y-1">
                    <li
                      v-for="(s, i) in budget.narrative.saving_tips"
                      :key="i"
                      class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
                    >
                      <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-botanical" />
                      <span>{{ s }}</span>
                    </li>
                  </ul>
                </div>

                <div v-if="budget.narrative.negotiation_tips?.length" class="mt-3">
                  <p class="mb-1 text-[11px] font-semibold text-wood">议价要点</p>
                  <ul class="space-y-1">
                    <li
                      v-for="(s, i) in budget.narrative.negotiation_tips"
                      :key="i"
                      class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
                    >
                      <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-wood" />
                      <span>{{ s }}</span>
                    </li>
                  </ul>
                </div>
              </div>

              <p
                class="rounded-xl border border-warm-border bg-warm-sidebar/50 p-2.5 text-[10px] leading-relaxed text-wood-muted"
              >
                金额全部由后端规则引擎按户型面积与地区系数算出，**不经过大模型**（ADR-07）。
                模型只负责上面那段文字解读。
              </p>
            </template>

            <p v-else class="py-10 text-center text-[12px] text-wood-muted">
              该方案没有产出预算数据
            </p>
          </template>

          <!-- ══ 风险预警 ══ -->
          <template v-else-if="tab === 'risks'">
            <template v-if="risks">
              <div
                class="flex items-start gap-3 rounded-xl border p-3.5"
                :class="
                  risks.overall_risk === 'high'
                    ? 'border-accent-red/30 bg-accent-red/5'
                    : 'border-botanical/30 bg-botanical-surface'
                "
              >
                <AppIcon
                  :name="risks.overall_risk === 'high' ? 'warning' : 'shield-check'"
                  :size="20"
                  class="mt-0.5 shrink-0"
                  :class="risks.overall_risk === 'high' ? 'text-accent-red' : 'text-botanical'"
                />
                <div class="min-w-0">
                  <p class="text-[13px] font-bold text-wood-dark">
                    整体风险等级：{{ label(SEVERITY_LABEL, risks.overall_risk) }}
                  </p>
                  <p class="mt-0.5 text-[11px] leading-relaxed text-wood-muted">
                    {{ risks.summary }}
                  </p>
                </div>
              </div>

              <div
                v-for="(f, i) in risks.findings"
                :key="i"
                class="card p-3.5"
              >
                <div class="flex items-start justify-between gap-2">
                  <span class="text-[13px] font-bold text-wood-dark">{{ f.title }}</span>
                  <span
                    class="shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold"
                    :class="risktone(f.severity)"
                  >
                    {{ label(SEVERITY_LABEL, f.severity) }}
                  </span>
                </div>
                <p class="mt-1 font-mono text-[10px] text-wood-muted">位置：{{ f.where }}</p>
                <p class="mt-1.5 text-[11px] leading-relaxed text-wood">{{ f.detail }}</p>
                <p
                  class="mt-2 rounded-lg border border-warm-border bg-warm-sidebar/60 p-2 text-[11px] leading-relaxed text-wood-dark"
                >
                  <span class="font-semibold text-botanical">建议：</span>{{ f.suggestion }}
                </p>

                <!-- 引用：可溯源是 AC-06 的核心 -->
                <div v-if="f.citations?.length" class="mt-2">
                  <p class="mb-1 flex items-center gap-1 text-[10px] font-semibold text-wood-muted">
                    <AppIcon name="book-open" :size="11" />
                    <span>依据（{{ f.citations.length }} 条）</span>
                  </p>
                  <ul class="space-y-1">
                    <li
                      v-for="(c, j) in f.citations"
                      :key="j"
                      class="rounded-lg border border-warm-border bg-white px-2 py-1.5"
                    >
                      <p class="truncate text-[11px] font-medium text-wood-dark">{{ c.title }}</p>
                      <p class="truncate font-mono text-[10px] text-wood-muted">{{ c.source }}</p>
                    </li>
                  </ul>
                </div>
                <p
                  v-else
                  class="mt-2 rounded-lg border border-accent-gold/30 bg-wood-light/50 px-2 py-1.5 text-[10px] leading-relaxed text-wood"
                >
                  这一项**没有找到可引用的依据**——它是模型的判断，请人工复核后再采纳。
                </p>
              </div>

              <!-- 编造的引用：如实标出来 -->
              <div
                v-if="risks.invented_citations?.length"
                class="rounded-xl border border-accent-gold/40 bg-wood-light/50 p-3"
              >
                <p class="flex items-center gap-1.5 text-[12px] font-bold text-wood-dark">
                  <AppIcon name="info" :size="14" class="text-accent-gold" />
                  <span>被剔除的引用（{{ risks.invented_citations.length }}）</span>
                </p>
                <p class="mt-0.5 text-[11px] leading-relaxed text-wood-muted">
                  模型给出的以下引用在知识库中不存在，已被剔除，未参与上面的结论：
                </p>
                <ul class="mt-1.5 space-y-0.5">
                  <li
                    v-for="(c, i) in risks.invented_citations"
                    :key="i"
                    class="break-words font-mono text-[10px] text-wood-muted"
                  >
                    {{ c }}
                  </li>
                </ul>
              </div>

              <div v-if="!risks.findings?.length" class="py-6 text-center">
                <AppIcon name="check-circle" :size="28" class="mx-auto text-botanical" />
                <p class="mt-2 text-[12px] font-semibold text-wood-dark">未发现风险项</p>
                <p class="mt-1 text-[11px] text-wood-muted">
                  这不等于"没有问题"——只说明在已检索到的知识范围内没有匹配到。
                </p>
              </div>
            </template>

            <p v-else class="py-10 text-center text-[12px] text-wood-muted">
              该方案没有产出风险审查
            </p>
          </template>

          <!-- ══ 材料清单 ══ -->
          <template v-else>
            <template v-if="materials">
              <div class="card flex items-center justify-between p-3.5">
                <div>
                  <span class="text-[11px] text-wood-muted">全友自有产品覆盖</span>
                  <div class="num mt-0.5 text-[20px] font-bold text-wood-dark">
                    {{ Math.round(materials.quanyou_coverage * 100) }}%
                  </div>
                </div>
                <span
                  class="rounded-full px-2.5 py-1 font-mono text-[11px] font-semibold"
                  :class="
                    materials.quanyou_met
                      ? 'bg-botanical-light text-botanical'
                      : 'bg-wood-light text-accent-gold'
                  "
                >
                  {{ materials.quanyou_met ? '达标 ≥60%' : '未达 60%' }}
                </span>
              </div>

              <div
                v-for="(m, i) in materials.items"
                :key="m.product_id"
                class="card flex items-center gap-3 p-3"
              >
                <div
                  class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg font-mono text-[12px] font-bold"
                  :class="m.is_quanyou ? 'bg-botanical-light text-botanical' : 'bg-wood-light text-wood'"
                >
                  {{ String(i + 1).padStart(2, '0') }}
                </div>
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-1.5">
                    <h4 class="truncate text-[13px] font-bold text-wood-dark">{{ m.name }}</h4>
                    <span
                      v-if="m.is_quanyou"
                      class="shrink-0 rounded border border-botanical/20 bg-botanical-light px-1 text-[9px] font-semibold text-botanical"
                    >
                      全友
                    </span>
                  </div>
                  <p class="mt-0.5 truncate text-[11px] text-wood-muted">
                    {{ m.brand }} · {{ m.spec || m.category_label }}
                  </p>
                </div>
                <div class="shrink-0 text-right">
                  <p class="num text-[12px] font-bold text-botanical">
                    ¥{{ m.price_range[0] }} - {{ m.price_range[1] }}
                  </p>
                  <p class="text-[10px] text-wood-muted">{{ m.unit }}</p>
                </div>
              </div>

              <div v-if="materials.auto_substitutions?.length" class="card p-3.5">
                <p class="mb-2 flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                  <AppIcon name="arrow-right" :size="14" class="text-botanical" />
                  <span>自动替代（{{ materials.auto_substitutions.length }}）</span>
                </p>
                <ul class="space-y-1.5">
                  <li
                    v-for="(s, i) in materials.auto_substitutions"
                    :key="i"
                    class="rounded-lg bg-warm-sidebar/50 p-2 text-[11px] leading-relaxed text-wood-muted"
                  >
                    <span class="font-mono text-wood">{{ s.from_product_id }}</span>
                    →
                    <span class="font-mono text-botanical">{{ s.to_product_id }}</span>
                    <span class="mt-0.5 block">{{ s.reason }}</span>
                  </li>
                </ul>
              </div>

              <p
                class="rounded-xl border border-accent-gold/30 bg-wood-light/40 p-2.5 text-[10px] leading-relaxed text-wood"
              >
                ⚠️ 价格来自演示用种子数据集，**不是真实市场报价**，仅供流程演示。
                实际价格以门店与官网为准。
              </p>
            </template>

            <p v-else class="py-10 text-center text-[12px] text-wood-muted">
              该方案没有产出材料选型
            </p>
          </template>

          <!-- 降级提示：放在抽屉底部，三个标签都看得见 -->
          <DegradedNotice
            v-if="plan.missing_artifacts?.length"
            compact
            :reasons="plan.missing_artifacts.map((k) => `缺少产物：${k}`)"
          />
        </div>
      </aside>
    </Transition>
  </Teleport>
</template>
