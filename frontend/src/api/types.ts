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
  /**
   * **未登录 / 登录已失效。**
   *
   * ⚠️ 后端刻意**不用 HTTP 401**：axios 拦截器按状态码分流，4xx 会被归一成
   * "服务错误"弹通用报错，而"请重新登录"是**可操作的提示**。理由见
   * `backend/app/api/deps.py` 的模块说明。前端拿到它的唯一动作是跳 `/login`。
   */
  UNAUTHORIZED: 4003,
  /** 任务不存在或已过期 */
  TASK_NOT_FOUND: 4004,
  /** 需要开通会员（AC-01 门控，`api/deps.py` 的 `require_paid`） */
  NEED_PAID: 4005,
  /** 当日额度用完（AC-13，`api/deps.py` 的 `consume_quota`） */
  QUOTA_EXCEEDED: 4006,
  /**
   * 账号已被管理员停用。
   *
   * ⚠️ **只在口令正确时才会返回它**（后端 `auth.is_disabled_account`）——
   * 口令都输错的人拿到的是合并过的 4001，否则这里就成了一个用户名枚举口子。
   * 与普通 4001 分开的意义：被封的人不会以为是自己记错了口令。
   */
  ACCOUNT_DISABLED: 4007,
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
// 认证与权限（AC-01）
// ══════════════════════════════════════════════════════════════════

/**
 * 角色 × 会员 —— 两个**正交**的维度，不要混成一条枚举。
 *
 *   role        admin / designer / user     —— 决定**能看什么**
 *   membership  free / paid                 —— 决定**用多少**
 *
 * 混在一起的后果：加"会员管理员"这种组合时要改枚举，而且判定会写成
 * `role === 'vip'` 这种把两个维度压扁的代码。后端 `core/auth.py` 的
 * `User.is_unlimited` / `can_use_paid_features` 就是按正交写的，
 * 前端必须同构，否则会出现"界面显示不限量、后端却把他限流了"。
 */
export type UserRole = 'admin' | 'designer' | 'user'
export type Membership = 'free' | 'paid'

/**
 * 当前用户。
 *
 * ⚠️ **不含任何口令材料**（后端 `User` 就没有 `password_hash` / `salt`，
 * 有测试钉着）。也不做本地持久化 —— 见 `stores/auth.ts` 的说明。
 */
export interface AuthUser {
  id: number
  username: string
  display_name: string
  title: string
  /** 头像上的文字（1–2 个字），后端给，前端不要自己截 */
  avatar_text: string
  role: UserRole
  role_label: string
  membership: Membership
  /** 不受额度限制：admin / designer */
  is_unlimited: boolean
  /** 付费功能可用：`is_unlimited || membership === 'paid'` */
  can_use_paid_features: boolean
  /** 是否启用。`false` = 已停用，**该账号登不进来**（管理页用它显示封禁态） */
  is_active: boolean
}

/**
 * 一条登录设备记录。
 *
 * ⚠️ **这是"从登录请求推断出的设备"，不是密码学意义的设备绑定。**
 * 依据只有 User-Agent 与来源 IP，两者都可伪造 —— 它只回答
 * "我自己看看有哪些地方登录过"，**不构成任何访问控制**。
 * 界面上也必须这么写，不要把它说成"设备锁"。
 */
export interface DeviceRecord {
  /** 同一台设备（UA + IP 相同）的稳定标识，后端算的 */
  key: string
  /** 形如 `Chrome · Windows`。**启发式推断，认不出就是「未知设备」** */
  name: string
  ip: string
  first_seen: string
  last_seen: string
  /** 这台设备登录过几次 */
  count: number
}

export interface DevicesData {
  devices: DeviceRecord[]
  /** 后端给的那句免责说明。**要原样展示** —— 不要自己重写一遍 */
  note: string
}

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginData {
  access_token: string
  token_type: string
  /** 秒。后端 12 小时，没有 refresh token（有意偏离需求 2.3.2，理由在 core/auth.py） */
  expires_in: number
  user: AuthUser
}

export interface MeData {
  user: AuthUser
}

/**
 * 一个任务类型的今日额度。
 *
 * `limit === null` 表示不限量（管理员 / 设计师），此时 `used` **仍是真的** ——
 * 次数对谁都累计，只是对不限量的人不设上限。所以界面要显示
 * 「已用 7 次 · 不限量」，而不是显示「7 / ∞」。
 */
