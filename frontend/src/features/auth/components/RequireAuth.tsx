import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { useAuthStore } from '../store';

/**
 * 权限边界的前端一道防线：未登录不放行。
 *
 * 真正的边界在后端 —— `/learning/*` 一律校验 access token 并从 `sub` 取用户身份，
 * 这里只负责体验（别让用户点了才吃 401）。
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const status = useAuthStore((state) => state.status);
  const location = useLocation();

  // 启动引导未完成：先转圈，避免把已登录用户误判成未登录弹去登录页
  if (status === 'unknown') {
    return (
      <div className="overlay">
        <div className="spin" />
      </div>
    );
  }

  if (status === 'anonymous') {
    const from = `${location.pathname}${location.search}`;
    return <Navigate to={`/login?redirect=${encodeURIComponent(from)}`} replace />;
  }

  return <>{children}</>;
}
