<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import AppIcon from './AppIcon.vue'
import { layoutHotspots, layoutPlanSvg, layoutPlanSvgUrl, messageOf } from '../api'
import type { PlanHotspot, PlanRenderData } from '../api'

/**
 * 矢量户型图 + 物品热区（AC-07 / AC-09 / AC-21）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 前端**一行几何计算都不做**
 * ══════════════════════════════════════════════════════════════════
 * SVG 是**内联**进 DOM 的（`v-html`），不是 `<img src="...svg">`。
 * 这不是炫技，是唯一能让命中测试免费正确的做法：
 *
 *   · 热区就是 SVG 里的 `<path>`，浏览器自己按 path 做命中判定 ——
 *     包括墙面热区 `fill-rule="evenodd"` 中间挖空的那块会自动穿透到下层。
 *     换成在前端"按 bbox 判断鼠标在不在里面"，那块挖空就得自己再实现一遍，
 *     而实现错了的表现是"悬停在墙中间也能弹出墙纸价格"——不报错，只是不对。
 *   · 提示卡跟着**光标**走，不用把画布像素换算成屏幕像素。
 *     少一次换算，就少一处能和后端的坐标漂开的地方。
 *
 * 结论：后端算坐标，浏览器判命中，前端只负责显示。
 *
 * ══════════════════════════════════════════════════════════════════
 * 安全：房间名是模型输出
 * ══════════════════════════════════════════════════════════════════
 * `v-html` 意味着这段内容会进 DOM。后端已经对房间名做了 XML 转义
 * （含引号 —— `xml.sax.saxutils.escape` 默认**不转引号**，我们补上了），
 * 并有测试钉住"解析后的属性里不许出现 `on*`"。
 * 前端这边额外收一道：热区信息全部用**文本插值**渲染，不再套 `v-html`。
 */

const props = withDefaults(
  defineProps<{
    layoutId: string
    /** 卡片标题 */
    title?: string
    /** 紧凑模式：热区列表收起，只留画布 */
    compact?: boolean
  }>(),
  { title: '户型矢量图', compact: false },
)

const svg = ref('')
const data = ref<PlanRenderData | null>(null)
const loading = ref(false)
const error = ref('')

/** 当前悬停的热区下标；-1 表示没有 */
const hovered = ref(-1)
/** 提示卡的位置，跟随光标（容器坐标系） */
const tip = ref({ x: 0, y: 0, flip: false })

const canvasEl = ref<HTMLElement | null>(null)

const hotspots = computed(() => data.value?.hotspots ?? [])
const hoveredSpot = computed<PlanHotspot | null>(
  () => hotspots.value.find((h) => h.index === hovered.value) ?? null,
)

/**
 * 品类 → 图标。
 *
 * ⚠️ 这里**没有**"品类 → 中文名"的表，是刻意的：标签由后端拼好
 * （`客厅地面` / `客厅墙面`），前端再拼一次就会出现两个来源，
 * 改一处漏一处。少一张表，少一处能对不上的地方。
 */
const CATEGORY_ICON: Record<string, string> = {
  floor: 'squares-four',
  paint: 'palette',
  door: 'door',
}

const openUrl = computed(() => layoutPlanSvgUrl(props.layoutId))

/**
 * 精确度 → 展示文案。
 *
 * ⚠️ 需求文档 2.2.6 的诚实性原则：`room_level` 必须说成"整间房的参考价"，
 * 不能假装精确到某一件家具。所以这里不是两个视觉样式那么简单，
 * **文案本身也要跟着改** —— 用户读到的和它能保证的必须是一回事。
 */
const PRECISION_LABEL: Record<string, string> = {
  exact: '几何精确定位',
  room_level: '房间级近似（虚线区域）',
  none: '本图不提供热区',
}

async function load() {
  if (!props.layoutId) return
  loading.value = true
  error.value = ''
  hovered.value = -1
  try {
    // 两个请求并行。后端保证同一 layout_id 得到同一套坐标变换，
    // 所以并发没有顺序依赖 —— 不用等图回来再去取热区。
    const [raw, payload] = await Promise.all([
      layoutPlanSvg(props.layoutId),
      layoutHotspots(props.layoutId),
    ])
    svg.value = raw
    data.value = payload
  } catch (e) {
    error.value = messageOf(e)
    svg.value = ''
    data.value = null
  } finally {
    loading.value = false
  }
}

