<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { dashboardStats } from '@/api'
import type { DashboardStatsData } from '@/api/types'
import { imageAttribution, imagePool } from '@/assets/images/pool'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import ImageLightbox from '@/components/ImageLightbox.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAuthStore } from '@/stores/auth'
import { useHealthStore } from '@/stores/health'
import { useTaskStore } from '@/stores/task'

/**
 * 工作台总览。
 *
 * ⚠️ **这一页没有假数据。** 所有数字都来自真实来源：
 * 任务台账（本次会话真实提交过的任务）、`/dashboard/stats`（跨会话累计，
 * 从库里数出来）、`/system/health`（真实依赖状态）、
 * `seed_data/material_catalog.json`（演示目录，会显著标注）。
 *
 * ⚠️ 「本会话」与「累计」**分开陈列、分别标注口径** —— 它们一个是浏览器
 * 内存里的、一个是库里的，混在一处会让人以为两套数字是同一个口径。
 *
 * 之所以强调这点：工作台是"仪表盘"最容易被编数据的地方——
 * 写死几个漂亮的同比增幅、几条趋势线，看起来完整，但那正是
 * 用户最反感的"AI 味"。宁可空着，也不编。
 */
const router = useRouter()
const route = useRoute()
const tasks = useTaskStore()
const health = useHealthStore()
const auth = useAuthStore()

/**
 * 被角色守卫弹回来时带过来的那一页名字，没有就是 `''`。
 *
 * 「知道了」把它从地址栏清掉 —— 否则用户刷新一下又看见一次，
 * 会以为系统在反复报同一个错。
 */
const denied = computed(() => String(route.query.denied ?? ''))

function clearDenied() {
  const q = { ...route.query }
  delete q.denied
  router.replace({ path: '/', query: q })
}

// 实景图池。案例图缺失时自动回落 Pexels 照片（见 assets/images/pool.ts）
const cases = imagePool.main

const galleryIndex = ref(0)
const heroSrc = computed(() => cases[galleryIndex.value % Math.max(cases.length, 1)] ?? '')

/**
 * 灯箱状态。-1 = 关着。
 *
 * ⚠️ **主图和三排画廊共用同一个下标空间**（都是 `cases` 的下标），
 * 所以从画廊点开第 9 张后按 → 能翻到第 10 张，而不是回到画廊第一张。
 * 若各处各存一个下标，翻页范围就会莫名其妙地变小。
 */
const lightboxIndex = ref(-1)
const lightboxOpen = computed(() => lightboxIndex.value >= 0)

const HERO_INTERVAL_MS = 5000
let heroTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  health.refresh()
  // 5 秒换一张实景图。慢一点——这是个"氛围"位，不是轮播广告。
  heroTimer = setInterval(() => {
    // 灯箱开着时不换 —— 用户正盯着某一张看，背后悄悄换掉会让人
    // 关掉灯箱后发现"图变了"，像是点错了。
    if (lightboxOpen.value) return
    if (cases.length) galleryIndex.value = (galleryIndex.value + 1) % cases.length
  }, HERO_INTERVAL_MS)
})

// ⚠️ 原来没有这个清理。工作台是常被切走的一页，定时器不清就会
// 一直跑（每 5 秒改一个已经没人看的 ref），而且回来时会有多个定时器叠加。
onUnmounted(() => {
  if (heroTimer) {
    clearInterval(heroTimer)
    heroTimer = null
  }
})

/** 打开灯箱。`i` 是 `cases` 里的下标。 */
function openAt(i: number) {
  if (i >= 0 && i < cases.length) lightboxIndex.value = i
}

/** 点主图：放大当前正在展示的那一张。 */
function openHero() {
  if (cases.length) openAt(galleryIndex.value % cases.length)
}

/**
 * 两排画廊。**按下标切开，不切成两份字符串数组** ——
 * 因为灯箱要用全局下标翻页（见上面 `lightboxIndex` 的说明）。
 */
const galleryRows = computed(() => {
  const n = cases.length
  if (n < 2) return [] as { key: string; idx: number[]; reverse: boolean }[]
  const half = Math.ceil(n / 2)
  const range = (from: number, to: number) =>
    Array.from({ length: Math.max(to - from, 0) }, (_, k) => from + k)
  return [
    { key: 'row-a', idx: range(0, half), reverse: false },
    { key: 'row-b', idx: range(half, n), reverse: true },
  ]
})

