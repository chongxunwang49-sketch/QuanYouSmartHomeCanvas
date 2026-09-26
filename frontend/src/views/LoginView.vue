<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
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

// ── 轮播 ────────────────────────────────────────────────
const slides = computed(() => imagePool.main)
const current = ref(0)
let timer: ReturnType<typeof setInterval> | null = null
/** 鼠标停在图上时暂停 —— 用户在看哪一张图，不该被自动翻走 */
const paused = ref(false)

const pad2 = (n: number) => String(n + 1).padStart(2, '0')

function stop() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

function start() {
  stop()
  if (slides.value.length < 2) return
  timer = setInterval(() => {
    if (!paused.value) current.value = (current.value + 1) % slides.value.length
  }, 5000)
}

function goto(i: number) {
  current.value = i
  start() // 手动切换后重新计时，否则刚点完立刻又被自动翻走
}

onMounted(start)
onBeforeUnmount(stop)

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
  <main class="relative flex min-h-full w-full overflow-hidden bg-warm-bg">
    <!-- ══════════════════════════════════════════════════════
         左栏：品牌与实拍
         ══════════════════════════════════════════════════════ -->
    <section
      class="relative hidden min-w-0 flex-1 flex-col justify-between overflow-hidden px-14 py-12 surface-glow lg:flex xl:px-20"
    >
      <!-- 品牌行 -->
      <div class="flex items-center gap-3">
        <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-botanical text-white">
          <AppIcon name="leaf" :size="19" weight="bold" />
        </span>
        <div class="leading-tight">
          <div class="font-serif text-[17px] font-bold tracking-wide text-wood-dark">
            全友 · 智绘家
          </div>
          <div class="text-[10px] font-semibold uppercase tracking-[0.22em] text-wood-muted">
            QuanYou Smart HomeCanvas
          </div>
        </div>
      </div>

      <!-- 艺术字标题：大字号衬线 + 短下划线（官网的标志性装置） -->
      <div class="max-w-xl animate-fade-up">
        <h1 class="font-serif text-[42px] font-bold leading-[1.18] text-wood-dark xl:text-[52px]">
          让每一张户型图<br />
          <span class="text-botanical">都能长出理想的家</span>
        </h1>

        <!-- 短下划线。官网在居中标题下方用一段两端收尖的短横条，本页保留 -->
        <span class="rule mt-6 block h-[3px] w-20 rounded-full bg-botanical/70" />

        <p class="mt-6 max-w-md text-[13px] leading-relaxed text-wood-muted">
          上传一张户型图，系统完成多模态解析、五维诊断、三套方案并行生成、
          预算测算与避坑审查 —— 全程可追溯到每一条结论的依据。
        </p>

        <!-- 三项主张 -->
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
              <div class="text-[12px] leading-relaxed text-wood-muted">{{ b.desc }}</div>
            </div>
          </li>
        </ul>
      </div>

      <!-- 实拍轮播 -->
      <div
        class="relative"
        @mouseenter="paused = true"
        @mouseleave="paused = false"
        @focusin="paused = true"
        @focusout="paused = false"
      >
        <div
          v-if="slides.length"
          class="relative h-[240px] overflow-hidden rounded-2xl border border-warm-border shadow-md xl:h-[280px]"
        >
          <!--
            逐张淡入淡出，**同一时刻只有两张图在 DOM 里**。

            ⚠️ 不要写成 `v-for` + `v-show` 把 14 张全挂上：那会让登录页
            一进来就拉 4.9MB 的图（`cases/` 实测 14 张共 4.9MB），
            而这 14 张里用户这次只看得见 1 张。用 `<Transition>` 换 key，
            Vue 会同时保留"正在离开"的那一张 —— 天然就是交叉淡化，
            且初始只 fetch 一张，每次切换只多 fetch 一张。

            `absolute inset-0` 是必须的：两张图要在同一位置重叠，
            否则新图会把容器撑高，形成"往上跳一下"。
          -->
          <Transition name="slide-fade">
            <img
              :key="current"
              :src="slides[current]"
              :alt="`全友家居实景案例 ${current + 1}`"
              class="absolute inset-0 h-full w-full object-cover"
            />
          </Transition>

          <!-- 上下两道压暗。**不是装饰，是让白字在任何一张图上都读得出来** ——
               案例图里有深色木作也有全白厨房，只压一侧的话总有一种会糊掉。
               中间留亮，图本身的主体不被削弱。 -->
          <div
            class="pointer-events-none absolute inset-x-0 top-0 h-1/3
                   bg-gradient-to-b from-wood-dark/50 to-transparent"
          />
          <div
            class="pointer-events-none absolute inset-x-0 bottom-0 h-2/3
                   bg-gradient-to-t from-wood-dark/80 via-wood-dark/35 to-transparent"
          />

          <!-- 超大描边序号：官网的装饰装置，这里同时充当"第几张"。
               ⚠️ 必须落在图**内部**（不能写 `-top-*`）—— 容器是
               `overflow-hidden`，负偏移会被直接裁掉，表现是"这个装饰
               写了但看不见"。 -->
          <span
            class="pointer-events-none absolute left-5 top-2 select-none font-serif
                   text-[84px] font-bold leading-none text-transparent
                   [-webkit-text-stroke:2px_rgba(250,248,243,0.6)]"
          >
            {{ pad2(current) }}
          </span>

          <div class="absolute inset-x-0 bottom-0 flex items-end justify-between gap-4 p-5">
            <p class="max-w-[70%] text-[11px] leading-relaxed text-white/85">
              {{ imageAttribution }}
            </p>
            <!-- 圆点：可点，键盘也能用（<button> 而不是 <span>） -->
            <div class="flex shrink-0 items-center gap-1.5">
              <button
                v-for="(src, i) in slides"
                :key="`dot-${src}`"
                type="button"
                class="h-1.5 rounded-full transition-all duration-200"
                :class="i === current ? 'w-5 bg-white' : 'w-1.5 bg-white/45 hover:bg-white/70'"
                :aria-label="`查看第 ${i + 1} 张实景图`"
                :aria-current="i === current"
                @click="goto(i)"
              />
            </div>
          </div>
        </div>

        <!-- 图片池为空（既没有案例图也没有回落照片）时不留空洞 -->
        <div
          v-else
          class="flex h-[240px] items-center justify-center rounded-2xl border
                 border-dashed border-warm-border bg-warm-sidebar/60"
        >
          <p class="text-[12px] text-wood-muted">未找到实景图（图片池为空）</p>
        </div>
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
          本地演示用的虚构账号，口令明文列出。不同档位登录后看到的菜单与可用操作
          确实不同 —— 权限是按角色真的判过的，不是摆设。
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
/* 轮播的淡入淡出。用 opacity 而不是位移 —— 图片尺寸不一，
   位移会让两张图的接缝露白。 */
.slide-fade-enter-active,
.slide-fade-leave-active {
  transition: opacity 0.7s cubic-bezier(0.16, 1, 0.3, 1);
}
.slide-fade-enter-from,
.slide-fade-leave-to {
  opacity: 0;
}
/* 离开的那张必须在下面，否则它会盖住正在淡入的一张 */
.slide-fade-leave-active {
  z-index: 0;
}
.slide-fade-enter-active {
  z-index: 1;
}
</style>