watch(() => props.layoutId, load, { immediate: true })

/**
 * 命中判定交给浏览器：事件委托到容器，冒泡上来的 `event.target`
 * 若是热区 `<path>` 就取它的 `data-hotspot`。
 *
 * 不用给每个 path 单独挂监听 —— 那样每次重画 SVG 都要重挂一遍，
 * 而某个热区漏挂的表现是"这块区域悬停没反应"，很难发现。
 */
function onOver(e: MouseEvent) {
  const el = (e.target as Element | null)?.closest?.('.hotspot') as HTMLElement | null
  if (!el) return
  const idx = Number(el.dataset.hotspot)
  if (Number.isFinite(idx)) hovered.value = idx
}

function onMove(e: MouseEvent) {
  const rect = canvasEl.value?.getBoundingClientRect()
  if (!rect) return
  const x = e.clientX - rect.left
  const y = e.clientY - rect.top
  // 靠近右/下边时把卡片翻到另一侧，免得被容器裁掉
  tip.value = { x, y, flip: x > rect.width - 280 }
}

function clear() {
  hovered.value = -1
}

const tooltipStyle = computed(() => ({
  left: `${tip.value.x + 14}px`,
  top: `${tip.value.y + 14}px`,
  transform: tip.value.flip ? 'translateX(-100%) translateX(-28px)' : undefined,
}))

const yuan = (n: number) => `¥${Math.round(n).toLocaleString('zh-CN')}`
</script>

