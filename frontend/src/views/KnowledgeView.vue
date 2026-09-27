<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import { knowledgeList, knowledgeUpload, messageOf } from '@/api'
import type { KnowledgeListData } from '@/api/types'
import { toast } from '@/utils/toast'

/**
 * 知识库管理。
 *
 * ══════════════════════════════════════════════════════════════════
 * ⚠️ 这一页曾经是**只读的**，而且当时那样做是对的
 * ══════════════════════════════════════════════════════════════════
 * 2026-09-26 之前，后端**没有**「文档列表」与「文档上传」接口 ——
 * 它们只在需求文档 §4.6 的表里存在。所以那一版只做两件真事：
 * 显示 `/system/health` 里的真实状态、说明这套 RAG 怎么组织。
 * 页面上明写着"这一页是只读的"，并刻意**不摆**那个点了没反应的按钮。
 *
 * 接口补齐之后，这一页才真的能管理：清单来自 `/knowledge/list`，
 * 入库走 `/knowledge/upload`。**顺序是先有接口再有用按钮** ——
 * 反过来做就是先摆一个假按钮等后端。
 *
 * ══════════════════════════════════════════════════════════════════
 * 几个刻意的取舍
 * ══════════════════════════════════════════════════════════════════
 * ① **上传收文本，不收文件**（与后端同一条取舍）。文件输入框只是
 *    方便你把 md 读进文本框，读进来之后仍然是文本 —— 这条路径上
 *    没有 multipart、没有"传上来的到底是 md 还是伪装成 md 的二进制"。
 * ② **可选语料类型从后端来**。硬编码一份会随语料类型变更静默漂移：
 *    界面照常显示一个后端已经不认的类型，用户选了，请求 4001 回来，
 *    看起来像后端坏了。
 * ③ **失败时原样显示后端那句话**。前端自己组织一套说法就会与后端判据
 *    分叉，而且这里最需要说清的是"到底写没写进去"。
 */
const data = ref<KnowledgeListData | null>(null)
const loading = ref(true)
const loadError = ref('')

const title = ref('')
/**
 * 内容类型的**中文名**。
 *
 * ⚠️ 接口里这几个值是英文枚举（`avoid_pit` / `regulation` / `quanyou_official`），
 *    它们是契约、不该动；但**界面上不该出现英文枚举**（验收要求）。
 *    所以在前端做一次展示层映射 —— 与后端 `routes.py::DOC_TYPE_CN` 同一份口径。
 *    认不出的取值原样显示：宁可露出一个陌生的词，也不要谎报类型。
 */
const DOC_TYPE_CN: Record<string, string> = {
  avoid_pit: '避坑经验',
  regulation: '规范与工艺标准',
  quanyou_official: '品牌官方资料',
}
const docTypeLabel = (t: string) => DOC_TYPE_CN[t] || t

const docType = ref('avoid_pit')
const tagText = ref('')
const text = ref('')
const submitting = ref(false)

/** 上传成功后的结果。**留着给用户看"写进去多少"**，而不是弹个 toast 就没了。 */
const lastUpload = ref<{ source: string; written: number; chunk_count: number } | null>(null)

const docs = computed(() => data.value?.documents ?? [])
const maxChars = computed(() => data.value?.limits.max_upload_chars ?? 200_000)

/**
 * 文档清单分页。
 *
 * ⚠️ 只切**显示**，不切数据：清单是服务端一次给全的（`/knowledge/list`
 * 不带分页参数），顶部状态卡里的 `document_count` 仍然是全量。
 * 让分页去改计数，就是在用一个被切过的数冒充总数。
 *
 * 为什么非分页不可：清单长了以后这一页要拉好几屏，而**用户真正要用的
 * 是下面的「入库一篇文档」** —— 得先滚过整张清单才够得着。
 */
const PAGE_SIZE = 8
const page = ref(1)
const pageCount = computed(() => Math.max(1, Math.ceil(docs.value.length / PAGE_SIZE)))

