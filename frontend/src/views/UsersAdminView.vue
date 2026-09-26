<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import { listUsers, updateUser } from '@/api'
import { messageOf } from '@/api/client'
import type { AuthUser, Membership, UserListData, UserRole } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { toast } from '@/utils/toast'

/**
 * 账号管理。**仅管理员。**
 *
 * ══════════════════════════════════════════════════════════════════
 * 这一页只做"管理别人"，不含任何"我自己"的内容
 * ══════════════════════════════════════════════════════════════════
 * 2026-09-24 需求方明确：「只有管理员拥有用户管理界面，其它权限的账号对应的
 * 界面应该是个人中心」，且**两者的排布和功能要分开**。
 *
 * 所以原来那一版（同页里既有"我的账号/今日额度"又有账号总表）被拆成两个页面：
 *
 *   本页（账号管理，仅管理员）  别人列表 · 权限分配 · 封禁启用
 *   个人中心（所有角色）        我自己的账号 · 套餐 · 登录设备 · 今日额度
 *
 * 拆开的好处是**职责单一**：本页不需要在"我"和"他们"之间切换视角，
 * 因此也不需要在每一行里判断"这行是不是我自己"。原来那种写法最容易漏的
 * 就是某一行忘了判断，于是普通用户看见了管理控件。
 *
 * ══════════════════════════════════════════════════════════════════
 * 三条后端会拒绝的操作（前端同步不给控件，理由写在界面上）
 * ══════════════════════════════════════════════════════════════════
 *   ① 改自己            —— 会把自己锁在门外
 *   ② 把别人设成管理员  —— 需求方：权限分配**不含管理员权限**
 *   ③ 动一个管理员账号  —— 同源；封了管理员就没人能解封
 *
 * 界面上对 ②③ 的做法是**不给控件并说明原因**，而不是给一个点了报错的按钮：
 * 一个必然失败的按钮比没有按钮更糟（本项目对"承诺了交互却不给"一贯的态度）。
 */
const auth = useAuthStore()

const data = ref<UserListData | null>(null)
const loading = ref(true)
const busy = ref('')
const loadError = ref('')

const ROLE_LABEL: Record<UserRole, string> = {
  admin: '管理员',
  designer: '设计师',
  user: '普通用户',
}
const MEMBERSHIP_LABEL: Record<Membership, string> = { paid: '会员', free: '免费版' }
const roleLabel = (r: UserRole) => ROLE_LABEL[r] ?? r

/** 可分配的角色。**排除 admin** —— 见文件头 ② */
const assignableRoles = computed(() =>
  (data.value?.roles ?? []).filter((r) => r !== 'admin'),
)

/** 管理员账号整体不在本页管辖范围内（文件头 ③） */
const isAdminAccount = (u: AuthUser) => u.role === 'admin'

