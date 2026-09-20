import { useCallback, useState, type FormEvent } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';

import { useToastStore } from '../../../components/ui/toastStore';
import { ApiError } from '../../../shared/api/client';
import { login } from '../api';
import { completeSignIn, safeRedirect } from '../session';
import { CaptchaChallenge, type CaptchaResult } from './CaptchaChallenge';
import { useCaptchaConfig } from '../hooks/useCaptchaConfig';
import styles from './Auth.module.css';

/** 登录页。登录成功后跳回 ?redirect= 指定的原页面（只接受站内路径）。 */
export function LoginPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const showToast = useToastStore((state) => state.show);

  const [identifier, setIdentifier] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  /** 连续失败次数：达到 1 次后按后端口径「已有失败记录」，后续登录需要人机校验 */
  const [attempts, setAttempts] = useState(0);
  /** 账号处于注销冷静期（403 ACCOUNT_PENDING_DELETION）时的专门提示 */
  const [pendingDeletion, setPendingDeletion] = useState(false);

  const captcha = useCaptchaConfig();
  const captchaEnabled = captcha.data?.enabled === true;
  const [captchaResult, setCaptchaResult] = useState<CaptchaResult | null>(null);
  const [captchaBlocked, setCaptchaBlocked] = useState(false);
  const [captchaReset, setCaptchaReset] = useState(0);

  const redirectTo = safeRedirect(searchParams.get('redirect'));

  // 后端仅在「该账号或 IP 已有失败记录」时要求人机校验；前端据本地失败次数对齐这一时机，
  // 避免给正常用户每次登录都弹验证。
  const showCaptcha = captchaEnabled && attempts > 0;
  const captchaIncomplete = showCaptcha && (captchaBlocked || !captchaResult);

  const handleCaptchaVerify = useCallback(
    (result: CaptchaResult | null) => setCaptchaResult(result),
    [],
  );
  const handleCaptchaUnavailable = useCallback(
    (unavailable: boolean) => setCaptchaBlocked(unavailable),
    [],
  );

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    if (!identifier.trim() || !password) {
      setError('请填写邮箱/用户名与密码。');
      return;
    }

    setSubmitting(true);
    setError('');
    setPendingDeletion(false);
    try {
      const data = await login({
        identifier: identifier.trim(),
        password,
        captcha_token: showCaptcha ? (captchaResult?.token ?? null) : null,
        captcha_knock: showCaptcha ? (captchaResult?.knock ?? null) : null,
        captcha_dfu: showCaptcha ? (captchaResult?.dfu ?? null) : null,
        captcha_ip: showCaptcha ? (captchaResult?.ip ?? null) : null,
      });
      completeSignIn(data.access_token, data.user);
      showToast(`欢迎回来，${data.user.username}`);
      navigate(redirectTo, { replace: true });
    } catch (cause) {
      if (cause instanceof ApiError) {
        if (cause.code === 'ACCOUNT_PENDING_DELETION') {
          setPendingDeletion(true);
        }
        // 记录一次失败：后续登录开始要求人机校验；若失败与人机校验相关则让用户重新验证
        if (
          cause.code === 'CAPTCHA_REQUIRED' ||
          cause.code === 'CAPTCHA_FAILED' ||
          cause.code === 'CAPTCHA_MISCONFIGURED'
        ) {
          setCaptchaReset((value) => value + 1);
        }
        if (cause.status === 401 || cause.status === 429) {
          setAttempts((value) => value + 1);
        }
        setError(cause.message);
      } else {
        setError('登录失败，请稍后重试。');
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

        <h1 className={styles.title}>登录</h1>
        <p className={styles.sub}>登录后可发起卡片讲解与问答，学习记录会归到你的账号下。</p>

        <form className={styles.form} onSubmit={handleSubmit} noValidate>
          {error && (
            <div className={styles.alert} role="alert">
              {error}
            </div>
          )}

          {pendingDeletion && (
            <div className={styles.notice} role="status">
              该账号已申请注销，正处于冷静期。
              <Link to="/account/delete">前往撤销注销申请</Link>
            </div>
          )}

          {attempts > 0 && !error && (
            <div className={styles.notice} role="status">
              登录尝试次数较多，请确认账号与密码无误。
            </div>
          )}

          <div className={styles.field}>
            <label className={styles.label} htmlFor="identifier">
              邮箱或用户名
            </label>
            <input
              id="identifier"
              className={styles.input}
              name="identifier"
              autoComplete="username"
              aria-invalid={error ? 'true' : 'false'}
              value={identifier}
              onChange={(event) => setIdentifier(event.target.value)}
              disabled={submitting}
            />
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="password">
              密码
            </label>
            <input
              id="password"
              className={styles.input}
              type="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={submitting}
            />
          </div>

          {showCaptcha && (
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
            {submitting ? '登录中…' : '登录'}
          </button>
        </form>

        <p className={styles.foot}>
          <Link to="/forgot-password">忘记密码？</Link>
        </p>
        <p className={styles.foot}>
          还没有账号？<Link to="/register">注册一个</Link>
        </p>
        <p className={styles.quietFoot}>
          <Link to="/">← 先随便看看</Link>
        </p>
      </div>
    </div>
  );
}
