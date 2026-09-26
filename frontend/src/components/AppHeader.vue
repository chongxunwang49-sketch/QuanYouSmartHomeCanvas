<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import AppIcon from './AppIcon.vue'
import { flattenNavForRole } from '@/config/nav'
import { useInfoPanel } from '@/composables/useInfoPanel'
import { useAuthStore } from '@/stores/auth'
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
const auth = useAuthStore()
const { show: showInfo } = useInfoPanel()

const paletteOpen = ref(false)
const query = ref('')
const cursor = ref(0)
const inputRef = ref<HTMLInputElement | null>(null)

// ── 账号菜单 ────────────────────────────────────────────
const menuOpen = ref(false)
const accountRef = ref<HTMLElement | null>(null)

function go(path: string) {
  menuOpen.value = false
  router.push(path)
}

/**
 * 打开「系统信息」面板。
 *
 * ⚠️ 这一项原来写的是 `@click="go('/')"` —— 点了**什么都不发生**
 * （本来就在工作台，`push('/')` 等于原地不动），而它旁边还写着
 * 「连接与依赖」四个字。这正是 AppSidebar 那次踩过的同一个坑：
 * **承诺了交互却不给**，比不画这个入口更糟。
 * 现在它真的打开那个面板（与侧栏用户卡同一个），并先收起菜单。
 */
function openSystemInfo() {
  menuOpen.value = false
  showInfo()
}

async function logout() {
  menuOpen.value = false
  auth.logout()
  await router.replace({ name: 'login' })
}

/**
 * 点外面关掉菜单（`mousedown` 而不是 `click`）。
 *
 * 用 `mousedown` 是因为 `click` 在"按下按钮 → 拖动到外面 → 松手"时
 * **不触发**在按钮上，于是菜单不会关；而 `mousedown` 只要按下就在外面了。
 * 这个差别在手快的时候很明显。
 */
function onDocMouseDown(e: MouseEvent) {
  if (!menuOpen.value) return
  if (!accountRef.value?.contains(e.target as Node)) menuOpen.value = false
}

// 路由一变就关。否则"点用户管理跳过去了、菜单还挂在右上角"
watch(() => router.currentRoute.value.fullPath, () => (menuOpen.value = false))

interface Entry {
  label: string
  hint: string
  icon: string
  run: () => void
}

/**
 * ⚠️ **这份菜单表从 `@/config/nav` 现推，不再自己维护一份。**
 *
 * 之前这里和 `AppSidebar.vue` 各写了一份（字段还不一样：这份的 `run`
 * 是闭包，侧栏那份是路径字符串）。6 个平级项时靠"改完记得改另一边"
 * 还能撑，加了子列表之后必定漏 —— 表现是"侧栏里有这个页面，⌘K 搜不到"，
 * 而用户只会觉得是 bug。
 *
 * `flattenNavForRole()` 展开顶层 + 全部子项**并按当前角色过滤** ——
 * 不过滤的话 ⌘K 会搜出一个守卫不让进的页面（`/knowledge`），
 * 点进去被弹回来，用户只会觉得是 bug。
 */
const routes = computed<Entry[]>(() =>
  flattenNavForRole(auth.role).map((leaf) => ({
    label: leaf.label,
    hint: leaf.hint,
    icon: leaf.icon,
    run: () => router.push(leaf.to),
  })),
)

const entries = computed<Entry[]>(() => {
  const q = query.value.trim().toLowerCase()
  const recent: Entry[] = tasks.recent.slice(0, 5).map((t) => ({
    label: `${t.kindLabel} · ${t.taskId.slice(-8)}`,
    hint: t.summary || t.phaseText,
    icon: 'clock',
    run: () => router.push(t.route),
  }))
  const all = [...routes.value, ...recent]
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

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  document.addEventListener('mousedown', onDocMouseDown)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  document.removeEventListener('mousedown', onDocMouseDown)
})
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

      <!-- 头像 → 「我的账号」下拉。
           ⚠️ 这段注释原来是「M1 认证是主动跳过的，做不出真的账号菜单」——
           那个决定已于 2026-09-23 反转（见需求文档 V2.4 修订说明第一节），
           现在这里是真的账号菜单：显示身份、进用户管理、退出登录。 -->
      <div ref="accountRef" class="relative">
        <button
          class="flex items-center gap-2 rounded-xl border border-transparent py-1 pl-2 pr-1.5 transition-colors hover:border-warm-border hover:bg-white"
          type="button"
          :title="auth.displayName || '账号'"
          :aria-expanded="menuOpen"
          @click="menuOpen = !menuOpen"
        >
          <span
            class="flex h-8 w-8 items-center justify-center rounded-full bg-botanical-light text-[12px] font-bold text-botanical ring-1 ring-warm-border"
          >
            {{ auth.user?.avatar_text || 'QY' }}
          </span>
          <AppIcon name="caret-down" :size="18" class="text-wood-muted" />
        </button>

        <div
          v-if="menuOpen"
          class="absolute right-0 top-[calc(100%+6px)] z-50 w-60 overflow-hidden rounded-xl border border-warm-border bg-white shadow-lg"
        >
          <div class="border-b border-warm-border bg-warm-sidebar/60 px-3.5 py-3">
            <div class="truncate text-[13px] font-semibold text-wood-dark">
              {{ auth.displayName }}
            </div>
            <div class="mt-0.5 truncate text-[11px] text-wood-muted">
              {{ auth.user?.title }} · {{ auth.user?.role_label }}
              <template v-if="auth.user && !auth.isUnlimited">
                · {{ auth.membership === 'paid' ? '会员' : '免费版' }}
              </template>
              <template v-else> · 不限量</template>
            </div>
          </div>

          <div class="p-1.5">
            <!--
              ⚠️ 菜单里这两项的可见性与侧栏一致（`config/nav.ts` 是唯一真源）：
              **账号管理只有管理员有，个人中心人人都有**。
              这里省略 nav 的过滤逻辑直接写 `auth.isAdmin` 是可以的 ——
              但两项的 label/icon 必须与 nav.ts 对齐，否则同一个页面在
              侧栏叫一个名字、在头像菜单里叫另一个名字。
            -->
            <button
              v-if="auth.isAdmin"
              class="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[12px] font-medium text-wood-dark transition-colors hover:bg-botanical-light/60"
              type="button"
              @click="go('/admin/users')"
            >
              <AppIcon name="users" :size="16" class="text-wood-muted" />
              账号管理
              <span class="ml-auto text-[10px] text-wood-muted/70">仅管理员</span>
            </button>
            <button
              class="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[12px] font-medium text-wood-dark transition-colors hover:bg-botanical-light/60"
              type="button"
              @click="go('/me')"
            >
              <AppIcon name="user-circle" :size="16" class="text-wood-muted" />
              个人中心
              <span class="ml-auto text-[10px] text-wood-muted/70">我的套餐与设备</span>
            </button>
            <button
              class="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[12px] font-medium text-wood-dark transition-colors hover:bg-botanical-light/60"
              type="button"
              @click="openSystemInfo"
            >
              <AppIcon name="info" :size="16" class="text-wood-muted" />
              系统信息
              <span class="ml-auto text-[10px] text-wood-muted/70">连接与依赖</span>
            </button>
            <div class="my-1.5 h-px bg-warm-border" />
            <button
              class="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[12px] font-medium text-accent-red transition-colors hover:bg-accent-red/5"
              type="button"
              @click="logout"
            >
              <AppIcon name="arrow-left" :size="16" />
              退出登录
            </button>
          </div>
        </div>
      </div>
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
