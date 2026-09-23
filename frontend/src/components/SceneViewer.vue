<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import type * as THREE_NS from 'three'

import AppIcon from './AppIcon.vue'
import {
  layoutPlanSvg,
  layoutWalkable,
  messageOf,
} from '../api'
import type { WalkableData, WalkableResponse } from '../api'
import { cappedPixelRatio, readGpuInfo, useFrameStats, type GpuInfo } from '../composables/useFrameStats'
import type { CameraRig } from '../three/rig'
// ⚠️ 从 coords 引，不从 rig 引 —— rig 会静态拉进 three（570KB）
import { DEFAULT_HFOV_DEG, MAX_HFOV_DEG, MIN_HFOV_DEG } from '../three/coords'

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

const props = withDefaults(
  defineProps<{ layoutId: string; height?: string }>(),
  { height: '520px' },
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

/** 离玩家最近、且够近可以够得着的那扇门 */
const nearDoor = ref<{ index: number; open: boolean; dist: number } | null>(null)
/** 最近一次开关门的提示 */
const doorToast = ref('')
let doorToastAt = 0

const stats = useFrameStats()
/** 探测到的显卡。拿不到就是 null，不影响运行。 */
const gpu = shallowRef<GpuInfo | null>(null)
const webglFailed = ref('')

const walkable = computed<WalkableData | null>(() => data.value?.walkable ?? null)
/** 模板里要读门的状态，用一个浅引用桥接（handles 是普通变量，Vue 追踪不到） */
const handlesRef = shallowRef<SceneHandles | null>(null)
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

/** 够得着门的距离（米）。门宽 0.9m，站在门口一两步之内 */
const DOOR_REACH_M = 2.0

const keys = {
  forward: false, back: false, left: false, right: false,
  up: false, down: false, run: false,
}

// ══════════════════════════════════════════════════════════════════
// 加载与建场景
// ══════════════════════════════════════════════════════════════════

async function load() {
  if (!props.layoutId) return
  loading.value = true
  error.value = ''
  try {
    // 两个请求并行：3D 几何 + 2D 户型图（小地图底图）
    const [payload, svg] = await Promise.all([
      layoutWalkable(props.layoutId),
      layoutPlanSvg(props.layoutId).catch(() => ''),   // 小地图拿不到不该挡住 3D
    ])
    data.value = payload
    planSvg.value = svg
    mode.value = payload.walkable.mode
    loading.value = false
    await buildAfterPaint()
  } catch (e) {
    error.value = messageOf(e)
    loading.value = false
  }
}

watch(() => props.layoutId, load, { immediate: true })

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

  handles = sceneMod.buildScene(payload)
  handlesRef.value = handles
  handles.applyTier(stats.tier.value)

  const aspect = (canvasEl.value?.clientWidth || 16) / (canvasEl.value?.clientHeight || 9)
  rig = new rigMod.CameraRig(payload.walkable, aspect)
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

  // 门相关的提示与"够得着"判定，按 10Hz 更新就够了
  if (now - doorAt >= 100) {
    doorAt = now
    updateNearDoor()
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

const KEYMAP: Record<string, keyof typeof keys> = {
  KeyW: 'forward', ArrowUp: 'forward',
  KeyS: 'back', ArrowDown: 'back',
  KeyA: 'left', ArrowLeft: 'left',
  KeyD: 'right', ArrowRight: 'right',
  Space: 'up', KeyE: 'up',
  KeyC: 'down', KeyQ: 'down',
  ShiftLeft: 'run', ShiftRight: 'run',
}

function onKeyDown(e: KeyboardEvent) {
  if (!locked.value) return
  const k = KEYMAP[e.code]
  if (k) {
    keys[k] = true
    e.preventDefault()   // 方向键/空格会滚动页面
    return
  }
  if (e.code === 'KeyR') { rig?.respawn(); e.preventDefault(); return }
  // F = 开关门（游戏惯例）。**切换行走/自由视角改成 G** ——
  // 用户明确要的是"走到门口按 F 开门"，那就得把 F 让给门。
  if (e.code === 'KeyF') { toggleDoor(); e.preventDefault(); return }
  if (e.code === 'KeyG') { toggleMode(); e.preventDefault() }
}

function onKeyUp(e: KeyboardEvent) {
  const k = KEYMAP[e.code]
  if (k) {
    keys[k] = false
    e.preventDefault()
  }
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
    // 失去锁定时把所有按键状态清掉。不清的话，按住 W 时按 Esc，
    // 人会**一直往前走**直到撞墙 —— 看起来像失控。
    for (const k of Object.keys(keys) as (keyof typeof keys)[]) keys[k] = false
  }
}

function toggleMode() {
  if (!canWalk.value) return          // 后端判了不可行走，就不给切回来
  mode.value = mode.value === 'walk' ? 'fly' : 'walk'
  rig?.setMode(mode.value)
}

/**
 * 找玩家够得着的那扇门。
 *
 * 射线检测在这里是多余的 —— 门只有个位数，直接比距离就够，
 * 而且"隔着一堵墙也能按 F 开对面的门"这种问题，用距离判定天然不存在
 * （够得着的门一定在同一间房里）。
 */
function updateNearDoor() {
  const ds = handles?.doors ?? []
  if (!rig || !ds.length) {
    nearDoor.value = null
    return
  }
  const [px, py] = rig.planPosition()
  let best: { index: number; open: boolean; dist: number } | null = null
  for (const d of ds) {
    const dist = Math.hypot(d.planPos[0] - px, d.planPos[1] - py)
    if (dist > DOOR_REACH_M) continue
    if (!best || dist < best.dist) {
      best = { index: d.index, open: d.isOpen(), dist }
    }
  }
  nearDoor.value = best
}

/** 开关最近的那扇门。 */
function toggleDoor() {
  const near = nearDoor.value
  const ds = handles?.doors ?? []
  if (!near || !ds.length) return
  const d = ds.find((x) => x.index === near.index)
  if (!d) return
  const willOpen = !d.isOpen()
  d.setOpen(willOpen ? 1 : 0)
  syncDoorCollision()
  doorToast.value = willOpen ? '门已打开' : '门已关上'
  doorToastAt = 1400
  updateNearDoor()
}

/** 把关着的门变成碰撞线段交给 rig —— 否则关上门还能直接走过去。 */
function syncDoorCollision() {
  const segs = (handles?.doors ?? [])
    .map((d) => d.blockingSegment())
    .filter((s): s is [[number, number], [number, number]] => s !== null)
    .map((s) => ({ a: s[0], b: s[1] }))
  rig?.setExtraCollision(segs)
}

/** 跳到某个房间的中心。房间中心是后端算好的**净空中心**，一定站得住。 */
function goToRoom(index: number) {
  rig?.goToRoom(index)
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
  document.addEventListener('pointerlockchange', onLockChange)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeyDown)
  window.removeEventListener('keyup', onKeyUp)
  window.removeEventListener('mousemove', onMouseMove)
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

const controlHint = computed(() =>
  mode.value === 'walk'
    ? 'W A S D 行走 · 鼠标转头 · Shift 疾走 · F 开关门 · R 回起点 · G 切自由视角'
    : 'W A S D 平移 · Space/E 上升 · C/Q 下降 · 鼠标转头 · F 开关门 · R 回起点 · G 切回行走',
)
</script>

<template>
  <section class="overflow-hidden rounded-2xl border border-warm-border bg-white shadow-md">
    <header class="flex flex-wrap items-center justify-between gap-2 border-b border-warm-border px-4 py-3">
      <div class="min-w-0">
        <h3 class="flex items-center gap-1.5 text-[14px] font-bold text-wood-dark">
          <AppIcon name="cube" :size="15" class="text-botanical" />
          3D 户型漫游
        </h3>
        <p class="mt-0.5 text-[11px] text-wood-muted">
          <template v-if="walkable">
            {{ walkable.rooms.length }} 间房 · {{ walkable.doors.length }} 扇门 ·
            <span :class="canWalk ? 'text-botanical' : 'text-accent-gold'">
              {{ canWalk ? '贴地行走（有碰撞）' : '自由视角（后端判定不可行走）' }}
            </span>
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
          {{ mode === 'walk' ? '切自由视角 (G)' : '切回行走 (G)' }}
        </button>
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
        <p class="max-w-sm px-6 text-center text-[11px] leading-relaxed text-white/75">
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
        <div class="rounded-lg bg-white/85 px-2.5 py-1.5 backdrop-blur-sm">
          <p class="text-[10px] uppercase tracking-wide text-wood-muted">当前位置</p>
          <p class="text-[12px] font-bold text-wood-dark">
            {{ currentRoom || '走道 / 门洞' }}
          </p>
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

      <!-- 门提示：走到门口时出现 -->
      <div
        v-if="locked && nearDoor"
        class="pointer-events-none absolute inset-x-0 bottom-16 flex justify-center"
      >
        <div class="rounded-xl bg-wood-dark/80 px-3.5 py-2 text-center backdrop-blur-sm">
          <p class="text-[12px] font-semibold text-white">
            <kbd class="rounded bg-white/20 px-1.5 py-0.5 font-mono">F</kbd>
            {{ nearDoor.open ? '关 门' : '开 门' }}
          </p>
          <p class="mt-0.5 text-[10px] text-white/70">
            当前状态：{{ nearDoor.open ? '已打开' : '已关闭（会挡住去路）' }}
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
    <div v-if="walkable?.issues.length" class="border-t border-warm-border bg-wood-light/30 px-4 py-2.5">
      <ul class="space-y-1">
        <li v-for="(x, i) in walkable.issues" :key="i" class="flex items-start gap-1.5 text-[11px] text-wood">
          <AppIcon name="info" :size="13" class="mt-0.5 shrink-0 text-accent-gold" />
          <span class="min-w-0">{{ x }}</span>
        </li>
      </ul>
    </div>

    <!-- ══ 房间快捷跳转 ══ -->
    <div v-if="walkable && !error && !webglFailed" class="border-t border-warm-border px-4 py-2.5">
      <p class="mb-1.5 text-[10px] uppercase tracking-wide text-wood-muted">快速前往</p>
      <div class="flex flex-wrap gap-1.5">
        <button
          v-for="r in walkable.rooms"
          :key="r.index"
          class="rounded-lg border px-2.5 py-1 text-[11px] transition"
          :class="r.reachable
            ? 'border-warm-border text-wood hover:bg-botanical-surface'
            : 'border-warm-border text-wood-muted/60 line-through'"
          :disabled="!r.reachable"
          :title="r.reachable ? `跳到${r.name}（${r.size_m[0]}×${r.size_m[1]}m）` : '这间房没有可通行的门'"
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
          <dt class="text-wood-muted">阴影</dt>
          <dd class="text-wood">关闭 —— {{ SHADOWS_OFF_REASON }}</dd>
        </dl>
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
