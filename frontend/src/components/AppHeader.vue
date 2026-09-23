<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import AppIcon from './AppIcon.vue'
import { useInfoPanel } from '@/composables/useInfoPanel'
import { useTaskStore } from '@/stores/task'

/**
 * 顶栏：搜索 / 新建 / 通知 / 账号。
 *
 * ⚠️ 设计稿里那个搜索框**没有实现成摆设**。
 * 一个点了没反应的搜索框是典型的"看起来像产品其实不是"——
 * 这里把它做成真实的 ⌘K 快速跳转：能搜路由、也能跳回历史任务。
 */
const router = useRouter()
const tasks = useTaskStore()
const { show: showInfo } = useInfoPanel()

const paletteOpen = ref(false)
const query = ref('')
const cursor = ref(0)
const inputRef = ref<HTMLInputElement | null>(null)

interface Entry {
  label: string
  hint: string
  icon: string
  run: () => void
}

const routes: Entry[] = [
  { label: '工作台', hint: '总览与最近任务', icon: 'squares-four', run: () => router.push('/') },
  { label: '户型解析', hint: '上传户型图并识别', icon: 'blueprint', run: () => router.push('/parse') },
  { label: '方案生成', hint: '多方案对比与明细', icon: 'sparkle', run: () => router.push('/generate') },
  { label: '知识库管理', hint: 'RAG 语料与检索', icon: 'book-open', run: () => router.push('/knowledge') },
  { label: '数据分析', hint: '调用与性能指标', icon: 'chart-line', run: () => router.push('/analytics') },
  { label: '用户管理', hint: '账号与权限', icon: 'users', run: () => router.push('/users') },
]

const entries = computed<Entry[]>(() => {
  const q = query.value.trim().toLowerCase()
  const recent: Entry[] = tasks.recent.slice(0, 5).map((t) => ({
    label: `${t.kindLabel} · ${t.taskId.slice(-8)}`,
    hint: t.summary || t.phaseText,
    icon: 'clock',
    run: () => router.push(t.route),
  }))
  const all = [...routes, ...recent]
  if (!q) return all
  return all.filter(
    (e) => e.label.toLowerCase().includes(q) || e.hint.toLowerCase().includes(q),
  )
})

watch(entries, () => (cursor.value = 0))

function open() {
  paletteOpen.value = true
  query.value = ''
  cursor.value = 0
  nextTick(() => inputRef.value?.focus())
}

function close() {
  paletteOpen.value = false
}

function choose(entry?: Entry) {
  const picked = entry ?? entries.value[cursor.value]
  if (!picked) return
  picked.run()
  close()
}

