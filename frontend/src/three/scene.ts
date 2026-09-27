import * as THREE from 'three'

import type { QualityTier } from '../composables/useFrameStats'
import type { FurnitureData, WalkableResponse } from '../api'
import { planToEngine } from './coords'
import {
  buildFurniture,
  type FurnitureHandles,
  type FurnitureInfo,
} from './furniture'

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
/** 被准星选中的门扇的自发光色。暖金，和设计系统的 accent-gold 同源。 */
const HIGHLIGHT_EMISSIVE = 0x6a4a1e

/**
 * 准星"够得着"的距离（米）。**和 `SceneViewer` 里那把门的判定同一口径。**
 *
 * 2.6m ≈ 门宽 0.9m 的两倍多一点 —— 站在门口一两步之内。
 * 放太长会让"站在客厅中间按 F 开了卧室的门"，那看起来就是乱响应。
 */
export const DOOR_REACH_M = 2.6

//: 准星认家具的最远距离（米）。**比门的伸手距离远得多** ——
//: 家具是隔着几米看的，用 2.6m 会让说明只在贴脸时才弹出来，
//: 而用户想看的是"那头那个大柜子是什么"。仍受"先撞墙就停"的限制。
export const FURNITURE_REACH_M = 40

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
  /** 被准星选中时点亮。**"F 会作用在哪扇门"必须看得见** */
  setHighlight(on: boolean): void
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
  /**
   * 天花板显隐。
   *
   * ⚠️ **进自由视角（飞行）时调用方应当调 `false`。** 需求方原话：
   * 「启动飞行模式就是为了在高处看格局，如果被天花板挡住了就没有任何意义」。
   * 这是对的 —— 天花板在贴地行走时是必要的（没有它，抬头看到的是
   * 场景背景色，不像室内），而在飞行时它恰好挡在唯一的观察方向上。
   */
  setCeilingVisible(v: boolean): void
  /**
   * **准星选门**：从画面正中射一条线，返回打中的那扇门；没打中门返回 null。
   *
   * 见文件里 `pickDoor` 的说明 —— 这是"两扇门贴得近时按 F 响应的不是
   * 自己想开的那扇"的解法。
   */
  doorAtCrosshair(camera: THREE.Camera): DoorHandle | null
  /** 离给定位置最近、且在 `maxDist` 之内的门。准星的退路。 */
  nearestDoor(planPos: [number, number], maxDist: number): DoorHandle | null
  /**
   * 准星对着哪件家具（没有就是 `null`）。
   *
   * ⚠️ **和门一样要"先撞到墙就不算"** —— 否则站在客厅能"看穿"墙
   * 报出隔壁卧室的床，而屏幕上那块地方明明是一面墙。
   */
  furnitureAtCrosshair(camera: THREE.Camera): FurnitureInfo | null
}

/**
 * @param data `/layout/{id}/walkable` 的返回
 * @param furniture `/layout/{id}/furniture` 的返回。**可选** ——
 *        家具接口拿不到时 3D 仍然要能看（空房子也比没有房子好），
 *        所以它是可选参数而不是必填。调用方负责把"为什么没有家具"
 *        告诉用户，不要在这里静默吞掉。
 */
