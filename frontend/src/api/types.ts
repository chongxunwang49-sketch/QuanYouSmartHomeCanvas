/**
 * 后端契约的 TypeScript 映射。
 *
 * ⚠️ **这里不引入 codegen 是有意的。** 后端用 FastAPI，`/openapi.json` 能自动
 * 生成类型，但生成出来的是 `Record<string, any>` 的层层嵌套 —— 因为后端的
 * `plans` / `layout` 在 pydantic 里是 `dict[str, Any]`（Agent 产物结构太活，
 * 强行建模会把 agent 层和接口层耦死）。既然生成物一样是 any，不如手写：
 * 至少有注释说明每个字段从哪来、什么时候可能是 null。
 *
 * 唯一的同步机制是 `scripts/check_contract.py`（对比 openapi 的路径与方法）。
 */

// ══════════════════════════════════════════════════════════════════
// 统一响应外壳
// ══════════════════════════════════════════════════════════════════

/**
 * 所有接口的外壳。
 *
 * ⚠️ **业务失败走 HTTP 200 + code != 0**，不用 4xx。
 * 这是需求文档 4.4 的硬性规定：用 4xx 表达"户型数据不足"这类业务结果，
 * 会被 axios 拦截器当成网络错误弹通用报错，而用户该看到的是
 * 「缺少房间面积，建议重新上传更清晰的户型图」这种可操作提示。
 */
export interface ApiEnvelope<T = unknown> {
  code: number
  msg: string
  data: T | null
}

/** 业务错误码，与后端 `schemas.py` 的 `ErrorCode` 一一对应。 */
export const ErrorCode = {
  OK: 0,
  /** 参数不合法 */
  BAD_REQUEST: 4001,
  /** 数据不支撑该操作 —— 对应业务连续性守卫（AC-33） */
  NOT_ALLOWED: 4002,
  /** 任务不存在或已过期 */
  TASK_NOT_FOUND: 4004,
  /** 执行失败 */
  EXEC_FAILED: 5001,
  /** 依赖不可用（Redis / 模型 / 知识库） */
  DEP_UNAVAILABLE: 5002,
} as const

/** 业务错误。`client.ts` 在 code != 0 时抛它，组件 catch 后展示 `message`。 */
export class BizError extends Error {
  constructor(
    readonly code: number,
    message: string,
    /** 4002 会带 `{missing, suggestion}`，用来把"为什么不能做"说清楚 */
    readonly data: unknown = null,
  ) {
    super(message)
    this.name = 'BizError'
  }
}

// ══════════════════════════════════════════════════════════════════
// 任务与进度
// ══════════════════════════════════════════════════════════════════

export type TaskKind = 'parse' | 'generate' | 'review'
export type TaskStatus = 'pending' | 'processing' | 'completed' | 'failed'

/**
 * 语义化阶段。**用 `phase_text` 直接展示，不要自己建映射表** ——
 * 后端 `redis_client.PHASE_TEXT` 是唯一来源，前端再维护一份必然漂移。
 */
export type TaskPhase =
  | 'queued'
  | 'prechecking'
  | 'analyzing'
  | 'detecting_rooms'
  | 'extracting_dimensions'
  | 'diagnosing'
  | 'planning'
  /** 报价单审查专用：与 planning 同节点不同语境，见后端 tasks.py 的 _phase_for */
  | 'reviewing'
  | 'finalizing'
  | 'done'
  | 'degraded'

export interface TaskStatusData<R = unknown> {
  task_id: string
  trace_id: string
  kind: TaskKind | ''
  status: TaskStatus
  phase: TaskPhase | string
  /** 中文阶段文案，来自后端。直接展示。 */
  phase_text: string
  progress: number
  /** 降级完成也是完成，但必须让用户看见（AC-17） */
  degraded: boolean
  error: string | null
  result: R | null
}

/** 异步接口创建任务的返回。 */
export interface TaskCreated {
  task_id: string
  trace_id: string
  status: 'processing'
  poll: string
  /** 后端给的估算值 —— **只用于文案，不参与轮询节奏**（见 useTaskPolling） */
  estimated_seconds?: number
  plan_count?: number
}

