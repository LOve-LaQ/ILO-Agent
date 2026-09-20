import { create } from 'zustand';

/**
 * 全局 Toast 提示
 * 对应原 index.html 中 app().toastMsg / showToast()，默认 2200ms 自动消失。
 */

const TOAST_DURATION = 2200;

interface ToastState {
  message: string;
  show: (message: string) => void;
  clear: () => void;
}

let timer: ReturnType<typeof setTimeout> | null = null;

export const useToastStore = create<ToastState>((set) => ({
  message: '',
  show: (message) => {
    if (timer !== null) {
      clearTimeout(timer);
    }
    set({ message });
    timer = setTimeout(() => set({ message: '' }), TOAST_DURATION);
  },
  clear: () => {
    if (timer !== null) {
      clearTimeout(timer);
    }
    set({ message: '' });
  },
}));
