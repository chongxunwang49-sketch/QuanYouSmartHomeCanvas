<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import AppIcon from './AppIcon.vue'
import { isNavActive, navForRole } from '@/config/nav'
import { useInfoPanel } from '@/composables/useInfoPanel'
import { useAuthStore } from '@/stores/auth'
import { useHealthStore } from '@/stores/health'

/**
 * 左侧主导航。240px 固定宽。
 *
 * 结构完全照 `ui参考/stitch_ai` 的稿子：品牌区 h-16 → 分组标题 → 导航项
 * → 底部用户卡。导航项**选中态是叶绿浅底 + 1.5px 圆点**，不是常见的左侧
 * 竖条 —— 竖条在这套圆角语言里显得硬。
 *
 * 菜单数据来自 `@/config/nav`（唯一真源，⌘K 面板读同一份），
 * 并按当前用户的**角色**过滤（`navForRole`）。
 */
const route = useRoute()
const router = useRouter()
const health = useHealthStore()
const auth = useAuthStore()
const { show: showInfo } = useInfoPanel()

/**
 * 当前角色可见的菜单。
 *
 * ⚠️ `auth.role` 在登录前是 `null`，`navForRole(null)` 返回**空数组** ——
 * 见那个函数的说明：宁可先空着，也不要先渲染出管理员菜单再收回去。
 */
const navItems = computed(() => navForRole(auth.role))

/**
 * 展开的父项集合。
 *
 * ⚠️ 用「当前路径自动展开」而不是「记住用户上次的展开状态」：
 * 侧栏是导航，用户从 ⌘K、从工作台卡片、从解析结果的「下一步」跳进
 * `/parse/drawing` 时，**必须**看到自己在哪个模块的哪一页。
 * 如果只能靠手点展开，那些入口进来的人会看到一组全部折叠的菜单，
 * 而当前页的入口藏在里面 —— 导航失去意义。
 *
 * 手动折叠仍然允许（点箭头），但下一次路由变化会把它再展开回来。
 */
const expanded = ref<Set<string>>(new Set())

function toggle(to: string) {
  const next = new Set(expanded.value)
  if (next.has(to)) next.delete(to)
  else next.add(to)
  expanded.value = next
}

const isExpanded = (to: string) => expanded.value.has(to)

watch(
  () => [route.path, navItems.value] as const,
  ([path]) => {
    const next = new Set(expanded.value)
    let changed = false
    for (const item of navItems.value) {
      if (item.children && isNavActive(path, item.to, false) && !next.has(item.to)) {
        next.add(item.to)
        changed = true
      }
    }
    if (changed) expanded.value = next
  },
  { immediate: true },
)

/** 角色 / 档位的展示标签。管理员与设计师不必显示档位（对他们无意义） */
const roleChip = computed(() => auth.user?.role_label ?? '')
const membershipChip = computed(() =>
  auth.user && !auth.isUnlimited ? (auth.membership === 'paid' ? '会员' : '免费版') : '',
)

async function logout() {
  auth.logout()
  await router.replace({ name: 'login' })
}

/**
 * 一级判定用 `startsWith`（在 `/parse/overview` 时「户型解析」也要亮）；
 * 二级用精确匹配 —— 详见 `@/config/nav` 文件头。
 */
const topActive = (to: string) => isNavActive(route.path, to, false)
const subActive = (to: string) => isNavActive(route.path, to, true)

