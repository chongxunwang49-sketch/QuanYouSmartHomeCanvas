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
 * **真的有内容**的面板。选了后者。
 *
 * ⚠️ 2026-09-27（验收阶段）按需求**清掉了这一页里的工程信息**：
 *    后端地址、构建模式、依赖清单（redis / deepseek / material_catalog 这些
 *    内部依赖名与后端原始说明）、X-Trace-Id 的链路解释、设计令牌色值。
 *    留下的只有"这次会话做了些什么""素材什么版权"——那是给用户看的。
 */
const { open, hide } = useInfoPanel()
const health = useHealthStore()
const tasks = useTaskStore()

const statusText = computed(() => {
  if (health.status === 'ok') return '全部正常'
  if (health.status === 'degraded') return '部分功能受限'
  return '未连接'
})

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
              <p class="text-[11px] leading-relaxed text-wood-muted">
                服务连接{{ health.status === 'ok' ? '正常' : '异常' }}。
                账号与数据均为虚构样本，仅供演示。
              </p>
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
                    { label: '未完整完成', value: sessionStats.degraded },
                  ]"
                  :key="s.label"
                  class="rounded-xl border border-warm-border bg-warm-sidebar/40 p-2.5 text-center"
                >
                  <p class="num text-[18px] font-bold text-wood-dark">{{ s.value }}</p>
                  <p class="text-[10px] text-wood-muted">{{ s.label }}</p>
                </div>
              </div>
            </section>

            <!-- 设计稿与素材 -->
            <section>
              <h4 class="mb-2 flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                <AppIcon name="palette" :size="14" class="text-botanical" />
                <span>素材授权</span>
              </h4>
              <div class="rounded-xl border border-warm-border bg-warm-sidebar/40 p-3 text-[11px] leading-relaxed">
                <p class="text-wood-muted">
                  图标 Iconify（MIT）· 插画 unDraw（开放许可，已按项目色板重上色）·
                  照片 Pexels（免费商用）· 实景图来自全友官网（版权归全友家居所有，仅作演示）。
                </p>
              </div>
            </section>
          </div>

          <div
            class="flex flex-shrink-0 items-center justify-between border-t border-warm-grid bg-warm-sidebar/60 px-6 py-3 text-[11px] text-wood-muted"
          >
            <!--
              ⚠️ 这里原来写的是「演示环境 · M1 认证未实现」—— **它现在在说谎。**
              AC-01 的三角色登录、RBAC、按角色/档位的功能可见性、
              用户管理都已经上线（2026-09-24）。一行不改的后果是：
              面试官打开这个面板，看到的第一条就是"认证没做"，
              而屏幕左上角正显示着登录进来的那个账号。
              换成本仓库始终成立的两条事实（数据是虚构的、服务只绑本机）。
            -->
            <span>演示环境 · 账号与数据均为虚构样本，服务仅监听 127.0.0.1</span>
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
