<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import AppIcon from '@/components/AppIcon.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import { materialPrice } from '@/api'
import { messageOf } from '@/api/client'
import type { MaterialPriceData } from '@/api/types'

/**
 * 材料价格查询（4.5，同步接口，纯查表）。
 *
 * ⚠️ **`disclaimer` 必须展示，而且不能弱化。**
 * 需求文档 5.3 与 R-09 要求演示数据显著标注，而这是最容易被前端
 * "顺手美化掉"的地方——把那段灰色小字塞进折叠区、或者做成 tooltip，
 * 就等于把"这些价格不是真的"这件事藏起来了。
 * 所以这里把它放在**结果区最上方**，和筛选条同级。
 */
const data = ref<MaterialPriceData | null>(null)
const loading = ref(false)
const error = ref('')

const category = ref('')
const brand = ref('')
const quanyouFirst = ref(true)

const categories = ref<{ key: string; label: string }[]>([])
const brands = ref<string[]>([])

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await materialPrice({
      category: category.value,
      brand: brand.value,
      quanyou_first: quanyouFirst.value,
    })
    // 品类与品牌从**全量**里取一次，避免筛选后选项自己变少
    if (!categories.value.length) {
      const all = await materialPrice({})
      const seen = new Map<string, string>()
      for (const p of [...all.quanyou_recommended, ...all.items]) {
        if (!seen.has(p.category)) seen.set(p.category, p.category_label)
      }
      categories.value = [...seen].map(([key, label]) => ({ key, label }))
      brands.value = [...new Set([...all.quanyou_recommended, ...all.items].map((p) => p.brand))]
    }
  } catch (e) {
    error.value = messageOf(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)

const qy = computed(() => data.value?.quanyou_recommended ?? [])
const others = computed(() => data.value?.items ?? [])

const fmtRange = (r: [number, number]) =>
  r[0] === r[1] ? `¥${r[0]}` : `¥${r[0]} - ${r[1]}`

const ecoTone = (lv: string) => {
  const s = (lv || '').toUpperCase()
  if (s.includes('ENF') || s.includes('E0')) return 'tag-botanical'
  return 'tag'
}
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6">
    <PageHeader
      :breadcrumb="['全友·智绘家', '材料价格查询']"
      title="材料价格查询"
      :status="{ icon: 'currency-cny', text: '同步接口 · 毫秒级', tone: 'wood' }"
      :code="data ? `CATALOG: ${data.catalog_version}` : ''"
    />

    <!-- ══ 演示数据声明：置顶，与筛选条同级 ══ -->
    <div
      v-if="data?.disclaimer"
      class="rounded-xl border border-accent-gold/40 bg-wood-light/60 p-3.5"
    >
      <div class="flex items-start gap-2.5">
        <AppIcon name="warning-circle" :size="20" class="mt-0.5 shrink-0 text-accent-gold" />
        <div>
          <p class="text-[13px] font-bold text-wood-dark">演示数据声明</p>
          <p class="mt-0.5 text-[11px] leading-relaxed text-wood">{{ data.disclaimer }}</p>
        </div>
      </div>
    </div>

    <!-- ══ 筛选 ══ -->
    <section class="card flex flex-wrap items-center gap-2 p-3">
      <div class="flex items-center gap-1.5">
        <AppIcon name="funnel" :size="15" class="text-wood-muted" />
        <span class="text-[12px] font-semibold text-wood-dark">筛选</span>
      </div>

      <select v-model="category" class="field w-auto px-3 py-1.5" @change="load">
        <option value="">全部品类</option>
        <option v-for="c in categories" :key="c.key" :value="c.key">{{ c.label }}</option>
      </select>

      <select v-model="brand" class="field w-auto px-3 py-1.5" @change="load">
        <option value="">全部品牌</option>
        <option v-for="b in brands" :key="b" :value="b">{{ b }}</option>
      </select>

      <label
        class="flex cursor-pointer items-center gap-2 rounded-xl border border-warm-border bg-white px-3 py-1.5"
      >
        <input v-model="quanyouFirst" class="accent-[#4A7C59]" type="checkbox" @change="load" />
        <span class="text-[12px] text-wood-dark">优先展示全友自有</span>
      </label>

      <span class="ml-auto text-[11px] text-wood-muted">
        共 <span class="num font-semibold text-wood-dark">{{ data?.total ?? 0 }}</span> 个商品
        <span v-if="loading" class="ml-1">· 查询中…</span>
      </span>
    </section>

    <p
      v-if="error"
      class="rounded-xl border border-accent-red/30 bg-accent-red/5 p-3 text-[12px] text-wood"
    >
      {{ error }}
    </p>

    <!-- ══ 全友自有 ══ -->
    <section v-if="quanyouFirst && qy.length" class="card p-4">
      <h2 class="mb-3 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="leaf" :size="17" class="text-botanical" />
        <span>全友自有产品</span>
        <span class="tag-botanical">{{ qy.length }} 项</span>
      </h2>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div
          v-for="p in qy"
          :key="p.id"
          class="flex flex-col gap-2 rounded-xl border border-botanical/20 bg-botanical-surface/50 p-3"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <h3 class="truncate text-[13px] font-bold text-wood-dark">{{ p.name }}</h3>
              <p class="mt-0.5 truncate text-[11px] text-wood-muted">
                {{ p.brand }} · {{ p.spec }}
              </p>
            </div>
            <span :class="ecoTone(p.eco_level)">{{ p.eco_level || '—' }}</span>
          </div>
          <div class="flex items-end justify-between">
            <div>
              <p class="num text-[16px] font-bold text-botanical">{{ fmtRange(p.price_range) }}</p>
              <p class="text-[10px] text-wood-muted">每 {{ p.unit }}</p>
            </div>
            <span class="tag">{{ p.category_label }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- ══ 市场对照 ══ -->
    <section v-if="others.length" class="card p-4">
      <h2 class="mb-3 flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark">
        <AppIcon name="scales" :size="17" class="text-wood" />
        <span>市场对照品牌</span>
        <span class="tag">{{ others.length }} 项</span>
      </h2>
      <div class="overflow-x-auto scroll-thin">
        <table class="w-full border-collapse text-left text-[12px]">
          <thead>
            <tr class="border-b border-warm-grid text-[11px] font-semibold text-wood-muted">
              <th class="px-3 py-2">品类</th>
              <th class="px-3 py-2">商品</th>
              <th class="px-3 py-2">品牌</th>
              <th class="px-3 py-2">规格</th>
              <th class="px-3 py-2">环保等级</th>
              <th class="px-3 py-2 text-right">参考价</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-warm-grid">
            <tr v-for="p in others" :key="p.id" class="hover:bg-warm-sidebar/50">
              <td class="px-3 py-2 text-wood-muted">{{ p.category_label }}</td>
              <td class="px-3 py-2 font-medium text-wood-dark">{{ p.name }}</td>
              <td class="px-3 py-2 text-wood">{{ p.brand }}</td>
              <td class="px-3 py-2 text-wood-muted">{{ p.spec }}</td>
              <td class="px-3 py-2">
                <span :class="ecoTone(p.eco_level)">{{ p.eco_level || '—' }}</span>
              </td>
              <td class="num px-3 py-2 text-right font-semibold text-wood-dark">
                {{ fmtRange(p.price_range) }}
                <span class="text-[10px] font-normal text-wood-muted">/{{ p.unit }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="!loading && !qy.length && !others.length && !error" class="card">
      <EmptyState
        art="预算与报价-budgeting_klon"
        title="没有匹配的商品"
        description="换一个品类或品牌试试，或者清空筛选条件查看全部。"
      />
    </div>
  </main>
</template>
