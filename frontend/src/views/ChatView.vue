<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import {
  askChat,
  chatConversations,
  chatMessages,
  deleteChatConversation,
  patchChatConversation,
  type ChatConversationRow,
  type ChatMessageRow,
  type ChatSource,
} from '@/api'
import { messageOf } from '@/api/client'
import { toast } from '@/utils/toast'
import { useTaskPolling } from '@/composables/useTaskPolling'
import type { GenerateResult, ParseResult } from '@/api/types'

/**
 * 智友问答（对话式 RAG）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 界面格局按需求方指定对齐 DeepSeek 网页
 * ══════════════════════════════════════════════════════════════════
 * 左：历史记录（**可置顶 / 重命名 / 删除**）
 * 右：对话区，底部输入框，**流式输出**，回答末尾列出引用来源
 *
 * ⚠️ 但配色、圆角、字体全部走本站令牌（Botanical Warmth）——
 *    "像 DeepSeek 的**布局**"不等于把它的皮肤搬过来。这一条是需求方
 *    原话点名的：「整体的 ui，布局，字体要与其它模块统一，要让人感觉
 *    是自然存在的功能模块而不是突兀出现的功能」。
 *
 * ══════════════════════════════════════════════════════════════════
 * 三类上下文自动带上，用户不用配置
 * ══════════════════════════════════════════════════════════════════
 *   · 户型：从解析会话里取当前 layout_id
 *   · 屋主详情：挂在户型上，后端自己取（这里只传 layout_id）
 *   · 装修方案：从方案会话里取**用户选定的那一套** plan_id
 * 顶栏把那三项"这次带了什么"如实标出来 —— 用户得知道助手看了什么，
 * 否则会奇怪"它怎么知道我家是南北通透的"。
 */
const parsePoll = useTaskPolling<ParseResult>('parse')
const generatePoll = useTaskPolling<GenerateResult>('generate')

const conversations = ref<ChatConversationRow[]>([])
const activeId = ref('')
const messages = ref<ChatMessageRow[]>([])
const loadingList = ref(true)
const loadingMessages = ref(false)
const listError = ref('')

const question = ref('')
const streaming = ref(false)
/** 正在流式生成的那条回答（与 messages 分开，避免流式期间重排整列表） */
const draft = ref('')
const draftSources = ref<ChatSource[]>([])
/** 这一轮的元信息（后端在流开头给）：一眼看出「它看到了哪些资料」 */
const lastMeta = ref<{ knowledge_count: number; has_layout: boolean; has_house_detail: boolean; has_plan: boolean; knowledge_available: boolean; knowledge_reason: string } | null>(null)

const scrollerEl = ref<HTMLElement | null>(null)
const inputEl = ref<HTMLTextAreaElement | null>(null)
/** 重命名中的会话 id（就地编辑） */
const renamingId = ref('')
const renameText = ref('')

/**
 * 当前户型 / 方案。
 *
 * ⚠️ **这里不能用 `useParseSession()` / `useGenerateSession()`。**
 *    那两个是 `provide/inject` 的（由 `ParseView` / `GenerateView` 提供），
 *    在别的模块里 `inject` 拿不到 —— 实测直接用会让整个页面**白屏**
 *    （组件 setup 抛错，Vue 只往控制台写一行）。
 *
 * 改用两条**全局**的路子，走的是同一份事实：
 *   · 任务的轮询会话（`useTaskPolling('parse'|'generate')`，它把 task_id
 *     存在 sessionStorage 里，切页面/刷新都还在）→ 结果里有 layout_id
 *   · 用户选定的方案 id 存在 `sessionStorage['qy.generate.selected.<户型>']`
 *     （这是 `useGenerateSession` 写下的**约定键**，见它的 `SELECTED_PREFIX`）
 */
const parseResult = computed<ParseResult | null>(
  () => (parsePoll.status.value?.result as ParseResult | null) ?? null,
)
const layoutId = computed(() => parseResult.value?.layout_id ?? '')

