import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { ApiError } from '../../../shared/api/client';
import { confirmEmail } from '../api';
import { useAuthStore } from '../store';
import styles from './Auth.module.css';

type Phase = 'verifying' | 'done' | 'error';

/**
 * 邮箱确认页：从邮件链接的 `?token=` 取出确认令牌，进页即自动确认。
 *
 * 【为什么自动提交而不放按钮】用户点邮件链接的意图已经足够明确，再让他点一次
 * 「确认」是多余的一步；而且后端确认是**幂等**的，重放无害。
 *
 * 【为什么重复执行是安全的】React StrictMode 会故意把 effect 跑两遍，这里不做
 * 去重 —— 靠的是后端的幂等性，而不是前端的小聪明。
 *
 * 【为什么成功与否都不影响使用】注册并不要求邮箱已验证：这一页只决定
 * 「将来忘了密码时找不找得回」，不改变账号现在能不能用。
 */
export function VerifyEmailPage() {
  const [searchParams] = useSearchParams();
  const token = searchParams.get('token') ?? '';

  const [phase, setPhase] = useState<Phase>('verifying');
  const [message, setMessage] = useState('');
  // 失败后允许重试；也用来在 token 不变时重新触发 effect
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!token) {
      setPhase('error');
      setMessage('确认链接不完整（缺少令牌），请登录后在「账号安全」页重新发送。');
      return;
    }

    let cancelled = false;
    setPhase('verifying');
    setMessage('');

    confirmEmail({ token })
      .then((data) => {
        if (cancelled) return;
        setPhase('done');
        setMessage(data.message);
        // 已登录时顺手同步本地用户信息，否则「账号安全」页会一直挂着「未验证」提示。
        // 从 getState() 现取而不是订阅 store：订阅会让 user 进入 effect 依赖，
        // 而下面会写入新的 user 对象 —— 那会把 effect 变成死循环。
        const { status, user, setUser } = useAuthStore.getState();
        if (status === 'authenticated' && user && !user.email_verified) {
          setUser({ ...user, email_verified: true });
        }
      })
      .catch((cause) => {
        if (cancelled) return;
        setPhase('error');
        setMessage(cause instanceof ApiError ? cause.message : '确认失败，请稍后重试。');
      });

    return () => {
      cancelled = true;
    };
  }, [token, attempt]);

  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <div className={styles.brand}>
          <span className={styles.brandMark}>I</span>
          <span>ILO 技术情报官</span>
        </div>

        <h1 className={styles.title}>确认邮箱</h1>
        <p className={styles.sub}>确认之后，忘记密码时才能收到重置链接。</p>

        <div className={styles.form}>
          {phase === 'verifying' && (
            <div className={styles.notice} role="status">
              正在确认邮箱…
            </div>
          )}

          {phase === 'done' && (
            <div className={styles.notice} role="status">
              {message}
            </div>
          )}

          {phase === 'error' && (
            <>
              <div className={styles.alert} role="alert">
                {message}
              </div>
              {token && (
                <button
                  type="button"
                  className={`btn btn-primary ${styles.submit}`}
                  onClick={() => setAttempt((value) => value + 1)}
                >
                  重试
                </button>
              )}
            </>
          )}
        </div>

        <p className={styles.foot}>
          <Link to="/login">← 返回登录</Link>
        </p>
        {phase === 'error' && (
          <p className={styles.quietFoot}>
            链接过期也没关系：登录后在「账号安全」页可以重新发送确认邮件。
          </p>
        )}
      </div>
    </div>
  );
}
