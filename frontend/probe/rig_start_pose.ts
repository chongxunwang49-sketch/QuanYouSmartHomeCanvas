/**
 * 数值探针：**自由视角的起点是"看格局"**。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么要有它
 * ══════════════════════════════════════════════════════════════════
 * 2026-09-26 需求方定了「自由视角当默认」，并指出原因：
 * 「启动飞行模式就是为了在高处看格局，如果被天花板挡住了就没有任何意义」。
 *
 * 这条要求落在代码上就是**相机的初始位姿**，而它极易被静默改掉：
 * 构造函数里 `flyHeight` 和 `pitch` 就是两个数，谁把 `pitch` 改回 0、
 * 或者把 `flyHeight` 改回眼高，**编译、类型、其它测试全都不会响** ——
 * 表现只是"打开之后看到的是一面墙"，而这种"看着别扭但不报错"的回归
 * 最容易活下来。
 *
 * 所以这里把起点姿态写成可断言的量：
 *   · 自由视角：**高于天花板**，且视线**朝下**
 *   · 行走：    眼高，平视
 *   · `respawn()` 按**当前模式**回到各自的起点
 *   · `goToRoom()` 在自由视角下把高度降到眼高（不然是"在空中看房间"）
 *
 * ⚠️ 判据用 `camera.getWorldDirection()`（three 自己从相机矩阵算），
 *    不用 `rig.ts` 里那几行三角函数 —— 用后者等于自己验自己。
 */

import * as THREE from 'three'

import type { WalkableData } from '../src/api/types'
import type { Action } from '../src/three/keys'
import { CameraRig } from '../src/three/rig'

const CEILING = 2.8
const EYE = 1.6

