<script setup lang="ts">
import { computed } from 'vue'

import AppIcon from './AppIcon.vue'
import type { PhaseLogEntry } from '@/composables/useTaskPolling'

/**
 * 语义化进度。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么不显示百分比
 * ══════════════════════════════════════════════════════════════════
 * 需求文档 11.2.4：「用户看到『60%』是不知道系统在干什么的」。
 *
 * 解析一个户型图要 33–48 秒，让用户盯着一个数字干等是很差的体验。
 * 所以**主视觉是 `phase_text`（"正在识别房间…"）**，百分比退成一条
 * 细线——它还在，但不占视觉重心。
 *
 * 这个细节在演示时价值极高：它体现的是产品思维，不只是工程实现。
 */
const props = defineProps<{
  /** 后端给的中文阶段文案。直接展示，前端不建映射表 */
  text: string
  progress: number
  /** 已经跑了多久（毫秒） */
  elapsedMs: number
  /** 预计还剩多少秒。`null` = 后端估不出来（含已超出预期），此时不显示倒计时 */
  etaSeconds?: number | null
  /** 已超出后端模型预期 —— 改口说"比预期久"，不再报一个失效的数字 */
  overrun?: boolean
  /** 阶段变化历史。从轮询 composable 出来的是只读数组，这里按只读接 */
  log?: readonly PhaseLogEntry[]
  status?: 'pending' | 'processing' | 'completed' | 'failed'
  /** 失败原因。`status='failed'` 时顶上那句话换成它 */
  error?: string
  /** 是不是用户自己按的中断（AC-31）。措辞与配色都按"这不是错误"处理 */
  cancelled?: boolean
}>()

/**
 * ⚠️ 百分比**只有一个来源**。
 *
 * 原来这里有两处各算各的：标题右边写的是
 * `text === '完成' || status === 'completed' ? 100 : percent`，
 * 而进度条宽度用的是 `percent`。任务失败时 phase 是 `done`
 * （文案"完成"）、`percent` 是 0 —— 于是**数字显示 100%、条子却是空的**，
 * 一屏之内自相矛盾。
 *
 * 现在两边都用这一个值。
 */
const percent = computed(() => Math.max(0, Math.min(100, props.progress || 0)))

/**
 * 顶上那句话。
 *
 * ⚠️ 失败时**不能照抄后端的阶段文案** —— 失败会把 phase 落成 `done`，
 * 而 `PHASE_TEXT['done']` 是"完成"。于是界面显示一个大红叉配"完成"，
 * 用户完全不知道发生了什么。
 */
const headline = computed(() => {
  // ⚠️ 顺序有讲究：**先判"是不是用户按的"**。
  //    中断在数据上也是 `status='failed'`（status 是控制流通道，
  //    见 api/types.ts 的说明），不先分流的话，用户自己按的按钮
  //    会被报成一个红色的"失败"。
  if (props.cancelled) return '已中断'
  return props.status === 'failed' ? '解析失败' : props.text
})

/**
 * ══════════════════════════════════════════════════════════════════
 * 主视觉是**剩余时间**，不是已用时间
 * ══════════════════════════════════════════════════════════════════
 * 原来这行只有「已等待 42 秒」。用户的原话是：
 * 「进度条保证可以预估剩余的时间（现在我只看到记录了已经等待的时间）」——
 * 已用时间回答的是"我忍了多久"，剩余时间回答的才是"我还要忍多久"。
 * 后者才是决定用户会不会中途放弃的那个数。
 *
 * 三种状态，都是**如实**的：
 *   · 有估算  —— 「预计还剩 约 1 分 20 秒 · 已用 42 秒」
 *   · 超出了  —— 「已用 2 分 10 秒 · 比预期久，任务仍在进行」
 *   · 估不出  —— 「已等待 42 秒」（拿不到开始时间、或阶段不在模型里）
 *
 * ⚠️ 第二种状态**不显示倒计时**。模型已经被实际耗时追上时，
 *    继续报"预计还剩 3 秒"再挂 40 秒，比不给数字更消耗信任。
 */
const detail = computed(() => {
  if (props.cancelled) {
    return '已按你的要求停下，没有产出结果。这次消耗的额度不退。'
  }
  if (props.status === 'failed') {
    return props.error || '任务未产出结果，请重试'
  }
  const used = `已用 ${formatDuration(seconds.value)}`
  if (props.status === 'completed') return `用时 ${formatDuration(seconds.value)}`
  // ⚠️ 只有**还在跑**的时候才说"任务仍在进行"。已经失败了再说这句话，
  //    用户会一直等下去。
  if (props.overrun) return `${used} · 比预期久，任务仍在进行`
  if (props.etaSeconds !== null && props.etaSeconds !== undefined) {
    return `预计还剩 ${formatEta(props.etaSeconds)} · ${used}`
  }
  return `已等待 ${formatDuration(seconds.value)}`
})

