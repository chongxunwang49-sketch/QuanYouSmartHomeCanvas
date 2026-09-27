<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import { authQuota, myDevices, setMyMembership } from '@/api'
import { messageOf } from '@/api/client'
import type { DevicesData, Membership, QuotaData } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { toast } from '@/utils/toast'

/**
 * 个人中心。**所有角色都能进，且只有"我自己"的内容。**
 *
 * ══════════════════════════════════════════════════════════════════
 * 与「账号管理」的分工（2026-09-24 需求方明确）
 * ══════════════════════════════════════════════════════════════════
 *   账号管理（仅管理员）  别人的列表 · 权限分配 · 封禁启用
 *   个人中心（本页，所有人）我的账号 · 套餐 · 登录设备 · 今日额度 · 权限明细
 *
 * ⚠️ **本页不放任何管理控件，一行都不放。** 这不是"普通用户看不到"，
 * 而是"这里根本没有"。两者的区别很实际：前者靠每行一个 `v-if` 维持，
 * 漏一个就穿帮；后者漏无可漏。
 *
 * ══════════════════════════════════════════════════════════════════
 * 「登录设备」的措辞是有边界的
 * ══════════════════════════════════════════════════════════════════
 * 依据只有登录请求的 User-Agent 与来源 IP，**两者都能伪造**。
 * 所以它只回答"我自己看看有哪些地方登录过"，不构成任何访问控制 ——
 * 界面上必须写出这句限定，不能叫它"设备锁"或"绑定设备"。
 * 后端 `GET /me/devices` 返回的那句 `note` 就是干这个的，原样展示。
 */
const auth = useAuthStore()

const quota = ref<QuotaData | null>(null)
const devices = ref<DevicesData | null>(null)
const loading = ref(true)
const busy = ref(false)
const loadError = ref('')

const TASK_LABEL: Record<string, string> = {
  parse: '户型解析',
  generate: '方案生成',
  review: '报价单审查',
}
const TASK_ORDER = ['parse', 'generate', 'review'] as const

const canToggleMembership = computed(() => auth.user?.role === 'user')

/**
 * 权限明细。**从真实判定推出来，不是写死的一张表。**
 *
 * 每一条都对应一处真实存在的判定：
 *   · 解析       `deps.consume_quota`（免费也能用，受每日配额）
 *   · 生成/审查  `deps.require_paid`（4005）
 *   · 知识库     `nav.ts` 的 `roles: ['admin']` + 路由守卫
 *   · 数据分析   `roles: ['admin','designer']`
 *   · 账号管理   `roles: ['admin']`
 */
const permissions = computed(() => {
  const role = auth.user?.role ?? null
  const paid = auth.canUsePaid
  return [
    {
      label: '户型图解析',
      state: 'ok' as const,
      why: quota.value?.quota.parse?.unlimited
        ? '不限量'
        : `免费功能 · 每日 ${quota.value?.quota.parse?.limit ?? 5} 次`,
    },
    { label: '方案生成', state: paid ? ('ok' as const) : ('locked' as const), why: paid ? '已解锁' : '需开通会员' },
    { label: '避坑审查', state: paid ? ('ok' as const) : ('locked' as const), why: paid ? '已解锁' : '需开通会员' },
    {
      label: '知识库管理',
      state: role === 'admin' ? ('ok' as const) : ('hidden' as const),
      why: role === 'admin' ? '管理员可维护语料' : '仅管理员可见（管理员负责语料维护）',
    },
    {
      label: '数据分析',
      state: role && role !== 'user' ? ('ok' as const) : ('hidden' as const),
      why: role && role !== 'user' ? '可看调用与性能指标' : '仅管理员与设计师可见',
    },
    {
      label: '账号管理',
      state: role === 'admin' ? ('ok' as const) : ('hidden' as const),
      why: role === 'admin' ? '可管理他人账号' : '仅管理员可见（你自己的信息就在本页）',
    },
  ]
})

