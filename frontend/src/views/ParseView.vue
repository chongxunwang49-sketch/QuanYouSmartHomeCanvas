<script setup lang="ts">
import { computed } from 'vue'
import { RouterView, useRoute } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import DegradedNotice from '@/components/DegradedNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import PhaseProgress from '@/components/PhaseProgress.vue'
import { NAV, isNavActive } from '@/config/nav'
import { provideParseSession } from '@/composables/useParseSession'

/**
 * 户型解析 —— **模块外壳**（父路由组件）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 这一层负责三件事，都不是"顺手放的"
 * ══════════════════════════════════════════════════════════════════
 *
 * ① **持有解析会话。** `provideParseSession()` 在这里创建轮询，
 *    子路由之间怎么切它都不会被掐断（原因见 useParseSession 文件头）。
 *
 * ② **降级提示挂在这一层，而不是子页里。**
 *    `DegradedNotice` 的注释写着它"不提供关闭按钮、也不做成可折叠的"，
 *    理由是"一个能被点掉的警告，在演示和真用的时候都会被第一批点掉"。
 *    如果把它放进某个子页，用户切到另一个子页就看不见了 ——
 *    那和"点掉"是同一种效果，只是换了个方式。所以它必须在**所有子页
 *    之上**。`degraded_basic` 那条红条同理（需求文档 2.2.4 的强制呈现）。
 *
 * ③ **页面头只在这里渲染一次。** 子页不再各自带 `<PageHeader>` ——
 *    嵌套的 `RouterView` 里再放一个会变成双标题，而且解析状态胶囊
 *    （phase_text / trace_id）是最该始终可见的东西，放子页里就跟着切走了。
 *
 * 内容区拆成 5 个子路由：上传解析 / 识别总览 / 户型矢量图 / 3D 漫游 /
 * 户型诊断。动因是纵向长度 —— 全部平铺时超过 3000px，用户要一直滚。
 */
const route = useRoute()
const s = provideParseSession()

const parseNav = NAV.find((n) => n.to === '/parse')

/** 是不是停在模块首页（上传页）。子页的判定要与它区分开。 */
const isIndex = computed(() => route.path === '/parse')

/**
 * 当前所在的子页。
 *
 * ⚠️ 这里用 `isNavActive(..., exact=true)` 精确匹配。子项里「上传解析」
 * 的路径就是 `/parse`，它是 `/parse/overview` 等所有子路由的前缀 ——
 * 用 startsWith 的话在上传页会同时匹配到它和别的项（详见 config/nav 文件头）。
 */
const currentChild = computed(() =>
  isIndex.value
    ? null
    : (parseNav?.children?.find((c) => isNavActive(route.path, c.to, true)) ?? null),
)

const breadcrumb = computed(() =>
  isIndex.value
    ? ['全友·智绘家', '户型解析']
    : ['全友·智绘家', '户型解析', currentChild.value?.label ?? ''],
)

const title = computed(() => (isIndex.value ? '户型图解析与重建' : (currentChild.value?.label ?? '')))

const headerStatus = computed(() =>
  s.status.value
    ? { icon: 'spinner', text: s.status.value.phase_text, tone: 'botanical' as const }
    : { icon: 'blueprint', text: '等待上传', tone: 'wood' as const },
)

const traceCode = computed(() =>
  s.status.value ? `TRACE: ${s.status.value.trace_id.slice(0, 12)}` : '',
)
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6">
    <PageHeader
      :breadcrumb="breadcrumb"
      :title="title"
      :status="headerStatus"
      :code="traceCode"
    />

    <!--
      ── 降级提示：故意放在子路由**之上**，切页也在 ──
      见文件头 ②。它不折叠、不可关闭，这是刻意继承的约束。
    -->
    <DegradedNotice v-if="s.result.value?.degraded" :reasons="s.result.value.degrade_reasons" />

    <!--
      ── `degraded_basic` 的强制呈现（需求文档 2.2.4）──
      只识别出房间名时，结构字段全空，后面每个子页都会是空的。
      这条红条必须比子页先被看到，否则用户会以为是页面坏了。
    -->
    <div
      v-if="s.isDegradedBasic.value"
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
              s.layout.value?.safety_notice ||
              '本地兜底模型只输出了房间名称，墙体、门窗、尺寸与面积均不可用。所有依赖这些数据的后续操作已被禁用。'
            }}
          </p>
        </div>
      </div>
    </div>

    <!-- ── 子页 ── -->
    <RouterView />

    <!--
      进度卡放在外壳层，**每个子页下面都能看到**。
      解析要跑 40 秒以上，这期间用户可能已经切到「识别总览」去看
      （那里此刻还是空的）—— 进度必须跟着他。
    -->
    <PhaseProgress
      v-if="s.status.value"
      :text="s.status.value.phase_text"
      :progress="s.status.value.progress"
      :elapsed-ms="s.poll.elapsedMs.value"
      :over-estimate="s.poll.overEstimate.value"
      :log="s.poll.phaseLog.value"
      :status="s.status.value.status"
      :error="s.status.value.error ?? ''"
    />
  </main>
</template>
