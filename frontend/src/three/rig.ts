import * as THREE from 'three'

import type { WalkableData } from '../api'
import type { Action } from './keys'
import {
  DEFAULT_HFOV_DEG,
  MAX_HFOV_DEG,
  MIN_HFOV_DEG,
  engineToPlan,
  headingToEngine,
  lookYawTowards,
  planToEngine,
  verticalFovDeg,
} from './coords'

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
 * ⚠️ 世界位移 → 图纸位移**只做一次**（这里踩过镜像级的坑）
 * ══════════════════════════════════════════════════════════════════
 * `planToEngine` 把图纸 y 映射到 **-z**（见 `coords.ts`），所以世界位移
 * `(dx, dz)` 对应图纸位移 `(dx, -dz)`。**那个负号是必须的。**
 *
 * 初版把这个换算只写在**行走**分支里，自由视角分支直接
 * `this.py += dz` —— 自由视角下 WASD 全部沿图纸 y 轴反了：按 W 后退、
 * 按 A 往右。而且**只在自由视角下错**，切到行走就正常，所以看起来
 * 像"按键随机失灵"。
 *
 * 藏得深的原因：自由视角是**穿墙**的，撞墙滑行那套反馈根本不会触发，
 * 没有"撞墙"这种外部信号能提示方向反了。而演示图的解析结果有
 * 一半判定为"不可行走"（实测 6 次解析 3 次），也就是**打开就是自由视角** ——
 * 用户第一次进来看到的正好是错的那一半。
 *
 * 现在的写法是：换算提在分支**之前**算一次，两个分支共用 `planDx/planDz`。
 * 两份实现迟早分叉，而这一处的分叉就是上面那个 bug。
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

/**
 * ══════════════════════════════════════════════════════════════════
 * 自由视角是**默认**视角，它的起点是"看格局"，不是"站在地上"
 * ══════════════════════════════════════════════════════════════════
 * 需求方 2026-09-26 定的：自由视角当默认。
 *
 * 起因是实测 —— 同一张演示图解析 6 次，只有 **3 次**判定"可贴地行走"
 * （可达率 33%~56%，从未超过 56%，见 `logs/walkable-distribution.json`）。
 * 也就是说"打开就是自由视角"是**常态而不是异常**，而原来的界面把它
 * 当成失败来措辞（"后端判定不可行走" + 一屏 issue），并且把相机放在
 * 房间里的 1.6m 高度、朝房间纵深看 —— **在那个机位上什么都看不出来**：
 * 需求方原话是"启动飞行模式就是为了在高处看格局"。
 *
 * 所以自由视角的起点改成：**升到天花板之上、俯角看下去**。
 * 第一帧就是整个户型，然后按 G 下到地面走。
 */

/** 自由视角起点高出天花板多少（米）。2.0m 能一眼收进整个户型。 */
const FLY_VANTAGE_ABOVE_CEILING_M = 2.0

/**
 * 起点高度的**下限**：至少要有这么多，才能把户型收进视野。
 *
 * ⚠️ **为什么不能只写"天花板 + 2 米"。** 后端会给"家具摆不下"的户型把
 *    3D 场景**等比放大**（实测演示户型 k=2.0，房子从 7.6×6.2 m 变成
 *    15.2×12.4 m，见 `services/furniture/scaling.py`）。而"天花板 + 2 米"
 *    是个**绝对高度**，不随房子长大 —— 房子大到 15×12 m 之后，7.6 m 高、
 *    38° 俯角的那条视线在 9.7 m 外就落地了，正好撞在远墙上：
 *    **打开 3D 看到的就是一面墙**，而不是需求方要的"一眼看格局"。
 *
 *    实测就是这个样子（scale=2 的户型，开局与按 R 回起点都是满屏墙面色）。
 *
 * 所以取两者的大：`max(天花板 + 2, 户型最长边 × 0.75)`。
 * 0.75 是让 38° 的视线在户型对角处还能落到地面 —— 系数偏大一点没关系，
 * 只是站得更高、看得更全，不会看不到东西。
 */
const FLY_VANTAGE_MIN_HEIGHT_FACTOR = 0.75

/**
 * 从 walkable 数据里量"户型最长边"（米），用于上面的下限。
 *
 * ⚠️ 量的是 `rooms[].free_rect` 的并集包围盒，**不是** `scene.width_m`：
 *    这个 rig 拿到的是 `/walkable` 里的 `walkable` 段，而 `scene` 段不在
 *    它的入参里（`WalkableData` 的类型就没有那个字段）。用房间框算，
 *    量到的是同一套（已被放大过的）坐标，且不依赖调用方多传东西。
 *    走廊这类没有房间的地方量不到，但那不影响"站得够高"这件事。
 */
