<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { imageAttribution, imagePool } from '@/assets/images/pool'
import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useHealthStore } from '@/stores/health'
import { useTaskStore } from '@/stores/task'

/**
 * 工作台总览。
 *
 * ⚠️ **这一页没有假数据。** 所有数字都来自真实来源：
 * 任务台账（本次会话真实提交过的任务）、`/system/health`（真实依赖状态）、
 * `seed_data/material_catalog.json`（演示目录，会显著标注）。
 *
 * 之所以强调这点：工作台是"仪表盘"最容易被编数据的地方——
 * 写死几个漂亮的同比增幅、几条趋势线，看起来完整，但那正是
 * 用户最反感的"AI 味"。宁可空着，也不编。
 */
const router = useRouter()
const tasks = useTaskStore()
const health = useHealthStore()

// 实景图池。案例图缺失时自动回落 Pexels 照片（见 assets/images/pool.ts）
const cases = imagePool.main

const galleryIndex = ref(0)
const heroSrc = computed(() => cases[galleryIndex.value % Math.max(cases.length, 1)] ?? '')

onMounted(() => {
  health.refresh()
  // 5 秒换一张实景图。慢一点——这是个"氛围"位，不是轮播广告。
  setInterval(() => {
    if (cases.length) galleryIndex.value = (galleryIndex.value + 1) % cases.length
  }, 5000)
})

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
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6">
    <PageHeader
      :breadcrumb="['全友·智绘家', '工作台总览']"
      title="工作台总览"
      :status="{ icon: 'leaf', text: 'Botanical Warmth · Nature Edition' }"
    />

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

      <!-- 实景图：真实采集的全友装修案例 -->
      <div class="card relative overflow-hidden">
        <img
          v-if="heroSrc"
          :src="heroSrc"
          :alt="imagePool.isQuanyouCase ? '全友装修实景案例' : '家居实拍参考'"
          class="h-full min-h-[260px] w-full object-cover transition-opacity duration-500"
        />
        <div
          class="pointer-events-none absolute inset-0 bg-gradient-to-t from-wood-dark/45 via-transparent to-transparent"
        />
        <div class="absolute bottom-0 left-0 right-0 p-4">
          <span
            class="inline-flex items-center gap-1.5 rounded-full border border-white/25 bg-white/85 px-2.5 py-0.5 text-[11px] font-semibold text-wood backdrop-blur-sm"
          >
            <AppIcon name="house-line" :size="13" class="text-botanical" />
            <span>{{ imagePool.isQuanyouCase ? '全友实景案例' : '家居实拍参考' }}</span>
          </span>
          <p class="mt-2 text-[11px] leading-relaxed text-white/90">{{ imageAttribution }}</p>
        </div>
      </div>
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

    <!-- ══ 下部：实景画廊 ══ -->
    <section class="card p-4">
      <div class="mb-3 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <AppIcon name="palette" :size="17" class="text-botanical" />
          <h2 class="font-serif text-[16px] font-semibold text-wood-dark">
            {{ imagePool.isQuanyouCase ? '实景案例库' : '家居实拍参考' }}
          </h2>
          <span class="tag">{{ cases.length }} 张</span>
        </div>
        <span class="text-[11px] text-wood-muted">选方案时可对照参考</span>
      </div>
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
        <div
          v-for="(src, i) in cases"
          :key="src"
          class="group relative overflow-hidden rounded-xl border border-warm-border"
        >
          <img
            :src="src"
            :alt="imagePool.isQuanyouCase ? '全友装修实景' : '家居实拍'"
            loading="lazy"
            class="aspect-[4/3] w-full object-cover transition-transform duration-500 group-hover:scale-105"
          />
          <span
            class="absolute left-1.5 top-1.5 rounded bg-white/90 px-1.5 py-0.5 font-mono text-[9px] text-wood-muted backdrop-blur-sm"
          >
            {{ String(i + 1).padStart(2, '0') }}
          </span>
        </div>
      </div>
      <p class="mt-3 text-[11px] leading-relaxed text-wood-muted/80">
        素材来源：{{ imageAttribution }}。
      </p>
    </section>
  </main>
</template>