// ══════════════════════════════════════════════════════════════════
// 户型与诊断
// ══════════════════════════════════════════════════════════════════

export type RoomType =
  | 'living_room' | 'bedroom' | 'kitchen' | 'bathroom'
  | 'dining_room' | 'study' | 'balcony' | 'storage' | 'other'

export interface Room {
  name: string
  type: RoomType
  area: number
  bbox: number[]
  orientation: string
  notes?: string
}

export interface Wall { type: string; coords: number[][]; note?: string }
export interface Door { position: number[]; width: number; swing: string }
export interface Window { position: number[]; width: number; orientation: string }
export interface Dimension { label: string; value: number; unit: string }

/**
 * 户型解析结果。
 *
 * ⚠️ `mode === 'degraded_basic'` 时，**只有 `rooms[].name` 有效**，
 * 其余结构字段全空。需求文档 2.2.4 规定此时前端必须：
 *   ① 加半透明水印 + 「需人工复核」标签
 *   ② 禁用所有依赖墙体/面积的后续操作
 *   ③ 显著展示 safety_notice
 * 这三条不是建议，是验收项（见 views/FloorPlanParse.vue）。
 */
export interface Layout {
  layout_id?: string
  mode?: 'full' | 'degraded_basic'
  rooms: Room[]
  walls: Wall[]
  doors: Door[]
  windows: Window[]
  dimensions: Dimension[]
  total_area: number
  entrance_orientation: string
  has_north_arrow: boolean
  confidence: number
  /** degraded_basic 下的安全提示，必须显著展示 */
  safety_notice?: string
  /** 数据缺口说明。**如实展示，不要藏** —— 它决定了用户能不能信任这份解析 */
  data_gaps?: string[]
  capabilities?: Capabilities
}

/** 五维诊断。"数据不足"是合法结论，不要渲染成 0 分。 */
export interface DiagnosisItem {
  score: number
  insufficient_data: boolean
  issues: string[]
  suggestions: string[]
}

export interface Diagnosis {
  lighting: DiagnosisItem
  ventilation: DiagnosisItem
  circulation: DiagnosisItem
  space_utilization: DiagnosisItem
  green_score: DiagnosisItem
  overall_score: number
  summary: string
  highlights: string[]
  load_bearing_warning: string[]
}

/**
 * 能力报告。**前端不需要自己推断"现在能做什么"** ——
 * 后端直接告诉你（需求文档 2.2.3）。
 */
export interface Capabilities {
  can_generate_plan?: boolean
  can_estimate_budget?: boolean
  can_select_materials?: boolean
  can_review?: boolean
  missing?: string[]
  suggestion?: string
  [key: string]: unknown
}

// ══════════════════════════════════════════════════════════════════
// 方案产物
// ══════════════════════════════════════════════════════════════════

export interface ZonePlan {
  room_name: string
  function: string
  rationale: string
  furniture: string[]
}

export interface StoragePlan { location: string; kind: string; note: string }
export interface CirculationFix { problem: string; solution: string; target_rooms: string[] }

/** A-03 空间规划 */
export interface SpacePlan {
  summary: string
  zones: ZonePlan[]
  storage_plans: StoragePlan[]
  circulation_fixes: CirculationFix[]
  key_moves: string[]
  data_gaps: string[]
  confidence: number
}

export interface BudgetLine {
  key: string
  label: string
  unit: string
  basis: string
  quantity: number
  unit_price_min: number
  unit_price_max: number
  amount_min: number
  amount_max: number
}

/**
 * A-04 预算。
 *
 * ⚠️ **金额全部由后端规则引擎算出，LLM 不参与**（ADR-07）。
 * `narrative` 里没有任何数字字段 —— 这是刻意的，模型编不出金额。
 */
export interface Budget {
  grade: string
  area: number
  lines: BudgetLine[]
  subtotal_min: number
  subtotal_max: number
  total_min: number
  total_max: number
  price_per_sqm_min: number
  price_per_sqm_max: number
  narrative: {
    summary: string
    grade_rationale: string
    cost_drivers: string[]
    negotiation_tips: string[]
    saving_tips: string[]
    warnings: string[]
    confidence: number
  }
  narrative_degraded?: boolean
}

