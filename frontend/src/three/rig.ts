import * as THREE from 'three'

import type { WalkableData } from '../api'
import { engineToPlan, headingToEngine, lookYawTowards, planToEngine } from './coords'

/**
 * 第一人称相机：**贴地行走（有碰撞）** 与 **自由视角（无碰撞）** 两种模式。
 *
 * ══════════════════════════════════════════════════════════════════
 * 碰撞只做一件事：点到线段的距离
 * ══════════════════════════════════════════════════════════════════
 * 碰撞体是后端给的 `collision[].a/b`（**门洞已经切好的**中心线），
 * 判定是"人的圆心到最近墙线段 < 玩家半径就撞墙"。没有物理引擎、
 * 没有包围盒树 —— 这个户型的碰撞线段只有个位数，暴力遍历比建树快。
 *
 * ══════════════════════════════════════════════════════════════════
 * 撞墙之后**沿墙滑**，不是停住
 * ══════════════════════════════════════════════════════════════════
 * 直接拦住的话，斜着走向墙面的体验是"整个人卡死"，非常难受 ——
 * 玩家会以为自己卡进几何里了。这里用**分轴尝试**：先试整体位移，
 * 不行就只走 X、再不行只走 Z。效果是贴着墙滑行，实现只有几行。
 *
 * ══════════════════════════════════════════════════════════════════
 * 子步进：防止穿墙
 * ══════════════════════════════════════════════════════════════════
 * 一帧的位移必须切成小步走。虽然按 60fps、2.5m/s 算单帧只有 4cm，
 * 远小于墙厚（20cm），但**帧率掉到 5fps 时单帧就是 50cm** —— 正好穿过
 * 一堵墙。而掉帧恰恰是低配机器上最容易发生的事，也就是最需要碰撞
 * 生效的时候。所以按"每步不超过 5cm"切分。
 */

/** 步行速度（米/秒）。成年人正常步速约 1.4m/s，这里给快一点，演示更顺。 */
const WALK_SPEED = 2.4
/** 按 Shift 疾走 */
const RUN_MULTIPLIER = 2.0
/** 自由视角的飞行速度 */
const FLY_SPEED = 4.0
/** 单次子步的最大位移（米）。见文件头"子步进"。 */
const MAX_SUBSTEP_M = 0.05
/** 鼠标灵敏度（弧度/像素） */
const LOOK_SENSITIVITY = 0.0022
/** 俯仰角上下限。±85° 而不是 ±90° —— 到 90° 时 up 向量与视线共线，画面会翻滚。 */
const PITCH_LIMIT = (85 * Math.PI) / 180

export interface RigInput {
  forward: boolean
  back: boolean
  left: boolean
  right: boolean
  up: boolean
  down: boolean
  run: boolean
}

export class CameraRig {
  readonly camera: THREE.PerspectiveCamera
  private readonly data: WalkableData
  private yaw = 0
  private pitch = 0
  /** 当前所在位置（**图纸平面坐标**，米）。碰撞全在这个坐标系里算。 */
  private px = 0
  private py = 0
  private floorY = 0
  /** 自由视角模式下的高度（图纸坐标的"高度"是独立的一个量） */
  private flyHeight = 1.6
  private colliding = true

  constructor(data: WalkableData, aspect: number) {
    this.data = data
    this.camera = new THREE.PerspectiveCamera(68, aspect, 0.05, 200)

    this.px = data.spawn.x
    this.py = data.spawn.y
    this.floorY = data.eye_height_m
    this.flyHeight = data.eye_height_m
    this.colliding = data.mode === 'walk'
    this.yaw = this.yawFromSpawn(data.spawn.yaw_deg)
    this.syncCamera()
  }

  /** 出生朝向：图纸的 yaw → Three 的 yaw。 */
  private yawFromSpawn(yawDeg: number): number {
    const [dx, dz] = headingToEngine(yawDeg)
    return lookYawTowards(dx, dz)
  }

  get mode(): 'walk' | 'fly' {
    return this.colliding ? 'walk' : 'fly'
  }

  setMode(mode: 'walk' | 'fly') {
    this.colliding = mode === 'walk'
    if (mode === 'walk') this.flyHeight = this.floorY
  }

  /** 鼠标移动 → 视角。`PointerLockControls` 的手感：右移视线右转。 */
  look(dxPx: number, dyPx: number) {
    this.yaw -= dxPx * LOOK_SENSITIVITY
    this.pitch = Math.max(
      -PITCH_LIMIT,
      Math.min(PITCH_LIMIT, this.pitch - dyPx * LOOK_SENSITIVITY),
    )
  }

  /** 回到出生点。演示时按 R 用，比"退出重进"体面得多。 */
  respawn() {
    this.px = this.data.spawn.x
    this.py = this.data.spawn.y
    this.flyHeight = this.data.eye_height_m
    this.yaw = this.yawFromSpawn(this.data.spawn.yaw_deg)
    this.pitch = 0
    this.syncCamera()
  }

  /**
   * 瞬移到图纸上的某个位置（米）。房间快捷跳转用。
   *
   * 目标点**必须是可站立的** —— 调用方（HUD）传的是后端算好的房间净空中心，
   * 那里一定站得住。扔到墙里的话下一帧就会被碰撞判定困住，表现为"卡住"。
   */
  moveTo(x: number, y: number) {
    this.px = x
    this.py = y
    this.syncCamera()
  }

