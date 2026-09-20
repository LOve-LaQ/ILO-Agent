import { create } from 'zustand';

import { publishAccessToken } from '../../shared/api/authToken';
import type { UserPublic } from './api';

/**
 * 登录态。
 *
 * - `unknown`：应用刚启动，还没确认 Cookie 里的登录态（正在用 refresh cookie 恢复）
 * - `anonymous`：确认未登录
 * - `authenticated`：已登录
 */
export type AuthStatus = 'unknown' | 'anonymous' | 'authenticated';

interface AuthState {
  status: AuthStatus;
  user: UserPublic | null;
  /**
   * access token **只放内存**，绝不落 localStorage / sessionStorage：
   * 刷新页面后靠 httpOnly refresh cookie 重新换取，避免 XSS 直接偷走长效凭证。
   */
  accessToken: string | null;

  /** 登录 / 注册成功后写入完整会话 */
  setSession: (accessToken: string, user: UserPublic) => void;
  /**
   * 局部更新用户信息（如邮箱确认成功后刷新 `email_verified`）。
   * 不动 status / accessToken —— 这不是登录态变化，只是资料变了。
   */
  setUser: (user: UserPublic) => void;
  /** 刷新换到新 access token 时仅更新 token，不改动 status / user */
  setAccessToken: (accessToken: string | null) => void;
  /** 退出或被判定为登录态失效 */
  clear: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  status: 'unknown',
  user: null,
  accessToken: null,

  // 三处写入都顺带推送给 shared：跨 feature 的能力（收藏 / 会话恢复）据此
  // 决定「现在能不能发需要登录的请求」，避免匿名游客被 401 弹去登录页。
  setSession: (accessToken, user) => {
    publishAccessToken(accessToken);
    set({ status: 'authenticated', user, accessToken });
  },
  setAccessToken: (accessToken) => {
    publishAccessToken(accessToken);
    set({ accessToken });
  },
  setUser: (user) => set({ user }),
  clear: () => {
    publishAccessToken(null);
    set({ status: 'anonymous', user: null, accessToken: null });
  },
}));