export function buildScene(
  data: WalkableResponse,
  furniture?: FurnitureData | null,
): SceneHandles {
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
    // ⚠️ 过梁**打上门的标记**。它是门的一部分（门框上沿），准星指着门楣
    //    时该选中这扇门 —— 不打标记的话准星稍微抬高一点就判定为"对着墙"，
    //    而门楣和门在同一平面上，用户看不出差别，只会觉得"对准了却没反应"。
    mesh.userData.doorIndex = d.index
    wallGroup.add(mesh)
  }

  // ══════════════════════════════════════════════════════════════
  // 门扇
  // ══════════════════════════════════════════════════════════════
  const leafBase = track(new THREE.MeshLambertMaterial({ color: DOOR_COLOR }))
  const doors: DoorHandle[] = []
  /** 门扇所在的分组 —— 准星射线只打这些（加上墙，用来判"中间隔着墙"）。*/
  const doorGroups: THREE.Group[] = []

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

    // ⚠️ **门扇用自己的材质实例**（不是共享的 `leafBase`）。
    //    高亮只能点亮一扇门 —— 共享材质的话点亮一扇等于点亮全部。
    //    实测门只有个位数，多几个材质实例的开销可以忽略。
    const leafMat = track(leafBase.clone())

    const leafGeo = track(new THREE.BoxGeometry(
      d.width_m, DOOR_HEIGHT_M, LEAF_THICK_M,
    ))
    const leaf = new THREE.Mesh(leafGeo, leafMat)
    leaf.position.set(d.width_m / 2, DOOR_HEIGHT_M / 2, 0)
    // 门扇也打标记：准星射线从门扇上取回是哪扇门
    leaf.userData.doorIndex = d.index
    group.add(leaf)

    // 门把手：一个小方块，让"这是门"一眼可见
    const knobGeo = track(new THREE.BoxGeometry(0.09, 0.09, 0.14))
    const knob = new THREE.Mesh(knobGeo, leafMat)
    knob.position.set(d.width_m - 0.12, 1.0, 0)
    knob.userData.doorIndex = d.index
    group.add(knob)

    scene.add(group)
    doorGroups.push(group)

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
      setHighlight(on: boolean) {
        // 自发光而不是换色：换色会和门本身的木色打架，也更难看出
        // "这是选中态"还是"这扇门本来就是浅色"。
        leafMat.emissive.setHex(on ? HIGHLIGHT_EMISSIVE : 0x000000)
      },
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
  // ── 地面色：有家具数据时用 3D 专用的 surface_floor ──────────────
  //
  // ⚠️ 常量 `FLOOR_COLOR` 是**兜底**（没有家具数据时）。后端的
  //    `surface_floor` 比它深，那是刻意的：浅色地面会让浅色家具
  //    （布艺、石面）跟地面同色 —— 实测 modern 的 fabric 对浅地面
  //    只有 1.01:1，等于没画。地面是中调，家具才能双向对比。
  const floorColor = (() => {
    const hex = furniture?.surface_floor
    if (!hex) return FLOOR_COLOR
    const n = parseInt(hex.replace('#', ''), 16)
    return Number.isNaN(n) ? FLOOR_COLOR : n
  })()

  const floorMat = track(new THREE.MeshLambertMaterial({
    color: floorColor, side: THREE.DoubleSide,
  }))
  const ceilMat = track(new THREE.MeshLambertMaterial({
    color: CEILING_COLOR, side: THREE.DoubleSide,
  }))
  // ⚠️ 天花板**单独一个分组**。它不是装饰：飞行模式要把它整体隐藏
  //    （见 `setCeilingVisible`），散在场景里就没法一次全隐。
  const ceilingGroup = new THREE.Group()
  ceilingGroup.name = 'ceilings'

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
    ceilingGroup.add(ceil)
  })
  scene.add(ceilingGroup)

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

  // ══════════════════════════════════════════════════════════════
  // 家具 —— 坐标全部来自后端（见 furniture.ts 的说明）
  // ══════════════════════════════════════════════════════════════
  let furnitureHandles: FurnitureHandles | null = null
  if (furniture && furniture.rooms?.length) {
    // 动态 import 会破坏 buildScene 的同步签名，而这个模块与 coords
    // 已经同属 three 那个 chunk，静态引入不会多拉任何东西。
    furnitureHandles = buildFurniture(furniture)
    scene.add(furnitureHandles.group)
  }

  // ══════════════════════════════════════════════════════════════
  // 准星选门
  // ══════════════════════════════════════════════════════════════
  //
  // ⚠️ **为什么不是"离得最近的那扇"。**
  //
  // 需求方原话：「两个门贴的很近时按 F 会出现相应的不是自己想要响应的门」。
  // 距离是**对称量**，人站在两扇门之间时，它根本无法表达"我想要哪一扇" ——
  // 那 5cm 的远近差别既不是用户能感知的，也不是用户能控制的。
  // 玩家能控制的是**看哪儿**，所以判据应该用视线，不是距离。
  //
  // 做法就是射击游戏的准星：从画面正中射一条线，打在门扇/门楣上的
  // 那扇门就是目标。顺带解决了两件事：
  //   · 隔着墙的门不会被选中（射线先撞墙）
  //   · 门开着、门扇转到房间里时，指着门扇也知道是哪一扇
  //
  // ⚠️ 射线只打**门扇/门楣/墙**这三类，不打地板、家具、天花板。
  //    打了的话，低头看地板就会命中"地板"，把门判成"准星没对准"——
  //    而玩家低头开门是很自然的动作。
  const raycaster = new THREE.Raycaster()
  const SCREEN_CENTER = new THREE.Vector2(0, 0)
  /** 命中结果里反查是哪扇门。门扇、门把手、门楣都打了 `doorIndex` 标记。 */
  const doorByIndex = new Map<number, DoorHandle>()
  for (const d of doors) doorByIndex.set(d.index, d)

  function doorOf(obj: THREE.Object3D): DoorHandle | null {
    let cur: THREE.Object3D | null = obj
    while (cur) {
      const idx = cur.userData?.doorIndex
      if (typeof idx === 'number') return doorByIndex.get(idx) ?? null
      cur = cur.parent
    }
    return null
  }

  function doorAtCrosshair(camera: THREE.Camera): DoorHandle | null {
    // 相机这一帧已经挪过位置（`rig.syncCamera()`），而 `matrixWorld` 是
    // 上一帧渲染时更新的。不刷的话射线起点差一帧的距离 —— 近距离开门时
    // 那点差别足够让射线从门缝里穿过去。
    camera.updateMatrixWorld()
    raycaster.setFromCamera(SCREEN_CENTER, camera)
    raycaster.far = DOOR_REACH_M

    // 墙也要一起打：**先撞到墙就说明中间隔着墙**，这时不算"够得着"。
    // 结果按距离排序，所以取第一个命中项判定即可。
    const hits = raycaster.intersectObjects([...doorGroups, wallGroup], true)
    const first = hits.find((h) => h.object.visible)
    if (!first) return null
    return doorOf(first.object)
  }

  function nearestDoor(planPos: [number, number], maxDist: number): DoorHandle | null {
    let best: DoorHandle | null = null
    let bestD = maxDist
    for (const d of doors) {
      const dist = Math.hypot(d.planPos[0] - planPos[0], d.planPos[1] - planPos[1])
      if (dist <= bestD) {
        bestD = dist
        best = d
      }
    }
    return best
  }

  /**
   * 准星对着哪件家具。
   *
   * 与 `doorAtCrosshair` 同一套做法，两处差别只有"打哪些物体"：
   * 门打门扇 + 墙体，家具打家具体块 + 墙体。
   *
   * ⚠️ `raycaster.far` 用一个**够远**的值（跨场景），而不是门的伸手距离 ——
   *    家具是可以隔着几米看的，用伸手距离会让它只在贴脸时才弹说明。
   *    但也不能无界：射到场景外面时不该报出背后房间的东西，
   *    所以仍受"先撞墙就停"的限制。
   */
  function furnitureAtCrosshair(camera: THREE.Camera): FurnitureInfo | null {
    if (!furnitureHandles) return null
    // 见 doorAtCrosshair 的说明：相机这一帧刚挪过，矩阵要手动刷
    camera.updateMatrixWorld()
    raycaster.setFromCamera(SCREEN_CENTER, camera)
    raycaster.far = FURNITURE_REACH_M
    const hits = raycaster.intersectObjects(
      [furnitureHandles.group, wallGroup], true,
    )
    const first = hits.find((h) => h.object.visible)
    if (!first) return null
    // 排在前面的是墙 → 家具在墙后面，不算对准
    return (first.object.userData?.furniture as FurnitureInfo) ?? null
  }

  function setCeilingVisible(v: boolean) {
    ceilingGroup.visible = v
  }

  return {
    scene,
    applyTier,
    doors,
    setCeilingVisible,
    doorAtCrosshair,
    nearestDoor,
    furnitureAtCrosshair,
    bounds: { sizeX, sizeZ, height },
    dispose() {
      for (const d of disposables) d.dispose()
      scene.clear()
    },
  }
}
