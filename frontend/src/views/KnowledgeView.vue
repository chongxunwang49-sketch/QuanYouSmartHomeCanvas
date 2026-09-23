<script setup lang="ts">
import { computed, onMounted } from 'vue'

import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useHealthStore } from '@/stores/health'

/**
 * 知识库管理。
 *
 * ⚠️ **这一页只读，而且不假装能管理。**
 *
 * 后端的 `/knowledge/upload` 属于 4.6 里**未实现**的部分（见
 * `backend/app/api/routes.py` 的模块说明）——没有上传接口，没有文档列表
 * 接口。所以这里只做两件真实的事：
 *   1. 展示 `/system/health` 里 knowledge 这一项的真实状态与条数
 *   2. 说明这套 RAG 是怎么组织的（这部分的实现是真的，值得展示）
 *
 * 摆一个"上传文档"的按钮、一个假的文档列表，是这一页最容易犯的错。
 */
const health = useHealthStore()

const knowledge = computed(() => health.checks.knowledge)

const chunkCount = computed(() => {
  const detail = knowledge.value?.detail ?? ''
  const m = detail.match(/(\d+)\s*条/)
  return m ? Number(m[1]) : null
})

/** RAG 链路：这部分是真实实现的，逐步说明 */
const PIPELINE = [
  {
    step: '01',
    icon: 'file-text',
    title: '语料入库',
    desc: 'Markdown 文档经两级切分：先按标题层级切（保留章节语义），再按长度递归切（控制单块体积）。',
  },
  {
    step: '02',
    icon: 'cube',
    title: '向量化',
    desc: 'bge-large-zh-v1.5，1024 维。批量 16 条一批，块 id 用文本 sha1 生成，重复入库不会产生副本。',
  },
  {
    step: '03',
    icon: 'magnifying-glass',
    title: '检索',
    desc: 'ChromaDB 嵌入式持久化，余弦空间。检索失败返回空结果而**不抛异常** —— 知识库挂了不该让整条审查链断掉。',
  },
  {
    step: '04',
    icon: 'shield-check',
    title: '引用校验',
    desc: '模型给出的 source_ids 必须能在检索结果里找到对应块，否则该引用被剔除并计入 invented_citations。',
  },
]

