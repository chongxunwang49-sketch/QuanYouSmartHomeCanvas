<script setup lang="ts">
import { RouterLink } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import PlanViewer from '@/components/PlanViewer.vue'
import { useParseSession } from '@/composables/useParseSession'

/**
 * 户型解析 · 户型矢量图（`/parse/drawing`）。
 *
 * 回答第二个问题：**长什么样。**（AC-07 / AC-09 / AC-21）
 *
 * ⚠️ 降级户型也照常挂载：后端会渲染出一张带说明的空状态图
 * （"未识别出房间轮廓，建议换一张更清晰的"），比整块消失更可操作。
 * AC-07 的字面要求就是"**任何**户型都能渲出"。
 */
const s = useParseSession()
</script>

<template>
  <div class="flex flex-col gap-4">
    <EmptyState
      v-if="!s.result.value"
      art="设计与户型-design-components_c2hs"
      title="还没有可渲染的户型"
      description="先在「上传解析」里交一张户型图。这一页会用矢量方式重画识别出的房间、墙体、门窗，并标出物品热区。"
    >
      <RouterLink class="btn-ghost px-4 py-2" to="/parse">
        <AppIcon name="upload-simple" :size="16" class="text-botanical" />
        <span>去上传户型图</span>
      </RouterLink>
    </EmptyState>

    <PlanViewer
      v-else-if="s.result.value.layout_id"
      :layout-id="s.result.value.layout_id"
      title="户型矢量图 · 物品热区"
    />
  </div>
</template>
