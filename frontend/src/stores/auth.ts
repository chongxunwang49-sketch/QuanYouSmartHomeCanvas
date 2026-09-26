import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { authLogin, authMe } from '@/api'
import { clearToken, getToken, setToken } from '@/api/token'
import type { AuthUser } from '@/api/types'

/**
 * 登录态与权限判定（AC-01）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 这里判的不是"能不能用"，是"界面该怎么显示"
 * ══════════════════════════════════════════════════════════════════
 * 门控做三层，**缺一层都不算数**（`backend/app/api/deps.py` 的模块说明）：
 *
 *   · 界面层  本文件 —— 不可用的入口置灰并说明原因
 *   · 接口层  `api/deps.py` 的 `require_paid` / `consume_quota` —— 真正拦得住 curl
 *   · 额度层  `core/redis_client.py` 的按天计数
 *
 * ⚠️ **前端的判定永远只是"提示"，不是"防线"。** 打开 DevTools 就能绕过
 * 界面层的置灰，所以后端的 4005 / 4006 才是真判据。这也意味着：
 * 前端每一处置灰都**必须**同时准备好接住后端返回的 4005 / 4006 ——
 * 不能假设"我置灰了它就不会被调用"（配额是按用户在后端算的，
 * 前端根本不知道今天已经用了几次）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 用户信息**不持久化**
 * ══════════════════════════════════════════════════════════════════
 * localStorage 里只有令牌。用户信息每次刷新向 `/auth/me` 重新问 ——
 * 管理员刚改了某人的角色/档位，刷新就该看到新的，而不是读出一个
 * 过期快照。代价是每次刷新多一个 8ms 的请求，完全可接受。
 *
 * 后端 `auth_me` 的注释写的是同一件事，两处必须一致。
 */

/** 需要开通会员的功能。**值与后端 `require_paid(user, feature)` 的入参一致。** */
export const PAID_FEATURES = {
  GENERATE: '方案生成',
  REVIEW: '避坑审查',
} as const

export type PaidFeatureKey = keyof typeof PAID_FEATURES

export const useAuthStore = defineStore('auth', () => {
  // ── 状态 ────────────────────────────────────────────────
  /** 令牌同步初值取本地：刷新页面时先认为"可能已登录"，再由 restore() 证实 */
  const token = ref(getToken())
  /**
   * 当前用户。**`null` 且 `restored === false` 时表示"还不知道"**，
   * 界面这个阶段不能当作"未登录"处理 —— 否则刷新会闪一下登录页。
   */
  const user = ref<AuthUser | null>(null)
  /** 首次向 /auth/me 求证是否完成。路由守卫靠它决定要不要等 */
  const restored = ref(false)
  const submitting = ref(false)
  /** 登录失败的文案。**原样展示后端给的那句**，不要自己编 */
  const error = ref('')

  // ── 派生 ────────────────────────────────────────────────
  const isLoggedIn = computed(() => user.value !== null)
  const displayName = computed(() => user.value?.display_name ?? '')
  const role = computed(() => user.value?.role ?? null)
  const membership = computed(() => user.value?.membership ?? null)

  /** 不受额度限制：管理员 / 设计师。与后端 `User.is_unlimited` 同一判据 */
  const isUnlimited = computed(() => user.value?.is_unlimited ?? false)
  /** 付费功能可用。与后端 `User.can_use_paid_features` 同一判据 */
  const canUsePaid = computed(() => user.value?.can_use_paid_features ?? false)
  const isAdmin = computed(() => role.value === 'admin')
  /** 管理员与设计师都能进「用户管理」看全局账号表 */
  const canManageUsers = computed(() => isUnlimited.value)

  /**
   * 某个付费功能是否可用。
   *
   * ⚠️ 返回值只用于**界面显示**。真正的拒绝在后端 `require_paid`。
   */
  function canUsePaidFeature(_feature: PaidFeatureKey): boolean {
    return canUsePaid.value
  }

  /**
   * 不可用时该对用户说什么。**拒绝必须说清缺什么、怎么办**
   * （`core/capabilities.py` 的立场，AC-33 已定成全项目的规矩）。
   *
   * 返回 `''` 表示可用。
   */
  function paidGateHint(feature: PaidFeatureKey): string {
    if (canUsePaid.value) return ''
    const name = PAID_FEATURES[feature]
    if (!user.value) return `「${name}」需要登录后使用`
    return `「${name}」需要开通会员。当前档位：免费版 —— 在「用户管理 → 我的套餐」里开通演示会员即可解锁（演示环境，不会真实扣费）`
  }

  // ── 动作 ────────────────────────────────────────────────

  /**
   * 首次求证登录态。**幂等且只跑一次**。
   *
   * 路由守卫在每次导航前都会调它，但真正的 `/auth/me` 只发一次：
   * 否则用户在页面间点来点去会打出一串重复请求。
   *
   * 令牌无效时不在这里跳转 —— 那是 `main.ts` 注册的 4003 回调的活，
   * 两处都做会互相打架。
   */
  async function restore(): Promise<void> {
    if (restored.value) return
    if (!token.value) {
      restored.value = true
      return
    }
    try {
      const data = await authMe()
      user.value = data.user
    } catch {
      // 令牌过期 / 被吊销 / 后端没起来。**一律当作未登录**，
      // 不区分原因：用户能做的动作都是同一个（重新登录）。
      clearToken()
      token.value = ''
      user.value = null
    } finally {
      restored.value = true
    }
  }

  /** 重新拉一次当前用户。管理员在「用户管理」里改完角色后调它。 */
  async function refresh(): Promise<void> {
    if (!token.value) return
    try {
      const data = await authMe()
      user.value = data.user
    } catch {
      /* 保留旧值：刷新失败不该把用户踢下线 */
    }
  }

  async function login(username: string, password: string): Promise<boolean> {
    submitting.value = true
    error.value = ''
    try {
      const data = await authLogin({ username: username.trim(), password })
      setToken(data.access_token)
      token.value = data.access_token
      user.value = data.user
      restored.value = true
      return true
    } catch (e) {
      // 口令错误时后端返回同一句话，不区分"用户不存在"与"口令错误"
      // （区分开就是用户名枚举接口）。这里原样展示，不要自作聪明地猜。
      error.value = e instanceof Error ? e.message : String(e)
      return false
    } finally {
      submitting.value = false
    }
  }

  /**
   * 登出。**只清本地** —— 后端没有登出接口。
   *
   * 这是自签发无状态令牌的固有代价：服务端不持有会话，没法主动吊销。
   * 演示场景可接受（令牌 12 小时自然过期），真要能做吊销就得引入
   * 服务端会话表或黑名单 —— 那是另一种架构，不在本项目范围内。
   */
  function logout(): void {
    clearToken()
    token.value = ''
    user.value = null
    restored.value = true
    error.value = ''
  }

  return {
    token,
    user,
    restored,
    submitting,
    error,
    isLoggedIn,
    displayName,
    role,
    membership,
    isUnlimited,
    canUsePaid,
    isAdmin,
    canManageUsers,
    canUsePaidFeature,
    paidGateHint,
    restore,
    refresh,
    login,
    logout,
  }
})
