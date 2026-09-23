<script setup lang="ts">
import { computed } from 'vue'

import AppIcon from './AppIcon.vue'
import type { Comparison } from '@/api/types'
import { GRADE_LABEL, STYLE_LABEL, label } from '@/api/types'

/**
 * 横向对比矩阵。
 *
 * ⚠️ **列与行完全由后端返回的 `fields` / `rows` 驱动**，前端不写死字段表。
 * 后端 `_COMPARISON_FIELDS` 加一项，这里自动多一列——不会出现"后端加了
 * 指标而界面看不见"的静默脱节。
 *
 * ⚠️ 也**不做"推荐"列高亮**。设计稿把中间列标成推荐位，但系统明确
 * 不替用户排序方案（`comparison.recommendation` 恒为 null）。
 * 高亮第二列等于偷偷塞了一个"推荐"结论进去——那正是后端刻意不做的事。
 * 这里改成"高亮与基准不同的值"，这才是对比表该干的事。
 */
const props = defineProps<{ comparison: Comparison | null; open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const fields = computed(() => props.comparison?.fields ?? [])
const rows = computed(() => props.comparison?.rows ?? [])

/** 从行里取方案名，作为表头 */
const planNames = computed(() =>
  rows.value.map((r, i) => {
    const style = label(STYLE_LABEL, String(r.style ?? ''))
    const grade = label(GRADE_LABEL, String(r.budget_grade ?? ''))
    return { code: `方案 ${String(i + 1).padStart(2, '0')}`, name: `${style} · ${grade}` }
  }),
)

/**
 * 同一行的值是否全都一样。全一样说明这一项没有区分度，
 * 在"仅看差异"模式下可以折叠掉。
 */
function isUniform(key: string): boolean {
  const vals = rows.value.map((r) => JSON.stringify(r[key] ?? null))
  return new Set(vals).size <= 1
}

function cell(r: Record<string, unknown>, key: string): string {
  const v = r[key]
  if (v === null || v === undefined || v === '') return '—'
  if (Array.isArray(v)) {
    if (!v.length) return '—'
    // key_moves 是个字符串数组，在表格里用「·」串起来比换行更易扫读
    return v.map((x) => String(x)).join(' · ')
  }
  if (typeof v === 'number') {
    // 预算字段按「万」显示，和后端 _comparison_row 的单位约定一致
    if (key.startsWith('budget_total')) return `${(v / 10000).toFixed(1)} 万`
    if (key === 'quanyou_coverage') return `${Math.round(v * 100)}%`
    return String(v)
  }
  return String(v)
}
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div
        v-if="open"
        class="fixed inset-0 z-[100] flex items-center justify-center bg-wood-dark/40 p-4 backdrop-blur-xs"
        @click.self="emit('close')"
      >
        <div
          class="flex max-h-[88vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-warm-border bg-white shadow-lg"
        >
          <!-- 头部 -->
          <div
            class="flex flex-shrink-0 items-center justify-between border-b border-warm-border bg-warm-sidebar p-4 px-6"
          >
            <div class="flex items-center gap-3">
              <AppIcon name="chart-bar" :size="22" class="text-botanical" />
              <div>
                <h3 class="font-serif text-[18px] font-bold text-wood-dark">
                  核心参数横向评估速查矩阵
                </h3>
                <p class="text-[12px] text-wood-muted">
                  多维量化核算对比，便于面对客户讲解与决策推演
                </p>
              </div>
            </div>
            <button
              class="rounded-lg p-1 text-wood-muted transition-colors hover:bg-white hover:text-wood"
              type="button"
              aria-label="关闭"
              @click="emit('close')"
            >
              <AppIcon name="x" :size="20" />
            </button>
          </div>

          <!-- 表体 -->
          <div class="flex-1 overflow-auto scroll-thin">
            <table v-if="fields.length && rows.length" class="w-full border-collapse text-left text-[13px]">
              <thead class="sticky top-0 z-10">
                <tr class="border-b border-warm-grid bg-warm-sidebar text-[12px] font-semibold text-wood-muted">
                  <th class="w-1/4 px-5 py-3">评估考核维度</th>
                  <th v-for="p in planNames" :key="p.code" class="px-5 py-3">
                    <span class="block text-[11px] font-mono text-wood-muted">{{ p.code }}</span>
                    <span class="block font-semibold text-wood-dark">{{ p.name }}</span>
                  </th>
                </tr>
              </thead>
              <tbody class="divide-y divide-warm-grid">
                <tr v-for="f in fields" :key="f.key" class="hover:bg-warm-sidebar/50">
                  <td class="px-5 py-3 font-medium text-wood-muted">{{ f.label }}</td>
                  <td
                    v-for="(r, i) in rows"
                    :key="i"
                    class="px-5 py-3 font-mono text-[12px]"
                    :class="
                      isUniform(f.key)
                        ? 'text-wood-muted'
                        : 'bg-botanical-surface/60 font-semibold text-wood-dark'
                    "
                  >
                    <span class="line-clamp-3">{{ cell(r, f.key) }}</span>
                  </td>
                </tr>
              </tbody>
            </table>

            <div v-else class="px-6 py-16 text-center">
              <AppIcon name="chart-bar" :size="30" class="mx-auto text-wood-muted/40" />
              <p class="mt-2 text-[13px] font-semibold text-wood-dark">对比表不可用</p>
              <p class="mt-1 text-[12px] text-wood-muted">
                {{ comparison?.unavailable_reason || '需要至少两套方案才能构成横向对比。' }}
              </p>
            </div>
          </div>

          <!-- 脚注：把系统的立场说清楚 -->
          <div
            class="flex flex-shrink-0 flex-col gap-2 border-t border-warm-grid bg-warm-sidebar/60 p-3.5 px-6 text-[11px] text-wood-muted sm:flex-row sm:items-center sm:justify-between"
          >
            <div class="flex items-start gap-1.5">
              <AppIcon name="info" :size="13" class="mt-0.5 shrink-0" />
              <span>
                着色的是**各方案之间有差异**的项；灰字表示三项取值相同、不构成区分。
              </span>
            </div>
            <button
              class="shrink-0 rounded-lg border border-warm-border bg-white px-3 py-1.5 text-wood transition-colors hover:bg-warm-sidebar"
              type="button"
              @click="emit('close')"
            >
              关闭
            </button>
          </div>

          <!-- 系统不排序方案的说明 -->
          <p
            v-if="comparison?.recommendation_note"
            class="flex-shrink-0 border-t border-warm-grid bg-white px-6 py-2.5 text-[11px] leading-relaxed text-wood-muted"
          >
            <span class="font-semibold text-wood">为什么不标"推荐"：</span>
            {{ comparison.recommendation_note }}
          </p>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
