import { onUnmounted, ref, shallowRef } from 'vue'

/**
 * 帧率测量 + 显卡探测 + **自动降质**。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么这不是"锦上添花的调试信息"
 * ══════════════════════════════════════════════════════════════════
 * 3D 漫游是**唯一一个把画质与流畅度押在用户机器上**的功能。
 * 后端那些功能（解析、出图、预算）跑在本机 CPU/GPU 上，性能是可控的；
 * 而 WebGL 跑在**浏览器**里，用的是哪块显卡、有没有开硬件加速、
 * 显示器缩放是多少 —— 这三件事我们事先都不知道，而且它们能差出十倍。
 *
 * 所以这里的原则是：**先量，再决定**。
 *   · 量出来是软件渲染（SwiftShader）→ 直接告诉用户去开硬件加速，别硬跑
 *   · 量出来帧率不达标 → 自动降一档画质（降分辨率倍率、关阴影、关动态光）
 *   · 全程把帧率和**实际使用的显卡**显示出来 —— 演示时这是加分项，
 *     出问题时这是唯一的线索
 *
 * ══════════════════════════════════════════════════════════════════
 * 本机实测的硬件情况（2026-09-23）
 * ══════════════════════════════════════════════════════════════════
 *     i7-11800H / 16GB / RTX 3050 Laptop 4GB + Intel UHD 双显卡
 *     显示器逻辑分辨率 1536×864 → **Windows 缩放 125%**
 *
 * 最后一条要专门处理：`devicePixelRatio` = 1.25 意味着同样的 CSS 尺寸下，
 * WebGL 要渲染的像素多 56%。不封顶的话画布越大越吃亏，而用户只看到
 * "有点卡"。所以 DPR 一律封顶。
 */

/** 画质档位。数值越小越省。 */
export type QualityTier = 0 | 1 | 2

export interface GpuInfo {
  /** `WEBGL_debug_renderer_info` 给出的真实显卡名 */
  renderer: string
  vendor: string
  webglVersion: 1 | 2
  /** 疑似软件渲染（没有可用的 GPU） */
  software: boolean
}

/** 低于这个帧率就降档。30 是"看起来还算流畅"的下限。 */
const DEGRADE_BELOW_FPS = 30
/** 降档前先观察这么久 —— 避免把首帧的抖动当成性能不足。 */
const SAMPLE_WINDOW_MS = 3000

/**
 * DPR 封顶。
 *
 * 本机 DPR = 1.25。封到 1.5 是为了兼顾高分屏（2.0 的屏上不封顶会渲染
 * 4 倍像素）；再低的话在 1.25 缩放的机器上会明显发虚。
 */
export function cappedPixelRatio(tier: QualityTier, dpr = window.devicePixelRatio): number {
  const cap = tier === 0 ? 1.5 : tier === 1 ? 1.0 : 0.75
  return Math.min(dpr, cap)
}

/** 读显卡信息。失败返回 null —— 拿不到不该让整个 3D 挂掉。 */
export function readGpuInfo(gl: WebGLRenderingContext | WebGL2RenderingContext): GpuInfo | null {
  try {
    const dbg = gl.getExtension('WEBGL_debug_renderer_info')
    const renderer = String(
      (dbg && gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)) || gl.getParameter(gl.RENDERER) || '',
    )
    const vendor = String(
      (dbg && gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL)) || gl.getParameter(gl.VENDOR) || '',
    )
    return {
      renderer,
      vendor,
      webglVersion: typeof WebGL2RenderingContext !== 'undefined' &&
        gl instanceof WebGL2RenderingContext ? 2 : 1,
      // SwiftShader / ANGLE 的纯软件后端 / llvmpipe 都是"没有 GPU"
      software: /swiftshader|software|llvmpipe|basic render/i.test(renderer),
    }
  } catch {
    return null
  }
}

export function useFrameStats() {
  const fps = ref(0)
  const avgFps = ref(0)
  const tier = ref<QualityTier>(0)
  const gpu = shallowRef<GpuInfo | null>(null)
  /** 降档时的原因说明。**必须让用户看见** —— 静默降质 = 让用户以为机器不行 */
  const degradeNote = ref('')

  let frames = 0
  let windowStart = 0
  let last = 0
  let dropped = false
  let framesThisSecond = 0
  let secondStart = 0

  function tick(now: number) {
    if (!last) {
      last = now
      windowStart = now
      secondStart = now
      return
    }
    frames++
    framesThisSecond++
    last = now

    if (now - secondStart >= 1000) {
      fps.value = Math.round((framesThisSecond * 1000) / (now - secondStart))
      framesThisSecond = 0
      secondStart = now
    }

    if (now - windowStart >= SAMPLE_WINDOW_MS) {
      const measured = (frames * 1000) / (now - windowStart)
      avgFps.value = Math.round(measured)

      // 每个窗口只降一档，降完重新观察 —— 一次降到底会让画质骤变，
      // 而问题可能只是那一瞬间的后台任务抢了 GPU。
      if (!dropped && measured < DEGRADE_BELOW_FPS && tier.value < 2) {
        const before = tier.value
        tier.value = (tier.value + 1) as QualityTier
        dropped = true
        degradeNote.value =
          `实测 ${Math.round(measured)} fps（低于 ${DEGRADE_BELOW_FPS}），` +
          `已自动降低画质第 ${before + 1} 档以保证流畅度`
      } else if (measured >= DEGRADE_BELOW_FPS) {
        dropped = false
      }
      frames = 0
      windowStart = now
    }
  }

  function reset() {
    frames = 0
    last = 0
    windowStart = 0
    secondStart = 0
    framesThisSecond = 0
    dropped = false
    fps.value = 0
    avgFps.value = 0
    tier.value = 0
    degradeNote.value = ''
  }

  let raf = 0
  function start() {
    const loop = (now: number) => {
      tick(now)
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
  }
  function stop() {
    if (raf) cancelAnimationFrame(raf)
    raf = 0
  }

  onUnmounted(stop)

  return { fps, avgFps, tier, gpu, degradeNote, tick, start, stop, reset }
}
