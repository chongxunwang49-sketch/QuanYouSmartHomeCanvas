<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import AppIcon from './AppIcon.vue'
import { useAuthStore, type PaidFeatureKey } from '@/stores/auth'

/**
 * 「需要开通会员」的置灰说明条（AC-01 门控的**界面层**）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么是"置灰 + 说明"而不是"整个藏起来"
 * ══════════════════════════════════════════════════════════════════
 * 本项目对拒绝的立场是「必须说清缺什么、怎么办」
 * （`core/capabilities.py`，AC-33 已定成全项目的规矩）。
 * 把方案生成对免费用户整个藏起来，他就永远不知道有这个东西、
 * 也不知道开通能解锁什么 —— 那不叫权限控制，叫功能消失。
 *
 * 所以正确形态是：**入口在、不可用、原因写在旁边、下一跳可点**。
 *
 * ══════════════════════════════════════════════════════════════════
 * ⚠️ 这只是提示，不是防线
 * ══════════════════════════════════════════════════════════════════
 * 真正的拦截在后端 `api/deps.py` 的 `require_paid`（4005）。
 * 前端每一处引用本组件的地方，都**必须**同时能接住 4005 ——
 * 因为配额是按用户在后端算的，前端根本不知道"今天已经用了几次"，
 * 更无法阻止有人绕开界面直接打接口。
 *
 * 用法：
 *   <PaidGateNotice feature="GENERATE" />
 * 可用时渲染 `null`，什么都不占位。
 */
const props = defineProps<{
  feature: PaidFeatureKey
}>()

const auth = useAuthStore()
const router = useRouter()

/**
 * 不可用时后端会给出的那句说明。**复用同一份文案**，不要在这里另写一句。
 *
 * ⚠️ 曾经这里还有一个 `name`（从 `PAID_FEATURES` 取中文名）用来拼标题，
 * 但后端那句话的开头就是同一个名字 —— 两行在说同一件事。
 * 现在中文名只在 `stores/auth.ts` 的 `PAID_FEATURES` 里维护一处。
 */
const hint = computed(() => auth.paidGateHint(props.feature))

/**
 * 管理员与设计师不受档位限制，**连这条提示都不该看见** ——
 * 给一个"你已开通"的通知等于噪音。
 */
const visible = computed(() => Boolean(auth.user) && !auth.canUsePaid)
</script>

<template>
  <div
    v-if="visible"
    class="flex items-start gap-3 rounded-xl border border-accent-gold/45 bg-wood-light/55 p-3.5"
  >
    <span
      class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg
             border border-accent-gold/40 bg-white/70 text-accent-gold"
    >
      <AppIcon name="shield" :size="16" weight="bold" />
    </span>

    <div class="min-w-0 flex-1">
      <!--
        ⚠️ **不要再套一层自己的标题。** 原来这里写的是
        「「方案生成」需要开通会员」，而后端 `paidGateHint` 给的那句话
        开头也是同一句 —— 渲染出来是"「方案生成」需要开通会员 / 「方案生成」
        需要开通会员。当前档位：免费版……"，两行在说同一件事。

        后端那句话本身就是**完整且可操作**的（说清了缺什么、当前档位、
        去哪开通、会不会真扣费），所以直接把它当主句。
        拒绝的文案只有一处真源，才不会两边漂移。
      -->
      <p class="text-[12px] font-semibold leading-relaxed text-wood-dark">
        {{ hint }}
      </p>
      <p class="mt-1 text-[10px] leading-relaxed text-wood-muted">
        当前账号：{{ auth.user?.username }}（{{ auth.user?.role_label }} ·
        {{ auth.membership === 'paid' ? '会员' : '免费版' }}）。
        免费版仍可使用户型解析（每日 5 次）。
      </p>
    </div>

    <!-- 指向**个人中心**而不是账号管理 —— 后者只有管理员能进，
         把免费用户指过去等于指向一扇进不去的门（`deps.UPGRADE_HINT` 同此口径） -->
    <button class="btn-soft shrink-0 px-3 py-1.5" type="button" @click="router.push('/me')">
      <AppIcon name="arrow-right" :size="14" weight="bold" />
      <span>去开通</span>
    </button>
  </div>
</template>
