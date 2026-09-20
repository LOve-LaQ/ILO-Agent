/**
 * 会话生命周期事件总线（shared 层）
 *
 * 【为什么需要它】
 * 换账号（登出或直接换号登录）时，除了 auth 自己的登录态，还有其它 feature 持有的
 * 本地状态需要一并丢弃 —— 最典型的是 learning 的学习抽屉：它可能正开着上一个账号的
 * 会话与对话。但按模块边界约定，`features/auth` 不得 import `features/learning`。
 *
 * 于是让 auth 只在此「广播事实」，需要响应的 feature 各自订阅并清理自己的状态。
 * 这是一种反向依赖：消费方主动挂到 shared 的总线上，生产方无需知道谁在听。
 */

type SessionResetListener = () => void;

const listeners = new Set<SessionResetListener>();

/**
 * 订阅「会话已重置」。返回取消订阅函数。
 *
 * 调用方通常在模块加载期就注册（例如 learning/store.ts），并让订阅在应用整个生命周期
 * 内存续 —— 摘掉订阅会让该 feature 悄悄失去跨账号清理能力。
 */
export function onSessionReset(listener: SessionResetListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** 广播「会话已重置」（由 features/auth 在换账号/登出时调用） */
export function emitSessionReset(): void {
  // 复制一份再遍历：监听器内部若触发新的订阅/退订，不应影响本次派发
  for (const listener of Array.from(listeners)) {
    listener();
  }
}
