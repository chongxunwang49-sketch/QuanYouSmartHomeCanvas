<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import type * as THREE_NS from 'three'

import AppIcon from './AppIcon.vue'
import {
  layoutFurniture,
  layoutPlanSvg,
  layoutWalkable,
  messageOf,
} from '../api'
import type { FurnitureData, WalkableData, WalkableResponse } from '../api'
import { cappedPixelRatio, readGpuInfo, useFrameStats, type GpuInfo } from '../composables/useFrameStats'
import type { CameraRig } from '../three/rig'
// ⚠️ 从 coords / keys 引，不从 rig 引 —— rig 会静态拉进 three（570KB）
import { DEFAULT_HFOV_DEG, MAX_HFOV_DEG, MIN_HFOV_DEG } from '../three/coords'
import { ACTIONS, bindingsFor, helpLine, KEYMAP, ONCE_MAP, type Action, type OnceAction } from '../three/keys'

import type { SceneHandles } from '../three/scene'

/**
 * 3D 户型漫游（第一人称行走 / 自由视角）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 前端不推导任何几何
 * ══════════════════════════════════════════════════════════════════
 * 墙体、地面、碰撞、出生点、连通图 —— 全部来自后端 `/layout/{id}/walkable`。
 * 前端只做三件事：建 Mesh、听键盘、摆相机。
 *
 * ══════════════════════════════════════════════════════════════════
 * ⚠️ 小地图不只是装饰，它是**镜像问题的唯一自动发现手段**
 * ══════════════════════════════════════════════════════════════════
 * 户型在 3D 里左右镜像之后**看上去完全正常** —— 还是一间卧室一间客厅，
 * 没有任何元素会提示"厨房跑到对面去了"。
 *
 * 所以这里把玩家位置**同时**画在两张图上：
 *   · 3D 世界里由 Three 渲染
 *   · 后端渲染的 2D 户型 SVG 上画一个点
 * 后者是**独立计算**（后端的投影，有 pytest 钉着朝向），前者走
 * `three/coords.ts` 的映射。映射写反的话，点会跑到户型的另一侧 ——
 * 走两步就看得出来。
 */

/**
 * ⚠️ **家具是「方案」的属性，不是「户型」的属性**（2026-09-24 需求方明确）。
 *
 * 同一个户型可以有三套装修方案，家具因此有三种摆法；而户型本身的 3D
 * 是一份、与方案无关。所以这个组件用 `planId` 决定画不画家具：
 *
 *   只给 layoutId                 → 空房子。这是「户型解析」的 3D 漫游
 *   layoutId + planId             → 在**同一份户型几何**上放这套方案的家具。
 *                                   这是「方案生成 · 三选一」之后的 3D 漫游
 *
 * 两条路径共用同一份 `/walkable` 几何，**不从 0 重新建模** —— 需求方
 * 原话：「「方案生成」模块的 3d 漫游就是在「户型解析」3d 漫游的情况下
 * 放入家具和装修，而不是自己重新从 0 生成，这样可以省资源和等待时间」。
 *
 * 之前的实现是无条件拉家具并画上 —— 于是「户型解析」的 3D 里出现了家具，
 * 而那里本来应该是空房子。那不是多画了几件东西：它让"解析结果"看起来
 * 已经带了装修，用户分不清看到的是户型还是方案。
 */
const props = withDefaults(
  defineProps<{
    layoutId: string
    height?: string
    /** 装修方案 ID。给了才画家具（见上面的说明）。 */
    planId?: string
    /** 方案风格，决定家具配色。不给时后端按 modern 兜底。 */
    planStyle?: string
  }>(),
  { height: '520px', planId: '', planStyle: '' },
)

const canvasEl = ref<HTMLCanvasElement | null>(null)
const wrapEl = ref<HTMLElement | null>(null)

const data = shallowRef<WalkableResponse | null>(null)
const planSvg = ref('')
const loading = ref(true)
const error = ref('')

const locked = ref(false)
const mode = ref<'walk' | 'fly'>('walk')
const currentRoom = ref('')
const bumping = ref(false)

/** 当前水平视场角。用户可调，存 localStorage —— 这是主观偏好，不该写死。 */
const hFov = ref(Number(localStorage.getItem('qy.viewer.hfov') || DEFAULT_HFOV_DEG))

/**
 * 当前会被 `F` 作用的那扇门。
 *
 * ⚠️ `byAim` 要显示给用户：**"F 会作用在哪扇门"必须是看得见的**。
 * 这正是"两扇门贴得近时开错了门"这个问题的根治办法 —— 距离判据下
 * 用户无从知道系统选了哪一扇，只能靠按下去再试。
 */
const doorTarget = ref<{ index: number; open: boolean; dist: number; byAim: boolean } | null>(null)
/** 最近一次开关门的提示 */
const doorToast = ref('')
let doorToastAt = 0
/** 上一帧点亮的门。切目标时先熄灭它，否则会留下一路亮着的门。 */
let litDoor: { index: number; setHighlight(on: boolean): void } | null = null

const stats = useFrameStats()
/** 探测到的显卡。拿不到就是 null，不影响运行。 */
const gpu = shallowRef<GpuInfo | null>(null)
const webglFailed = ref('')

const walkable = computed<WalkableData | null>(() => data.value?.walkable ?? null)
/**
 * 家具。**只有给了 `planId` 才会去取**（见 props 的说明）。
 * null = 这套没有家具，画空房子。`furnitureError` 单独记"该有但取不到"。
 */
const furniture = shallowRef<FurnitureData | null>(null)
const furnitureError = ref('')
/** 摆不下的家具 —— 与"没生成"是两件事，界面上要分开说。 */
const furnitureRejected = computed(() => furniture.value?.rejected ?? [])
/** 这次要不要画家具。见 props 的说明。 */
const wantsFurniture = computed(() => Boolean(props.planId))
/** 模板里要读门的状态，用一个浅引用桥接（handles 是普通变量，Vue 追踪不到） */
const handlesRef = shallowRef<SceneHandles | null>(null)
/**
 * 后端判定的"可贴地行走"。**这是"能不能切"，不是"默不默认"。**
 *
 * ⚠️ 2026-09-26 需求方定了：**自由视角是默认视角**。
 *
 * 起因是实测 —— 同一张演示图解析 6 次只有 3 次判定可行走
 * （可达率 33%~56%，从未超过 56%）。也就是"打开就是自由视角"是**常态**，
 * 而原来的界面把它当失败来措辞（"后端判定不可行走"+ 一屏 issue），
 * 相机还放在房间里 1.6m 高处平视 —— 那个机位什么都看不出来。
 *
 * 所以现在：**开局一律自由视角，起点在天花板之上俯视**（见 `rig.ts`
 * 的 `FLY_VANTAGE_*`），能走的户型多给一个"下到地面行走"。
 */
