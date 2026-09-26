<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import FloorMaterialPanel from '@/components/FloorMaterialPanel.vue'
import PlanViewer from '@/components/PlanViewer.vue'
import { useParseSession } from '@/composables/useParseSession'

/**
 * 户型解析 · 户型矢量图（`/parse/drawing`）。
 *
 * 回答第二个问题：**长什么样。**（AC-07 / AC-09 / AC-21 / AC-10）
 *
 * ⚠️ 降级户型也照常挂载：后端会渲染出一张带说明的空状态图
 * （"未识别出房间轮廓，建议换一张更清楚的"），比整块消失更可操作。
 * AC-07 的字面要求就是"**任何**户型都能渲出"。
 *
 * ══════════════════════════════════════════════════════════════════
 * 这一页持有「选中哪间房」与「图纸版本号」两件事
 * ══════════════════════════════════════════════════════════════════
 * 它们跨了两个组件：`PlanViewer`（画布，负责"点了哪间房""画出什么"）
 * 与 `FloorMaterialPanel`（换成什么材料）。所以放在共同父组件里 ——
 * 放进任一个子组件，另一个就读不到了。
 *
 * ⚠️ `revision` 是**必须的**，不是装饰：`/plan.svg` 带
 *    `Cache-Control: private, max-age=300`，换完材质不换 URL 的话
 *    浏览器会拿缓存里的旧图 —— 表现为"接口成功了、图上没变化"，
 *    而控制台一片安静。面板每次改完 emit `changed`，这里 +1。
 */
const s = useParseSession()

/** 画布上选中的房间下标。-1 = 没选。 */
const selectedRoom = ref(-1)
/** 图纸版本号。变了 `PlanViewer` 就重拉 SVG（见上面的说明）。 */
const revision = ref(0)
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

    <template v-else-if="s.result.value.layout_id">
      <PlanViewer
        :layout-id="s.result.value.layout_id"
        title="户型矢量图 · 物品热区"
        :selected-room="selectedRoom"
        :revision="revision"
        @floor-select="selectedRoom = $event"
      />

      <FloorMaterialPanel
        :layout-id="s.result.value.layout_id"
        :selected-room="selectedRoom"
        @select-room="selectedRoom = $event"
        @changed="revision += 1"
      />
    </template>
  </div>
</template>