export interface QuotaBucket {
  allowed: boolean
  used: number
  limit: number | null
  remaining: number | null
  role: string
  unlimited: boolean
  /** 何时重置，形如 `2026-09-23 24:00`。后端给，前端不要自己算 */
  reset_at: string
  /**
   * ⚠️ **Redis 不可用时为 true，此时额度是"放行且不计数"。**
   *
   * 界面必须如实说「额度统计暂不可用」，**不能拿 `remaining` 当真** ——
   * 那个数字在降级时是拿默认值填的。这正是本项目「不产出看起来合理的错误」
   * 那条原则在界面上的一次具体应用。
   */
  degraded: boolean
}

export interface QuotaData {
  quota: Record<string, QuotaBucket>
  user_id: number
  role: UserRole
  unlimited: boolean
}

export interface UserListData {
  users: AuthUser[]
  /** 允许的取值由后端给（`core/auth.py` 的 ROLES / MEMBERSHIPS），前端不另写一份 */
  roles: UserRole[]
  memberships: Membership[]
}

/**
 * 4005 / 4006 的 `data` 形状。**拒绝时必须说清缺什么、怎么办** ——
 * 所以这里每个字段都是给用户看的，不是给开发者调试的。
 */
export interface GateInfo {
  feature?: string
  membership?: Membership
  required_membership?: Membership
  /** 去哪开通。后端 `deps.UPGRADE_HINT` 给，前端不要自己编一句 */
  upgrade_hint?: string
  task_type?: string
  limit?: number
  used?: number
  reset_at?: string
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
  | 'diagnosing'
  | 'planning'
  /** 方案链内的避坑审查：与 planning 同节点不同语境，见后端 tasks.py 的 _phase_for */
  | 'checking_risks'
  /** 报价单审查专用：与 checking_risks 同节点不同语境 */
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
  /** 进入当前阶段时的百分比，且已按实际进度在阶段内插值（单调不回退） */
  progress: number
  /**
   * 已用秒数，**以服务端为准**。
   *
   * ⚠️ 前端不要自己从"点了按钮那一刻"算：刷新页面、从别的页面切回来，
   * 本地计时都会归零，而任务其实已经跑了一分多钟 ——
   * 用户会看到"已等待 0 秒"配一个 60% 的进度条。
   * 为 null 表示服务端也不知道（拿不到开始时间），此时前端才回退到本地计时。
   */
  elapsed_seconds: number | null
  /**
   * 预计剩余秒数。**`null` = 估不出来，不是 0** ——
   * 模型已经被实际耗时追上的时候（`overrun`）后端就不给数字了，
   * 因为"预计还剩 3 秒"再挂 40 秒比不给还糟。
   */
  eta_seconds: number | null
  /** 已超出模型预期。界面此时改口说"比预期久"，而不是继续报一个失效的倒计时。 */
  overrun: boolean
  /** 降级完成也是完成，但必须让用户看见（AC-17） */
  degraded: boolean
  /**
   * 是不是被用户自己中断的（AC-31）。
   *
   * ⚠️ `status` 在中断时仍然是 `failed` —— 它是**控制流通道**
   * （轮询循环靠"completed / failed 就停"来判断该不该继续），
   * 而这个是**措辞通道**：界面据此说"已中断"而不是"失败"。
   * 用户自己按的按钮，不该被报成一个错误。
   */
  cancelled?: boolean
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
  /**
   * 依据来源清单，**由后端填**（平面图解析 / 屋主提供的户型详情）。
   *
   * ⚠️ 加它的理由：界面上一句"综合 8.2 分"必须说得清是**拿什么算的**。
   * 屋主补了户型详情之后评分会变高 —— 不标出来源，那个变化就成了黑箱。
   */
  evidence_sources?: string[]
  /** 数据不足或推断成分较大的地方 */
  data_gaps?: string[]
  /** 置信度（0–1）。缺数据时后端会下调 */
  confidence?: number
  /** 这次诊断是基于什么算出来的（计数，便于排查） */
  model_based_on?: Record<string, boolean | number>
}

/**
 * 屋主补充的「户型详情」——**文字资料，不是图片**。
 *
 * 它补的正是二维平面图读不出来的东西：层高、朝向、采光面、通风路径、
 * 收纳、设备。户型诊断一直"数据不足、置信度低"就是因为缺这些。
 */
export interface HouseDetail {
  text: string
  title: string
  source: string
  updated_at: string
}

