<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import AppIcon from './AppIcon.vue'
import { floorMaterials, messageOf, setFloorMaterial } from '../api'
import type { FloorMaterialsData, FloorSubstitution } from '../api/types'
import { toast } from '../utils/toast'

/**
 * 地面材质替换（AC-10）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 这条 AC 原来是「框选地板 → 输入"深色胡桃木" → 局部重绘」
 * ══════════════════════════════════════════════════════════════════
 * 它的载体是 AI 出图路径（局部重绘 = Inpainting），而那条路径已随
 * AC-08 作废。2026-09-26 需求方重定性为：**在矢量图上换地面材质**。
 *
 * 重定性的合理性在于它和已有交付物本来就是一套的：矢量户型图上
 * **本来就挂着材料与价格**（AC-09/21 的热区），换材质反映到造价上
 * 才有业务价值，而规则引擎算造价是 ADR-07 的事。
 *
 * ══════════════════════════════════════════════════════════════════
 * 三处刻意的做法
 * ══════════════════════════════════════════════════════════════════
 * ① **可选材料与房间清单都从后端来。** 硬编码一份会随目录变更静默漂移：
 *    界面照常显示一个后端已经不认的 id，用户选了，请求 4001 回来，
 *    看起来像后端坏了。
 * ② **造价显示成区间。** 单价本身是区间，取中位数会变成一个"看起来很
 *    确定"的数字 —— 后端有意给区间，界面就不该把它抹成一个数。
 * ③ **改完让父组件把图纸重拉一次**（`emit('changed')`）。`/plan.svg` 带
 *    `max-age=300`，不换 URL 的话浏览器会拿缓存里的旧图，而接口是成功的。
 */
const props = defineProps<{
  layoutId: string
  /** 当前选中的房间（由父组件持有，与画布上的选中是同一个） */
  selectedRoom: number
}>()

const emit = defineEmits<{
  changed: []
  'select-room': [roomIndex: number]
}>()

const data = ref<FloorMaterialsData | null>(null)
const loading = ref(true)
const error = ref('')
/** 正在提交的房间号（只锁那一行，不锁整页） */
const busyRoom = ref<number | null>(null)

const subs = computed(() => data.value?.substitutions ?? [])
const rooms = computed(() => data.value?.rooms ?? [])
const options = computed(() => data.value?.eligible ?? [])

const subOf = computed(() => {
  const m = new Map<number, FloorSubstitution>()
  for (const s of subs.value) m.set(s.room_index, s)
  return m
})

const selectedName = computed(
  () => rooms.value.find((r) => r.room_index === props.selectedRoom)?.room_name ?? '',
)

async function load() {
  if (!props.layoutId) return
  loading.value = true
  error.value = ''
  try {
    data.value = await floorMaterials(props.layoutId)
  } catch (e) {
    data.value = null
    error.value = messageOf(e)
  } finally {
    loading.value = false
  }
}

async function apply(roomIndex: number, materialId: string) {
  busyRoom.value = roomIndex
  try {
    data.value = await setFloorMaterial(props.layoutId, {
      room_index: roomIndex, material_id: materialId,
    })
    const name = rooms.value.find((r) => r.room_index === roomIndex)?.room_name ?? '这间房'
    if (materialId) {
      const m = options.value.find((o) => o.id === materialId)
      toast.success(`${name}的地面已换成「${m?.name ?? materialId}」`)
    } else {
      toast.success(`${name}的地面已还原成默认配色`)
    }
    emit('changed')      // 让父组件把图纸重拉一次（见文件头 ③）
  } catch (e) {
    // 原样显示后端那句话：它说清了"哪个房间号不合法""哪种材料不能铺地"
    toast.error(messageOf(e))
  } finally {
    busyRoom.value = null
  }
}

watch(() => props.layoutId, load)
onMounted(load)
</script>

