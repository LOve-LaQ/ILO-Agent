import { useCallback, useMemo, useState, type FormEvent } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';

import { useToastStore } from '../../../components/ui/toastStore';
import { ApiError } from '../../../shared/api/client';
import { register } from '../api';
import { completeSignIn, safeRedirect } from '../session';
import { CaptchaChallenge, type CaptchaResult } from './CaptchaChallenge';
import { useCaptchaConfig } from '../hooks/useCaptchaConfig';
import { checkPassword, checkUsername, passwordStrength, USERNAME_MAX, USERNAME_MIN } from '../validation';
import styles from './Auth.module.css';

/**
 * 前端校验与后端契约保持一致（backend/src/schemas/auth.py）：
 * - email 走 EmailStr
 * - username 走 core/username_policy（字符集 + 保留字 + 分隔符）
 * - password 走 core/password_policy（长度 + 弱密码黑名单 + 序列/重复 + 与身份重合）
 *
 * 规则集中在 ../validation.ts，此处只负责「什么时候把错误显示出来」。
 */
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

type FieldName = 'email' | 'username' | 'password' | 'confirm';
type FieldErrors = Partial<Record<FieldName, string>>;

const FIELD_ORDER: FieldName[] = ['email', 'username', 'password', 'confirm'];

function validateAll(values: {
  email: string;
  username: string;
  password: string;
  confirm: string;
}): FieldErrors {
  const errors: FieldErrors = {};

  if (!EMAIL_PATTERN.test(values.email.trim())) {
    errors.email = '请输入有效的邮箱地址。';
  }
  const usernameError = checkUsername(values.username);
  if (usernameError) {
    errors.username = usernameError;
  }
  const passwordError = checkPassword(values.password, {
    username: values.username.trim(),
    email: values.email.trim(),
  });
  if (passwordError) {
    errors.password = passwordError;
  }
  if (values.confirm !== values.password) {
    errors.confirm = '两次输入的密码不一致。';
  }

  return errors;
}