const canWalk = computed(() => walkable.value?.mode === 'walk')

/**
 * ⚠️ **three.js 走动态 import，不静态引入。**
 *
 * 它是 568 KB（gzip 148 KB）的东西，而 3D 漫游只是解析页上的一个模块 ——
 * 静态引入的话，**每次打开解析页都要先下完 three**，哪怕用户根本不点漫游。
 *
 * 这个项目的既有纪律就是"桶式入口摇不掉、深路径导入压到 28 KB"，
 * 在这里无条件背上 568 KB 是说不过去的。动态 import 让 Vite 把它切成
 * 独立 chunk，只有 `build()` 真的跑到时才加载。
 */
let renderer: THREE_NS.WebGLRenderer | null = null
let handles: SceneHandles | null = null
let rig: CameraRig | null = null
let ro: ResizeObserver | null = null
let lastT = 0
let bumpTimer = 0
let doorAt = 0

/** 够得着门的距离（米）。**口径在 `three/scene.ts` 里，一处定义。** */
const DOOR_REACH_M = 2.6

/**
 * 按键状态。字段名 = `three/keys.ts` 的 `Action`，**那份表是唯一定义处**。
 *
 * 用 `??=` 而不是写死一份初始对象：键表加了新动作时，这里漏一个就是
 * `keys[action]` 恒为 undefined 的静默失败（表现为"新键按了没反应"）。
 */
const keys = {} as Record<Action, boolean>
for (const a of ACTIONS) keys[a] = false

// ══════════════════════════════════════════════════════════════════
// 加载与建场景
// ══════════════════════════════════════════════════════════════════

async function load() {
  if (!props.layoutId) return
  loading.value = true
  error.value = ''
  furnitureError.value = ''
  try {
    // 三个请求并行：3D 几何 + 家具 + 2D 户型图（小地图底图）
    //
    // ⚠️ 没有 `planId` 时**根本不发家具请求** —— 不是"发了但不用"。
    //    户型解析的 3D 是空房子，那是一个确定的结论，不是一个缺省。
    //    发出去再丢掉会让后端白算一遍摆放（要遍历房间 × 目录），
    //    也会让日志里出现一个没人用的调用。
    const [payload, furn, svg] = await Promise.all([
      layoutWalkable(props.layoutId),
      wantsFurniture.value
        // ⚠️ **家具拿不到不挡住 3D。** 空房子也比没有房子好。
        //    但"为什么没有家具"要记下来给用户看，不能静默（AC-17）。
        ? layoutFurniture(props.layoutId, {
            planId: props.planId,
            style: props.planStyle || undefined,
          }).catch((e: unknown) => {
            furnitureError.value = messageOf(e) || '家具数据取不到'
            return null
          })
        : Promise.resolve(null),
      layoutPlanSvg(props.layoutId).catch(() => ''),   // 小地图拿不到不该挡住 3D
    ])
    furniture.value = furn
    data.value = payload
    planSvg.value = svg
    // ⚠️ **开局一律自由视角**，不跟 `payload.walkable.mode` 走。
    //    跟它走的话，同一个演示户型两次打开会是两种视角 —— 而"能走"
    //    只有一半的时候成立（实测 6 次解析 3 次）。见 `canWalk` 的说明。
    mode.value = 'fly'
    loading.value = false
    await buildAfterPaint()
  } catch (e) {
    error.value = messageOf(e)
    loading.value = false
  }
}

watch(() => [props.layoutId, props.planId], load, { immediate: true })

/** 等一帧再建：canvas 的 clientWidth/Height 要等布局完成才有值。 */
async function buildAfterPaint() {
  await new Promise((r) => requestAnimationFrame(r))
  try {
    await build()
  } catch (e) {
    // 动态 import 失败（断网 / 产物缺失）必须说出来，不能只留一个空白画布
    webglFailed.value = `3D 模块加载失败：${(e as Error).message}`
  }
}

async function build() {
  const host = canvasEl.value
  const payload = data.value
  if (!host || !payload) return

  // three.js 与依赖它的两个模块在这里才加载（见上面的说明）。
  // 三个一起 await：它们是同一个 chunk，不会多花往返。
  const [three, rigMod, sceneMod] = await Promise.all([
    import('three'),
    import('../three/rig'),
    import('../three/scene'),
  ])
  // 组件在 await 期间可能已被卸载
  if (!canvasEl.value || !data.value) return

  // ── WebGL 可用性：**先探测再建**，不测的话失败信息没人看得懂 ──
  try {
    renderer = new three.WebGLRenderer({ canvas: host, antialias: true, powerPreference: 'high-performance' })
  } catch (e) {
    webglFailed.value = `本机浏览器无法创建 WebGL 上下文：${(e as Error).message}`
    return
  }
  if (!renderer.getContext()) {
    webglFailed.value = '本机浏览器不支持 WebGL，3D 漫游不可用'
    return
  }

  gpu.value = readGpuInfo(renderer.getContext())
  renderer.setPixelRatio(cappedPixelRatio(stats.tier.value))
  renderer.shadowMap.enabled = false   // 见下方「为什么不开阴影」

  handles = sceneMod.buildScene(payload, furniture.value)
  handlesRef.value = handles
  handles.applyTier(stats.tier.value)
  // 建完立刻按当前模式定天花板 —— 后端判"不可行走"时开局就是自由视角，
  // 不在这里设的话开局会被天花板挡住
  applyCeiling()

  const aspect = (canvasEl.value?.clientWidth || 16) / (canvasEl.value?.clientHeight || 9)
  // ⚠️ **必须把起始模式显式传进去。** 不传的话 rig 会沿用
  //    `walkable.mode`（后端判的"能不能贴地行走"），于是**可走路的户型
  //    开局是行走、不可走路的才是自由视角** —— 而需求方要的是
  //    "自由视角当默认"，两种户型开局应当一致。
  //    两处不一致的后果实测过：界面写着"自由视角（俯瞰格局）"，
  //    画面却是一扇门贴在眼前，而且第一下按 G 没有反应。
  rig = new rigMod.CameraRig(payload.walkable, aspect, mode.value)
  rig.setHorizontalFov(hFov.value)

  resize()
  ro = new ResizeObserver(resize)
  if (wrapEl.value) ro.observe(wrapEl.value)
  stats.start()
  renderer.setAnimationLoop(frame)
}

