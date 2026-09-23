<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'

import AppIcon from './AppIcon.vue'

/**
 * 图片放大查看（灯箱）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么做成通用组件而不是写在工作台里
 * ══════════════════════════════════════════════════════════════════
 * 工作台上有**三处**要放大：右侧主图、下面两排滚动画廊。
 * 三处各写一遍的结果一定是键盘行为不一致（A 处能按 Esc，B 处不能），
 * 而这恰恰是用户最容易察觉的粗糙。
 *
 * 所以这里只认两个东西：**一个图片数组** + **当前下标**（-1 表示关闭）。
 * 调用方决定从哪张开始放大、放大的是哪一批。
 *
 * ══════════════════════════════════════════════════════════════════
 * 键盘与可访问性
 * ══════════════════════════════════════════════════════════════════
 *   Esc      关闭
 *   ← / →    上一张 / 下一张
 *
 * ⚠️ 监听挂在 `window` 上而不是容器上 —— 灯箱打开时焦点可能在任何地方
 * （用户是点了图进来的），挂容器上会出现"按 Esc 没反应"。
 * 卸载时**必须摘掉**，否则每开一次就多一个监听器。
 */
const props = defineProps<{
  /** 可翻看的图片集合 */
  images: string[]
  /** 当前下标。-1（或越界）表示关闭。 */
  index: number
  alt?: string
  /** 底部图注。版权标注这类必须跟着图一起出现。 */
  caption?: string
}>()

const emit = defineEmits<{ close: []; 'update:index': [number] }>()

const open = computed(
  () => props.index >= 0 && props.index < props.images.length && props.images.length > 0,
)
const current = computed(() => (open.value ? props.images[props.index] : ''))
const canFlip = computed(() => props.images.length > 1)

/** 翻页是**环绕**的：最后一张按右键回到第一张。 */
function step(delta: number) {
  if (!canFlip.value) return
  const n = props.images.length
  emit('update:index', (props.index + delta + n) % n)
}

function onKey(e: KeyboardEvent) {
  if (!open.value) return
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('close')
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault()
    step(-1)
  } else if (e.key === 'ArrowRight') {
    e.preventDefault()
    step(1)
  }
}

onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div
        v-if="open"
        class="fixed inset-0 z-[120] flex flex-col items-center justify-center bg-wood-dark/80 p-4 backdrop-blur-sm"
        role="dialog"
        aria-modal="true"
        @click.self="emit('close')"
      >
        <!-- 关闭 -->
        <button
          class="absolute right-4 top-4 flex h-10 w-10 items-center justify-center rounded-full border border-white/20 bg-white/10 text-white transition-colors hover:bg-white/25"
          type="button"
          aria-label="关闭（Esc）"
          @click="emit('close')"
        >
          <AppIcon name="x" :size="20" />
        </button>

        <!-- 计数 -->
        <span
          v-if="canFlip"
          class="absolute left-1/2 top-5 -translate-x-1/2 rounded-full border border-white/20 bg-white/10 px-3 py-1 font-mono text-[11px] text-white/90"
        >
          {{ index + 1 }} / {{ images.length }}
        </span>

        <!-- 上一张 / 下一张。只有一张时不显示 —— 摆两个点了没反应的箭头
             比不摆更糟（本项目 AppSidebar 的注释里记着同一条教训）。 -->
        <button
          v-if="canFlip"
          class="absolute left-3 top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full border border-white/20 bg-white/10 text-white transition-colors hover:bg-white/25"
          type="button"
          aria-label="上一张（←）"
          @click.stop="step(-1)"
        >
          <AppIcon name="arrow-left" :size="20" />
        </button>
        <button
          v-if="canFlip"
          class="absolute right-3 top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full border border-white/20 bg-white/10 text-white transition-colors hover:bg-white/25"
          type="button"
          aria-label="下一张（→）"
          @click.stop="step(1)"
        >
          <AppIcon name="arrow-right" :size="20" />
        </button>

        <!-- 图片本身。object-contain + 视口上限：竖图横图都完整可见，不裁切 -->
        <img
          :src="current"
          :alt="alt || '查看大图'"
          class="max-h-[82vh] max-w-[92vw] rounded-xl border border-white/15 object-contain shadow-lg"
          @click.stop
        />

        <p
          v-if="caption"
          class="mt-3 max-w-[80vw] text-center text-[11px] leading-relaxed text-white/70"
        >
          {{ caption }}
        </p>
        <p v-if="canFlip" class="mt-1 text-[10px] text-white/50">
          ← → 翻看 · Esc 关闭
        </p>
      </div>
    </Transition>
  </Teleport>
</template>
