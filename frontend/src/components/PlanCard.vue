<script setup lang="ts">
import { computed } from 'vue'

import AppIcon from './AppIcon.vue'
import type { Plan } from '@/api/types'
import { GRADE_TONE, STYLE_LABEL, GRADE_LABEL, label } from '@/api/types'

/**
 * 单张方案卡。三列并排，推荐位加粗叶绿边。
 *
 * ══════════════════════════════════════════════════════════════════
 * 关于图片上的"材质热点"
 * ══════════════════════════════════════════════════════════════════
 * 设计稿里图片上浮着几颗叶绿圆点，悬停弹出「选材热点 · 地面 / ¥129 ㎡」。
 * **这一版没有实现那个位置感**，因为：我们手上只有实景参考图，
 * 没有"这个方案的沙发在图上的第几个像素"这种数据。把热点摆在编造的
 * 坐标上，用户悬停看到的是一句真话（材料名和价格都是真的）配一个假位置
 * ——这种"半真"比全假更坏，因为它看起来可信。
 *
 * 所以这里把材料清单**平铺在图下方**（信息不丢），并在图上明确标注
 * 「参考实景 · 非本方案渲染图」。等 M5 的矢量渲染器产出了真实热区坐标，
 * 再把热点接回图上——那时位置是真的，热点才有意义。
 */
const props = defineProps<{
  plan: Plan
  /** 是否是推荐位（第二张卡）。由父组件决定，本组件不自行判断优劣 */
  featured?: boolean
  /** 参考实景图的地址 */
  image?: string
  imageIndex?: number
}>()

const emit = defineEmits<{ detail: []; select: [] }>()

const budget = computed(() => props.plan.budget)
const space = computed(() => props.plan.space_plan)
const materials = computed(() => props.plan.materials)
const risks = computed(() => props.plan.risks)

/** 造价以「万」为单位展示——这是家装行业的口语习惯 */
const wan = (v: number) => (v / 10000).toFixed(1)

const priceText = computed(() => {
  const b = budget.value
  if (!b || (!b.total_min && !b.total_max)) return null
  return { min: wan(b.total_min), max: wan(b.total_max) }
})

const planCode = computed(() => `PLAN-${String(props.plan.plan_index + 1).padStart(3, '0')}`)

const quanyouPct = computed(() => {
  const c = materials.value?.quanyou_coverage
  return typeof c === 'number' ? Math.round(c * 100) : null
})

/** 高风险项计数。用来在卡面上给出一个"要不要细看"的信号 */
const highRisks = computed(
  () => risks.value?.findings?.filter((f) => f.severity === 'high').length ?? 0,
)

const hasMissing = computed(() => (props.plan.missing_artifacts?.length ?? 0) > 0)

const MISSING_LABEL: Record<string, string> = {
  space_plan: '空间规划',
  budget: '预算造价',
  materials: '材料选型',
  risks: '风险审查',
}
</script>