/**
 * 页码**可能被点飞** —— 这不是理论：连点「下一页」时，DOM 的 `disabled`
 * 要等下一次渲染才生效，几下点在同一个 tick 里就会把 `page` 加到远超总页数。
 * ⚠️ 踩过的具体表现：页码跳到 12/5，范围行写成「**第 89–40 篇 / 共 40 篇**」，
 * 表格空白。一个自相矛盾的数字出现在屏幕上，正是这个项目最反对的东西。
 *
 * 所以**显示层一律只认 safePage**，`page` 只是个原始意图。
 */
const safePage = computed(() => Math.min(Math.max(1, page.value), pageCount.value))
const pagedDocs = computed(() =>
  docs.value.slice((safePage.value - 1) * PAGE_SIZE, safePage.value * PAGE_SIZE),
)
const rangeText = computed(() => {
  if (!docs.value.length) return '清单为空'
  const from = (safePage.value - 1) * PAGE_SIZE + 1
  const to = Math.min(safePage.value * PAGE_SIZE, docs.value.length)
  return `第 ${from}–${to} 篇 / 共 ${docs.value.length} 篇`
})

/** 清单变短后页码可能越界（入库会重新拉清单）—— 夹回合法范围，不留一张空白页 */
watch(docs, () => {
  if (page.value > pageCount.value) page.value = pageCount.value
})
const canSubmit = computed(
  () => Boolean(title.value.trim()) && Boolean(text.value.trim())
    && text.value.length <= maxChars.value && !submitting.value,
)

/** 上传按钮为什么点不了 —— 置灰必须说明原因，否则用户以为界面坏了。 */
const blockedReason = computed(() => {
  if (submitting.value) return '正在入库…'
  if (!title.value.trim()) return '还没有填标题 —— 它会作为引用里的「出处」显示'
  if (!text.value.trim()) return '正文还是空的'
  if (text.value.length > maxChars.value) {
    return `正文 ${text.value.length} 字符，超过单次上限 ${maxChars.value} —— 拆成多篇分批上传`
  }
  return ''
})

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    data.value = await knowledgeList()
  } catch (e) {
    // 管理员之外的角色调这条路会拿到 4002。**原样显示后端那句话**，
    // 不自己编一句"无权限" —— 后端那句里带着当前角色。
    loadError.value = messageOf(e)
  } finally {
    loading.value = false
  }
}

/** 把选中的 md 读进文本框。**只是省一次手动复制**，不改变"提交的是文本"这件事。 */
async function onPickFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try {
    text.value = await file.text()
    if (!title.value.trim()) title.value = file.name.replace(/\.md$/i, '')
    toast.success(`已读入 ${file.name}（${text.value.length} 字符），确认后点「入库」`)
  } catch (e) {
    toast.error(`读取文件失败：${(e as Error).message}`)
  } finally {
    input.value = ''   // 允许重复选同一个文件
  }
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  lastUpload.value = null
  try {
    const r = await knowledgeUpload({
      title: title.value.trim(),
      text: text.value,
      doc_type: docType.value,
      tags: tagText.value.split(/[,\s，、]+/).map((t) => t.trim()).filter(Boolean),
    })
    lastUpload.value = r
    toast.success(`已入库 ${r.written} 段资料，库内共 ${r.chunk_count} 段`)
    title.value = ''
    tagText.value = ''
    text.value = ''
    await load()          // 清单要立刻反映这次入库
  } catch (e) {
    // ⚠️ 入库失败时后端会说清"本次没有写入任何内容" —— 那句话必须让用户看到
    toast.error(messageOf(e))
  } finally {
    submitting.value = false
  }
}

