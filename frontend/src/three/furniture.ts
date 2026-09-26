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

/** 体块的颜色角色 → 颜色。`color_role` 由目录给，是稳定契约。 */
function colorFor(data: FurnitureData, role: string): number {
  const hex = (data.palette as Record<string, string | null>)[role]
  if (hex && /^#[0-9a-fA-F]{6}$/.test(hex)) return parseInt(hex.slice(1), 16)
  return FALLBACK_COLOR
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
  const materials = new Map<string, THREE.Material>()
  const materialFor = (role: string): THREE.Material => {
    const key = role || 'wood'
    let m = materials.get(key)
    if (!m) {
      m = track(new THREE.MeshLambertMaterial({
        color: colorFor(data, key),
        // 半透明只给"不参与碰撞"的软性件（地毯/地台），让它们看着是铺在地上的
        transparent: false,
      }))
      materials.set(key, m)
    }
    return m
  }

  for (const room of data.rooms) {
    for (const p of room.placements) {
      addOne(group, track, p, materialFor(p.color_role))
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
    group.add(em)
  }
}
