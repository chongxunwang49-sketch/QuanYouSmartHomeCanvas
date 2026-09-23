/**
 * 户型平面坐标 ↔ Three.js 世界坐标。**整个 3D 模块只有这一处做换算。**
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么要单独一个文件
 * ══════════════════════════════════════════════════════════════════
 * 因为这里会出一个**看不见的错**：镜像。
 *
 *     平面坐标（图纸）             Three.js 世界
 *     y 向上（远离观察者）          y 是高度，地面是 XZ 平面
 *     (0,0) 在左下                 (0,0,0) 在原点
 *
 * 从上方俯视 Three 的场景时，屏幕上是 **+x 向右、+z 向下**；而图纸上是
 * **+x 向右、+y 向上**。所以图纸的 y 轴必须映射到 **-z**。
 *
 * 映射成 **+z** 的话，整个户型会左右镜像 —— 而镜像的户型**看上去完全正常**，
 * 只是厨房跑到了客厅左边。没有任何界面元素会提示这件事。
 * （同一个坑在几何内核里已经踩过一次：Y 轴不翻转时 3D 是镜像的。）
 *
 * 所以：
 *   · 换算只写在这里，别处一律调这两个函数
 *   · 朝向、移动、放置都从同一个 `headingToEngine` 走
 *
 * ══════════════════════════════════════════════════════════════════
 * 自检：这个镜像问题靠什么发现
 * ══════════════════════════════════════════════════════════════════
 * 光看 3D 画面发现不了 —— 镜像的户型也是"一间卧室、一间客厅"。
 * 所以 `SceneViewer` 把玩家位置**同时**画在后端渲染的 2D 户型图上
 * （那个 SVG 的朝向有 pytest 钉着：`test_上下没有颠倒` / `test_左右没有镜像`）。
 *
 * 两个**互相独立**的计算（Three 的世界坐标 vs 后端的画布像素）指向同一个
 * 位置 —— 一旦映射写反，小地图上的点会跑到户型的另一侧，一眼就看得出来。
 */

/** 图纸平面坐标（米，y 向上）→ Three.js 世界坐标（y 是高度）。 */
export function planToEngine(x: number, y: number, height = 0): [number, number, number] {
  return [x, height, -y]
}

/** Three.js 世界坐标 → 图纸平面坐标（米）。**`planToEngine` 的逆。** */
export function engineToPlan(ex: number, ez: number): [number, number] {
  return [ex, -ez]
}

/**
 * 朝向角（度，图纸坐标系：0° = +x，90° = +y）→ Three 里的单位方向向量。
 *
 * ⚠️ 不能直接在 Three 里用 `cos/sin` 得到 (x, z) —— 那等于把 y 当成了 z。
 * 必须经过 `planToEngine` 的取反：图纸 +y 方向在 Three 里是 **-z**。
 */
export function headingToEngine(yawDeg: number): [number, number] {
  const rad = (yawDeg * Math.PI) / 180
  return [Math.cos(rad), -Math.sin(rad)]
}

/**
 * Three 里相机看向某点所需的 yaw（弧度，绕 Y 轴）。
 *
 * Three 的相机默认朝 **-z**；`Object3D.rotation.y = 0` 时看向 -z。
 * 所以"看向 (dx, dz) 方向"对应的 yaw 是 `atan2(-dx, -dz)`。
 * 写成 `atan2(dx, dz)` 的话视角会差 180° —— 一进漫游就背对着房间。
 */
export function lookYawTowards(dx: number, dz: number): number {
  return Math.atan2(-dx, -dz)
}


// ══════════════════════════════════════════════════════════════════
// 相机视场角
// ══════════════════════════════════════════════════════════════════
//
// ⚠️ **放在这个文件里，不放 rig.ts。**
//
// `rig.ts` 顶部 import 了 three（570KB）。`SceneViewer` 只是在模板里
// 显示一个默认值，如果从 rig.ts 引这个常量，**three 就被静态拉回主包** ——
// ParseView 的分包会从 45KB 涨回 437KB，而动态 import 的意义全没了。
//
// 实测踩过：加完门和视野控制之后重新构建，ParseView 从 45KB 变成 437KB。
// 这个文件不 import three，放这里就安全。

/**
 * 默认**水平**视场角（度）。
 *
 * ══════════════════════════════════════════════════════════════════
 * ⚠️ 这里踩过一个坑：Three.js 的 `fov` 是**垂直**的
 * ══════════════════════════════════════════════════════════════════
 * 初版直接写 `new PerspectiveCamera(68, aspect, ...)`，心里想的是
 * "68 度，跟游戏差不多"。但文档原话是
 * *vertical field of view, from bottom to top* —— **垂直**视场角。
 *
 * 68° 垂直换算成水平是 `2·atan(tan(34°)·1.7) ≈ 98°` —— 超广角。
 * 而广角会让东西看起来**更小更远**。用户的原话是"感觉房间都很小"，
 * 可房间的实际尺寸是对的（实测主卧算出来 3.99×3.10m，图纸上 4.0×3.2m）。
 * **不是几何错了，是镜头错了。**
 *
 * 人坐在屏幕前看，水平 70–80° 最接近"身临其境"。
 */
export const DEFAULT_HFOV_DEG = 78

/** 水平视场角的可调范围。再窄像望远镜，再宽像鱼眼。 */
export const MIN_HFOV_DEG = 50
export const MAX_HFOV_DEG = 100

/** 由水平视场角与画布宽高比反算**垂直**视场角（度）。 */
export function verticalFovDeg(hFovDeg: number, aspect: number): number {
  const h = (hFovDeg * Math.PI) / 180
  const v = 2 * Math.atan(Math.tan(h / 2) / Math.max(aspect, 0.2))
  return (v * 180) / Math.PI
}
