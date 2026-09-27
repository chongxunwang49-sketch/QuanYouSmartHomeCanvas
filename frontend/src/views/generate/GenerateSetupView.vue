<script setup lang="ts">
import { RouterLink, useRouter } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import { GRADE_LABEL, STYLE_LABEL } from '@/api/types'
import { PAIRS, useGenerateSession } from '@/composables/useGenerateSession'

/**
 * 方案生成 · 生成参数（`/generate`）。
 *
 * 回答第一个问题：**要生成什么？**
 *
 * 表单状态全部住在会话里（`useGenerateSession`），不在这里 ——
 * 生成要跑 90 秒以上，用户切到「三方案对比」看一眼再切回来，
 * 填了一半的参数不能丢。
 *
 * ⚠️ 「开始生成」按钮**不在这一页**，在模块外壳的页面头上 ——
 * 它在三个子页里都该在（尤其在「三方案对比」页上写着「重新生成」），
 * 那是模块级动作，不是这一页的动作。
 */
const s = useGenerateSession()
const router = useRouter()

const OPTIONS = [
  { k: 'has_children', label: '有儿童' },
  { k: 'has_elderly', label: '有老人' },
  { k: 'pets', label: '养宠物' },
  { k: 'smart_home', label: '要智能家居' },
] as const

/**
 * 切换一个选项。
 *
 * 收**数组本身**而不是 Ref：模板里的 ref 会自动解包，传进来的是 `.value`
 * 那个响应式数组。就地 push/splice 依然能触发更新（ref 对数组是深层的），
 * 所以这里不需要 `.value`。
 */
function toggle(list: string[], key: string) {
  const i = list.indexOf(key)
  if (i >= 0) list.splice(i, 1)
  else list.push(key)
  s.filterProblems.value = []   // 用户一动手，上一次的报错就不再是当前状态
}

/** 全友不能被排除 —— 排除它会直接违反 AC-18，后端一律 4001 拒绝。
 *  前端不做这个判断的第二份实现，只是把选项标灰并说明原因。 */
function isLockedBrand(brand: string): boolean {
  return brand === (s.materialOpts.value?.quanyou_brand ?? '全友')
}
</script>