/**
 * 知识库这条链在做什么。
 *
 * ⚠️ 这四条原来写的是**实现**：模型名 `bge-large-zh-v1.5`、向量库 `ChromaDB`、
 *    内部字段 `source_ids` / `invented_citations`、「上传与离线脚本共用同一份切块实现」。
 *    验收阶段要求界面上不出现这类工程细节，所以改成"对用户意味着什么"的说法：
 *    四步还是那四步，但不再解释这套系统是怎么搭的。
 *
 * ⚠️ `tests/test_frontend_contract.py` 里那条「脚本里给模板用的字符串也不能带
 *    Markdown 强调」的用例，就是被这里原来的 `**不抛异常**` 引出来的 ——
 *    改这四条时别把 `**` 带回来（界面按纯文本渲染，会原样显示成星号）。
 */
const PIPELINE = [
  {
    step: '01',
    icon: 'file-text',
    title: '资料入库',
    desc: '把上传的文档按章节整理成一段段可检索的知识，长文会自动分段。',
  },
  {
    step: '02',
    icon: 'cube',
    title: '语义索引',
    desc: '按意思建立索引，所以换个问法也能找到同一段资料，不只是对关键词。',
  },
  {
    step: '03',
    icon: 'magnifying-glass',
    title: '找出依据',
    desc: '做诊断、审查与问答之前，先在这里把相关的原文找出来。',
  },
  {
    step: '04',
    icon: 'shield-check',
    title: '核对出处',
    desc: '每条引用都要能对回原文；对不上的引用会被剔除 —— 宁可少说，也不编。',
  },
]