const planId = computed(() => {
  const plans = (generatePoll.status.value?.result as GenerateResult | null)?.plans ?? []
  const layout = layoutId.value
  if (!layout || !plans.length) return ''
  let saved = ''
  try {
    saved = sessionStorage.getItem(`qy.generate.selected.${layout}`) || ''
  } catch {
    /* 隐私模式下 sessionStorage 会抛 —— 那就当没选定 */
  }
  // 选定的那套还在就用它；不在了（换了户型/重新生成）就退回第一套，
  // 而不是留一个后端查不到的 plan_id（那会让 /chat/ask 报 4004）
  return plans.some((p) => p.plan_id === saved) ? saved : (plans[0]?.plan_id ?? '')
})
const hasContext = computed(() => Boolean(layoutId.value || planId.value))

/** 页面挂载时把两个轮询会话"接回来"（它们自己从 sessionStorage 恢复）。 */
async function restorePolls() {
  const pt = parsePoll.savedTaskId()
  if (pt) await parsePoll.refreshOnce()
  const gt = generatePoll.savedTaskId()
  if (gt) await generatePoll.refreshOnce()
}

async function loadList() {
  loadingList.value = true
  listError.value = ''
  try {
    const d = await chatConversations()
    conversations.value = d.conversations
  } catch (e) {
    // ⚠️ 库不通时说清楚，别显示成"你还没有对话"（历史没丢，只是读不到）
    listError.value = messageOf(e)
  } finally {
    loadingList.value = false
  }
}

async function openConversation(id: string) {
  if (activeId.value === id) return
  activeId.value = id
  loadingMessages.value = true
  try {
    const d = await chatMessages(id)
    messages.value = d.messages
    await scrollToBottom()
  } catch (e) {
    toast.error(messageOf(e))
    messages.value = []
  } finally {
    loadingMessages.value = false
  }
}

function newConversation() {
  // 只是清空当前视图：**真正建会话发生在第一次提问时**（后端在 /chat/ask 里建）
  activeId.value = ''
  messages.value = []
  draft.value = ''
  draftSources.value = []
  lastMeta.value = null
  question.value = ''
  inputEl.value?.focus()
}

async function togglePin(row: ChatConversationRow) {
  try {
    await patchChatConversation(row.conversation_id, { pinned: !row.pinned })
    await loadList()
  } catch (e) {
    toast.error(messageOf(e))
  }
}

function startRename(row: ChatConversationRow) {
  renamingId.value = row.conversation_id
  renameText.value = row.title
}

async function commitRename() {
  const id = renamingId.value
  const title = renameText.value.trim()
  renamingId.value = ''
  if (!id || !title) return
  try {
    await patchChatConversation(id, { title })
    await loadList()
  } catch (e) {
    toast.error(messageOf(e))
  }
}

async function removeConversation(row: ChatConversationRow) {
  if (!window.confirm(`删除会话「${row.title}」？删除后无法恢复。`)) return
  try {
    await deleteChatConversation(row.conversation_id)
    if (activeId.value === row.conversation_id) newConversation()
    await loadList()
    toast.success('已删除')
  } catch (e) {
    toast.error(messageOf(e))
  }
}

async function scrollToBottom() {
  await nextTick()
  const el = scrollerEl.value
  if (el) el.scrollTop = el.scrollHeight
}

let abort: AbortController | null = null