export interface MaterialItem {
  category: string
  category_label?: string
  product_id: string
  name: string
  brand: string
  spec?: string
  price_range: number[]
  unit?: string
  is_quanyou: boolean
  eco_level?: string
  reason?: string
}

/** A-05 材料选型。价格由代码回填，模型只负责指认候选。 */
export interface Materials {
  grade: string
  style: string
  plan_id: string
  items: MaterialItem[]
  product_ids: string[]
  quanyou_coverage: number
  /** 是否达到 AC-18 要求的 60% 全友覆盖 */
  quanyou_met: boolean
  auto_substitutions: { from_product_id: string; to_product_id: string; reason: string }[]
}

/** A-06 风险项。`source_ids` 会由后端映射成真实引用，编造的引用记在 `invented_citations`。 */
export interface RiskFinding {
  risk_type: string
  severity: 'high' | 'medium' | 'low' | string
  title: string
  where: string
  detail: string
  suggestion: string
  source_ids: string[]
  citations?: { title: string; source: string }[]
}

export interface RiskReview {
  summary: string
  findings: RiskFinding[]
  overall_risk: string
  negotiation_points: string[]
  data_gaps: string[]
  confidence: number
  invented_citations?: string[]
  mode?: string
}

/** 一套完整方案。三个分支产物 + 一次汇聚审查。 */
export interface Plan {
  plan_id: string
  plan_index: number
  style: string
  budget_grade: string
  space_plan: SpacePlan | null
  budget: Budget | null
  materials: Materials | null
  risks: RiskReview | null
  /** 哪些产物没产出。**如实展示缺失，不要假装完整** */
  missing_artifacts: string[]
}

export interface ComparisonField { key: string; label: string }

/**
 * 横向对比表。
 *
 * ⚠️ `recommendation` 恒为 null —— 系统**不替用户排序方案**。
 * 哪套合适取决于业主优先级，`recommendation_note` 会说清这一点。
 * 前端不要自己造一个"推荐"标签出来。
 */
export interface Comparison {
  available: boolean
  plan_count: number
  requested_count: number
  fields: ComparisonField[]
  rows: Record<string, unknown>[]
  notes: string[]
  unavailable_reason?: string
  recommendation: null
  recommendation_note?: string
}

// ══════════════════════════════════════════════════════════════════
// 接口返回体（`_build_result` 裁剪后的形状）
// ══════════════════════════════════════════════════════════════════

export interface TraceEntry {
  agent?: string
  step?: string
  status?: string
  elapsed_ms?: number
  message?: string
  [key: string]: unknown
}

/**
 * 图片质量预检结果（AC-27）。
 *
 * 不合格时任务以 `status="failed"` 结束，`result` 里仍然带着它 ——
 * 所以**失败态也要渲染这一块**，否则用户只看到一句错误、看不到具体哪里不合格。
 */
export interface Precheck {
  ok: boolean
  width: number
  height: number
  blur_score: number | null
  rejections: string[]
  warnings: string[]
  advice: string
}

export interface ParseResult {
  layout_id: string | null
  layout: Layout | null
  precheck?: Precheck | null
  diagnosis: Diagnosis | null
  capabilities: Capabilities | null
  degraded: boolean
  degrade_reasons: string[]
  errors: { agent?: string; type?: string; message?: string }[]
  trace: TraceEntry[]
}

export interface GenerateResult {
  layout_id: string | null
  diagnosis: Diagnosis | null
  plans: Plan[]
  comparison: Comparison | null
  degraded: boolean
  degrade_reasons: string[]
  errors: { agent?: string; type?: string; message?: string }[]
  trace: TraceEntry[]
}

export interface ReviewResult {
  review: RiskReview | null
  degraded: boolean
  trace: TraceEntry[]
}

// ══════════════════════════════════════════════════════════════════
// 材料价格查询（4.5，同步接口）
// ══════════════════════════════════════════════════════════════════

export interface MaterialPriceRow {
  id: string
  name: string
  brand: string
  category: string
  category_label: string
  spec: string
  price_range: [number, number]
  unit: string
  eco_level: string
  search_url: string
}

