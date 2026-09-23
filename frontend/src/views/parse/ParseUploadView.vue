<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import { parseLayout } from '@/api'
import { messageOf } from '@/api/client'
import { toast } from '@/utils/toast'
import { useParseSession } from '@/composables/useParseSession'
import { useTaskStore } from '@/stores/task'

/**
 * 户型解析 · 上传解析（`/parse`）。
 *
 * 模块首页。工作只有一件：把图交上去。解析结果不在这里展示 ——
 * 它属于「识别总览 / 户型矢量图 / 3D 漫游 / 户型诊断」四个子页。
 * 这一页在拿到结果后只留一张**指路卡**，把用户送过去。
 */
const router = useRouter()
const route = useRoute()
const tasks = useTaskStore()
const s = useParseSession()

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
  s.poll.stop()
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

    const snap = await s.poll.start(created.task_id, created.estimated_seconds)
    if (s.poll.timedOut.value) {
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
      toast.success('解析完成，去「识别总览」看看')
      // 解析成功的**唯一**一次自动跳转：用户刚交完图，
      // 接下来想看的就是识别出了什么。
      router.push('/parse/overview')
    }
  } catch (e) {
    submitError.value = messageOf(e)
    toast.error(submitError.value)
  } finally {
    submitting.value = false
  }
}

/** 有结果时露出的指路卡。不是重复展示结果，只是导航。 */
const resultLinks = computed(() => [
  { to: '/parse/overview', icon: 'house-line', label: '识别总览', hint: '房间清单与面积' },
  { to: '/parse/drawing', icon: 'layout', label: '户型矢量图', hint: '2D 图与物品热区' },
  { to: '/parse/walkthrough', icon: 'cube', label: '3D 漫游', hint: '第一人称走进去' },
  { to: '/parse/diagnosis', icon: 'chart-bar', label: '户型诊断', hint: '五维评分' },
])
</script>

<template>
  <div class="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
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
          :disabled="!imageDataUri || s.poll.running.value || submitting"
          @click="submit"
        >
          <AppIcon :name="s.poll.running.value ? 'spinner' : 'sparkle'" :size="17" />
          <span>{{ s.poll.running.value ? '解析中…' : '开始解析' }}</span>
        </button>

        <p v-if="!s.poll.running.value" class="mt-2 text-center text-[11px] text-wood-muted/80">
          实测耗时 33–48 秒，取决于图片复杂度
        </p>
      </div>
    </section>

    <!-- ══ 右：状态 / 指路 ══ -->
    <section class="flex flex-col gap-4">
      <!-- 被图片预检拦下：给一屏专门的说明，不是空面板 -->
      <div v-if="s.rejectedByPrecheck.value" class="card p-5">
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
            v-for="(r, i) in s.precheck.value?.rejections ?? []"
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
              {{ s.precheck.value?.width }}×{{ s.precheck.value?.height }}
            </p>
          </div>
          <div class="rounded-xl border border-warm-border bg-warm-sidebar/50 p-3">
            <p class="text-[10px] font-semibold uppercase text-wood-muted">清晰度</p>
            <p class="num mt-0.5 text-[15px] font-bold text-wood-dark">
              {{ s.precheck.value?.blur_score != null ? s.precheck.value.blur_score.toFixed(0) : '—' }}
            </p>
          </div>
        </div>

        <button class="btn-primary mt-4 w-full py-2.5" type="button" @click="clearFile">
          <AppIcon name="upload-simple" :size="16" />
          <span>换一张图</span>
        </button>
      </div>

      <!-- 已有结果：只做指路，不重复展示 -->
      <div v-else-if="s.result.value" class="card p-4">
        <div class="mb-3 flex items-center justify-between">
          <h2 class="flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
            <AppIcon name="check-circle" :size="17" class="text-botanical" />
            <span>识别完成</span>
          </h2>
          <span class="tag">
            <span class="num">{{ s.layout.value?.rooms?.length ?? 0 }}</span> 个房间
          </span>
        </div>

        <div class="grid grid-cols-2 gap-2">
          <RouterLink
            v-for="l in resultLinks"
            :key="l.to"
            :to="l.to"
            class="card-hover flex items-start gap-2.5 rounded-xl border border-warm-border bg-white p-3"
          >
            <span
              class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-botanical/20 bg-botanical-light text-botanical"
            >
              <AppIcon :name="l.icon" :size="18" />
            </span>
            <span class="min-w-0">
              <span class="block truncate text-[12px] font-semibold text-wood-dark">
                {{ l.label }}
              </span>
              <span class="mt-0.5 block truncate text-[10px] text-wood-muted">{{ l.hint }}</span>
            </span>
          </RouterLink>
        </div>

        <div class="mt-3 flex justify-end">
          <button class="btn-ghost px-3 py-1.5" type="button" @click="clearFile">
            <AppIcon name="upload-simple" :size="15" />
            <span>重新解析</span>
          </button>
        </div>
      </div>

      <!-- 未开始时 -->
      <div v-else class="card">
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
            解析完成后，左侧会解锁「识别总览 / 户型矢量图 / 3D 漫游 / 户型诊断」
            四个子页。它们分别回答：识别出了什么、长什么样、走进去什么感觉、
            这套户型好不好。
          </p>
        </div>
      </div>

      <!-- 错误 -->
      <div
        v-if="s.result.value?.errors?.length"
        class="rounded-xl border border-accent-red/30 bg-accent-red/5 p-3.5"
      >
        <p class="flex items-center gap-1.5 text-[12px] font-bold text-wood-dark">
          <AppIcon name="bug" :size="14" class="text-accent-red" />
          <span>执行中的错误（{{ s.result.value.errors.length }}）</span>
        </p>
        <ul class="mt-1.5 space-y-1">
          <li
            v-for="(e, i) in s.result.value.errors"
            :key="i"
            class="break-words font-mono text-[11px] leading-relaxed text-wood"
          >
            [{{ e.agent || '—' }}] {{ e.message }}
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>