/**
 * 为什么不开阴影。
 *
 * 阴影是这里最贵的一项（每个点光源一张 shadow map）。而本机可用显存
 * 是 4GB、还是双显卡笔记本（浏览器可能跑在 Intel 核显上）——
 * 开了之后帧率会掉得比"没有影子"严重得多，而室内本来光线就平，
 * 影子带来的观感提升很有限。**先保证走得顺**。
 */
const SHADOWS_OFF_REASON = '室内光照较平，阴影收益有限而开销大；优先保证帧率'

function resize() {
  const el = wrapEl.value
  if (!renderer || !el) return
  const w = el.clientWidth
  const h = el.clientHeight
  if (w === 0 || h === 0) return
  renderer.setPixelRatio(cappedPixelRatio(stats.tier.value))
  renderer.setSize(w, h, false)
  // ⚠️ 必须走 rig.setAspect：它同时重算垂直 FOV。
  // 只改 aspect 的话画面一变形，视野宽窄也跟着变。
  rig?.setAspect(w / h)
}

// 视野变了立刻生效，并存下来（用户偏好）
watch(hFov, (v) => {
  rig?.setHorizontalFov(v)
  localStorage.setItem('qy.viewer.hfov', String(v))
})

// 画质降档后要立刻生效
watch(
  () => stats.tier.value,
  (t) => {
    handles?.applyTier(t)
    resize()
  },
)

// ══════════════════════════════════════════════════════════════════
// 主循环
// ══════════════════════════════════════════════════════════════════

function frame(now: number) {
  if (!renderer || !handles || !rig) return
  const dt = lastT ? Math.min((now - lastT) / 1000, 0.1) : 0
  lastT = now

  // ⚠️ 单帧 dt 封顶 0.1 秒。不封的话，切走标签页再切回来时
  //    dt 会是几十秒，玩家一帧之内被瞬移到户型外面。
  const hit = rig.update({ ...keys }, dt)
  currentRoom.value = rig.currentRoom()

  // 门相关的提示与瞄准判定，按 10Hz 更新就够了
  if (now - doorAt >= 100) {
    doorAt = now
    // ⚠️ 只加锁时才瞄准。没加锁时鼠标是系统光标，用户指不了画面正中，
    //    而且引导层会盖住画布 —— 那时还亮着一扇门只会让人困惑。
    if (locked.value) updateDoorTarget()
    else setDoorTarget(null)
    if (doorToastAt > 0) {
      doorToastAt -= 100
      if (doorToastAt <= 0) doorToast.value = ''
    }
  }

  if (hit) {
    bumping.value = true
    bumpTimer = 300
  } else if (bumpTimer > 0) {
    bumpTimer -= dt * 1000
    if (bumpTimer <= 0) bumping.value = false
  }

  renderer.render(handles.scene, rig.camera)
  stats.tick(now)

  if (now - dotAt >= DOT_INTERVAL_MS) {
    dotAt = now
    syncDot()
  }
}

// ══════════════════════════════════════════════════════════════════
// 输入
// ══════════════════════════════════════════════════════════════════

/**
 * 一次性动作的分发。**映射在 `three/keys.ts`**，这里只实现行为。
 *
 * ⚠️ 之前 `KEYMAP` 和这一段 if 各写着"哪个键干什么"，加上界面提示文案
 * 一共三份。三份里改一份，另外两份都不报错。
 */
const ONCE: Record<OnceAction, () => void> = {
  door: () => toggleDoor(),
  respawn: () => rig?.respawn(),
  mode: () => toggleMode(),
}

function onKeyDown(e: KeyboardEvent) {
  if (!locked.value) return
  const k = KEYMAP[e.code]
  if (k) {
    keys[k] = true
    e.preventDefault()   // 方向键/空格会滚动页面
    return
  }
  const once = ONCE_MAP[e.code]
  if (once) {
    ONCE[once]()
    e.preventDefault()
  }
}

/**
 * 抬起按键。**这里不看 `locked`** —— 加锁状态变化与按键抬起是两件独立的事，
 * 而且漏掉一次抬起就会留下一个"按住不放"的键。
 */
function onKeyUp(e: KeyboardEvent) {
  const k = KEYMAP[e.code]
  if (k) {
    keys[k] = false
    e.preventDefault()
  }
}

/**
 * 把**所有**按键状态清空。
 *
 * ⚠️ 有两个触发点，缺一不可：
 *   · 失去指针锁定（按 Esc）—— 不清的话，按住 W 时按 Esc，
 *     人会**一直往前走**直到撞墙，看起来像失控
 *   · 窗口失焦（Alt+Tab / 切标签页）—— **浏览器在失焦后不再派发
 *     keyup**，于是按住 W 切走再切回来，人还在往前走。
 *     这个我是量出来的：切走标签页再切回来，`keys.forward` 仍是 true，
 *     下一个 `mousemove` 之前人就一直在飘。
 */
function clearKeys() {
  for (const a of ACTIONS) keys[a] = false
}

function onBlur() {
  clearKeys()
}

function onMouseMove(e: MouseEvent) {
  if (!locked.value || !rig) return
  rig.look(e.movementX, e.movementY)
}

async function requestLock() {
  const el = canvasEl.value
  if (!el) return
  try {
    await el.requestPointerLock()
  } catch {
    /* 浏览器拒绝（比如刚退出锁定后立刻再请求）—— 忽略，用户再点一次即可 */
  }
}

