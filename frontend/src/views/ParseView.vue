<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import DegradedNotice from '@/components/DegradedNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import PhaseProgress from '@/components/PhaseProgress.vue'
import PlanViewer from '@/components/PlanViewer.vue'
import SceneViewer from '@/components/SceneViewer.vue'
import { parseLayout } from '@/api'
import { toast } from '@/utils/toast'
import { messageOf } from '@/api/client'
import type { DiagnosisItem, ParseResult } from '@/api/types'
import { useTaskPolling } from '@/composables/useTaskPolling'
import { useTaskStore } from '@/stores/task'

/**
 * 户型解析。
 *
 * 这一页落实了需求文档的几条硬性规定，每一条都写在对应位置的注释里：
 *  · 轮询节奏 2.2.4（在 useTaskPolling 里）
 *  · `mode=degraded_basic` 的三条强制呈现（本文件）
 *  · 能力守卫：数据不支撑时禁用下游入口（本文件）
 *  · 降级提示可见（DegradedNotice）
 */
const router = useRouter()
const route = useRoute()
const tasks = useTaskStore()
const poll = useTaskPolling<ParseResult>()

//: 模板里 `ref="fileInput"` 绑的就是它。
// ⚠️ **这个声明不能漏。** 漏掉时模板里 `fileInput` 不存在，
// `@click="fileInput?.click()"` 里的 ?. 会安静地短路 ——
// 表现为"点上传区没反应"，不报错、不警告。
// （第一版用的是 `$refs.fileInput`，同样取不到，问题一模一样。）
const fileInput = ref<HTMLInputElement | null>(null)
const file = ref<File | null>(null)
const previewUrl = ref('')
const imageDataUri = ref('')
const detailLevel = ref<'basic' | 'full'>('full')
/** 隐私模式：勾上后图像绝不出本机，只走本地 Ollama */
const preferLocal = ref(false)
const submitting = ref(false)
const submitError = ref('')
const dragging = ref(false)

const result = computed(() => poll.status.value?.result ?? null)
const layout = computed(() => result.value?.layout ?? null)
const diagnosis = computed(() => result.value?.diagnosis ?? null)

/** 图片质量预检结果（AC-27）。不合格时任务失败，但 result 里仍然带着它。 */
const precheck = computed(() => result.value?.precheck ?? null)

/**
 * 被预检拦下的情况。
 *
 * 这时 `result` 只有 `precheck`，`layout` / `diagnosis` 全是 null ——
 * 如果照常渲染下面的结果面板，用户会看到一屏空框，而真正的原因
 * （"分辨率过低"）藏在一条 toast 里飘过去。
 * 所以这一态要单独渲染。
 */
const rejectedByPrecheck = computed(
  () => poll.status.value?.status === 'failed' && precheck.value != null && !precheck.value.ok,
)

/**
 * ⚠️ `mode === 'degraded_basic'` 时，**只有 `rooms[].name` 是有效的**。
 * 需求文档 2.2.4 规定前端必须做三件事，见模板里的 `v-if="isDegradedBasic"`。
 */
const isDegradedBasic = computed(() => layout.value?.mode === 'degraded_basic')

/** 能力守卫给的结果。后端在入口就拦，前端据此置灰 —— 两道都要有。 */
const capabilities = computed(() => {
  const fromResult = result.value?.capabilities
  const fromLayout = layout.value?.capabilities
  return fromResult ?? fromLayout ?? null
})

const canGenerate = computed(() => {
  if (isDegradedBasic.value) return false // 降级时结构字段全空，生成无意义
  if (!layout.value) return false
  const c = capabilities.value
  if (c && typeof c.can_generate_plan === 'boolean') return c.can_generate_plan
  // 后端没给能力报告时，按"有没有房间和面积"自行判断——
  // 这不是替后端做决定，只是避免界面出现一个必然失败的可点按钮。
  return (layout.value.rooms?.length ?? 0) > 0 && (layout.value.total_area ?? 0) > 0
})

