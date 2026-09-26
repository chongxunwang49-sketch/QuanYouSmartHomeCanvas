/**
 * 探针跑手：把 `frontend/probe/*.ts` 打进一个临时 bundle 再跑。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么需要它（而不是 `node --experimental-strip-types`）
 * ══════════════════════════════════════════════════════════════════
 * Node 内置的 TS 支持只做**类型擦除**，不改写模块路径 —— 而 `three/rig.ts`
 * 里写的是 `from './coords'`（无扩展名，Vite/TS 的写法），Node 的 ESM
 * 解析器要的是 `'./coords.ts'`。于是第一版直接 `node rig_wasd.ts` 报的是
 * `ERR_MODULE_NOT_FOUND`，跟被测代码毫无关系。
 *
 * esbuild 顺手解决这个：它按前端构建的同一条解析规则（tsconfig 的
 * `moduleResolution`）拼 bundle，于是探针跑的是**真的那份源码**，
 * 而不是照着源码又抄一遍的第二份实现。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么探针不放在 `src/` 里
 * ══════════════════════════════════════════════════════════════════
 * 它是**验证工具**，不是应用代码。放 `src/` 下会被 Vite 当作可选入口
 * 暴露出去，也会混进 `vue-tsc` 的产物判断里。放 `probe/` 下，只有
 * 这个跑手和 pytest 会用到它。
 *
 * 用法：`node probe/run.mjs probe/rig_wasd.ts`（工作目录 = `frontend/`）
 * 退出码即探针的退出码 —— 调用方（pytest）据此判定。
 */

import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

import { build } from 'esbuild'

const entry = process.argv[2]
if (!entry) {
  console.error('用法：node probe/run.mjs probe/<名字>.ts')
  process.exit(2)
}

const dir = await mkdtemp(join(tmpdir(), 'qy-probe-'))
const out = join(dir, 'bundle.mjs')
try {
  await build({
    entryPoints: [resolve(entry)],
    outfile: out,
    bundle: true,
    format: 'esm',
    platform: 'node',
    // three 是给浏览器写的，但它本身不碰 window/document，
    // 建 Mesh / Camera、算矩阵在 Node 里都跑得动。
    target: 'node20',
    logLevel: 'warning',
    // 探针要打 JSON 给 pytest 读，别让 esbuild 的横幅污染 stdout
    banner: { js: '// qy-probe bundle' },
  })
  await import(pathToFileURL(out).href)
} finally {
  await rm(dir, { recursive: true, force: true })
}