function onLockChange() {
  locked.value = document.pointerLockElement === canvasEl.value
  if (!locked.value) {
    clearKeys()
    setDoorTarget(null)
  }
}

/**
 * 切行走 / 自由视角。
 *
 * ⚠️ 切回**自由视角时保持当前高度**（不做"瞬移到俯瞰位"）——
 *    从房间里按 G 的人想要的是"浮起来越过墙看看"，把他弹到屋顶之上
 *    会很突兀。要回到俯瞰位按 `R`（`respawn()` 按当前模式取起点姿态）。
 */
function toggleMode() {
  if (!canWalk.value) return          // 后端判了不可行走，就不给切过去（会穿墙）
  mode.value = mode.value === 'walk' ? 'fly' : 'walk'
  rig?.setMode(mode.value)
  applyCeiling()
  setDoorTarget(null)                 // 换模式后准星要重新瞄
}

/**
 * 天花板跟随模式。**飞行时自动变透明。**
 *
 * 需求方原话：「启动飞行模式就是为了在高处看格局，如果被天花板挡住了
 * 就没有任何意义」。飞行时人是往上飞的，而天花板正好在唯一的观察方向上。
 *
 * 行走时**必须留着** —— 没有它，抬头看到的是场景背景色，不像在室内。
 */
function applyCeiling() {
  handles?.setCeilingVisible(mode.value === 'walk')
}

/**
 * 更新"F 会作用在哪扇门"。
 *
 * ══════════════════════════════════════════════════════════════════
 * 准星优先，距离兜底 —— 顺序不能反
 * ══════════════════════════════════════════════════════════════════
 * 需求方原话：「两个门贴的很近时按 F 会出现相应的不是自己想要响应的门，
 * 希望你做一个视角中心点（类似于枪战游戏），遇到两个门离得很近的时候
 * 可以通过中心点对准门进行选定」。
 *
 * 所以判据是**看哪儿**，不是**离哪扇近** —— 距离是用户无法控制的量，
 * 站在两门之间时它根本没有表达"我想开哪一扇"的能力。
 *
 * 距离判定保留为兜底：准星对着地板/家具/空处时它仍然生效，
 * 否则用户会遇到"明明站在门口，按 F 却没反应"。兜底时
 * `byAim = false`，HUD 会把这件事说明白 —— **不能让用户猜**。
 */
function updateDoorTarget() {
  if (!rig || !handles?.doors.length) {
    setDoorTarget(null)
    return
  }
  const aimed = handles.doorAtCrosshair(rig.camera)
  if (aimed) {
    const [px, py] = rig.planPosition()
    setDoorTarget({
      index: aimed.index,
      open: aimed.isOpen(),
      dist: Math.hypot(aimed.planPos[0] - px, aimed.planPos[1] - py),
      byAim: true,
    })
    return
  }
  const [px, py] = rig.planPosition()
  const near = handles.nearestDoor([px, py], DOOR_REACH_M)
  if (!near) {
    setDoorTarget(null)
    return
  }
  setDoorTarget({
    index: near.index,
    open: near.isOpen(),
    dist: Math.hypot(near.planPos[0] - px, near.planPos[1] - py),
    byAim: false,
  })
}

/** 换目标时先把上一扇熄灭 —— 不清的话会留下一路亮着的门。 */
function setDoorTarget(t: { index: number; open: boolean; dist: number; byAim: boolean } | null) {
  if (litDoor && litDoor.index !== t?.index) {
    litDoor.setHighlight(false)
    litDoor = null
  }
  if (t && !litDoor) {
    const d = (handles?.doors ?? []).find((x) => x.index === t.index)
    if (d) {
      d.setHighlight(true)
      litDoor = d
    }
  }
  doorTarget.value = t
}

/** 开关当前目标门（准星对准的那扇，或兜底选出的最近那扇）。 */
function toggleDoor() {
  const t = doorTarget.value
  if (!t) return
  const d = (handles?.doors ?? []).find((x) => x.index === t.index)
  if (!d) return
  const willOpen = !d.isOpen()
  d.setOpen(willOpen ? 1 : 0)
  syncDoorCollision()
  doorToast.value = willOpen ? '门已打开' : '门已关上'
  doorToastAt = 1400
  updateDoorTarget()
}

/** 把关着的门变成碰撞线段交给 rig —— 否则关上门还能直接走过去。 */
function syncDoorCollision() {
  const segs = (handles?.doors ?? [])
    .map((d) => d.blockingSegment())
    .filter((s): s is [[number, number], [number, number]] => s !== null)
    .map((s) => ({ a: s[0], b: s[1] }))
  rig?.setExtraCollision(segs)
}

/**
 * 这间房现在能不能跳过去。
 *
 * ⚠️ **取决于模式，不是只看 `reachable`。** `reachable` 说的是
 * "贴地行走模式下从出生点走得到吗"；自由视角是穿墙的，每间房都到得了。
 * 原来一律按 `reachable` 置灰，于是自由视角下能去的房间点不动。
 */
function jumpable(r: WalkableData['rooms'][number]): boolean {
  return mode.value === 'fly' || r.reachable
}

/** 跳到某个房间的中心。房间中心是后端算好的**净空中心**，一定站得住。 */
function goToRoom(index: number) {
  rig?.goToRoom(index)
  syncDoorCollision()
  updateDoorTarget()
}

// ══════════════════════════════════════════════════════════════════
// 小地图：把玩家画在后端渲染的户型图上
// ══════════════════════════════════════════════════════════════════

const playerDot = ref<{ x: number; y: number } | null>(null)
const mappingChecked = ref(false)
const mappingWarning = ref('')
/** 小地图刷新间隔。跟着 60fps 刷会让 Vue 每帧重渲染一次，没必要。 */
const DOT_INTERVAL_MS = 100
let dotAt = 0

/**
 * 把玩家位置换算到**后端户型图的画布像素**。
 *
 * ⚠️ 这段是**独立于 Three** 的一套换算 —— 用的是后端给的投影参数
 *    （`plan_transform`），和 3D 世界里的位置出自同一个 `planPosition()`，
 *    但投影公式来自后端（有 pytest 钉着朝向）。
 *
 *    两套互相独立的结果指向同一个位置，所以**映射写反时看得见**：
 *    小地图上的绿点会跑到户型的另一侧。这正是"3D 户型镜像了"
 *    这个问题唯一的自动发现手段。
 */
