<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '@/components/AppIcon.vue'
import { imageAttribution, imagePool } from '@/assets/images/pool'
import { useAuthStore } from '@/stores/auth'

/**
 * 登录页（AC-01）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 版式取自全友官网首页，配色用本站令牌
 * ══════════════════════════════════════════════════════════════════
 * 官网首页的视觉语言（2026-09-23 用无头浏览器取计算样式实测，不是看源码猜的）
 * 抽出来是四件套，本页逐件还原：
 *
 *   ① **品牌绿标题** —— 官网用 #00C7A1，本站令牌是 `botanical` (#4A7C59)。
 *      同一个位置、同一个用法，只换色值。
 *   ② **居中标题下方的短下划线** —— 官网的"艺术字"其实就是这件东西：
 *      一段两端对齐的短横条 + 大字号标题。本页保留（`.rule`）。
 *   ③ **超大描边装饰数字** —— 官网用它在分区里落一个几乎只有轮廓的
 *      巨大序号。本页拿它做轮播的当前序号，**既有装饰作用又承担信息**，
 *      而不是纯贴图。
 *   ④ **浅色分区块 + 充裕留白** —— 官网分区底 #F7F7F7、卡片浅底 #EEF5F5。
 *      本页用 `warm-sidebar` / `white`，间隔一律放大一档。
 *
 * ⚠️ **组件样式（圆角 / 阴影 / 边框）沿用本站令牌，没有照搬官网。**
 * 官网的按钮圆角 ≤2px（近乎直角），照搬过来会让登录页看起来**不是
 * 这个产品的一部分** —— 设计系统的一致性比单页像不像官网更重要。
 * 字体层级、间距比例、装饰件这些"版式"的东西才是官网真正的辨识度所在，
 * 也才是本页还原的部分。
 *
 * ══════════════════════════════════════════════════════════════════
 * 轮播图用的是「图片池」，不是直接 import
 * ══════════════════════════════════════════════════════════════════
 * `assets/images/pool.ts` 已经处理了版权问题：本地有全友实拍就用实拍，
 * 公开仓库里那批图被 `.gitignore` 挡在外面，自动回落到 Pexels。
 * **不要绕过它去 import 具体文件** —— 那会让公开仓库里的首屏裂图。
 *
 * 连带的一条：图注**必须**跟着 `imageAttribution` 走。回落到 Pexels 时
 * 还写"全友实景"就是虚假标注。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么把演示账号明文列出来
 * ══════════════════════════════════════════════════════════════════
 * 这是本地演示系统，`seed_data/users.json` 里就是虚构账号。
 * 面试官需要**当场**看到"不同权限登录后界面不同"，把口令藏起来只会
 * 让演示卡在第一步。列出来并且把每个账号能看到什么写清楚，
 * 比藏起来更专业 —— 藏起来反而像是忘了做。
 */

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

// ── 表单 ────────────────────────────────────────────────
const username = ref('')
const password = ref('')
/** 表单校验失败（空值）时的提示。**与登录失败分开** —— 一个是"你没填"，一个是"填错了" */
const formHint = ref('')

/**
 * 登录成功后去哪。
 *
 * ⚠️ **必须校验，不能直接 `router.replace(route.query.redirect)`。**
 * `//evil.com` 是协议相对地址，浏览器会把它解析成 `https://evil.com` ——
 * 一个 `?redirect=//evil.com` 的链接就能把登录成功的人送到外站。
 * 只接受以单个 `/` 开头的站内路径。
 */
const safeRedirect = computed(() => {
  const raw = route.query.redirect
  const path = typeof raw === 'string' ? raw : ''
  if (path.startsWith('/') && !path.startsWith('//')) return path
  return '/'
})

async function submit() {
  formHint.value = ''
  if (!username.value.trim()) {
    formHint.value = '请输入用户名'
    return
  }
  if (!password.value) {
    formHint.value = '请输入口令'
    return
  }
  const ok = await auth.login(username.value, password.value)
  if (ok) await router.replace(safeRedirect.value)
}

