import { useCallback, useMemo, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';

import { useToastStore } from '../../../components/ui/toastStore';
import { ApiError } from '../../../shared/api/client';
import { fetchMyExport } from '../../../shared/api/me';
import { downloadJsonFile } from '../../../shared/lib/download';
import { fmtDateTime, fmtRelative } from '../../../shared/lib/format';
import { changePassword, resendVerificationEmail } from '../api';
import { useAuthSessions, useRevokeOtherSessions, useRevokeSession } from '../hooks/useAuthSessions';
import { useCaptchaConfig } from '../hooks/useCaptchaConfig';
import { useAuthStore } from '../store';
import { checkPassword, passwordStrength } from '../validation';
import { CaptchaChallenge, type CaptchaResult } from './CaptchaChallenge';
import styles from './Auth.module.css';

function isCaptchaError(code: string | undefined): boolean {
  return (
    code === 'CAPTCHA_REQUIRED' || code === 'CAPTCHA_FAILED' || code === 'CAPTCHA_MISCONFIGURED'
  );
}

/**
 * 账号安全页（需登录）：登录设备管理 + 修改密码 + 数据导出 + 注销入口。
 *
 * 【为什么把设备管理放这里】改密会撤销「除当前设备外」的全部会话（后端行为），
 * 用户需要一个地方看到「现在有哪些设备在登录」，否则改密后「其他设备被下线」是不可见的。
 */
export function AccountSecurityPage() {
  const user = useAuthStore((state) => state.user);
  const showToast = useToastStore((state) => state.show);

  const sessions = useAuthSessions();
  const revokeOne = useRevokeSession();
  const revokeOthers = useRevokeOtherSessions();

  // ===== 修改密码 =====
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [pwError, setPwError] = useState('');
  const [pwDone, setPwDone] = useState('');
  const [pwSubmitting, setPwSubmitting] = useState(false);

  const captcha = useCaptchaConfig();
  const captchaRequired = captcha.data?.enabled === true;
  const [captchaResult, setCaptchaResult] = useState<CaptchaResult | null>(null);
  const [captchaBlocked, setCaptchaBlocked] = useState(false);
  const [captchaReset, setCaptchaReset] = useState(0);

  const newPasswordError = useMemo(
    () =>
      newPassword
        ? checkPassword(newPassword, { username: user?.username, email: user?.email }) ?? ''
        : '',
    [newPassword, user?.username, user?.email],
  );
  const strength = useMemo(() => passwordStrength(newPassword), [newPassword]);

  const handleCaptchaVerify = useCallback(
    (result: CaptchaResult | null) => setCaptchaResult(result),
    [],
  );
  const handleCaptchaUnavailable = useCallback(
    (unavailable: boolean) => setCaptchaBlocked(unavailable),
    [],
  );
  const captchaIncomplete = captchaRequired && (captchaBlocked || !captchaResult);

  async function handleChangePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pwSubmitting) return;

    if (!currentPassword || !newPassword) {
      setPwError('请填写当前密码与新密码。');
      return;
    }
    const reason = checkPassword(newPassword, { username: user?.username, email: user?.email });
    if (reason) {
      setPwError(reason);
      return;
    }
    if (confirm !== newPassword) {
      setPwError('两次输入的新密码不一致。');
      return;
    }

    setPwSubmitting(true);
    setPwError('');
    setPwDone('');
    try {
      const data = await changePassword({
        current_password: currentPassword,
        new_password: newPassword,
        captcha_token: captchaRequired ? (captchaResult?.token ?? null) : null,
        captcha_knock: captchaRequired ? (captchaResult?.knock ?? null) : null,
        captcha_dfu: captchaRequired ? (captchaResult?.dfu ?? null) : null,
        captcha_ip: captchaRequired ? (captchaResult?.ip ?? null) : null,
      });
      setPwDone(data.message);
      setCurrentPassword('');
      setNewPassword('');
      setConfirm('');
      setCaptchaReset((value) => value + 1);
      showToast('密码已修改');
    } catch (cause) {
      if (cause instanceof ApiError) {
        if (isCaptchaError(cause.code)) setCaptchaReset((value) => value + 1);
        setPwError(cause.message);
      } else {
        setPwError('修改失败，请稍后重试。');
      }
    } finally {
      setPwSubmitting(false);
    }
  }

  // ===== 数据导出 =====
  const [exporting, setExporting] = useState(false);

  async function handleExport() {
    if (exporting) return;
    setExporting(true);
    try {
      const data = await fetchMyExport();
      downloadJsonFile(data, `ilo-export-${user?.username ?? 'me'}.json`);
      showToast('数据已导出');
    } catch (cause) {
      showToast(cause instanceof ApiError ? cause.message : '导出失败，请稍后重试');
    } finally {
      setExporting(false);
    }
  }

  // ===== 邮箱验证 =====
  const [resending, setResending] = useState(false);

  async function handleResendVerification() {
    if (resending) return;
    setResending(true);
    try {
      const data = await resendVerificationEmail();
      showToast(data.message);
    } catch (cause) {
      showToast(cause instanceof ApiError ? cause.message : '发送失败，请稍后重试');
    } finally {
      setResending(false);
    }
  }

  const sessionItems = sessions.data?.items ?? [];

  return (
    <div className={styles.page}>
      <div className={`${styles.card} ${styles.wideCard}`}>
        <div className={styles.brand}>
          <span className={styles.brandMark}>I</span>
          <span>账号安全</span>
        </div>

        {/* ============ 登录设备 ============ */}
        <h2 className={styles.title}>登录设备</h2>
        <p className={styles.sub}>改密或下线其他设备后，对应设备上的登录会立即失效。</p>

        <div className={styles.infoList}>
          {sessions.isPending && <span className={styles.muted}>加载中…</span>}
          {sessions.isError && <span className={styles.fieldError}>设备列表加载失败。</span>}
          {sessionItems.map((item) => (
            <div key={item.id} className={styles.infoRow}>
              <span>
                {item.user_agent ? item.user_agent.slice(0, 48) : '未知设备'}
                {item.current && <strong>（当前设备）</strong>}
                <br />
                <small>
                  最近活跃 {fmtRelative(item.last_seen_at)} · 创建于 {fmtDateTime(item.created_at)}
                </small>
              </span>
              {!item.current && (
                <button
                  type="button"
                  className="mini-btn"
                  onClick={() => revokeOne.mutate(item.id)}
                  disabled={revokeOne.isPending}
                >
                  下线
                </button>
              )}
            </div>
          ))}
          {!sessions.isPending && !sessions.isError && sessionItems.length === 0 && (
            <span className={styles.muted}>没有其他登录设备。</span>
          )}
        </div>

        {sessionItems.length > 1 && (
          <div className={styles.inlineActions}>
            <button
              type="button"
              className="mini-btn"
              onClick={() => revokeOthers.mutate()}
              disabled={revokeOthers.isPending}
            >
              下线其他所有设备
            </button>
          </div>
        )}

        {/* ============ 修改密码 ============ */}
        <h2 className={styles.title} style={{ marginTop: 26 }}>
          修改密码
        </h2>
        <p className={styles.sub}>修改后，除当前设备外的其他设备会被立即下线。</p>

        <form className={styles.form} onSubmit={handleChangePassword} noValidate>
          {pwError && (
            <div className={styles.alert} role="alert">
              {pwError}
            </div>
          )}
          {pwDone && (
            <div className={styles.notice} role="status">
              {pwDone}
            </div>
          )}

          <div className={styles.field}>
            <label className={styles.label} htmlFor="current-password">
              当前密码
            </label>
            <input
              id="current-password"
              className={styles.input}
              type="password"
              autoComplete="current-password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
              disabled={pwSubmitting}
            />
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="new-password">
              新密码
            </label>
            <input
              id="new-password"
              className={styles.input}
              type="password"
              autoComplete="new-password"
              aria-invalid={newPasswordError ? 'true' : 'false'}
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              disabled={pwSubmitting}
            />
            {newPassword && (
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
            {newPasswordError && <span className={styles.fieldError}>{newPasswordError}</span>}
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="confirm-new-password">
              确认新密码
            </label>
            <input
              id="confirm-new-password"
              className={styles.input}
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              disabled={pwSubmitting}
            />
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
            disabled={pwSubmitting || captchaIncomplete}
          >
            {pwSubmitting ? '提交中…' : '修改密码'}
          </button>
        </form>

        {/* ============ 邮箱验证 ============ */}
        {/* 注册不阻塞邮箱校验（只用人机校验），因此「收不到确认邮件」是用户发现
            自己把邮箱填错的唯一信号。这里就是那个信号的回执处。 */}
        <h2 className={styles.title} style={{ marginTop: 26 }}>
          邮箱验证
        </h2>
        <p className={styles.sub}>
          {user?.email_verified
            ? '这个邮箱已确认可以正常收信。'
            : '这个邮箱还没有确认。若它不是你的常用邮箱，将来忘记密码时会收不到重置邮件。'}
        </p>
        {user && !user.email_verified && (
          <div className={styles.inlineActions}>
            <button
              type="button"
              className="mini-btn"
              onClick={() => void handleResendVerification()}
              disabled={resending}
            >
              {resending ? '发送中…' : '重新发送确认邮件'}
            </button>
          </div>
        )}

        {/* ============ 数据与账号 ============ */}
        <h2 className={styles.title} style={{ marginTop: 26 }}>
          数据与账号
        </h2>
        <p className={styles.sub}>你可以随时导出自己的全部数据，或申请注销账号。</p>
        <div className={styles.inlineActions}>
          <button
            type="button"
            className="mini-btn"
            onClick={() => void handleExport()}
            disabled={exporting}
          >
            {exporting ? '导出中…' : '导出我的数据（JSON）'}
          </button>
          <Link className="mini-btn" to="/account/delete">
            注销账号
          </Link>
        </div>

        <p className={styles.quietFoot}>
          <Link to="/">← 返回首页</Link>
        </p>
      </div>
    </div>
  );
}