function makeData(mode: 'walk' | 'fly'): WalkableData {
  return {
    ok: mode === 'walk',
    mode,
    spawn: { x: 5, y: 5, room: 0, yaw_deg: 0 },
    player_radius_m: 0.3,
    eye_height_m: EYE,
    ceiling_height_m: CEILING,
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

interface Row {
  case: string
  detail: string
  ok: boolean
  note?: string
}

const rows: Row[] = []
/**
 * ⚠️ **`note` 只在失败时带上。**
 * 它是"错了说明什么"，通过时还挂在报告里的话，读的人会以为出了问题
 * —— 第一版就是这样：9 条全过，报告里却躺着
 * 「按 R 之后没回到俯瞰位」和「切模式把人弹到屋顶之上会很突兀」两句，
 * 而断言用的 `ok` 全是 true。**报告本身也不能说谎。**
 */
const push = (c: string, detail: string, ok: boolean, note?: string) =>
  rows.push(ok ? { case: c, detail, ok } : { case: c, detail, ok, note })

/** 视线方向与水平面的夹角（度，正数 = 朝下看）。 */
function pitchDeg(rig: CameraRig): number {
  const d = new THREE.Vector3()
  rig.camera.getWorldDirection(d)
  return (Math.asin(-d.y) * 180) / Math.PI
}

// ── ① 自由视角的起点 ──
{
  const rig = new CameraRig(makeData('fly'), 16 / 9)
  const y = rig.camera.position.y
  const p = pitchDeg(rig)
  push('自由视角起点高于天花板', `y=${y.toFixed(2)} 天花板=${CEILING}`, y > CEILING + 0.5,
       y > CEILING + 0.5 ? undefined : '起点没升起来 —— 会被天花板挡住')
  push('自由视角起点朝下看', `俯角 ${p.toFixed(1)}°`, p > 15 && p < 75,
       p > 15 && p < 75 ? undefined
         : p <= 15 ? '几乎是平视，看不到格局' : '太陡了，看不出是三维的')
}

// ── ② 行走的起点 ──
{
  const rig = new CameraRig(makeData('walk'), 16 / 9)
  const y = rig.camera.position.y
  const p = pitchDeg(rig)
  push('行走起点是眼高', `y=${y.toFixed(2)} 眼高=${EYE}`, Math.abs(y - EYE) < 1e-9)
  push('行走起点平视', `俯角 ${p.toFixed(1)}°`, Math.abs(p) < 1e-6,
       Math.abs(p) < 1e-6 ? undefined : '行走起点不该低头/抬头')
}

// ── ③ respawn 按模式回到各自起点 ──
{
  const rig = new CameraRig(makeData('fly'), 16 / 9)
  rig.update({ ...ZERO, up: true }, 0.5)          // 先飞走
  rig.look(400, 200)                              // 再乱看一下
  rig.respawn()
  const y = rig.camera.position.y
  const p = pitchDeg(rig)
  push('自由视角 respawn 回到俯瞰位', `y=${y.toFixed(2)} 俯角 ${p.toFixed(1)}°`,
       y > CEILING && p > 15,
       '按 R 之后没回到俯瞰位 —— 那 R 的语义就不是"回到起点"了')

  const w = new CameraRig(makeData('walk'), 16 / 9)
  w.look(400, 200)
  w.respawn()
  push('行走 respawn 回到眼高平视',
       `y=${w.camera.position.y.toFixed(2)} 俯角 ${pitchDeg(w).toFixed(1)}°`,
       Math.abs(w.camera.position.y - EYE) < 1e-9 && Math.abs(pitchDeg(w)) < 1e-6)
}

// ── ④ goToRoom：自由视角下降到眼高 ──
{
  const rig = new CameraRig(makeData('fly'), 16 / 9)
  rig.goToRoom(0)
  const y = rig.camera.position.y
  push('自由视角跳到房间会降到眼高', `y=${y.toFixed(2)}`, Math.abs(y - EYE) < 1e-9,
       Math.abs(y - EYE) < 1e-9 ? undefined
         : '按"快速前往 主卧"却停在空中 —— 看到的只有屋面')
}

// ── ④′ 起始模式由调用方指定，不是由 walkable.mode 推 ──
{
  // 一个"后端判定可行走"的户型，但调用方要求开局用自由视角
  const rig = new CameraRig(makeData('walk'), 16 / 9, 'fly')
  const y = rig.camera.position.y
  const p = pitchDeg(rig)
  push('startMode 覆盖 walkable.mode', `walkable=walk + startMode=fly → y=${y.toFixed(2)} 俯角 ${p.toFixed(1)}°`,
       y > CEILING && p > 15,
       '起始模式没被 startMode 覆盖 —— 于是"可走路的户型开局是行走、'
       + '不可走路的才是自由视角"，而需求要的是开局一致')

  // 反向：不可行走的户型被要求开局行走（理论上调用方不会这么做，
  // 但接口不应当因此错乱 —— 它就该照做）
  const rig2 = new CameraRig(makeData('fly'), 16 / 9, 'walk')
  push('startMode 可以反向覆盖', `walkable=fly + startMode=walk → y=${rig2.camera.position.y.toFixed(2)}`,
       Math.abs(rig2.camera.position.y - EYE) < 1e-9)
}

// ── ⑤ 切模式：切回自由视角不瞬移 ──
{
  const rig = new CameraRig(makeData('walk'), 16 / 9)
  const before = rig.camera.position.y
  rig.setMode('fly')
  const after = rig.camera.position.y
  push('行走→自由视角保持高度（不瞬移）',
       `${before.toFixed(2)} → ${after.toFixed(2)}`,
       Math.abs(before - after) < 1e-9,
       '切模式把人弹到屋顶之上会很突兀；要俯瞰应当按 R')
  rig.setMode('walk')
  const back = rig.camera.position.y
  push('自由视角→行走落回眼高', `${after.toFixed(2)} → ${back.toFixed(2)}`,
       Math.abs(back - EYE) < 1e-9)
}

const failed = rows.filter((r) => !r.ok)
console.log(JSON.stringify({ total: rows.length, failed: failed.length,
                             failures: failed, rows }, null, 2))
if (failed.length) {
  console.error(`\n[rig_start_pose] ${failed.length}/${rows.length} 个用例失败`)
  process.exit(1)
}
console.error(`[rig_start_pose] ${rows.length} 个用例全部通过`)