/** 导航项图标在 hover 时染成叶绿 —— 稿子里悬停的颜色变化就是这一处 */
const iconTone = (to: string) =>
  topActive(to) ? 'text-botanical' : 'text-wood-muted group-hover:text-botanical'

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
        <template v-for="item in navItems" :key="item.to">
          <!--
            一级项整体套一个带 padding 的容器（.nav-item 自带 px-3 py-2），
            里面再放路由链接 —— 这样右侧的展开箭头能落在同一块高亮底上。
            直接把 RouterLink 当容器的话，箭头就得嵌在 <a> 里，
            点箭头会同时触发跳转。
          -->
          <div :class="[topActive(item.to) ? 'nav-item-active' : 'nav-item group', '!gap-2']">
            <RouterLink :to="item.to" class="flex min-w-0 flex-1 items-center gap-3">
              <AppIcon :name="item.icon" :size="19" :class="iconTone(item.to)" />
              <span class="flex-1 truncate">{{ item.label }}</span>
              <!-- 选中态的小圆点。稿子里就是这么做的，比竖条柔和 -->
              <span v-if="topActive(item.to)" class="h-1.5 w-1.5 rounded-full bg-botanical" />
            </RouterLink>

            <!--
              ⚠️ 箭头只能用 caret-down + 旋转。图标集里**没有** caret-right /
              chevron，写 name="caret-right" 会静默渲染成 "?" 占位方块
              （见 AppIcon 的兜底逻辑）—— 不报错，只是难看。
            -->
            <button
              v-if="item.children?.length"
              class="-mr-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-wood-muted/70 transition-colors hover:bg-white/70 hover:text-wood-dark"
              type="button"
              :title="isExpanded(item.to) ? '收起子菜单' : '展开子菜单'"
              :aria-expanded="isExpanded(item.to)"
              @click="toggle(item.to)"
            >
              <AppIcon
                name="caret-down"
                :size="13"
                class="transition-transform duration-200"
                :class="isExpanded(item.to) ? '' : '-rotate-90'"
              />
            </button>
          </div>

          <!-- 二级列表 -->
          <div v-if="item.children?.length && isExpanded(item.to)" class="flex flex-col gap-0.5">
            <RouterLink
              v-for="child in item.children"
              :key="child.to"
              :to="child.to"
              :class="subActive(child.to) ? 'nav-subitem-active' : 'nav-subitem'"
            >
              <span class="flex-1 truncate">{{ child.label }}</span>
            </RouterLink>
          </div>
        </template>
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
         这里原来是写死的「演示账号 / Demo Session」占位。现在读真实登录用户，
         并且把登出做成一个**独立可见的按钮** —— 藏进信息面板里的登出
         在演示时很难找，而对面试官展示"换个账号登录"正是要看的一件事。 -->
    <div class="border-t border-warm-border/80 p-3">
      <div class="flex items-center gap-2">
        <button
          class="flex min-w-0 flex-1 items-center gap-2.5 rounded-xl border border-warm-border bg-white/70 p-2.5 text-left shadow-xs transition-colors hover:border-botanical/40 hover:bg-white"
          type="button"
          title="查看系统信息（后端连接、依赖状态、设计稿出处）"
          @click="showInfo()"
        >
          <div class="relative flex-shrink-0">
            <!-- 头像文字由后端给（`avatar_text`），前端不自己截用户名 ——
                 "admin" 截两个字是 "ad"，不如后端配的"管" -->
            <div
              class="flex h-8 w-8 items-center justify-center rounded-full border border-botanical/20 bg-botanical-light text-[13px] font-bold text-botanical"
            >
              {{ auth.user?.avatar_text || '—' }}
            </div>
            <span
              class="absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-botanical ring-2 ring-white"
            />
          </div>
          <div class="flex min-w-0 flex-col">
            <span class="truncate text-[12px] font-semibold text-wood-dark">
              {{ auth.displayName || '未登录' }}
            </span>
            <span class="flex min-w-0 items-center gap-1">
              <span class="truncate text-[10px] text-wood-muted">
                {{ auth.user?.title || '—' }}
              </span>
            </span>
          </div>
        </button>

        <button
          class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-warm-border bg-white/70 text-wood-muted shadow-xs transition-colors hover:border-accent-red/40 hover:text-accent-red"
          type="button"
          title="退出登录"
          aria-label="退出登录"
          @click="logout"
        >
          <AppIcon name="arrow-left" :size="16" />
        </button>
      </div>

      <!-- 角色 × 档位。**两个维度分开显示**，不要合成一个标签 ——
           合成之后就说不清"会员但仍然受限"这种事 -->
      <div v-if="auth.user" class="mt-2 flex flex-wrap items-center gap-1.5 px-1">
        <span class="chip">{{ roleChip }}</span>
        <span
          v-if="membershipChip"
          class="rounded-full border px-2 py-0.5 text-[11px] font-semibold"
          :class="
            auth.membership === 'paid'
              ? 'border-accent-gold/40 bg-wood-light text-wood'
              : 'border-warm-border bg-warm-sidebar text-wood-muted'
          "
        >
          {{ membershipChip }}
        </span>
        <span v-if="auth.isUnlimited" class="text-[10px] text-wood-muted">不限量</span>
      </div>
    </div>
  </aside>
</template>
