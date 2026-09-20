/**
 * 全局 QueryClient（shared 层）
 *
 * 【为什么从 App.tsx 下沉到这里】
 * 它不再只被应用根使用：`features/auth/session.ts` 也要在换账号时清空缓存。
 * 若继续留在 App.tsx，auth 就得反向 import 应用根 —— 破坏模块边界。放到 shared 后，
 * App.tsx 与 auth 都只依赖 shared，方向正确。
 *
 * 【为什么 staleTime 不能是 0】
 * TanStack Query 默认 staleTime=0，挂载时会**先渲染缓存再后台重新校验**（stale-while-
 * revalidate）。而 `meKeys` 全部是不含 user_id 的固定 key（`['me','profile']`…
 * `['me','bookmarks']`），A 登出后 B 登录会先看到 A 的收藏与学习记录。
 * 除了在换账号时调用 `clear()`，这里再给一个非 0 的 staleTime，缩小两次会话之间
 * 缓存被当作「新鲜数据」直接渲染的窗口。
 */
import { QueryClient } from '@tanstack/react-query';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      // 非 0：避免挂载瞬间把上一位登录者的缓存当作新鲜数据直接渲染
      staleTime: 30_000,
    },
  },
});

/**
 * 清空全部查询缓存。
 *
 * 换账号/登出时必须调用：`removeQueries` 只删数据不清空 mutation，`clear` 才是
 * 彻底重置（包括正在进行中的请求记录），与「换了一个人」的语义一致。
 */
export function clearQueryCache(): void {
  queryClient.clear();
}
