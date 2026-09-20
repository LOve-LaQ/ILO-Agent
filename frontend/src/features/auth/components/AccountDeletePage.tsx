import { useCallback, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';

import { useToastStore } from '../../../components/ui/toastStore';
import { ApiError } from '../../../shared/api/client';
import { cancelAccountDeletion, requestAccountDeletion } from '../api';
import { resetClientState } from '../session';
import { useAuthStore } from '../store';
import { useCaptchaConfig } from '../hooks/useCaptchaConfig';
import { CaptchaChallenge, type CaptchaResult } from './CaptchaChallenge';
import styles from './Auth.module.css';

function isCaptchaError(code: string | undefined): boolean {
  return (
    code === 'CAPTCHA_REQUIRED' || code === 'CAPTCHA_FAILED' || code === 'CAPTCHA_MISCONFIGURED'
  );
}

/**
 * 账号注销页。
 *
 * 两个互斥的入口放在同一页：
 * - **申请注销**（需登录）：输入密码 + 用户名二次确认，进入冷静期后账号立即停用；
 * - **撤销注销**：冷静期内账号已停用、无法正常登录，因此用「标识 + 密码」自证身份，
 *   所以这一段不要求登录态，谁（知道凭证的本人）都能来撤销。
 */
export function AccountDeletePage() {
  const status = useAuthStore((state) => state.status);
  const user = useAuthStore((state) => state.user);
  const showToast = useToastStore((state) => state.show);

  const captcha = useCaptchaConfig();
  const captchaRequired = captcha.data?.enabled === true;
  const [captchaResult, setCaptchaResult] = useState<CaptchaResult | null>(null);
  const [captchaBlocked, setCaptchaBlocked] = useState(false);
  const [captchaReset, setCaptchaReset] = useState(0);
  const handleCaptchaVerify = useCallback(
    (result: CaptchaResult | null) => setCaptchaResult(result),
    [],
  );
  const handleCaptchaUnavailable = useCallback(
    (unavailable: boolean) => setCaptchaBlocked(unavailable),
    [],
  );
  const captchaIncomplete = captchaRequired && (captchaBlocked || !captchaResult);

  // ===== 申请注销 =====
  const [password, setPassword] = useState('');
  const [confirmUsername, setConfirmUsername] = useState('');
  const [error, setError] = useState('');
  const [done, setDone] = useState('');
  const [submitting, setSubmitting] = useState(false);

  async function handleDelete(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    if (!password) {
      setError('请输入当前密码。');
      return;
    }
    if (confirmUsername.trim() !== (user?.username ?? '')) {
      setError('用户名不匹配，请准确输入你的用户名以确认。');
      return;
    }

    setSubmitting(true);
    setError('');
    try {
      const data = await requestAccountDeletion({
        password,
        confirm_username: confirmUsername.trim(),
        captcha_token: captchaRequired ? (captchaResult?.token ?? null) : null,
        captcha_knock: captchaRequired ? (captchaResult?.knock ?? null) : null,
        captcha_dfu: captchaRequired ? (captchaResult?.dfu ?? null) : null,
        captcha_ip: captchaRequired ? (captchaResult?.ip ?? null) : null,
      });
      setDone(data.message);
      // 账号已停用、全部会话已撤销：本地也一并退出，避免停在「假登录态」
      resetClientState();
      useAuthStore.getState().clear();
      showToast('注销申请已受理');
    } catch (cause) {
      if (cause instanceof ApiError) {
        if (isCaptchaError(cause.code)) setCaptchaReset((value) => value + 1);
        setError(cause.message);
      } else {
        setError('提交失败，请稍后重试。');
      }
    } finally {
      setSubmitting(false);
    }
  }

  // ===== 撤销注销 =====
  const [cancelIdentifier, setCancelIdentifier] = useState('');
  const [cancelPassword, setCancelPassword] = useState('');
  const [cancelError, setCancelError] = useState('');
  const [cancelDone, setCancelDone] = useState('');
  const [cancelSubmitting, setCancelSubmitting] = useState(false);

  async function handleCancel(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (cancelSubmitting) return;
    if (!cancelIdentifier.trim() || !cancelPassword) {
      setCancelError('请输入邮箱/用户名与密码。');
      return;
    }

    setCancelSubmitting(true);
    setCancelError('');
    try {
      const data = await cancelAccountDeletion({
        identifier: cancelIdentifier.trim(),
        password: cancelPassword,
      });
      setCancelDone(data.message);
      showToast('已撤销注销申请');
    } catch (cause) {
      setCancelError(cause instanceof ApiError ? cause.message : '操作失败，请稍后重试。');
    } finally {
      setCancelSubmitting(false);
    }
  }

  return (
    <div className={styles.page}>
      <div className={`${styles.card} ${styles.wideCard}`}>
        <div className={styles.brand}>
          <span className={styles.brandMark}>I</span>
          <span>账号注销</span>
        </div>

        {status === 'authenticated' ? (
          <>
            <h1 className={styles.title}>注销账号</h1>
            <p className={styles.sub}>
              提交后账号将在冷静期（默认 15 天）后被永久删除。冷静期内可撤销。
            </p>

            <form className={styles.form} onSubmit={handleDelete} noValidate>
              {error && (
                <div className={styles.alert} role="alert">
                  {error}
                </div>
              )}
              {done && (
                <div className={styles.notice} role="status">
                  {done}
                </div>
              )}

              <div className={styles.field}>
                <label className={styles.label} htmlFor="delete-password">
                  当前密码
                </label>
                <input
                  id="delete-password"
                  className={styles.input}
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  disabled={submitting || Boolean(done)}
                />
              </div>

              <div className={styles.field}>
                <label className={styles.label} htmlFor="confirm-username">
                  输入用户名以确认（<strong>{user?.username}</strong>）
                </label>
                <input
                  id="confirm-username"
                  className={styles.input}
                  value={confirmUsername}
                  onChange={(event) => setConfirmUsername(event.target.value)}
                  disabled={submitting || Boolean(done)}
                />
              </div>

              {captchaRequired && !done && (
                <CaptchaChallenge
                  vid={captcha.data?.vid ?? ''}
                  onVerify={handleCaptchaVerify}
                  onUnavailable={handleCaptchaUnavailable}
                  resetSignal={captchaReset}
                />
              )}

              <button
                className={`btn ${styles.submit}`}
                type="submit"
                disabled={submitting || captchaIncomplete || Boolean(done)}
                style={{ background: 'var(--danger)', color: '#fff' }}
              >
                {submitting ? '提交中…' : '申请注销账号'}
              </button>
            </form>
          </>
        ) : (
          <>
            <h1 className={styles.title}>注销账号</h1>
            <p className={styles.sub}>申请注销需要先登录。若你已申请注销，可在下方撤销。</p>
            <p className={styles.foot}>
              <Link to="/login">先去登录</Link>
            </p>
          </>
        )}

        <h2 className={styles.title} style={{ marginTop: 26 }}>
          撤销注销申请
        </h2>
        <p className={styles.sub}>冷静期内可随时撤销，撤销后账号立即恢复。</p>

        <form className={styles.form} onSubmit={handleCancel} noValidate>
          {cancelError && (
            <div className={styles.alert} role="alert">
              {cancelError}
            </div>
          )}
          {cancelDone && (
            <div className={styles.notice} role="status">
              {cancelDone}
            </div>
          )}

          <div className={styles.field}>
            <label className={styles.label} htmlFor="cancel-identifier">
              邮箱或用户名
            </label>
            <input
              id="cancel-identifier"
              className={styles.input}
              autoComplete="username"
              value={cancelIdentifier}
              onChange={(event) => setCancelIdentifier(event.target.value)}
              disabled={cancelSubmitting}
            />
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="cancel-password">
              密码
            </label>
            <input
              id="cancel-password"
              className={styles.input}
              type="password"
              autoComplete="current-password"
              value={cancelPassword}
              onChange={(event) => setCancelPassword(event.target.value)}
              disabled={cancelSubmitting}
            />
          </div>

          <button className={`btn ${styles.submit}`} type="submit" disabled={cancelSubmitting}>
            {cancelSubmitting ? '提交中…' : '撤销注销申请'}
          </button>
        </form>

        <p className={styles.quietFoot}>
          <Link to="/">← 返回首页</Link>
        </p>
      </div>
    </div>
  );
}