async function send() {
  const q = question.value.trim()
  if (!q || streaming.value) return
  question.value = ''
  streaming.value = true
  draft.value = ''
  draftSources.value = []
  lastMeta.value = null
  // 用户这句先上屏（后端也会落库；这里只是不等它）
  messages.value = [...messages.value, { role: 'user', content: q }]
  await scrollToBottom()

  abort = new AbortController()
  let convId = activeId.value
  try {
    await askChat(
      {
        question: q,
        conversation_id: convId || undefined,
        layout_id: layoutId.value || undefined,
        plan_id: planId.value || undefined,
      },
      (ev) => {
        if (ev.type === 'meta') {
          convId = ev.conversation_id
          lastMeta.value = ev
        } else if (ev.type === 'delta') {
          draft.value += ev.text
          void scrollToBottom()
        } else if (ev.type === 'sources') {
          draftSources.value = ev.sources
        } else if (ev.type === 'error') {
          toast.error(ev.message)
        }
      },
      abort.signal,
    )
  } catch (e) {
    // 用户主动中断（切页面/按停止）不算错误
    if ((e as Error)?.name !== 'AbortError') toast.error(messageOf(e))
  } finally {
    streaming.value = false
    abort = null
    // 收尾：把流式内容落成一条正式消息，并刷新历史列表（新建的会话要出现）
    if (draft.value.trim()) {
      messages.value = [
        ...messages.value,
        { role: 'assistant', content: draft.value, sources: draftSources.value },
      ]
    }
    draft.value = ''
    draftSources.value = []
    if (convId && convId !== activeId.value) activeId.value = convId
    await loadList()
    if (activeId.value) {
      // 以服务端为准重拉一次：拿到 id / 时间戳 / 真正的 sources
      try {
        const d = await chatMessages(activeId.value)
        messages.value = d.messages
      } catch {
        /* 重拉失败就保留本地这份，界面不清空 */
      }
    }
    await scrollToBottom()
  }
}

function stop() {
  abort?.abort()
  streaming.value = false
}

function onKeydown(e: KeyboardEvent) {
  // Enter 发送、Shift+Enter 换行 —— 与 DeepSeek 的输入习惯一致
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    void send()
  }
}

function sourceLabel(s: ChatSource): string {
  if (s.kind === 'knowledge') return s.citation
  return s.citation
}

onMounted(async () => {
  await restorePolls()
  await loadList()
  // 打开最近一条（有历史时），没有就停在空态等提问
  if (conversations.value.length) await openConversation(conversations.value[0].conversation_id)
  else inputEl.value?.focus()
})

