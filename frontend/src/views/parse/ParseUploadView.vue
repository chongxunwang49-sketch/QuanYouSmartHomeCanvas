<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import {
  layoutHouseDetail,
  parseLayout,
  rerunDiagnosis,
  sampleHouseDetail,
  saveHouseDetail,
} from '@/api'
import type { Diagnosis, HouseDetail } from '@/api/types'
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
      // 期限由后端估算算出（见 useTaskPolling），报实际值而不是写死"120 秒"
      toast.warning(
        `轮询超时（${s.poll.timeoutSeconds.value} 秒）。任务可能仍在后台执行，可用 trace_id 排查。`,
      )
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

/**
 * 识别到的房间。**右栏下半部分靠它撑起来。**
 *
 * 起因是界面问题：这一页在 xl 下是左右两栏，左栏（上传 + 选项）本来就高，
 * 右栏只有一个「识别完成 + 四张指路卡」—— 于是右栏下方是一大片空白，
 * 整页看着像没做完。补内容不能补废话，能补的**真实内容**恰好有两样：
 *
 *   ① 刚识别出来的房间清单（名字 + 面积 + 朝向）—— 用户此刻最想知道的
 *      就是"它到底认出了什么"。指路卡只说明"还能去哪看"，不回答这个问题。
 *   ② 解析这条链路本身做了什么（左栏没有结果时右栏也不空）。
 *
 * ⚠️ `degraded_basic` 时**只有房间名有效**（需求 2.2.4），面积/朝向都是空的。
 *    这时候不能把 0 当面积显示出来 —— 那会变成"这间房 0 ㎡"这种假数据。
 *    所以下面按 `isDegradedBasic` 分两种渲染：降级只列名字并说明原因。
 */
const roomRows = computed(() =>
  (s.layout.value?.rooms ?? []).map((r) => ({
    name: r.name,
    area: r.area,
    orientation: r.orientation,
    notes: r.notes,
  })),
)
const roomAreaSum = computed(() =>
  roomRows.value.reduce((acc, r) => acc + (Number(r.area) || 0), 0),
)

/** 没结果时右栏的「解析链路」—— 说的是真的会跑的步骤，不是营销文案。 */
const PIPELINE = [
  { icon: 'shield-check', title: '本地质量预检', hint: '分辨率/模糊/长宽比先在本地判，不合格不花 Token' },
  { icon: 'blueprint', title: '多模态解析', hint: '房间、墙体、门窗、尺寸、朝向，产结构化 JSON' },
  { icon: 'chart-bar', title: '五维诊断', hint: '采光 / 通风 / 动线 / 收纳 / 绿色，逐项给依据' },
  { icon: 'layout', title: '矢量图与热区', hint: '矢量户型图必出，物品热区挂价格与购买链接' },
] as const

// ══════════════════════════════════════════════════════════════════
// 户型详情（文字资料）—— 补平面图读不出来的那部分
// ══════════════════════════════════════════════════════════════════
/**
 * ⚠️ **这是与左边「上传平面图」并列的第二个上传模块，但传的是文字。**
 *
 * 为什么要有它：平面图是二维的 —— 层高、朝向、采光面、通风路径、收纳位置
 * 图上根本没有，而户型诊断的五个维度全都要用这些。于是诊断只能一直写
 * "数据不足、置信度下调"（需求方反馈的原话就是"综合评分低、数据不足"）。
 * 屋主补一份文字说明把这些补齐，评分与置信度就上得来。
 *
 * ⚠️ **两个上传模块必须在文案上就能分清**，否则用户会把户型图传到这里、
 *    或把这段文字粘到那里。所以这里的每一处措辞都点名"文字/文档"，
 *    并明确写「不是图片」。
 */
const detailText = ref('')
const detailTitle = ref('')
const detailSaved = ref<HouseDetail | null>(null)
const detailBusy = ref(false)
const detailHint = ref('')
const detailFileInput = ref<HTMLInputElement | null>(null)
/** 重新诊断后的结果。用来当场显示"补完资料之后分数变了多少" */
const rediagnosed = ref<Diagnosis | null>(null)

