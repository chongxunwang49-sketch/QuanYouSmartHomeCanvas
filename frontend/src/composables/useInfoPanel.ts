import { ref } from 'vue'

/**
 * 「系统信息」面板的开关。
 *
 * 模块级单例——顶栏头像和侧栏用户卡都要打开同一个面板，
 * 各自持有一份状态会变成两个面板。
 */
const open = ref(false)

export function useInfoPanel() {
  return {
    open,
    show: () => (open.value = true),
    hide: () => (open.value = false),
    toggle: () => (open.value = !open.value),
  }
}