const blockedReason = computed(() => {
  if (isDegradedBasic.value) return '当前为降级解析结果，结构与面积字段不可用'
  const c = capabilities.value
  if (c?.suggestion) return String(c.suggestion)
  if (!(layout.value?.total_area ?? 0)) return '缺少户型总面积，无法估算造价'
  return ''
})

function pick(f: File) {
  if (!f.type.startsWith('image/')) {
    toast.error('请选择图片文件（PNG / JPG / WebP）')
    return
  }
  // 20MB 上限不是随便定的：base64 会膨胀约 33%，再叠一次 JSON 转义，
  // 40MB 的请求体足以让某些中间层直接 413。
  if (f.size > 20 * 1024 * 1024) {
    toast.error('图片超过 20MB，请先压缩')
    return
  }
  file.value = f
  const reader = new FileReader()
  reader.onload = () => {
    imageDataUri.value = String(reader.result)
    previewUrl.value = imageDataUri.value
  }
  reader.readAsDataURL(f)
}

function onDrop(e: DragEvent) {
  dragging.value = false
  const f = e.dataTransfer?.files?.[0]
  if (f) pick(f)
}

function onChange(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (f) pick(f)
}

function clearFile() {
  file.value = null
  previewUrl.value = ''
  imageDataUri.value = ''
  poll.stop()
}

async function submit() {
  if (!imageDataUri.value) {
    toast.warning('请先选择一张户型图')
    return
  }
  submitting.value = true
  submitError.value = ''
  try {
    // 后端接受 data URI 或裸 base64。这里送 data URI —— 它自带 MIME，
    // 后端不必猜格式。
    const created = await parseLayout({
      image: imageDataUri.value,
      image_media_type: file.value?.type || 'image/png',
      detail_level: detailLevel.value,
      prefer_local: preferLocal.value,
    })
    tasks.track({ taskId: created.task_id, kind: 'parse' })
    // 把 task_id 写进地址栏：刷新页面后还能接回同一个任务（结果在后端留 1 小时）
    router.replace({ query: { ...route.query, task: created.task_id } })

    const snap = await poll.start(created.task_id, created.estimated_seconds)
    if (poll.timedOut.value) {
      toast.warning('轮询超时（120 秒）。任务可能仍在后台执行，可用 trace_id 排查。')
      return
    }
    if (!snap) return

    tasks.update(created.task_id, {
      status: snap.status,
      phaseText: snap.phase_text,
      degraded: snap.degraded,
      summary: snap.result?.layout
        ? `${snap.result.layout.rooms?.length ?? 0} 个房间 · ${snap.result.layout.total_area ?? 0} ㎡`
        : snap.error || '',
    })

    if (snap.status === 'failed') {
      toast.error(snap.error || '解析失败')
    } else if (snap.degraded) {
      toast.warning('解析已完成，但走了降级路径，请查看提示')
    } else {
      toast.success('解析完成')
    }
  } catch (e) {
    submitError.value = messageOf(e)
    toast.error(submitError.value)
  } finally {
    submitting.value = false
  }
}

/** 从地址栏的 task_id 恢复：刷新页面不该丢掉正在跑的任务。 */
onMounted(async () => {
  const taskId = String(route.query.task || '')
  if (!taskId) return
  const snap = await poll.start(taskId)
  if (snap?.result) {
    tasks.track({
      taskId,
      kind: 'parse',
      summary: snap.result.layout
        ? `${snap.result.layout.rooms?.length ?? 0} 个房间 · ${snap.result.layout.total_area ?? 0} ㎡`
        : '',
    })
    tasks.update(taskId, { status: snap.status, phaseText: snap.phase_text })
  }
})

// ── 展示辅助 ──────────────────────────────────────────────

const ROOM_TYPE_LABEL: Record<string, string> = {
  living_room: '客厅',
  bedroom: '卧室',
  kitchen: '厨房',
  bathroom: '卫生间',
  dining_room: '餐厅',
  study: '书房',
  balcony: '阳台',
  storage: '储藏',
  other: '其它',
}
const roomType = (t: string) => ROOM_TYPE_LABEL[t] ?? t

