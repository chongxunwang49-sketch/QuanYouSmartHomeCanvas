import * as THREE from 'three'

import type { FurnitureData, FurniturePlacement } from '../api'
import { planToEngine } from './coords'

/**
 * 把后端算好的家具摆放画成体块。
 *
 * ══════════════════════════════════════════════════════════════════
 * 前端不推导任何坐标，也不判断"该摆哪儿"
 * ══════════════════════════════════════════════════════════════════
 * `x` / `y` / `w` / `d` / `rot_deg` 全部来自
 * `GET /layout/{id}/furniture`，那里的摆放是**规则算出来的**，
 * 而且逐件过了三条几何判据（在房间里 / 不堵门 / 不压别的家具）。
 * 前端再判一次就等于多出第二套判据 —— 两套只要有一处不一样，
 * 表现就是"看着摆对了、其实挡住门"。
 *
 * ══════════════════════════════════════════════════════════════════
 * 朝向：`rot_deg` 是**图纸平面的**逆时针角度
 * ══════════════════════════════════════════════════════════════════
 * 图纸 (x, y) → Three (x, h, -y)（见 `coords.ts` 的 `planToEngine`），
 * 所以图纸里逆时针转 θ，在 Three 里是绕 Y 轴转 **−θ**。这个符号是推出来的：
 *
 *     图纸： x' = x·cosθ − y·sinθ      z' = −y
 *     Three：x' = x·cosφ + z·sinφ      z' = −x·sinφ + z·cosφ
 *     代入 y = −z 后与第一式对齐 ⇒ φ = −θ
 *
 * 拿不准时会写成 `+θ`，而房间是矩形、家具只取 0/90/180/270 时，
 * **正负号错了照样看着"摆进去了"** —— 只是背对着墙。所以写下来。
 *
 * ⚠️ **但今天这个旋转对主视觉体块是"惰性"的，如实记下来免得有人以为它在起作用。**
 * `BoxGeometry` 是拿后端给的**世界轴对齐** `w` / `d` 建的，而盒子左右对称 ——
 * 于是绕 Y 转 90° 与不转，占位与外观完全一样。它现在真正起作用的地方是
 * `extras`（附件跟着父级一起转）以及将来换成真模型/贴图时的朝向。
 *
 * 这条是**用数值验证发现的**，不是看代码看出来的：写了个 Node 探针把
 * `buildFurniture` 建出来的每个网格的 position / 尺寸 / 离地偏移与后端
 * 声明逐件对比，**24/24 完全相等**；探针第一版按"旋转要交换 w/d"去比，
 * 结果 10 件不符 —— 错的是探针，而它顺带说明了这层旋转不影响占位。
 */

/** 没有配色时的兜底。设计系统里的木色，不用纯灰。 */
const FALLBACK_COLOR = 0xbfa98a

export interface FurnitureHandles {
  group: THREE.Group
  dispose(): void
}

/**
 * 体块上挂的"这是什么"，供准星射线取回。
 *
 * ⚠️ **挂在 `userData` 上而不是另建一张 Map**：射线命中拿到的是
 * `Object3D`，`userData` 是它自带的、随对象生命周期走 ——
 * 另建一张表就要处理"重建场景后旧表还在"的清理问题，
 * 而那种残留的表现是"对准新家具弹出旧家具的名字"。
 */
export interface FurnitureInfo {
  spec_id: string
  label: string
  room_name: string
  /** 尺寸（米），弹窗里显示 */
  w: number
  d: number
  h: number
  /** 3D 体块的色号，弹窗里画一个小色块与眼前的东西对上 */
  color: string
  /** 后端给的"为什么摆在这里"。**要能说出口**（见 Placement.basis 的说明） */
  basis: string[]
}

const HEX_RE = /^#[0-9a-fA-F]{6}$/

/** 体块的颜色角色 → 颜色。`color_role` 由目录给，是稳定契约。 */
function colorFor(data: FurnitureData, role: string): number {
  const hex = (data.palette as Record<string, string | null>)[role]
  return hex && HEX_RE.test(hex) ? parseInt(hex.slice(1), 16) : FALLBACK_COLOR
}