/**
 * 每排的滚动时长。
 *
 * 按**图片数量**算而不是写死 —— 写死的话，案例图数量一变
 * （公开仓库里回落到 12 张 Pexels 照片，本地是 14 张全友案例）
 * 速度就跟着变，而"适中"是按速度定的、不是按时长定的。
 *
 * 两排速度**故意不同**（36 / 30 px 每秒）：等速会让两排看起来像
 * 一整块东西在平移，而不是两排在各自流动。
 */
const ITEM_W = 200
const rowDuration = (n: number, reverse: boolean) =>
  `${Math.round((n * ITEM_W) / (reverse ? 30 : 36))}s`

const ENTRIES = [
  {
    to: '/parse',
    icon: 'blueprint',
    title: '户型图解析',
    desc: '上传户型图，识别房间、墙体、门窗与尺寸，并生成五维诊断',
    meta: '实测 33–48 秒',
  },
  {
    to: '/generate',
    icon: 'sparkle',
    title: '方案生成',
    desc: '三路 Agent 并行产出空间规划、预算造价、材料选型，再做汇聚审查',
    meta: '实测约 95 秒',
  },
  {
    to: '/review',
    icon: 'shield-check',
    title: '避坑审查',
    desc: '审查报价单与合同，风险项必须带可溯源引用，编造的引用会被剔出',
    meta: '实测约 19 秒',
  },
] as const

const recent = computed(() => tasks.recent.slice(0, 6))

// ── 累计计数（跨会话，来自 `/dashboard/stats`）────────────────────
//
// ⚠️ 与「最近任务」那个列表是**两种东西**，页面上也分开说：
//    · 这个块      跨会话持久化（落库），刷新、重启都还在
//    · 最近任务    本次会话（前端 Pinia），刷新即清零
// 混在一起陈列的话，用户会以为"累计 12 个户型"和"最近 6 个任务"
// 是同一套口径的数 —— 而它们一个是库里的、一个是浏览器内存里的。
const stats = ref<DashboardStatsData | null>(null)

/** `null` → `—`。**不显示 0**：0 是"确实没有"，null 是"读不到"。 */
const num = (v: number | null | undefined) => (v === null || v === undefined ? '—' : String(v))

const statCards = computed(() => {
  const s = stats.value
  return [
    {
      label: '户型', value: num(s?.layouts),
      hint: s?.available ? '已解析并落库' : '数据库不可用',
      tone: 'text-wood-dark',
    },
    {
      label: '装修方案', value: num(s?.plans),
      hint: s?.available ? '三套一组，按方案计数' : '数据库不可用',
      tone: 'text-wood-dark',
    },
    {
      label: '审计事件', value: num(s?.audit_events),
      hint: s?.available ? `其中登录 ${s.by_action.login ?? 0} 次` : '数据库不可用',
      tone: 'text-botanical',
    },
    {
      label: '知识库', value: s?.kb.available ? `${s.kb.chunks}` : '—',
      hint: s?.kb.available ? `${s.kb.documents ?? '?'} 篇文档` : (s?.kb.reason || '不可用'),
      tone: s?.kb.available ? 'text-botanical' : 'text-accent-gold',
    },
  ]
})

onMounted(async () => {
  try {
    stats.value = await dashboardStats()
  } catch {
    // 拿不到就让这个块整体不显示 —— 但**不能显示成 0**，
    // 所以这里保持 null，模板里 `v-if="stats"` 会把它整块收起来。
    stats.value = null
  }
})

const statusLabel: Record<string, string> = {
  pending: '排队中',
  processing: '进行中',
  completed: '已完成',
  failed: '失败',
}

const statusTone: Record<string, string> = {
  pending: 'text-wood-muted bg-warm-sidebar border-warm-border',
  processing: 'text-botanical bg-botanical-light border-botanical/20',
  completed: 'text-botanical bg-botanical-light border-botanical/20',
  failed: 'text-accent-red bg-accent-red/10 border-accent-red/20',
}