/** 后端按面积推荐的演示详情（`GET /layout/{id}/house-detail/sample`）。 */
export interface HouseDetailSample {
  title: string
  text: string
  /** 为什么推这份 —— 直接显示给用户，不要让他猜 */
  matched_by: string
  note: string
}

/** 单个操作的能力判定（`CapabilityReport.to_dict()` 里 `operations` 的每一项）。 */
export interface Capability {
  allowed: boolean
  reason: string
  /** 缺什么。**拒绝时必须说清**（AC-33 的立场），前端直接展示 */
  missing: string[]
  /** 怎么办。同样直接展示，不要改写成"系统繁忙" */
  suggestion: string
}

/**
 * 能力报告。**前端不需要自己推断"现在能做什么"** ——
 * 后端直接告诉你（需求文档 2.2.3）。
 *
 * ⚠️ **形状在 2026-09-24 修正过：它是嵌套的，不是扁平的。**
 *
 * 这里原来声明的是 `can_generate_plan` / `can_estimate_budget` /
 * `can_select_materials` / `can_review` / `missing` / `suggestion` 一组
 * **扁平字段**，而后端 `CapabilityReport.to_dict()` 返回的是
 * `{mode, reason, operations: {generate_plan: {allowed, reason, missing, suggestion}}}`
 * —— **那组扁平字段一个都不存在。**
 *
 * 更糟的是旧类型带了 `[key: string]: unknown` 索引签名，于是
 * `c.can_generate_plan` 取到 `undefined` 也**不报类型错**。
 * 实测后果（读代码确认的因果链，不是推测）：
 *   · `canGenerate` 里那句 `typeof c.can_generate_plan === 'boolean'`
 *     永远为假 → 一直走"按 rooms + total_area 自己判断"的兜底分支；
 *   · `blockedReason` 读 `c.suggestion` 永远是 undefined →
 *     后端给的那句「缺少墙体信息」从来没能显示给用户。
 * 于是"缺墙体"的户型（**有房间、有面积，只是没墙**）在前端看来是可生成的，
 * 按钮亮着，点下去后端回 4002 —— 界面承诺了一个做不到的操作。
 *
 * 现在按后端真实形状声明，并去掉索引签名：**让类型系统真的能拦住这类漂移。**
 * `tests/test_frontend_contract.py` 里还有一条逐键比对的用例兜底。
 */
/**
 * 能力报告。**嵌套结构**，不是扁平字段。
 *
 * ⚠️ 这里踩过一次真实的、静默的漂移（2026-09-24）：旧的类型声明的是
 * `can_generate_plan` / `can_estimate_budget` / `missing` / `suggestion`
 * 一组字段，而后端 `CapabilityReport.to_dict()` 返回的是
 * `{mode, reason, operations: {<op>: {allowed, missing, reason, suggestion}}}` ——
 * 那组扁平字段**一个都不存在**。更糟的是旧类型带 `[key: string]: unknown`，
 * 于是取到 `undefined` 也不报类型错，界面一直在猜：
 * 「有房间有面积就能生成」的兜底分支恒为真，用户点下去后端回 4002。
 * 所以这里**不许再加索引签名**。
 */
export interface Capabilities {
  mode: string
  reason: string
  operations: Record<string, CapabilityOperation>
}

