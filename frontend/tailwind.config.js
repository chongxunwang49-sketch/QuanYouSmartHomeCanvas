/**
 * 设计令牌 —— 全部来自 ui参考/stitch_ai/botanical_warmth_natural_living/DESIGN.md
 * 与三份 code.html 的 tailwind.config 内联块。
 *
 * ⚠️ **这些值不要随手改。** 它们不是"选的颜色"，是一套有立场的配色：
 * DESIGN.md 明确写了「禁止纯黑、禁止霓虹渐变、禁止科技感冷灰，只做浅色
 * 阳光模式」。任何往色板里塞高饱和色或 #000 的改动，都会同时破坏
 * 十几处依赖色阶关系的对比度。
 *
 * 例如 `wood-dark` 是 #2C2418 而不是 #000 —— 正文用纯黑会在这套
 * 暖色画布上显得又硬又脏，这是刻意的。
 */
/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // ── 主色：叶绿。主按钮 / 进度 / 热点 / 选中态 ──
        botanical: '#4A7C59',
        'botanical-hover': '#3F6D4D', // 主按钮 hover（设计稿里写死的 #3f6d4d）
        'botanical-light': '#E8F0E5', // 侧栏选中底 / 标签底
        'botanical-surface': '#F3F8F1', // 推荐卡数值条 / 矩阵高亮列

        // ── 栗棕：品牌与次要强调 ──
        wood: '#6B4F3A',
        'wood-dark': '#2C2418', // 正文与标题 —— 不是纯黑
        'wood-muted': '#7A6E5E', // 次级文字
        'wood-light': '#FAF3E1', // 高端标签底

        // ── 暖色底 ──
        'warm-bg': '#FAF8F3', // 蛋壳画布
        'warm-sidebar': '#F5F2EC', // 亚麻：侧栏 / 卡片内嵌条
        'warm-border': '#EDE8E0',
        'warm-grid': '#EFEAE2', // 表格分隔线

        // ── 点缀 ──
        'accent-gold': '#C9A961',
        'accent-red': '#C85A48',
      },
      borderRadius: {
        // 设计稿的 rounded-* 比 Tailwind 默认小一档，照搬
        DEFAULT: '0.25rem',
        lg: '0.5rem',
        xl: '0.75rem',
        '2xl': '1rem',
        full: '9999px',
      },
      fontFamily: {
        sans: ['Manrope', 'Inter', 'Noto Sans SC', 'sans-serif'],
        // 衬线只给标题和方案名用 —— 全文用衬线会失去"界面感"
        serif: ['Newsreader', 'Noto Serif SC', 'serif'],
        mono: ['JetBrains Mono', 'Consolas', 'monospace'],
      },
      boxShadow: {
        // 设计稿全站阴影都带绿色调，没有一处是中性黑
        xs: '0 1px 2px rgba(44, 36, 24, 0.04)',
        sm: '0 1px 3px rgba(44, 36, 24, 0.06), 0 1px 2px rgba(44, 36, 24, 0.04)',
        md: '0 4px 12px rgba(44, 36, 24, 0.08)',
        lg: '0 8px 24px rgba(44, 36, 24, 0.10)',
        // 推荐方案的专属投影：绿色调，用来把它从另外两张卡里"抬"起来
        botanical: '0 8px 24px rgba(74, 124, 89, 0.12)',
      },
      transitionTimingFunction: {
        out: 'cubic-bezier(0.16, 1, 0.3, 1)',
      },
      keyframes: {
        'fade-up': {
          from: { opacity: '0', transform: 'translateY(6px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        'fade-up': 'fade-up 0.28s cubic-bezier(0.16, 1, 0.3, 1)',
      },
    },
  },
  plugins: [],
}