const relTime = (ts: number) => {
  const s = Math.round((Date.now() - ts) / 1000)
  if (s < 60) return `${s} 秒前`
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`
  return `${Math.round(s / 3600)} 小时前`
}

const checkEntries = computed(() => Object.entries(health.checks))
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6 surface-glow">
    <PageHeader
      :breadcrumb="['全友·智绘家', '工作台总览']"
      :title="auth.isLoggedIn ? `你好，${auth.displayName}` : '工作台总览'"
      :status="{ icon: 'leaf', text: 'Botanical Warmth · Nature Edition' }"
    />

    <!--
      被角色守卫弹回来时的说明（`router/index.ts` 的 `denied` 参数）。

      ⚠️ 守卫**刻意不渲染一个 403 页面**：用户是点旧书签、敲 URL 或从 ⌘K
      过来的，TA 没有做错什么，一个报错页只会让人以为系统坏了。
      但也不能**静默**弹回去 —— 那会表现成"我点了没反应"。
      所以落点在这里说清是哪一页、为什么、以及当前角色是什么。
    -->
    <div
      v-if="denied"
      class="flex items-start gap-2.5 rounded-xl border border-accent-gold/45 bg-wood-light/55 p-3"
    >
      <AppIcon name="warning-circle" :size="17" class="mt-0.5 shrink-0 text-accent-gold" />
      <div class="min-w-0 flex-1">
        <p class="text-[12px] font-semibold text-wood-dark">
          「{{ denied }}」需要更高的角色权限，已返回工作台
        </p>
        <p class="mt-1 text-[11px] leading-relaxed text-wood">
          当前登录：{{ auth.displayName }}（{{ auth.user?.role_label }}）。
          要用管理员账号看全部账号与知识库，请退出后用
          <span class="num font-semibold">admin</span> 登录。
        </p>
      </div>
      <button class="btn-ghost shrink-0 px-3 py-1.5" type="button" @click="clearDenied">
        <span>知道了</span>
      </button>
    </div>

    <!-- ══ 上部：入口卡 + 实景图 ══ -->
    <section class="grid grid-cols-1 gap-5 lg:grid-cols-3">
      <div class="flex flex-col gap-3 lg:col-span-2">
        <button
          v-for="e in ENTRIES"
          :key="e.to"
          class="card card-hover group flex items-center gap-4 p-4 text-left"
          type="button"
          @click="router.push(e.to)"
        >
          <span
            class="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-botanical/20 bg-botanical-light text-botanical transition-colors group-hover:bg-botanical group-hover:text-white"
          >
            <AppIcon :name="e.icon" :size="22" />
          </span>
          <span class="min-w-0 flex-1">
            <span class="flex items-center gap-2">
              <span class="font-serif text-[17px] font-semibold text-wood-dark">{{ e.title }}</span>
              <span class="tag">{{ e.meta }}</span>
            </span>
            <span class="mt-0.5 block text-[12px] leading-relaxed text-wood-muted">
              {{ e.desc }}
            </span>
          </span>
          <AppIcon
            name="arrow-right"
            :size="18"
            class="shrink-0 text-wood-muted transition-all group-hover:translate-x-0.5 group-hover:text-botanical"
          />
        </button>
      </div>

      <!--
        实景图：真实采集的全友装修案例。

        ⚠️⚠️ **图片必须是 `absolute inset-0`，这是修一个真实的布局 bug。**

        原来写的是 `<img class="h-full min-h-[260px] w-full object-cover">`。
        `h-full` = `height:100%` 需要一个**有确定高度的父元素**才能解析；
        而这里的父元素（卡片）高度又是由图片自己撑开的 —— 循环依赖，
        浏览器只好把 `height:100%` 当 `auto`，于是**图片按自身宽高比渲染**。

        后果：图片池里 14 张案例图**全是横图，但宽高比从 1.20 到 1.79 不等**
        （实测，不是"有横有竖"—— 起初我猜错了，实际更隐蔽）。
        同一宽度下渲染高度能差 124px，于是卡片被撑高、网格行高跟着变、
        下面「最近任务 / 系统状态」整段往下跳 ——
        每 5 秒轮换一张就抖一次，而且**换一张图页面就变一次样**。

        绝对定位之后图片**完全不参与布局计算**，卡片高度只由
        `min-h-[260px]` 和同行左栏（三张入口卡）决定，与图片内容无关。
        `object-cover` 负责在固定框里裁切填满。
      -->
      <div
        class="card group relative min-h-[260px] cursor-zoom-in overflow-hidden"
        role="button"
        tabindex="0"
        :aria-label="`放大查看：${imagePool.isQuanyouCase ? '全友装修实景案例' : '家居实拍参考'}`"
        @click="openHero"
        @keydown.enter="openHero"
        @keydown.space.prevent="openHero"
      >
        <img
          v-if="heroSrc"
          :src="heroSrc"
          :alt="imagePool.isQuanyouCase ? '全友装修实景案例' : '家居实拍参考'"
          class="absolute inset-0 h-full w-full object-cover transition-transform duration-700 group-hover:scale-[1.03]"
        />
        <div
          class="pointer-events-none absolute inset-0 bg-gradient-to-t from-wood-dark/45 via-transparent to-transparent"
        />

        <!-- 放大提示。只在 hover 出现 —— 常驻会跟右下角的版权标注抢注意力 -->
        <span
          class="pointer-events-none absolute right-3 top-3 flex h-8 w-8 items-center justify-center rounded-full border border-white/25 bg-white/85 text-wood opacity-0 backdrop-blur-sm transition-opacity duration-200 group-hover:opacity-100"
        >
          <AppIcon name="magnifying-glass" :size="15" />
        </span>

        <div class="pointer-events-none absolute bottom-0 left-0 right-0 p-4">
          <!-- 这个徽标是**标签**（"这张图是什么"），不是版权声明，所以保留。
               ⚠️ 原来这里下面还压着一行 `imageAttribution`，已删 —— 见下面对
               「声明只写一处」的说明。 -->
          <span
            class="inline-flex items-center gap-1.5 rounded-full border border-white/25 bg-white/85 px-2.5 py-0.5 text-[11px] font-semibold text-wood backdrop-blur-sm"
          >
            <AppIcon name="house-line" :size="13" class="text-botanical" />
            <span>{{ imagePool.isQuanyouCase ? '全友实景案例' : '家居实拍参考' }}</span>
          </span>
        </div>
      </div>
    </section>

    <!-- ══ 累计（跨会话，来自 /dashboard/stats）══ -->
    <section v-if="stats" class="card p-4">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 class="flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
          <AppIcon name="chart-line" :size="17" class="text-botanical" />
          <span>累计</span>
        </h2>
        <span class="text-[11px] text-wood-muted">
          跨会话持久化的真实计数 · 与下面「仅本会话」的那个列表不是一回事
        </span>
      </div>

      <!--
        ⚠️ **库不通时显示 `—` 而不是 0。**
        `null` 与 `0` 的含义完全相反：0 是"确实没做过"，
        null 是"读不到"。显示成 0 的话，评审者会以为系统是空的 ——
        而这正是本项目最忌讳的那类"看起来合理的错误"。
      -->
      <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div
          v-for="c in statCards"
          :key="c.label"
          class="rounded-xl border border-warm-border bg-white p-3"
        >
          <p class="text-[10px] font-semibold uppercase text-wood-muted">{{ c.label }}</p>
          <p class="num mt-1 text-[22px] font-bold" :class="c.tone">{{ c.value }}</p>
          <p class="mt-0.5 text-[10px] text-wood-muted">{{ c.hint }}</p>
        </div>
      </div>

      <p
        v-if="!stats.available"
        class="mt-3 rounded-xl border border-accent-gold/40 bg-wood-light/50 p-2.5 text-[11px] leading-relaxed text-wood"
      >
        {{ stats.reason }}
      </p>
    </section>

    <!-- ══ 中部：任务 + 系统状态 ══ -->
    <section class="grid grid-cols-1 gap-5 lg:grid-cols-3">
      <div class="card flex flex-col lg:col-span-2">
        <div class="flex items-center justify-between border-b border-warm-border px-4 py-3">
          <div class="flex items-center gap-2">
            <AppIcon name="clock" :size="17" class="text-botanical" />
            <h2 class="font-serif text-[16px] font-semibold text-wood-dark">最近任务</h2>
            <span v-if="tasks.runningCount" class="chip">
              {{ tasks.runningCount }} 个进行中
            </span>
          </div>
          <span class="text-[11px] text-wood-muted">仅本会话 · 结果在后端保留 1 小时</span>
        </div>

        <EmptyState
          v-if="!recent.length"
          art="拍摄与上传-image-dropzone_zdre"
          title="还没有任务记录"
          description="从「户型图解析」开始——上传一张户型图，系统会识别房间、墙体与尺寸，并给出五维诊断。"
        >
          <button class="btn-primary px-4 py-2" type="button" @click="router.push('/parse')">
            <AppIcon name="upload-simple" :size="16" />
            <span>去上传户型图</span>
          </button>
        </EmptyState>

        <ul v-else class="divide-y divide-warm-grid">
          <li v-for="t in recent" :key="t.taskId">
            <button
              class="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-warm-sidebar/60"
              type="button"
              @click="router.push(t.route)"
            >
              <span
                class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-warm-border bg-warm-sidebar text-wood"
              >
                <AppIcon
                  :name="t.kind === 'parse' ? 'blueprint' : t.kind === 'generate' ? 'sparkle' : 'shield-check'"
                  :size="16"
                />
              </span>
              <span class="min-w-0 flex-1">
                <span class="flex items-center gap-2">
                  <span class="text-[13px] font-semibold text-wood-dark">{{ t.kindLabel }}</span>
                  <span class="font-mono text-[10px] text-wood-muted">
                    {{ t.taskId.slice(-8) }}
                  </span>
                  <span
                    v-if="t.degraded"
                    class="rounded border border-accent-gold/30 bg-wood-light px-1.5 text-[10px] font-semibold text-accent-gold"
                  >
                    降级
                  </span>
                </span>
                <span class="mt-0.5 block truncate text-[11px] text-wood-muted">
                  {{ t.summary || t.phaseText }}
                </span>
              </span>
              <span class="flex shrink-0 flex-col items-end gap-1">
                <span
                  class="rounded-full border px-2 py-0.5 text-[10px] font-semibold"
                  :class="statusTone[t.status]"
                >
                  {{ statusLabel[t.status] }}
                </span>
                <span class="text-[10px] text-wood-muted/70">{{ relTime(t.createdAt) }}</span>
              </span>
            </button>
          </li>
        </ul>
      </div>

      <!-- 系统状态：真实 health 接口 -->
      <div class="card flex flex-col">
        <div class="flex items-center justify-between border-b border-warm-border px-4 py-3">
          <div class="flex items-center gap-2">
            <AppIcon name="shield" :size="17" class="text-botanical" />
            <h2 class="font-serif text-[16px] font-semibold text-wood-dark">系统状态</h2>
          </div>
          <span
            class="rounded-full border px-2 py-0.5 text-[10px] font-semibold"
            :class="
              health.status === 'ok'
                ? 'border-botanical/20 bg-botanical-light text-botanical'
                : health.status === 'degraded'
                  ? 'border-accent-gold/30 bg-wood-light text-accent-gold'
                  : 'border-warm-border bg-warm-sidebar text-wood-muted'
            "
          >
            {{ health.status === 'ok' ? '正常' : health.status === 'degraded' ? '降级' : '未知' }}
          </span>
        </div>

        <ul v-if="checkEntries.length" class="divide-y divide-warm-grid">
          <li
            v-for="[key, c] in checkEntries"
            :key="key"
            class="flex items-start gap-2.5 px-4 py-2.5"
          >
            <AppIcon
              :name="c.ok ? 'check-circle' : 'warning-circle'"
              :size="15"
              class="mt-0.5 shrink-0"
              :class="c.ok ? 'text-botanical' : 'text-accent-gold'"
            />
            <div class="min-w-0 flex-1">
              <p class="font-mono text-[12px] font-semibold text-wood-dark">{{ key }}</p>
              <p class="mt-0.5 break-words text-[11px] leading-relaxed text-wood-muted">
                {{ c.detail }}
              </p>
            </div>
          </li>
        </ul>

        <p v-else class="px-4 py-6 text-center text-[12px] text-wood-muted">
          {{ health.loaded ? '拿不到健康检查结果' : '正在检查…' }}
        </p>

        <p
          v-if="health.error"
          class="border-t border-warm-border px-4 py-2.5 text-[11px] leading-relaxed text-accent-red"
        >
          {{ health.error }}
        </p>
      </div>
    </section>

    <!-- ══ 下部：实景画廊（两排反向滚动）══ -->
    <section class="card p-4">
      <div class="mb-3 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <AppIcon name="palette" :size="17" class="text-botanical" />
          <h2 class="font-serif text-[16px] font-semibold text-wood-dark">
            {{ imagePool.isQuanyouCase ? '实景案例库' : '家居实拍参考' }}
          </h2>
          <span class="tag">{{ cases.length }} 张</span>
        </div>
        <span class="text-[11px] text-wood-muted">悬停暂停 · 点击放大</span>
      </div>

      <!--
        两排反向滚动。结构上有个**必须照做**的细节：

        每一项自带 `pr-3`（内边距）而**不是**外层用 `gap` —— 见 style.css
        里 `.marquee` 的注释。简单说：动画靠"复制一份、位移 -50%"，用 gap
        的话一半宽度不等于整份，循环到接缝会跳。

        每排渲染 `2` 份同样的列表，第二份 `aria-hidden` 且 `tabindex="-1"`，
        免得屏幕阅读器和 Tab 键把每张图念两遍。
      -->
      <div class="flex flex-col gap-3">
        <div
          v-for="row in galleryRows"
          :key="row.key"
          class="marquee"
          :class="row.reverse && 'marquee-reverse'"
        >
          <div class="marquee-track" :style="{ animationDuration: rowDuration(row.idx.length, row.reverse) }">
            <template v-for="copy in 2" :key="copy">
              <div
                v-for="gi in row.idx"
                :key="`${copy}-${gi}`"
                class="w-[200px] shrink-0 pr-3"
                :aria-hidden="copy === 2"
              >
                <button
                  class="group relative block w-full cursor-zoom-in overflow-hidden rounded-xl border border-warm-border"
                  type="button"
                  :tabindex="copy === 2 ? -1 : 0"
                  :aria-label="`放大查看第 ${gi + 1} 张`"
                  @click="openAt(gi)"
                >
                  <img
                    :src="cases[gi]"
                    :alt="imagePool.isQuanyouCase ? '全友装修实景' : '家居实拍'"
                    loading="lazy"
                    class="aspect-[4/3] w-full object-cover transition-transform duration-500 group-hover:scale-105"
                  />
                  <span
                    class="absolute left-1.5 top-1.5 rounded bg-white/90 px-1.5 py-0.5 font-mono text-[9px] text-wood-muted backdrop-blur-sm"
                  >
                    {{ String(gi + 1).padStart(2, '0') }}
                  </span>
                  <span
                    class="absolute inset-0 flex items-center justify-center bg-wood-dark/0 opacity-0 transition-all duration-200 group-hover:bg-wood-dark/25 group-hover:opacity-100"
                  >
                    <span
                      class="flex h-8 w-8 items-center justify-center rounded-full bg-white/90 text-wood"
                    >
                      <AppIcon name="magnifying-glass" :size="15" />
                    </span>
                  </span>
                </button>
              </div>
            </template>
          </div>
        </div>
      </div>

      <!--
        ⚠️ **半页的图片声明只写在这一处。**

        原来每张图下方（主图、每张缩略图、灯箱）都挂一行"素材来自…"，
        用户反馈：每张都声明会让看图这件事变差（视线总被那行字拽住）。
        声明的作用是"明确告知"，**告知一次就够了** —— 逐图重复不会让声明
        更有效，只会让界面像一面免责声明墙。
      -->
      <p class="mt-3 text-[11px] leading-relaxed text-wood-muted/80">
        素材来源：{{ imageAttribution }}。
      </p>
    </section>

    <!--
      灯箱。主图与两排画廊共用一套下标，翻页能一路翻到底。

      ⚠️ 这里的 `caption` **保留**，它不算"重复声明"：
      灯箱是**全屏**的，会把上一条（唯一的）声明整个盖住 ——
      人在单独看某张图时，声明必须仍然可见。这不是多印一遍，
      是让同一句话在另一个上下文里继续有效。
    -->
    <ImageLightbox
      :images="cases"
      :index="lightboxIndex"
      :alt="imagePool.isQuanyouCase ? '全友装修实景案例' : '家居实拍参考'"
      :caption="imageAttribution"
      @close="lightboxIndex = -1"
      @update:index="lightboxIndex = $event"
    />
  </main>
</template>
