import clsx from 'clsx';
import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';

import { useToastStore } from '../../../components/ui/toastStore';
import { signOut } from '../session';
import { useAuthStore } from '../store';
import styles from './AccountMenu.module.css';

/**
 * 顶栏账号区域。
 *
 * 以插槽形式由组合根（routes.tsx）注入 NavBar，
 * 这样 discovery 不需要 import auth，features 之间保持零交叉依赖。
 *
 * 【两种形态】桌面端平铺按钮（一眼能看到全部入口）；窄屏收成一个头像按钮 +
 * 下拉菜单 —— 桌面版有 5 个元素（昵称/我的/账号安全/管理后台/退出），
 * 在 390px 顶栏里平铺会直接吃掉整行，把「抓取最新」挤出屏幕。
 */
export function AccountMenu() {
  const status = useAuthStore((state) => state.status);
  const user = useAuthStore((state) => state.user);
  const navigate = useNavigate();
  const location = useLocation();
  const showToast = useToastStore((state) => state.show);
  const [leaving, setLeaving] = useState(false);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // 路由变化时收起：否则点完「我的」进入新页面，菜单还浮在新页面上
  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  // 点空白处 / Esc 收起。用 pointerdown 而不是 click：
  // 手指按下的瞬间就收起，不用等 300ms 的点击合成，触感更接近原生
  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false);
    }
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  // 引导未完成：占位而不是先渲染「登录」，避免登录用户看到一瞬间的登录按钮
  if (status === 'unknown') {
    return <span className={styles.placeholder} aria-hidden="true" />;
  }

  if (status === 'anonymous') {
    return (
      <div className={styles.group}>
        <Link className="mini-btn" to="/login">
          登录
        </Link>
        <Link className={clsx('mini-btn', 'solid')} to="/register">
          注册
        </Link>
      </div>
    );
  }

  function handleSignOut() {
    if (leaving) return;
    setLeaving(true);
    setOpen(false);
    void signOut().finally(() => {
      setLeaving(false);
      showToast('已退出登录');
      navigate('/');
    });
  }

  return (
    <div className={styles.root} ref={rootRef}>
      {/* 桌面端：平铺入口。
          这里刻意不渲染昵称：内容区上限 1032px，加昵称后整行需要 1091px，
          超出的宽度会让 nav 里的每个按钮收缩到 min-content（「账号安 / 全」竖排）。
          昵称在「我的」页与下拉菜单里都能看到，不值得用它换一次顶栏塌陷。 */}
      <div className={styles.inline}>
        <Link className="mini-btn" to="/library">
          我的
        </Link>
        <Link className="mini-btn" to="/account/security">
          账号安全
        </Link>
        {/* 管理入口只对 admin 显示：这只是体验优化，真正的边界在后端 require_admin */}
        {user?.role === 'admin' && (
          <Link className="mini-btn" to="/admin">
            管理后台
          </Link>
        )}
        <button type="button" className="mini-btn" onClick={handleSignOut} disabled={leaving}>
          {leaving ? '退出中…' : '退出'}
        </button>
      </div>

      {/* 窄屏：头像触发下拉 */}
      <button
        type="button"
        className={styles.trigger}
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="账号菜单"
      >
        <span className={styles.avatar}>👤</span>
        <span className={styles.caret} aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <div className={styles.sheet} role="menu">
          <div className={styles.sheetUser}>
            <span className={styles.sheetName}>{user?.username}</span>
            <span className={styles.sheetMeta}>{user?.email}</span>
          </div>
          <Link className={styles.item} to="/library" role="menuitem">
            我的学习
          </Link>
          <Link className={styles.item} to="/account/security" role="menuitem">
            账号安全
          </Link>
          {user?.role === 'admin' && (
            <Link className={styles.item} to="/admin" role="menuitem">
              管理后台
            </Link>
          )}
          <button
            type="button"
            className={styles.item}
            onClick={handleSignOut}
            disabled={leaving}
            role="menuitem"
          >
            {leaving ? '退出中…' : '退出登录'}
          </button>
        </div>
      )}
    </div>
  );
}
