/**
 * 访问令牌的存取。**单独一个模块，就是为了打断循环依赖。**
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么不放在 auth store 里
 * ══════════════════════════════════════════════════════════════════
 * 需要令牌的有两处，而它们互相不能 import：
 *
 *   client.ts     每个请求要带 `Authorization`，拿到 4003 要清令牌
 *   stores/auth.ts 登录后写入、登出后清除
 *
 * 如果 `client.ts` 去 import store，链条会变成
 *   client → store → api/index → client      ← 环
 * Pinia 的 store 在模块初始化时就要用到 pinia 实例，环里的求值顺序
 * 一旦变化就是运行时报错（而且只在某些页面复现）。
 *
 * 所以把「令牌存在哪」这件事下沉到本模块：它只依赖 localStorage，
 * 谁都可以 import，谁都读得到同一个值。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么是 localStorage
 * ══════════════════════════════════════════════════════════════════
 * 演示场景要的是**"刷新页面不用重新登录"**（需求 4.1 的口径）。
 * sessionStorage 撑不住这一点，而 httpOnly Cookie 需要后端配合
 * 改认证方式。本项目自签发 HS256 令牌、服务只监听 127.0.0.1，
 * localStorage 的 XSS 面在这里不构成实际风险 —— 这是一个**有边界的
 * 取舍**，不是"忘了考虑"：真要上公网，这条必须改成 httpOnly Cookie。
 *
 * ⚠️ **只存令牌，不存用户信息。** 用户信息每次向 `/auth/me` 重新问
 * （理由见 `api/index.ts` 的 `authMe` 与后端 `auth_me` 的注释）：
 * 管理员改了某人的档位，刷新就该看到新的，而不是读出一个过期快照。
 */

const TOKEN_KEY = 'qy.access_token'

/**
 * localStorage 在某些环境下会**抛异常**（隐私模式的 Safari、
 * 被策略禁用存储的嵌入式浏览器）。存取都不能因此让整个应用起不来 ——
 * 那种情况下退化成"不能保持登录"，而不是白屏。
 */
function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn()
  } catch {
    return fallback
  }
}

export function getToken(): string {
  return safe(() => localStorage.getItem(TOKEN_KEY) ?? '', '')
}

export function setToken(token: string): void {
  safe(() => {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  }, undefined)
}

export function clearToken(): void {
  setToken('')
}

export function hasToken(): boolean {
  return getToken().length > 0
}
