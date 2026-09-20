/**
 * access token 的响应式快照（shared 层）
 *
 * 【为什么放在 shared】
 * 收藏（discovery 的卡片开关）与会话恢复（learning 的学习抽屉）都需要回答一个问题：
 * 「现在登录了吗？没有就别发需要登录的请求」。但按模块边界约定，features 之间不得
 * 互相 import，shared 也不得 import 任何 feature —— 于是让 features/auth 把 token
 * **主动推送**到这里（见 auth/store.ts），shared 只维护一份可订阅的快照。
 *
 * 【不这样做的代价】
 * 匿名访问首页时收藏接口会返回 401，客户端随即判定「登录态失效」并把用户弹去登录页。
 * 也就是说：不区分登录态地自动拉取 /me/*，等于给每个游客强制跳转登录。
 */

import { useSyncExternalStore } from 'react';

let currentToken: string | null = null;
const listeners = new Set<() => void>();

/** 由 features/auth 在会话变化时调用（登录 / 刷新换取 / 退出） */
export function publishAccessToken(token: string | null): void {
  if (token === currentToken) return;
  currentToken = token;
  for (const listener of listeners) {
    listener();
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

const getSnapshot = (): string | null => currentToken;
const getServerSnapshot = (): string | null => null;

/** 当前 access token；null 表示尚未登录（含启动引导未完成） */
export function useAccessToken(): string | null {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
