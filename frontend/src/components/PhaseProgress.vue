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
 * 需求文档 2.2.4：「用户看到『60%』是不知道系统在干什么的」。
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
  /** 是否已超出后端给的估算 */
  overEstimate?: boolean
  /** 阶段变化历史。从轮询 composable 出来的是只读数组，这里按只读接 */
  log?: readonly PhaseLogEntry[]
  status?: 'pending' | 'processing' | 'completed' | 'failed'
}>()

const percent = computed(() => Math.max(0, Math.min(100, props.progress || 0)))

const seconds = computed(() => Math.round(props.elapsedMs / 1000))

const tone = computed(() => {
  if (props.status === 'failed') return 'text-accent-red'
  if (props.status === 'completed') return 'text-botanical'
  return 'text-botanical'
})

const barTone = computed(() => {
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
      <AppIcon v-else name="x-circle" :size="20" class="text-accent-red" />

      <div class="min-w-0 flex-1">
        <!-- 主视觉是这句话，不是百分比 -->
        <p class="truncate text-[14px] font-semibold" :class="tone">{{ text }}</p>
        <p class="mt-0.5 text-[11px] text-wood-muted">
          已等待 <span class="num">{{ seconds }}</span> 秒
          <span v-if="overEstimate" class="ml-1 text-accent-gold">
            · 比预期久一些，任务仍在进行
          </span>
        </p>
      </div>

      <span class="num hidden shrink-0 text-[13px] font-semibold text-wood-muted sm:block">
        {{ text === '完成' || status === 'completed' ? 100 : percent }}%
      </span>
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