/** 注册页。注册成功即进入登录态。 */
export function RegisterPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const showToast = useToastStore((state) => state.show);

  const [email, setEmail] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [acceptedTerms, setAcceptedTerms] = useState(false);
  const [touched, setTouched] = useState<Partial<Record<FieldName, boolean>>>({});
  const [showAll, setShowAll] = useState(false);
  const [termsError, setTermsError] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const captcha = useCaptchaConfig();
  const captchaRequired = captcha.data?.enabled === true;
  const [captchaResult, setCaptchaResult] = useState<CaptchaResult | null>(null);
  const [captchaBlocked, setCaptchaBlocked] = useState(false);
  const [captchaReset, setCaptchaReset] = useState(0);

  const redirectTo = safeRedirect(searchParams.get('redirect'));

  const allErrors = useMemo(
    () => validateAll({ email, username, password, confirm }),
    [email, username, password, confirm],
  );

  // 只显示「已失焦过」或「提交过」的字段错误，避免一进页面就满屏报红
  const visibleErrors = useMemo<FieldErrors>(() => {
    const shown: FieldErrors = {};
    for (const field of FIELD_ORDER) {
      if ((touched[field] || showAll) && allErrors[field]) {
        shown[field] = allErrors[field];
      }
    }
    return shown;
  }, [allErrors, touched, showAll]);

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
  const submitDisabled = submitting || captcha.isLoading || captchaIncomplete;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    setShowAll(true);
    if (Object.keys(allErrors).length > 0) {
      return;
    }
    if (!acceptedTerms) {
      setTermsError('请先阅读并同意用户协议与隐私政策。');
      return;
    }

    setSubmitting(true);
    setError('');
    setTermsError('');
    try {
      const data = await register({
        email: email.trim(),
        username: username.trim(),
        password,
        captcha_token: captchaRequired ? (captchaResult?.token ?? null) : null,
        captcha_knock: captchaRequired ? (captchaResult?.knock ?? null) : null,
        captcha_dfu: captchaRequired ? (captchaResult?.dfu ?? null) : null,
        captcha_ip: captchaRequired ? (captchaResult?.ip ?? null) : null,
        accept_terms: acceptedTerms,
      });
      completeSignIn(data.access_token, data.user);
      showToast(`注册成功，欢迎加入，${data.user.username}`);
      navigate(redirectTo, { replace: true });
    } catch (cause) {
      if (cause instanceof ApiError) {
        // 人机校验类失败：让用户重新验证（token 可能已过期或被拒）
        if (
          cause.code === 'CAPTCHA_REQUIRED' ||
          cause.code === 'CAPTCHA_FAILED' ||
          cause.code === 'CAPTCHA_MISCONFIGURED'
        ) {
          setCaptchaReset((value) => value + 1);
        }
        setError(cause.message);
      } else {
        setError('注册失败，请稍后重试。');
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

        <h1 className={styles.title}>注册</h1>
        <p className={styles.sub}>创建账号后即可开始学习，学习记录只对你自己可见。</p>

        <form className={styles.form} onSubmit={handleSubmit} noValidate>
          {error && (
            <div className={styles.alert} role="alert">
              {error}
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
              aria-invalid={visibleErrors.email ? 'true' : 'false'}
              aria-describedby={visibleErrors.email ? 'email-error' : undefined}
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              onBlur={() => setTouched((prev) => ({ ...prev, email: true }))}
              disabled={submitting}
            />
            {visibleErrors.email && (
              <span id="email-error" className={styles.fieldError}>
                {visibleErrors.email}
              </span>
            )}
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="username">
              用户名
            </label>
            <input
              id="username"
              className={styles.input}
              name="username"
              autoComplete="nickname"
              aria-invalid={visibleErrors.username ? 'true' : 'false'}
              aria-describedby={visibleErrors.username ? 'username-error' : 'username-hint'}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              onBlur={() => setTouched((prev) => ({ ...prev, username: true }))}
              disabled={submitting}
            />
            {visibleErrors.username ? (
              <span id="username-error" className={styles.fieldError}>
                {visibleErrors.username}
              </span>
            ) : (
              <span id="username-hint" className={styles.hint}>
                {USERNAME_MIN}–{USERNAME_MAX} 个字符，仅限字母、数字、下划线和连字符，登录时也用它。
              </span>
            )}
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
              autoComplete="new-password"
              aria-invalid={visibleErrors.password ? 'true' : 'false'}
              aria-describedby={visibleErrors.password ? 'password-error' : 'password-hint'}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              onBlur={() => setTouched((prev) => ({ ...prev, password: true }))}
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
            {visibleErrors.password ? (
              <span id="password-error" className={styles.fieldError}>
                {visibleErrors.password}
              </span>
            ) : (
              <span id="password-hint" className={styles.hint}>
                至少 8 个字符，别用常见密码或与你账号相关的字词。
              </span>
            )}
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="confirm">
              确认密码
            </label>
            <input
              id="confirm"
              className={styles.input}
              type="password"
              name="confirm"
              autoComplete="new-password"
              aria-invalid={visibleErrors.confirm ? 'true' : 'false'}
              aria-describedby={visibleErrors.confirm ? 'confirm-error' : undefined}
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              onBlur={() => setTouched((prev) => ({ ...prev, confirm: true }))}
              disabled={submitting}
            />
            {visibleErrors.confirm && (
              <span id="confirm-error" className={styles.fieldError}>
                {visibleErrors.confirm}
              </span>
            )}
          </div>

          {captchaRequired && (
            <CaptchaChallenge
              vid={captcha.data?.vid ?? ''}
              onVerify={handleCaptchaVerify}
              onUnavailable={handleCaptchaUnavailable}
              resetSignal={captchaReset}
            />
          )}

          <div className={styles.field}>
            <label className={styles.checkboxRow} htmlFor="accept-terms">
              <input
                id="accept-terms"
                type="checkbox"
                checked={acceptedTerms}
                onChange={(event) => {
                  setAcceptedTerms(event.target.checked);
                  if (event.target.checked) setTermsError('');
                }}
                disabled={submitting}
              />
              <span>
                我已阅读并同意
                <Link to="/legal/terms" target="_blank" rel="noreferrer">
                  《用户协议》
                </Link>
                与
                <Link to="/legal/privacy" target="_blank" rel="noreferrer">
                  《隐私政策》
                </Link>
              </span>
            </label>
            {termsError && <span className={styles.fieldError}>{termsError}</span>}
          </div>

          <button
            className={`btn btn-primary ${styles.submit}`}
            type="submit"
            disabled={submitDisabled}
          >
            {submitting ? '注册中…' : '注册'}
          </button>
        </form>

        <p className={styles.foot}>
          已有账号？<Link to="/login">直接登录</Link>
        </p>
        <p className={styles.quietFoot}>
          <Link to="/">← 先随便看看</Link>
        </p>
      </div>
    </div>
  );
}