  /** 传送到某个房间的中心，并把视线朝向房间纵深方向。 */
  goToRoom(roomIndex: number): boolean {
    const room = this.data.rooms.find((r) => r.index === roomIndex)
    if (!room) return false
    this.moveTo(room.center[0], room.center[1])
    this.pitch = 0
    return true
  }

  /**
   * 走一步。`dt` 秒。
   *
   * 返回是否发生了碰撞（给 HUD 显示"贴墙"提示用）。
   */
  update(input: RigInput, dt: number): boolean {
    // 把"前后左右"从屏幕空间转到世界空间：前 = 视线方向在水平面上的投影
    let fx = -Math.sin(this.yaw)
    let fz = -Math.cos(this.yaw)
    // 右 = 前的右手侧（水平面内顺时针 90°）
    let rx = -fz
    let rz = fx

    let mx = 0
    let mz = 0
    if (input.forward) { mx += fx; mz += fz }
    if (input.back) { mx -= fx; mz -= fz }
    if (input.right) { mx += rx; mz += rz }
    if (input.left) { mx -= rx; mz -= rz }

    const mag = Math.hypot(mx, mz)
    if (mag > 1e-6) {
      mx /= mag
      mz /= mag
    }

    const speed = (this.colliding ? WALK_SPEED : FLY_SPEED) *
      (input.run ? RUN_MULTIPLIER : 1)
    let dx = mx * speed * dt
    let dz = mz * speed * dt

    // 垂直移动只在自由视角下有效 —— 第一人称是"贴地走"，锁死眼高
    if (!this.colliding) {
      let dy = 0
      if (input.up) dy += FLY_SPEED * dt
      if (input.down) dy -= FLY_SPEED * dt
      this.flyHeight = Math.max(-2, Math.min(this.data.ceiling_height_m + 6, this.flyHeight + dy))
    }

    if (!this.colliding) {
      // 自由视角：不做任何碰撞判定，直接位移
      this.px += dx
      this.py += dz
      this.syncCamera()
      return false
    }

    // ── 行走：子步进 + 分轴滑行 ──
    // ⚠️ 注意这里的 x/z 是 **Three 的世界轴**，而碰撞数据是**图纸平面**的。
    //    图纸 y 映射到 Three 的 -z，所以世界位移 (dx, dz) 对应图纸位移 (dx, -dz)。
    const planDx = dx
    const planDz = -dz
    const steps = Math.max(1, Math.ceil(Math.hypot(planDx, planDz) / MAX_SUBSTEP_M))
    const sdx = planDx / steps
    const sdz = planDz / steps

    let hit = false
    for (let i = 0; i < steps; i++) {
      if (this.free(this.px + sdx, this.py + sdz)) {
        this.px += sdx
        this.py += sdz
        continue
      }
      hit = true
      // 分轴尝试：沿墙滑行
      if (this.free(this.px + sdx, this.py)) {
        this.px += sdx
      } else if (this.free(this.px, this.py + sdz)) {
        this.py += sdz
      }
      // 两轴都不行 → 这一子步完全挡住，原地不动
    }

    this.syncCamera()
    return hit
  }

  /** 该位置站得下吗（离所有墙都超过玩家半径）。 */
  private free(x: number, y: number): boolean {
    const r = this.data.player_radius_m
    for (const seg of this.data.collision) {
      const [ax, ay] = seg.a
      const [bx, by] = seg.b
      if (distToSeg(x, y, ax, ay, bx, by) < r) return false
    }
    return true
  }

  private syncCamera() {
    const h = this.colliding ? this.floorY : this.flyHeight
    const [ex, , ez] = planToEngine(this.px, this.py, h)
    this.camera.position.set(ex, h, ez)
    this.camera.rotation.order = 'YXZ'
    this.camera.rotation.y = this.yaw
    this.camera.rotation.x = this.pitch
    this.camera.rotation.z = 0
  }

  /** 玩家当前在图纸平面上的位置（米）。HUD 的小地图要用。 */
  planPosition(): [number, number] {
    return [this.px, this.py]
  }

  /** 当前所在房间的下标（不在任何房间时返回 -1）。 */
  currentRoomIndex(): number {
    for (const room of this.data.rooms) {
      const [x1, y1, x2, y2] = room.free_rect
      const pad = this.data.player_radius_m
      if (
        x1 - pad <= this.px && this.px <= x2 + pad &&
        y1 - pad <= this.py && this.py <= y2 + pad
      ) {
        return room.index
      }
    }
    return -1
  }

  /** 当前所在房间名（未知返回空串）。HUD 显示"你在客厅"用。 */
  currentRoom(): string {
    const i = this.currentRoomIndex()
    return i < 0 ? '' : (this.data.rooms.find((r) => r.index === i)?.name ?? '')
  }
}

/** 点到线段的距离。碰撞判定的全部内容。 */
export function distToSeg(
  px: number, py: number,
  ax: number, ay: number,
  bx: number, by: number,
): number {
  const dx = bx - ax
  const dy = by - ay
  if (dx === 0 && dy === 0) return Math.hypot(px - ax, py - ay)
  let t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
  t = Math.max(0, Math.min(1, t))
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy))
}

export { engineToPlan }
