<script setup lang="ts">
import { RouterLink } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import { useParseSession } from '@/composables/useParseSession'

/**
 * 户型解析 · 识别总览（`/parse/overview`）。
 *
 * 回答第一个问题：**识别出了什么。**
 *
 * 含四块：预检通过信息条、户型概览四格、房间清单、数据缺口。
 * 「下一步」按钮也放这里 —— 用户看完"识别成什么样"之后，
 * 下一步动作（生成方案 / 查材料）在这一页最顺手。
 *
 * ⚠️ 降级提示与 `degraded_basic` 红条**不在这里**，它们在
 * `ParseView.vue` 外壳上（那样切子页也看得见）。这里只负责
 * 在降级时给概览卡加一层水印（需求文档 2.2.4 的呈现要求之一）。
 */
const s = useParseSession()
</script>

<template>
  <div class="flex flex-col gap-4">
    <EmptyState
      v-if="!s.result.value"
      art="设计与户型-business-plan_zrf7"
      title="还没有解析结果"
      description="先在「上传解析」里交一张户型图。解析完这一页会显示识别出的房间清单、面积、朝向与数据缺口。"
    >
      <RouterLink class="btn-ghost px-4 py-2" to="/parse">
        <AppIcon name="upload-simple" :size="16" class="text-botanical" />
        <span>去上传户型图</span>
      </RouterLink>
    </EmptyState>

    <template v-else>
      <!-- 预检通过时的信息条 + 未阻断的提醒 -->
      <div
        v-if="s.precheck.value?.ok"
        class="rounded-xl border border-warm-border bg-warm-sidebar/50 p-3"
      >
        <div class="flex items-center gap-2 text-[11px] text-wood-muted">
          <AppIcon name="check-circle" :size="14" class="shrink-0 text-botanical" />
          <span>
            图片预检通过 · <span class="num">{{ s.precheck.value.width }}×{{ s.precheck.value.height }}</span>
            <template v-if="s.precheck.value.blur_score != null">
              · 清晰度 <span class="num">{{ s.precheck.value.blur_score.toFixed(0) }}</span>
            </template>
          </span>
        </div>
        <ul v-if="s.precheck.value.warnings?.length" class="mt-1.5 space-y-0.5 pl-5">
          <li
            v-for="(w, i) in s.precheck.value.warnings"
            :key="i"
            class="flex items-start gap-1.5 text-[10px] leading-relaxed text-wood-muted"
          >
            <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent-gold" />
            <span>{{ w }}</span>
          </li>
        </ul>
      </div>

      <!-- 户型概览 -->
      <div class="card relative overflow-hidden p-4">
        <!-- degraded_basic 时加半透明水印（需求文档 2.2.4 要求） -->
        <div
          v-if="s.isDegradedBasic.value"
          class="pointer-events-none absolute inset-0 z-10 flex items-center justify-center"
        >
          <span
            class="select-none -rotate-[18deg] text-[38px] font-bold tracking-widest text-accent-red/10"
          >
            需人工复核
          </span>
        </div>

        <div class="mb-3 flex items-center justify-between">
          <h2 class="flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
            <AppIcon name="house-line" :size="17" class="text-botanical" />
            <span>户型概览</span>
          </h2>
          <div class="flex items-center gap-2">
            <span v-if="s.layout.value?.mode" class="tag">mode: {{ s.layout.value.mode }}</span>
            <span class="tag">
              置信度
              <span class="num font-semibold text-wood-dark">
                {{ Math.round((s.layout.value?.confidence ?? 0) * 100) }}%
              </span>
            </span>
          </div>
        </div>

        <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3">
            <p class="text-[10px] font-semibold uppercase text-wood-muted">房间</p>
            <p class="num mt-0.5 text-[20px] font-bold text-wood-dark">
              {{ s.layout.value?.rooms?.length ?? 0 }}
            </p>
          </div>
          <div class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3">
            <p class="text-[10px] font-semibold uppercase text-wood-muted">总面积</p>
            <p class="num mt-0.5 text-[20px] font-bold text-wood-dark">
              {{ (s.layout.value?.total_area ?? 0).toFixed(1) }}
              <span class="text-[12px] font-medium">㎡</span>
            </p>
          </div>
          <div class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3">
            <p class="text-[10px] font-semibold uppercase text-wood-muted">门窗</p>
            <p class="num mt-0.5 text-[20px] font-bold text-wood-dark">
              {{ s.layout.value?.doors?.length ?? 0 }}
              <span class="text-[12px] font-medium text-wood-muted">/</span>
              {{ s.layout.value?.windows?.length ?? 0 }}
            </p>
          </div>
          <div class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3">
            <p class="text-[10px] font-semibold uppercase text-wood-muted">入户朝向</p>
            <p class="mt-0.5 text-[20px] font-bold text-wood-dark">
              {{ s.layout.value?.entrance_orientation || '未知' }}
            </p>
          </div>
        </div>

        <!-- 房间清单 -->
        <div v-if="s.layout.value?.rooms?.length" class="mt-4">
          <p class="mb-2 text-[12px] font-semibold text-wood-dark">房间清单</p>
          <div class="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <div
              v-for="(r, i) in s.layout.value.rooms"
              :key="r.name + i"
              class="rounded-xl border border-warm-border bg-white p-2.5"
            >
              <div class="flex items-center justify-between">
                <span class="truncate text-[12px] font-semibold text-wood-dark">
                  {{ r.name }}
                </span>
                <span class="tag shrink-0 !px-1.5 !text-[10px]">{{ s.roomType(r.type) }}</span>
              </div>
              <p class="num mt-1 text-[11px] text-wood-muted">
                <!-- 降级时面积恒为 0，直接说"不可用"而不是显示 0.0 ㎡ -->
                <template v-if="s.isDegradedBasic.value || !r.area">面积不可用</template>
                <template v-else>{{ r.area.toFixed(1) }} ㎡</template>
                <span v-if="r.orientation && r.orientation !== 'unknown'">
                  · 朝向 {{ r.orientation }}
                </span>
              </p>
            </div>
          </div>
        </div>

        <!-- 数据缺口：如实说，不藏 -->
        <div
          v-if="s.layout.value?.data_gaps?.length"
          class="mt-4 rounded-xl border border-warm-border bg-warm-sidebar/50 p-3"
        >
          <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
            <AppIcon name="info" :size="14" class="text-wood-muted" />
            <span>数据缺口</span>
          </p>
          <ul class="mt-1.5 space-y-1">
            <li
              v-for="(g, i) in s.layout.value.data_gaps"
              :key="i"
              class="flex items-start gap-1.5 text-[11px] leading-relaxed text-wood-muted"
            >
              <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-wood-muted/50" />
              <span>{{ g }}</span>
            </li>
          </ul>
        </div>
      </div>

      <!-- 下一步：能力守卫 -->
      <div class="card p-4">
        <h2 class="mb-3 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
          <AppIcon name="arrow-right" :size="16" class="text-botanical" />
          <span>下一步</span>
        </h2>

        <div class="flex flex-wrap gap-2">
          <RouterLink class="btn-ghost px-4 py-2" to="/parse/drawing">
            <AppIcon name="layout" :size="16" class="text-botanical" />
            <span>看户型矢量图</span>
          </RouterLink>
          <RouterLink class="btn-ghost px-4 py-2" to="/parse/walkthrough">
            <AppIcon name="cube" :size="16" class="text-botanical" />
            <span>进 3D 走一圈</span>
          </RouterLink>
          <button
            class="btn-primary px-4 py-2"
            type="button"
            :disabled="!s.canGenerate.value"
            :title="s.canGenerate.value ? '' : s.blockedReason.value"
            @click="s.goGenerate()"
          >
            <AppIcon name="sparkle" :size="16" />
            <span>生成装修方案</span>
          </button>
        </div>

        <!-- 为什么不能点，直说。前端置灰是体验，后端也会拦（两道都要有） -->
        <p
          v-if="!s.canGenerate.value && s.blockedReason.value"
          class="mt-2.5 flex items-start gap-1.5 rounded-xl border border-warm-border bg-warm-sidebar/60 p-2.5 text-[11px] leading-relaxed text-wood-muted"
        >
          <AppIcon name="info" :size="13" class="mt-0.5 shrink-0" />
          <span>{{ s.blockedReason.value }}</span>
        </p>
      </div>
    </template>
  </div>
</template>
