import axios, { AxiosError, type AxiosInstance, type AxiosResponse } from 'axios'

import { BizError, type ApiEnvelope } from './types'

/**
 * axios 封装。三件事：拆信封、分流错误、透传 trace_id。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么业务错误不能走 HTTP 4xx
 * ══════════════════════════════════════════════════════════════════
 * 需求文档 4.4 规定了「任务未完成时返回 200」——理由同样适用于所有业务错误：
 * axios 拦截器按 HTTP 状态码判成败，一旦用 4xx 表达"户型数据不足"，
 * 拦截器就会弹一个通用报错，而用户需要看到的是
 * 「缺少房间面积，建议重新上传更清晰的户型图」这种**可操作**的提示。
 *
 * 所以这里的分流是：
 *   HTTP 200 + code === 0   → resolve(data)
 *   HTTP 200 + code !== 0   → reject(BizError)，带 code / msg / data
 *   HTTP 4xx/5xx / 网络错误 → reject(BizError)，code 用负数区分
 *
 * ══════════════════════════════════════════════════════════════════
 * trace_id
 * ══════════════════════════════════════════════════════════════════
 * 每个请求生成一个，通过 `X-Trace-Id` 送出去，后端沿用同一个值贯穿
 * API → Agent → LLM → 审计日志，并在响应头回写。
 * 请求失败时把 trace_id 拼进错误消息 —— 用户截图报障时凭它就能定位日志。
 *
 * **`X-Trace-Id` 是后端支持的（main.py 的 TRACE_HEADER），不是这里自己发明的。**
 */

/** 本机没有 crypto.randomUUID 时（老浏览器 / 非 HTTPS 上下文）的兜底。 */
function newTraceId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID().replace(/-/g, '').slice(0, 32)
  }
  return Math.random().toString(16).slice(2).padEnd(32, '0').slice(0, 32)
}

/** 网络层负数码。与后端的正数码分开，便于界面区分"业务失败"和"没连上"。 */
export const NetErrorCode = {
  /** 没连上后端 */
  UNREACHABLE: -1,
  /** 后端返回了非 200 的 HTTP 状态 */
  HTTP_ERROR: -2,
  /** HTTP 200 但响应体不是预期的信封 */
  MALFORMED: -3,
  /** 主动取消（组件卸载） */
  CANCELED: -4,
} as const

export interface RequestMeta {
  traceId: string
  /** 后端中间件回写的耗时，单位毫秒。性能埋点直接用这个，不要在前端自己掐表。 */
  elapsedMs: number | null
}

const http: AxiosInstance = axios.create({
  baseURL: '/api/v1',
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
})

/** 最近一次请求的元信息。错误提示要展示 trace_id，所以得留一份。 */
let lastMeta: RequestMeta = { traceId: '', elapsedMs: null }
export const getLastMeta = (): RequestMeta => lastMeta

http.interceptors.request.use((config) => {
  const traceId = newTraceId()
  lastMeta = { traceId, elapsedMs: null }
  config.headers.set('X-Trace-Id', traceId)
  return config
})

http.interceptors.response.use(
  (resp: AxiosResponse<ApiEnvelope>) => resp,
  (err: AxiosError) => {
    // 把网络层错误归一成 BizError，组件里就只需要 catch 一种东西
    const traceId = lastMeta.traceId
    if (axios.isCancel(err)) {
      throw new BizError(NetErrorCode.CANCELED, '请求已取消', { traceId })
    }
    if (err.response) {
      const detail =
        (err.response.data as { detail?: string; msg?: string } | undefined)?.detail ??
        (err.response.data as { msg?: string } | undefined)?.msg ??
        err.response.statusText
      throw new BizError(
        NetErrorCode.HTTP_ERROR,
        `服务返回 ${err.response.status}：${detail}`,
        { traceId, status: err.response.status },
      )
    }
    throw new BizError(
      NetErrorCode.UNREACHABLE,
      '连不上后端服务。请确认 `uvicorn backend.app.main:app --port 8000` 已在运行。',
      { traceId },
    )
  },
)

/**
 * 发一个请求并拆信封。
 *
 * 泛型 `T` 是 `data` 字段的类型 —— 调用方写 `request<ParseResult>(...)`。
 */
export async function request<T>(
  method: 'get' | 'post' | 'delete',
  url: string,
  payload?: unknown,
  config: { timeout?: number; signal?: AbortSignal } = {},
): Promise<T> {
  const resp = await http.request<ApiEnvelope<T>>({
    method,
    url,
    data: payload,
    timeout: config.timeout,
    signal: config.signal,
  })

  const traceId = String(resp.headers['x-trace-id'] ?? lastMeta.traceId)
  const elapsedRaw = resp.headers['x-elapsed-ms']
  lastMeta = {
    traceId,
    elapsedMs: elapsedRaw ? Number(elapsedRaw) : null,
  }

  const body = resp.data
  // 后端所有接口都走统一信封。拿不到信封说明命中了别的路由（比如被代理
  // 拦到了前端自己的 index.html）—— 这时报"响应格式异常"比报"解析失败"准。
  if (!body || typeof body !== 'object' || !('code' in body)) {
    throw new BizError(NetErrorCode.MALFORMED, '服务响应格式异常（缺少统一信封）', {
      traceId,
      received: String(resp.data).slice(0, 200),
    })
  }

  if (body.code !== 0) {
    throw new BizError(body.code, body.msg || '操作失败', { ...(body.data as object), traceId })
  }

  return body.data as T
}

/**
 * 取一个**不走统一信封**的接口的原始文本（目前只有 `/plan.svg`）。
 *
 * ⚠️ 不能复用 `request<T>()`：那个函数的第一件事就是拆 `{code,msg,data}`，
 * 拆不动就抛 MALFORMED。而矢量图接口故意返回裸 `image/svg+xml` ——
 * 它的消费者是 `<img src>` 和浏览器窗口，套上信封就没法渲染了。
 *
 * 所以这里单独开一条路：只做错误归一，不碰响应体。
 */
export async function requestText(
  url: string,
  config: { timeout?: number; signal?: AbortSignal } = {},
): Promise<string> {
  const resp = await http.request<string>({
    method: 'get',
    url,
    timeout: config.timeout,
    signal: config.signal,
    responseType: 'text',
    // 覆盖掉默认的 application/json，否则某些代理/浏览器会尝试按 JSON 解析
    headers: { Accept: 'image/svg+xml,*/*' },
    transformResponse: [(d: unknown) => d],   // 不要让 axios 试着解析
  })
  return typeof resp.data === 'string' ? resp.data : String(resp.data)
}

/** 从 BizError 里取 trace_id，供界面展示。 */
export function traceIdOf(err: unknown): string {
  if (err instanceof BizError && err.data && typeof err.data === 'object') {
    const t = (err.data as { traceId?: string }).traceId
    if (t) return t
  }
  return lastMeta.traceId
}

/** 把任意异常转成可直接展示的中文文案。组件里不用再各写一份。 */
export function messageOf(err: unknown): string {
  if (err instanceof BizError) {
    if (err.code === NetErrorCode.CANCELED) return ''
    const trace = traceIdOf(err)
    return trace ? `${err.message}（trace: ${trace.slice(0, 8)}）` : err.message
  }
  if (err instanceof Error) return err.message
  return String(err)
}

export default http