function syncDot() {
  const t = data.value?.plan_transform
  if (!rig || !t) return
  const [px, py] = rig.planPosition()
  playerDot.value = {
    x: t.offset_x + px * t.scale,
    y: t.offset_y + (t.draw_depth_m - py) * t.scale,
  }
  verifyMapping(false)
}

/**
 * ⚠️ **映射自检：这是"3D 户型被镜像"唯一能被自动抓住的地方。**
 *
 * 原理是拿两个**互相独立**的计算做交叉验证：
 *
 *   ① 玩家在房间里的位置 —— 来自 Three 世界坐标，经 `coords.ts` 换算
 *      成图纸坐标，再经**后端的**投影参数换算成画布像素
 *   ② 该房间的多边形 —— 直接读后端渲染的 SVG 里那个 `<polygon>`
 *
 * 如果 `planToEngine` 的符号写反（图纸 y 映射到 +z 而不是 -z），
 * 3D 里整个户型会左右镜像，而**画面上完全看不出来**（还是一间卧室
 * 一间客厅）。但那时 ① 算出来的点会落到 ② 的多边形**外面** ——
 * 通常还落在隔壁房间的多边形里。这里就会报出来。
 *
 * 只在第一次拿到房间时判一次，之后不再打扰。
 */
function verifyMapping(force: boolean) {
  if (mappingChecked.value && !force) return
  const idx = rig?.currentRoomIndex() ?? -1
  const dot = playerDot.value
  if (idx < 0 || !dot) return

  // 后端 SVG 里的房间多边形是**画布像素**，和 dot 同一坐标系
  const el = wrapEl.value?.querySelector(`#room-${idx}`) as SVGPolygonElement | null
  if (!el) return
  const pts = (el.getAttribute('points') || '')
    .trim()
    .split(/\s+/)
    .map((pair) => pair.split(',').map(Number))
    .filter((p) => p.length === 2 && p.every(Number.isFinite))
  if (pts.length < 3) return

  const xs = pts.map((p) => p[0])
  const ys = pts.map((p) => p[1])
  const inside =
    dot.x >= Math.min(...xs) && dot.x <= Math.max(...xs) &&
    dot.y >= Math.min(...ys) && dot.y <= Math.max(...ys)

  mappingChecked.value = true
  if (!inside) {
    mappingWarning.value =
      `玩家位置换算到户型图上时落到了「${rig?.currentRoom()}」的轮廓之外 —— ` +
      `说明 three/coords.ts 的平面→世界映射有问题（多半是 y 轴符号写反，` +
      `表现为整个户型左右镜像）。3D 画面本身看不出来，请对照平面图核实。`
  }
}

// ══════════════════════════════════════════════════════════════════
// 生命周期
// ══════════════════════════════════════════════════════════════════

onMounted(() => {
  window.addEventListener('keydown', onKeyDown)
  window.addEventListener('keyup', onKeyUp)
  window.addEventListener('mousemove', onMouseMove)
  // 失焦必须清键，见 clearKeys 的说明
  window.addEventListener('blur', onBlur)
  document.addEventListener('pointerlockchange', onLockChange)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeyDown)
  window.removeEventListener('keyup', onKeyUp)
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('blur', onBlur)
  document.removeEventListener('pointerlockchange', onLockChange)
  if (document.pointerLockElement === canvasEl.value) document.exitPointerLock()
  stats.stop()
  ro?.disconnect()
  renderer?.setAnimationLoop(null)
  handles?.dispose()
  renderer?.dispose()
  renderer = null
  handles = null
  rig = null
})

/** 模板里用不了 `window`，所以在这里读一次。**Dpr 探测的输入值，必须在界面上可见** */
const devicePixelRatio = window.devicePixelRatio || 1

/**
 * 操作提示。**从 `three/keys.ts` 的按键表推出来，不手写。**
 *
 * 手写的那一份必然会漂移：改一个键、或者加一个新键，文案不会跟着变，
 * 于是界面教用户按一个不存在的键。（这一条是"上升/下降写进通用提示、
 * 而它们在行走模式下按了没反应"暴露出来的。）
 */
const controlHint = computed(() => helpLine(mode.value))

/**
 * 这个视角**是用来干什么的**。与按键表分开放是有意的：
 * 按键表是"按什么做什么"，这句是"为什么给你这个视角"。
 *
 * ⚠️ 需求方 2026-09-26 定了自由视角当默认，理由是实测 6 次解析只有 3 次
 * 判定可行走 —— 也就是说打开就是自由视角是常态。那么它就不该是一种
 * 需要解释的异常状态，而该直接说清它能干什么。
 */
const modePurpose = computed(() =>
  mode.value === 'fly'
    ? '从高处看整个格局。可以穿墙、可以升空 —— 走到哪儿都拦不住你。'
    : '贴着地面走，有碰撞、要开门。看的是"住进去是什么感觉"。',
)

/** 按键清单，给下面的"操作"折叠面板用。同样来自按键表。 */
const keyChips = computed(() =>
  bindingsFor(mode.value).filter((b) => b.action !== 'run' || b.code === 'ShiftLeft'),
)
</script>

