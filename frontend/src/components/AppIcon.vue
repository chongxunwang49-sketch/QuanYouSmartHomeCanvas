<script setup lang="ts">
import { computed } from 'vue'

/**
 * 图标。**内联采集来的 Iconify SVG**（`ui参考/03-图标-Iconify/`），不走网络。
 *
 * 为什么不用图标字体或 CDN：
 *  1. 项目要求断网可跑（后端测试全程不联网，前端同理）
 *  2. 字体图标是"一整包全都下载"，而这里 Vite 只会把**实际引用到**的
 *     SVG 打进产物，没引用的不进包
 *
 * 用法：
 *   <AppIcon name="house" />              → ph-house.svg
 *   <AppIcon name="house" weight="bold" /> → ph-house-bold.svg
 *   <AppIcon name="sofa" />                → tabler-sofa.svg（ph 没有时自动回落）
 */
const props = withDefaults(
  defineProps<{
    name: string
    /** 尺寸。直接给字号即可，SVG 是 1em 的 */
    size?: number | string
    weight?: 'regular' | 'bold'
    /** 图标集。默认 ph（Phosphor），找不到会回落 tabler */
    set?: 'ph' | 'tabler'
  }>(),
  { size: 18, weight: 'regular', set: 'ph' },
)

// eager + ?raw：构建期就把 SVG 文本读进来，运行时零请求
const modules = import.meta.glob('../assets/icons/**/*.svg', {
  eager: true,
  query: '?raw',
  import: 'default',
}) as Record<string, string>

/** 文件名 → SVG 文本。同名时后出现的覆盖先出现的，无所谓——内容一致。 */
const byFile: Record<string, string> = {}
for (const [path, svg] of Object.entries(modules)) {
  const file = path.split('/').pop()!
  byFile[file] = svg
}

const svg = computed(() => {
  const candidates =
    props.weight === 'bold'
      ? [`${props.set}-${props.name}-bold.svg`, `${props.set}-${props.name}.svg`]
      : [`${props.set}-${props.name}.svg`, `${props.set}-${props.name}-bold.svg`]

  for (const file of candidates) {
    if (byFile[file]) return byFile[file]
  }
  // 指定图标集里没有就换个集子找 —— 两个集子的覆盖率不一样，
  // 与其让界面出现一个空洞，不如换个风格的近似图标。
  const other = props.set === 'ph' ? 'tabler' : 'ph'
  for (const file of [`${other}-${props.name}.svg`, `${other}-${props.name}-bold.svg`]) {
    if (byFile[file]) return byFile[file]
  }
  return ''
})

const sizeStyle = computed(() =>
  typeof props.size === 'number' ? `${props.size}px` : props.size,
)
</script>

<template>
  <!--
    图标是构建期从本地文件读进来的静态 SVG，不是用户输入，不存在注入面。
    （需求文档对 XSS 的要求针对的是"用户内容高亮"，见 RiskFinding 的渲染。）
  -->
  <span
    v-if="svg"
    class="inline-flex shrink-0 items-center justify-center leading-none"
    :style="{ width: sizeStyle, height: sizeStyle }"
    v-html="svg"
  />
  <!-- 找不到时留一个等宽占位，而不是塌陷导致布局跳动 -->
  <span
    v-else
    class="inline-flex shrink-0 items-center justify-center rounded bg-warm-sidebar text-[10px] text-wood-muted/50"
    :style="{ width: sizeStyle, height: sizeStyle }"
    :title="`缺少图标 ${name}`"
  >
    ?
  </span>
</template>

<style scoped>
/* 让内联 SVG 跟随文字颜色和尺寸 */
:deep(svg) {
  width: 100%;
  height: 100%;
  fill: currentColor;
  display: block;
}
</style>
