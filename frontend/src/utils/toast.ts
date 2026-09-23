/**
 * 轻提示。**全项目唯一的 Element Plus 接触点。**
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么这里引的是深路径而不是 `from 'element-plus'`
 * ══════════════════════════════════════════════════════════════════
 * 试过桶式导入，产物里 Element Plus 那个 chunk 有 **938 KB**（gzip 301 KB）
 * —— 而这个项目只用了 ElMessage 一个东西。
 *
 * 根因不是 `manualChunks` 配错，是摇不掉：
 * element-plus 的 `es/index.mjs` 把每个组件都 re-export 一遍，而每个组件的
 * `index.mjs` 在**模块顶层**调了 `withInstall(...)`。那是货真价实的副作用，
 * Rollup 不能证明它可丢，于是整个组件库都被保留。
 * （它的 `sideEffects` 字段声明是对的，但声明救不了这种模块级调用。）
 *
 * 直接引组件文件就绕开了这个桶：只有 ElMessage 及其真实依赖进产物。
 * 实测 chunk 从 938 KB 降到 ~50 KB。
 *
 * ⚠️ **代价是这个路径是 element-plus 的内部结构**，不是公开 API。
 * 所以把它锁在这一个文件里 —— 将来升级 Element Plus 时如果路径变了，
 * 只需要改这里，而不是散在七个 view 里。
 *
 * 顺带说一句：如果哪天这个项目要用 Element Plus 的表格、表单、日期选择器
 * 这类真·复杂组件，那 938 KB 是**值得付的**，届时应当改回桶式导入。
 * 现在为一个 toast 付这个体积，不划算。
 */

import { ElMessage } from 'element-plus/es/components/message/index.mjs'
import 'element-plus/es/components/message/style/css.mjs'

type Kind = 'success' | 'warning' | 'error' | 'info'

/** 统一的提示入口。语义化的方法名比到处写 `ElMessage({type:'error'})` 清楚。 */
export const toast = {
  success: (msg: string) => ElMessage({ type: 'success', message: msg, grouping: true }),
  warning: (msg: string) => ElMessage({ type: 'warning', message: msg, grouping: true }),
  error: (msg: string) => ElMessage({ type: 'error', message: msg, grouping: true }),
  info: (msg: string) => ElMessage({ type: 'info', message: msg, grouping: true }),
  show: (msg: string, kind: Kind = 'info') => ElMessage({ type: kind, message: msg, grouping: true }),
}
