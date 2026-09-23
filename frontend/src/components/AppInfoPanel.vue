<script setup lang="ts">
import { computed } from 'vue'

import AppIcon from './AppIcon.vue'
import { useInfoPanel } from '@/composables/useInfoPanel'
import { useHealthStore } from '@/stores/health'
import { useTaskStore } from '@/stores/task'

/**
 * 系统信息面板。
 *
 * ══════════════════════════════════════════════════════════════════
 * 这个面板存在的理由
 * ══════════════════════════════════════════════════════════════════
 * 顶栏的头像和侧栏的用户卡本来都是**点了没反应**的装饰——
 * 那是最坏的一种 UI：它承诺了一个菜单，然后什么都不给。
 *
 * 有两条路：接一个假的"个人设置/退出登录"菜单，或者让它们打开一个
 * **真的有内容**的面板。选了后者——这一页放的全是演示时真会被问到的东西：
 * 后端连的哪、依赖怎么样、这次会话做了些什么、设计稿在哪、素材什么版权。
 *
 * 演示时有人问"你这个后端跑在哪"，直接点这里，比翻代码快。
 */
const { open, hide } = useInfoPanel()
const health = useHealthStore()
const tasks = useTaskStore()

const checks = computed(() => Object.entries(health.checks))

const statusText = computed(() => {
  if (health.status === 'ok') return '全部正常'
  if (health.status === 'degraded') return '降级运行'
  return '未连接'
})

const degradedDeps = computed(() => checks.value.filter(([, c]) => !c.ok).map(([k]) => k))

/** 前端拿到的后端地址。开发期是 /api 反代，所以显示代理目标。 */
const backend = computed(() => import.meta.env.VITE_BACKEND || 'http://127.0.0.1:8000（dev 代理）')

const buildMode = import.meta.env.PROD ? 'production' : 'development'