/**
 * 一件家具的体块颜色。
 *
 * ⚠️ **优先用后端给的 `p.color`（族色），而不是自己拿 `color_role` 查表。**
 *
 * `color_role` 是按**材质**分的六个角色（木/布艺/金属/石材/绿植/白）——
 * 同一间卧室里床和床头柜都是 `fabric`，画出来是两块一模一样的色块，
 * 分不出哪个是床。需求方要的是"按家具种类上色"。
 *
 * 所以族色由后端算好（`catalog.family_color`，由
 * `scripts/derive_family_palette.py` 推出，约束是对地面 ≥2:1 且
 * 会同房的族两两 ΔE ≥6），前端只负责用。**前端不自己查目录** ——
 * 两处各判一次"这个族该是什么色"迟早会分叉。
 *
 * 后端没给（旧数据、目录里新加的族）时才退回角色色。
 */
function bodyColor(data: FurnitureData, p: FurniturePlacement): number {
  const own = (p as { color?: string }).color
  if (own && HEX_RE.test(own)) return parseInt(own.slice(1), 16)
  return colorFor(data, p.color_role)
}

export function buildFurniture(data: FurnitureData): FurnitureHandles {
  const group = new THREE.Group()
  group.name = 'furniture'

  const disposables: { dispose(): void }[] = []
  const track = <T extends { dispose(): void }>(x: T): T => {
    disposables.push(x)
    return x
  }

  // 每种颜色一个材质，避免每件家具都新建一个（几十件时是明显的开销）
  //
  // ⚠️ 缓存键是**解析出来的色号**（族色），不是 `color_role` ——
  //    同族要同色，而键用角色的话，两个不同族会被映射到同一个材质。
  const materials = new Map<number, THREE.Material>()
  const materialFor = (color: number): THREE.Material => {
    const key = color
    let m = materials.get(key)
    if (!m) {
      m = track(new THREE.MeshLambertMaterial({
        color,
        // 半透明只给"不参与碰撞"的软性件（地毯/地台），让它们看着是铺在地上的
        transparent: false,
      }))
      materials.set(key, m)
    }
    return m
  }

  for (const room of data.rooms) {
    for (const p of room.placements) {
      addOne(group, track, p, materialFor(bodyColor(data, p)))
    }
  }

  return {
    group,
    dispose() {
      for (const d of disposables) d.dispose()
      group.clear()
    },
  }
}

function addOne(
  group: THREE.Group,
  track: <T extends { dispose(): void }>(x: T) => T,
  p: FurniturePlacement,
  material: THREE.Material,
): void {
  if (p.w <= 0 || p.d <= 0 || p.height_m <= 0) return

  const geo = track(new THREE.BoxGeometry(p.w, p.height_m, p.d))
  const mesh = new THREE.Mesh(geo, material)
  mesh.name = `furniture:${p.spec_id}`
  // 准星对准它时弹出来的那张小卡片（见 FurnitureInfo 的说明）
  const info: FurnitureInfo = {
    spec_id: p.spec_id, label: p.label, room_name: p.room_name,
    w: p.w, d: p.d, h: p.height_m, color: p.color ?? '',
    basis: p.basis ?? [],
  }
  mesh.userData.furniture = info

  // 底面离地 = y_offset_m；中心再抬半个高度
  const [ex, , ez] = planToEngine(p.x, p.y, p.y_offset_m + p.height_m / 2)
  mesh.position.set(ex, p.y_offset_m + p.height_m / 2, ez)
  // 见文件头：图纸逆时针 θ = Three 绕 Y 转 −θ
  mesh.rotation.y = (-p.rot_deg * Math.PI) / 180

  group.add(mesh)

  // 挂墙件的附件（镜面、吊柜）跟着主体一起画 —— 位置由后端给的 extras 决定
  for (const extra of p.extras ?? []) {
    const w = Number(extra.w ?? 0)
    const d = Number(extra.d ?? 0)
    const h = Number(extra.h ?? 0)
    if (w <= 0 || d <= 0 || h <= 0) continue
    const eg = track(new THREE.BoxGeometry(w, h, d))
    const em = new THREE.Mesh(eg, material)
    const [gx, , gz] = planToEngine(
      p.x + Number(extra.dx ?? 0),
      p.y + Number(extra.dy ?? 0),
      p.y_offset_m + Number(extra.dz ?? 0) + h / 2,
    )
    em.position.set(gx, p.y_offset_m + Number(extra.dz ?? 0) + h / 2, gz)
    em.rotation.y = mesh.rotation.y
    // 附件（镜面/吊柜）也打同一个标记 —— 准星打在镜子上时
    // 用户心里想的是"这是浴室柜"，不是"这是一块镜子"
    em.userData.furniture = info
    group.add(em)
  }
}
