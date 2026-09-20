import clsx from 'clsx';
import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { useToastStore } from '../../../components/ui/toastStore';
import { signOut } from '../session';
import { useAuthStore } from '../store';
import styles from './AccountMenu.module.css';

/**
 * 顶栏账号区域。
 *
 * 以插槽形式由组合根（routes.tsx）注入 NavBar，
 * 这样 discovery 不需要 import auth，features 之间保持零交叉依赖。
 */
export function AccountMenu() {
  const status = useAuthStore((state) => state.status);
  const user = useAuthStore((state) => state.user);
  const navigate = useNavigate();
  const showToast = useToastStore((state) => state.show);
  const [leaving, setLeaving] = useState(false);

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
    void signOut().finally(() => {
      setLeaving(false);
      showToast('已退出登录');
      navigate('/');
    });
  }

  return (
    <div className={styles.group}>
      <span className={styles.user} title={user?.email ?? ''}>
        👤 {user?.username}
      </span>
      <Link className="mini-btn" to="/library">
        我的
      </Link>
      <Link className="mini-btn" to="/account/security">
        账号安全
      </Link>
      <button type="button" className="mini-btn" onClick={handleSignOut} disabled={leaving}>
        {leaving ? '退出中…' : '退出'}
      </button>
    </div>
  );
}
