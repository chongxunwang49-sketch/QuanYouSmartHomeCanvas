/**
 * 3D 漫游的按键表。**唯一的按键定义处。**
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么要单独一个文件，而不是写在 rig.ts 里
 * ══════════════════════════════════════════════════════════════════
 * 和 `coords.ts` 里那组视场角常量是同一个理由：**`rig.ts` import 了 three
 * （570KB）**。按键表要给 `SceneViewer` 用（它要渲染"按什么键干什么"的
 * 提示），从 rig.ts 引就会把 three 静态拉回主包，`ParseView` 的分包
 * 从 45KB 涨回 437KB —— 那个坑已经踩过一次，写在 `coords.ts` 里了。
 *
 * 这个文件**不 import 任何东西**。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么要做成一张表，而不是散落的 if
 * ══════════════════════════════════════════════════════════════════
 * 2026-09-24 需求方反馈「wasd 出现混乱，按住 a 甚至可能是往右走」。
 * 那次的根因在 `rig.ts`（世界位移→图纸位移的负号只写在行走分支里），
 * 不在按键表。但查的时候暴露了另一件事：**"哪个键干什么"在这份代码里
 * 有三分** —— `KEYMAP` 一份、`onKeyDown` 里的 if 一份、界面提示文案
 * 又手写一份。三份里改一份，另外两份都不会报错。
 *
 * 所以这里合并成一张表，`KEYMAP` 和界面提示**都从它推**：
 *   · 想加一个键 → 只改这里
 *   · 界面提示写错了 → 不可能，它是推出来的
 *
 * ⚠️ 用 `code` 而不用 `key`。`key` 会随输入法、大小写锁定、以及
 *    `Shift` 组合变化（按 Shift+W 时 `key` 是 `'W'`），中文输入法下
 *    更不可靠。`code` 是物理键位，不受这些影响。
 *    —— 这一条是从"按住 Shift 疾走时反而走不动"推出来的：Shift+W 的
 *    `key` 是 `'W'`，用 `key` 查表就查不到。
 */

/** 持续按住才生效的动作。一次性动作（开门/回到起点/切模式）不在这里。 */
export type Action =
  | 'forward' | 'back' | 'left' | 'right'
  | 'up' | 'down' | 'run'

/** 一次性动作标识。按下即生效，不记状态。 */
export type OnceAction = 'door' | 'respawn' | 'mode'

export interface KeyBinding {
  /** `KeyboardEvent.code`。见文件头：不用 `key`。 */
  code: string
  /** 界面上显示的键名。 */
  cap: string
  /** 这个键干什么。**界面提示文案的唯一来源。** */
  help: string
  /** 持续动作对应的 `keys` 字段；一次性动作用 `once`。 */
  action?: Action
  once?: OnceAction
  /**
   * 只在某个模式下有意义。不写 = 两种模式都可用。
   *
   * `fly` = 只有自由视角下才有意义（上升/下降）；`walk` = 只有行走下才有。
   */
  mode?: 'walk' | 'fly'
  /** 在提示里排前面（移动类）。 */
  primary?: boolean
}

/**
 * 按键表。**顺序就是界面提示的顺序。**
 *
 * 方向键与 WASD 等价 —— 笔记本没有小键盘时方向键更好按，而且这是
 * 第一人称漫游的通行做法（Minecraft / SketchUp 都是这样）。
 */
