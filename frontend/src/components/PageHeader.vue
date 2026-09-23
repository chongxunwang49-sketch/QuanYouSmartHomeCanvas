<script setup lang="ts">
import AppIcon from './AppIcon.vue'

/**
 * 页头：面包屑 + 衬线大标题 + 状态胶囊。
 *
 * ⚠️ **标题用衬线（Newsreader）、正文用无衬线（Manrope）** 是这套设计
 * 最容易被忽略的一条。衬线只出现在 `h1`/`h2` 和方案名上——一旦漫延到
 * 正文，界面会从"产品"变成"杂志"。
 */
defineProps<{
  breadcrumb: string[]
  title: string
  /** 状态胶囊：图标名 + 文案。传空则不渲染 */
  status?: { icon: string; text: string; tone?: 'botanical' | 'wood' | 'gold' }
  /** 右侧的哈希/编号之类的等宽小字 */
  code?: string
}>()

const TONE: Record<string, string> = {
  botanical: 'bg-botanical-light text-botanical border-botanical/20',
  wood: 'bg-wood-light text-wood border-wood/20',
  gold: 'bg-wood-light text-accent-gold border-accent-gold/30',
}
</script>

<template>
  <section class="flex flex-col gap-3 pb-2 xl:flex-row xl:items-center xl:justify-between">
    <div class="flex min-w-0 flex-col gap-1">
      <nav class="flex items-center gap-2 text-[12px] font-medium text-wood-muted">
        <template v-for="(crumb, i) in breadcrumb" :key="crumb">
          <span :class="i === breadcrumb.length - 1 ? 'truncate font-semibold text-wood-dark' : ''">
            {{ crumb }}
          </span>
          <span v-if="i < breadcrumb.length - 1" class="text-warm-border">/</span>
        </template>
        <span class="h-1.5 w-1.5 rounded-full bg-botanical" />
      </nav>

      <div class="flex flex-wrap items-center gap-3">
        <h1 class="font-serif text-[24px] font-medium leading-tight tracking-tight text-wood-dark">
          {{ title }}
        </h1>
        <div
          v-if="status"
          class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold"
          :class="TONE[status.tone ?? 'botanical']"
        >
          <AppIcon :name="status.icon" :size="14" />
          <span>{{ status.text }}</span>
        </div>
        <span v-if="code" class="font-mono text-[11px] text-wood-muted/80">{{ code }}</span>
      </div>
    </div>

    <div v-if="$slots.actions" class="flex flex-wrap items-center gap-2">
      <slot name="actions" />
    </div>
  </section>
</template>