/**
 * 当前户型 id。**从解析结果里取** —— `ParseSession` 上没有单独的 `layoutId`，
 * 而户型详情与五维诊断都是挂在**户型**上的，所以它是这个模块的主键。
 */
const layoutId = computed(() => s.layout.value?.layout_id ?? '')

// 进页面读一次已保存的详情；换了户型（重新解析）也要重读 ——
// 详情是挂在**户型**上的，不是挂在这次会话上。
onMounted(loadHouseDetail)
watch(() => layoutId.value, () => {
  detailSaved.value = null
  rediagnosed.value = null
  detailHint.value = ''
  void loadHouseDetail()
})

async function loadHouseDetail() {
  const id = layoutId.value
  if (!id) return
  try {
    const d = await layoutHouseDetail(id)
    detailSaved.value = d.house_detail
    if (d.house_detail && !detailText.value) {
      detailText.value = d.house_detail.text
      detailTitle.value = d.house_detail.title
    }
  } catch {
    /* 取不到就当没填过 —— 这个模块不该让整页报错 */
  }
}

/** 读入一份 .md / .txt。**只是省一次手动复制**，提交的仍然是文本。 */
async function onPickDetailFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  if (!f) return
  try {
    detailText.value = await f.text()
    if (!detailTitle.value.trim()) detailTitle.value = f.name
    detailHint.value = `已读入 ${f.name}（${detailText.value.length} 字符），确认后点「保存并重新诊断」`
  } catch (e) {
    toast.error(`读取文件失败：${(e as Error).message}`)
  } finally {
    input.value = ''
  }
}

/** 载入演示样例：**只填进输入框**，仍然要用户点保存。 */
async function loadSampleDetail() {
  const id = layoutId.value
  if (!id) return
  detailBusy.value = true
  detailHint.value = ''
  try {
    const sample = await sampleHouseDetail(id)
    detailText.value = sample.text
    detailTitle.value = `${sample.title}（演示样例）`
    detailHint.value = `${sample.matched_by}。${sample.note}`
  } catch (e) {
    detailHint.value = messageOf(e)
  } finally {
    detailBusy.value = false
  }
}

async function saveDetailAndRediagnose() {
  const id = layoutId.value
  if (!id) return
  if (!detailText.value.trim()) {
    toast.warning('先把户型详情写进去（或点「载入演示样例」）')
    return
  }
  detailBusy.value = true
  detailHint.value = ''
  try {
    const saved = await saveHouseDetail(id, {
      text: detailText.value,
      title: detailTitle.value.trim() || '户型详情',
    })
    detailSaved.value = saved.house_detail
    // ⚠️ 保存之后**必须重新诊断**：诊断是拿详情算出来的，不重跑就还是旧的那份。
    const d = await rerunDiagnosis(id)
    rediagnosed.value = d.diagnosis
    toast.success(
      `已保存，并重新诊断：综合 ${d.diagnosis.overall_score.toFixed(1)} 分 · `
      + `置信度 ${(d.diagnosis.confidence ?? 0).toFixed(2)}`,
    )
    if (s.poll.status.value?.result) {
      // 会话里那份结果也换上新的诊断，免得诊断页显示的与这里不一致
      s.poll.status.value.result.diagnosis = d.diagnosis
    }
  } catch (e) {
    detailHint.value = messageOf(e)
    toast.error(detailHint.value)
  } finally {
    detailBusy.value = false
  }
}

</script>

