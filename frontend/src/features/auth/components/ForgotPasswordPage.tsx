import { useCallback, useMemo, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';

import { ApiError } from '../../../shared/api/client';
import { forgotPassword } from '../api';
import { CaptchaChallenge, type CaptchaResult } from './CaptchaChallenge';
import { useCaptchaConfig } from '../hooks/useCaptchaConfig';
import styles from './Auth.module.css';

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function isCaptchaError(code: string | undefined): boolean {
  return (
    code === 'CAPTCHA_REQUIRED' || code === 'CAPTCHA_FAILED' || code === 'CAPTCHA_MISCONFIGURED'
  );
}

/**
 * 忘记密码页：提交邮箱后由后端发送一次性重置链接。
 *
 * 【防枚举】无论邮箱是否存在，后端返回的文案完全一致 —— 这一页也据此只做「已发送」
 * 提示，绝不根据响应区分「邮箱存在 / 不存在」，否则等于把账号枚举能力送给攻击者。
 */
export function ForgotPasswordPage() {
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');
  const [sent, setSent] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const captcha = useCaptchaConfig();
  const captchaRequired = captcha.data?.enabled === true;
  const [captchaResult, setCaptchaResult] = useState<CaptchaResult | null>(null);
  const [captchaBlocked, setCaptchaBlocked] = useState(false);
  const [captchaReset, setCaptchaReset] = useState(0);

  const emailError = useMemo(
    () => (email && !EMAIL_PATTERN.test(email.trim()) ? '请输入有效的邮箱地址。' : ''),
    [email],
  );

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

    if (!EMAIL_PATTERN.test(email.trim())) {
      setError('请输入有效的邮箱地址。');
      return;
    }

    setSubmitting(true);
    setError('');
    setSent('');
    try {
      const data = await forgotPassword({
        email: email.trim(),
        captcha_token: captchaRequired ? (captchaResult?.token ?? null) : null,
        captcha_knock: captchaRequired ? (captchaResult?.knock ?? null) : null,
        captcha_dfu: captchaRequired ? (captchaResult?.dfu ?? null) : null,
        captcha_ip: captchaRequired ? (captchaResult?.ip ?? null) : null,
      });
      setSent(data.message);
    } catch (cause) {
      if (cause instanceof ApiError) {
        if (isCaptchaError(cause.code)) {
          setCaptchaReset((value) => value + 1);
        }
        setError(cause.message);
      } else {
        setError('提交失败，请稍后重试。');
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

        <h1 className={styles.title}>找回密码</h1>
        <p className={styles.sub}>输入注册邮箱，我们会发送一条 30 分钟内有效的重置链接。</p>

        <form className={styles.form} onSubmit={handleSubmit} noValidate>
          {error && (
            <div className={styles.alert} role="alert">
              {error}
            </div>
          )}
          {sent && (
            <div className={styles.notice} role="status">
              {sent}
            </div>
          )}

          <div className={styles.field}>
            <label className={styles.label} htmlFor="email">
              邮箱
            </label>
            <input
              id="email"
              className={styles.input}
              type="email"
              name="email"
              autoComplete="email"
              aria-invalid={error || emailError ? 'true' : 'false'}
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              disabled={submitting}
            />
            {emailError && <span className={styles.fieldError}>{emailError}</span>}
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
            disabled={submitting || captchaIncomplete}
          >
            {submitting ? '发送中…' : '发送重置链接'}
          </button>
        </form>

        <p className={styles.foot}>
          <Link to="/login">← 返回登录</Link>
        </p>
        <p className={styles.quietFoot}>
          开发环境：重置链接不会真发邮件，可从后端日志或 `email_outbox` 表里取。
        </p>
      </div>
    </div>
  );
}