const seconds = computed(() => Math.round(props.elapsedMs / 1000))

/**
 * 已用时间：按实际的秒数报。它是**量出来的**，不该四舍五入。
 */
function formatDuration(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds} 秒`
  const m = Math.floor(totalSeconds / 60)
  const s = totalSeconds % 60
  return s ? `${m} 分 ${s} 秒` : `${m} 分钟`
}

/**
 * 剩余时间：**必须粗**。
 *
 * 「预计还剩 1 分 23 秒」是假精度 —— 那 3 秒来自一个会被实际速度修正的模型，
 * 不可能准到秒。四舍五入到 10 秒（1 分半以内）或半分钟（更久）之后，
 * 数字才和它实际的可信度相称。
 */
function formatEta(etaSeconds: number): string {
  if (etaSeconds <= 15) return '只剩几秒'
  if (etaSeconds < 90) return `约 ${Math.round(etaSeconds / 10) * 10} 秒`
  const halfMinutes = Math.round(etaSeconds / 30) / 2
  return Number.isInteger(halfMinutes)
    ? `约 ${halfMinutes} 分钟`
    : `约 ${Math.floor(halfMinutes)} 分 30 秒`
}

const tone = computed(() => {
  // 中断用中性灰而不是红色 —— 用户自己按的按钮不该被画成故障
  if (props.cancelled) return 'text-wood-muted'
  if (props.status === 'failed') return 'text-accent-red'
  if (props.status === 'completed') return 'text-botanical'
  return 'text-botanical'
})

const barTone = computed(() => {
  if (props.cancelled) return 'bg-wood-muted/60'
  if (props.status === 'failed') return 'bg-accent-red'
  if (props.status === 'completed') return 'bg-botanical'
  return 'bg-botanical'
})

const duration = (ms: number) => (ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`)
</script>

<template>
  <div class="card p-4">
    <div class="flex items-center gap-3">
      <!-- 旋转指示器：只在真正处理中时转 -->
      <AppIcon
        v-if="status === 'processing' || status === 'pending'"
        name="spinner"
        :size="20"
        class="animate-spin text-botanical"
      />
      <AppIcon
        v-else-if="status === 'completed'"
        name="check-circle"
        :size="20"
        class="text-botanical"
      />
      <AppIcon
        v-else
        name="x-circle"
        :size="20"
        :class="cancelled ? 'text-wood-muted' : 'text-accent-red'"
      />

      <div class="min-w-0 flex-1">
        <!-- 主视觉是这句话，不是百分比 -->
        <p class="truncate text-[14px] font-semibold" :class="tone">{{ headline }}</p>
        <p class="mt-0.5 text-[11px] leading-relaxed text-wood-muted">
          {{ detail }}
        </p>
      </div>

      <span class="num hidden shrink-0 text-[13px] font-semibold text-wood-muted sm:block">
        {{ percent }}%
      </span>

      <!--
        操作位。**用插槽而不是直接内嵌「中断」按钮**：
        这个组件是纯展示的（它连 api 都不 import），而中断要发请求、
        要弹确认框。放进来的话，进度组件就同时成了"会改后端状态的东西"，
        测试和复用都会变难。谁用谁往里放。
      -->
      <slot name="actions" />
    </div>

    <div class="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-warm-border">
      <div
        class="h-full rounded-full transition-[width] duration-500 ease-out"
        :class="barTone"
        :style="{ width: `${percent}%` }"
      />
    </div>

    <!-- 阶段轨迹：让用户看到"系统做过什么"，而不只是"现在在做什么"。
         解析链有 8 个阶段，走完后这条轨迹本身就是一份可读的执行记录。 -->
    <ol v-if="log && log.length" class="mt-3 flex flex-wrap gap-x-3 gap-y-1">
      <li
        v-for="(p, i) in log"
        :key="p.phase + i"
        class="flex items-center gap-1.5 text-[10px] text-wood-muted"
      >
        <span
          class="h-1 w-1 rounded-full"
          :class="i === log.length - 1 ? 'bg-botanical' : 'bg-warm-border'"
        />
        <span :class="i === log.length - 1 ? 'font-semibold text-wood' : ''">{{ p.text }}</span>
        <span class="num text-wood-muted/60">{{ duration(p.atMs) }}</span>
      </li>
    </ol>
  </div>
</template>
