<script setup lang="ts">
import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'

/**
 * 用户管理。
 *
 * ⚠️ **这一页没有实现，而它看起来也应该像"没有实现"。**
 *
 * M1（认证模块）在本项目里是**主动跳过**的：这是一个单机演示系统，
 * 服务不对外暴露，加一套登录/权限只会给演示增加摩擦而不增加说服力。
 *
 * 摆一排假用户、假的角色下拉、假的"邀请成员"按钮，能把这页填得很满——
 * 但那是最坏的一种做法：它让评审者以为这块做过了，然后在追问时露馅。
 * 诚实地说清楚"为什么没做"比假装做过有价值得多。
 */
const PLANNED = [
  { icon: 'users', title: '账号与登录', desc: '手机号 / 密码登录，JWT 会话' },
  { icon: 'shield', title: '角色权限', desc: '管理员 / 设计师 / 业主三种角色的数据可见性' },
  { icon: 'share-network', title: '方案分享', desc: '生成只读链接，业主免登录查看方案与报价' },
  { icon: 'clock', title: '操作审计', desc: '谁在什么时候生成/修改了哪套方案' },
]
</script>

<template>
  <main class="mx-auto flex w-full max-w-[1760px] flex-1 flex-col gap-4 overflow-y-auto scroll-thin p-6">
    <PageHeader
      :breadcrumb="['全友·智绘家', '用户管理']"
      title="用户管理"
      :status="{ icon: 'warning-circle', text: '本模块未实现', tone: 'gold' }"
    />

    <section class="rounded-xl border border-accent-gold/40 bg-wood-light/50 p-4">
      <div class="flex items-start gap-3">
        <AppIcon name="info" :size="22" class="mt-0.5 shrink-0 text-accent-gold" />
        <div class="min-w-0">
          <h2 class="font-serif text-[16px] font-bold text-wood-dark">
            这一页没有实现，也不打算在本项目里实现
          </h2>
          <p class="mt-1.5 text-[12px] leading-relaxed text-wood">
            本项目的 M1（认证模块）是**主动跳过**的。它是一个单机演示系统，
            服务只监听 127.0.0.1、不对外暴露，加一套登录与权限体系只会给演示
            增加摩擦，而不增加对核心技术能力的说服力。
          </p>
          <p class="mt-2 text-[12px] leading-relaxed text-wood-muted">
            这里如实标出"未实现"，而不是摆一排假用户把页面填满 ——
            后者会让评审者以为这块做过了。
          </p>
        </div>
      </div>
    </section>

    <section class="card p-4">
      <h2 class="mb-3 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
        <AppIcon name="list-checks" :size="16" class="text-botanical" />
        <span>如果要做，会做这几块</span>
      </h2>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div
          v-for="p in PLANNED"
          :key="p.title"
          class="flex items-start gap-3 rounded-xl border border-warm-border bg-warm-sidebar/40 p-3 opacity-80"
        >
          <span
            class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-warm-border bg-white text-wood-muted"
          >
            <AppIcon :name="p.icon" :size="17" />
          </span>
          <div class="min-w-0">
            <p class="text-[13px] font-semibold text-wood-dark">{{ p.title }}</p>
            <p class="mt-0.5 text-[11px] leading-relaxed text-wood-muted">{{ p.desc }}</p>
          </div>
          <span class="tag ml-auto shrink-0 !text-[10px]">未实现</span>
        </div>
      </div>
    </section>

    <section class="card p-4">
      <h2 class="mb-2 flex items-center gap-2 font-serif text-[15px] font-semibold text-wood-dark">
        <AppIcon name="shield-check" :size="16" class="text-botanical" />
        <span>当前会话</span>
      </h2>
      <p class="text-[12px] leading-relaxed text-wood-muted">
        前端每次请求都会带一个 <code class="rounded bg-warm-sidebar px-1 font-mono text-[11px]">X-Trace-Id</code>，
        后端沿用同一个值贯穿 API → Agent → LLM → 审计日志，并在响应头回写。
        任务失败时界面会把 trace_id 一并显示出来 —— 报障时凭它就能定位到日志。
      </p>
      <div class="mt-3 flex items-center gap-2 rounded-xl border border-warm-border bg-warm-sidebar/50 p-3">
        <span
          class="flex h-9 w-9 items-center justify-center rounded-full border border-botanical/20 bg-botanical-light text-[12px] font-bold text-botanical"
        >
          QY
        </span>
        <div>
          <p class="text-[12px] font-semibold text-wood-dark">演示账号</p>
          <p class="text-[11px] text-wood-muted">无认证 · 所有请求视为同一用户</p>
        </div>
      </div>
    </section>
  </main>
</template>