function fill(name: string, pwd: string) {
  username.value = name
  password.value = pwd
  formHint.value = ''
  auth.error = ''
}

// ── 演示账号 ────────────────────────────────────────────
const DEMO_ACCOUNTS = [
  {
    username: 'admin',
    password: 'admin123',
    name: '管理员',
    tag: '全部不限量',
    tone: 'botanical',
    avatar: '管',
    can: '所有功能，且不受每日配额约束',
  },
  {
    username: 'designer',
    password: 'designer123',
    name: '设计师',
    tag: '全部不限量',
    tone: 'botanical',
    avatar: '设',
    can: '所有功能，且不受每日配额约束',
  },
  {
    username: 'vip',
    password: 'vip123',
    name: '普通用户 · 会员',
    tag: '功能全开',
    tone: 'gold',
    avatar: '会',
    can: '全部功能可用，但仍受每日配额保护',
  },
  {
    username: 'demo',
    password: 'demo123',
    name: '普通用户 · 免费版',
    tag: '受限',
    tone: 'muted',
    avatar: '免',
    can: '解析 5 次/日；方案生成与避坑审查需开通',
  },
] as const

// ── 左侧实景图墙 ────────────────────────────────────────
/**
 * 左栏的背景是**滚动着的实景图**：两列竖向反向缓慢滚动。
 *
 * ⚠️ **只用 6 张，不是全部。** 图片池现在有 12 张（约 2.3MB），
 *    全铺进背景会让登录页一进来就拉满整个池子 —— 而左栏是背景，
 *    用户不会逐张去看。取 6 张（每列 3 张）既能铺满两列，也让首屏
 *    的图片体积只有全量的一半。这是取舍，不是遗漏。
 *
 * ⚠️ **文字不进这个容器**（见模板）：图墙是 `position:absolute` 的
 *    独立图层，品牌行与标题在外面的内容层里 —— 图怎么滚都推不动、
 *    也盖不住左上角那行字。
 */
const WALL_PER_COLUMN = 3
const wallColumns = computed(() => {
  const all = imagePool.main
  if (!all.length) return [] as string[][]
  const cols: string[][] = [[], []]
  for (let i = 0; i < Math.min(all.length, WALL_PER_COLUMN * 2); i++) {
    cols[i % 2].push(all[i])
  }
  return cols.filter((c) => c.length)
})

/**
 * 每列的滚动时长。**按图片数量算，让两列线速度接近** ——
 * 两列用同一个秒数的话，图片多的那列会明显更快。
 * 第二列再错开一点，避免两列看起来像一整块在平移。
 */
function wallDuration(col: number, count: number): string {
  const base = Math.max(4, count) * 16
  return `${col === 1 ? base + 8 : base}s`
}

/**
 * 品牌主张。**说的是本系统的立场，不是给全友安的话。**
 *
 * 官网首页那一段"艺术字 + 图片"讲的是企业理念。本页要还原的是那个
 * **装置**（大字 + 短下划线的居中标题），但内容不能替全友编企业口号 ——
 * 那是把没有出处的话挂在别人名下。所以换成这套系统自己的主张，
 * 每一句都有代码可对应。
 */
const BELIEFS = [
  { icon: 'blueprint', title: '先把户型看懂', desc: '房间、墙体、门窗、尺寸先落到结构上，再谈风格' },
  { icon: 'coins', title: '每一分钱都可追溯', desc: '预算由规则引擎算，不由模型编；材料项逐条可查' },
  { icon: 'shield-check', title: '风险先于报价被指出', desc: '避坑审查的每条结论都必须能引回知识库原文' },
] as const
</script>