function planSpanM(data: WalkableData): number {
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const r of data.rooms ?? []) {
    const f = r.free_rect
    if (!f) continue
    minX = Math.min(minX, f[0])
    minY = Math.min(minY, f[1])
    maxX = Math.max(maxX, f[2])
    maxY = Math.max(maxY, f[3])
  }
  if (!Number.isFinite(minX)) return 1
  return Math.max(maxX - minX, maxY - minY, 1)
}
/**
 * 自由视角起点的俯角（度，正数 = 往下看）。
 *
 * ⚠️ 不能取 0（平视）：那个机位在房间高度上平视，看到的只有墙，
 *    而这正是"被天花板挡住"的同一个毛病换了个方向。
 *    也不能取太陡（-80°）：那是正俯视，读不出层高和门洞，像看平面图。
 *    38° 兼顾"看得到整个格局"与"还看得出是三维的"。
 */
const FLY_VANTAGE_PITCH_DEG = 38

/**
 * 每帧喂给 `update` 的按键状态。
 *
 * ⚠️ 字段名与 `keys.ts` 的 `Action` 是同一个联合类型 —— **按键表是唯一
 * 的定义处**（见 `keys.ts` 文件头）。这里写死一份字段名的话，
 * 加一个动作就要改两处，漏一处是 `keys[action]` 恒为 undefined 的静默失败。
 */