<template>
  <div class="flex flex-col gap-4">
    <!--
      按钮置灰必须说明原因。
      「开始生成」在没填 layout_id 时是 disabled —— 如果不解释，
      用户点一下发现没反应，只会认为界面坏了。
    -->
    <div
      v-if="!s.layoutId.value.trim() && !s.plans.value.length"
      class="flex items-start gap-2.5 rounded-xl border border-accent-gold/40 bg-wood-light/50 p-3"
    >
      <AppIcon name="info" :size="17" class="mt-0.5 shrink-0 text-accent-gold" />
      <p class="text-[11px] leading-relaxed text-wood">
        <span class="font-semibold">「开始生成」当前不可点，因为还没有户型 ID。</span>
        生成方案要用一份<strong class="font-semibold">已解析的户型</strong> ——
        先到「户型解析」上传一张户型图，解析完成后点「生成装修方案」会自动带着 ID 跳过来。
      </p>
    </div>

    <section class="card p-4">
      <h2 class="mb-3 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
        <AppIcon name="gear" :size="16" class="text-botanical" />
        <span>生成参数</span>
      </h2>

      <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div class="flex flex-col gap-3">
          <div>
            <label class="mb-1.5 block text-[12px] font-semibold text-wood-dark">
              户型 ID
              <span class="ml-1 font-normal text-wood-muted">（来自「户型解析」）</span>
            </label>
            <div class="flex gap-2">
              <input
                v-model="s.layoutId.value"
                class="field px-3 py-2 font-mono"
                placeholder="layout_20260923_xxxxxx"
              />
              <button class="btn-ghost shrink-0 px-3 py-2" type="button" @click="router.push('/parse')">
                <AppIcon name="blueprint" :size="15" class="text-botanical" />
                <span>去解析</span>
              </button>
            </div>
            <RouterLink
              v-if="s.layoutId.value.trim()"
              class="mt-1.5 inline-flex items-center gap-1 text-[11px] text-botanical hover:underline"
              :to="{ path: '/parse/walkthrough' }"
            >
              <AppIcon name="cube" :size="12" />
              <span>先看看这个户型的空房子（3D 户型漫游）</span>
            </RouterLink>
          </div>

          <div>
            <p class="mb-1.5 text-[12px] font-semibold text-wood-dark">
              风格 × 预算档（按位置配对）
            </p>
            <div class="flex flex-col gap-2">
              <div
                v-for="(p, i) in PAIRS"
                :key="p.style"
                class="flex items-center gap-2 rounded-xl border border-warm-border bg-white p-2.5"
              >
                <span class="font-mono text-[11px] text-wood-muted">
                  {{ String(i + 1).padStart(2, '0') }}
                </span>
                <select v-model="s.styles.value[i]" class="field flex-1 px-2.5 py-1.5">
                  <option v-for="(v, k) in STYLE_LABEL" :key="k" :value="k">{{ v }}</option>
                </select>
                <AppIcon name="arrow-right" :size="14" class="shrink-0 text-wood-muted" />
                <select v-model="s.grades.value[i]" class="field flex-1 px-2.5 py-1.5">
                  <option v-for="(v, k) in GRADE_LABEL" :key="k" :value="k">{{ v }}</option>
                </select>
              </div>
            </div>
          </div>
        </div>

        <div class="flex flex-col gap-3">
          <div>
            <p class="mb-1.5 text-[12px] font-semibold text-wood-dark">业主需求</p>
            <div class="grid grid-cols-2 gap-2">
              <label class="flex items-center gap-2 rounded-xl border border-warm-border bg-white px-3 py-2">
                <span class="text-[12px] text-wood-muted">常住人数</span>
                <input
                  v-model.number="s.requirements.value.family_size"
                  class="field w-14 px-2 py-1 text-center"
                  type="number"
                  min="1"
                  max="10"
                />
              </label>
              <label
                v-for="opt in OPTIONS"
                :key="opt.k"
                class="flex cursor-pointer items-center gap-2 rounded-xl border border-warm-border bg-white px-3 py-2"
              >
                <input
                  v-model="s.requirements.value[opt.k]"
                  class="accent-[#4A7C59]"
                  type="checkbox"
                />
                <span class="text-[12px] text-wood-dark">{{ opt.label }}</span>
              </label>
            </div>
          </div>

          <label
            class="flex cursor-pointer items-start gap-3 rounded-xl border border-warm-border bg-white p-3"
          >
            <input v-model="s.quanyouPriority.value" class="mt-0.5 accent-[#4A7C59]" type="checkbox" />
            <span>
              <span class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
                <AppIcon name="leaf" :size="14" class="text-botanical" />
                <span>同等条件下优先推荐全友自有产品</span>
              </span>
              <span class="mt-0.5 block text-[11px] leading-relaxed text-wood-muted">
                这是<strong class="font-semibold">排序偏好</strong>：开启后同分候选里全友排在前面。
                方案本身始终会保证全友自有产品的占比，这一点不受这个开关影响；
                替换了哪几项，在材料明细里看得到。
              </span>
            </span>
          </label>

          <div class="rounded-xl border border-warm-border bg-warm-sidebar/60 p-3">
            <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
              <AppIcon name="info" :size="14" class="text-wood-muted" />
              <span>关于耗时</span>
            </p>
            <p class="mt-1 text-[11px] leading-relaxed text-wood-muted">
              三套方案会同时开工，各自产出空间规划、造价明细与材料选型，最后再统一复核一遍。
              约需 95 秒，期间可以切到别的页面，进度不会中断。
            </p>
          </div>
        </div>
      </div>

      <!--
        AC-19 材料偏好。

        交互刻意做成"排除 + 偏好"而不是逐件勾商品：全友覆盖率是**系统**的
        验收指标，让用户把竞品挑满等于让用户替系统背指标 —— 那时系统
        要么违约，要么无视用户的选择，两条路都是界面在说谎。
      -->
      <div
        v-if="s.materialOpts.value"
        class="mt-4 flex flex-col gap-3 rounded-xl border border-warm-border bg-white p-3"
      >
        <p class="flex items-center gap-1.5 text-[12px] font-semibold text-wood-dark">
          <AppIcon name="funnel" :size="14" class="text-botanical" />
          <span>材料偏好</span>
          <span class="font-normal text-wood-muted">（可选，不选则按全部品类选材）</span>
        </p>

        <div>
          <p class="mb-1.5 text-[11px] text-wood-muted">不要这些品类</p>
          <div class="flex flex-wrap gap-1.5">
            <button
              v-for="c in s.materialOpts.value.categories"
              :key="c.key"
              class="chip-toggle"
              :class="s.excludedCategories.value.includes(c.key) ? 'chip-toggle-on' : ''"
              type="button"
              @click="toggle(s.excludedCategories.value, c.key)"
            >
              {{ c.label }}
            </button>
          </div>
        </div>

        <div>
          <p class="mb-1.5 text-[11px] text-wood-muted">不要这些品牌</p>
          <div class="flex flex-wrap gap-1.5">
            <button
              v-for="b in s.materialOpts.value.brands"
              :key="b"
              class="chip-toggle"
              :class="[
                s.excludedBrands.value.includes(b) ? 'chip-toggle-on' : '',
                isLockedBrand(b) ? 'chip-toggle-locked' : '',
              ]"
              type="button"
              :disabled="isLockedBrand(b)"
              :title="
                isLockedBrand(b)
                  ? `不能排除「${b}」：平台要求方案中全友覆盖率不低于 ${Math.round(
                      s.materialOpts.value.constants.min_quanyou_coverage * 100,
                    )}%，这是系统指标，不是可选项`
                  : ''
              "
              @click="toggle(s.excludedBrands.value, b)"
            >
              {{ b }}
            </button>
          </div>
        </div>

        <div>
          <p class="mb-1.5 text-[11px] text-wood-muted">同等条件下更想要这些品牌</p>
          <div class="flex flex-wrap gap-1.5">
            <button
              v-for="b in s.materialOpts.value.brands"
              :key="b"
              class="chip-toggle"
              :class="s.preferredBrands.value.includes(b) ? 'chip-toggle-on' : ''"
              type="button"
              @click="toggle(s.preferredBrands.value, b)"
            >
              {{ b }}
            </button>
          </div>
        </div>

        <!-- 冲突说明贴在这里，而不是只弹一个一闪而过的 toast ——
             用户得一边看着标红的选项一边改。文案由**后端**给。 -->
        <ul
          v-if="s.filterProblems.value.length"
          class="flex flex-col gap-1 rounded-lg border border-accent-red/40 bg-accent-red/5 p-2"
        >
          <li
            v-for="p in s.filterProblems.value"
            :key="p.code"
            class="text-[11px] leading-relaxed text-wood-dark"
          >
            · {{ p.message }}
          </li>
        </ul>
      </div>
    </section>

    <!-- 还没生成过才给空状态；生成过之后这一页仍然有用（改参数、重新生成） -->
    <div v-if="!s.plans.value.length && !s.poll.running.value" class="card">
      <EmptyState
        art="设计与户型-design-components_c2hs"
        title="还没有生成方案"
        description="填入一个已解析的户型 ID，选好风格与预算档，系统会并行产出空间规划、造价明细与材料选型，再做一次汇聚审查。"
      >
        <button class="btn-ghost px-4 py-2" type="button" @click="router.push('/parse')">
          <AppIcon name="blueprint" :size="16" class="text-botanical" />
          <span>先去解析户型图</span>
        </button>
      </EmptyState>
    </div>

    <RouterLink
      v-else-if="s.plans.value.length"
      class="btn-ghost self-start px-4 py-2"
      to="/generate/plans"
    >
      <AppIcon name="chart-bar" :size="16" class="text-botanical" />
      <span>去看这 {{ s.plans.value.length }} 套方案</span>
      <AppIcon name="arrow-right" :size="14" class="text-wood-muted" />
    </RouterLink>
  </div>
</template>