export const KEY_BINDINGS: readonly KeyBinding[] = [
  { code: 'KeyW', cap: 'W', help: '前进', action: 'forward', primary: true },
  { code: 'ArrowUp', cap: '↑', help: '前进', action: 'forward' },
  { code: 'KeyS', cap: 'S', help: '后退', action: 'back', primary: true },
  { code: 'ArrowDown', cap: '↓', help: '后退', action: 'back' },
  { code: 'KeyA', cap: 'A', help: '左移', action: 'left', primary: true },
  { code: 'ArrowLeft', cap: '←', help: '左移', action: 'left' },
  { code: 'KeyD', cap: 'D', help: '右移', action: 'right', primary: true },
  { code: 'ArrowRight', cap: '→', help: '右移', action: 'right' },
  // ⚠️ 上升/下降**只在自由视角下有意义** —— 行走模式锁死眼高（贴地走）。
  //    原先把它们写进通用提示里，行走模式下按了没反应，看着像坏了。
  { code: 'Space', cap: 'Space', help: '上升', action: 'up', mode: 'fly' },
  { code: 'KeyE', cap: 'E', help: '上升', action: 'up', mode: 'fly' },
  { code: 'KeyC', cap: 'C', help: '下降', action: 'down', mode: 'fly' },
  { code: 'KeyQ', cap: 'Q', help: '下降', action: 'down', mode: 'fly' },
  { code: 'ShiftLeft', cap: 'Shift', help: '加速', action: 'run' },
  { code: 'ShiftRight', cap: 'Shift', help: '加速', action: 'run' },

  { code: 'KeyF', cap: 'F', help: '开/关门（准星对准的那扇）', once: 'door' },
  // G 的文案要同时说清两个方向：默认是自由视角，按 G 是"下去走"，
  // 在行走里按 G 是"升上来"。见 SceneViewer 里 `canWalk` 的说明。
  { code: 'KeyG', cap: 'G', help: '下到地面行走 / 升空俯瞰', once: 'mode', mode: 'walk' },
  // ⚠️ R 的落点与模式有关：自由视角下回到"天花板之上俯瞰"那个起点
  //    （见 rig.ts 的 `applyStartPose`），行走下回到出生点平视。
  { code: 'KeyR', cap: 'R', help: '回到起点（自由视角下回到俯瞰位）', once: 'respawn' },
]

/**
 * `KeyboardEvent.code` → 持续动作。**从 `KEY_BINDINGS` 推出来的，不手写。**
 *
 * 同一个动作有多个键（W 与 ↑）时后写的覆盖前面的值 —— 值相同，
 * 所以顺序无所谓。
 */
export const KEYMAP: Readonly<Record<string, Action>> = Object.freeze(
  Object.fromEntries(
    KEY_BINDINGS
      .filter((b): b is KeyBinding & { action: Action } => b.action != null)
      .map((b) => [b.code, b.action]),
  ),
)

/** `KeyboardEvent.code` → 一次性动作。 */
export const ONCE_MAP: Readonly<Record<string, OnceAction>> = Object.freeze(
  Object.fromEntries(
    KEY_BINDINGS
      .filter((b): b is KeyBinding & { once: OnceAction } => b.once != null)
      .map((b) => [b.code, b.once]),
  ),
)

/**
 * 全部持续动作，去重且顺序固定。
 *
 * 调用方拿它建"按键状态"初值对象 —— 这样键表加一个动作时，
 * 状态对象自动跟着有那个字段，不会漏（漏了就是按下去没反应）。
 */
export const ACTIONS: readonly Action[] = Object.freeze([
  ...new Set(
    KEY_BINDINGS
      .filter((b): b is KeyBinding & { action: Action } => b.action != null)
      .map((b) => b.action),
  ),
])

/**
 * 当前模式下**真正能用**的按键。界面提示与自检都走它。
 *
 * `G` 只在能切回行走时才有意义（后端判了不可行走时不让切），
 * 所以它的 `mode` 标成 `walk` —— 自由视角下不该提示"按 G 切行走"，
 * 按下去是被拒绝的。
 */
export function bindingsFor(mode: 'walk' | 'fly'): KeyBinding[] {
  return KEY_BINDINGS.filter((b) => !b.mode || b.mode === mode)
}

/** 一行提示文案。`SceneViewer` 的引导层用它。 */
export function helpLine(mode: 'walk' | 'fly'): string {
  const seen = new Set<string>()
  const parts: string[] = []
  for (const b of bindingsFor(mode)) {
    if (b.action === 'run' && seen.has('run')) continue
    seen.add(b.action ?? b.once ?? b.code)
    parts.push(`${b.cap} ${b.help}`)
  }
  return parts.join(' · ')
}
