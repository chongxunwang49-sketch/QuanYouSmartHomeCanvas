<script setup lang="ts">
import { computed } from 'vue'

/**
 * 空状态。插画来自 `ui参考/02-插画-unDraw/`（MIT 类许可，可商用）。
 *
 * 空状态是最容易被做成"一句话 + 一个灰图标"的地方，也是最能体现
 * 产品完成度的地方——用户在这里第一次感到"这个系统是给我用的"。
 */
const props = withDefaults(
  defineProps<{
    /** 插画名，对应 src/assets/illustrations 里的文件关键词 */
    art?: string
    title: string
    description?: string
  }>(),
  { art: '设计与户型-design-components_c2hs' },
)

// eager + 作为 URL 引入：Vite 会给每个插画生成带 hash 的地址，
// 只有真正用到的才进产物。
const modules = import.meta.glob('../assets/illustrations/*.svg', {
  eager: true,
  query: '?url',
  import: 'default',
}) as Record<string, string>

const byStem: Record<string, string> = {}
for (const [path, url] of Object.entries(modules)) {
  byStem[path.split('/').pop()!.replace(/\.svg$/, '')] = url
}

/** 允许只给关键词，取名里包含它的第一张。缺了就退回不显示插画。 */
const src = computed(() => {
  if (byStem[props.art]) return byStem[props.art]
  const key = Object.keys(byStem).find((k) => k.includes(props.art))
  return key ? byStem[key] : ''
})
</script>

<template>
  <div class="flex flex-col items-center justify-center px-6 py-12 text-center">
    <img
      v-if="src"
      :src="src"
      alt=""
      class="mb-5 h-40 w-auto opacity-90"
      loading="lazy"
    />
    <h3 class="font-serif text-[18px] font-semibold text-wood-dark">{{ title }}</h3>
    <p v-if="description" class="mt-1.5 max-w-md text-[13px] leading-relaxed text-wood-muted">
      {{ description }}
    </p>
    <div v-if="$slots.default" class="mt-5 flex flex-wrap items-center justify-center gap-2">
      <slot />
    </div>
  </div>
</template>
