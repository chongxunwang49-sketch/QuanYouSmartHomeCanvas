/**
 * ⚠️ **这个脚本跑不起来**（2026-09-28 整理时发现，如实标注而不是留着让人踩）：
 *    它 `import { buildFurniture } from './.probe-furniture.mjs'`，
 *    而那个文件**在仓库里从来没有过**（`git log --diff-filter=A` 查过，
 *    一次都没有）。多半是当时只在本地存在、随某次事故一起没了。
 *
 * 留着的理由：它记着"怎么把家具构造器单独摘出来跑"这件事本身。
 * 要用的话，把 `.probe-furniture.mjs` 照 `frontend/src/three/` 里的
 * 家具构造补出来即可；不需要就直接删。
 */

import { buildFurniture } from './.probe-furniture.mjs'

const data = JSON.parse(process.argv[2])
const h = buildFurniture(data)

// 后端声明的盒子，按 spec_id 索引（同一 spec 可能出现在不同房间，用坐标配对）
const declared = []
for (const r of data.rooms) for (const p of r.placements) declared.push(p)

const meshes = []
h.group.traverse((o) => { if (o.isMesh && o.name.startsWith('furniture:')) meshes.push(o) })

let mismatched = 0
const problems = []
for (const m of meshes) {
  const p = m.position
  const planX = p.x, planY = -p.z
  const g = m.geometry.parameters
  // 旋转 90/270 时，几何的 width 沿图纸 y、depth 沿图纸 x
  const rot = ((Math.round(-m.rotation.y * 180 / Math.PI) % 360) + 360) % 360
  const swap = rot === 90 || rot === 270
  const w = swap ? g.depth : g.width
  const d = swap ? g.width : g.depth

  const near = declared.find(
    (q) => Math.abs(q.x - planX) < 1e-6 && Math.abs(q.y - planY) < 1e-6 &&
           Math.abs(q.w - w) < 1e-6 && Math.abs(q.d - d) < 1e-6,
  )
  if (!near) {
    mismatched++
    problems.push({ spec: m.name, planX: +planX.toFixed(3), planY: +planY.toFixed(3),
                    w: +w.toFixed(3), d: +d.toFixed(3), rot })
  } else {
    // 高度与离地偏移也要对
    const expectY = near.y_offset_m + near.height_m / 2
    if (Math.abs(p.y - expectY) > 1e-6) {
      mismatched++
      problems.push({ spec: m.name, 高度不符: { 实际: p.y, 期望: expectY } })
    }
    if (near.height_m !== g.height) {
      mismatched++
      problems.push({ spec: m.name, 高度尺寸不符: { 几何: g.height, 声明: near.height_m } })
    }
  }
}

console.log(JSON.stringify({
  后端声明件数: declared.length,
  前端建出网格: meshes.length,
  对不上的: mismatched,
  问题: problems.slice(0, 5),
}, null, 1))
h.dispose()