export type RigInput = Record<Action, boolean>

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
  /**
   * **额外的**碰撞线段（图纸平面坐标）。目前只有一种来源：**关着的门**。
   *
   * 墙的碰撞段是静态的（来自后端），而门的开合是运行期状态 ——
   * 关上门就该挡住人，否则"关门"只是个视觉动作，走过去直接穿帮。
   */
  private extra: { a: [number, number]; b: [number, number] }[] = []

  /** 当前的水平视场角（度）。用户可调 —— 见 HUD 的视野控制。 */
  private hFov = DEFAULT_HFOV_DEG

  /**
   * @param startMode 开局用哪个模式。**不给就沿用 `data.mode`**
   *        （后端判定"能不能贴地行走"）。
   *
   * ⚠️ **调用方应当传它，别依赖默认。** 2026-09-26 需求方把自由视角定为
   *    默认视角，而"默认用哪个"是**界面层的决定**，不该由后端那句
   *    "这个户型的门洞够不够走路"来推。不传的后果实测过一次：
   *    UI 上是自由视角、`rig` 里却 `colliding = true`（走在碰撞里、
   *    停在眼高平视），两处对同一个状态各说各话 —— 而且**第一下按 G
   *    不会切模式**（UI 从 fly 翻到 walk、rig 本来就在 walk）。
   */
  constructor(data: WalkableData, aspect: number, startMode?: 'walk' | 'fly') {
    this.data = data
    this.camera = new THREE.PerspectiveCamera(
      verticalFovDeg(this.hFov, aspect), aspect, 0.05, 200,
    )

    this.px = data.spawn.x
    this.py = data.spawn.y
    this.floorY = data.eye_height_m
    this.colliding = (startMode ?? data.mode) === 'walk'
    this.yaw = this.yawFromSpawn(data.spawn.yaw_deg)
    // 起点按模式分：行走是站在地上，自由视角是**升到天花板之上俯视**。
    // `respawn()` 走同一套，保证按 R 回到的是"同一个起点"。
    this.applyStartPose()
    this.syncCamera()
  }

  /**
   * 把相机摆到当前模式的**起点**。构造函数与 `respawn()` 共用。
   *
   * 自由视角的起点是"看格局"（见文件头 `FLY_VANTAGE_*` 的说明）；
   * 行走的起点是出生点、平视、眼高锁死。
   */
  private applyStartPose() {
    if (this.colliding) {
      this.flyHeight = this.floorY
      this.pitch = 0
      return
    }
    this.flyHeight = Math.max(
      this.data.ceiling_height_m + FLY_VANTAGE_ABOVE_CEILING_M,
      planSpanM(this.data) * FLY_VANTAGE_MIN_HEIGHT_FACTOR,
    )
    this.pitch = -(FLY_VANTAGE_PITCH_DEG * Math.PI) / 180
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

  /**
   * 画布尺寸变了。**必须同时重算垂直 FOV** ——
   * 只改 `aspect` 的话，画面一变形，视野的宽窄也跟着变。
   */
  setAspect(aspect: number) {
    this.camera.aspect = aspect
    this.camera.fov = verticalFovDeg(this.hFov, aspect)
    this.camera.updateProjectionMatrix()
  }

  /** 设置水平视场角（度）。用户偏好，调用方负责持久化。 */
  setHorizontalFov(deg: number) {
    this.hFov = Math.min(MAX_HFOV_DEG, Math.max(MIN_HFOV_DEG, deg))
    this.camera.fov = verticalFovDeg(this.hFov, this.camera.aspect)
    this.camera.updateProjectionMatrix()
  }

  get horizontalFov(): number {
    return this.hFov
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
    this.yaw = this.yawFromSpawn(this.data.spawn.yaw_deg)
    // ⚠️ 起点姿态按**当前模式**算（`applyStartPose`），不能一律 `pitch = 0`：
    //    自由视角下平视等于回到"看不到格局"的那个机位，
    //    而按 R 的语义是"回到起点"，不是"回到一个我看不懂的地方"。
    this.applyStartPose()
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

  /**
   * 传送到某个房间的中心，并把视线朝向房间纵深方向。
   *
   * ⚠️ **自由视角下要同时把高度降到眼高。**
   *    不然会从"天花板之上俯瞰"的那个机位瞬移到房间中心、
   *    却仍停在空中平视 —— 看到的只有屋面和空气，
   *    而用户按的是"快速前往 主卧"，他期待的是**站在主卧里**。
   */
  goToRoom(roomIndex: number): boolean {
    const room = this.data.rooms.find((r) => r.index === roomIndex)
    if (!room) return false
    this.moveTo(room.center[0], room.center[1])
    if (!this.colliding) this.flyHeight = this.data.eye_height_m
    this.pitch = 0
    this.syncCamera()
    return true
  }

  /**
   * 走一步。`dt` 秒。
   *
   * 返回是否发生了碰撞（给 HUD 显示"贴墙"提示用）。
   */
  update(input: RigInput, dt: number): boolean {
    // 把"前后左右"从屏幕空间转到世界空间：前 = 视线方向**在水平面上的投影**
    //
    // ══════════════════════════════════════════════════════════════
    // ⚠️ "只取水平投影"是**有意的**，两种模式都这样
    // ══════════════════════════════════════════════════════════════
    // 另一种常见做法是第一人称飞行的"沿视线飞"：低头按 W 就往下钻。
    // 这里**不那样**，理由是自由视角的用途被定成"在高处看格局"：
    //   · 低头看户型图是默认姿态（俯角 38°，见 `FLY_VANTAGE_PITCH_DEG`），
    //     沿视线飞的话一按 W 就往下栽，想横着挪一下都做不到
    //   · 升/降有专门的键（Space/E 与 C/Q），W A S D 只负责水平面内移动
    //   · 两种模式的 WASD 语义因此**完全一致** —— 切模式不用重新学
    //
    // 这条不变量由 `probe/rig_wasd.ts` 钉着（它会先去掉基准向量的 y 分量
    // 再比方向，并单独断言"没按升降键时垂直位移为 0"）。
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
    const dx = mx * speed * dt
    const dz = mz * speed * dt

    // ── 世界位移 → 图纸位移。**只在这里做一次**，两个分支共用（见文件头）──
    //    dx 是 Three 的世界 x，与图纸 x 同向；dz 是世界 z，图纸 y = -世界 z。
    const planDx = dx
    const planDz = dz

    if (!this.colliding) {
      // 垂直移动只在自由视角下有效 —— 第一人称是"贴地走"，锁死眼高
      let dy = 0
      if (input.up) dy += FLY_SPEED * dt
      if (input.down) dy -= FLY_SPEED * dt
      this.flyHeight = Math.max(-2, Math.min(this.data.ceiling_height_m + 6, this.flyHeight + dy))

      // 自由视角：不做任何碰撞判定，直接位移
      this.px += planDx
      this.py += -planDz
      this.syncCamera()
      return false
    }

    // ── 行走：子步进 + 分轴滑行 ──
    //
    // ⚠️ **图纸 y 与世界 z 反号**：`coords.ts` 的 `planToEngine` 是 `[x, h, -y]`。
    //    所以世界位移要先换成**图纸位移**再往 `px/py` 上累加。
    //
    //    这里原来漏了那个负号（上面自由视角分支写对了）—— 后果是行走模式下
    //    按 D 往左走、按 W 斜着走，而**自由视角一切正常**，所以肉眼很难发现。
    //    `probe/rig_wasd.ts` 把朝向扫了 8 档才钉住它：朝向 37° 时前进方向
    //    与相机自身朝向差 2×37°，点积恰是 cos74° = 0.2756。
    const pdx = planDx
    const pdy = -planDz
    const steps = Math.max(1, Math.ceil(Math.hypot(pdx, pdy) / MAX_SUBSTEP_M))
    const sdx = pdx / steps
    const sdz = pdy / steps

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

  /** 更新"关着的门"带来的碰撞。调用方在每次开关门后调一次。 */
  setExtraCollision(segs: { a: [number, number]; b: [number, number] }[]) {
    this.extra = segs
  }

  /** 该位置站得下吗（离所有墙都超过玩家半径）。 */
  private free(x: number, y: number): boolean {
    const r = this.data.player_radius_m
    for (const seg of this.data.collision) {
      const [ax, ay] = seg.a
      const [bx, by] = seg.b
      if (distToSeg(x, y, ax, ay, bx, by) < r) return false
    }
    for (const seg of this.extra) {
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
