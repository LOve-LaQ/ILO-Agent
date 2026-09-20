import { api } from '../../shared/api/client';
import type { components } from '../../shared/api/schema';

/**
 * 认证接口调用。
 *
 * 【契约边界】以下类型全部由后端 OpenAPI 派生，勿手写字段。
 * 权威定义见 backend/src/schemas/auth.py，改动后端后执行 `npm run gen:api` 同步。
 */

export type RegisterRequest = components['schemas']['RegisterRequest'];
export type LoginRequest = components['schemas']['LoginRequest'];
export type TokenResponse = components['schemas']['TokenResponse'];
export type RefreshTokenResponse = components['schemas']['RefreshTokenResponse'];
export type LogoutResponse = components['schemas']['LogoutResponse'];
export type UserPublic = components['schemas']['UserPublic'];

export type CaptchaConfigResponse = components['schemas']['CaptchaConfigResponse'];
export type SimpleMessageResponse = components['schemas']['SimpleMessageResponse'];
export type PasswordForgotRequest = components['schemas']['PasswordForgotRequest'];
export type PasswordResetRequest = components['schemas']['PasswordResetRequest'];
export type PasswordChangeRequest = components['schemas']['PasswordChangeRequest'];
export type AccountDeleteRequest = components['schemas']['AccountDeleteRequest'];
export type AccountDeleteCancelRequest = components['schemas']['AccountDeleteCancelRequest'];
export type EmailConfirmRequest = components['schemas']['EmailConfirmRequest'];

export type AuthSessionItem = components['schemas']['AuthSessionItem'];
export type AuthSessionListResponse = components['schemas']['AuthSessionListResponse'];
export type SessionRevokeResponse = components['schemas']['SessionRevokeResponse'];

/** POST /auth/register —— 注册成功即进入登录态 */
export function register(body: RegisterRequest): Promise<TokenResponse> {
  return api.post<TokenResponse>('/auth/register', body);
}

/** POST /auth/login —— 邮箱或用户名 + 密码 */
export function login(body: LoginRequest): Promise<TokenResponse> {
  return api.post<TokenResponse>('/auth/login', body);
}

/**
 * POST /auth/refresh
 * 用 httpOnly Cookie 里的 refresh token 换新 access token；
 * refresh token 本身前端 JS 永远读不到，也不该读到。
 */
export function refreshTokens(): Promise<RefreshTokenResponse> {
  return api.post<RefreshTokenResponse>('/auth/refresh');
}

/** POST /auth/logout —— 清掉服务端 refresh Cookie（access token 由前端丢弃） */
export function logout(): Promise<LogoutResponse> {
  return api.post<LogoutResponse>('/auth/logout');
}

/**
 * GET /auth/me
 * @param accessToken 显式指定 token（启动引导阶段 store 里还没有 token 可注入）
 */
export function fetchMe(accessToken?: string): Promise<UserPublic> {
  return api.get<UserPublic>(
    '/auth/me',
    accessToken ? { headers: { Authorization: `Bearer ${accessToken}` } } : undefined,
  );
}

/** GET /auth/captcha —— 人机校验配置（vid 公开，key 绝不返回） */
export function fetchCaptchaConfig(): Promise<CaptchaConfigResponse> {
  return api.get<CaptchaConfigResponse>('/auth/captcha');
}

/** POST /auth/password/forgot —— 申请找回密码（响应与邮箱是否存在无关，防枚举） */
export function forgotPassword(body: PasswordForgotRequest): Promise<SimpleMessageResponse> {
  return api.post<SimpleMessageResponse>('/auth/password/forgot', body);
}

/** POST /auth/password/reset —— 用一次性令牌重置密码；成功后该账号全部会话被撤销 */
export function resetPassword(body: PasswordResetRequest): Promise<SimpleMessageResponse> {
  return api.post<SimpleMessageResponse>('/auth/password/reset', body);
}

/** POST /auth/password/change —— 已登录改密；其他设备会被下线 */
export function changePassword(body: PasswordChangeRequest): Promise<SimpleMessageResponse> {
  return api.post<SimpleMessageResponse>('/auth/password/change', body);
}

/** POST /auth/account/delete —— 申请注销账号（进入冷静期并立即停用） */
export function requestAccountDeletion(
  body: AccountDeleteRequest,
): Promise<SimpleMessageResponse> {
  return api.post<SimpleMessageResponse>('/auth/account/delete', body);
}

/** POST /auth/account/delete/cancel —— 冷静期内撤销注销申请 */
export function cancelAccountDeletion(
  body: AccountDeleteCancelRequest,
): Promise<SimpleMessageResponse> {
  return api.post<SimpleMessageResponse>('/auth/account/delete/cancel', body);
}

/**
 * POST /auth/email/confirm —— 确认邮箱（点击确认邮件链接后自动调）。
 *
 * 【幂等】后端重复确认返回完全相同的成功话术，所以 React StrictMode 下
 * effect 被故意重复执行两次也不会出错，无需额外去重。
 */
export function confirmEmail(body: EmailConfirmRequest): Promise<SimpleMessageResponse> {
  return api.post<SimpleMessageResponse>('/auth/email/confirm', body);
}

/** POST /auth/email/resend —— 重发确认邮件（需登录；确认邮件丢了的唯一补救入口） */
export function resendVerificationEmail(): Promise<SimpleMessageResponse> {
  return api.post<SimpleMessageResponse>('/auth/email/resend');
}

/** GET /auth/sessions —— 登录设备列表（与 /me/sessions 的学习记录不同） */
export function fetchAuthSessions(): Promise<AuthSessionListResponse> {
  return api.get<AuthSessionListResponse>('/auth/sessions');
}

/** DELETE /auth/sessions/{id} —— 下线指定设备 */
export function revokeAuthSession(sessionId: string): Promise<SessionRevokeResponse> {
  return api.delete<SessionRevokeResponse>(`/auth/sessions/${encodeURIComponent(sessionId)}`);
}

/** DELETE /auth/sessions —— 下线除当前设备外的全部设备 */
export function revokeOtherAuthSessions(): Promise<SessionRevokeResponse> {
  return api.delete<SessionRevokeResponse>('/auth/sessions');
}
