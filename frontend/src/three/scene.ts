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

/** 门洞高度（米）。国内住宅门洞常见 2.0–2.1m。 */
const DOOR_HEIGHT_M = 2.05
/** 门扇厚度（米）。 */
const LEAF_THICK_M = 0.045
const DOOR_COLOR = 0x8a6a4b

/**
 * 门扇的转轴与开合。
 *
 * ══════════════════════════════════════════════════════════════════
 * 几何全部来自后端，这里只负责"摆"和"转"
 * ══════════════════════════════════════════════════════════════════
 * `hinge`（转轴）、`along`（门扇边长方向）、`normal`（全开时朝的方向）
 * 都是后端算好的 —— 见 `walkable.py` 里 `DoorEdge` 的说明。
 * 前端自己从"墙 + 偏移"再推一遍的话，两套只要差一点，
 * 门扇就会挂偏半个门宽或者转轴埋进墙里。
 *
 * 转向的算法：门组绕自身 Y 轴转 θ 时，局部 +X 会转到
 * `(cosθ, 0, -sinθ)`。让 θ 从 0 转到 +90°，正好把"沿墙"转到"垂直于墙"，
 * 也就是从关到开。这个符号是推出来的，不是试出来的。
 */
/**
 * 让一个"长度沿局部 +X"的盒子对齐到给定的**图纸平面**方向。
 *
 * ⚠️ 符号是推出来的：盒子绕自身 Y 轴转 θ 时，局部 +X 会转到
 * `(cosθ, 0, -sinθ)`；而图纸方向 `(ax, ay)` 在 Three 里是 `(ax, -ay)`。
 * 于是 `cosθ = ax`、`sinθ = ay`，即 `θ = atan2(ay, ax)`。
 *
 * 写反了（`-atan2`）对**对称的墙盒**看不出来 —— ±90° 转出来的形状一样。
 * 但对**门扇**是致命的：门扇的质心偏在 +X 一侧、把手也在那一端，
 * 转反了门就挂到门洞外面去了。
 */
function yawAlong(along: [number, number]): number {
  return Math.atan2(along[1], along[0])
}

export interface DoorHandle {
  index: number
  /** 门中心的**图纸平面**坐标（米）。找最近的门用它 */
  planPos: [number, number]
  /** 门的宽度，用于判断"够不够近" */
  widthM: number
  /** 0 = 关，1 = 全开 */
  setOpen(t: number): void
  isOpen(): boolean
  /** 关着的时候，这块地方应当挡住人。返回图纸平面上的线段 */
  blockingSegment(): [[number, number], [number, number]] | null
}

export interface SceneHandles {
  scene: THREE.Scene
  /** 按画质档位重建可变部分（灯光） */
  applyTier(tier: QualityTier): void
  dispose(): void
  /** 包围盒（米），给相机远近裁剪面用 */
  bounds: { sizeX: number; sizeZ: number; height: number }
  /** 可开关的门。**默认全部打开** */
  doors: DoorHandle[]
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
  // 门洞上方的过梁
  // ══════════════════════════════════════════════════════════════
  // 碰撞线段在门洞处是断的 —— 3D 墙体于是也断到顶。不加过梁的话，
  // 门洞是一个**通到天花板的洞**，看着不像门、像墙塌了一块。
  // 补一段 2.05m 以上的墙，门洞才读得出"门"的形状。
  for (const d of w.doors) {
    const h = Math.max(height - DOOR_HEIGHT_M, 0)
    if (h <= 0.01) continue
    const geo = track(new THREE.BoxGeometry(d.width_m, h, halfThick * 2))
    const mesh = new THREE.Mesh(geo, wallMat)
    const [ex, , ez] = planToEngine(d.position[0], d.position[1],
                                    DOOR_HEIGHT_M + h / 2)
    mesh.position.set(ex, DOOR_HEIGHT_M + h / 2, ez)
    mesh.rotation.y = yawAlong(d.along)
    wallGroup.add(mesh)
  }

  // ══════════════════════════════════════════════════════════════
  // 门扇
  // ══════════════════════════════════════════════════════════════
  const leafMat = track(new THREE.MeshLambertMaterial({ color: DOOR_COLOR }))
  const doors: DoorHandle[] = []

  for (const d of w.doors) {
    const [hx, hy] = d.hinge
    const [axp, ayp] = d.along

    // 门组挂在铰链上，门扇沿组的局部 +X 伸出去
    const group = new THREE.Group()
    const [gx, , gz] = planToEngine(hx, hy, 0)
    group.position.set(gx, 0, gz)

    // 局部 +X 对齐"沿墙方向"。绕 Y 转 θ 会把 +X 送到 (cosθ, 0, -sinθ)，
    // 所以 θ = atan2(-dz, dx)，其中 (dx,dz) 是沿墙方向在 Three 里的表示
    const base = yawAlong([axp, ayp])

    const leafGeo = track(new THREE.BoxGeometry(
      d.width_m, DOOR_HEIGHT_M, LEAF_THICK_M,
    ))
    const leaf = new THREE.Mesh(leafGeo, leafMat)
    leaf.position.set(d.width_m / 2, DOOR_HEIGHT_M / 2, 0)
    group.add(leaf)

    // 门把手：一个小方块，让"这是门"一眼可见
    const knobGeo = track(new THREE.BoxGeometry(0.09, 0.09, 0.14))
    const knob = new THREE.Mesh(knobGeo, wallMat)
    knob.position.set(d.width_m - 0.12, 1.0, 0)
    group.add(knob)

    scene.add(group)

    let open = true                      // ⚠️ 默认全部打开
    const apply = (t: number) => {
      group.rotation.y = base + t * (Math.PI / 2)
    }
    apply(1)

    doors.push({
      index: d.index,
      planPos: [d.position[0], d.position[1]],
      widthM: d.width_m,
      setOpen(t: number) {
        open = t > 0.5
        apply(t)
      },
      isOpen: () => open,
      blockingSegment() {
        if (open) return null
        // 关着 → 门洞里多一段实心墙。用**碰撞中心线**那套端点，
        // 不是渲染端点（渲染端点是外延过的，会把门挤窄）
        return [
          [hx, hy],
          [hx + axp * d.width_m, hy + ayp * d.width_m],
        ]
      },
    })
  }

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
    doors,
    bounds: { sizeX, sizeZ, height },
    dispose() {
      for (const d of disposables) d.dispose()
      scene.clear()
    },
  }
}
