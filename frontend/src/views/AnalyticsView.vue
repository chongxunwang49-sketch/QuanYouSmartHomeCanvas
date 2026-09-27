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
import { systemMetrics } from '@/api'
import { messageOf } from '@/api'
import type { MetricsData } from '@/api/types'
import { useHealthStore } from '@/stores/health'
import { useTaskStore } from '@/stores/task'

/**
 * 数据分析。
 *
 * ══════════════════════════════════════════════════════════════════
 * ⚠️ **这一页只展示真实观测到的数据，一条都不编。**
 * ══════════════════════════════════════════════════════════════════
 * 仪表盘是最容易造假的地方：放几条漂亮的同比曲线、几个"平均节省 23%"，
 * 页面立刻显得很完整，而评审者一问数据来源就穿帮。
 *
 * 数据分三层，**来源各不相同，所以分开陈列**：
 *   · 本次会话任务    —— 前端 Pinia 里的真实提交记录（刷新即清零，写明了）
 *   · 后端性能指标    —— `/system/metrics`（AC-23）：各阶段 P50/P95、
 *                        按 Agent 分的 LLM 耗时与平均 Token
 *   · 依赖状态        —— `/system/health` 的实时结果
 *
 * ⚠️ **2026-09-26 改掉了一句过期的话。**
 * 这一段原来写着「后端目前在 `/system/health` 里**没有**暴露计数，
 * 所以这一页不显示 Token 消耗和调用次数」。而 `/system/metrics` 早就有了
 * （AC-23 实现时就带着 P95 与 Token），只是**前端零消费方** ——
 * 全仓 grep 只在测试里命中它。于是这一页长期少了一整块它本来能显示的东西，
 * 而页面上还写着"后端没有"。
 *
 * 这类"注释比代码旧"的失败模式值得记：**它不报错、不影响功能，
 * 只是让界面少显示一些真实数据，并用一句话解释成"后端没有"。**
 * 所以现在把指标接上，并把那句话删掉。
 *
 * ⚠️ `available: false` 与 `0` 是两件事：样本不足时后端给 `available: false`
 * + `reason`，界面上显示成「样本不足」而不是 0 —— 一个假的 0 会被读成
 * "快到无法测量"。
 */
const tasks = useTaskStore()
const health = useHealthStore()

const chartEl = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null

// ── 后端性能指标（AC-23）──
const metrics = ref<MetricsData | null>(null)
const metricsError = ref('')
/** 回溯天数。后端限 1..30（上限那条是接口自己拒绝的，不是前端限制） */
const days = ref(1)

const stageRows = computed(() =>
  Object.entries(metrics.value?.metrics ?? {}).map(([key, m]) => ({ key, ...m })),
)
const agentRows = computed(() =>
  Object.entries(metrics.value?.llm_by_agent ?? {})
    .map(([agent, m]) => ({ agent, ...m }))
    .sort((a, b) => b.p95 - a.p95),
)
/** 总 Token —— **后端给的是每个 Agent 的平均值**，乘它的样本数再求和 */
const totalTokens = computed(() =>
  agentRows.value.reduce((acc, a) => acc + a.avg_total_tokens * a.n, 0),
)

async function loadMetrics() {
  metricsError.value = ''
  try {
    metrics.value = await systemMetrics(days.value)
  } catch (e) {
    // 需要登录。非登录态下原样显示后端那句话，不自己编
    metrics.value = null
    metricsError.value = messageOf(e)
  }
}