<template>
  <section class="overflow-hidden rounded-2xl border border-warm-border bg-white shadow-md">
    <!-- ── 头 ── -->
    <header class="flex items-center justify-between gap-3 border-b border-warm-border px-4 py-3">
      <div class="min-w-0">
        <h3 class="truncate text-[14px] font-bold text-wood-dark">{{ title }}</h3>
        <p class="mt-0.5 text-[11px] text-wood-muted">
          <template v-if="data">
            {{ data.hotspots.length }} 个物品热区 · 悬停查看参考价与购买链接
          </template>
          <template v-else-if="loading">正在渲染…</template>
        </p>
      </div>
      <a
        v-if="data"
        :href="openUrl"
        target="_blank"
        rel="noopener"
        class="flex shrink-0 items-center gap-1.5 rounded-lg border border-warm-border px-2.5 py-1.5 text-[11px] font-medium text-wood transition hover:bg-botanical-surface"
      >
        <AppIcon name="app-window" :size="14" />
        打开原图
      </a>
    </header>

    <!--
      ══════════════════════════════════════════════════════════════
      ⚠️ 这里为什么要拆成左右两列
      ══════════════════════════════════════════════════════════════
      后端渲染的 SVG 是 **1024×1024 的正方形**（`build_projection` 会把短边
      补到 `min_side_px`）。而内容区最宽 1760px —— 按 `width:100%` 铺开的话，
      **高度也跟着被拉到 1700px 左右**，整页冲到 1.85 屏，用户必须滚过一整张
      图才能看到下面的热区清单。

      单纯加 `max-height` 也能压矮，但那会让方形图缩在 1700px 宽的容器中间、
      两侧空出大片底色 —— 那是"空洞"，不是"清晰"。

      所以宽屏（xl 及以上）改成**图占左列、清单占右列**：用横向空间换纵向，
      图仍然是这一屏里最大的东西，清单也一屏可见，两边都不浪费。
      窄屏退回上下堆叠（原本的样子）。

      ⚠️ 热区清单**不能**拆到独立子页去 —— 它和图上热区是**联动**的
      （清单行 `@mouseenter` 高亮图上的热区，反之亦然）。拆开就把
      "点一下看看是哪块"这个动作切断了。
    -->
    <div class="flex flex-col xl:flex-row">
      <!-- 左列：画布 + 画不准的地方 -->
      <div class="min-w-0 xl:flex-1">
    <!-- ── 画布 ── -->
    <div
      ref="canvasEl"
      class="plan-canvas relative bg-warm-bg"
      :class="{ 'is-busy': loading }"
      @mouseover="onOver"
      @mousemove="onMove"
      @mouseleave="clear"
    >
      <!-- SVG 内联：命中测试由浏览器负责，见脚本顶部的说明 -->
      <div v-if="svg" class="plan-svg" v-html="svg" />

      <div v-else-if="loading" class="flex h-64 items-center justify-center">
        <div class="flex items-center gap-2 text-[12px] text-wood-muted">
          <AppIcon name="spinner" :size="16" class="animate-spin" />
          正在渲染矢量图…
        </div>
      </div>

      <div v-else-if="error" class="flex h-64 flex-col items-center justify-center gap-2 px-6 text-center">
        <AppIcon name="warning-circle" :size="24" class="text-accent-red" />
        <p class="text-[12px] text-wood-muted">{{ error }}</p>
      </div>

      <!-- ── 悬停提示卡 ── -->
      <Transition
        enter-active-class="transition duration-100"
        enter-from-class="opacity-0 scale-95"
        leave-active-class="transition duration-75"
        leave-to-class="opacity-0"
      >
        <div
          v-if="hoveredSpot"
          class="pointer-events-none absolute z-20 w-[264px] rounded-xl border border-warm-border bg-white p-3 shadow-lg"
          :style="tooltipStyle"
        >
          <div class="flex items-center gap-1.5">
            <AppIcon
              :name="CATEGORY_ICON[hoveredSpot.category] ?? 'info'"
              :size="14"
              class="text-botanical"
            />
            <span class="text-[12px] font-bold text-wood-dark">{{ hoveredSpot.label }}</span>
          </div>

          <!-- 精确度徽标：形状 + 文案都要跟着精度变（需求文档 2.2.6） -->
          <p
            class="mt-1.5 inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium"
            :class="
              hoveredSpot.precision === 'exact'
                ? 'bg-botanical-light text-botanical'
                : 'border border-dashed border-accent-gold/60 bg-wood-light/60 text-wood'
            "
          >
            {{ PRECISION_LABEL[hoveredSpot.precision] ?? hoveredSpot.precision }}
          </p>

          <template v-if="hoveredSpot.price_range">
            <p class="mt-2 text-[13px] font-bold text-wood-dark">
              {{ yuan(hoveredSpot.price_range[0]) }}–{{ yuan(hoveredSpot.price_range[1]) }}
              <span class="text-[10px] font-normal text-wood-muted">{{ hoveredSpot.unit }}</span>
            </p>
            <p v-if="hoveredSpot.estimate_range" class="mt-0.5 text-[10px] text-wood-muted">
              本区域参考总价
              {{ yuan(hoveredSpot.estimate_range[0]) }}–{{ yuan(hoveredSpot.estimate_range[1]) }}
              <template v-if="hoveredSpot.quantity">
                （{{ hoveredSpot.quantity }}{{ hoveredSpot.category === 'door' ? '樘' : '㎡' }}）
              </template>
            </p>
          </template>
          <p v-else class="mt-2 text-[11px] text-wood-muted">暂无报价数据</p>

          <ul v-if="hoveredSpot.items.length" class="mt-2 space-y-1 border-t border-warm-border pt-2">
            <li
              v-for="it in hoveredSpot.items"
              :key="it.id"
              class="flex items-baseline justify-between gap-2 text-[11px]"
            >
              <span class="flex min-w-0 items-center gap-1">
                <span
                  v-if="it.is_quanyou"
                  class="shrink-0 rounded bg-botanical px-1 text-[9px] font-bold text-white"
                >全友</span>
                <span class="truncate text-wood">{{ it.name }}</span>
              </span>
              <span class="shrink-0 font-medium text-wood-dark">{{ yuan(it.price) }}</span>
            </li>
          </ul>

          <!-- 口径说明：层高是假设值、未扣门窗等，必须说出来 -->
          <p v-if="hoveredSpot.note" class="mt-2 border-t border-warm-border pt-1.5 text-[10px] leading-relaxed text-wood-muted">
            {{ hoveredSpot.note }}
          </p>

          <p v-if="hoveredSpot.search_url" class="mt-1.5 text-[10px] text-botanical">
            点击下方列表可跳转官网查看 →
          </p>
        </div>
      </Transition>
    </div>

    <!-- ── 画不准的地方（不许静默）── -->
    <div v-if="data?.warnings.length" class="border-t border-warm-border bg-wood-light/30 px-4 py-2.5">
      <ul class="space-y-1">
        <li
          v-for="(w, i) in data.warnings"
          :key="i"
          class="flex items-start gap-1.5 text-[11px] text-wood"
        >
          <AppIcon name="info" :size="13" class="mt-0.5 shrink-0 text-accent-gold" />
          <span class="min-w-0">{{ w }}</span>
        </li>
      </ul>
    </div>

      </div>
      <!-- 右列 -->

    <!-- ── 热区清单 ── -->
    <div
      v-if="data && !compact"
      class="scroll-thin border-t border-warm-border xl:max-h-[70vh] xl:w-[360px] xl:shrink-0 xl:overflow-y-auto xl:border-l xl:border-t-0"
    >
      <div class="grid grid-cols-1 gap-px bg-warm-border sm:grid-cols-2 xl:grid-cols-1">
        <a
          v-for="h in hotspots"
          :key="h.index"
          :href="h.search_url || undefined"
          target="_blank"
          rel="noopener"
          class="group flex items-center gap-2 bg-white px-3.5 py-2 transition hover:bg-botanical-surface"
          @mouseenter="hovered = h.index"
          @mouseleave="hovered = -1"
        >
          <AppIcon
            :name="CATEGORY_ICON[h.category] ?? 'info'"
            :size="14"
            class="shrink-0 text-wood-muted group-hover:text-botanical"
          />
          <span class="min-w-0 flex-1 truncate text-[12px] text-wood-dark">{{ h.label }}</span>
          <span v-if="h.price_range" class="shrink-0 text-[11px] font-medium text-wood">
            {{ yuan(h.price_range[0]) }} 起
          </span>
          <AppIcon
            v-if="h.search_url"
            name="arrow-right"
            :size="12"
            class="shrink-0 text-wood-muted opacity-0 transition group-hover:opacity-100"
          />
        </a>
      </div>
    </div>
    </div>

    <!-- ── 演示数据声明，**必须展示**（需求文档 5.3 / R-09）── -->
    <p
      v-if="data?.disclaimer"
      class="border-t border-warm-border bg-warm-sidebar px-4 py-2 text-[10px] leading-relaxed text-wood-muted"
    >
      {{ data.disclaimer }}
    </p>
  </section>
