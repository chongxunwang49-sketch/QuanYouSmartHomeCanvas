<script setup lang="ts">
import { RouterLink } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import SceneViewer from '@/components/SceneViewer.vue'
import { useParseSession } from '@/composables/useParseSession'

/**
 * 户型解析 · 3D 漫游（`/parse/walkthrough`）。
 *
 * 回答第三个问题：**走进去是什么感觉。**
 *
 * ⚠️ 高度给到 `min(74vh, 820px)` —— 比原来嵌在长页里时的 540px 高。
 * 这一页现在只干这一件事，没理由再让用户对着一个小窗口。
 *
 * ⚠️ `SceneViewer` **自己**向 `/layout/{id}/walkable` 拉几何，
 * 只依赖 `layout_id` 一个字段。所以它拆到独立子页不需要任何额外传参
 * —— 这也是这次拆分能做成的前提（见 ParseView 的文件头）。
 */
const s = useParseSession()
</script>

<template>
  <div class="flex flex-col gap-4">
    <EmptyState
      v-if="!s.result.value"
      art="家居空间-knocking-on-the-door_vgly"
      title="还没有可以走进去的户型"
      description="先在「上传解析」里交一张户型图。这一页会把识别出的墙体、门窗按真实比例搭成 3D，可以第一人称走进去。"
    >
      <RouterLink class="btn-ghost px-4 py-2" to="/parse">
        <AppIcon name="upload-simple" :size="16" class="text-botanical" />
        <span>去上传户型图</span>
      </RouterLink>
    </EmptyState>

    <template v-else-if="s.result.value.layout_id">
      <!-- 操作提示。原来嵌在长页里时是 SceneViewer 内部显示的，
           独立成页后放到外面，免得跟画布里的 HUD 重复。 -->
      <div
        class="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-xl border border-warm-border bg-warm-sidebar/50 px-3 py-2 text-[11px] text-wood-muted"
      >
        <span class="flex items-center gap-1.5">
          <AppIcon name="info" :size="13" class="text-botanical" />
          <span class="font-semibold text-wood-dark">操作</span>
        </span>
        <span><kbd class="tag !px-1.5 !text-[10px]">W A S D</kbd> 移动</span>
        <span><kbd class="tag !px-1.5 !text-[10px]">F</kbd> 开/关门</span>
        <span><kbd class="tag !px-1.5 !text-[10px]">G</kbd> 切换行走/自由视角</span>
        <span><kbd class="tag !px-1.5 !text-[10px]">R</kbd> 回到出生点</span>
      </div>

      <SceneViewer :layout-id="s.result.value.layout_id" height="min(74vh, 820px)" />
    </template>
  </div>
</template>
