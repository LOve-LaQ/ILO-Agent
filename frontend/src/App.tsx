import { QueryClientProvider } from '@tanstack/react-query';
import { useEffect } from 'react';
import { RouterProvider } from 'react-router-dom';

import { Toast } from './components/ui/Toast';
import { bootstrapSession, installAuthClient } from './features/auth/session';
import { router } from './routes';
import { queryClient } from './shared/api/queryClient';

// 组合根：把 auth 能力注入 shared/http 客户端（shared 不得反向依赖 feature）。
// 放在模块加载期完成，保证任何组件发出请求前「token 注入 + 401 刷新重放」已就位。
installAuthClient((to) => {
  void router.navigate(to);
});

/** 应用根：QueryClientProvider + Router + 全局 Toast */
export function App() {
  // 刷新页面后用 httpOnly refresh cookie 恢复登录态
  // （access token 只存内存、不落任何持久化存储，所以必须在这里换一次）
  useEffect(() => {
    void bootstrapSession();
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      <Toast />
    </QueryClientProvider>
  );
}
