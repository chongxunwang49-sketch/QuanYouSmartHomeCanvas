<script setup lang="ts">
import { RouterLink } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import SceneViewer from '@/components/SceneViewer.vue'
import { useParseSession } from '@/composables/useParseSession'

/**
 * 户型解析 · 3D 漫游（`/parse/walkthrough`）。
 *
 * 回答第三个问题：**这间房子长什么样。**
 *
 * ══════════════════════════════════════════════════════════════════
 * ⚠️ 这一页是**空房子**，不画家具（2026-09-24 需求方明确）
 * ══════════════════════════════════════════════════════════════════
 * 需求方原话：「在**平面图解析后**在该 3d 界面可以进行移动看查
 * **没有家具**的情况；而在用户拿到 id 后进入的装修搭配推荐**三选一后**，
 * 可以在该模块再次生成一个**包含家具**的 3d 界面进行移动」。
 *
 * 所以两页的差别只有一个：**画不画家具。**
 *   这一页    不给 `planId` → `SceneViewer` 连家具接口都不请求
 *   /generate/walkthrough  给 `planId` → 画选定那套方案的家具
 *
 * 之前是无条件拉家具并画上 —— 于是"解析结果"看起来已经带了装修，
 * 用户分不清自己看到的是**户型**还是**方案**。
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
      <!--
        这一页是**空房子**这件事必须写在界面上。
        ⚠️ 不说的话，用户看到一间没有家具的房子，只会以为"家具生成坏了" ——
        而它本来就该是空的：家具属于装修方案，下一站才画。
      -->
      <div
        class="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-xl border border-warm-border bg-warm-sidebar/50 px-3 py-2 text-[11px] text-wood-muted"
      >
        <span class="flex items-center gap-1.5">
          <AppIcon name="info" :size="13" class="text-botanical" />
          <span class="font-semibold text-wood-dark">这是一间空房子</span>
        </span>
        <span>
          只画户型本身（墙体、门窗、层高）——
          家具属于<strong class="font-semibold">装修方案</strong>，
          生成方案并选定之后到「方案生成 · 3D 装修漫游」里看
        </span>
        <RouterLink class="text-botanical hover:underline" to="/generate">
          去生成方案 →
        </RouterLink>
      </div>

      <SceneViewer :layout-id="s.result.value.layout_id" height="min(74vh, 820px)" />
    </template>
  </div>
</template>