onMounted(load)
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6 surface-arcs">
    <PageHeader
      :breadcrumb="['全友·智绘家', '知识库管理']"
      title="知识库管理"
      :status="
        data?.available
          ? { icon: 'check-circle', text: `已就绪 · ${data.chunk_count} 段资料` }
          : { icon: 'warning-circle', text: '不可用', tone: 'gold' }
      "
    />

    <!-- ══ 当前状态 ══ -->
    <section class="card p-4">
      <h2 class="mb-3 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="book-open" :size="17" class="text-botanical" />
        <span>当前状态</span>
      </h2>

      <div v-if="loading" class="flex items-center gap-2 text-[12px] text-wood-muted">
        <AppIcon name="spinner" :size="16" class="animate-spin" />
        正在读取知识库…
      </div>

      <!-- 取不到：原样显示后端那句话（含当前角色），不自己编一句 -->
      <div
        v-else-if="loadError"
        class="flex items-start gap-3 rounded-xl border border-accent-gold/40 bg-wood-light/50 p-3.5"
      >
        <AppIcon name="warning-circle" :size="22" class="mt-0.5 shrink-0 text-accent-gold" />
        <div class="min-w-0">
          <p class="text-[14px] font-bold text-wood-dark">读不到知识库</p>
          <p class="mt-0.5 break-words text-[11px] leading-relaxed text-wood-muted">
            {{ loadError }}
          </p>
        </div>
      </div>

      <template v-else-if="data">
        <div
          class="flex items-start gap-3 rounded-xl border p-3.5"
          :class="
            data.available
              ? 'border-botanical/30 bg-botanical-surface'
              : 'border-accent-gold/40 bg-wood-light/50'
          "
        >
          <AppIcon
            :name="data.available ? 'check-circle' : 'warning-circle'"
            :size="22"
            class="mt-0.5 shrink-0"
            :class="data.available ? 'text-botanical' : 'text-accent-gold'"
          />
          <div class="min-w-0">
            <p class="text-[14px] font-bold text-wood-dark">
              {{ data.available ? '知识库可用' : '知识库当前不可用' }}
            </p>
            <p class="mt-0.5 break-words text-[11px] leading-relaxed text-wood-muted">
              {{ data.available
                ? `${data.chunk_count} 段资料 · ${data.document_count} 篇文档`
                : data.reason }}
            </p>
          </div>
        </div>

        <!--
          ⚠️ 原来这三格写的是「向量库 / ChromaDB 嵌入式 / <服务器上的绝对路径>」、
             「嵌入模型 / bge-large-zh-v1.5 / 1024 维 · 余弦空间」——
             技术选型与**服务器文件系统路径**都在界面上，业主看不懂也用不上，
             验收阶段按需求去掉。换成两格用户真正关心的计数。
        -->
        <div class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div class="rounded-xl border border-warm-border bg-white p-3">
            <p class="text-[10px] font-semibold uppercase text-wood-muted">已收录资料</p>
            <p class="num mt-0.5 text-[13px] font-semibold text-wood-dark">
              {{ data.document_count }} 篇
            </p>
          </div>
          <div class="rounded-xl border border-warm-border bg-white p-3">
            <p class="text-[10px] font-semibold uppercase text-wood-muted">知识片段</p>
            <p class="num mt-0.5 text-[13px] font-semibold text-wood-dark">
              {{ data.chunk_count }} 段
            </p>
          </div>
        </div>

        <!-- 知识库挂了的影响：如实说 -->
        <div
          v-if="!data.available"
          class="mt-3 rounded-xl border border-warm-border bg-warm-sidebar/50 p-3"
        >
          <p class="text-[12px] font-semibold text-wood-dark">会有什么影响</p>
          <ul class="mt-1.5 space-y-1">
            <li
              v-for="t in [
                '避坑审查仍可运行，但所有结论都会标记为「无引用依据」',
                '方案生成、预算与材料选型不受影响',
                '界面上会明确提示，不会悄悄跳过',
              ]"
              :key="t"
              class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
            >
              <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-wood-muted/50" />
              <span>{{ t }}</span>
            </li>
          </ul>
        </div>
      </template>
    </section>

    <!-- ══ 文档清单（真实数据）══ -->
    <section v-if="data?.available" class="card p-4">
      <h2 class="mb-1 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="list-checks" :size="17" class="text-botanical" />
        <span>文档清单</span>
        <span class="font-sans text-[11px] font-normal text-wood-muted">
          {{ data.document_count }} 篇
        </span>
      </h2>
      <p class="mb-3 text-[11px] leading-relaxed text-wood-muted">
        按来源汇总，列出每份资料被切成了多少段。每页 {{ PAGE_SIZE }} 篇。
      </p>

      <div class="overflow-hidden rounded-xl border border-warm-border">
        <table class="w-full border-collapse text-[11px]">
          <thead class="bg-warm-sidebar/70 text-left text-wood-muted">
            <tr>
              <th class="px-3 py-2 font-semibold">来源</th>
              <th class="w-16 px-2 py-2 text-right font-semibold">片段</th>
              <th class="w-28 px-2 py-2 font-semibold">类型</th>
              <th class="px-2 py-2 font-semibold">标签</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="d in pagedDocs"
              :key="d.source"
              class="border-t border-warm-border/70 align-top"
            >
              <td class="px-3 py-2">
                <p class="break-all font-mono text-[10.5px] leading-relaxed text-wood-dark">
                  {{ d.source }}
                </p>
                <p
                  v-if="d.headings.length"
                  class="mt-0.5 line-clamp-2 text-[10px] leading-relaxed text-wood-muted"
                >
                  {{ d.headings.join(' / ') }}
                </p>
              </td>
              <td class="num px-2 py-2 text-right text-wood">{{ d.chunks }}</td>
              <td class="px-2 py-2">
                <span class="rounded-full bg-warm-sidebar px-2 py-0.5 font-mono text-[10px] text-wood">
                  {{ docTypeLabel(d.doc_type) || '—' }}
                </span>
              </td>
              <td class="px-2 py-2">
                <span
                  v-for="t in d.tags"
                  :key="t"
                  class="mr-1 inline-block rounded bg-botanical-light px-1.5 py-0.5 text-[10px] text-botanical"
                >{{ t }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 翻页。只有一页时不摆按钮 —— 摆一排点不动的控件是噪声 -->
      <div class="mt-2 flex flex-wrap items-center justify-between gap-2">
        <p class="text-[11px] text-wood-muted">{{ rangeText }}</p>
        <div v-if="pageCount > 1" class="flex items-center gap-1.5">
          <button
            class="flex items-center gap-1 rounded-lg border border-warm-border bg-white px-2.5 py-1.5 text-[11px] text-wood transition-colors hover:bg-warm-sidebar disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-white"
            type="button"
            :disabled="safePage <= 1"
            @click="page = Math.max(safePage - 1, 1)"
          >
            <AppIcon name="caret-down" :size="13" class="rotate-90" />
            <span>上一页</span>
          </button>
          <span class="num px-1 text-[11px] text-wood-muted">{{ safePage }} / {{ pageCount }}</span>
          <button
            class="flex items-center gap-1 rounded-lg border border-warm-border bg-white px-2.5 py-1.5 text-[11px] text-wood transition-colors hover:bg-warm-sidebar disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-white"
            type="button"
            :disabled="safePage >= pageCount"
            @click="page = Math.min(safePage + 1, pageCount)"
          >
            <span>下一页</span>
            <AppIcon name="caret-down" :size="13" class="-rotate-90" />
          </button>
        </div>
      </div>
    </section>

    <!-- ══ 上传入库（真实接口）══ -->
    <section v-if="data" class="card p-4">
      <h2 class="mb-1 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="upload-simple" :size="17" class="text-botanical" />
        <span>入库一篇文档</span>
      </h2>
      <p class="mb-3 text-[11px] leading-relaxed text-wood-muted">
        同一份内容重复上传会<strong class="font-semibold">覆盖</strong>旧版本，不会在检索结果里出现两次。
      </p>

      <div class="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div class="flex flex-col gap-3">
          <div>
            <label class="mb-1.5 block text-[12px] font-semibold text-wood-dark">
              标题
              <span class="ml-1 font-normal text-wood-muted">（会作为引用里的「出处」显示）</span>
            </label>
            <input
              v-model="title"
              class="field px-3 py-2"
              placeholder="例如：内江地区泥瓦工艺增项常见做法"
            />
          </div>

          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="mb-1.5 block text-[12px] font-semibold text-wood-dark">语料类型</label>
              <!-- ⚠️ 可选项来自后端：硬编码一份会随语料类型变更静默漂移 -->
              <select v-model="docType" class="field px-2.5 py-2">
                <option v-for="t in data.doc_types" :key="t" :value="t">{{ docTypeLabel(t) }}</option>
              </select>
            </div>
            <div>
              <label class="mb-1.5 block text-[12px] font-semibold text-wood-dark">
                标签
                <span class="ml-1 font-normal text-wood-muted">（逗号分隔）</span>
              </label>
              <input v-model="tagText" class="field px-3 py-2" placeholder="增项, 泥瓦" />
            </div>
          </div>

          <div>
            <label class="mb-1.5 block text-[12px] font-semibold text-wood-dark">
              或直接选一个 .md 文件
              <span class="ml-1 font-normal text-wood-muted">（读进下面的正文框，仍然按文本提交）</span>
            </label>
            <input
              class="field px-3 py-2 text-[11px] file:mr-2 file:rounded file:border-0 file:bg-warm-sidebar file:px-2 file:py-1 file:text-[11px] file:text-wood"
              type="file"
              accept=".md,.txt,text/markdown,text/plain"
              @change="onPickFile"
            />
          </div>

          <div class="rounded-xl border border-warm-border bg-warm-sidebar/50 p-3">
            <p class="text-[11px] leading-relaxed text-wood-muted">
              单次最多
              <span class="num font-semibold">{{ maxChars.toLocaleString() }}</span>
              字符。更长的文档请拆成几篇分批上传 —— 分批不影响检索，
              同一份资料分几次传进来效果是一样的。
            </p>
          </div>
        </div>

        <div class="flex flex-col gap-2">
          <label class="text-[12px] font-semibold text-wood-dark">
            正文（Markdown）
            <span class="ml-1 font-normal" :class="text.length > maxChars ? 'text-accent-red' : 'text-wood-muted'">
              {{ text.length.toLocaleString() }} / {{ maxChars.toLocaleString() }}
            </span>
          </label>
          <textarea
            v-model="text"
            class="field min-h-[240px] flex-1 resize-y px-3 py-2 font-mono text-[11px] leading-relaxed"
            placeholder="# 一级标题&#10;&#10;正文……&#10;&#10;## 二级标题&#10;&#10;按标题层级切块，所以用标题组织内容检索效果更好。"
          />
          <!-- 置灰必须说明原因 -->
          <p v-if="blockedReason" class="text-[11px] text-wood-muted">
            {{ blockedReason }}
          </p>
          <button
            class="btn-primary self-start px-4 py-2"
            type="button"
            :disabled="!canSubmit"
            @click="submit"
          >
            <AppIcon :name="submitting ? 'spinner' : 'upload-simple'" :size="16" />
            <span>{{ submitting ? '正在入库…' : '入库' }}</span>
          </button>

          <!-- 入库结果留着，而不是弹个 toast 就没了 -->
          <div
            v-if="lastUpload"
            class="rounded-xl border border-botanical/30 bg-botanical-surface p-3"
          >
            <p class="text-[12px] font-semibold text-wood-dark">
              已写入 {{ lastUpload.written }} 段资料
            </p>
            <p class="mt-0.5 break-all text-[10.5px] text-wood-muted">
              {{ lastUpload.source }}
            </p>
            <p class="mt-0.5 text-[11px] text-wood-muted">
              库内共 {{ lastUpload.chunk_count }} 段。
            </p>
          </div>
        </div>
      </div>
    </section>

    <!-- ══ 知识库在做什么 ══ -->
    <section class="card p-4">
      <h2 class="mb-3 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="cube" :size="17" class="text-botanical" />
        <span>知识库在做什么</span>
      </h2>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div
          v-for="p in PIPELINE"
          :key="p.step"
          class="flex flex-col gap-2 rounded-xl border border-warm-border bg-warm-sidebar/40 p-3.5"
        >
          <div class="flex items-center gap-2">
            <span
              class="flex h-8 w-8 items-center justify-center rounded-lg border border-botanical/20 bg-botanical-light font-mono text-[11px] font-bold text-botanical"
            >
              {{ p.step }}
            </span>
            <AppIcon :name="p.icon" :size="16" class="text-wood" />
          </div>
          <p class="text-[13px] font-bold text-wood-dark">{{ p.title }}</p>
          <p class="text-[11px] leading-relaxed text-wood-muted">{{ p.desc }}</p>
        </div>
      </div>
    </section>

    <!-- ══ 知识来源 ══ -->
    <section class="card p-4">
      <h2 class="mb-2 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="plant" :size="17" class="text-botanical" />
        <span>知识来源</span>
      </h2>
      <p class="mb-3 text-[11px] leading-relaxed text-wood-muted">
        知识来源分两类，界面上分开标识：
      </p>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div class="rounded-xl border border-warm-border bg-white p-3">
          <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
            <AppIcon name="list-checks" :size="14" class="text-botanical" />
            <span>公司资料</span>
          </p>
          <p class="mt-1.5 text-[11px] leading-relaxed text-wood-muted">
            收录规范、工艺与报价方面的标准资料。这部分是避坑审查与材料选型
            的主要依据来源。
          </p>
        </div>
        <div class="rounded-xl border border-warm-border bg-white p-3">
          <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
            <AppIcon name="upload-simple" :size="14" class="text-botanical" />
            <span>用户上传</span>
          </p>
          <p class="mt-1.5 text-[11px] leading-relaxed text-wood-muted">
            你自己传进来的资料，来源单独标识 —— 一条结论引的是公司资料
            还是临时上传的，在引用里看得出来。
          </p>
        </div>
      </div>
    </section>
  </main>
</template>
