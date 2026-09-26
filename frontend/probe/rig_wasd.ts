/**
 * 数值探针：**按 W 一定朝画面正前方走，按 A 一定朝画面左侧走。**
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么要有这个探针
 * ══════════════════════════════════════════════════════════════════
 * 2026-09-24 需求方反馈「wasd 出现混乱，按住 a 甚至可能是往右走」。
 *
 * 根因在 `rig.ts`：世界位移 → 图纸位移的负号（`planToEngine` 把图纸 y
 * 映射到 -z，所以要用 `-dz`）**只写在行走分支里**，自由视角分支直接
 * `py += dz`。于是自由视角下 WASD 全部沿图纸 y 轴反了。
 *
 * 这个 bug 有两处"为什么一直没被发现"：
 *
 *   ① **只在自由视角下错。** 切到行走就正常，所以看起来像按键随机失灵。
 *   ② **演示图的解析结果有一半判定为"不可行走"**（实测 6 次解析 3 次），
 *      也就是打开就是自由视角 —— 用户第一次进来看到的正好是错的那一半。
 *
 * 静态检查、类型检查、以及"按键表与提示文案一致"这类契约测试**都看不见它** ——
 * 它们的对象是代码文本，而这个 bug 只在"跑起来、动起来"之后才有表现。
 * 所以这里做**数值验证**：真的建一个 `CameraRig`，真的喂一帧输入，
 * 真的比较前后位置。
 *
 * ══════════════════════════════════════════════════════════════════
 * ⚠️ 判据不能用 rig 自己的公式（否则等于自己验自己）
 * ══════════════════════════════════════════════════════════════════
 * "往哪走对了"必须由**独立于被测代码**的量来回答。这里用
 * `THREE.Object3D.getWorldDirection()` 与 `camera.quaternion` ——
 * 它们由 three 自己从相机的矩阵算出来，`rig.ts` 一行都没参与。
 *
 * 于是不变式是：
 *     按 W 之后的位移方向 · 相机自己的前方向 > 0（且接近 1）
 * 镜像、符号、轴交换，任何一种错都会让这个点积变负。
 *
 * 只断言"位移非零"是不够的 —— 那对方向错误一无所知。
 *
 * 用法（工作目录 = `frontend/`）：
 *     node probe/run.mjs probe/rig_wasd.ts
 * 退出码 0 = 全部通过；1 = 有失败（明细以 JSON 打到 stdout）。
 */

import * as THREE from 'three'

import type { WalkableData } from '../src/api/types'
import type { Action } from '../src/three/keys'
import { CameraRig } from '../src/three/rig'

const DT = 1 / 60

/** 一份最小可用、可以自由走动的户型。碰撞线段只在远处，不干扰位移方向。 */
function makeData(mode: 'walk' | 'fly', yawDeg: number): WalkableData {
  return {
    ok: true,
    mode,
    spawn: { x: 5, y: 5, room: 0, yaw_deg: yawDeg },
    player_radius_m: 0.3,
    eye_height_m: 1.6,
    ceiling_height_m: 2.8,
    // 空碰撞：这一条探的是**方向**，不是碰撞。有碰撞反而会把位移
    // 滑掉一部分、让点积偏离 1，把方向判断搅浑。
    collision: [],
    rooms: [{
      index: 0, name: '客厅', kind: 'living_room',
      center: [5, 5], free_rect: [0, 0, 10, 10],
      size_m: [10, 10], reachable: true,
    }],
    doors: [],
    graph: [],
    issues: [],
    notes: [],
  } as unknown as WalkableData
}

const ZERO: Record<Action, boolean> = {
  forward: false, back: false, left: false, right: false,
  up: false, down: false, run: false,
}
const press = (...a: Action[]): Record<Action, boolean> => {
  const k = { ...ZERO }
  for (const x of a) k[x] = true
  return k
}

interface Row {
  mode: 'walk' | 'fly'
  heading_deg: number
  key: string
  /** 位移方向与"相机自己的前/右/上"的点积。1 = 完全一致，-1 = 完全相反。 */
  dot: number
  /** 位移长度（米）。0 表示按键没生效。 */
  moved: number
  ok: boolean
  note?: string
}

const rows: Row[] = []

/**
 * 跑一个用例：按住某个键一帧，比较"相机自己算出的方向"与"实际位移"。
 *
 * `axis` 是拿来做基准的相机本地轴：
 *   · 'forward' → 相机前方向（局部 -Z）
 *   · 'right'   → 相机右方向（局部 +X）
 *   · 'up'      → 世界上方
 */
