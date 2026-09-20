import { useState } from 'react';
import { createBrowserRouter } from 'react-router-dom';

import { AccountDeletePage } from './features/auth/components/AccountDeletePage';
import { AccountMenu } from './features/auth/components/AccountMenu';
import { AccountSecurityPage } from './features/auth/components/AccountSecurityPage';
import { ForgotPasswordPage } from './features/auth/components/ForgotPasswordPage';
import { LoginPage } from './features/auth/components/LoginPage';
import { RegisterPage } from './features/auth/components/RegisterPage';
import { RequireAuth } from './features/auth/components/RequireAuth';
import { ResetPasswordPage } from './features/auth/components/ResetPasswordPage';
import { VerifyEmailPage } from './features/auth/components/VerifyEmailPage';
import { DiscoveryPage } from './features/discovery/DiscoveryPage';
import { PrivacyPage, TermsPage } from './features/legal/LegalPages';
import { LibraryPage } from './features/library/LibraryPage';
import { LearningDrawer } from './features/learning/components/LearningDrawer';
import { useLearningSession } from './features/learning/hooks/useLearningSession';

/**
 * 路由表 —— 同时也是**组合层**。
 *
 * 【模块边界】features 之间不互相 import，跨 feature 的组合都写在这里：
 * - 账号区域（auth）以插槽形式注入发现页与「我的」页的顶栏；
 * - 「我的」页只声明 `onStartLearning` 插槽，具体由这里接到 learning 的学习抽屉上。
 *
 * 权限边界：
 * - `/discover/*` 匿名可读，首页无需登录
 * - `/learning/*` 必须登录：后端强制校验 access token，前端在未登录时点「开始讲解」
 *   会拿到 401，客户端随即引导到 `/login?redirect=原地址`
 * - `/library` 用 <RequireAuth> 包一层：真正的边界在后端（`/me/*` 一律校验 token 且
 *   只返回 current_user 自己的数据），这里只负责别让用户点进去才吃 401
 */
function LibraryRoute() {
  // 学习抽屉是 learning feature 的东西，「我的」页不该知道它存在 ——
  // 组合层负责把 startLearning 接进 LibraryPage 的插槽。
  const { startLearning } = useLearningSession();
  // 抽屉只在用户主动点「继续学习」后才挂载。
  //
  // 不能无条件挂载：抽屉挂载时会尝试恢复上次未完成的会话（restore），成功就自己展开，
  // 而它带一层覆盖整页的半透明遮罩 —— 用户一进「我的」就被压住，连 tab 都点不动。
  // 恢复会话是发现页的事：那边挂载抽屉是必须的，这里不是。
  const [drawerNeeded, setDrawerNeeded] = useState(false);

  return (
    <RequireAuth>
      <LibraryPage
        accountSlot={<AccountMenu />}
        onStartLearning={(card) => {
          setDrawerNeeded(true);
          void startLearning(card);
        }}
      />
      {drawerNeeded && <LearningDrawer />}
    </RequireAuth>
  );
}

export const router = createBrowserRouter([
  { path: '/', element: <DiscoveryPage accountSlot={<AccountMenu />} /> },
  { path: '/library', element: <LibraryRoute /> },
  { path: '/login', element: <LoginPage /> },
  { path: '/register', element: <RegisterPage /> },
  { path: '/forgot-password', element: <ForgotPasswordPage /> },
  { path: '/reset-password', element: <ResetPasswordPage /> },
  // 确认邮件链接落地页：可匿名访问（用户很可能在别的设备/未登录状态下点开邮件）
  { path: '/verify-email', element: <VerifyEmailPage /> },
  {
    // 账号安全（设备管理 / 改密 / 导出）必须登录：真正的边界在后端 /auth/* 与 /me/*
    path: '/account/security',
    element: (
      <RequireAuth>
        <AccountSecurityPage />
      </RequireAuth>
    ),
  },
  // 注销页可匿名访问：冷静期内账号已停用、无法登录，撤销注销需在此页用「标识 + 密码」自证
  { path: '/account/delete', element: <AccountDeletePage /> },
  { path: '/legal/privacy', element: <PrivacyPage /> },
  { path: '/legal/terms', element: <TermsPage /> },
]);