const sessionStats = computed(() => ({
  total: tasks.entries.length,
  running: tasks.runningCount,
  degraded: tasks.entries.filter((t) => t.degraded).length,
}))
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div
        v-if="open"
        class="fixed inset-0 z-[110] flex items-center justify-center bg-wood-dark/35 p-4 backdrop-blur-xs"
        @click.self="hide"
      >
        <div
          class="flex max-h-[85vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-warm-border bg-white shadow-lg"
        >
          <!-- 头部 -->
          <div
            class="flex flex-shrink-0 items-center justify-between border-b border-warm-border bg-warm-sidebar p-4 px-6"
          >
            <div class="flex items-center gap-3">
              <span
                class="flex h-9 w-9 items-center justify-center rounded-xl border border-botanical/20 bg-botanical-light text-botanical"
              >
                <AppIcon name="leaf" :size="20" />
              </span>
              <div>
                <h3 class="font-serif text-[17px] font-bold text-wood-dark">系统信息</h3>
                <p class="text-[11px] text-wood-muted">全友·智绘家 · Nature Edition</p>
              </div>
            </div>
            <button
              class="rounded-lg p-1.5 text-wood-muted transition-colors hover:bg-white hover:text-wood-dark"
              type="button"
              aria-label="关闭"
              @click="hide"
            >
              <AppIcon name="x" :size="20" />
            </button>
          </div>

          <div class="flex flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-5">
            <!-- 连接 -->
            <section>
              <h4 class="mb-2 flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                <AppIcon name="share-network" :size="14" class="text-botanical" />
                <span>连接</span>
                <span
                  class="ml-auto rounded-full border px-2 py-0.5 text-[10px] font-semibold"
                  :class="
                    health.status === 'ok'
                      ? 'border-botanical/20 bg-botanical-light text-botanical'
                      : health.status === 'degraded'
                        ? 'border-accent-gold/30 bg-wood-light text-accent-gold'
                        : 'border-accent-red/25 bg-accent-red/5 text-accent-red'
                  "
                >
                  {{ statusText }}
                </span>
              </h4>
              <dl class="rounded-xl border border-warm-border bg-warm-sidebar/40 p-3 text-[11px]">
                <div class="flex justify-between gap-3 py-1">
                  <dt class="text-wood-muted">后端地址</dt>
                  <dd class="truncate font-mono text-wood-dark">{{ backend }}</dd>
                </div>
                <div class="flex justify-between gap-3 py-1">
                  <dt class="text-wood-muted">后端版本</dt>
                  <dd class="font-mono text-wood-dark">{{ health.version || '—' }}</dd>
                </div>
                <div class="flex justify-between gap-3 py-1">
                  <dt class="text-wood-muted">前端构建</dt>
                  <dd class="font-mono text-wood-dark">{{ buildMode }}</dd>
                </div>
              </dl>
            </section>

            <!-- 依赖 -->
            <section>
              <h4 class="mb-2 flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                <AppIcon name="cube" :size="14" class="text-botanical" />
                <span>依赖</span>
                <span v-if="degradedDeps.length" class="text-[10px] font-normal text-accent-gold">
                  （{{ degradedDeps.join('、') }} 不可用）
                </span>
              </h4>
              <ul class="divide-y divide-warm-grid overflow-hidden rounded-xl border border-warm-border">
                <li
                  v-for="[key, c] in checks"
                  :key="key"
                  class="flex items-start gap-2.5 px-3 py-2"
                >
                  <AppIcon
                    :name="c.ok ? 'check-circle' : 'warning-circle'"
                    :size="14"
                    class="mt-0.5 shrink-0"
                    :class="c.ok ? 'text-botanical' : 'text-accent-gold'"
                  />
                  <span class="min-w-0 flex-1">
                    <span class="block font-mono text-[11px] font-semibold text-wood-dark">
                      {{ key }}
                    </span>
                    <span class="block text-[10px] leading-relaxed text-wood-muted">
                      {{ c.detail }}
                    </span>
                  </span>
                </li>
              </ul>
            </section>

            <!-- 本次会话 -->
            <section>
              <h4 class="mb-2 flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                <AppIcon name="clock" :size="14" class="text-botanical" />
                <span>本次会话</span>
              </h4>
              <div class="grid grid-cols-3 gap-2">
                <div
                  v-for="s in [
                    { label: '提交任务', value: sessionStats.total },
                    { label: '进行中', value: sessionStats.running },
                    { label: '降级完成', value: sessionStats.degraded },
                  ]"
                  :key="s.label"
                  class="rounded-xl border border-warm-border bg-warm-sidebar/40 p-2.5 text-center"
                >
                  <p class="num text-[18px] font-bold text-wood-dark">{{ s.value }}</p>
                  <p class="text-[10px] text-wood-muted">{{ s.label }}</p>
                </div>
              </div>
              <p class="mt-2 text-[10px] leading-relaxed text-wood-muted">
                每个请求都带一个 <code class="rounded bg-warm-sidebar px-1 font-mono">X-Trace-Id</code>，
                贯穿 API → Agent → LLM → 审计日志。失败时界面会把 trace_id 一并显示出来。
              </p>
            </section>

            <!-- 设计稿与素材 -->
            <section>
              <h4 class="mb-2 flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                <AppIcon name="palette" :size="14" class="text-botanical" />
                <span>设计稿与素材</span>
              </h4>
              <div class="rounded-xl border border-warm-border bg-warm-sidebar/40 p-3 text-[11px] leading-relaxed">
                <p class="text-wood">
                  设计系统：<span class="font-semibold">Botanical Warmth &amp; Natural Living</span>
                </p>
                <p class="mt-0.5 text-wood-muted">
                  主色叶绿 <code class="font-mono">#4A7C59</code> · 栗棕
                  <code class="font-mono">#6B4F3A</code> · 画布
                  <code class="font-mono">#FAF8F3</code>；标题衬线 Newsreader，
                  正文 Manrope；禁纯黑、禁霓虹。
                </p>
                <hr class="my-2 border-warm-border" />
                <p class="text-wood-muted">
                  素材：图标 Iconify（MIT）· 插画 unDraw（开放许可，**已按项目色板重上色**）·
                  照片 Pexels（免费商用）· 实景图来自全友官网（版权归全友家居所有，仅作演示）。
                </p>
              </div>
            </section>
          </div>

          <div
            class="flex flex-shrink-0 items-center justify-between border-t border-warm-grid bg-warm-sidebar/60 px-6 py-3 text-[11px] text-wood-muted"
          >
            <span>演示环境 · M1 认证未实现</span>
            <button
              class="rounded-lg border border-warm-border bg-white px-3 py-1.5 text-wood transition-colors hover:bg-warm-sidebar"
              type="button"
              @click="hide"
            >
              关闭
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