export interface MaterialPriceData {
  category: string
  category_name: string
  quanyou_recommended: MaterialPriceRow[]
  items: MaterialPriceRow[]
  total: number
  catalog_version: string
  /**
   * ⚠️ **必须展示。** 需求文档 5.3 与 R-09 要求演示数据显著标注，
   * 而这是最容易被前端"顺手美化"掉的地方。
   */
  disclaimer: string
}

// ══════════════════════════════════════════════════════════════════
// 矢量图与热区（4.3′，AC-07 / AC-09 / AC-21）
// ══════════════════════════════════════════════════════════════════

/**
 * 热区精确度。
 *
 * - `exact`      坐标由几何**量出来**的（矢量图路径）。边界就是房间边界。
 * - `room_level` 只有房间级近似（AI 生成图路径）。
 * - `none`       不提供热区。
 *
 * ⚠️ `room_level` **必须用视觉样式区分**，悬停时要写"本区域整体参考价"
 * 而不是假装精确到某一件家具（需求文档 2.2.6 的诚实性原则）。
 * **用户可以接受近似，不能接受被骗。**
 */
export type HotspotPrecision = 'exact' | 'room_level' | 'none'

/** 热区坐标来源，便于溯源。 */
export type HotspotSource = 'vector_layer' | 'layout_bbox_mapping'

/** 热区里挂的一件商品。 */
export interface HotspotItem {
  id: string
  brand: string
  name: string
  /** 区间下限价，用于卡片主展示 */
  price: number
  price_range: [number, number]
  spec: string
  eco_level: string
  is_quanyou: boolean
  url: string
}

/** 一块热区。 */
export interface PlanHotspot {
  /** 与 SVG 里 `data-hotspot="N"` 一一对应 */
  index: number
  label: string
  /** floor / paint / door —— 与材料目录的品类 key 对齐 */
  category: string
  /** 画布像素 `[left, top, right, bottom]` */
  bbox: [number, number, number, number]
  /** 画布像素顶点。墙面热区是多环，用 `rings` */
  polygon: [number, number][]
  /** 子路径。1 个环 = 实心区域；2 个环 = 中间挖空（墙面） */
  rings: [number, number][][]
  precision: HotspotPrecision
  source: HotspotSource
  /** 计价数量（㎡ 或 樘）。null 表示不按量算 */
  quantity: number | null
  unit: string
  /** 这块热区的几何口径说明（层高是假设值、未扣门窗等） */
  note: string
  price_range: [number, number] | null
  /** 数量 × 单价 = 本区域参考总价 */
  estimate_range: [number, number] | null
  items: HotspotItem[]
  search_url: string
  notes: string[]
}

/** 画布变换参数。与 `projection.Projection.as_dict()` 一一对应。 */
export interface PlanTransform {
  scale: number
  offset_x: number
  offset_y: number
  draw_width_m: number
  draw_depth_m: number
  width_px: number
  height_px: number
}

/** `GET /layout/{id}/hotspots` 的返回体。 */
export interface PlanRenderData {
  layout_id: string
  image: { url: string; width: number; height: number }
  transform: PlanTransform
  /** 画不准的地方。**不许静默**，界面要么展示要么记日志 */
  warnings: string[]
  hotspots: PlanHotspot[]
  count: number
  by_precision: Record<string, number>
  source: string
  /** 演示价格声明，**必须展示** */
  disclaimer: string
}

// ══════════════════════════════════════════════════════════════════
// 健康检查（4.6）
// ══════════════════════════════════════════════════════════════════

export interface HealthCheck { ok: boolean; detail: string }

export interface HealthData {
  status: 'ok' | 'degraded'
  checks: Record<string, HealthCheck>
  running_tasks: string[]
  version: string
}

// ══════════════════════════════════════════════════════════════════
// 请求体
// ══════════════════════════════════════════════════════════════════

export interface ParseRequest {
  image: string
  image_media_type?: string
  detail_level?: 'basic' | 'full'
  /** 隐私模式：True 时图像绝不出本机 */
  prefer_local?: boolean
}

export interface GenerateRequest {
  layout_id: string
  styles?: string[]
  budget_grades?: string[]
  quanyou_priority?: boolean
  requirements?: Record<string, unknown>
}