<template>
  <div class="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
    <!-- ══ 左：上传与选项 ══ -->
    <section class="flex flex-col gap-4">
      <!--
        ⚠️ 第一张卡 `flex-1`：两栏的内容长度并不相等（左：上传+选项 /
        右：详情+房间清单），不撑的话**短的那一栏下面会空出一段**，
        紧跟着的整幅「完成」进度框就显得贴在一侧、另一侧吊空。
        让各自的第一张卡吸收多余高度（左边是拖放区变大、右边是文本框变高），
        两栏底边就自然对齐了。
      -->
      <div class="card flex flex-1 flex-col p-4">
        <!-- 拖放区 -->
        <div
          class="relative flex min-h-[240px] flex-1 cursor-pointer flex-col items-center justify-center rounded-2xl border border-dashed p-6 text-center transition-all"
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
                开启后图像<strong class="font-semibold">绝不出本机</strong>，只走本地 Ollama 模型。代价是识别精度低于云端模型，
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
              不合格的图片在<strong class="font-semibold">进入模型之前</strong>就被本地拦下了 —— 没有消耗任何模型调用。
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

      <!--
        ══ 户型详情（第二个上传模块 —— 传的是**文字**）══

        ⚠️ **它替代了原来的「识别完成 + 四张指路卡」。** 那两个模块的取舍：
        指路卡只是导航（侧栏里本来就有同样的四个入口），而"补一份户型详情"
        是**只有这一页能做**的事，也是诊断能不能给出可信评分的前提。
        四个入口没有丢 —— 挪到下面「识别到的房间」卡的脚注里了。

        ⚠️ 与左边「上传平面图」必须一眼分得清：标题、图标、说明、按钮文案
        全部点名"文字/文档"，并反复写「不是图片」。用户传反了就没意义了。
      -->
      <div v-else-if="s.result.value" class="card flex flex-1 flex-col p-4">
        <div class="mb-3 flex items-start justify-between gap-2">
          <h2 class="flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
            <AppIcon name="file-plus" :size="17" class="text-botanical" />
            <span>户型详情<span class="text-wood-muted">（文字资料）</span></span>
          </h2>
          <span v-if="detailSaved" class="tag shrink-0">
            <AppIcon name="check-circle" :size="12" class="mr-1 text-botanical" />
            已保存
          </span>
        </div>

        <p class="mb-3 rounded-xl border border-botanical/25 bg-botanical-surface p-2.5 text-[11px] leading-relaxed text-wood">
          <strong class="font-semibold">这一栏填文字，不是图片</strong> ——
          房子在哪个朝向、层高多少、哪面墙有窗、通风走哪条路、柜子能做多大，
          这些<strong class="font-semibold">平面图上读不出来</strong>，
          而户型诊断的五个维度都要用。左边传的是户型图（PNG / JPG），这里传的是
          文字说明（直接打字，或读入 .md / .txt）。
        </p>

        <label class="mb-1.5 block text-[12px] font-semibold text-wood-dark">
          资料名称<span class="ml-1 font-normal text-wood-muted">（会显示在诊断的依据来源里）</span>
        </label>
        <input
          v-model="detailTitle"
          class="mb-3 w-full rounded-xl border border-warm-border bg-white px-3 py-2 text-[12px] text-wood-dark outline-none focus:border-botanical"
          placeholder="例如：XX 小区 3 栋 2 单元 602 户型详情"
          type="text"
        />

        <textarea
          v-model="detailText"
          class="min-h-[220px] w-full flex-1 resize-y rounded-xl border border-warm-border bg-white p-3 text-[12px] leading-relaxed text-wood-dark outline-none focus:border-botanical"
          placeholder="按条目写就行，例如：&#10;· 层高 2.90 m，装修后净高 2.72 m&#10;· 主朝向正南，南向采光面 9.6 m，厨房与卫生间都有外窗&#10;· 南北通透，夏季穿堂风速 1.0–1.5 m/s&#10;· 承重墙：南向外墙、东侧分户墙；其余为 120 mm 隔墙&#10;· 洗衣机位与地漏在阳台，燃气热水器位在厨房北窗旁"
        />

        <div class="mt-2 flex flex-wrap items-center gap-2">
          <input
            ref="detailFileInput"
            class="hidden"
            type="file"
            accept=".md,.markdown,.txt,text/plain"
            @change="onPickDetailFile"
          />
          <button
            class="btn-ghost px-3 py-1.5"
            type="button"
            @click="detailFileInput?.click()"
          >
            <AppIcon name="file-plus" :size="15" />
            <span>读入 .md / .txt</span>
          </button>
          <button
            class="btn-ghost px-3 py-1.5"
            type="button"
            :disabled="detailBusy"
            @click="loadSampleDetail"
          >
            <AppIcon :name="detailBusy ? 'spinner' : 'sparkle'" :size="15" />
            <span>载入演示样例</span>
          </button>
          <span class="text-[10px] text-wood-muted/80">
            样例只是帮您把格式填好，仍然要您确认后保存
          </span>
        </div>

        <p v-if="detailHint" class="mt-2 rounded-lg bg-warm-sidebar/70 p-2 text-[11px] leading-relaxed text-wood-muted">
          {{ detailHint }}
        </p>

        <button
          class="btn-primary mt-3 w-full py-2.5"
          type="button"
          :disabled="detailBusy || !detailText.trim() || !layoutId"
          @click="saveDetailAndRediagnose"
        >
          <AppIcon :name="detailBusy ? 'spinner' : 'check-circle'" :size="17" />
          <span>{{ detailBusy ? '保存并重新诊断…' : '保存并重新诊断' }}</span>
        </button>

        <!-- 重新诊断的结果当场给出来：分数变了多少，用户要看得见 -->
        <div
          v-if="rediagnosed"
          class="mt-3 rounded-xl border border-botanical/30 bg-botanical-surface p-3"
        >
          <p class="text-[12px] font-semibold text-wood-dark">
            重新诊断完成：综合
            <span class="num text-botanical">{{ rediagnosed.overall_score.toFixed(1) }}</span> 分 ·
            置信度 <span class="num">{{ ((rediagnosed.confidence ?? 0) * 100).toFixed(0) }}%</span>
            · 数据缺口 <span class="num">{{ rediagnosed.data_gaps?.length ?? 0 }}</span> 项
          </p>
          <ul v-if="rediagnosed.evidence_sources?.length" class="mt-1.5 space-y-0.5">
            <li
              v-for="(src, i) in rediagnosed.evidence_sources"
              :key="i"
              class="text-[10px] leading-relaxed text-wood-muted"
            >
              · 依据 {{ i + 1 }}：{{ src }}
            </li>
          </ul>
          <RouterLink class="mt-2 inline-flex items-center gap-1 text-[11px] text-botanical hover:underline" to="/parse/diagnosis">
            <span>去「户型诊断」看五维明细</span>
            <AppIcon name="arrow-right" :size="13" />
          </RouterLink>
        </div>
      </div>

      <!--
        ══ 识别到的房间 ══
        它回答"这份解析认出了什么"。降级模式只列名字（见 `roomRows` 的说明）。

        ⚠️ 页脚那排入口是**从原来的「识别完成」卡搬过来的** ——
        那张卡换成了「户型详情」上传模块（见上），但四个子页的入口不能丢，
        所以缩成一行链接放在这里。侧栏里也有同样四项，两条路都通。
      -->
      <div v-if="s.result.value && roomRows.length" class="card p-4">
        <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 class="flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
            <AppIcon name="house-line" :size="16" class="text-botanical" />
            <span>识别到的房间</span>
          </h2>
          <span v-if="!s.isDegradedBasic.value" class="text-[11px] text-wood-muted">
            合计 <span class="num font-semibold text-wood-dark">{{ roomAreaSum.toFixed(1) }}</span> ㎡
            <span class="text-wood-muted/60"> · 含墙体与过道，与套内面积口径不同</span>
          </span>
        </div>

        <!-- 降级模式：只有名字有效，先把话说清楚，再列名字 -->
        <p
          v-if="s.isDegradedBasic.value"
          class="mb-2 rounded-xl border border-accent-gold/40 bg-wood-light/50 p-2.5 text-[11px] leading-relaxed text-wood"
        >
          这次走的是<strong class="font-semibold">降级解析</strong>：只拿到了房间名，
          面积/朝向/墙体这些结构字段为空。要用完整数据请换一张更清晰的户型图重试。
        </p>

        <ul class="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
          <li
            v-for="(r, i) in roomRows"
            :key="`${r.name}-${i}`"
            class="flex items-center gap-2.5 rounded-xl border border-warm-border bg-white px-3 py-2"
          >
            <span
              class="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-botanical-light text-[10px] font-bold text-botanical"
            >
              <AppIcon name="door" :size="13" />
            </span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-[12px] font-semibold text-wood-dark">
                {{ r.name }}
              </span>
              <!--
                第二行放**解析给的备注**（"图中标注面积 15.1㎡，位于户型左上区域，
                上方外墙开设窗洞"），不放 `orientation` —— 实测那一栏经常就是
                字符串 "unknown"，摆在界面上等于占位噪声。
                备注是真信息，且能解释"它是怎么认出来的"。超长用省略号 +
                `title` 给全文。
              -->
              <span
                v-if="r.notes"
                class="block truncate text-[10px] text-wood-muted"
                :title="r.notes"
              >
                {{ r.notes }}
              </span>
            </span>
            <!-- 降级时面积无效：显示 — 而不是 0（0 会被读成"这间房没有面积"） -->
            <span class="num shrink-0 text-[12px] font-semibold text-wood">
              {{ s.isDegradedBasic.value ? '—' : `${(Number(r.area) || 0).toFixed(1)} ㎡` }}
            </span>
          </li>
        </ul>

        <!-- 四个子页入口（从原「识别完成」卡搬来的一行） -->
        <div class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-warm-border pt-3">
          <span class="text-[10px] font-semibold uppercase tracking-wider text-wood-muted/70">
            接着看
          </span>
          <RouterLink
            v-for="l in resultLinks"
            :key="l.to"
            :to="l.to"
            class="inline-flex items-center gap-1 text-[11px] text-botanical hover:underline"
            :title="l.hint"
          >
            <AppIcon :name="l.icon" :size="13" />
            <span>{{ l.label }}</span>
          </RouterLink>
          <button class="btn-ghost ml-auto px-2.5 py-1" type="button" @click="clearFile">
            <AppIcon name="upload-simple" :size="13" />
            <span>重新解析</span>
          </button>
        </div>
      </div>

      <!-- 未开始时：等待卡 + 解析链路（右栏不留大片空白） -->
      <template v-else-if="!s.result.value">
        <div class="card flex flex-1 flex-col">
          <div class="flex flex-1 flex-col items-center justify-center px-6 py-10 text-center">
            <span
              class="flex h-14 w-14 items-center justify-center rounded-2xl border border-botanical/20 bg-botanical-light text-botanical"
            >
              <AppIcon name="blueprint" :size="26" />
            </span>
            <h3 class="mt-3 font-serif text-[17px] font-semibold text-wood-dark">
              等待解析结果
            </h3>
            <p class="mt-1.5 max-w-md text-[12px] leading-relaxed text-wood-muted">
              解析完成后，右侧会解锁「识别总览 / 户型矢量图 / 3D 漫游 / 户型诊断」
              四个子页。它们分别回答：识别出了什么、长什么样、走进去什么感觉、
              这套户型好不好。
            </p>
          </div>
        </div>

        <!--
          解析链路。**不是占位文案**：这四步是后端真的会跑的（预检零 Token、
          多模态解析、五维诊断、矢量图与热区），写在这里是为了让用户在等的
          时候知道自己在等什么。
        -->
        <div class="card p-4">
          <h2 class="mb-3 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
            <AppIcon name="list-checks" :size="16" class="text-botanical" />
            <span>上传后会发生什么</span>
          </h2>
          <ol class="flex flex-col gap-2.5">
            <li v-for="(p, i) in PIPELINE" :key="p.title" class="flex items-start gap-3">
              <span
                class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-botanical/20 bg-botanical-light text-botanical"
              >
                <AppIcon :name="p.icon" :size="14" />
              </span>
              <span class="min-w-0">
                <span class="flex items-center gap-1.5">
                  <span class="num text-[10px] font-bold text-wood-muted/70">
                    {{ String(i + 1).padStart(2, '0') }}
                  </span>
                  <span class="text-[12px] font-semibold text-wood-dark">{{ p.title }}</span>
                </span>
                <span class="mt-0.5 block text-[11px] leading-relaxed text-wood-muted">
                  {{ p.hint }}
                </span>
              </span>
            </li>
          </ol>
        </div>
      </template>

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
