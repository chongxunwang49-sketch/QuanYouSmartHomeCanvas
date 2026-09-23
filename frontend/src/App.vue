<script setup lang="ts">
import { onMounted } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import AppInfoPanel from '@/components/AppInfoPanel.vue'
import AppSidebar from '@/components/AppSidebar.vue'
import { useHealthStore } from '@/stores/health'

/**
 * 应用外壳。
 *
 * 布局照设计稿：**整页不滚动**，只有内容区滚动。
 * 稿子里 `html,body{overflow:hidden}` + `main.flex-1`，是刻意的——
 * 侧栏和顶栏始终在位，滚动条只出现在内容区里，这样"界面框架"和
 * "界面内容"在视觉上是两层，不会一起被推走。
 */
const health = useHealthStore()

onMounted(() => {
  health.refresh()
  // 依赖状态不需要高频刷新；但完全只拉一次的话，用户开着页面把
  // Redis 起来之后界面会一直显示"不可用"。60 秒是个够用又不吵的间隔。
  setInterval(() => health.refresh(), 60_000)
})
</script>

<template>
  <div
    class="relative flex h-full flex-col selection:bg-botanical/20 selection:text-botanical"
  >
    <!-- 顶部 2px 叶绿装饰线。设计稿里贯穿全站，是品牌识别的一部分 -->
    <div class="pointer-events-none fixed left-0 right-0 top-0 z-50 h-[2px] bg-[#4A7C59]/40" />

    <div class="flex h-full w-full overflow-hidden">
      <AppSidebar />

      <div class="flex h-full min-w-0 flex-1 flex-col overflow-hidden">
        <AppHeader />
        <RouterView v-slot="{ Component }">
          <Transition name="fade" mode="out-in">
            <component :is="Component" />
          </Transition>
        </RouterView>
      </div>
    </div>

    <!-- 顶栏头像与侧栏用户卡都打开它 —— 见 AppInfoPanel 的说明 -->
    <AppInfoPanel />
  </div>
</template>