watch(days, loadMetrics)

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
  { label: '户型解析', range: '33–48s', note: '真实测量区间' },
  { label: '方案生成', range: '~95s', note: '含前置解析时约 110s' },
  { label: '避坑审查', range: '~19s', note: '单份报价单' },
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
  loadMetrics()
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
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6 surface-grid-fine">
    <PageHeader
      :breadcrumb="['全友·智绘家', '数据分析']"
      title="运行数据分析"
      :status="{ icon: 'chart-line', text: '仅本会话观测值', tone: 'wood' }"
      :code="health.version ? `版本 v${health.version}` : ''"
    />

    <!-- 口径声明 -->
    <section class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3.5">
      <div class="flex items-start gap-2.5">
        <AppIcon name="info" :size="18" class="mt-0.5 shrink-0 text-wood-muted" />
        <p class="text-[11px] leading-relaxed text-wood-muted">
          <span class="font-semibold text-wood">口径说明：</span>
          这一页有三层数据，<strong class="font-semibold">来源各不相同，所以分开陈列</strong>：
          「本次会话」是你在这个标签页里真正提交过的任务（刷新即清零）；
          「累计性能指标」是跨会话累计下来的历史数字；
          「参考耗时区间」是标准样张上的基准值，与上面两者都不是一回事。
        </p>
      </div>
    </section>

    <!-- ══ 后端性能指标（AC-23）══ -->
    <section class="card p-4">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 class="flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
          <AppIcon name="cpu" :size="17" class="text-botanical" />
          <span>后端性能指标</span>
        </h2>
        <label class="flex items-center gap-2 text-[11px] text-wood-muted">
          <span>统计窗口</span>
          <select v-model.number="days" class="field px-2 py-1">
            <option :value="1">最近 1 天</option>
            <option :value="7">最近 7 天</option>
            <option :value="30">最近 30 天</option>
          </select>
        </label>
      </div>

      <p
        v-if="metricsError"
        class="rounded-xl border border-accent-gold/40 bg-wood-light/50 p-3 text-[11px] leading-relaxed text-wood"
      >
        {{ metricsError }}
      </p>

      <template v-else-if="metrics">
        <!--
          ⚠️ `available: false` 显示成「样本不足 + 原因」，**不显示 0**。
          一个假的 0 会被读成"快到无法测量"，而真相是"根本没数据"。
        -->
        <div class="overflow-hidden rounded-xl border border-warm-border">
          <table class="w-full border-collapse text-[11px]">
            <thead class="bg-warm-sidebar/70 text-left text-wood-muted">
              <tr>
                <th class="px-3 py-2 font-semibold">阶段</th>
                <th class="w-16 px-2 py-2 text-right font-semibold">样本</th>
                <th class="w-20 px-2 py-2 text-right font-semibold">P50</th>
                <th class="w-20 px-2 py-2 text-right font-semibold">P95</th>
                <th class="w-20 px-2 py-2 text-right font-semibold">目标</th>
                <th class="w-20 px-2 py-2 text-center font-semibold">达标</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="m in stageRows"
                :key="m.key"
                class="border-t border-warm-border/70"
              >
                <td class="px-3 py-2 text-wood-dark">{{ m.label }}</td>
                <template v-if="m.available">
                  <td class="num px-2 py-2 text-right text-wood-muted">{{ m.n }}</td>
                  <td class="num px-2 py-2 text-right text-wood">{{ m.p50 }}{{ m.unit }}</td>
                  <td class="num px-2 py-2 text-right font-semibold text-wood-dark">
                    {{ m.p95 }}{{ m.unit }}
                  </td>
                  <td class="num px-2 py-2 text-right text-wood-muted">
                    ≤{{ m.target_p95 }}{{ m.unit }}
                  </td>
                  <td class="px-2 py-2 text-center">
                    <span
                      class="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                      :class="m.meets_target
                        ? 'bg-botanical-light text-botanical'
                        : 'bg-accent-red/10 text-accent-red'"
                    >{{ m.meets_target ? '达标' : '未达标' }}</span>
                  </td>
                </template>
                <td v-else colspan="5" class="px-3 py-2 text-[11px] text-wood-muted">
                  样本不足 —— {{ m.reason }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="mt-2 text-[10px] leading-relaxed text-wood-muted">
          「达标」按各项指标的既定目标判定，界面不另行计算。
        </p>

        <!-- ── LLM 调用：按 Agent 分 ── -->
        <div class="mt-4">
          <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
            <h3 class="flex items-center gap-1.5 text-[13px] font-semibold text-wood-dark">
              <AppIcon name="robot" :size="15" class="text-botanical" />
              <span>各 Agent 的模型调用</span>
            </h3>
            <p v-if="totalTokens" class="text-[11px] text-wood-muted">
              近 {{ days }} 天累计 Token 约
              <b class="num text-wood-dark">{{ Math.round(totalTokens).toLocaleString() }}</b>
            </p>
          </div>
          <div class="overflow-hidden rounded-xl border border-warm-border">
            <table class="w-full border-collapse text-[11px]">
              <thead class="bg-warm-sidebar/70 text-left text-wood-muted">
                <tr>
                  <th class="px-3 py-2 font-semibold">Agent</th>
                  <th class="w-16 px-2 py-2 text-right font-semibold">调用</th>
                  <th class="w-20 px-2 py-2 text-right font-semibold">P50</th>
                  <th class="w-20 px-2 py-2 text-right font-semibold">P95</th>
                  <th class="w-24 px-2 py-2 text-right font-semibold">平均 Token</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="a in agentRows"
                  :key="a.agent"
                  class="border-t border-warm-border/70"
                >
                  <td class="px-3 py-2 font-mono text-wood-dark">{{ a.agent }}</td>
                  <td class="num px-2 py-2 text-right text-wood-muted">{{ a.n }}</td>
                  <td class="num px-2 py-2 text-right text-wood">
                    {{ (a.p50 / 1000).toFixed(1) }}s
                  </td>
                  <td class="num px-2 py-2 text-right font-semibold text-wood-dark">
                    {{ (a.p95 / 1000).toFixed(1) }}s
                  </td>
                  <td class="num px-2 py-2 text-right text-wood">
                    {{ Math.round(a.avg_total_tokens).toLocaleString() }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="mt-2 text-[10px] leading-relaxed text-wood-muted">
            用量是<strong class="font-semibold">估算值</strong>：按每次调用的平均值累计，
            不会精确到每一次。
          </p>
        </div>

        <div
          v-if="metrics.notes?.length"
          class="mt-3 rounded-xl border border-warm-border bg-warm-sidebar/40 p-3"
        >
          <ul class="space-y-1">
            <li
              v-for="(n, i) in metrics.notes"
              :key="i"
              class="flex items-start gap-1.5 text-[10px] leading-relaxed text-wood-muted"
            >
              <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent-gold" />
              <span>{{ n }}</span>
            </li>
          </ul>
        </div>
      </template>

      <p v-else class="flex items-center gap-2 py-4 text-[12px] text-wood-muted">
        <AppIcon name="spinner" :size="16" class="animate-spin" />
        正在读取后端指标…
      </p>
    </section>

    <!-- ══ 本次会话 ══ -->
    <section class="flex items-center gap-2 pt-1">
      <span class="h-px flex-1 bg-warm-border" />
      <span class="text-[11px] font-semibold uppercase tracking-wide text-wood-muted">
        以下为本会话观测值
      </span>
      <span class="h-px flex-1 bg-warm-border" />
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
        以下是<strong class="font-semibold">标准样张上的基准值</strong>，不是本会话的实测，
        所以和上面的图分开陈列。
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
