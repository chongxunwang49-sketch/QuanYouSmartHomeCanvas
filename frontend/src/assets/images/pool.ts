/**
 * 图片池。**带降级**。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么要有这一层
 * ══════════════════════════════════════════════════════════════════
 * `images/cases/` 里的 14 张全友实景案例图是**全友的版权作品**，
 * 因此被 `.gitignore` 挡在仓库外（见根目录 .gitignore 的说明）。
 *
 * 后果是：从仓库 clone 下来的人**本地没有这批图**。如果代码直接
 * `import.meta.glob('.../cases/*')` 就结束，那些位置会全部裂图 ——
 * 首屏看着像坏了。
 *
 * 所以这里做一层回落：**案例图缺失时自动用 Pexels 照片顶上**。
 * Pexels 是免费商用许可，可以入库，所以新克隆的仓库至少有一批能看的图。
 *
 * 本地开发（跑过采集脚本）看到的是全友实景，公开仓库里看到的是
 * Pexels 家居照片 —— 两边都不裂图，且没有任何版权素材被推上公开渠道。
 */

export interface ImagePool {
  /** 主图池。有案例图就是案例图，否则是 Pexels 照片 */
  main: string[]
  /** 这批图是不是全友案例图（决定要不要打版权标注） */
  isQuanyouCase: boolean
}

const toUrls = (mods: Record<string, string>): string[] =>
  Object.entries(mods)
    .sort(([a], [b]) => a.localeCompare(b)) // 顺序稳定：同一套方案每次看到同一张图
    .map(([, url]) => url)

// eager + ?url：构建期解析成带 hash 的静态地址，只有用到的进产物
const caseModules = import.meta.glob('./cases/*', {
  eager: true,
  query: '?url',
  import: 'default',
}) as Record<string, string>

const photoModules = import.meta.glob('./photos/*', {
  eager: true,
  query: '?url',
  import: 'default',
}) as Record<string, string>

const cases = toUrls(caseModules)
const photos = toUrls(photoModules)

/** 案例图优先；没有就落到 Pexels 照片。两者都没有才是真的空。 */
export const imagePool: ImagePool = cases.length
  ? { main: cases, isQuanyouCase: true }
  : { main: photos, isQuanyouCase: false }

/** 按序号稳定取图。序号超出范围就取模 —— 方案数可能多于图数。 */
export function imageAt(index: number): string {
  const pool = imagePool.main
  return pool.length ? pool[index % pool.length] : ''
}

/** 图上那句版权标注的文案。**回落到 Pexels 时不能还写"全友实景"**。 */
export const imageAttribution = imagePool.isQuanyouCase
  ? '素材来自全友官网公开案例，版权归全友家居所有，仅作演示与设计参考'
  : '素材来自 Pexels（免费商用许可）'
