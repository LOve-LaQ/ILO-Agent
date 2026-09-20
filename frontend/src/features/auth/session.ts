import { setAuthHandlers } from '../../shared/api/client';
import { clearQueryCache } from '../../shared/api/queryClient';
import { emitSessionReset } from '../../shared/lib/sessionBus';
import { fetchMe, logout as logoutRequest, refreshTokens, type UserPublic } from './api';
import { useAuthStore } from './store';

/**
 * 认证会话编排：把「token 从哪来 / 刷新失败怎么办」注入 shared 层。
 *
 * 【模块边界】shared/api/client.ts 不 import 任何 feature，能力由这里注入；
 * 本文件也不 import routes —— 跳转函数由组合根（App.tsx）传入，避免循环依赖。
 */

/** 登录页地址，带上当前位置以便登录后回到原页面 */
export function loginHref(): string {
  const { pathname, search } = window.location;
  if (pathname === '/login' || pathname === '/register') {
    return '/login';
  }
  return `/login?redirect=${encodeURIComponent(`${pathname}${search}`)}`;
}

/** 解析 ?redirect=，只允许站内路径，避免被构造成开放重定向 */
export function safeRedirect(value: string | null): string {
  if (!value || !value.startsWith('/') || value.startsWith('//')) {
    return '/';
  }
  return value;
}

/**
 * 重置「跨账号」的客户端状态。
 *
 * 【为什么必须有这一步】
 * `meKeys` 全部是不含 user_id 的固定 key（`['me','profile']`、`['me','bookmarks']`…），
 * A 登出后 B 在本浏览器登录，若不物理清空，挂载瞬间会先渲染 A 的收藏与学习记录
 * （stale-while-revalidate），`useBookmarks` 的乐观更新还会在 A 的缓存上算出错值。
 * 这里把三件事一起做掉：
 * 1. 清空全部查询缓存（服务端数据，按人划分）；
 * 2. 清 sessionStorage（可能残留跨页面的临时快照）；
 * 3. 广播「会话已重置」——learning 等 feature 各自订阅并丢弃本地状态
 *    （auth 不得 import learning，只能走总线）。
 */
export function resetClientState(): void {
  clearQueryCache();
  try {
    sessionStorage.clear();
  } catch {
    /* 隐私模式下 sessionStorage 可能不可用，清理失败不影响退出登录 */
  }
  emitSessionReset();
}

/**
 * 登录 / 注册成功后的**统一入口**。
 *
 * 必须先 `resetClientState()` 再 `setSession()`，才能覆盖「不登出直接换账号登录」这条
 * 路径 —— 否则新账号的页面会短暂叠着上一个账号的缓存。
 */
export function completeSignIn(accessToken: string, user: UserPublic): void {
  resetClientState();
  useAuthStore.getState().setSession(accessToken, user);
}

/**
 * 换新 access token 并写回内存。
 * 只负责「换到没有」；清登录态与引导登录统一由 client 的 onAuthRequired 处理，
 * 避免两条路径各清一次、互相打架。
 */
async function refreshAccessToken(): Promise<string | null> {
  try {
    const data = await refreshTokens();
    useAuthStore.getState().setAccessToken(data.access_token);
    return data.access_token;
  } catch {
    return null;
  }
}

/**
 * 应用启动时调用一次，完成依赖注入。
 * @param navigate 路由跳转函数（由 App.tsx 传入，避免 auth → routes 的反向依赖）
 */
export function installAuthClient(navigate: (to: string) => void): void {
  setAuthHandlers({
    getAccessToken: () => useAuthStore.getState().accessToken,
    refreshAccessToken,
    onAuthRequired: () => {
      resetClientState();
      useAuthStore.getState().clear();
      navigate(loginHref());
    },
  });
}

let bootstrapInFlight: Promise<void> | null = null;

/**
 * 启动引导：凭 httpOnly refresh cookie 恢复登录态。
 *
 * 这是「刷新页面仍保持登录」的唯一入口 —— access token 不落任何持久化存储。
 * 带单飞保护：React StrictMode 会让 effect 跑两次，不加会打两次 refresh。
 */
export function bootstrapSession(): Promise<void> {
  if (useAuthStore.getState().status !== 'unknown') {
    return Promise.resolve();
  }
  if (!bootstrapInFlight) {
    bootstrapInFlight = runBootstrap().finally(() => {
      bootstrapInFlight = null;
    });
  }
  return bootstrapInFlight;
}

async function runBootstrap(): Promise<void> {
  try {
    const { access_token } = await refreshTokens();
    // 显式带上刚拿到的 token：此刻 store 里还没有，不能依赖自动注入
    const user = await fetchMe(access_token);
    useAuthStore.getState().setSession(access_token, user);
  } catch {
    useAuthStore.getState().clear();
  }
}

/** 退出登录：先让服务端清 Cookie，再清前端状态与跨账号缓存 */
export async function signOut(): Promise<void> {
  try {
    await logoutRequest();
  } catch {
    /* 服务端清不掉 Cookie 也必须让前端退出，否则会卡在假登录态 */
  }
  // 先清缓存再清登录态：清缓存会广播 sessionBus，learning 等据此丢弃上一个账号的本地状态
  resetClientState();
  useAuthStore.getState().clear();
}
