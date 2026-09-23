import * as THREE from 'three'

import type { QualityTier } from '../composables/useFrameStats'
import type { WalkableResponse } from '../api'
import { planToEngine } from './coords'

/**
 * 从后端给的米制场景建 Three.js 场景。
 *
 * ══════════════════════════════════════════════════════════════════
 * 全部几何来自后端，前端不推导任何坐标
 * ══════════════════════════════════════════════════════════════════
 * 墙体用的是 `walkable.collision[].render_a/render_b` —— 也就是**门洞已经
 * 切开**的那份中心线（外延过墙角的那一套端点）。
 *
 * 这意味着 3D 里的门洞和碰撞几何**天生一致**：走过去过得去，看得见门。
 * 如果这里改用 `scene.walls` 自己切门洞，就会多出第二套坐标计算 ——
 * 而两套坐标只要有一处不一样，表现就是"门画在这儿、人得从旁边过"，
 * 属于最难查的那类问题。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么不用"房间轮廓挤出"
 * ══════════════════════════════════════════════════════════════════
 * 按房间 bbox 挤出一圈墙看起来更省事，但相邻房间的 bbox 会**重叠**
 * （隔墙两侧各算一次），挤出来两面墙贴在同一个平面上 → z-fighting，
 * 表现为墙面上出现闪烁的条纹。用碰撞线段建则每段墙只建一次。
 */

/** 墙体颜色。与前端设计系统的木色系同源，不用纯白纯灰。 */
const WALL_COLOR = 0xf2ece1
const FLOOR_COLOR = 0xe8dcc8
const CEILING_COLOR = 0xfaf7f1

/** 每个房间地面的抬升步长，用来避免相邻房间共面时的 z-fighting。 */
const FLOOR_STEP = 0.0015

export interface SceneHandles {
  scene: THREE.Scene
  /** 按画质档位重建可变部分（灯光） */
  applyTier(tier: QualityTier): void
  dispose(): void
  /** 包围盒（米），给相机远近裁剪面用 */
  bounds: { sizeX: number; sizeZ: number; height: number }
}

export function buildScene(data: WalkableResponse): SceneHandles {
  const scene = new THREE.Scene()
  scene.background = new THREE.Color(0xf7f4ee)

  const { scene: s, walkable: w } = data
  const height = w.ceiling_height_m
  const halfThick = Math.max(
    ...s.walls.map((x) => x.thickness_m),
    0.2,
  ) / 2

  const disposables: { dispose(): void }[] = []
  const track = <T extends { dispose(): void }>(x: T): T => {
    disposables.push(x)
    return x
  }

  // ══════════════════════════════════════════════════════════════
  // 墙体 —— 用门洞已切开的碰撞线段
  // ══════════════════════════════════════════════════════════════
  const wallMat = track(new THREE.MeshLambertMaterial({ color: WALL_COLOR }))
  const wallGroup = new THREE.Group()
  wallGroup.name = 'walls'

  for (const seg of w.collision) {
    const [x1, y1] = seg.render_a
    const [x2, y2] = seg.render_b
    const len = Math.hypot(x2 - x1, y2 - y1)
    if (len < 1e-4) continue

    const geo = track(new THREE.BoxGeometry(len, height, halfThick * 2))
    const mesh = new THREE.Mesh(geo, wallMat)
    const [ex, , ez] = planToEngine((x1 + x2) / 2, (y1 + y2) / 2, height / 2)
    mesh.position.set(ex, height / 2, ez)
    // 墙体沿长度方向摆放：图纸上的角度要取反（见 coords.ts）
    mesh.rotation.y = -Math.atan2(y2 - y1, x2 - x1)
    mesh.castShadow = true
    mesh.receiveShadow = true
    wallGroup.add(mesh)
  }
  scene.add(wallGroup)

  // ══════════════════════════════════════════════════════════════
  // 地面 / 天花板 —— 每间房一块
  // ══════════════════════════════════════════════════════════════
  const floorMat = track(new THREE.MeshLambertMaterial({
    color: FLOOR_COLOR, side: THREE.DoubleSide,
  }))
  const ceilMat = track(new THREE.MeshLambertMaterial({
    color: CEILING_COLOR, side: THREE.DoubleSide,
  }))

  w.rooms.forEach((room, i) => {
    const [x1, y1, x2, y2] = room.free_rect
    // 地面铺到墙中心线（不是净空），否则墙脚会露出一条缝看到外面
    const pad = w.player_radius_m
    const cx1 = x1 - pad, cy1 = y1 - pad, cx2 = x2 + pad, cy2 = y2 + pad
    const wdt = cx2 - cx1
    const dpt = cy2 - cy1
    if (wdt <= 0 || dpt <= 0) return

    const lift = i * FLOOR_STEP
    const geo = track(new THREE.PlaneGeometry(wdt, dpt))

    const floor = new THREE.Mesh(geo, floorMat)
    const [fx, , fz] = planToEngine((cx1 + cx2) / 2, (cy1 + cy2) / 2, 0)
    floor.position.set(fx, lift, fz)
    floor.rotation.x = -Math.PI / 2
    floor.receiveShadow = true
    scene.add(floor)

    const ceil = new THREE.Mesh(geo, ceilMat)
    ceil.position.set(fx, height - lift, fz)
    ceil.rotation.x = Math.PI / 2
    scene.add(ceil)
  })

  // ══════════════════════════════════════════════════════════════
  // 灯光
  // ══════════════════════════════════════════════════════════════
  // ⚠️ 有天花板就挡光，纯平行光会让室内一片黑。所以主要靠
  //    **每个房间一盏点光源**做室内照明，再补一点环境光把暗角托起来。
  //    点光源数量随画质档位变化 —— 这是降质时最有效的开关。
  const ambient = new THREE.AmbientLight(0xffffff, 0.55)
  const hemi = new THREE.HemisphereLight(0xfff6e6, 0xb8a98f, 0.5)
  scene.add(ambient, hemi)

  const roomLights: THREE.PointLight[] = []
  w.rooms.forEach((room) => {
    const [cx, cy] = room.center
    const [ex, , ez] = planToEngine(cx, cy, height - 0.35)
    const light = new THREE.PointLight(0xfff2dd, 0.55, Math.max(6, height * 3.2), 1.6)
    light.position.set(ex, height - 0.35, ez)
    scene.add(light)
    roomLights.push(light)
  })

  function applyTier(tier: QualityTier) {
    // 档位越高越省：0 全开，1 关一半灯，2 只留环境光
    roomLights.forEach((l, i) => {
      l.visible = tier === 0 ? true : tier === 1 ? i % 2 === 0 : false
    })
    ambient.intensity = tier === 2 ? 0.9 : 0.55
    hemi.intensity = tier === 2 ? 0.75 : 0.5
    wallGroup.visible = true
  }

  const sizeX = s.width_m
  const sizeZ = s.depth_m

  return {
    scene,
    applyTier,
    bounds: { sizeX, sizeZ, height },
    dispose() {
      for (const d of disposables) d.dispose()
      scene.clear()
    },
  }
}
