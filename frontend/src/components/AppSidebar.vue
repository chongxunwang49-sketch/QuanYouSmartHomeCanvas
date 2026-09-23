<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import AppIcon from './AppIcon.vue'
import { useInfoPanel } from '@/composables/useInfoPanel'
import { useHealthStore } from '@/stores/health'

/**
 * 左侧主导航。240px 固定宽。
 *
 * 结构完全照 `ui参考/stitch_ai` 的稿子：品牌区 h-16 → 分组标题 → 导航项
 * → 底部用户卡。导航项**选中态是叶绿浅底 + 1.5px 圆点**，不是常见的左侧
 * 竖条 —— 竖条在这套圆角语言里显得硬。
 */
const route = useRoute()
const health = useHealthStore()
const { show: showInfo } = useInfoPanel()

const NAV = [
  { to: '/', icon: 'squares-four', label: '工作台' },
  { to: '/parse', icon: 'blueprint', label: '户型解析' },
  { to: '/generate', icon: 'sparkle', label: '方案生成' },
  { to: '/knowledge', icon: 'book-open', label: '知识库管理' },
  { to: '/analytics', icon: 'chart-line', label: '数据分析' },
  { to: '/users', icon: 'users', label: '用户管理' },
] as const

const isActive = (to: string) =>
  to === '/' ? route.path === '/' : route.path.startsWith(to)

/** 导航项图标在 hover 时染成叶绿 —— 稿子里悬停的颜色变化就是这一处 */
const iconTone = (to: string) =>
  isActive(to) ? 'text-botanical' : 'text-wood-muted group-hover:text-botanical'

const degradedDeps = computed(() =>
  Object.entries(health.checks).filter(([, c]) => !c.ok),
)
</script>

<template>
  <aside
    class="z-40 flex h-full w-60 flex-shrink-0 flex-col justify-between border-r border-warm-border bg-warm-sidebar"
  >
    <div class="flex flex-col">
      <!-- ── 品牌区 ── -->
      <div class="flex h-16 items-center gap-3 border-b border-warm-border bg-warm-sidebar px-4">
        <div
          class="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl border border-botanical/20 bg-botanical-light text-botanical"
        >
          <AppIcon name="leaf" :size="22" />
        </div>
        <div class="flex min-w-0 flex-col">
          <span class="truncate text-[15px] font-bold tracking-tight text-wood">全友·智绘家</span>
          <div class="flex items-center gap-1.5">
            <span
              class="rounded bg-botanical-light px-1 text-[10px] font-semibold uppercase tracking-wider text-botanical"
            >
              Nature Edition
            </span>
            <span class="text-[10px] text-wood-muted">v2.4</span>
          </div>
        </div>
      </div>

      <div class="px-4 pb-2 pt-4">
        <span class="text-[11px] font-semibold uppercase tracking-wider text-wood-muted/70">
          核心工作台
        </span>
      </div>

      <!-- ── 导航 ── -->
      <nav class="flex flex-col gap-1 px-3">
        <RouterLink
          v-for="item in NAV"
          :key="item.to"
          :to="item.to"
          :class="isActive(item.to) ? 'nav-item-active' : 'nav-item group'"
        >
          <AppIcon :name="item.icon" :size="19" :class="iconTone(item.to)" />
          <span class="flex-1">{{ item.label }}</span>
          <!-- 选中态的小圆点。稿子里就是这么做的，比竖条柔和 -->
          <span v-if="isActive(item.to)" class="h-1.5 w-1.5 rounded-full bg-botanical" />
        </RouterLink>
      </nav>

      <!-- ── 依赖健康：后端哪个依赖挂了在这里如实说 ── -->
      <div v-if="health.loaded" class="mt-3 px-3">
        <div
          class="rounded-xl border px-3 py-2 text-[11px] leading-relaxed"
          :class="
            degradedDeps.length
              ? 'border-accent-gold/30 bg-wood-light/60 text-wood'
              : 'border-botanical/20 bg-botanical-light/60 text-botanical'
          "
        >
          <div class="flex items-center gap-1.5 font-semibold">
            <AppIcon :name="degradedDeps.length ? 'warning-circle' : 'check-circle'" :size="13" />
            <span>{{ degradedDeps.length ? '部分依赖降级' : '依赖全部正常' }}</span>
          </div>
          <ul v-if="degradedDeps.length" class="mt-1 space-y-0.5 text-wood-muted">
            <li v-for="[key, c] in degradedDeps" :key="key" class="truncate" :title="c.detail">
              {{ key }}：{{ c.detail }}
            </li>
          </ul>
        </div>
      </div>
    </div>

    <!-- ── 底部用户卡 ──
         整张卡是一个按钮：打开「系统信息」面板。
         之前这里是个纯装饰的 div —— 有 hover 效果、有齿轮图标，
         点了什么都不发生。那种"承诺了交互却不给"比不画齿轮更糟。 -->
    <div class="border-t border-warm-border/80 p-3">
      <button
        class="flex w-full items-center justify-between rounded-xl border border-warm-border bg-white/70 p-2.5 text-left shadow-xs transition-colors hover:border-botanical/40 hover:bg-white"
        type="button"
        title="查看系统信息（后端连接、依赖状态、设计稿出处）"
        @click="showInfo()"
      >
        <div class="flex min-w-0 items-center gap-2.5">
          <div class="relative flex-shrink-0">
            <div
              class="flex h-8 w-8 items-center justify-center rounded-full border border-botanical/20 bg-botanical-light text-[12px] font-bold text-botanical"
            >
              QY
            </div>
            <span
              class="absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-botanical ring-2 ring-white"
            />
          </div>
          <div class="flex min-w-0 flex-col">
            <span class="truncate text-[12px] font-semibold text-wood-dark">演示账号</span>
            <span class="truncate text-[10px] text-wood-muted">Demo Session</span>
          </div>
        </div>
        <AppIcon name="caret-down" :size="16" class="text-wood-muted" />
      </button>
    </div>
  </aside>
</template>