/** 概览用的小统计。**从真实列表算，不写死** */
const stats = computed(() => {
  const us = data.value?.users ?? []
  return {
    total: us.length,
    unlimited: us.filter((u) => u.is_unlimited).length,
    paid: us.filter((u) => !u.is_unlimited && u.membership === 'paid').length,
    free: us.filter((u) => !u.is_unlimited && u.membership === 'free').length,
    disabled: us.filter((u) => !u.is_active).length,
  }
})

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    data.value = await listUsers()
  } catch (e) {
    // 非管理员会拿到 4002。**原样展示后端那句话** —— 它已经说清了缺什么。
    loadError.value = messageOf(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 改完之后重新拉一次，保证表里显示的是**后端真值**而不是本地的乐观值 */
async function patch(u: AuthUser, body: { role?: UserRole; membership?: Membership; is_active?: boolean }, okText: string) {
  busy.value = String(u.id)
  try {
    await updateUser(u.id, body)
    toast.success(okText)
  } catch (e) {
    toast.error(messageOf(e))
  } finally {
    busy.value = ''
    await load()
  }
}

const changeRole = (u: AuthUser, role: UserRole) =>
  role === u.role ? undefined : patch(u, { role }, `已把 ${u.username} 的角色改为${roleLabel(role)}`)

const changeMembership = (u: AuthUser, membership: Membership) =>
  membership === u.membership
    ? undefined
    : patch(u, { membership }, `已把 ${u.username} 的档位改为${MEMBERSHIP_LABEL[membership]}`)

const toggleActive = (u: AuthUser) =>
  patch(
    u,
    { is_active: !u.is_active },
    u.is_active
      ? `已停用 ${u.username} —— 该账号将无法登录`
      : `已启用 ${u.username}`,
  )
</script>

<template>
  <main
    class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6 surface-cross"
  >
    <PageHeader
      :breadcrumb="['全友·智绘家', '账号管理']"
      title="账号管理"
      :status="{ icon: 'shield', text: '仅管理员', tone: 'botanical' }"
    />

    <!-- 加载失败（非管理员拿到 4002）时如实说一句，不留空白页 -->
    <div
      v-if="loadError"
      class="flex items-start gap-2.5 rounded-xl border border-accent-red/30 bg-accent-red/5 p-3.5"
    >
      <AppIcon name="warning-circle" :size="17" class="mt-0.5 shrink-0 text-accent-red" />
      <div class="min-w-0">
        <p class="text-[12px] font-semibold text-accent-red">{{ loadError }}</p>
        <p class="mt-1 text-[11px] leading-relaxed text-wood-muted">
          你自己的账号信息与套餐在「个人中心」。
        </p>
      </div>
    </div>

    <template v-else-if="data">
      <!-- ══ 概览 ══ -->
      <section class="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div
          v-for="s in [
            { label: '账号总数', value: stats.total, icon: 'users', tone: 'botanical' },
            { label: '不限量（管理员 / 设计师）', value: stats.unlimited, icon: 'shield-check', tone: 'botanical' },
            { label: '会员', value: stats.paid, icon: 'star', tone: 'gold' },
            { label: '免费版', value: stats.free, icon: 'user-circle', tone: 'muted' },
          ]"
          :key="s.label"
          class="card p-4"
        >
          <div class="flex items-start justify-between">
            <span class="text-[11px] leading-relaxed text-wood-muted">{{ s.label }}</span>
            <AppIcon
              :name="s.icon"
              :size="16"
              :class="
                s.tone === 'gold' ? 'text-accent-gold' : s.tone === 'muted' ? 'text-wood-muted/60' : 'text-botanical'
              "
            />
          </div>
          <div class="num mt-2 font-serif text-[26px] font-bold text-wood-dark">{{ s.value }}</div>
        </div>
      </section>

      <!-- 有停用账号时给一条显著提示。**停用是个安静的操作**，
           不留神就会忘了自己封过谁 —— 而那个人只会看到"登录失败" -->
      <div
        v-if="stats.disabled"
        class="flex items-start gap-2.5 rounded-xl border border-accent-gold/45 bg-wood-light/55 p-3"
      >
        <AppIcon name="warning-circle" :size="17" class="mt-0.5 shrink-0 text-accent-gold" />
        <p class="text-[11px] leading-relaxed text-wood">
          当前有 <span class="num font-semibold">{{ stats.disabled }}</span> 个账号处于
          <strong class="font-semibold">已停用</strong>状态 —— 它们无法登录，
          在表里以灰色标记。要恢复请点该行的「启用」。
        </p>
      </div>

      <!-- ══ 账号列表 ══ -->
      <section class="card overflow-hidden">
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-warm-border px-5 py-3.5">
          <h2 class="flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
            <AppIcon name="users" :size="16" class="text-botanical" />
            <span>全部账号</span>
          </h2>
          <span class="text-[10px] text-wood-muted">
            改动会落盘到
            <code class="rounded bg-warm-sidebar px-1 font-mono">data/users_override.json</code>
            （不进仓库）
          </span>
        </div>

        <div class="overflow-x-auto scroll-thin">
          <table class="w-full min-w-[860px] text-left">
            <thead>
              <tr class="border-b border-warm-grid text-[10px] uppercase tracking-wider text-wood-muted">
                <th class="px-5 py-2 font-semibold">账号</th>
                <th class="px-3 py-2 font-semibold">职务</th>
                <th class="px-3 py-2 font-semibold">角色</th>
                <th class="px-3 py-2 font-semibold">档位</th>
                <th class="px-3 py-2 font-semibold">可用范围</th>
                <th class="px-5 py-2 font-semibold">状态</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="u in data.users"
                :key="u.id"
                class="border-b border-warm-grid/60 last:border-0"
                :class="u.is_active ? '' : 'bg-warm-sidebar/40'"
              >
                <td class="px-5 py-3">
                  <div class="flex items-center gap-2.5">
                    <span
                      class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[12px] font-bold"
                      :class="
                        u.is_active
                          ? 'bg-botanical-light text-botanical'
                          : 'bg-warm-border/70 text-wood-muted'
                      "
                    >
                      {{ u.avatar_text }}
                    </span>
                    <span class="min-w-0">
                      <span
                        class="block truncate text-[12px] font-semibold"
                        :class="u.is_active ? 'text-wood-dark' : 'text-wood-muted line-through'"
                      >
                        {{ u.display_name }}
                      </span>
                      <span class="num block truncate text-[10px] text-wood-muted">{{ u.username }}</span>
                    </span>
                    <span
                      v-if="u.id === auth.user?.id"
                      class="shrink-0 rounded bg-botanical-light px-1.5 py-px text-[10px] font-semibold text-botanical"
                    >
                      当前登录
                    </span>
                  </div>
                </td>

                <td class="px-3 py-3 text-[11px] text-wood-muted">{{ u.title }}</td>

                <!-- 角色：管理员账号只读（③），自己只读（①），其余可改且不含 admin（②） -->
                <td class="px-3 py-3">
                  <select
                    v-if="!isAdminAccount(u) && u.id !== auth.user?.id"
                    class="field h-8 px-2 text-[11px]"
                    :value="u.role"
                    :disabled="busy === String(u.id)"
                    @change="changeRole(u, ($event.target as HTMLSelectElement).value as UserRole)"
                  >
                    <option v-for="r in assignableRoles" :key="r" :value="r">{{ roleLabel(r) }}</option>
                  </select>
                  <span v-else class="flex items-center gap-1 text-[11px] text-wood-muted">
                    {{ u.role_label }}
                    <span class="text-wood-muted/60">
                      （{{ isAdminAccount(u) ? '管理员账号不可改' : '不可改自己' }}）
                    </span>
                  </span>
                </td>

                <!-- 档位：对不限量的角色无意义，如实标"不适用"而不是给个改了没用的下拉 -->
                <td class="px-3 py-3">
                  <select
                    v-if="!isAdminAccount(u) && u.id !== auth.user?.id && u.role === 'user'"
                    class="field h-8 px-2 text-[11px]"
                    :value="u.membership"
                    :disabled="busy === String(u.id)"
                    @change="
                      changeMembership(u, ($event.target as HTMLSelectElement).value as Membership)
                    "
                  >
                    <option v-for="m in data.memberships" :key="m" :value="m">
                      {{ MEMBERSHIP_LABEL[m] }}
                    </option>
                  </select>
                  <span v-else class="text-[11px] text-wood-muted">
                    {{ u.is_unlimited ? '不适用' : MEMBERSHIP_LABEL[u.membership] }}
                  </span>
                </td>

                <td class="px-3 py-3">
                  <div class="flex flex-wrap gap-1">
                    <span v-if="u.is_unlimited" class="tag-botanical">全部功能 · 不限量</span>
                    <template v-else>
                      <span class="tag">解析 5 次/日</span>
                      <span v-if="u.can_use_paid_features" class="tag">生成 · 审查可用</span>
                      <span v-else class="tag border-accent-gold/40 bg-wood-light/60 text-wood">
                        生成 · 审查需开通
                      </span>
                    </template>
                  </div>
                </td>

                <!-- 封禁 / 启用 -->
                <td class="px-5 py-3">
                  <div class="flex items-center gap-2">
                    <span
                      class="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-semibold"
                      :class="
                        u.is_active
                          ? 'border border-botanical/20 bg-botanical-light text-botanical'
                          : 'border border-accent-red/30 bg-accent-red/5 text-accent-red'
                      "
                    >
                      <AppIcon :name="u.is_active ? 'check-circle' : 'x-circle'" :size="11" />
                      {{ u.is_active ? '正常' : '已停用' }}
                    </span>
                    <!-- 管理员账号不给封禁按钮（③）。不可逆性说明：
                         停用本身可逆（再点一次就恢复），所以这里不加二次确认 ——
                         行内状态已经把它变得可见。 -->
                    <button
                      v-if="!isAdminAccount(u) && u.id !== auth.user?.id"
                      class="btn-ghost px-2 py-1 text-[11px]"
                      type="button"
                      :disabled="busy === String(u.id)"
                      @click="toggleActive(u)"
                    >
                      {{ u.is_active ? '停用' : '启用' }}
                    </button>
                    <span v-else class="text-[10px] text-wood-muted/70">不可停用</span>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="border-t border-warm-border bg-warm-sidebar/40 px-5 py-3">
          <h3 class="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold text-wood-dark">
            <AppIcon name="info" :size="13" class="text-wood-muted" />
            <span>本页做不了的三件事，以及为什么</span>
          </h3>
          <ul class="space-y-1 text-[10px] leading-relaxed text-wood-muted">
            <li>
              · <strong class="font-semibold">不能改自己</strong> —— 演示时一旦把自己降级或停用，
              就没有账号能改回来了，只能手删 <code class="rounded bg-white px-1 font-mono">data/users_override.json</code>。
            </li>
            <li>
              · <strong class="font-semibold">不能把别人设成管理员</strong> ——
              本页做的是权限分配，但<strong class="font-semibold">不含管理员权限</strong>。
              这条路径一旦可点，一个误操作就能造出一个权限对等的账号，
              而且没有任何地方能看出是谁造的。
            </li>
            <li>
              · <strong class="font-semibold">不能停用管理员账号</strong> —— 封了管理员就没人能解封。
              要增减管理员请直接改 <code class="rounded bg-white px-1 font-mono">seed_data/users.json</code> 并重启。
            </li>
          </ul>
        </div>
      </section>
    </template>

    <p v-else-if="loading" class="text-[12px] text-wood-muted">正在读取账号列表…</p>
  </main>
</template>
