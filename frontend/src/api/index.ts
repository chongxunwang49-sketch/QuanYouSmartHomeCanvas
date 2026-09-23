/**
 * 六个接口。与 `backend/app/api/routes.py` 一一对应。
 *
 * 四个异步接口（parse / generate / review）形状一致：立即拿 task_id，
 * 再轮询 `/task/{id}/status`。两个同步接口（material/price、system/health）
 * 直接返回结果。
 */

import { request, requestText } from './client'
import type {
  GenerateRequest,
  HealthData,
  MaterialPriceData,
  ParseRequest,
  PlanRenderData,
  ReviewRequest,
  TaskCreated,
  TaskStatusData,
} from './types'

// ══════════════════════════════════════════════════════════════════
// 4.2 户型解析（异步）
// ══════════════════════════════════════════════════════════════════

/**
 * 提交户型图解析。
 *
 * `image` 接受 data URI 或裸 base64 —— 图在上传前会被转成 data URI，
 * **不经过任何第三方**。`prefer_local: true` 时后端在提供方选择层强制
 * 只走本机 Ollama，图像不出本机（ADR-12，已抓包验证）。
 *
 * 只跑「解析 → 诊断」这一段，实测 33–48 秒。
 */
export const parseLayout = (body: ParseRequest) =>
  request<TaskCreated>('post', '/layout/parse', body)

// ══════════════════════════════════════════════════════════════════
// 4.3 方案生成（异步）
// ══════════════════════════════════════════════════════════════════

/**
 * 按 `layout_id` 生成方案。实测约 95 秒。
 *
 * ⚠️ **不重新解析户型图。** 视觉解析约 16 秒且有随机性——同一个
 * `layout_id` 重新解析可能得到不同的房间数，于是用户看到的户型
 * 和生成方案用的不是同一份，而界面上写着同一个 id。
 * 同一个 id 必须指同一份数据。
 *
 * `styles` 与 `budget_grades` **按位置配对**：styles[0] 配 budget_grades[0]，
 * 产出的方案数取两者较短的。
 */
export const designGenerate = (body: GenerateRequest) =>
  request<TaskCreated>('post', '/design/generate', body)

// ══════════════════════════════════════════════════════════════════
// 4.6 报价单 / 合同审查（异步）
// ══════════════════════════════════════════════════════════════════

/** 单次 A-06 实测约 19 秒。走 quote 模式（分支内的 plan 模式由链上自动触发）。 */
export const avoidPitReview = (body: ReviewRequest) =>
  request<TaskCreated>('post', '/avoid-pit/review', body)

// ══════════════════════════════════════════════════════════════════
// 4.4 任务状态轮询（同步）
// ══════════════════════════════════════════════════════════════════

/**
 * 查询任务状态。
 *
 * ⚠️ **未完成时同样返回 200 + code=0**，只是 `status`/`phase` 不同。
 * 见 client.ts 的说明。
 *
 * 响应标了 `Cache-Control: no-store`，所以这里不要再叠一层前端缓存 ——
 * 一旦缓存，界面会卡在某个阶段不动。
 */
export const taskStatus = <R = unknown>(taskId: string, signal?: AbortSignal) =>
  request<TaskStatusData<R>>('get', `/task/${taskId}/status`, undefined, {
    signal,
    // 轮询请求要快进快出，卡住会拖慢整条轮询节奏
    timeout: 10_000,
  })

// ══════════════════════════════════════════════════════════════════
// 4.3′ 矢量图与热区（同步，AC-07 / AC-09 / AC-21）
// ══════════════════════════════════════════════════════════════════

/**
 * 热区数据 + 画布变换参数。
 *
 * ⚠️ 返回的 `hotspots[].bbox` / `polygon` 是**画布像素**，只对同一
 * `layout_id` 的 `/plan.svg` 有意义。两个接口在后端共用同一个纯函数
 * 计算变换，所以只要 `layout_id` 相同，坐标必然对得上 ——
 * **前端不需要、也不应该自己做任何坐标换算。**
 */
export const layoutHotspots = (layoutId: string) =>
  request<PlanRenderData>('get', `/layout/${encodeURIComponent(layoutId)}/hotspots`, undefined, {
    timeout: 10_000,
  })

/**
 * 取矢量户型图的 SVG **源码**。
 *
 * 拿源码而不是 `<img src>` 的 URL，是为了把 SVG 内联进 DOM ——
 * 内联之后热区的命中测试交给浏览器（`fill-rule="evenodd"` 的挖空区域
 * 会自动穿透到下层），前端一行几何计算都不用写。
 * `<img>` 方式下这些都做不到。
 */
export const layoutPlanSvg = (layoutId: string, signal?: AbortSignal) =>
  requestText(`/layout/${encodeURIComponent(layoutId)}/plan.svg`, {
    timeout: 10_000,
    signal,
  })

/** 给"在新窗口打开/下载"用的直链。 */
export const layoutPlanSvgUrl = (layoutId: string) =>
  `/api/v1/layout/${encodeURIComponent(layoutId)}/plan.svg`

// ══════════════════════════════════════════════════════════════════
// 4.5 材料价格查询（同步，纯查表）
// ══════════════════════════════════════════════════════════════════

/**
 * 材料价格查询。毫秒级返回，不调模型、不查向量库。
 *
 * 返回体里的 `disclaimer` **必须展示** —— 演示数据要显著标注（需求文档
 * 5.3 / R-09），而这是最容易被前端"顺手美化"掉的地方。
 */
export async function materialPrice(params: {
  category?: string
  brand?: string
  quanyou_first?: boolean
}): Promise<MaterialPriceData> {
  const qs = new URLSearchParams()
  if (params.category) qs.set('category', params.category)
  if (params.brand) qs.set('brand', params.brand)
  if (params.quanyou_first === false) qs.set('quanyou_first', 'false')
  const suffix = qs.toString() ? `?${qs}` : ''
  return request<MaterialPriceData>('get', `/material/price${suffix}`, undefined, {
    timeout: 10_000,
  })
}

// ══════════════════════════════════════════════════════════════════
// 4.6 健康检查（同步）
// ══════════════════════════════════════════════════════════════════

/**
 * 健康检查。**整体永远返回 200**，哪个依赖挂了由 `checks` 说清楚。
 *
 * 这是刻意的：知识库没起来时前端仍应能打开页面、看到"知识库不可用"，
 * 而不是拿到一个连不上的服务。
 */
export const systemHealth = () => request<HealthData>('get', '/system/health', undefined, {
  timeout: 8_000,
})

export * from './types'
export { getLastMeta, messageOf, traceIdOf, NetErrorCode } from './client'