/** 单个操作的可用性。`missing` 缺什么、`suggestion` 怎么修 —— 都要能显示给用户 */
export interface CapabilityOperation {
  allowed: boolean
  missing: string[]
  reason: string
  suggestion: string
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
  /**
   * ⚠️ 后端给的是 `from` / `to` **两个商品对象**（`catalog.to_dict()` 的形状），
   *    而这里原来声明成 `from_product_id` / `to_product_id` —— 字段名对不上，
   *    界面渲染出来是**两个空白**（`PlanDrawer` 那两处 {{ }} 一直是空的）。
   */
  auto_substitutions: {
    from: { id: string; name: string; brand?: string }
    to: { id: string; name: string; brand?: string }
    reason: string
  }[]
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
/**
 * 一项室内环境风险（AC-20）。
 *
 * ⚠️ **它没有、也不该有浓度字段。** 判据是材料环保等级 + 户型通风条件，
 * 推到的是**风险档**；甲醛浓度只能现场采样检测。所以 `note`/`disclaimer`
 * 里的每一句都要原样展示 —— 缺了它们，`risk` 会被读成检测结论。
 */
export interface HazardItem {
  risk: 'low' | 'medium' | 'high' | 'unknown'
  /** 评不了就是评不了，**不是**"没问题"。显示时要与 low 明确区分 */
  insufficient_data: boolean
  /** 每条结论的依据。**要展示** —— 用户据此才知道怎么改善 */
  basis: string[]
  note: string
}

export interface PlanEnvironment {
  formaldehyde: HazardItem
  tvoc: HazardItem
  ventilation: { poor: boolean; basis: string }
  computed_by: string
  /** 强制展示的免责声明 */
  disclaimer: string
}

export interface Plan {
  plan_id: string
  plan_index: number
  style: string
  budget_grade: string
  space_plan: SpacePlan | null
  budget: Budget | null
  materials: Materials | null
  risks: RiskReview | null
  /**
   * 室内环境风险（AC-20）。**由规则引擎算，不由模型写** ——
   * 见后端 `services/environment.py`。缺产物时为 undefined。
   */
  environment?: PlanEnvironment | null
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
  /**
   * **结果是否只交付了一部分。**
   *
   * 方案在 A-06 避坑审查**开始前**就已经产出并交付了（实测：整链 123 秒，
   * 方案在第 38 秒就齐了），此时 `partial=true`，每套方案的
   * `missing_artifacts` 里都有 `risks`。审查跑完后由完整结果覆盖，
   * 届时 `partial=false`。
   *
   * ⚠️ 所以**不能**拿"有没有 plans"当"任务完成了"的判据 ——
   * 判据只有 `status === 'completed'`。
   */
  partial: boolean
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
  /**
   * 属于哪间房。**空值是 null**（门热区没有房间）。
   *
   * ⚠️ 漏了它的话 AC-10「点一块地面 → 选定房间」会静默失效：
   * `undefined != null` 为 false，判断会走进另一条分支。
   * 命名必须 snake_case —— 后端返回的就是 `room_index`。
   */
  room_index: number | null
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

export interface HealthCheck {
  ok: boolean
  detail: string
  /** 中文名（`redis` → 「进度缓存」）。**界面渲染这个，不要渲染键名** —— 键名是内部组件名。 */
  label?: string
}

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
  /**
   * 同等条件下是否优先推荐全友自有产品（软偏好）。
   *
   * ⚠️ **AC-18 的 60% 覆盖率保底不受这个开关影响。** 它是平台级要求，
   * 不是可选项 —— 能被用户关掉的验收指标是测不了的。
   * 关掉之后覆盖率会从接近 100% 回落到贴着底线，替代记录随之增多，
   * 那才是这个开关可观测的效果。
   */
  quanyou_priority?: boolean
  requirements?: Record<string, unknown>
  /** AC-19：排除的品类 key。与后端 `GET /material/options` 的清单一致 */
  excluded_categories?: string[]
  /** AC-19：排除的品牌。**不能排除全友** —— 与 AC-18 冲突，后端 4001 拒绝 */
  excluded_brands?: string[]
  /** AC-19：同等条件下优先的品牌（加分高于平台的全友优先，但低于 AC-18 底线） */
  preferred_brands?: string[]
}

/**
 * AC-19 材料偏好的可选项。
 *
 * 清单来自后端目录，前端**不另存一份** —— 见 `materialOptions()` 的说明。
 */
export interface MaterialOptionsData {
  categories: { key: string; label: string; unit: string }[]
  brands: string[]
  quanyou_brand: string
  /** 判据常量与后端同源，避免前端各写一遍 0.6 / 1.5 / 2.0 */
  constants: {
    min_quanyou_coverage: number
    preferred_brand_bonus: number
    quanyou_preference_bonus: number
  }
  catalog_version: string
}

// ══════════════════════════════════════════════════════════════════
// 3D 家具摆放（"3D 带装修"）
// ══════════════════════════════════════════════════════════════════

/**
 * 一件摆好的家具。**坐标是场景坐标系（米），与 `/walkable` 同一套** ——
 * 所以 `three/coords.ts` 的 `planToEngine` 直接能用，不需要第二套换算。
 */
export interface FurniturePlacement {
  spec_id: string
  label: string
  room_index: number
  room_name: string
  /** 中心点（米，图纸平面坐标） */
  x: number
  y: number
  /** 世界轴对齐的宽与深（米）。旋转已在后端折进这两个值里 */
  w: number
  d: number
  /** 图纸平面的逆时针角度，只有 0/90/180/270 */
  rot_deg: number
  mount: 'floor' | 'wall' | 'ceiling' | string
  height_m: number
  /** 在风格配色里的角色：wood / fabric / metal / stone / green / white */
  color_role: string
  /** 这一件的实际体色（`#RRGGBB`）。同族同色 —— 见后端 derive_family_palette */
  color: string
  y_offset_m: number
  /** True = 不参与行走碰撞（地毯、地台、吊灯） */
  no_collide: boolean
  /** 挂墙件的附件（镜面、吊柜）。与主体一起画 */
  extras: { w?: number; d?: number; h?: number; dx?: number; dy?: number; dz?: number }[]
  /** 为什么摆在这里。**要能说出口** —— 悬停与排错都用它 */
  basis: string[]
}

export interface FurnitureRoom {
  index: number
  name: string
  kind: string | null
  area_m2: number | null
  free_rect: [number, number, number, number] | null
  placements: FurniturePlacement[]
}

export interface FurnitureRejection {
  room: string
  spec_id: string
  label: string
  /** 为什么没摆下。**必须显示或至少保留** —— 不许静默丢弃 */
  reason: string
  /**
   * 拒绝的**性质**，由后端给，前端不按文案猜：
   *
   *   `"space"`  空间不够 —— 等比放大房间可以解决
   *   `"policy"` 规则不摆（同类只摆一件、本间房占地达上限）——
   *              放大一万倍也解决不了
   *
   * ⚠️ 分这两类不是措辞讲究：混在一起显示，用户会以为
   * "等比放大没生效"（它确实解决不了 policy 那一半）。
   */
  kind?: 'space' | 'policy'
  skipped_by_capacity?: string[]
}

export interface FurnitureData {
  layout_id?: string
  plan_id?: string | null
  style: string
  /** 3D 表面色（**不是**界面配色）。后端给的是 surface 那一套 —— 见后端 seed_data 的说明 */
  palette: Record<string, string | null>
  wall_color: string
  /**
   * 3D 场景自己的地面色，**比 `floor_color` 深**。
   *
   * ⚠️ 两个都要用对：`surface_floor` 是给 3D 的地面，`floor_color` 是
   * 2D 平面图的底色。拿后者铺 3D 地面的话，浅色家具会跟地面糊在一起
   * （实测 modern 的 fabric 只有 1.01:1）。
   */
  surface_floor: string
  /** 界面/2D 用的地面色。3D **不要**用它铺地 */
  floor_color: string
  rooms: FurnitureRoom[]
  placed_count: number
  rejected: FurnitureRejection[]
  warnings: string[]
  notes: string[]
  /**
   * 3D 场景被**等比放大**了多少倍（1 = 没放大）。
   *
   * 家具摆不下时，后端会把 3D 场景放大到装得下为止 —— 见
   * `backend/app/services/furniture/scaling.py`。**只放大 3D**：
   * 平面图、热区、造价仍按真实尺寸。所以界面上必须显示这个倍数，
   * 否则用户会拿 3D 目测房间大小，而那是个错的数。
   */
  scene_scale?: number
  scene_scale_note?: string
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
/**
 * ⚠️ **这份表必须与后端 `schemas/plan.py` 的 `PlanStyle` 逐字对应。**
 *
 * 它同时是两个东西的数据源：
 *   ① 展示层的中文名（`PlanCard` / `PlanDrawer` / `DiffMatrix`）
 *   ② 「生成参数」里风格下拉框的**可选项**（`GenerateView.vue`）
 *
 * 因为 ②，这里多一个键就等于**让用户选到一个后端不支持的值**，
 * 而后端的表现是静默的：`space_planner.py` 用
 * `_STYLE_HINTS.get(style, '按该风格的通行做法处理')` 兜底，
 * 那一套方案就**拿不到任何风格引导**，但界面上仍显示那个风格名。
 *
 * 实测踩过（2026-09-23）：这里曾写 `luxury: '意式轻奢'`，后端没有这个值，
 * 于是「生成参数」默认的第三套（高端档）实际是"无风格"生成的。
 * 另外 `japanese` 也与后端的 `japandi` 对不上。
 *
 * 后端权威枚举：modern / nordic / chinese / cream / japandi / industrial
 */
export const STYLE_LABEL: Record<string, string> = {
  modern: '现代简约',
  nordic: '北欧自然',
  chinese: '新中式',
  cream: '奶油风',
  japandi: '日式侘寂',
  industrial: '工业风',
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

/**
 * 任务状态该怎么说给用户听。**任何展示 `phase_text` 的地方都该过这一层。**
 *
 * ⚠️ 为什么不能直接用 `phase_text`：
 *
 * 用户按下「中断」之后，后端的 `phase` **刻意停在断点上**（不写成 `done` ——
 * 那会让进度条假装走完，而且 `done` 的文案是"完成"）。
 * 于是 `phase_text` 会一直说"正在分析图片…"，而那条任务早就停了。
 * 页面头的状态胶囊正是直接展示 `phase_text` 的，实测就出现了
 * "进度卡说『已中断』、页头却说『正在分析图片…』"这种自相矛盾的画面。
 */
export function statusHeadline(
  snap: Pick<TaskStatusData, 'cancelled' | 'status' | 'phase_text'> | null | undefined,
): string {
  if (!snap) return ''
  if (snap.cancelled) return '已中断'
  return snap.phase_text
}

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
  /**
   * 这扇门**通不通**。
   *
   * ⚠️ `false` = **画出来但走不过去**（典型是入户门：它开在外墙上，
   * 两侧不构成"两间房之间"）。不可通行的门 `from_room`/`to_room` 都是 `-1`。
   *
   * 这个字段存在的原因就是"平面图与 3D 对不上"：这些门原先**根本不返回**，
   * 于是矢量图上画了、3D 里没有。
   */
  passable: boolean
  /** 转轴位置（米）。门扇绕它旋转 */
  hinge: [number, number]
  /** 沿墙的单位方向：从铰链指向门洞另一端 */
  along: [number, number]
  /** 墙的单位法向。门扇全开时朝这个方向 */
  normal: [number, number]
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
  /**
   * 3D 场景被**等比放大**了多少倍（1 = 没放大）。
   *
   * ⚠️ 放大之后，这份响应里的长度（房间、墙、门、出生点）**全部是放大后的**，
   * 而 `player_radius_m` / `eye_height_m` 仍是真人尺寸 —— 那是"放大房子、
   * 不放大人的"这个口径的直接体现。`plan_transform.scale` 已经除过 k，
   * 所以小地图上的绿点仍然落在正确的位置。
   *
   * **平面图接口（`/plan.svg`、`/hotspots`）不受影响，仍是真实尺寸。**
   */
  scene_scale?: number
  scene_scale_note?: string
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


// ══════════════════════════════════════════════════════════════════
// 4.6′ 知识库与工作台统计（2026-09-26 补齐）
// ══════════════════════════════════════════════════════════════════

/** 库里的一篇文档（**按 `source` 聚合**，不是一条 chunk）。 */
export interface KnowledgeDocument {
  source: string
  source_dir: string
  doc_type: string
  priority: string
  tags: string[]
  chunks: number
  /** 章节路径，最多几条 —— 用来在清单里看出这篇讲什么 */
  headings: string[]
}

/**
 * `GET /knowledge/list` 的返回体。
 *
 * ⚠️ `available: false` **不是错误**，是"这个能力现在用不了，因为 reason"。
 * 与 `/system/health` 同一立场 —— 界面据此显示状态，
 * 而不是把它渲染成一片空白（空白会被读成"知识库是空的"）。
 */
export interface KnowledgeListData {
  available: boolean
  reason: string
  collection: string
  path: string
  chunk_count: number
  document_count: number
  documents: KnowledgeDocument[]
  /** 可选语料类型。**由后端给** —— 前端硬编码一份会随语料类型变更漂移 */
  doc_types: string[]
  limits: { max_upload_chars: number; min_chunk_chars: number }
  notes: string[]
}

/** `POST /knowledge/upload` 的返回体。 */
export interface KnowledgeUploadResult {
  source: string
  written: number
  chunk_count: number
  available: boolean
  notes: string[]
}

/** `GET /dashboard/stats` 的返回体。 */
export interface DashboardStatsData {
  available: boolean
  reason: string
  /**
   * ⚠️ `null` 表示**读不到**，不是 0 —— 两者含义相反（见后端注释）。
   * 界面上必须区别显示："—" 与 "0"。
   */
  layouts: number | null
  plans: number | null
  audit_events: number | null
  by_action: Record<string, number>
  kb: { available: boolean; reason: string; collection: string; chunks: number; documents?: number }
  notes: string[]
}

/**
 * 一个阶段/一类请求的性能摘要。
 *
 * ⚠️ **`available: false` 与"0 毫秒"是两件事。** 样本不足时后端给
 * `available: false` + `reason`，而不是给一个 0 —— 性能报告里一个假的 0
 * 会被读成"快到无法测量"，而真相是"根本没数据"。
 */
export interface MetricSummary {
  label: string
  available: boolean
  reason?: string
  unit?: string
  n?: number
  p50?: number
  p95?: number
  max?: number
  target_p95?: number
  /** 是否达到需求文档 2.3.1 给的目标。**后端算好给**，前端不重算 */
  meets_target?: boolean
  source?: string
}

/** 按 Agent 分的 LLM 调用指标。`avg_total_tokens` 就是"Token 消耗"。 */
export interface AgentMetric {
  unit: string
  n: number
  p50: number
  p95: number
  max: number
  avg_total_tokens: number
  /**
   * 这个环节的**中文名**（`A-01` → 「户型解析」）。
   *
   * ⚠️ 键名仍是内部代号（接口结构不动），但**界面上不该出现 `A-01`** ——
   *    验收要求清掉这类工程细节。所以后端在条目里补了一个 `label`，
   *    前端渲染它。缺失时前端回落到代号（宁可露出代号，也不要渲染空白格）。
   */
  label?: string
}

/** `GET /system/metrics` 的返回体（AC-23）。 */
export interface MetricsData {
  window_days: number
  metrics: Record<string, MetricSummary>
  llm_by_agent: Record<string, AgentMetric>
  /** 在飞任务的 id。⚠️ 所以这个接口要登录 —— task_id 是取消任务的凭据 */
  running_tasks: string[]
  /**
   * 这批数字是从哪儿来的。
   *
   * ⚠️ **是对象不是字符串。** 第一版按字符串写、模板里直接
   * `{{ metrics.source }}` —— 那样渲染出来是 `[object Object]`，
   * 而类型检查**不会报错**（`unknown`/对象都能插值）。
   * 属于"看起来像个故障、但不报错"的那种。
   */
  source: {
    audit_files: string[]
    scanned_lines: number
    task_samples: Record<string, number>
    llm_samples: Record<string, number>
    /** 聚合过程中出错的文件与原因。非空就说明这批数字**不完整** */
    errors: string[]
  }
  notes: string[]
  persistence: {
    enabled: boolean
    available: boolean
    reason?: string
    sink: Record<string, number>
    pool: Record<string, unknown>
    migrations?: string[]
    audit_rows?: number
    note?: string
  }
}

// ══════════════════════════════════════════════════════════════════
// 4.3‴ 地面材质替换（AC-10，2026-09-26 重定性）
// ══════════════════════════════════════════════════════════════════

/** 一种可以用在地面的材料。**清单由后端给**，前端不硬编码。 */
export interface FloorMaterialOption {
  id: string
  name: string
  brand: string
  is_quanyou: boolean
  spec: string
  price_range: [number, number]
  /** 材料**代表色**（`#RRGGBB`）。它是代表色、不是效果图 */
  swatch: string
  search_url: string
}

/** 一间房当前换了什么。 */
export interface FloorSubstitution {
  room_index: number
  room_name: string
  area_m2: number
  /** 材料下架时为 null —— 这时看 `note` 怎么说 */
  material: FloorMaterialOption | null
  /**
   * 造价区间 = 面积 × 单价区间。
   *
   * ⚠️ **是区间不是数**：单价本身有区间，取中位数会变成一个看起来
   * 很确定、其实不确定的数字（后端有意不这么做）。
   */
  /** `unit` 是「元」—— 这是**总额**（已乘面积），不是单价。单价在 material 里 */
  cost: { min: number; max: number; unit: string } | null
  note?: string
}

export interface FloorMaterialsData {
  layout_id: string
  /** 每间房的地面（点图选房用）。后端 2026-09-26 起返回 */
  rooms: FloorMaterialRoom[]
  substitutions: FloorSubstitution[]
  eligible: FloorMaterialOption[]
  notes: string[]
}

/** 一间房的地面：换材质时要按面积算造价，所以面积也在这儿 */
export interface FloorMaterialRoom {
  room_index: number
  room_name: string
  area_m2: number
  /** 当前材料 id；没换过是空串 */
  material_id: string
}