<template>
  <section class="overflow-hidden rounded-2xl border border-warm-border bg-white shadow-md">
    <header class="flex flex-wrap items-center justify-between gap-2 border-b border-warm-border px-4 py-3">
      <div class="min-w-0">
        <h3 class="flex items-center gap-1.5 text-[14px] font-bold text-wood-dark">
          <AppIcon name="cube" :size="15" class="text-botanical" />
          {{ wantsFurniture ? '3D 装修漫游（含家具）' : '3D 户型漫游（空房子）' }}
        </h3>
        <!--
          ⚠️ 措辞不再把「自由视角」当成一种失败。
          原来写的是「自由视角（后端判定不可行走）」，还配金色告警色 ——
          读起来像"这个户型有问题"。而实测 6 次解析有 3 次是这种情况，
          也就是它**本来就是常态**（需求方 2026-09-26 定了自由视角当默认）。
          现在两句都是中性的，各自说清"这个视角能干什么"。
        -->
        <p class="mt-0.5 text-[11px] text-wood-muted">
          <template v-if="walkable">
            {{ walkable.rooms.length }} 间房 · {{ walkable.doors.length }} 扇门 ·
            <span class="text-botanical">自由视角（俯瞰格局，可穿墙）</span>
            <template v-if="canWalk">
              · <span class="text-botanical">这个户型也能贴地行走</span>
            </template>
            <template v-else>
              · <span class="text-wood-muted">解析出的门洞不足以支撑行走，故只提供自由视角</span>
            </template>
          </template>
          <template v-else-if="loading">正在准备 3D 场景…</template>
        </p>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <button
          v-if="canWalk"
          class="rounded-lg border border-warm-border px-2.5 py-1.5 text-[11px] font-medium text-wood transition hover:bg-botanical-surface"
          @click="toggleMode"
        >
          {{ mode === 'walk' ? '升空俯瞰 (G)' : '下到地面行走 (G)' }}
        </button>
        <span
          v-else
          class="rounded-lg border border-warm-border/60 px-2.5 py-1.5 text-[11px] text-wood-muted"
          title="后端判定这个户型的门洞不足以支撑贴地行走，所以不提供该模式"
        >仅自由视角</span>
        <span
          v-if="stats.fps.value"
          class="num rounded-lg px-2 py-1.5 text-[11px] font-medium"
          :class="stats.fps.value >= 45 ? 'bg-botanical-light text-botanical' :
                  stats.fps.value >= 25 ? 'bg-wood-light text-wood' : 'bg-accent-red/10 text-accent-red'"
          :title="`实测帧率。低于 30 会自动降一档画质`"
        >{{ stats.fps.value }} fps</span>
      </div>
    </header>

    <!-- ══ 画布 ══ -->
    <div ref="wrapEl" class="relative bg-warm-bg" :style="{ height: props.height }">
      <canvas
        ref="canvasEl"
        class="block h-full w-full"
        @click="requestLock"
      />

      <!-- 未进入漫游：盖一层引导。**不能省** —— 不进指针锁定时鼠标是系统光标，
           拖不动视角，用户会以为坏了 -->
      <div
        v-if="!locked && !loading && !error && !webglFailed"
        class="absolute inset-0 flex cursor-pointer flex-col items-center justify-center gap-2 bg-wood-dark/45 backdrop-blur-[2px]"
        @click="requestLock"
      >
        <AppIcon name="cube" :size="30" class="text-white/90" />
        <p class="text-[14px] font-semibold text-white">点击进入漫游</p>
        <!-- 先说来这个视角是干什么的，再说按什么键 -->
        <p class="max-w-md px-6 text-center text-[11px] leading-relaxed text-white/85 [text-wrap:balance]">
          {{ modePurpose }}
        </p>
        <!--
          ⚠️ 宽度给到 `max-w-xl` 是**按键表变长之后**才需要调的。
          原来写 `max-w-sm`（24rem），而现在的提示有 11 项、约 60 个字，
          于是在 384px 里折成三行、还把「R 回到出生点」从中间劈开 ——
          截图里看得很明显（`logs/v1-parse-empty.png`）。
          `[text-wrap:balance]` 让折行位置落在词组之间而不是字中间。
        -->
        <p class="max-w-xl px-6 text-center text-[11px] leading-relaxed text-white/70 [text-wrap:balance]">
          {{ controlHint }}
        </p>
        <p class="text-[11px] text-white/60">按 Esc 退出鼠标锁定</p>
      </div>

      <div v-if="loading" class="absolute inset-0 flex items-center justify-center">
        <div class="flex items-center gap-2 text-[12px] text-wood-muted">
          <AppIcon name="spinner" :size="16" class="animate-spin" />
          正在构建 3D 场景…
        </div>
      </div>

      <div
        v-else-if="error || webglFailed"
        class="absolute inset-0 flex flex-col items-center justify-center gap-2 px-8 text-center"
      >
        <AppIcon name="warning-circle" :size="26" class="text-accent-red" />
        <p class="text-[12px] font-medium text-wood-dark">3D 漫游无法启动</p>
        <p class="max-w-md text-[11px] leading-relaxed text-wood-muted">
          {{ error || webglFailed }}
        </p>
      </div>

      <!-- ══ HUD ══ -->
      <div
        v-if="!loading && !error && !webglFailed"
        class="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between gap-2 p-3"
      >
        <!-- 你在哪 -->
        <div class="flex flex-col gap-1.5">
          <div class="rounded-lg bg-white/85 px-2.5 py-1.5 backdrop-blur-sm">
            <p class="text-[10px] uppercase tracking-wide text-wood-muted">当前位置</p>
            <p class="text-[12px] font-bold text-wood-dark">
              {{ currentRoom || '走道 / 门洞' }}
            </p>
          </div>

          <!--
            家具状况。**四种状态分开说**，不合并成一句"家具已加载"：

              这是户型解析的 3D   → 说明"空房子是应该的，家具在方案里"
              有家具             → 报件数
              一件都没摆下        → 说明为什么（房间净空过小是最常见的）
              整块取不到          → 报错误（3D 仍然能看，只是空房子）

            ⚠️ 第一行是**必需的**：只画空房子而不说为什么，用户会以为
               家具生成失败了。这与"不许静默降级"是同一条 ——
               用户看到的是一间空房，他有权知道是"本来就该空"、
               是"没摆下"、还是"接口挂了"。
          -->
          <div
            v-if="!wantsFurniture || furnitureError || furnitureRejected.length"
            class="pointer-events-auto max-w-[220px] rounded-lg bg-white/85 px-2.5 py-1.5 backdrop-blur-sm"
            :title="furnitureError || furnitureRejected.map((r) => r.reason).join('；')"
          >
            <p class="text-[10px] uppercase tracking-wide text-wood-muted">家具</p>
            <p v-if="!wantsFurniture" class="text-[11px] leading-relaxed text-wood-muted">
              空房子 —— 家具属于<strong class="font-semibold">装修方案</strong>，选定方案后才画
            </p>
            <p v-else-if="furnitureError" class="text-[11px] text-accent-red">
              取不到家具数据（3D 仍可浏览）：{{ furnitureError }}
            </p>
            <p v-else class="text-[11px] text-wood-dark">
              有 {{ furnitureRejected.length }} 件摆不下（已跳过，未硬塞）
            </p>
          </div>
        </div>

        <!-- 小地图：**镜像问题的发现手段** -->
        <div
          v-if="planSvg && playerDot"
          class="relative overflow-hidden rounded-lg border border-warm-border bg-white/90 p-1 backdrop-blur-sm"
          style="width: 132px"
        >
          <div class="plan-mini" v-html="planSvg" />
          <svg
            class="pointer-events-none absolute inset-1"
            :viewBox="`0 0 ${data?.plan_transform.width_px ?? 1024} ${data?.plan_transform.height_px ?? 1024}`"
          >
            <circle
              :cx="playerDot.x"
              :cy="playerDot.y"
              r="26"
              fill="#4A7C59"
              fill-opacity="0.28"
            />
            <circle :cx="playerDot.x" :cy="playerDot.y" r="13" fill="#4A7C59" />
          </svg>
          <p class="mt-0.5 text-center text-[9px] leading-tight text-wood-muted">
            绿点＝你在 3D 里的位置<br />对上平面图即方向正确
          </p>
        </div>
      </div>

      <!--
        ══ 准星 ══
        射击游戏式的画面中心点。门的选择以它为准（见 updateDoorTarget）。
        命中门时变亮、放大一点 —— 这是"F 会作用在哪扇门"的第一重反馈。
      -->
      <div
        v-if="locked && !loading && !error && !webglFailed"
        class="pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2"
      >
        <span
          class="block rounded-full ring-1 transition-all duration-100"
          :class="doorTarget?.byAim
            ? 'h-2.5 w-2.5 bg-accent-gold ring-accent-gold/60'
            : 'h-1.5 w-1.5 bg-white/80 ring-black/30'"
        />
      </div>

      <!--
        门提示。**必须说清"F 会作用在哪一扇"** —— 这正是"
        两扇门贴得近时开错门"那个问题的根治办法：用户看得见系统的选择，
        而不是按下去才知道选错了。
      -->
      <div
        v-if="locked && doorTarget"
        class="pointer-events-none absolute inset-x-0 bottom-16 flex justify-center"
      >
        <div class="rounded-xl bg-wood-dark/80 px-3.5 py-2 text-center backdrop-blur-sm">
          <p class="text-[12px] font-semibold text-white">
            <kbd class="rounded bg-white/20 px-1.5 py-0.5 font-mono">F</kbd>
            {{ doorTarget.open ? '关 门' : '开 门' }}
            <span class="font-normal text-white/70">· 第 {{ doorTarget.index + 1 }} 扇</span>
          </p>
          <p class="mt-0.5 text-[10px]" :class="doorTarget.byAim ? 'text-accent-gold' : 'text-white/70'">
            <template v-if="doorTarget.byAim">准星已对准这扇门</template>
            <template v-else>
              准星没对着门，按的是最近的一扇（{{ doorTarget.dist.toFixed(1) }}m）——
              把中心的点对准想要的那扇可改为指定它
            </template>
          </p>
          <p class="mt-0.5 text-[10px] text-white/60">
            当前状态：{{ doorTarget.open ? '已打开' : '已关闭（会挡住去路）' }}
          </p>
        </div>
      </div>

      <!-- 开关门的瞬时反馈 -->
      <Transition
        enter-active-class="transition duration-150"
        enter-from-class="opacity-0 translate-y-1"
        leave-active-class="transition duration-200"
        leave-to-class="opacity-0"
      >
        <div
          v-if="doorToast"
          class="pointer-events-none absolute inset-x-0 bottom-32 flex justify-center"
        >
          <span class="rounded-full bg-botanical/90 px-3 py-1 text-[11px] font-medium text-white">
            {{ doorToast }}
          </span>
        </div>
      </Transition>

      <!-- 撞墙提示 -->
      <div
        v-if="bumping && mode === 'walk'"
        class="pointer-events-none absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full bg-wood-dark/70 px-3 py-1 text-[11px] text-white"
      >
        碰到墙了 —— 门开着才能过去
      </div>
    </div>

    <!-- ══ 后端判定的问题，照实说 ══ -->
    <!--
      ⚠️ 这些是**后端自己报出来的**问题，照实显示，不藏。
      但要加一句说明它们的**适用范围**：其中"走不到 N 间房"这类只在
      贴地行走模式下成立，而自由视角是穿墙的 ——
      不然用户会以为眼前这个能自由逛的模型"缺了几间房"。
      （需求方 2026-09-26 把自由视角定为默认之后，这个区别才有意义。）
    -->
    <div v-if="walkable?.issues.length" class="border-t border-warm-border bg-wood-light/30 px-4 py-2.5">
      <ul class="space-y-1">
        <li v-for="(x, i) in walkable.issues" :key="i" class="flex items-start gap-1.5 text-[11px] text-wood">
          <AppIcon name="info" :size="13" class="mt-0.5 shrink-0 text-accent-gold" />
          <span class="min-w-0">{{ x }}</span>
        </li>
      </ul>
      <p class="mt-1.5 text-[10px] leading-relaxed text-wood-muted">
        以上是<strong class="font-semibold">几何层面的实情</strong>，不影响自由视角浏览 ——
        「走不到某间房」只在贴地行走模式下成立，自由视角可以穿墙看全部房间。
      </p>
    </div>

    <!--
      ══ 房间快捷跳转 ══

      ⚠️ **可点性取决于当前模式，不是只看 `reachable`。**
      `reachable` 说的是"贴地行走模式下从出生点走得到吗"。而自由视角是
      穿墙的 —— 在自由视角下**每一间房都到得了**，包括那些"走不到"的。

      原来这里一律按 `reachable` 置灰加删除线，于是自由视角下的用户
      看着一排划掉的房间名，明明能去却点不动 —— 那不是保守，那是错的。
    -->
    <div v-if="walkable && !error && !webglFailed" class="border-t border-warm-border px-4 py-2.5">
      <p class="mb-1.5 text-[10px] uppercase tracking-wide text-wood-muted">
        快速前往
        <span v-if="mode === 'fly'" class="font-normal normal-case tracking-normal">
          （自由视角下每间房都能去）
        </span>
      </p>
      <div class="flex flex-wrap gap-1.5">
        <button
          v-for="r in walkable.rooms"
          :key="r.index"
          class="rounded-lg border px-2.5 py-1 text-[11px] transition"
          :class="jumpable(r)
            ? 'border-warm-border text-wood hover:bg-botanical-surface'
            : 'border-warm-border text-wood-muted/60 line-through'"
          :disabled="!jumpable(r)"
          :title="
            jumpable(r)
              ? `跳到${r.name}（${r.size_m[0]}×${r.size_m[1]}m）` +
                (r.reachable || mode === 'fly' ? '' : '')
              : `${r.name} 没有可通行的门 —— 切到自由视角就能进去`
          "
          @click="goToRoom(r.index)"
        >
          {{ r.name }}
        </button>
      </div>
    </div>

    <!-- ══ 性能与硬件诊断 ══ -->
    <div class="border-t border-warm-border bg-warm-sidebar px-4 py-2.5">
      <details class="group">
        <summary class="cursor-pointer list-none text-[11px] font-medium text-wood-muted hover:text-wood">
          性能与显卡信息
          <span v-if="gpu" class="ml-1 font-normal">
            {{ gpu.software ? '· 检测到软件渲染' : `· ${gpu.webglVersion === 2 ? 'WebGL 2' : 'WebGL 1'}` }}
          </span>
        </summary>
        <dl class="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[10px]">
          <dt class="text-wood-muted">实时帧率</dt>
          <dd class="num text-wood">{{ stats.fps.value }} fps（窗口均值 {{ stats.avgFps.value }}）</dd>
          <dt class="text-wood-muted">画质档位</dt>
          <dd class="text-wood">{{ stats.tier.value }} / 2 · 渲染倍率 {{ cappedPixelRatio(stats.tier.value).toFixed(2) }}</dd>
          <dt class="text-wood-muted">设备像素比</dt>
          <dd class="num text-wood">{{ devicePixelRatio.toFixed(2) }}</dd>
          <dt class="text-wood-muted">显卡</dt>
          <dd class="break-all text-wood">{{ gpu?.renderer || '未能读取' }}</dd>
          <dt class="text-wood-muted">门</dt>
          <dd class="text-wood">
            {{ handlesRef?.doors.length ?? 0 }} 扇 ·
            {{ (handlesRef?.doors ?? []).filter((d) => d.isOpen()).length }} 扇开着
            （默认全开，走到门口按 F 开关）
          </dd>
          <dt class="text-wood-muted">天花板</dt>
          <dd class="text-wood">
            {{ mode === 'walk' ? '显示' : '飞行模式自动隐藏' }}
            —— 飞行时被它挡住就看不到格局了
          </dd>
          <dt class="text-wood-muted">阴影</dt>
          <dd class="text-wood">关闭 —— {{ SHADOWS_OFF_REASON }}</dd>
        </dl>

        <!--
          操作键位表。**从 `three/keys.ts` 推出来，不手写。**
          手写的那份在加/改键时不会跟着变，界面就会教用户按一个不存在的键。
        -->
        <div class="mt-2.5">
          <p class="mb-1.5 text-[10px] text-wood-muted">
            操作（{{ mode === 'walk' ? '行走' : '自由视角' }}模式下可用）
          </p>
          <dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[10px]">
            <template v-for="b in keyChips" :key="b.code">
              <dt>
                <kbd class="tag !px-1.5 !text-[10px] font-mono">{{ b.cap }}</kbd>
              </dt>
              <dd class="text-wood">{{ b.help }}</dd>
            </template>
          </dl>
        </div>
        <!--
          视野调节。**做成可调的是有意的** —— "房间看起来多大"是主观感受，
          跟屏幕尺寸、坐姿、个人习惯都有关系，写死一个值总有人觉得不对。
          水平视场角：70–80° 接近人眼；更窄看得更"近"，更宽看得更"远"。
        -->
        <div class="mt-2.5">
          <label class="flex items-center gap-2 text-[10px] text-wood-muted">
            <span class="shrink-0">视野</span>
            <input
              v-model.number="hFov"
              type="range"
              :min="MIN_HFOV_DEG"
              :max="MAX_HFOV_DEG"
              step="1"
              class="h-1 flex-1 accent-botanical"
            />
            <span class="num w-16 shrink-0 text-right text-wood">
              {{ hFov }}° 水平
            </span>
          </label>
          <p class="mt-0.5 text-[10px] leading-relaxed text-wood-muted/80">
            调小 = 看得更近（房间显大）；调大 = 看得更广（房间显小）。
            {{ hFov > 88 ? '当前偏广角，房间会显得小。' : hFov < 66 ? '当前偏长焦，会有压迫感。' : '当前接近人眼观感。' }}
          </p>
        </div>

        <p v-if="stats.degradeNote.value" class="mt-2 text-[10px] text-accent-gold">
          ⚠️ {{ stats.degradeNote.value }}
        </p>
        <p v-if="mappingWarning" class="mt-2 text-[10px] leading-relaxed text-accent-red">
          ⚠️ {{ mappingWarning }}
        </p>
        <p v-else-if="mappingChecked" class="mt-2 text-[10px] text-botanical">
          ✓ 坐标映射自检通过：玩家在 3D 里的位置与平面图上的落点一致
        </p>
        <p v-if="gpu?.software" class="mt-2 text-[10px] leading-relaxed text-accent-red">
          检测到浏览器在用软件渲染（没有走显卡）。请到
          <span class="font-mono">edge://settings/system</span> 打开「使用图形加速」后重启浏览器，
          否则 3D 会明显卡顿。
        </p>
      </details>
    </div>
  </section>
</template>

<style scoped>
/* 小地图里的 SVG 缩到 132px 宽，热区层要关掉（点不了，还会挡住绿点） */
.plan-mini :deep(svg) {
  display: block;
  width: 100%;
  height: auto;
}
.plan-mini :deep(#hotspots) {
  display: none;
}
.plan-mini :deep(text) {
  display: none;
}
</style>