</template>

<style scoped>
/**
 * 热区的样式。
 *
 * ⚠️ 必须用 `:deep()`：SVG 是 `v-html` 进来的，Vue 的 scoped 属性
 * **不会**加到这份内容上，普通选择器选不中它。
 *
 * 后端把热区画成 `fill="transparent"` + `pointer-events="all"` ——
 * 体积为零但可命中。视觉反馈全在这里给：
 */
.plan-svg :deep(.hotspot) {
  fill: transparent;
  cursor: pointer;
  transition: fill 120ms ease;
}
.plan-svg :deep(.hotspot:hover) {
  fill: rgb(74 124 89 / 0.16);
}
/* room_level 用虚线区域区分（AC-09 要求"前端须以视觉样式区分"） */
.plan-svg :deep(.hotspot.hotspot-room_level) {
  stroke: rgb(201 169 97 / 0.8);
  stroke-width: 2;
  stroke-dasharray: 6 4;
}
.plan-svg :deep(svg) {
  display: block;
  width: 100%;
  height: auto;
  /*
   * ⚠️ **必须封顶。** 后端渲染的是 1024×1024 的正方形（短边会被
   * `build_projection` 补到 `min_side_px`），而内容区最宽 1760px ——
   * 只有 `width:100%` 时高度会一路涨到 1700px，整页 1.85 屏。
   *
   * 根元素带 `viewBox` 且有确定的内在比例，所以浏览器的
   * min/max 规则会**按比例反算宽度**（不是压扁），配 `margin: auto`
   * 居中即可。实测：加了这一条之后整页从 2216px 降到一屏内。
   */
  max-height: 70vh;
  margin: 0 auto;
}
/* 渲染中不要闪旧图 */
.plan-canvas.is-busy .plan-svg {
  opacity: 0.4;
}
</style>