const STATE_STYLE = {
  ok: { icon: 'check-circle', cls: 'text-botanical' },
  locked: { icon: 'shield', cls: 'text-accent-gold' },
  hidden: { icon: 'x-circle', cls: 'text-wood-muted/60' },
} as const

async function load() {
  loading.value = true
  loadError.value = ''
  const results = await Promise.allSettled([authQuota(), myDevices()])
  const [q, d] = results
  if (q.status === 'fulfilled') quota.value = q.value
  else loadError.value = messageOf(q.reason)
  if (d.status === 'fulfilled') devices.value = d.value
  loading.value = false
}

onMounted(load)

async function changeMembership(next: Membership) {
  if (!auth.user || auth.user.membership === next) return
  busy.value = true
  try {
    const res = await setMyMembership(next)
    // ⚠️ 必须同步 auth store：档位决定 `canUsePaid`，侧栏、
    //    方案生成页、避坑审查页的置灰都读它 —— 只改这一页的本地状态
    //    会让别的页面要刷新才变。
    auth.user = res.user
    auth.error = ''
    toast.success(
      next === 'paid'
        ? '已开通演示会员 —— 方案生成与避坑审查已解锁（仍受每日配额约束）'
        : '已取消会员，方案生成与避坑审查将需要开通后使用',
    )
    await load()
  } catch (e) {
    toast.error(messageOf(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main
    class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6 surface-cross"
  >
    <PageHeader
      :breadcrumb="['全友·智绘家', '个人中心']"
      :title="auth.displayName ? `${auth.displayName} 的账号` : '个人中心'"
      :status="
        auth.user
          ? { icon: 'user-circle', text: auth.user.role_label, tone: 'botanical' }
          : undefined
      "
    />

    <div
      v-if="loadError"
      class="flex items-start gap-2.5 rounded-xl border border-accent-red/30 bg-accent-red/5 p-3"
    >
      <AppIcon name="warning-circle" :size="17" class="mt-0.5 shrink-0 text-accent-red" />
      <p class="text-[11px] leading-relaxed text-accent-red">{{ loadError }}</p>
    </div>

    <div class="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <!-- ══ 左：账号 / 套餐 / 额度 ══
           顺序：身份 → 能买什么 → 用了多少。
           两列高度也按这个顺序配过（大致等高），避免左下角留一块空白
           —— 需求方在意「清新不代表空洞」。 -->
      <div class="flex flex-col gap-4">
        <section class="card p-5">
          <h2 class="mb-4 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
            <AppIcon name="user-circle" :size="16" class="text-botanical" />
            <span>我的账号</span>
          </h2>

          <div v-if="auth.user" class="flex items-start gap-4">
            <span
              class="flex h-14 w-14 shrink-0 items-center justify-center rounded-2xl border
                     border-botanical/20 bg-botanical-light text-[20px] font-bold text-botanical"
            >
              {{ auth.user.avatar_text }}
            </span>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="font-serif text-[17px] font-bold text-wood-dark">
                  {{ auth.user.display_name }}
                </span>
                <span class="chip">{{ auth.user.role_label }}</span>
                <span
                  v-if="!auth.isUnlimited"
                  class="rounded-full border px-2 py-0.5 text-[11px] font-semibold"
                  :class="
                    auth.membership === 'paid'
                      ? 'border-accent-gold/40 bg-wood-light text-wood'
                      : 'border-warm-border bg-warm-sidebar text-wood-muted'
                  "
                >
                  {{ auth.membership === 'paid' ? '会员' : '免费版' }}
                </span>
              </div>
              <p class="mt-1 text-[12px] text-wood-muted">
                {{ auth.user.title }} · 账号
                <span class="num font-semibold text-wood-dark">{{ auth.user.username }}</span>
              </p>
              <p class="mt-2 text-[11px] leading-relaxed text-wood-muted">
                账号与角色由管理员分配 —— 你看到的功能可见性就是由它们决定的。
                要改角色请找管理员；本页只处理你自己的套餐与设备。
              </p>
            </div>
          </div>
        </section>

<!-- 我的套餐 -->
        <section class="card p-5">
          <h2 class="mb-3 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
            <AppIcon name="star" :size="16" class="text-botanical" />
            <span>我的套餐</span>
          </h2>

          <template v-if="canToggleMembership">
            <div class="flex flex-wrap items-center gap-2">
              <span
                class="rounded-full border px-2.5 py-1 text-[11px] font-semibold"
                :class="
                  auth.membership === 'paid'
                    ? 'border-accent-gold/40 bg-wood-light text-wood'
                    : 'border-warm-border bg-warm-sidebar text-wood-muted'
                "
              >
                当前：{{ auth.membership === 'paid' ? '会员' : '免费版' }}
              </span>
              <button
                v-if="auth.membership !== 'paid'"
                class="btn-primary px-3 py-1.5"
                type="button"
                :disabled="busy"
                @click="changeMembership('paid')"
              >
                <AppIcon name="sparkle" :size="14" weight="bold" />
                <span>开通演示会员</span>
              </button>
              <button
                v-else
                class="btn-ghost px-3 py-1.5"
                type="button"
                :disabled="busy"
                @click="changeMembership('free')"
              >
                <span>取消会员（回到免费版）</span>
              </button>
            </div>
            <p class="mt-2.5 text-[10px] leading-relaxed text-wood-muted">
              演示环境，<strong class="font-semibold">不会真实扣费</strong>。
              会员解锁的是"能不能用"，每日配额仍然照常计算。
            </p>
          </template>

          <p
            v-else
            class="rounded-lg border border-warm-border bg-warm-sidebar/60 px-3 py-2.5 text-[11px] leading-relaxed text-wood-muted"
          >
            你的角色是<strong class="font-semibold text-wood-dark">{{ auth.user?.role_label }}</strong>，
            不受会员档位限制 —— 所有功能都可用且没有每日配额上限，
            因此没有可开通或取消的档位。
          </p>
        </section>

<!-- 今日额度 -->
        <section class="card p-5">
          <h2 class="mb-1 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
            <AppIcon name="clock" :size="16" class="text-botanical" />
            <span>今日额度</span>
          </h2>
          <p class="mb-3.5 text-[11px] leading-relaxed text-wood-muted">
            按<strong class="font-semibold">自然日</strong>统计，与设备无关。
          </p>

          <ul v-if="quota" class="space-y-2.5">
            <li
              v-for="key in TASK_ORDER"
              :key="key"
              class="rounded-xl border border-warm-border bg-warm-sidebar/40 px-3.5 py-3"
            >
              <div class="flex items-baseline justify-between gap-3">
                <span class="text-[12px] font-semibold text-wood-dark">{{ TASK_LABEL[key] ?? key }}</span>
                <!-- ⚠️ 降级时**不能**显示 remaining —— 那个数字是拿默认值填的 -->
                <span v-if="quota.quota[key]?.degraded" class="text-[11px] font-semibold text-accent-gold">
                  额度统计暂不可用
                </span>
                <span v-else-if="quota.quota[key]?.unlimited" class="text-[11px] text-wood-muted">
                  已用 <span class="num font-semibold text-wood-dark">{{ quota.quota[key].used }}</span> 次 · 不限量
                </span>
                <span v-else class="text-[11px] text-wood-muted">
                  <span class="num font-semibold text-wood-dark">{{ quota.quota[key]?.used ?? 0 }}</span>
                  / <span class="num">{{ quota.quota[key]?.limit ?? '—' }}</span> 次
                </span>
              </div>

              <div
                v-if="quota.quota[key] && !quota.quota[key].unlimited && !quota.quota[key].degraded"
                class="mt-2 h-1.5 overflow-hidden rounded-full bg-warm-border"
              >
                <div
                  class="h-full rounded-full transition-all duration-300"
                  :class="(quota.quota[key].remaining ?? 0) === 0 ? 'bg-accent-red' : 'bg-botanical'"
                  :style="{
                    width: `${Math.min(100, ((quota.quota[key].used ?? 0) / (quota.quota[key].limit || 1)) * 100)}%`,
                  }"
                />
              </div>

              <p v-if="quota.quota[key]?.degraded" class="mt-1.5 text-[10px] leading-relaxed text-accent-gold">
                这几次暂时统计不到用量，先按可用处理。
              </p>
              <p v-else-if="quota.quota[key]?.remaining === 0" class="mt-1.5 text-[10px] text-accent-red">
                今日已用完，{{ quota.quota[key].reset_at }} 重置
              </p>
              <p v-else class="mt-1.5 text-[10px] text-wood-muted">
                {{ quota.quota[key]?.reset_at }} 重置
              </p>
            </li>
          </ul>
          <p v-else-if="loading" class="text-[12px] text-wood-muted">正在读取额度…</p>
          <p v-else class="text-[12px] text-wood-muted">额度信息暂不可用。</p>
        </section>
              </div>

      <!-- ══ 右：权限 / 设备 ══ -->
      <div class="flex flex-col gap-4">
<!-- 权限明细 -->
        <section class="card p-5">
          <h2 class="mb-3 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
            <AppIcon name="list-checks" :size="16" class="text-botanical" />
            <span>我当前的权限</span>
          </h2>
          <ul class="space-y-1.5">
            <li
              v-for="p in permissions"
              :key="p.label"
              class="flex items-center gap-2.5 rounded-lg bg-warm-sidebar/50 px-2.5 py-2"
            >
              <AppIcon :name="STATE_STYLE[p.state].icon" :size="14" :class="STATE_STYLE[p.state].cls" />
              <span class="w-[5.5rem] shrink-0 text-[11px] font-semibold text-wood-dark">
                {{ p.label }}
              </span>
              <span class="min-w-0 flex-1 text-[10px] leading-relaxed text-wood-muted">
                {{ p.why }}
              </span>
            </li>
          </ul>
          <p class="mt-2 text-[10px] leading-relaxed text-wood-muted">
            「需开通」的功能入口仍然可见，只是置灰并说明原因 —— 藏起来会让人不知道有这个能力，
            也不知道开通能解锁什么。
          </p>
        </section>

<!-- 登录设备 -->
        <section class="card p-5">
          <h2 class="mb-1 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
            <AppIcon name="app-window" :size="16" class="text-botanical" />
            <span>登录设备</span>
          </h2>
          <!-- 免责说明来自后端（`GET /me/devices` 的 note），**原样展示** ——
               前端不要自己重写一遍，两处措辞漂移时以谁为准说不清 -->
          <p class="mb-3.5 text-[10px] leading-relaxed text-wood-muted">
            {{ devices?.note || '依据登录时的设备与网络信息推断，仅供你自己核对。' }}
          </p>

          <ul v-if="devices?.devices?.length" class="space-y-2">
            <li
              v-for="(d, i) in devices.devices"
              :key="d.key"
              class="flex items-center gap-3 rounded-xl border border-warm-border bg-warm-sidebar/40 px-3.5 py-2.5"
            >
              <span
                class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-botanical/20 bg-white text-botanical"
              >
                <AppIcon name="app-window" :size="15" />
              </span>
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-[12px] font-semibold text-wood-dark">{{ d.name }}</span>
                  <!-- 最新一条标"本次"。列表按最近登录排序，后端做的 -->
                  <span v-if="i === 0" class="chip">最近一次</span>
                </div>
                <p class="num mt-0.5 text-[10px] text-wood-muted">
                  {{ d.ip }} · 首次 {{ d.first_seen }} · 共 {{ d.count }} 次
                </p>
              </div>
              <span class="num shrink-0 text-[10px] text-wood-muted">{{ d.last_seen }}</span>
            </li>
          </ul>
          <p v-else-if="loading" class="text-[12px] text-wood-muted">正在读取设备记录…</p>
          <p v-else class="text-[12px] text-wood-muted">
            还没有登录记录。下一次登录会在这里留下一条。
          </p>
        </section>
                              </div>
    </div>
  </main>
</template>