const DIMS: { key: keyof NonNullable<typeof diagnosis.value>; label: string; icon: string }[] = [
  { key: 'lighting', label: '采光', icon: 'sparkle' },
  { key: 'ventilation', label: '通风', icon: 'leaf' },
  { key: 'circulation', label: '动线', icon: 'arrow-right' },
  { key: 'space_utilization', label: '空间利用率', icon: 'layout' },
  { key: 'green_score', label: '环保', icon: 'plant' },
]

function dimOf(key: string): DiagnosisItem | null {
  const d = diagnosis.value as unknown as Record<string, DiagnosisItem> | null
  return d?.[key] ?? null
}

/** 分数配色。不做"低于 60 就是红的"这种武断判断——只是视觉分档。 */
function scoreTone(score: number) {
  if (score >= 80) return 'text-botanical'
  if (score >= 60) return 'text-wood'
  return 'text-accent-gold'
}

function goGenerate() {
  const id = result.value?.layout_id
  if (!id) return
  router.push({ path: '/generate', query: { layout: id } })
}
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6">
    <PageHeader
      :breadcrumb="['全友·智绘家', '户型解析']"
      title="户型图解析与重建"
      :status="
        poll.status.value
          ? { icon: 'spinner', text: poll.status.value.phase_text, tone: 'botanical' }
          : { icon: 'blueprint', text: '等待上传', tone: 'wood' }
      "
      :code="poll.status.value ? `TRACE: ${poll.status.value.trace_id.slice(0, 12)}` : ''"
    />

    <div class="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
      <!-- ══ 左：上传与选项 ══ -->
      <section class="flex flex-col gap-4">
        <div class="card p-4">
          <!-- 拖放区 -->
          <div
            class="relative flex min-h-[240px] cursor-pointer flex-col items-center justify-center rounded-2xl border border-dashed p-6 text-center transition-all"
            :class="
              dragging
                ? 'border-botanical bg-botanical-surface'
                : 'border-warm-border bg-white hover:border-botanical/50 hover:bg-botanical-surface/40'
            "
            @click="fileInput?.click()"
            @dragover.prevent="dragging = true"
            @dragleave.prevent="dragging = false"
            @drop.prevent="onDrop"
          >
            <input
              ref="fileInput"
              class="hidden"
              type="file"
              accept="image/png,image/jpeg,image/webp"
              @change="onChange"
            />

            <template v-if="!previewUrl">
              <span
                class="flex h-14 w-14 items-center justify-center rounded-2xl border border-botanical/20 bg-botanical-light text-botanical"
              >
                <AppIcon name="upload-simple" :size="26" />
              </span>
              <p class="mt-3 text-[14px] font-semibold text-wood-dark">
                拖入户型图，或点击选择
              </p>
              <p class="mt-1 text-[12px] leading-relaxed text-wood-muted">
                支持 PNG / JPG / WebP，单张不超过 20MB
              </p>
              <p class="mt-3 max-w-xs text-[11px] leading-relaxed text-wood-muted/80">
                建议使用带尺寸标注的原始户型图。截图或手绘草图也能识别，但置信度会降低。
              </p>
            </template>

            <template v-else>
              <img
                :src="previewUrl"
                alt="待解析的户型图"
                class="max-h-[280px] w-auto rounded-xl border border-warm-border object-contain"
              />
              <div class="mt-3 flex items-center gap-2">
                <span class="tag">{{ file?.name }}</span>
                <span class="tag">{{ ((file?.size ?? 0) / 1024 / 1024).toFixed(2) }} MB</span>
              </div>
            </template>
          </div>

          <div v-if="previewUrl" class="mt-3 flex justify-end">
            <button class="btn-ghost px-3 py-1.5" type="button" @click="clearFile">
              <AppIcon name="trash" :size="15" />
              <span>换一张</span>
            </button>
          </div>
        </div>

        <!-- 解析选项 -->
        <div class="card p-4">
          <h2 class="mb-3 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
            <AppIcon name="gear" :size="16" class="text-botanical" />
            <span>解析选项</span>
          </h2>

          <div class="flex flex-col gap-3">
            <div>
              <p class="mb-1.5 text-[12px] font-semibold text-wood-dark">详细程度</p>
              <div class="flex gap-2">
                <button
                  v-for="opt in [
                    { v: 'full', label: '完整结构', hint: '房间/墙体/门窗/尺寸' },
                    { v: 'basic', label: '仅房间名', hint: '更快，结构字段留空' },
                  ]"
                  :key="opt.v"
                  class="flex-1 rounded-xl border px-3 py-2 text-left transition-all"
                  :class="
                    detailLevel === opt.v
                      ? 'border-botanical bg-botanical-light'
                      : 'border-warm-border bg-white hover:border-wood/30'
                  "
                  type="button"
                  @click="detailLevel = opt.v as 'full' | 'basic'"
                >
                  <span
                    class="block text-[12px] font-semibold"
                    :class="detailLevel === opt.v ? 'text-botanical' : 'text-wood-dark'"
                  >
                    {{ opt.label }}
                  </span>
                  <span class="mt-0.5 block text-[10px] text-wood-muted">{{ opt.hint }}</span>
                </button>
              </div>
            </div>

            <!-- 隐私模式 -->
            <label
              class="flex cursor-pointer items-start gap-3 rounded-xl border border-warm-border bg-white p-3 transition-colors hover:border-botanical/40"
            >
              <input v-model="preferLocal" class="mt-0.5 accent-[#4A7C59]" type="checkbox" />
              <span class="min-w-0">
                <span class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                  <AppIcon name="shield-check" :size="14" class="text-botanical" />
                  <span>本地隐私模式</span>
                </span>
                <span class="mt-0.5 block text-[11px] leading-relaxed text-wood-muted">
                  开启后图像**绝不出本机**，只走本地 Ollama 模型。代价是识别精度低于云端模型，
                  复杂户型可能只出房间名。
                </span>
              </span>
            </label>
          </div>

          <button
            class="btn-primary mt-4 w-full py-2.5"
            type="button"
            :disabled="!imageDataUri || poll.running.value || submitting"
            @click="submit"
          >
            <AppIcon :name="poll.running.value ? 'spinner' : 'sparkle'" :size="17" />
            <span>{{ poll.running.value ? '解析中…' : '开始解析' }}</span>
          </button>

          <p
            v-if="!poll.running.value"
            class="mt-2 text-center text-[11px] text-wood-muted/80"
          >
            实测耗时 33–48 秒，取决于图片复杂度
          </p>
        </div>

        <!-- 进度 -->
        <PhaseProgress
          v-if="poll.status.value"
          :text="poll.status.value.phase_text"
          :progress="poll.status.value.progress"
          :elapsed-ms="poll.elapsedMs.value"
          :over-estimate="poll.overEstimate.value"
          :log="poll.phaseLog.value"
          :status="poll.status.value.status"
        />
      </section>

      <!-- ══ 右：解析结果 ══ -->
      <section class="flex flex-col gap-4">
        <!-- 未开始时 -->
        <div v-if="!result" class="card">
          <div class="flex flex-col items-center justify-center px-6 py-16 text-center">
            <span
              class="flex h-14 w-14 items-center justify-center rounded-2xl border border-botanical/20 bg-botanical-light text-botanical"
            >
              <AppIcon name="blueprint" :size="26" />
            </span>
            <h3 class="mt-3 font-serif text-[17px] font-semibold text-wood-dark">
              等待解析结果
            </h3>
            <p class="mt-1.5 max-w-md text-[12px] leading-relaxed text-wood-muted">
              解析完成后，这里会展示识别出的房间、墙体、门窗与尺寸，
              以及采光、通风、动线、空间利用率、环保五个维度的诊断。
            </p>
          </div>
        </div>

        <!-- ══ 被图片预检拦下：给一屏专门的说明，不是空面板 ══ -->
        <div v-else-if="rejectedByPrecheck" class="card p-5">
          <div class="flex items-start gap-3">
            <span
              class="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-accent-red/25 bg-accent-red/5 text-accent-red"
            >
              <AppIcon name="warning-circle" :size="22" />
            </span>
            <div class="min-w-0">
              <h3 class="font-serif text-[17px] font-bold text-wood-dark">
                图片未通过质量预检
              </h3>
              <p class="mt-1 text-[12px] leading-relaxed text-wood-muted">
                不合格的图片在**进入模型之前**就被本地拦下了 —— 没有消耗任何模型调用。
                换一张更清晰的户型图即可。
              </p>
            </div>
          </div>

          <ul class="mt-4 space-y-2">
            <li
              v-for="(r, i) in precheck?.rejections ?? []"
              :key="i"
              class="flex items-start gap-2 rounded-xl border border-accent-red/25 bg-accent-red/5 p-3"
            >
              <AppIcon name="x-circle" :size="15" class="mt-0.5 shrink-0 text-accent-red" />
              <span class="text-[12px] leading-relaxed text-wood">{{ r }}</span>
            </li>
          </ul>

          <!--
            只显示实际测得的值。**不在这里复述阈值** ——
            上面每条 rejections 的文案里已经带了阈值（"短边 150px < 下限 400px"），
            而阈值是后端配置。前端再写一份就是第二处真相，改了后端这里不会跟着变。
          -->
          <div class="mt-4 grid grid-cols-2 gap-3">
            <div class="rounded-xl border border-warm-border bg-warm-sidebar/50 p-3">
              <p class="text-[10px] font-semibold uppercase text-wood-muted">实际尺寸</p>
              <p class="num mt-0.5 text-[15px] font-bold text-wood-dark">
                {{ precheck?.width }}×{{ precheck?.height }}
              </p>
            </div>
            <div class="rounded-xl border border-warm-border bg-warm-sidebar/50 p-3">
              <p class="text-[10px] font-semibold uppercase text-wood-muted">清晰度</p>
              <p class="num mt-0.5 text-[15px] font-bold text-wood-dark">
                {{ precheck?.blur_score != null ? precheck.blur_score.toFixed(0) : '—' }}
              </p>
            </div>
          </div>

          <button class="btn-primary mt-4 w-full py-2.5" type="button" @click="clearFile">
            <AppIcon name="upload-simple" :size="16" />
            <span>换一张图</span>
          </button>
        </div>

        <template v-else>
          <!-- 降级提示：必须有，且不可关闭 -->
          <DegradedNotice v-if="result.degraded" :reasons="result.degrade_reasons" />

          <!-- 预检通过时的信息条 + 未阻断的提醒 -->
          <div
            v-if="precheck?.ok"
            class="rounded-xl border border-warm-border bg-warm-sidebar/50 p-3"
          >
            <div class="flex items-center gap-2 text-[11px] text-wood-muted">
              <AppIcon name="check-circle" :size="14" class="shrink-0 text-botanical" />
              <span>
                图片预检通过 · <span class="num">{{ precheck.width }}×{{ precheck.height }}</span>
                <template v-if="precheck.blur_score != null">
                  · 清晰度 <span class="num">{{ precheck.blur_score.toFixed(0) }}</span>
                </template>
              </span>
            </div>
            <ul v-if="precheck.warnings?.length" class="mt-1.5 space-y-0.5 pl-5">
              <li
                v-for="(w, i) in precheck.warnings"
                :key="i"
                class="flex items-start gap-1.5 text-[10px] leading-relaxed text-wood-muted"
              >
                <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent-gold" />
                <span>{{ w }}</span>
              </li>
            </ul>
          </div>

          <!-- degraded_basic 的强制呈现 -->
          <div
            v-if="isDegradedBasic"
            class="rounded-xl border border-accent-red/30 bg-accent-red/5 p-3.5"
          >
            <div class="flex items-start gap-2.5">
              <AppIcon name="warning-circle" :size="20" class="mt-0.5 shrink-0 text-accent-red" />
              <div class="min-w-0">
                <p class="text-[13px] font-bold text-wood-dark">
                  仅识别出房间名，其余结构字段为空 —— 需人工复核
                </p>
                <p class="mt-1 text-[11px] leading-relaxed text-wood-muted">
                  {{
                    layout?.safety_notice ||
                    '本地兜底模型只输出了房间名称，墙体、门窗、尺寸与面积均不可用。所有依赖这些数据的后续操作已被禁用。'
                  }}
                </p>
              </div>
            </div>
          </div>

          <!-- 户型概览 -->
          <div class="card relative overflow-hidden p-4">
            <!-- degraded_basic 时加半透明水印（需求文档 2.2.4 要求） -->
            <div
              v-if="isDegradedBasic"
              class="pointer-events-none absolute inset-0 z-10 flex items-center justify-center"
            >
              <span
                class="select-none -rotate-[18deg] text-[38px] font-bold tracking-widest text-accent-red/10"
              >
                需人工复核
              </span>
            </div>

            <div class="mb-3 flex items-center justify-between">
              <h2 class="flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
                <AppIcon name="house-line" :size="17" class="text-botanical" />
                <span>户型概览</span>
              </h2>
              <div class="flex items-center gap-2">
                <span v-if="layout?.mode" class="tag">
                  mode: {{ layout.mode }}
                </span>
                <span class="tag">
                  置信度
                  <span class="num font-semibold text-wood-dark">
                    {{ Math.round((layout?.confidence ?? 0) * 100) }}%
                  </span>
                </span>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3">
                <p class="text-[10px] font-semibold uppercase text-wood-muted">房间</p>
                <p class="num mt-0.5 text-[20px] font-bold text-wood-dark">
                  {{ layout?.rooms?.length ?? 0 }}
                </p>
              </div>
              <div class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3">
                <p class="text-[10px] font-semibold uppercase text-wood-muted">总面积</p>
                <p class="num mt-0.5 text-[20px] font-bold text-wood-dark">
                  {{ (layout?.total_area ?? 0).toFixed(1) }}
                  <span class="text-[12px] font-medium">㎡</span>
                </p>
              </div>
              <div class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3">
                <p class="text-[10px] font-semibold uppercase text-wood-muted">门窗</p>
                <p class="num mt-0.5 text-[20px] font-bold text-wood-dark">
                  {{ layout?.doors?.length ?? 0 }}
                  <span class="text-[12px] font-medium text-wood-muted">/</span>
                  {{ layout?.windows?.length ?? 0 }}
                </p>
              </div>
              <div class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3">
                <p class="text-[10px] font-semibold uppercase text-wood-muted">入户朝向</p>
                <p class="mt-0.5 text-[20px] font-bold text-wood-dark">
                  {{ layout?.entrance_orientation || '未知' }}
                </p>
              </div>
            </div>

            <!-- 房间清单 -->
            <div v-if="layout?.rooms?.length" class="mt-4">
              <p class="mb-2 text-[12px] font-semibold text-wood-dark">房间清单</p>
              <div class="grid grid-cols-2 gap-2 sm:grid-cols-3">
                <div
                  v-for="(r, i) in layout.rooms"
                  :key="r.name + i"
                  class="rounded-xl border border-warm-border bg-white p-2.5"
                >
                  <div class="flex items-center justify-between">
                    <span class="truncate text-[12px] font-semibold text-wood-dark">
                      {{ r.name }}
                    </span>
                    <span class="tag shrink-0 !px-1.5 !text-[10px]">{{ roomType(r.type) }}</span>
                  </div>
                  <p class="num mt-1 text-[11px] text-wood-muted">
                    <!-- 降级时面积恒为 0，直接说"不可用"而不是显示 0.0 ㎡ -->
                    <template v-if="isDegradedBasic || !r.area">面积不可用</template>
                    <template v-else>{{ r.area.toFixed(1) }} ㎡</template>
                    <span v-if="r.orientation && r.orientation !== 'unknown'">
                      · 朝向 {{ r.orientation }}
                    </span>
                  </p>
                </div>
              </div>
            </div>

            <!-- 数据缺口：如实说，不藏 -->
            <div
              v-if="layout?.data_gaps?.length"
              class="mt-4 rounded-xl border border-warm-border bg-warm-sidebar/50 p-3"
            >
              <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                <AppIcon name="info" :size="14" class="text-wood-muted" />
                <span>数据缺口</span>
              </p>
              <ul class="mt-1.5 space-y-1">
                <li
                  v-for="(g, i) in layout.data_gaps"
                  :key="i"
                  class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
                >
                  <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-wood-muted/50" />
                  <span>{{ g }}</span>
                </li>
              </ul>
            </div>
          </div>

          <!--
            矢量户型图 + 物品热区（AC-07 / AC-09 / AC-21）。

            放在房间清单**之前** —— 这是整页最直观的一块，用户解析完
            第一件想看的不是房间面积表，而是"我家被识别成什么样了"。

            ⚠️ 降级户型也照常挂载：后端会渲染出一张带说明的空状态图
            （"未识别出房间轮廓，建议换一张更清晰的"），比整块消失更可操作。
            AC-07 的字面要求就是"**任何**户型都能渲出"。
          -->
          <PlanViewer
            v-if="result.layout_id"
            :layout-id="result.layout_id"
            title="户型矢量图 · 物品热区"
          />

          <!--
            3D 户型漫游（第一人称行走）。

            放在平面图**之后** —— 平面图回答"识别成什么样"，3D 回答
            "走进去是什么感觉"。顺序反过来的话，用户会先被 3D 挡住，
            而 3D 的几何完全依赖平面图那份解析结果。
          -->
          <SceneViewer
            v-if="result.layout_id"
            :layout-id="result.layout_id"
            height="540px"
          />

          <!-- 五维诊断 -->
          <div v-if="diagnosis" class="card p-4">
            <div class="mb-3 flex items-center justify-between">
              <h2 class="flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
                <AppIcon name="chart-bar" :size="17" class="text-botanical" />
                <span>户型诊断</span>
              </h2>
              <div class="flex items-center gap-2">
                <span class="text-[11px] text-wood-muted">综合</span>
                <span class="num text-[18px] font-bold" :class="scoreTone(diagnosis.overall_score)">
                  {{ diagnosis.overall_score.toFixed(1) }}
                </span>
              </div>
            </div>

            <div class="flex flex-col gap-2">
              <div
                v-for="d in DIMS"
                :key="d.key"
                class="rounded-xl border border-warm-border bg-white p-3"
              >
                <div class="flex items-center justify-between">
                  <span class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                    <AppIcon :name="d.icon" :size="14" class="text-botanical" />
                    <span>{{ d.label }}</span>
                  </span>
                  <!-- "数据不足"是合法结论，不能渲染成 0 分 -->
                  <span
                    v-if="dimOf(d.key)?.insufficient_data"
                    class="rounded border border-warm-border bg-warm-sidebar px-2 py-0.5 text-[10px] font-semibold text-wood-muted"
                  >
                    数据不足
                  </span>
                  <span v-else class="num text-[14px] font-bold" :class="scoreTone(dimOf(d.key)?.score ?? 0)">
                    {{ (dimOf(d.key)?.score ?? 0).toFixed(1) }}
                  </span>
                </div>

                <div class="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-warm-border">
                  <div
                    class="h-full rounded-full bg-botanical transition-[width] duration-500"
                    :style="{ width: `${dimOf(d.key)?.insufficient_data ? 0 : (dimOf(d.key)?.score ?? 0)}%` }"
                  />
                </div>

                <ul
                  v-if="dimOf(d.key)?.issues?.length"
                  class="mt-2 flex flex-col gap-0.5"
                >
                  <li
                    v-for="(s, i) in dimOf(d.key)?.issues"
                    :key="i"
                    class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
                  >
                    <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-wood-muted/50" />
                    <span>{{ s }}</span>
                  </li>
                </ul>
              </div>
            </div>

            <p
              v-if="diagnosis.summary"
              class="mt-3 rounded-xl border border-warm-border bg-warm-sidebar/50 p-3 text-[12px] leading-relaxed text-wood"
            >
              {{ diagnosis.summary }}
            </p>

            <!-- 承重墙警告：这条不能折叠 -->
            <div
              v-if="diagnosis.load_bearing_warning?.length"
              class="mt-3 rounded-xl border border-accent-red/30 bg-accent-red/5 p-3"
            >
              <p class="flex items-center gap-1.5 text-[12px] font-bold text-wood-dark">
                <AppIcon name="warning" :size="14" class="text-accent-red" />
                <span>承重结构提示</span>
              </p>
              <ul class="mt-1.5 space-y-1">
                <li
                  v-for="(w, i) in diagnosis.load_bearing_warning"
                  :key="i"
                  class="text-[11px] leading-relaxed text-wood"
                >
                  {{ w }}
                </li>
              </ul>
            </div>
          </div>

          <!-- 下一步：能力守卫 -->
          <div class="card p-4">
            <h2 class="mb-3 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
              <AppIcon name="arrow-right" :size="16" class="text-botanical" />
              <span>下一步</span>
            </h2>

            <div class="flex flex-wrap gap-2">
              <button
                class="btn-primary px-4 py-2"
                type="button"
                :disabled="!canGenerate"
                :title="canGenerate ? '' : blockedReason"
                @click="goGenerate"
              >
                <AppIcon name="sparkle" :size="16" />
                <span>生成装修方案</span>
              </button>
              <button class="btn-ghost px-4 py-2" type="button" @click="router.push('/materials')">
                <AppIcon name="currency-cny" :size="16" class="text-botanical" />
                <span>查材料价格</span>
              </button>
            </div>

            <!-- 为什么不能点，直说。前端置灰是体验，后端也会拦（两道都要有） -->
            <p
              v-if="!canGenerate && blockedReason"
              class="mt-2.5 flex items-start gap-1.5 rounded-xl border border-warm-border bg-warm-sidebar/60 p-2.5 text-[11px] leading-relaxed text-wood-muted"
            >
              <AppIcon name="info" :size="13" class="mt-0.5 shrink-0" />
              <span>{{ blockedReason }}</span>
            </p>
          </div>

          <!-- 执行轨迹 -->
          <div v-if="result.trace?.length" class="card p-4">
            <h2 class="mb-2 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
              <AppIcon name="list-checks" :size="16" class="text-botanical" />
              <span>执行轨迹</span>
              <span class="tag">{{ result.trace.length }} 步</span>
            </h2>
            <ul class="flex flex-col gap-1">
              <li
                v-for="(t, i) in result.trace"
                :key="i"
                class="flex items-center gap-2 rounded-lg bg-warm-sidebar/40 px-2.5 py-1.5 font-mono text-[11px]"
              >
                <span class="text-wood-muted">{{ String(i + 1).padStart(2, '0') }}</span>
                <span class="min-w-0 flex-1 truncate text-wood">
                  {{ t.agent || t.step || '—' }}
                </span>
                <span v-if="t.elapsed_ms" class="num text-wood-muted">{{ t.elapsed_ms }}ms</span>
              </li>
            </ul>
          </div>

          <!-- 错误 -->
          <div
            v-if="result.errors?.length"
            class="rounded-xl border border-accent-red/30 bg-accent-red/5 p-3.5"
          >
            <p class="flex items-center gap-1.5 text-[12px] font-bold text-wood-dark">
              <AppIcon name="bug" :size="14" class="text-accent-red" />
              <span>执行中的错误（{{ result.errors.length }}）</span>
            </p>
            <ul class="mt-1.5 space-y-1">
              <li
                v-for="(e, i) in result.errors"
                :key="i"
                class="break-words font-mono text-[11px] leading-relaxed text-wood"
              >
                [{{ e.agent || '—' }}] {{ e.message }}
              </li>
            </ul>
          </div>
        </template>
      </section>
    </div>
  </main>
</template>