function run(
  mode: 'walk' | 'fly', headingDeg: number, keys: Action[],
  label: string, axis: 'forward' | 'right' | 'up', expectSign: 1 | -1,
) {
  const rig = new CameraRig(makeData(mode, headingDeg), 16 / 9)
  const cam = rig.camera

  const before = cam.position.clone()
  rig.update(press(...keys), DT)
  // 相机矩阵在 syncCamera 之后要刷一次，否则 quaternion 是旧的
  cam.updateMatrixWorld(true)
  const delta = cam.position.clone().sub(before)

  const basis = new THREE.Vector3()
  if (axis === 'forward') cam.getWorldDirection(basis)
  else if (axis === 'right') basis.set(1, 0, 0).applyQuaternion(cam.quaternion)
  else basis.set(0, 1, 0)

  // ⚠️ **水平轴要先去掉 y 分量再比。**
  //
  // 移动是水平的（见 `rig.ts` 里 `update` 的说明：WASD 只负责水平面内
  // 移动，升降走 Space/C），而 `getWorldDirection()` 带着俯仰角。
  // 自由视角的默认俯角是 38°，不比的话点积恒等于 cos38° = 0.788 ——
  // 用例会在**代码完全正确**的情况下红掉。
  //
  // 这条是 2026-09-26 把自由视角改成"开局俯瞰"之后**探针自己先红**发现的：
  // 报的是 `[fly] 朝向 0° 按 W 前进：点积 0.788`，而 0.788 正好是 cos(38°)。
  // 也就是说**探针的判据当时是错的，不是代码错了**。
  // 垂直方向（'up'）本来就只有 y 分量，不动。
  if (axis !== 'up') basis.y = 0
  if (basis.lengthSq() > 1e-12) basis.normalize()

  const moved = delta.length()
  // 位移方向与基准轴同向 ⇒ 点积 ≈ +|Δ|；反向 ⇒ ≈ -|Δ|。
  // 归一化成"对齐度"，1 = 完全对齐，-1 = 完全相反。
  const dot = moved < 1e-9 ? 0 : delta.dot(basis) / moved

  const ok = moved > 1e-6 && dot * expectSign > 0.9
  rows.push({
    mode, heading_deg: headingDeg, key: label,
    dot: Number(dot.toFixed(4)), moved: Number(moved.toFixed(4)), ok,
    note: moved < 1e-9 ? '按键没有产生位移'
      : dot * expectSign <= 0.9 ? `方向与相机自身的「${axis}」不一致` : undefined,
  })
}

/**
 * 测多个朝向。**朝向是关键** —— 这个 bug 在 yaw=0 时恰好看不出来
 * （那时前后正好落在图纸 y 轴上、而多出来/少掉的负号在另一个分量上），
 * 转过 90° 才暴露。只测一个朝向的探针会漏掉它。
 */
const HEADINGS = [0, 37, 90, 137, 180, 233, 270, 315]

for (const mode of ['walk', 'fly'] as const) {
  for (const h of HEADINGS) {
    run(mode, h, ['forward'], 'W 前进', 'forward', 1)
    run(mode, h, ['back'], 'S 后退', 'forward', -1)
    run(mode, h, ['right'], 'D 右移', 'right', 1)
    run(mode, h, ['left'], 'A 左移', 'right', -1)
  }
}

// 上升/下降只在自由视角下有效；行走模式锁死眼高（这是设计，不是 bug）。
for (const h of [0, 90, 210]) {
  run('fly', h, ['up'], 'Space 上升', 'up', 1)
  run('fly', h, ['down'], 'C 下降', 'up', -1)
}

// 行走模式下按上升不该有任何垂直位移 —— 单独记一条，
// 免得以后有人"顺手"让它生效而没人发现。
for (const h of [0, 90]) {
  const rig = new CameraRig(makeData('walk', h), 16 / 9)
  const y0 = rig.camera.position.y
  rig.update(press('up'), DT)
  const dy = rig.camera.position.y - y0
  rows.push({
    mode: 'walk', heading_deg: h, key: 'Space 上升（行走模式应无效）',
    dot: Number(dy.toFixed(4)), moved: Number(Math.abs(dy).toFixed(4)),
    ok: Math.abs(dy) < 1e-9,
    note: Math.abs(dy) < 1e-9 ? undefined : '行走模式下眼高被改了',
  })
}

// ⚠️ **自由视角下 WASD 必须是纯水平位移**（垂直只由 Space/C 负责）。
//
// 这条不变量在 2026-09-26 之前没有用例守着，而它是有意选择的 ——
// 另一种做法（第一人称飞行：低头按 W 就往下钻）在这里是错的，
// 因为自由视角的默认姿态是**低头 38° 看格局**，那样一按 W 就往下栽。
// 见 `rig.ts` 里 `update` 的说明。
for (const h of [0, 90, 210]) {
  const rig = new CameraRig(makeData('fly', h), 16 / 9)
  // 先低头，模拟默认姿态 —— 不低头的话这条用例测不出"沿视线飞"这种错法
  rig.look(0, 300)
  // ⚠️ 确认"真的低头了"要看**视线方向**，不能看 `camera.position.y`：
  //    `look()` 只改 yaw/pitch，**不移动相机**（它不调 `syncCamera`）。
  //    第一版就是拿 y 前后比，于是恒相等 —— 用例报的是
  //    「look() 没有让相机低头」，而其实是这句自检写错了。
  const dir = new THREE.Vector3()
  rig.camera.getWorldDirection(dir)
  const looking = dir.y < -1e-6
  const y1 = rig.camera.position.y
  for (const k of ['forward', 'back', 'left', 'right'] as const) {
    rig.update(press(k), DT)
  }
  const dy = rig.camera.position.y - y1
  rows.push({
    mode: 'fly', heading_deg: h, key: 'WASD 只水平移动（低头后按 4 个方向）',
    dot: Number(dy.toFixed(6)), moved: Number(Math.abs(dy).toFixed(6)),
    ok: looking && Math.abs(dy) < 1e-9,
    note: !looking
      ? '自检失败：look() 之后视线不是朝下的，这条用例没测到东西'
      : Math.abs(dy) >= 1e-9
        ? '低头之后按 WASD 产生了垂直位移 —— 变成"沿视线飞"了'
        : undefined,
  })
}

const failed = rows.filter((r) => !r.ok)
console.log(JSON.stringify({
  total: rows.length,
  failed: failed.length,
  failures: failed,
  rows,
}, null, 2))

if (failed.length) {
  console.error(`\n[rig_wasd] ${failed.length}/${rows.length} 个用例失败`)
  process.exit(1)
}
console.error(`[rig_wasd] ${rows.length} 个用例全部通过`)
