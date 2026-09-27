/**
 * 接口目录。与 `backend/app/api/routes.py` 一一对应。
 *
 * 三种形状：
 *   · 异步任务（parse / generate / review）—— 立即拿 task_id，再轮询
 *     `/task/{id}/status`
 *   · 认证（auth/login、auth/me）—— 直接返回结果，**唯一不需要令牌的是 login**
 *   · 同步查询（material/price、system/health、layout/*）—— 直接返回结果
 */

import { request, requestText } from './client'
import type {
  DashboardStatsData,
  DevicesData,
  Diagnosis,
  FloorMaterialsData,
  GenerateRequest,
  HouseDetail,
  HouseDetailSample,
  KnowledgeListData,
  KnowledgeUploadResult,
  HealthData,
  LoginData,
  LoginRequest,
  MaterialPriceData,
  MaterialOptionsData,
  MetricsData,
  FurnitureData,
  MeData,
  Membership,
  ParseRequest,
  PlanRenderData,
  QuotaData,
  UserListData,
  UserRole,
  WalkableResponse,
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

/**
 * 中断一个在飞任务（AC-31）。
 *
 * ⚠️ **三个"什么都没发生"的情况是正常响应（code 0），不是错误**：
 * 任务刚好跑完、任务早就不在服务端了、重复点两次。所以调用方
 * 不要看到 200 就以为"确实中断成功了" —— 判据是返回体里的
 * `cancelled` 字段（true = 真的被这一次请求停掉了）。
 *
 * ⚠️ 额度**不退**：后端在建任务之前就扣了。确认框里必须提前写明，
 * 不能等用户点完再说（这是需求文档里定下的口径）。
 *
 * 超时给 15 秒：后端要等协程真的退出（上限 5 秒）并写终态记录，
 * 比普通请求慢，但那正是"接口回来时状态已经是终态"的代价。
 */
export const cancelTask = <R = unknown>(taskId: string) =>
  request<TaskStatusData<R> & { cancelled: boolean }>(
    'post', `/task/${taskId}/cancel`, undefined, { timeout: 15_000 },
  )

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
export const layoutPlanSvg = (
  layoutId: string, signal?: AbortSignal, revision = 0,
) =>
  // ⚠️ `?v=` 不是装饰：`/plan.svg` 带 `private, max-age=300`，换了地面材质
  //    之后 URL 不变的话浏览器会拿缓存里的旧图 —— 表现为"接口成功了、
  //    图上没变化"，而且控制台一片安静。见 PlanViewer 的 `revision` 说明。
  requestText(
    `/layout/${encodeURIComponent(layoutId)}/plan.svg${revision ? `?v=${revision}` : ''}`,
    { timeout: 10_000, signal },
  )

/**
 * 3D 漫游的全部几何输入：米制场景 + 碰撞线段 + 连通图 + 出生点。
 *
 * ⚠️ 返回体里的 `walkable.mode` 决定前端走哪条路：
 * `walk` 做第一人称贴地行走，`fly` 降级成自由视角。
 * **不要自己判断能不能走** —— 判据（墙闭合、房间站得下、门够宽、连通）
 * 都在后端，且有测试。前端重算一遍就会出现两套判据。
 */
export const layoutWalkable = (layoutId: string, planId?: string) => {
  const qs = planId ? `?plan_id=${encodeURIComponent(planId)}` : ''
  return request<WalkableResponse>(
    'get', `/layout/${encodeURIComponent(layoutId)}/walkable${qs}`, undefined,
    { timeout: 10_000 },
  )
}

/** 给"在新窗口打开/下载"用的直链。 */
export const layoutPlanSvgUrl = (layoutId: string) =>
  `/api/v1/layout/${encodeURIComponent(layoutId)}/plan.svg`

// ══════════════════════════════════════════════════════════════════
// 4.1 认证（同步）
// ══════════════════════════════════════════════════════════════════

/**
 * 登录。**唯一不需要令牌的写接口**（否则死锁）。
 *
 * ⚠️ 口令错误时后端返回**同一句话**，不区分"用户不存在"与"口令错误" ——
 * 区分开就等于给了一个用户名枚举接口。前端也不要去猜测是哪种，
 * 直接展示后端给的那句。
 */
export const authLogin = (body: LoginRequest) =>
  request<LoginData>('post', '/auth/login', body, { timeout: 15_000 })

/**
 * 当前用户。**刷新页面后靠它恢复登录态。**
 *
 * 令牌存在 localStorage，但**用户信息不持久化**，每次重新问一次：
 * 管理员刚改了某人的角色/档位，刷新就该看到新的，而不是从 localStorage
 * 里读出一个过期快照。后端 `auth_me` 的注释写的是同一件事。
 */
export const authMe = () => request<MeData>('get', '/auth/me', undefined, { timeout: 8_000 })

/**
 * 我今日的额度（AC-13）。只读，不消耗。
 *
 * ⚠️ **不要在本地自己累加。** 额度是按用户在后端按天算的，
 * 前端记的计数在换设备、刷新页面、并发提交之后全是错的。
 */
export const authQuota = () => request<QuotaData>('get', '/auth/quota', undefined, { timeout: 8_000 })

/**
 * 自助开通 / 取消会员（演示开关，没有扣费）。
 *
 * ⚠️ 只对普通用户有效。管理员与设计师不受档位限制，后端会返回 `4002`
 * 并说明原因 —— 前端要把那句原样展示，不要自己吞掉。
 */
export const setMyMembership = (membership: Membership) =>
  request<MeData>('post', '/auth/membership', { membership }, { timeout: 10_000 })

/**
 * 全部账号。**仅管理员**，其他角色返回 `4002`。
 *
 * 刻意不在前端做角色判断来决定要不要发这个请求 —— 那样前端就成了判据。
 * 判据在后端，前端只管把拒绝的理由展示出来。
 */
export const listUsers = () => request<UserListData>('get', '/users', undefined, { timeout: 10_000 })

/** 我的登录设备（个人中心）。只读。 */
export const myDevices = () => request<DevicesData>('get', '/me/devices', undefined, { timeout: 8_000 })

/**
 * 管理员改**别人**的角色 / 档位 / 启用状态。
 *
 * ⚠️ 三种情况后端会拒绝，前端要把理由原样展示（不要自己另写一句）：
 *   · 改自己
 *   · 把 role 设成 `admin`（本页不提供管理员权限分配）
 *   · 动一个**管理员**账号（改角色/档位/封禁都不行）
 */
export const updateUser = (
  userId: number,
  body: { role?: UserRole; membership?: Membership; is_active?: boolean },
) => request<MeData>('post', `/users/${userId}`, body, { timeout: 10_000 })

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

/**
 * 材料偏好的可选项（AC-19）。
 *
 * **不要在前端硬编码品类与品牌清单。** 硬编码的那份会随后端目录变更而
 * 静默漂移：界面照常显示一个后端已经不认的品类，用户勾上，请求被 4001 退回，
 * 在用户看来像是后端坏了。清单与常量都从目录取，只有一处定义。
 */
export async function materialOptions(): Promise<MaterialOptionsData> {
  return request<MaterialOptionsData>('get', '/material/options', undefined, {
    timeout: 10_000,
  })
}

/**
 * 3D 家具摆放。
 *
 * **纯规则算**，坐标与 `/walkable` 同一套场景坐标系（米）。
 * 摆不下的家具在 `rejected` 里带原因 —— 调用方**不要把它当噪音丢掉**，
 * 那是"少了一件"的唯一解释。
 */
export async function layoutFurniture(
  layoutId: string,
  opts: { style?: string; planId?: string } = {},
): Promise<FurnitureData> {
  const qs = new URLSearchParams()
  if (opts.style) qs.set('style', opts.style)
  if (opts.planId) qs.set('plan_id', opts.planId)
  const suffix = qs.toString() ? `?${qs}` : ''
  // SVG 渲染实测毫秒级，但摆放要遍历房间 × 目录，给足余量
  return request<FurnitureData>('get', `/layout/${layoutId}/furniture${suffix}`, undefined, {
    timeout: 15_000,
  })
}

// ══════════════════════════════════════════════════════════════════
// 4.3′ 屋主户型详情 + 五维诊断
// ══════════════════════════════════════════════════════════════════

/**
 * 屋主补充的「户型详情」（**文字资料**，不是图片）。
 *
 * 平面图是二维的：层高、朝向、采光面、通风路径、收纳位置它都没有，
 * 而诊断的五个维度恰好都要用这些 —— 这就是"综合评分低、数据不足"的根因。
 */
export const layoutHouseDetail = (layoutId: string) =>
  request<{ layout_id: string; house_detail: HouseDetail | null }>(
    'get', `/layout/${encodeURIComponent(layoutId)}/house-detail`, undefined,
    { timeout: 10_000 },
  )

/** 保存屋主填的户型详情。**保存后要重新诊断才有意义**（见 `rerunDiagnosis`）。 */
export const saveHouseDetail = (
  layoutId: string,
  body: { text: string; title?: string },
) =>
  request<{ layout_id: string; house_detail: HouseDetail }>(
    'post', `/layout/${encodeURIComponent(layoutId)}/house-detail`, body,
    // 提示词侧不做校验，服务端只存文本；超长会被后端拒（4001）
    { timeout: 15_000 },
  )

/**
 * 演示用的户型详情样例（后端按**套内面积最接近**挑一份）。
 *
 * ⚠️ 拿到之后是**填进输入框、由用户点保存**，不是后端自动替他提交 ——
 * 数据得由人确认一次。
 */
export const sampleHouseDetail = (layoutId: string) =>
  request<HouseDetailSample>(
    'get', `/layout/${encodeURIComponent(layoutId)}/house-detail/sample`, undefined,
    { timeout: 10_000 },
  )

/** 读五维诊断：有缓存就直接给，没有就现跑一次。 */
export const layoutDiagnosis = (layoutId: string) =>
  request<{ layout_id: string; diagnosis: Diagnosis }>(
    'get', `/layout/${encodeURIComponent(layoutId)}/diagnosis`, undefined,
    // 现跑要一次 LLM 调用，给足余量（A-02 超时是 60s）
    { timeout: 90_000 },
  )

/** 强制重跑五维诊断（补完户型详情之后用）。 */
export const rerunDiagnosis = (layoutId: string) =>
  request<{ layout_id: string; diagnosis: Diagnosis }>(
    'post', `/layout/${encodeURIComponent(layoutId)}/diagnose`, undefined,
    { timeout: 90_000 },
  )

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

// ══════════════════════════════════════════════════════════════════
// 4.6′ 知识库管理 + 工作台统计（同步）
// ══════════════════════════════════════════════════════════════════
//
// ⚠️ 知识库两条**需要管理员**（后端 4002）。前端不做第二份角色判断 ——
// 菜单已经按角色显隐（`config/nav.ts`），真判据在服务端。
// 非管理员调用了就把后端那句话原样显示出来，而不是编一句自己的。

export const knowledgeList = () =>
  request<KnowledgeListData>('get', '/knowledge/list', undefined, { timeout: 15_000 })

/**
 * 入库一段文本。
 *
 * ⚠️ 超时给到 60 秒：这一步要**同步等 embedding**（几十条 chunk 约几秒），
 * 而上限 20 万字符。默认超时会在慢机器上把它判成失败 ——
 * 而失败的后端其实可能已经写完了。
 */
export const knowledgeUpload = (body: {
  title: string
  text: string
  doc_type?: string
  tags?: string[]
}) => request<KnowledgeUploadResult>('post', '/knowledge/upload', body, { timeout: 60_000 })

export const dashboardStats = () =>
  request<DashboardStatsData>('get', '/dashboard/stats', undefined, { timeout: 15_000 })

/**
 * 性能指标（AC-23）。**需要登录**（`running_tasks` 里是取消任务的凭据）。
 */
export const systemMetrics = (days = 1) =>
  request<MetricsData>('get', `/system/metrics?days=${days}`, undefined, { timeout: 20_000 })


// ══════════════════════════════════════════════════════════════════
// 4.3‴ 地面材质替换（AC-10）
// ══════════════════════════════════════════════════════════════════

/** 当前替换 + 可选材料清单。**一次拿全**，前端不需要再调材料接口。 */
export const floorMaterials = (layoutId: string) =>
  request<FloorMaterialsData>(
    'get', `/layout/${encodeURIComponent(layoutId)}/floor-materials`, undefined,
    { timeout: 15_000 },
  )

/** 换一间房的地面。`materialId` 传空串 = **还原**成默认配色。 */
export const setFloorMaterial = (
  layoutId: string, body: { room_index: number; material_id: string },
) =>
  request<FloorMaterialsData>(
    'post', `/layout/${encodeURIComponent(layoutId)}/floor-material`, body,
    { timeout: 15_000 },
  )