<template>
  <section class="card p-4">
    <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
      <h2 class="flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
        <AppIcon name="squares-four" :size="16" class="text-botanical" />
        <span>地面材质替换</span>
      </h2>
      <p v-if="selectedName" class="text-[11px] text-wood-muted">
        画布上选中的是
        <strong class="font-semibold text-botanical">{{ selectedName }}</strong>
      </p>
    </div>

    <p class="mb-3 text-[11px] leading-relaxed text-wood-muted">
      <strong class="font-semibold text-wood">在左边图上点一块地面</strong>，
      或者从下面挑一间房 —— 然后选一种材料。图上那间房会换成该材料的
      <strong class="font-semibold">代表色</strong>，造价按这间房的地面面积重算。
      <br />
      图上填的是代表色、不是效果图；真实纹理要看实物或官网。
    </p>

    <div v-if="loading" class="flex items-center gap-2 py-4 text-[12px] text-wood-muted">
      <AppIcon name="spinner" :size="16" class="animate-spin" />
      正在读取…
    </div>

    <p
      v-else-if="error"
      class="rounded-xl border border-accent-gold/40 bg-wood-light/50 p-3 text-[11px] leading-relaxed text-wood"
    >
      {{ error }}
    </p>

    <template v-else-if="data">
      <!-- ── 选房间 ── -->
      <div class="mb-3">
        <p class="mb-1.5 text-[11px] text-wood-muted">选一间房</p>
        <div class="flex flex-wrap gap-1.5">
          <button
            v-for="r in rooms"
            :key="r.room_index"
            class="chip-toggle"
            :class="[
              r.room_index === selectedRoom ? 'chip-toggle-on' : '',
              subOf.has(r.room_index) ? 'ring-1 ring-accent-gold' : '',
            ]"
            type="button"
            :title="`${r.room_name} · ${r.area_m2}㎡`"
            @click="emit('select-room', r.room_index)"
          >
            {{ r.room_name }}
            <span v-if="subOf.has(r.room_index)" class="ml-1">●</span>
          </button>
        </div>
      </div>

      <!-- ── 选材料 ── -->
      <div v-if="selectedRoom >= 0" class="mb-3">
        <p class="mb-1.5 text-[11px] text-wood-muted">
          换成（{{ options.length }} 种可选，都按 元/㎡ 计价）
        </p>
        <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <button
            v-for="o in options"
            :key="o.id"
            class="flex items-start gap-2.5 rounded-xl border p-2.5 text-left transition disabled:opacity-50"
            :class="
              subOf.get(selectedRoom)?.material?.id === o.id
                ? 'border-accent-gold bg-accent-gold/10'
                : 'border-warm-border bg-white hover:border-wood/30'
            "
            type="button"
            :disabled="busyRoom !== null"
            @click="apply(selectedRoom, o.id)"
          >
            <!-- 材料代表色。**空 span 带背景色**，不用图片 -->
            <span
              class="mt-0.5 h-7 w-7 shrink-0 rounded-md border border-warm-border"
              :style="{ backgroundColor: o.swatch }"
            />
            <span class="min-w-0 flex-1">
              <span class="flex items-center gap-1">
                <span class="truncate text-[12px] font-semibold text-wood-dark">{{ o.name }}</span>
                <span
                  v-if="o.is_quanyou"
                  class="shrink-0 rounded-full bg-botanical-light px-1.5 py-0.5 text-[9px] font-semibold text-botanical"
                >全友</span>
              </span>
              <span class="mt-0.5 block text-[10.5px] text-wood-muted">
                {{ o.brand }} · {{ o.spec }}
              </span>
              <span class="num mt-0.5 block text-[11px] font-semibold text-wood">
                {{ o.price_range[0] }}–{{ o.price_range[1] }} 元/㎡
              </span>
            </span>
          </button>
        </div>
      </div>

      <p
        v-else
        class="rounded-xl border border-warm-border bg-warm-sidebar/50 p-2.5 text-[11px] text-wood-muted"
      >
        还没有选房间 —— 在左边图上点一块地面，或点上面任一房间名。
      </p>

      <!-- ── 当前替换 ── -->
      <div v-if="subs.length" class="mt-3">
        <p class="mb-1.5 text-[11px] text-wood-muted">已换（{{ subs.length }} 间）</p>
        <ul class="flex flex-col gap-1.5">
          <li
            v-for="s in subs"
            :key="s.room_index"
            class="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-warm-border bg-white p-2.5"
          >
            <div class="min-w-0">
              <p class="text-[12px] font-semibold text-wood-dark">
                {{ s.room_name }}
                <span class="font-normal text-wood-muted">
                  · {{ s.area_m2 }}㎡ ·
                </span>
                <span v-if="s.material">{{ s.material.name }}</span>
                <span v-else class="text-accent-red">材料不在目录里</span>
              </p>
              <p v-if="s.cost" class="num mt-0.5 text-[11px] text-wood">
                地面造价 {{ s.cost.min.toLocaleString() }}–{{ s.cost.max.toLocaleString() }}
                {{ s.cost.unit }}
              </p>
              <p v-else-if="s.note" class="mt-0.5 text-[10.5px] text-accent-gold">{{ s.note }}</p>
            </div>
            <button
              class="btn-ghost shrink-0 px-2.5 py-1 text-[11px]"
              type="button"
              :disabled="busyRoom !== null"
              @click="apply(s.room_index, '')"
            >
              <AppIcon :name="busyRoom === s.room_index ? 'spinner' : 'arrow-right'" :size="13" />
              <span>还原</span>
            </button>
          </li>
        </ul>
      </div>

      <ul class="mt-3 space-y-1">
        <li
          v-for="(n, i) in data.notes"
          :key="i"
          class="flex items-start gap-1.5 text-[10px] leading-relaxed text-wood-muted"
        >
          <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent-gold" />
          <span>{{ n }}</span>
        </li>
      </ul>
    </template>
  </section>
</template>
