<script setup lang="ts">
import AppIcon from './AppIcon.vue'

/**
 * 降级提示。
 *
 * ⚠️ **这不是一个可选的装饰。** 需求文档 AC-17 的原话是
 * 「静默降级 = 欺骗用户」——系统走了本地兜底、少调了模型、或者某个
 * 分支超时没产出时，用户必须看得见，否则他会以为自己拿到的是一份
 * 完整结果。
 *
 * 所以这个组件**不提供"关闭"按钮**，也不做成可折叠的。理由：
 * 一个能被点掉的警告，在演示和真用的时候都会被第一批点掉。
 * 它只在 `degraded=true` 时出现，用户看到它的时候，确实有事发生了。
 */
withDefaults(
  defineProps<{
    /** 后端给的降级原因，原样展示 —— 不要改写成"系统繁忙"这种糊弄话 */
    reasons?: string[]
    /** 更短的标题，用于局部（比如单张方案卡）*/
    compact?: boolean
  }>(),
  { reasons: () => [], compact: false },
)
</script>

<template>
  <div
    class="rounded-xl border border-accent-gold/40 bg-wood-light/50"
    :class="compact ? 'p-3' : 'p-3.5'"
  >
    <div class="flex items-start gap-2.5">
      <AppIcon name="warning-circle" :size="compact ? 16 : 20" class="mt-0.5 shrink-0 text-accent-gold" />
      <div class="min-w-0 flex-1">
        <p class="text-[13px] font-bold text-wood-dark">
          {{ compact ? '本项已降级产出' : '本次结果为降级模式' }}
        </p>
        <p class="mt-0.5 text-[11px] leading-relaxed text-wood-muted">
          {{
            compact
              ? '部分内容来自兜底路径，完整度低于正常水平。'
              : '系统在部分环节走了兜底路径（本地模型 / 规则回退 / 分支超时）。结果仍可用，但完整度低于正常水平——请按下面的原因逐条判断。'
          }}
        </p>
        <ul v-if="reasons.length" class="mt-2 space-y-1">
          <li
            v-for="(r, i) in reasons"
            :key="i"
            class="flex items-start gap-1.5 text-[11px] text-wood"
          >
            <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent-gold" />
            <span class="min-w-0 break-words font-mono">{{ r }}</span>
          </li>
        </ul>
      </div>
    </div>
  </div>
</template>