function onKeydown(e: KeyboardEvent) {
  // ⌘K / Ctrl+K —— 与设计稿里 kbd 提示的一致
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    paletteOpen.value ? close() : open()
    return
  }
  if (!paletteOpen.value) return

  if (e.key === 'Escape') close()
  else if (e.key === 'ArrowDown') {
    e.preventDefault()
    cursor.value = (cursor.value + 1) % Math.max(entries.value.length, 1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    cursor.value =
      (cursor.value - 1 + Math.max(entries.value.length, 1)) %
      Math.max(entries.value.length, 1)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    choose()
  }
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <header
    class="z-30 flex h-16 flex-shrink-0 items-center justify-between border-b border-warm-border bg-warm-bg/95 px-8 backdrop-blur-md"
  >
    <!-- ── 搜索（真实可用的 ⌘K 面板入口）── -->
    <div class="flex max-w-md flex-1 items-center">
      <button
        class="group relative flex w-full items-center rounded-xl border border-warm-border bg-white py-2 pl-9 pr-16 text-left text-[13px] text-wood-muted/70 shadow-xs transition-all hover:border-wood/30 focus:outline-none focus:ring-2 focus:ring-botanical/20"
        type="button"
        @click="open"
      >
        <AppIcon
          name="magnifying-glass"
          :size="19"
          class="pointer-events-none absolute left-3 text-wood-muted"
        />
        <span>搜索页面、跳回历史任务…</span>
        <span class="absolute right-2.5 flex items-center gap-0.5">
          <kbd
            class="rounded border border-warm-border bg-warm-sidebar px-1.5 py-0.5 text-[10px] font-medium text-wood-muted"
          >
            ⌘
          </kbd>
          <kbd
            class="rounded border border-warm-border bg-warm-sidebar px-1.5 py-0.5 text-[10px] font-medium text-wood-muted"
          >
            K
          </kbd>
        </span>
      </button>
    </div>

    <!-- ── 右侧动作 ── -->
    <div class="flex items-center gap-3">
      <button class="btn-ghost px-3.5 py-2" type="button" @click="router.push('/parse')">
        <AppIcon name="plus" :size="18" class="text-botanical" />
        <span>新建户型</span>
      </button>

      <button
        class="relative rounded-xl border border-transparent p-2 text-wood-muted transition-all hover:border-warm-border hover:bg-white hover:text-wood-dark"
        type="button"
        :title="
          tasks.runningCount
            ? `有 ${tasks.runningCount} 个任务在跑`
            : '当前没有运行中的任务'
        "
        @click="router.push('/analytics')"
      >
        <AppIcon name="bell" :size="20" />
        <span
          v-if="tasks.runningCount"
          class="absolute right-1.5 top-1.5 flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-botanical px-1 text-[9px] font-bold text-white"
        >
          {{ tasks.runningCount }}
        </span>
      </button>

      <!-- 头像下拉 → 打开「系统信息」面板。
           设计稿这里是个人菜单，但 M1 认证是主动跳过的（见 UsersView），
           做不出真的账号菜单。与其摆一个假的"退出登录"，不如让这个
           交互指向一个真有内容的东西。 -->
      <button
        class="flex items-center gap-2 rounded-xl border border-transparent py-1 pl-2 pr-1.5 transition-colors hover:border-warm-border hover:bg-white"
        type="button"
        title="系统信息"
        @click="showInfo()"
      >
        <span
          class="flex h-8 w-8 items-center justify-center rounded-full bg-botanical-light text-[11px] font-bold text-botanical ring-1 ring-warm-border"
        >
          QY
        </span>
        <AppIcon name="caret-down" :size="18" class="text-wood-muted" />
      </button>
    </div>

    <!-- ── ⌘K 面板 ── -->
    <Teleport to="body">
      <div
        v-if="paletteOpen"
        class="fixed inset-0 z-[100] flex items-start justify-center bg-wood-dark/30 p-4 pt-[12vh] backdrop-blur-xs"
        @click.self="close"
      >
        <div class="w-full max-w-lg overflow-hidden rounded-2xl border border-warm-border bg-white shadow-lg">
          <div class="flex items-center gap-2 border-b border-warm-border px-4 py-3">
            <AppIcon name="magnifying-glass" :size="18" class="text-wood-muted" />
            <input
              ref="inputRef"
              v-model="query"
              class="flex-1 bg-transparent text-[14px] text-wood-dark outline-none placeholder:text-wood-muted/60"
              placeholder="输入页面名，或任务编号后 8 位…"
            />
            <kbd
              class="rounded border border-warm-border bg-warm-sidebar px-1.5 py-0.5 text-[10px] text-wood-muted"
            >
              ESC
            </kbd>
          </div>

          <ul class="max-h-80 overflow-y-auto scroll-thin p-2">
            <li v-for="(e, i) in entries" :key="e.label + i">
              <button
                class="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left transition-colors"
                :class="i === cursor ? 'bg-botanical-light' : 'hover:bg-warm-sidebar'"
                type="button"
                @mouseenter="cursor = i"
                @click="choose(e)"
              >
                <AppIcon
                  :name="e.icon"
                  :size="18"
                  :class="i === cursor ? 'text-botanical' : 'text-wood-muted'"
                />
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-[13px] font-medium text-wood-dark">
                    {{ e.label }}
                  </span>
                  <span class="block truncate text-[11px] text-wood-muted">{{ e.hint }}</span>
                </span>
                <AppIcon v-if="i === cursor" name="arrow-right" :size="15" class="text-botanical" />
              </button>
            </li>
            <li v-if="!entries.length" class="px-3 py-6 text-center text-[12px] text-wood-muted">
              没有匹配项
            </li>
          </ul>
        </div>
      </div>
    </Teleport>
  </header>
</template>