onMounted(() => health.refresh())
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6">
    <PageHeader
      :breadcrumb="['全友·智绘家', '知识库管理']"
      title="知识库管理"
      :status="
        knowledge?.ok
          ? { icon: 'check-circle', text: `已就绪 · ${chunkCount ?? '?'} 条 chunk` }
          : { icon: 'warning-circle', text: '不可用', tone: 'gold' }
      "
    />

    <!-- ══ 只读声明 ══ -->
    <section class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3.5">
      <div class="flex items-start gap-2.5">
        <AppIcon name="info" :size="18" class="mt-0.5 shrink-0 text-wood-muted" />
        <p class="text-[11px] leading-relaxed text-wood-muted">
          <span class="font-semibold text-wood">这一页是只读的。</span>
          后端目前**没有**文档上传与文档列表接口（4.6 的
          <code class="rounded bg-white px-1 font-mono">/knowledge/upload</code>
          属于未实现部分）。所以这里只展示真实的入库状态，以及这套 RAG
          的组织方式 —— 而不是摆一个点了没反应的上传按钮。
        </p>
      </div>
    </section>

    <!-- ══ 真实状态 ══ -->
    <section class="card p-4">
      <h2 class="mb-3 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="book-open" :size="17" class="text-botanical" />
        <span>当前状态</span>
      </h2>

      <div
        class="flex items-start gap-3 rounded-xl border p-3.5"
        :class="
          knowledge?.ok
            ? 'border-botanical/30 bg-botanical-surface'
            : 'border-accent-gold/40 bg-wood-light/50'
        "
      >
        <AppIcon
          :name="knowledge?.ok ? 'check-circle' : 'warning-circle'"
          :size="22"
          class="mt-0.5 shrink-0"
          :class="knowledge?.ok ? 'text-botanical' : 'text-accent-gold'"
        />
        <div class="min-w-0">
          <p class="text-[14px] font-bold text-wood-dark">
            {{ knowledge?.ok ? '知识库向量集合可用' : '知识库当前不可用' }}
          </p>
          <p class="mt-0.5 break-words font-mono text-[11px] leading-relaxed text-wood-muted">
            {{ knowledge?.detail || '正在检查…' }}
          </p>
        </div>
      </div>

      <div class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div class="rounded-xl border border-warm-border bg-white p-3">
          <p class="text-[10px] font-semibold uppercase text-wood-muted">向量库</p>
          <p class="mt-0.5 text-[13px] font-semibold text-wood-dark">ChromaDB 嵌入式</p>
          <p class="mt-0.5 font-mono text-[10px] text-wood-muted">E:/quanyou/data/chroma</p>
        </div>
        <div class="rounded-xl border border-warm-border bg-white p-3">
          <p class="text-[10px] font-semibold uppercase text-wood-muted">嵌入模型</p>
          <p class="mt-0.5 text-[13px] font-semibold text-wood-dark">bge-large-zh-v1.5</p>
          <p class="mt-0.5 font-mono text-[10px] text-wood-muted">1024 维 · 余弦空间</p>
        </div>
        <div class="rounded-xl border border-warm-border bg-white p-3">
          <p class="text-[10px] font-semibold uppercase text-wood-muted">检索默认条数</p>
          <p class="num mt-0.5 text-[13px] font-semibold text-wood-dark">top_k = 10</p>
          <p class="mt-0.5 font-mono text-[10px] text-wood-muted">审查场景已封顶</p>
        </div>
      </div>

      <!-- 知识库挂了的影响：如实说 -->
      <div
        v-if="knowledge && !knowledge.ok"
        class="mt-3 rounded-xl border border-warm-border bg-warm-sidebar/50 p-3"
      >
        <p class="text-[12px] font-semibold text-wood-dark">会有什么影响</p>
        <ul class="mt-1.5 space-y-1">
          <li
            v-for="t in [
              '避坑审查仍可运行，但所有结论都会标记为「无引用依据」',
              '方案生成与预算、材料选型不受影响（它们不依赖向量库）',
              '系统整体标记为降级状态，界面会显著提示',
            ]"
            :key="t"
            class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
          >
            <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-wood-muted/50" />
            <span>{{ t }}</span>
          </li>
        </ul>
      </div>
    </section>

    <!-- ══ RAG 链路 ══ -->
    <section class="card p-4">
      <h2 class="mb-3 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="cube" :size="17" class="text-botanical" />
        <span>检索链路</span>
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

    <!-- ══ 语料来源（真实文件）══ -->
    <section class="card p-4">
      <h2 class="mb-2 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="plant" :size="17" class="text-botanical" />
        <span>语料来源</span>
      </h2>
      <p class="mb-3 text-[11px] leading-relaxed text-wood-muted">
        语料由 <code class="rounded bg-warm-sidebar px-1 font-mono">seed_data/references_manifest.yaml</code>
        白名单管理，只采清单内的目录，**上游文件一律不修改**。
        <code class="rounded bg-warm-sidebar px-1 font-mono">scripts/validate_references.py</code>
        会校验清单与磁盘是否一致。
      </p>
      <div class="rounded-xl border border-warm-border bg-white p-3">
        <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
          <AppIcon name="list-checks" :size="14" class="text-botanical" />
          <span>入库命令</span>
        </p>
        <pre class="mt-2 overflow-x-auto rounded-lg bg-warm-sidebar/70 p-2.5 font-mono text-[11px] leading-relaxed text-wood">{{ `# 幂等：块 id 是文本 sha1，重复跑不会产生副本
python scripts/ingest_knowledge.py` }}</pre>
      </div>

      <div class="mt-3 rounded-xl border border-warm-border bg-warm-sidebar/40 p-3">
        <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
          <AppIcon name="info" :size="14" class="text-wood-muted" />
          <span>还没有做的</span>
        </p>
        <ul class="mt-1.5 space-y-1">
          <li
            v-for="t in [
              '国标语料（GB 50327 / GB 18580 / GB-T 39600 摘要）—— 让合规类结论有法条可引',
              '文档上传与列表接口 —— 目前入库只能通过命令行脚本',
              '检索效果评估集 —— 召回率目前只有避坑审查那一条样本上的观测值',
            ]"
            :key="t"
            class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
          >
            <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent-gold" />
            <span>{{ t }}</span>
          </li>
        </ul>
      </div>
    </section>
  </main>
</template>