<template>
  <!--
    ⚠️ **这一层必须能纵向滚。** 原来是 `overflow-hidden`，配上 `min-h-full`：
    浏览器窗口不高的时候（实测视口高 460px），两栏的内容各自有 797px 高 ——
    于是整页比视口高 337px，而根节点又是 `overflow-hidden`，
    **下面那 337px 永远看不到也滚不到**：演示账号卡与「登录」按钮全在里面。
    用户的原话就是「浏览器没开全屏时滑轮滑不到最下面，按不到下面的按钮」。

    修法：根节点 `h-full` + 纵向可滚（横向仍然裁 —— 图墙的横向滚动条不该出现在页面上）。
    `h-full` 让根节点的高度是**确定的**（= 外层的高度），内容超出才滚；
    窗口够高时内容本来就装得下，不会出现多余滚动条。
  -->
  <main class="relative flex h-full w-full overflow-y-auto overflow-x-hidden scroll-thin bg-warm-bg">
    <!-- ══════════════════════════════════════════════════════
         左栏：品牌与实拍
         ══════════════════════════════════════════════════════ -->
    <section
      class="login-left relative hidden min-w-0 flex-1 flex-col justify-between overflow-hidden px-14 py-12 lg:flex xl:px-20"
    >
      <!--
        ══ 背景：滚动的实景图墙（两列，一列上、一列下）══

        ⚠️⚠️ **三个「必须这样写」，都是为了左上角那行字不闪：**

          ① 图墙是 `absolute inset-0` 的**独立图层**，不参与内容布局 ——
             它怎么滚都不会推动文字，也不会把它挤出视口。
          ② **不用带 `mask-image` 的 `.marquee`**：遮罩层每帧重绘会把压在
             它上面的兄弟元素擦掉一帧，肉眼就是"文字消失一下又回来"。
             边缘渐隐改由「上下两道渐变压暗」实现（见下面的遮罩层）。
          ③ 内容层带 `transform-gpu`（`translateZ(0)`），自己占一个合成层，
             与图墙的动画互不干扰。

        ⚠️ `pointer-events-none` + `aria-hidden`：这是**背景**，不是可点区域，
           屏幕阅读器也不该念一串没有 alt 的图。
      -->
      <div class="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden="true">
        <div class="flex h-full w-full gap-4">
          <div
            v-for="(col, ci) in wallColumns"
            :key="`col-${ci}`"
            class="wall flex-1"
            :class="ci === 1 && 'wall-reverse'"
          >
            <div
              class="wall-track"
              :style="{ animationDuration: wallDuration(ci, col.length) }"
            >
              <!--
                渲染两份同样的列表，位移 -50% 才能无缝循环。
                第二份 `aria-hidden`（外层已经 aria-hidden，这里只是标明意图）。

                ⚠️ 间距用每项自带的 `mb-4`，**不是** 轨道的 `gap` ——
                   见 style.css 里 `.wall` 的注释（用 gap 会在接缝处跳）。
              -->
              <template v-for="copy in 2" :key="`c${ci}-${copy}`">
                <img
                  v-for="src in col"
                  :key="`c${ci}-${copy}-${src}`"
                  :src="src"
                  alt=""
                  loading="lazy"
                  decoding="async"
                  class="mb-4 aspect-[4/3] w-full rounded-2xl object-cover"
                />
              </template>
            </div>
          </div>
        </div>

        <!--
          ══ 边缘渐隐（唯一压在图上的一层，且很淡）══
          让滚动中的图从上下边缘柔和进出，不是硬切。
        -->
        <div class="absolute inset-x-0 top-0 h-16 bg-gradient-to-b from-warm-bg/35 to-transparent" />
        <div class="absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-warm-bg/35 to-transparent" />
      </div>

      <!--
        ══ 内容层 ══
        `relative z-10 transform-gpu`：压在背景之上，并且**自己一个合成层**
        （`transform-gpu` 是 `translateZ(0)`），图墙的动画不会让它重绘。

        ⚠️ **内部不再是 `justify-between`。** 以前三段均分（品牌行顶 / 标题居中 /
        声明兜底），标题被推到垂直中间 —— 实测标题块的顶边在 y=293，
        离品牌行的底边有 207px，中间空着一大块。需求方要求"让标题紧挨着上面那行字"，
        所以改成：品牌行贴顶、标题用 `mt-4` 紧跟其后、声明用 `mt-auto` 压到底。

        ⚠️ **品牌行的 `px-4 py-2.5` 刻意留着。** 这块原来是带底板的卡片
        （`bg-warm-bg/80`），底板按需求去掉了，但内边距保留 ——
        它现在是不可见的内缩，作用就是**让左上角那行字的坐标与改版前逐像素相同**
        （需求方明确要求"上面的文字不动"）。去掉它品牌文字会左移 16px。
      -->
      <div class="relative z-10 flex min-h-0 flex-1 flex-col transform-gpu">
        <!-- 品牌行 -->
        <div class="flex w-fit items-center gap-3 px-4 py-2.5">
        <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-botanical text-white">
          <AppIcon name="leaf" :size="19" weight="bold" />
        </span>
        <div class="leading-tight">
          <div class="font-serif text-[17px] font-bold tracking-wide text-wood-dark">
            全友 · 智绘家
          </div>
          <!--
            ⚠️ 这行英文原来也是 `text-wood-muted`（暖灰），底板去掉之后压在照片上
            就认不出了，所以按"这一类文字改纯白"一并处理。
            「全友 · 智绘家」那行按需求方要求**保持原样不动**。
          -->
          <div class="text-[10px] font-semibold uppercase tracking-[0.22em] text-white">
            QuanYou Smart HomeCanvas
          </div>
        </div>
      </div>

      <!--
        艺术字标题：大字号衬线 + 短下划线（官网的标志性装置）。

        ⚠️ **这一块原来带一层暖色衬底**（`bg-warm-bg/85 rounded-3xl p-6`），
           是为了"图恢复原样"与"字读得清"两全。需求方看下来觉得那块底
           压在照片上突兀，明确要求去掉 —— 去掉之后靠**把这一类文字改成纯白**
           来保证可读性。

        ⚠️ `px-4` 是配合品牌行留的：品牌行内边距也是 `px-4`，
           这样标题与品牌图样**左边缘对齐在同一条竖线上**（都在 x=96）。
           原来靠底板把两块"看起来"对齐，底板一撤就得让文字自己对齐。

        ⚠️ **这一块是纯白字，没有描边、也没有底板。**
           白字压到亮照片上（图池里有白厨房、白卫浴那几张）会有几秒看不清 ——
           先加过一版四向描边来救，需求方看过之后明确不要黑边
           （"改成白色无黑白的艺术字，字体大小和布局不变"），所以去掉了。
           字号与排版从头到尾没动过。真要恢复可读性，加回一层 `text-shadow`
           即可（历史版本在 git 里）。
      -->
      <div class="mt-4 max-w-xl animate-fade-up px-4">
        <h1 class="font-serif text-[42px] font-bold leading-[1.18] text-wood-dark xl:text-[52px]">
          让每一张户型图<br />
          <span class="text-botanical">都能长出理想的家</span>
        </h1>

        <!-- 短下划线。官网在居中标题下方用一段两端收尖的短横条，本页保留 -->
        <span class="rule mt-6 block h-[3px] w-20 rounded-full bg-botanical/70" />

        <p class="mt-6 max-w-md text-[13px] leading-relaxed text-white">
          上传一张户型图，系统完成多模态解析、五维诊断、三套方案并行生成、
          预算测算与避坑审查 —— 全程可追溯到每一条结论的依据。
        </p>

        <!--
          三项主张。

          ⚠️ 小标题（先把户型看懂 / 每一分钱都可追溯 / 风险先于报价被指出）
             **保持 `text-wood-dark` 不动** —— 需求方点名不要动这几行。
             跟着变白的是它们下面那行说明（与副标题同属一类文字）。
        -->
        <ul class="mt-8 space-y-3.5">
          <li v-for="b in BELIEFS" :key="b.title" class="flex items-start gap-3">
            <span
              class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg
                     border border-botanical/20 bg-botanical-light text-botanical"
            >
              <AppIcon :name="b.icon" :size="15" weight="bold" />
            </span>
            <div class="min-w-0">
              <div class="text-[13px] font-semibold text-wood-dark">{{ b.title }}</div>
              <div class="text-[12px] leading-relaxed text-white">{{ b.desc }}</div>
            </div>
          </li>
        </ul>
      </div>

        <!--
          底部：图片来源声明。

          ⚠️ **轮播卡片已经删掉了** —— 那批实景图现在就是左栏的背景。
             再摆一个轮播就是同一批图出现两次，而且卡片会挡住背景。
             声明这一行必须留着：换成 Pexels 回落图时，写"全友实景案例"
             就是把没有出处的东西挂在别人名下（见 `assets/images/pool.ts`）。
             图池为空（既没有案例图也没有回落照片）时这一行也不显示。

          ⚠️ `mt-auto` 把它压到左栏底部 —— 内容层不再是 `justify-between` 之后，
             底部这一行得自己声明"我在最后"。它的暖色衬底同样按需求去掉，
             改用白字 + 投影。
        -->
        <p
          v-if="imagePool.main.length"
          class="mt-auto w-fit max-w-md px-3 py-1.5
                 text-[11px] leading-relaxed text-white"
        >
          {{ imageAttribution }}
        </p>
      </div>
    </section>

    <!-- ══════════════════════════════════════════════════════
         右栏：登录表单
         ══════════════════════════════════════════════════════ -->
    <section
      class="flex w-full shrink-0 flex-col justify-center overflow-y-auto scroll-thin
             border-l border-warm-border bg-white px-7 py-10 lg:w-[440px] xl:w-[480px] xl:px-12"
    >
      <!-- 窄屏时的品牌行（左栏被隐藏了，这里得补上） -->
      <div class="mb-8 flex items-center gap-3 lg:hidden">
        <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-botanical text-white">
          <AppIcon name="leaf" :size="19" weight="bold" />
        </span>
        <div class="font-serif text-[17px] font-bold text-wood-dark">全友 · 智绘家</div>
      </div>

      <h2 class="font-serif text-[26px] font-bold text-wood-dark">欢迎回来</h2>
      <span class="rule mt-3 block h-[3px] w-12 rounded-full bg-botanical/70" />
      <p class="mt-3 text-[12px] text-wood-muted">
        登录后按账号的<span class="font-semibold text-wood">角色</span>与
        <span class="font-semibold text-wood">会员档位</span>呈现对应的功能可见性。
      </p>

      <form class="mt-7 space-y-4" novalidate @submit.prevent="submit">
        <label class="block">
          <span class="mb-1.5 block text-[12px] font-semibold text-wood-dark">用户名</span>
          <input
            v-model="username"
            type="text"
            autocomplete="username"
            class="field h-11 px-3.5"
            placeholder="admin"
            :disabled="auth.submitting"
          />
        </label>

        <label class="block">
          <span class="mb-1.5 block text-[12px] font-semibold text-wood-dark">口令</span>
          <input
            v-model="password"
            type="password"
            autocomplete="current-password"
            class="field h-11 px-3.5"
            placeholder="••••••"
            :disabled="auth.submitting"
          />
        </label>

        <!-- 错误一律**原样展示后端那句话**，不自作聪明地猜原因。
             后端刻意不区分"用户不存在"与"口令错误"—— 区分开就是
             一个用户名枚举接口。 -->
        <div
          v-if="formHint || auth.error"
          class="flex items-start gap-2 rounded-xl border border-accent-red/30
                 bg-accent-red/5 px-3 py-2.5"
          role="alert"
        >
          <AppIcon name="warning-circle" :size="16" class="mt-px shrink-0 text-accent-red" />
          <p class="text-[12px] leading-relaxed text-accent-red">
            {{ formHint || auth.error }}
          </p>
        </div>

        <button
          type="submit"
          class="btn-primary h-11 w-full gap-2 text-[14px]"
          :disabled="auth.submitting"
        >
          <AppIcon
            :name="auth.submitting ? 'spinner' : 'arrow-right'"
            :size="16"
            weight="bold"
            :class="auth.submitting ? 'animate-spin' : ''"
          />
          {{ auth.submitting ? '正在验证…' : '登 录' }}
        </button>
      </form>

      <!-- 演示账号 -->
      <div class="mt-8 border-t border-warm-border pt-6">
        <div class="flex items-baseline justify-between">
          <h3 class="text-[12px] font-semibold text-wood-dark">演示账号</h3>
          <span class="text-[10px] text-wood-muted">点击即填入表单</span>
        </div>
        <p class="mt-1 text-[11px] leading-relaxed text-wood-muted">
          不同角色登录后看到的菜单与可用操作不同，可以逐个对比。
        </p>

        <ul class="mt-3 space-y-2">
          <li v-for="a in DEMO_ACCOUNTS" :key="a.username">
            <button
              type="button"
              class="group flex w-full items-center gap-3 rounded-xl border border-warm-border
                     bg-warm-sidebar/50 px-3 py-2.5 text-left transition-all duration-150
                     hover:border-botanical/30 hover:bg-botanical-light/60"
              @click="fill(a.username, a.password)"
            >
              <!-- 用「管 / 设 / 会 / 免」这类**语义缩写**，不用用户名前两个字母 ——
                   后者会撞车（designer 和 demo 都是 "DE"），看起来像复制粘贴错了 -->
              <span
                class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-white
                       text-[13px] font-bold text-wood"
              >
                {{ a.avatar }}
              </span>
              <span class="min-w-0 flex-1">
                <span class="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <span class="text-[12px] font-semibold text-wood-dark">{{ a.username }}</span>
                  <span
                    class="rounded-md px-1.5 py-px text-[10px] font-semibold"
                    :class="{
                      'bg-botanical-light text-botanical': a.tone === 'botanical',
                      'bg-wood-light text-wood': a.tone === 'gold',
                      'bg-warm-border/70 text-wood-muted': a.tone === 'muted',
                    }"
                  >
                    {{ a.tag }}
                  </span>
                </span>
                <!-- ⚠️ **不要 truncate。** 这段是"这个账号能做什么/不能做什么"，
                     截断它等于把权限说明藏起来 —— 一个截掉尾巴的
                     "方案生成与避坑审查需…" 比不写还糟。宁可换行。 -->
                <span class="mt-0.5 block text-[11px] leading-snug text-wood-muted">
                  {{ a.name }} · {{ a.can }}
                </span>
              </span>
              <AppIcon
                name="arrow-right"
                :size="14"
                class="shrink-0 text-wood-muted transition-transform
                       group-hover:translate-x-0.5 group-hover:text-botanical"
              />
            </button>
          </li>
        </ul>
      </div>
    </section>
  </main>
</template>

<style scoped>
/*
  轮播那套 `.slide-fade-*` 过渡样式已经删掉 —— 轮播换成了左栏的滚动图墙。
  图墙的动画在全局 `style.css` 里（`.wall` / `.wall-track`），不在这个
  scoped 块里：那里还写清了「为什么不用带 mask 的 `.marquee`」这个坑。
*/

/*
  窗口矮的时候（实测视口 460px）左栏的内容比框高 35px —— 左栏根是
  `overflow-hidden`（图墙必须裁），那 35px 就永远看不到，正好压在
  「素材来自…」那行声明上。

  这里不引入滚动条（左栏是品牌区，滚起来很怪），而是**把纵向留白收紧**：
  上下内边距与标题上方的间距各收一档，实测能收回 40px 以上，刚好够。
  阈值取 620px：比常见笔记本视口（约 640~700）低一点，避免正常窗口下也变样。
*/
@media (max-height: 620px) {
  .login-left {
    padding-top: 1.75rem;
    padding-bottom: 1.75rem;
  }
  .login-left h1 {
    font-size: 2rem;
    line-height: 1.2;
  }
  .login-left ul {
    margin-top: 1rem;
  }
}
</style>
