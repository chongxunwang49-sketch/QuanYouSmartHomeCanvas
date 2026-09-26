<script setup lang="ts">
import { ref } from 'vue'

import AppIcon from './AppIcon.vue'
import { cancelTask } from '@/api'
import { messageOf } from '@/api/client'
import { toast } from '@/utils/toast'

/**
 * 「中断」按钮 + 确认框（AC-31）。
 *
 * ══════════════════════════════════════════════════════════════════
 * 为什么是"用户按"而不是"系统到点就杀"
 * ══════════════════════════════════════════════════════════════════
 * 原来的口径是停机时主动 `cancel()` 在飞任务，实测它会把任务留在
 * `processing` 上（界面永远"正在分析图片…"）。现在的口径是
 * **取消权归用户**：系统不猜用户愿意等多久，把决定权交回去。
 *
 * 所以这个按钮的定位不是"紧急出口"，而是"我不想再等了" ——
 * 配合进度区那个会自己修正的剩余时间（见 PhaseProgress），
 * 用户在按之前是有依据的：他知道还剩大概多久。
 *
 * ══════════════════════════════════════════════════════════════════
 * 确认框必须提前写明"额度不退"
 * ══════════════════════════════════════════════════════════════════
 * 额度在**建任务之前**就已经扣了（`deps.consume_quota` 的顺序是有意的：
 * 先校验入参再扣，免得一个拼错的请求白扣一次）。等用户按完再说
 * "哦对了这次次数不退"，那是事后通知，不是知情同意。
 *
 * ══════════════════════════════════════════════════════════════════
 * 三种"什么都没发生"都是正常结果
 * ══════════════════════════════════════════════════════════════════
 * 任务刚好跑完 / 早就不在服务端了 / 用户点了两次 —— 后端一律返回
 * code 0，用 `cancelled` 字段说明"这一次到底停没停掉"。
 * 所以这里的成功提示分两句，不把"它自己跑完了"说成"你中断成功"。
 */
const props = defineProps<{
  /** 要中断的任务。空串表示没有在跑的任务（按钮不渲染） */
  taskId: string
  /** 关掉：任务已经不在跑、或没有权限时置灰 */
  disabled?: boolean
}>()

const emit = defineEmits<{
  /** 中断请求已发出。`stopped` = 这一次真的把它停掉了 */
  (e: 'settled', payload: { stopped: boolean }): void
}>()

const open = ref(false)
const busy = ref(false)

async function confirm() {
  if (busy.value) return
  busy.value = true
  try {
    const snap = await cancelTask(props.taskId)
    open.value = false
    if (snap.cancelled) {
      toast.success('已中断。本次消耗的额度不退。')
    } else {
      // ⚠️ 不能说"已中断" —— 它是自己跑完的，用户点的时候已经晚了。
      //    谎报一次成功，用户下次就不会再信这个按钮。
      toast.info('任务在你点之前就已经结束了，按原状态保留。')
    }
    emit('settled', { stopped: snap.cancelled })
  } catch (e) {
    // 4005（不是自己的任务）之类走这里。**不关确认框** ——
    // 关了用户会以为成功了，而状态其实一点没变。
    toast.error(messageOf(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <button
    v-if="taskId && !disabled"
    class="btn-ghost shrink-0 px-2.5 py-1 text-[11px]"
    type="button"
    @click="open = true"
  >
    <AppIcon name="x-circle" :size="14" class="text-wood-muted" />
    <span>中断</span>
  </button>

  <!-- 确认框。**不用 `window.confirm`** —— 它拦不住"额度不退"这种
       需要说清楚的事（一句话塞不下），而且在无头浏览器里没法验证。 -->
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-wood-dark/30 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="cancel-title"
      @click.self="open = false"
    >
      <div class="card w-full max-w-[420px] p-5">
        <h2
          id="cancel-title"
          class="flex items-center gap-2 font-serif text-[16px] font-semibold text-wood-dark"
        >
          <AppIcon name="x-circle" :size="18" class="text-wood-muted" />
          <span>中断这次任务？</span>
        </h2>

        <ul class="mt-3 space-y-2 text-[12px] leading-relaxed text-wood">
          <li class="flex gap-2">
            <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent-gold" />
            <span>
              任务会<strong class="font-semibold">立刻停下</strong>，这一次不会有结果产出。已经跑完的部分无法恢复。
            </span>
          </li>
          <li class="flex gap-2">
            <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent-gold" />
            <span>
              <strong class="font-semibold">这次消耗的额度不退。</strong>
              额度在任务开始时就已经计入今日用量（演示环境每天重置）。
            </span>
          </li>
          <li class="flex gap-2">
            <span class="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent-gold" />
            <span>
              想继续等也可以 —— 上面的剩余时间是按实际速度算的，
              不是固定值。等下去不会白等。
            </span>
          </li>
        </ul>

        <div class="mt-5 flex justify-end gap-2">
          <button class="btn-ghost px-3.5 py-1.5" type="button" @click="open = false">
            继续等待
          </button>
          <button
            class="btn-primary px-3.5 py-1.5"
            type="button"
            :disabled="busy"
            @click="confirm"
          >
            <AppIcon :name="busy ? 'spinner' : 'x-circle'" :size="15" />
            <span>{{ busy ? '正在中断…' : '确认中断' }}</span>
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
