import { useCallback, useMemo, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { ApiError } from '../../../shared/api/client';
import { resetPassword } from '../api';
import { CaptchaChallenge, type CaptchaResult } from './CaptchaChallenge';
import { useCaptchaConfig } from '../hooks/useCaptchaConfig';
import { checkPassword, passwordStrength } from '../validation';
import styles from './Auth.module.css';

function isCaptchaError(code: string | undefined): boolean {
  return (
    code === 'CAPTCHA_REQUIRED' || code === 'CAPTCHA_FAILED' || code === 'CAPTCHA_MISCONFIGURED'
  );
}

/**
 * 重置密码页：从邮件链接的 `?token=` 拿到一次性令牌后设置新密码。
 *
 * 成功后后端会撤销该账号的**全部会话**；此页引导用户回到登录页用新密码登录。
 */
export function ResetPasswordPage() {
  const [searchParams] = useSearchParams();
  const token = searchParams.get('token') ?? '';

  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const captcha = useCaptchaConfig();
  const captchaRequired = captcha.data?.enabled === true;
  const [captchaResult, setCaptchaResult] = useState<CaptchaResult | null>(null);
  const [captchaBlocked, setCaptchaBlocked] = useState(false);
  const [captchaReset, setCaptchaReset] = useState(0);

  const passwordError = useMemo(() => (password ? checkPassword(password) : ''), [password]);
  const confirmError = useMemo(
    () => (confirm && confirm !== password ? '两次输入的密码不一致。' : ''),
    [confirm, password],
  );
  const strength = useMemo(() => passwordStrength(password), [password]);

  const handleCaptchaVerify = useCallback(
    (result: CaptchaResult | null) => setCaptchaResult(result),
    [],
  );
  const handleCaptchaUnavailable = useCallback(
    (unavailable: boolean) => setCaptchaBlocked(unavailable),
    [],
  );
  const captchaIncomplete = captchaRequired && (captchaBlocked || !captchaResult);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    if (!token) {
      setError('重置链接无效，请重新申请。');
      return;
    }
    if (checkPassword(password)) {
      setError('请设置符合要求的密码。');
      return;
    }
    if (confirm !== password) {
      setError('两次输入的密码不一致。');
      return;
    }

    setSubmitting(true);
    setError('');
    try {
      await resetPassword({
        token,
        new_password: password,
        captcha_token: captchaRequired ? (captchaResult?.token ?? null) : null,
        captcha_knock: captchaRequired ? (captchaResult?.knock ?? null) : null,
        captcha_dfu: captchaRequired ? (captchaResult?.dfu ?? null) : null,
        captcha_ip: captchaRequired ? (captchaResult?.ip ?? null) : null,
      });
      setDone(true);
    } catch (cause) {
      if (cause instanceof ApiError) {
        if (isCaptchaError(cause.code)) {
          setCaptchaReset((value) => value + 1);
        }
        setError(cause.message);
      } else {
        setError('重置失败，请稍后重试。');
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <div className={styles.brand}>
          <span className={styles.brandMark}>I</span>
          <span>ILO 技术情报官</span>
        </div>

        <h1 className={styles.title}>设置新密码</h1>
        <p className={styles.sub}>设置完成后，其他设备上的登录会立即失效。</p>

        {done ? (
          <>
            <div className={styles.notice} role="status" style={{ marginTop: 20 }}>
              密码已重置，请使用新密码登录。
            </div>
            <p className={styles.foot}>
              <Link to="/login">前往登录</Link>
            </p>
          </>
        ) : (
          <form className={styles.form} onSubmit={handleSubmit} noValidate>
            {!token && (
              <div className={styles.alert} role="alert">
                重置链接无效或缺少令牌，请重新申请。
              </div>
            )}
            {error && (
              <div className={styles.alert} role="alert">
                {error}
              </div>
            )}

            <div className={styles.field}>
              <label className={styles.label} htmlFor="password">
                新密码
              </label>
              <input
                id="password"
                className={styles.input}
                type="password"
                name="new-password"
                autoComplete="new-password"
                aria-invalid={passwordError ? 'true' : 'false'}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={submitting}
              />
              {password && (
                <div className={styles.strength} aria-hidden="true">
                  <div className={styles.strengthBar}>
                    <div
                      className={styles.strengthFill}
                      data-score={strength.score}
                      style={{ width: `${(strength.score > 0 ? strength.score : 0.4) * 25}%` }}
                    />
                  </div>
                  <span className={styles.strengthLabel}>{strength.label}</span>
                </div>
              )}
              {passwordError ? (
                <span className={styles.fieldError}>{passwordError}</span>
              ) : (
                <span className={styles.hint}>至少 8 个字符，避免常见密码。</span>
              )}
            </div>

            <div className={styles.field}>
              <label className={styles.label} htmlFor="confirm">
                确认新密码
              </label>
              <input
                id="confirm"
                className={styles.input}
                type="password"
                name="confirm"
                autoComplete="new-password"
                aria-invalid={confirmError ? 'true' : 'false'}
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
                disabled={submitting}
              />
              {confirmError && <span className={styles.fieldError}>{confirmError}</span>}
            </div>

            {captchaRequired && (
              <CaptchaChallenge
                vid={captcha.data?.vid ?? ''}
                onVerify={handleCaptchaVerify}
                onUnavailable={handleCaptchaUnavailable}
                resetSignal={captchaReset}
              />
            )}

            <button
              className={`btn btn-primary ${styles.submit}`}
              type="submit"
              disabled={submitting || captchaIncomplete || !token}
            >
              {submitting ? '提交中…' : '重置密码'}
            </button>
          </form>
        )}

        <p className={styles.foot}>
          <Link to="/login">← 返回登录</Link>
        </p>
      </div>
    </div>
  );
}