onBeforeUnmount(() => abort?.abort())
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6 surface-glow">
    <PageHeader
      :breadcrumb="['全友·智绘家', '智友问答']"
      title="智友问答"
      :status="{
        icon: 'star',
        text: hasContext ? '已带上你的户型与方案' : '还没选中户型或方案',
      }"
    />

    <div class="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
      <!-- ══ 左：历史记录 ══ -->
      <aside class="card flex max-h-[70vh] flex-col p-3 lg:max-h-none">
        <div class="mb-2 flex items-center justify-between gap-2">
          <h2 class="flex items-center gap-1.5 font-serif text-[14px] font-semibold text-wood-dark">
            <AppIcon name="clock" :size="15" class="text-botanical" />
            <span>历史记录</span>
          </h2>
          <button class="btn-ghost px-2.5 py-1" type="button" @click="newConversation">
            <AppIcon name="file-plus" :size="14" />
            <span>新对话</span>
          </button>
        </div>

        <p v-if="listError" class="rounded-lg border border-accent-red/30 bg-accent-red/5 p-2 text-[11px] leading-relaxed text-accent-red">
          {{ listError }}
        </p>
        <p v-else-if="loadingList" class="p-2 text-[11px] text-wood-muted">正在读取…</p>
        <p v-else-if="!conversations.length" class="p-2 text-[11px] leading-relaxed text-wood-muted">
          还没有对话。在右边直接提问就会自动建一条。
        </p>

        <ul v-else class="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto scroll-thin">
          <li
            v-for="c in conversations"
            :key="c.conversation_id"
            class="group rounded-xl border px-2.5 py-2 transition-colors"
            :class="c.conversation_id === activeId
              ? 'border-botanical/40 bg-botanical-surface'
              : 'border-transparent hover:border-warm-border hover:bg-white/70'"
          >
            <div class="flex items-start gap-1.5">
              <button class="min-w-0 flex-1 text-left" type="button" @click="openConversation(c.conversation_id)">
                <span class="flex items-center gap-1">
                  <AppIcon v-if="c.pinned" name="star" :size="11" class="shrink-0 text-accent-gold" />
                  <span class="truncate text-[12px] font-semibold text-wood-dark">{{ c.title }}</span>
                </span>
                <span class="mt-0.5 block text-[10px] text-wood-muted">
                  {{ (c.updated_at || '').slice(5, 16).replace('T', ' ') }}
                </span>
              </button>
            </div>

            <!-- 就地重命名。放在条目内部，避免弹窗打断浏览 -->
            <div v-if="renamingId === c.conversation_id" class="mt-1.5 flex gap-1">
              <input
                v-model="renameText"
                class="min-w-0 flex-1 rounded-lg border border-warm-border px-2 py-1 text-[11px]"
                @keydown.enter="commitRename"
                @keydown.esc="renamingId = ''"
              />
              <button class="btn-ghost px-2 py-1 text-[10px]" type="button" @click="commitRename">存</button>
            </div>

            <div class="mt-1 flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
              <button
                class="btn-ghost px-1.5 py-0.5 text-[10px]"
                type="button"
                :title="c.pinned ? '取消置顶' : '置顶'"
                @click="togglePin(c)"
              >
                <AppIcon name="star" :size="11" />
              </button>
              <button class="btn-ghost px-1.5 py-0.5 text-[10px]" type="button" title="重命名" @click="startRename(c)">
                <AppIcon name="file-plus" :size="11" />
              </button>
              <button class="btn-ghost px-1.5 py-0.5 text-[10px]" type="button" title="删除" @click="removeConversation(c)">
                <AppIcon name="trash" :size="11" />
              </button>
            </div>
          </li>
        </ul>
      </aside>

      <!-- ══ 右：对话 ══ -->
      <section class="card flex min-h-0 flex-col p-0">
        <!-- 这一轮带了什么资料：**必须让用户看见** -->
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-warm-border px-4 py-2.5">
          <span class="text-[11px] font-semibold text-wood-dark">这次会带上</span>
          <span class="flex items-center gap-1 text-[11px]" :class="layoutId ? 'text-botanical' : 'text-wood-muted/70'">
            <AppIcon :name="layoutId ? 'check-circle' : 'x-circle'" :size="12" />
            <span>户型解析结果</span>
          </span>
          <span class="flex items-center gap-1 text-[11px]" :class="layoutId ? 'text-botanical' : 'text-wood-muted/70'">
            <AppIcon :name="layoutId ? 'check-circle' : 'x-circle'" :size="12" />
            <span>屋主户型详情（有则带上）</span>
          </span>
          <span class="flex items-center gap-1 text-[11px]" :class="planId ? 'text-botanical' : 'text-wood-muted/70'">
            <AppIcon :name="planId ? 'check-circle' : 'x-circle'" :size="12" />
            <span>选定的装修方案</span>
          </span>
          <RouterLink v-if="!hasContext" class="ml-auto text-[11px] text-botanical hover:underline" to="/parse">
            <span>先去解析一张户型图 →</span>
          </RouterLink>
        </div>

        <!-- 消息区 -->
        <div ref="scrollerEl" class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto scroll-thin p-4">
          <div v-if="loadingMessages" class="p-3 text-[12px] text-wood-muted">正在读取对话…</div>

          <div v-else-if="!messages.length && !streaming" class="flex flex-1 flex-col items-center justify-center gap-2 py-10 text-center">
            <span class="flex h-12 w-12 items-center justify-center rounded-2xl border border-botanical/20 bg-botanical-light text-botanical">
              <AppIcon name="star" :size="24" />
            </span>
            <p class="font-serif text-[16px] font-semibold text-wood-dark">问点什么吧</p>
            <p class="max-w-md text-[12px] leading-relaxed text-wood-muted">
              答案会同时依据知识库文档、你这套户型的解析结果、你上传的户型详情、
              以及你选定的装修方案 —— 回答末尾会列出这次用到了哪几份资料。
            </p>
          </div>

          <template v-for="(m, i) in messages" :key="`m-${i}`">
            <div v-if="m.role === 'user'" class="flex justify-end">
              <div class="max-w-[80%] rounded-2xl rounded-br-md bg-botanical px-4 py-2.5 text-[13px] leading-relaxed text-white">
                {{ m.content }}
              </div>
            </div>
            <div v-else class="flex gap-2.5">
              <span class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-botanical/20 bg-botanical-light text-botanical">
                <AppIcon name="star" :size="14" />
              </span>
              <div class="min-w-0 flex-1">
                <div class="rounded-2xl rounded-tl-md border border-warm-border bg-white px-4 py-3 text-[13px] leading-relaxed text-wood whitespace-pre-wrap">{{ m.content }}</div>
                <ul v-if="m.sources?.length" class="mt-1.5 space-y-0.5 pl-1">
                  <li class="text-[10px] font-semibold uppercase tracking-wider text-wood-muted/70">依据来源</li>
                  <li v-for="(s, si) in m.sources" :key="`s-${si}`" class="text-[10px] leading-relaxed text-wood-muted">
                    · {{ sourceLabel(s) }}
                  </li>
                </ul>
              </div>
            </div>
          </template>

          <!-- 流式中的那条 -->
          <div v-if="streaming" class="flex gap-2.5">
            <span class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-botanical/20 bg-botanical-light text-botanical">
              <AppIcon name="spinner" :size="14" class="animate-spin" />
            </span>
            <div class="min-w-0 flex-1">
              <div class="rounded-2xl rounded-tl-md border border-warm-border bg-white px-4 py-3 text-[13px] leading-relaxed text-wood whitespace-pre-wrap">
                {{ draft || '正在检索知识库…' }}
                <span class="ml-0.5 inline-block h-3.5 w-[2px] animate-pulse bg-botanical align-middle" />
              </div>
              <p v-if="lastMeta" class="mt-1.5 text-[10px] text-wood-muted">
                知识库命中 {{ lastMeta.knowledge_count }} 条
                <template v-if="!lastMeta.knowledge_available">（知识库不可用：{{ lastMeta.knowledge_reason }}）</template>
                · 户型 {{ lastMeta.has_layout ? '有' : '无' }}
                · 屋主详情 {{ lastMeta.has_house_detail ? '有' : '无' }}
                · 方案 {{ lastMeta.has_plan ? '有' : '无' }}
              </p>
            </div>
          </div>
        </div>

        <!-- 输入区 -->
        <div class="border-t border-warm-border p-3">
          <div class="flex items-end gap-2">
            <textarea
              ref="inputEl"
              v-model="question"
              rows="2"
              class="min-h-[52px] flex-1 resize-none rounded-xl border border-warm-border bg-white px-3 py-2.5 text-[13px] leading-relaxed text-wood-dark outline-none focus:border-botanical"
              placeholder="问吧，例如：我这套户型厨房能改开放式吗？预算大概要多少？"
              @keydown="onKeydown"
            />
            <button v-if="streaming" class="btn-ghost h-[52px] px-4" type="button" @click="stop">
              <AppIcon name="x-circle" :size="16" />
              <span>停止</span>
            </button>
            <button v-else class="btn-primary h-[52px] px-5" type="button" :disabled="!question.trim()" @click="send">
              <AppIcon name="arrow-right" :size="17" />
              <span>发送</span>
            </button>
          </div>
          <p class="mt-1.5 text-[10px] text-wood-muted/80">
            Enter 发送 · Shift+Enter 换行 · 回答由模型生成，价格与尺寸请以知识库与方案里的口径为准
          </p>
        </div>
      </section>
    </div>
  </main>
</template>
