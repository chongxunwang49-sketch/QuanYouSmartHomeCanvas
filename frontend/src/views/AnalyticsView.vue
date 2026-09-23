<script setup lang="ts">
// ⚠️ 按需注册 ECharts，**不要 `import * as echarts from 'echarts'`**。
//
// 全量引入是 1036 KB（gzip 343 KB）——而这一页只画一个柱状图。
// `echarts/core` + 显式 use() 之后只打进来真正用到的那几个模块，
// 体积降到约 1/6。
//
// 代价：以后要用别的图表类型（折线、饼图）必须在这里补注册，
// 否则运行时静默不渲染（ECharts 对未注册类型的处理是空白，不报错）。
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { init, use, type ECharts } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useHealthStore } from '@/stores/health'
import { useTaskStore } from '@/stores/task'

/**
 * 数据分析。
 *
 * ⚠️ **这一页只展示本会话真实观测到的数据，一条都不编。**
 *
 * 仪表盘是最容易造假的地方：放几条漂亮的同比曲线、几个"平均节省 23%"，
 * 页面立刻显得很完整，而评审者一问数据来源就穿帮。
 * 所以这里的口径是：
 *   · 任务数、耗时 —— 本次会话真实提交过的任务（刷新即清零，页面上写明了）
 *   · 依赖状态 —— `/system/health` 的实时结果
 *   · 模型调用 —— 后端目前在 `/system/health` 里**没有**暴露计数，
 *     所以这一页**不显示** Token 消耗和调用次数，而不是估一个数填上
 */
const tasks = useTaskStore()
const health = useHealthStore()

const chartEl = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null

const finished = computed(() => tasks.entries.filter((t) => t.finishedAt !== null))

const durations = computed(() =>
  finished.value.map((t) => ({
    label: `${t.kindLabel} ${t.taskId.slice(-4)}`,
    kind: t.kindLabel,
    seconds: ((t.finishedAt! - t.createdAt) / 1000).toFixed(1),
    value: (t.finishedAt! - t.createdAt) / 1000,
  })),
)

const stats = computed(() => {
  const all = tasks.entries
  const by = (s: string) => all.filter((t) => t.status === s).length
  const secs = durations.value.map((d) => d.value)
  return {
    total: all.length,
    completed: by('completed'),
    failed: by('failed'),
    running: by('processing') + by('pending'),
    degraded: all.filter((t) => t.degraded).length,
    avg: secs.length ? (secs.reduce((a, b) => a + b, 0) / secs.length).toFixed(1) : '—',
    max: secs.length ? Math.max(...secs).toFixed(1) : '—',
    min: secs.length ? Math.min(...secs).toFixed(1) : '—',
  }
})

/** 后端文档里的实测区间。用来做对照 —— 标注为"参考"而不是实测。 */
const REFERENCE = [
  { label: '户型解析', range: '33–48s', note: '两次真实解析分别为 33s / 48s' },
  { label: '方案生成', range: '~95s', note: '整条链 110s，含解析段' },
  { label: '避坑审查', range: '~19s', note: '单次 A-06' },
]

function renderChart() {
  if (!chartEl.value) return
  if (!chart) chart = init(chartEl.value)

  const d = durations.value
  chart.setOption(
    {
      grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: '#FFFFFF',
        borderColor: '#EDE8E0',
        textStyle: { color: '#2C2418', fontSize: 12 },
        formatter: (p: { name: string; value: number }[]) =>
          `${p[0].name}<br/>耗时 <b>${p[0].value.toFixed(1)}s</b>`,
      },
      xAxis: {
        type: 'category',
        data: d.map((x) => x.label),
        axisLine: { lineStyle: { color: '#EFEAE2' } },
        axisLabel: { color: '#7A6E5E', fontSize: 11 },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        name: '秒',
        nameTextStyle: { color: '#7A6E5E', fontSize: 11 },
        splitLine: { lineStyle: { color: '#F5F2EC' } },
        axisLabel: { color: '#7A6E5E', fontSize: 11 },
      },
      series: [
        {
          type: 'bar',
          data: d.map((x) => x.value),
          barMaxWidth: 42,
          itemStyle: { color: '#4A7C59', borderRadius: [6, 6, 0, 0] },
        },
      ],
    },
    true,
  )
}

const onResize = () => chart?.resize()

// 图表要在 DOM 就绪后才能 init，所以监听放在 setup 层、渲染在回调里：
// immediate 触发的那一次会因为 chartEl 还没挂载而直接返回，
// 真正的首绘由 onMounted 里调一次 renderChart 完成。
watch(durations, () => renderChart())

onMounted(() => {
  health.refresh()
  window.addEventListener('resize', onResize)
  renderChart()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  chart?.dispose()
  chart = null
})