<template>
  <article
    class="relative flex flex-col justify-between p-4"
    :class="featured ? 'card-featured' : 'card card-hover'"
  >
    <div class="flex flex-col gap-2.5">
      <!-- ── 头部：档位徽章 + 编号 ── -->
      <div class="flex items-center justify-between gap-2">
        <span
          v-if="featured"
          class="inline-flex items-center gap-1 rounded-full bg-botanical px-2.5 py-0.5 text-[11px] font-semibold text-white shadow-xs"
        >
          <AppIcon name="leaf" :size="13" />
          <span>平衡优选 · 推荐位</span>
        </span>
        <span
          v-else
          class="inline-flex items-center gap-1 rounded-full border border-warm-border bg-warm-sidebar px-2 py-0.5 text-[11px] font-medium text-wood"
        >
          <span class="h-1.5 w-1.5 rounded-full bg-wood-muted" />
          {{ label(GRADE_TONE, plan.budget_grade) }}
        </span>

        <span class="shrink-0 font-mono text-[11px] text-wood-muted">{{ planCode }}</span>
      </div>

      <!-- ── 方案名（衬线）── -->
      <div>
        <h2
          class="font-serif text-[18px] leading-tight text-wood-dark"
          :class="featured ? 'font-bold' : 'font-semibold'"
        >
          方案 {{ String(plan.plan_index + 1).padStart(2, '0') }}：{{
            label(STYLE_LABEL, plan.style)
          }} · {{ label(GRADE_LABEL, plan.budget_grade) }}
        </h2>
        <p class="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-wood-muted">
          {{ space?.summary || budget?.narrative?.summary || '该分支未产出设计说明' }}
        </p>
      </div>

      <!-- ── 造价条 ── -->
      <div
        class="flex items-center justify-between rounded-xl border p-2.5"
        :class="
          featured
            ? 'border-botanical/30 bg-botanical-surface'
            : 'border-warm-border bg-warm-sidebar/80'
        "
      >
        <div>
          <span
            class="block text-[10px] font-semibold uppercase"
            :class="featured ? 'text-botanical' : 'text-wood-muted'"
          >
            预估总造价
          </span>
          <span v-if="priceText" class="num text-[20px] font-bold leading-none text-wood-dark">
            ¥ {{ priceText.min }} - {{ priceText.max }}
            <span class="text-[12px] font-medium">万</span>
          </span>
          <span v-else class="text-[15px] font-semibold text-wood-muted">预算分支未产出</span>
        </div>
        <div class="text-right">
          <div class="flex items-center justify-end gap-1 text-[12px] text-wood-dark">
            <AppIcon
              :name="quanyouPct !== null ? 'check-circle' : 'warning-circle'"
              :size="14"
              :class="featured ? 'text-botanical' : 'text-wood-muted'"
            />
            <span class="num font-medium">
              {{ quanyouPct !== null ? `全友覆盖 ${quanyouPct}%` : '覆盖未知' }}
            </span>
          </div>
          <span class="mt-0.5 block text-[10px]" :class="featured ? 'text-botanical' : 'text-wood-muted'">
            {{ budget?.lines?.length ? `${budget.lines.length} 个分项` : '无分项' }}
          </span>
        </div>
      </div>

      <!-- ── 参考实景图 ── -->
      <div
        class="group/render relative overflow-hidden rounded-xl border border-warm-border bg-warm-sidebar"
      >
        <img
          v-if="image"
          :src="image"
          :alt="`方案 ${plan.plan_index + 1} 参考实景`"
          loading="lazy"
          class="aspect-[16/9] w-full object-cover transition-transform duration-500 group-hover/render:scale-[1.03]"
        />
        <div
          v-else
          class="flex aspect-[16/9] w-full items-center justify-center text-wood-muted/50"
        >
          <AppIcon name="house-line" :size="32" />
        </div>

        <div
          class="pointer-events-none absolute inset-0 bg-gradient-to-t from-wood-dark/25 via-transparent to-transparent"
        />

        <!--
          ⚠️ 标注必须留：这张图是**参考实景**，不是本方案的三维渲染图。
          不标的话，用户会以为看到的就是自己家装完的样子。
        -->
        <span
          class="absolute left-2 top-2 rounded border border-warm-border bg-white/95 px-1.5 py-0.5 font-mono text-[9px] text-wood-muted backdrop-blur-sm"
        >
          参考实景 · 非本方案渲染图
        </span>

        <span
          v-if="highRisks"
          class="absolute bottom-2 right-2 flex items-center gap-1 rounded border border-accent-red/25 bg-white/95 px-1.5 py-0.5 text-[9px] font-semibold text-accent-red backdrop-blur-sm"
        >
          <AppIcon name="warning" :size="11" />
          <span>{{ highRisks }} 项高风险</span>
        </span>
      </div>

      <!-- ── 核心标签 ── -->
      <div class="flex flex-wrap gap-1.5">
        <template v-if="featured">
          <span
            v-for="lv in (space?.key_moves ?? []).slice(0, 3)"
            :key="lv"
            class="tag-botanical max-w-full truncate"
          >
            {{ lv }}
          </span>
        </template>
        <template v-else>
          <span
            v-for="lv in (space?.key_moves ?? []).slice(0, 3)"
            :key="lv"
            class="tag max-w-full truncate"
          >
            {{ lv }}
          </span>
        </template>
        <span v-if="!space?.key_moves?.length" class="tag text-wood-muted">暂无核心改动</span>
      </div>

      <!-- ── 缺失产物：如实列出，不假装完整 ── -->
      <div
        v-if="hasMissing"
        class="flex items-start gap-1.5 rounded-lg border border-accent-gold/30 bg-wood-light/50 px-2 py-1.5"
      >
        <AppIcon name="warning-circle" :size="12" class="mt-0.5 shrink-0 text-accent-gold" />
        <span class="text-[10px] leading-relaxed text-wood">
          缺失：{{
            plan.missing_artifacts.map((k) => MISSING_LABEL[k] ?? k).join('、')
          }}
        </span>
      </div>
    </div>

    <!-- ── 底部动作 ── -->
    <div class="mt-2 flex gap-2 border-t border-warm-border/60 pt-3">
      <button
        class="flex flex-1 items-center justify-center gap-1.5 rounded-xl px-3 py-2 text-[12px] font-semibold transition-all"
        :class="
          featured
            ? 'bg-botanical text-white shadow-sm hover:bg-botanical-hover'
            : 'border border-warm-border bg-warm-sidebar text-wood-dark shadow-xs hover:border-wood/30 hover:bg-white'
        "
        type="button"
        @click="emit('detail')"
      >
        <AppIcon name="eye" :size="16" :class="featured ? '' : 'text-wood-muted'" />
        <span>查看明细</span>
      </button>
      <button
        class="rounded-xl px-3 py-2 text-[12px] font-medium transition-all"
        :class="
          featured
            ? 'border border-botanical/30 bg-botanical-light font-semibold text-botanical hover:bg-botanical hover:text-white'
            : 'border border-warm-border bg-white text-wood shadow-xs hover:border-wood/40'
        "
        type="button"
        @click="emit('select')"
      >
        选定
      </button>
    </div>
  </article>
</template>