export interface ReviewRequest {
  quote_text: string
  requirements?: Record<string, unknown>
}

// ══════════════════════════════════════════════════════════════════
// 展示用词表
// ══════════════════════════════════════════════════════════════════

/**
 * 风格与预算档的中文名。
 *
 * 这份表**只影响展示**，不参与任何逻辑判断 —— 后端的配对是按位置来的，
 * 前端不重算。缺项时回落到原始 key，而不是猜一个中文名。
 */
export const STYLE_LABEL: Record<string, string> = {
  modern: '现代简约',
  nordic: '北欧自然',
  chinese: '新中式',
  luxury: '意式轻奢',
  cream: '奶油风',
  japanese: '日式侘寂',
}

export const GRADE_LABEL: Record<string, string> = {
  economy: '极简经济型',
  medium: '舒适中档型',
  high: '高端尊享型',
}

export const GRADE_TONE: Record<string, string> = {
  economy: '经济 · 严控成本',
  medium: '品质 · 平衡优选',
  high: '高端 · 尊享定制',
}

export const SEVERITY_LABEL: Record<string, string> = {
  high: '高风险',
  medium: '中风险',
  low: '低风险',
}

export const label = (dict: Record<string, string>, key: string): string => dict[key] ?? key

// ══════════════════════════════════════════════════════════════════
// 3D 漫游（4.3″，第一人称行走）
// ══════════════════════════════════════════════════════════════════

/**
 * 3D 渲染用的墙条。
 *
 * ⚠️ `a`/`b` 与 `render_a`/`render_b` 是**两套端点**，不要混用：
 * - 碰撞用 `a`/`b`（精确中心线）
 * - 建 3D 几何用 `render_a`/`render_b`（墙角外延、门洞边缘不外延）
 *
 * 用错的表现：用 a/b 建墙 → 每个墙角一道竖缝；
 * 用 render_* 做碰撞 → 玩家被挡在离墙 10cm 的地方，贴不到墙。
 */
export interface WalkCollisionSeg {
  a: [number, number]
  b: [number, number]
  render_a: [number, number]
  render_b: [number, number]
  wall: number
}

export interface WalkRoom {
  index: number
  name: string
  kind: string
  center: [number, number]
  free_rect: [number, number, number, number]
  size_m: [number, number]
  area_m2: number
  standable: boolean
  reachable: boolean
}

export interface WalkDoor {
  index: number
  position: [number, number]
  width_m: number
  from_room: number
  to_room: number
}

export interface WalkableData {
  ok: boolean
  /** `walk` = 贴地行走（有碰撞）；`fly` = 自由视角（可穿墙） */
  mode: 'walk' | 'fly'
  spawn: { x: number; y: number; room: number; yaw_deg: number }
  player_radius_m: number
  eye_height_m: number
  ceiling_height_m: number
  collision: WalkCollisionSeg[]
  rooms: WalkRoom[]
  doors: WalkDoor[]
  graph: [number, number][]
  /** 不能走的原因。`ok=false` 时必定非空 */
  issues: string[]
  notes: string[]
}

/** `GET /layout/{id}/walkable` 的返回体。 */
export interface WalkableResponse {
  layout_id: string
  /** 米制场景。3D 建几何用这份，不用再算一遍坐标 */
  scene: SceneData
  walkable: WalkableData
  plan_transform: PlanTransform
}

/** 米制场景（与后端 `Scene.to_dict()` 对齐）。 */
export interface SceneData {
  units: 'm'
  px_per_m: number
  width_m: number
  depth_m: number
  ceiling_height_m: number
  assumptions: string[]
  walls: { kind: string; thickness_m: number; is_loop: boolean; length_m: number; points: [number, number][] }[]
  openings: { kind: string; center: [number, number]; width_m: number; wall_index: number; width_is_assumed: boolean }[]
  rooms: { name: string; kind: string; area_m2: number; polygon: [number, number][]; polygon_is_bbox: boolean }[]
  quality: { walls_closed: boolean; wall_count: number; can_build_walls: boolean; issues: string[] }
  confidence: number
}