const checkEntries = computed(() => Object.entries(health.checks))
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6">
    <PageHeader
      :breadcrumb="['全友·智绘家', '数据分析']"
      title="运行数据分析"
      :status="{ icon: 'chart-line', text: '仅本会话观测值', tone: 'wood' }"
      :code="health.version ? `BACKEND v${health.version}` : ''"
    />

    <!-- 口径声明 -->
    <section class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3.5">
      <div class="flex items-start gap-2.5">
        <AppIcon name="info" :size="18" class="mt-0.5 shrink-0 text-wood-muted" />
        <p class="text-[11px] leading-relaxed text-wood-muted">
          <span class="font-semibold text-wood">口径说明：</span>
          下面所有数字都来自**本次会话真实提交过的任务**，刷新页面即清零。
          没有历史趋势、没有同比环比 —— 后端的
          <code class="rounded bg-white px-1 font-mono">/system/health</code>
          目前不暴露统计计数，所以这一页也**不显示** Token 消耗与调用次数，
          而不是估一个数填上。
          要做真正的历史分析，需要先把每次任务的指标落库（目前任务结果只保留 1 小时）。
        </p>
      </div>
    </section>

    <!-- ══ 会话统计 ══ -->
    <section class="grid grid-cols-2 gap-4 lg:grid-cols-4">
      <div
        v-for="s in [
          { label: '本会话任务', value: stats.total, icon: 'list-checks', tone: 'text-wood-dark' },
          { label: '成功完成', value: stats.completed, icon: 'check-circle', tone: 'text-botanical' },
          { label: '其中降级', value: stats.degraded, icon: 'warning-circle', tone: 'text-accent-gold' },
          { label: '失败 / 进行中', value: `${stats.failed} / ${stats.running}`, icon: 'spinner', tone: 'text-wood' },
        ]"
        :key="s.label"
        class="card p-4"
      >
        <div class="flex items-center justify-between">
          <span class="text-[11px] font-semibold uppercase text-wood-muted">{{ s.label }}</span>
          <AppIcon :name="s.icon" :size="16" :class="s.tone" />
        </div>
        <p class="num mt-1 text-[26px] font-bold" :class="s.tone">{{ s.value }}</p>
      </div>
    </section>

    <!-- ══ 耗时图 ══ -->
    <section class="card p-4">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 class="flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
          <AppIcon name="clock" :size="17" class="text-botanical" />
          <span>任务实测耗时</span>
          <span class="tag">{{ durations.length }} 次</span>
        </h2>
        <div v-if="durations.length" class="flex items-center gap-3 text-[11px] text-wood-muted">
          <span>平均 <b class="num text-wood-dark">{{ stats.avg }}s</b></span>
          <span>最快 <b class="num text-wood-dark">{{ stats.min }}s</b></span>
          <span>最慢 <b class="num text-wood-dark">{{ stats.max }}s</b></span>
        </div>
      </div>

      <div v-if="durations.length" ref="chartEl" class="h-[260px] w-full" />

      <EmptyState
        v-else
        art="预算与报价-data-transfer_hz9g"
        title="还没有可统计的任务"
        description="去跑一次户型解析或方案生成，完成后这里会画出真实的耗时。"
      />
    </section>

    <!-- ══ 参考区间（明确标注为文档实测，不是本会话数据）══ -->
    <section class="card p-4">
      <h2 class="mb-1 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="scales" :size="17" class="text-wood" />
        <span>参考耗时区间</span>
      </h2>
      <p class="mb-3 text-[11px] leading-relaxed text-wood-muted">
        ⚠️ 下表是**后端开发期的实测记录**（记在 README 与面试亮点文档里），
        不是本会话的观测值，因此和上面的图分开陈列。
      </p>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div
          v-for="r in REFERENCE"
          :key="r.label"
          class="rounded-xl border border-warm-border bg-warm-sidebar/40 p-3"
        >
          <p class="text-[12px] font-semibold text-wood-dark">{{ r.label }}</p>
          <p class="num mt-0.5 text-[18px] font-bold text-botanical">{{ r.range }}</p>
          <p class="mt-1 text-[10px] leading-relaxed text-wood-muted">{{ r.note }}</p>
        </div>
      </div>
    </section>

    <!-- ══ 依赖状态 ══ -->
    <section class="card p-4">
      <h2 class="mb-3 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="shield" :size="17" class="text-botanical" />
        <span>依赖状态</span>
        <span
          class="rounded-full border px-2 py-0.5 text-[10px] font-semibold"
          :class="
            health.status === 'ok'
              ? 'border-botanical/20 bg-botanical-light text-botanical'
              : 'border-accent-gold/30 bg-wood-light text-accent-gold'
          "
        >
          {{ health.status === 'ok' ? '全部正常' : '降级运行' }}
        </span>
      </h2>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div
          v-for="[key, c] in checkEntries"
          :key="key"
          class="flex items-start gap-2.5 rounded-xl border border-warm-border bg-white p-3"
        >
          <AppIcon
            :name="c.ok ? 'check-circle' : 'warning-circle'"
            :size="16"
            class="mt-0.5 shrink-0"
            :class="c.ok ? 'text-botanical' : 'text-accent-gold'"
          />
          <div class="min-w-0">
            <p class="font-mono text-[12px] font-semibold text-wood-dark">{{ key }}</p>
            <p class="mt-0.5 break-words text-[11px] leading-relaxed text-wood-muted">
              {{ c.detail }}
            </p>
          </div>
        </div>
      </div>
      <p v-if="!checkEntries.length" class="py-4 text-center text-[12px] text-wood-muted">
        正在检查…
      </p>
    </section>
  </main>
</template>
