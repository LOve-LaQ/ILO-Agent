import { create } from 'zustand';

/**
 * 全局 Toast 提示
 * 对应原 index.html 中 app().toastMsg / showToast()，默认 2200ms 自动消失。
 */

const TOAST_DURATION = 2200;

interface ToastState {
  message: string;
  /**
   * 展示一条提示。
   *
   * `durationMs` 用于需要读完才明白的提示 —— 尤其是**失败**提示：
   * 默认 2.2s 只够看清「成功」两个字，而「23 条全部未能入库」这种句子
   * 读不完就消失了，等于没提示（抓取失败曾被用户完全忽略，就是这么来的）。
   */
  show: (message: string, durationMs?: number) => void;
  clear: () => void;
}

let timer: ReturnType<typeof setTimeout> | null = null;

export const useToastStore = create<ToastState>((set) => ({
  message: '',
  show: (message, durationMs = TOAST_DURATION) => {
    if (timer !== null) {
      clearTimeout(timer);
    }
    set({ message });
    timer = setTimeout(() => set({ message: '' }), durationMs);
  },
  clear: () => {
    if (timer !== null) {
      clearTimeout(timer);
    }
    set({ message: '' });
  },
}));
