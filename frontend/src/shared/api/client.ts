/**
 * 统一 HTTP 客户端
 *
 * 职责：
 * - 统一 baseURL（开发环境走 vite proxy 的同源 /api/v1）
 * - JSON 序列化与解析
 * - 把非 2xx 响应归一成 ApiError（后端统一错误契约 {code,message,detail}）
 * - 权限边界：自动注入 access token；401 时单飞刷新并重放一次，
 *   仍救不回来才通知业务层（清理登录态并引导登录）
 *
 * 【模块边界】本文件属于 shared 层，**不得 import 任何 feature**。
 * token 从哪来、刷新失败怎么办，都由 features/auth 通过 setAuthHandlers 注入。
 */

import type { components } from './schema';

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api/v1';

/** 统一错误信封，由 OpenAPI 派生（后端 src/schemas/common.py 的 ErrorResponse） */
export type ApiErrorBody = components['schemas']['ErrorResponse'];

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly detail?: unknown;

  constructor(status: number, message: string, code?: string, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }

  /** 网络层失败（后端未启动 / 断网），status 为 0 */
  get isNetworkError(): boolean {
    return this.status === 0;
  }

  /** 登录态失效类错误（凭证过期 / 无效 / 未携带） */
  get isAuthExpired(): boolean {
    return this.status === 401 && REFRESHABLE_CODES.has(this.code ?? '');
  }
}

/**
 * 只有这些 401 错误码代表「access token 本身有问题」，
 * 才值得拿 refresh cookie 去换一张新的。
 * USER_DISABLED 等属权限问题，刷新解决不了。
 */
const REFRESHABLE_CODES = new Set([
  'NOT_AUTHENTICATED',
  'TOKEN_EXPIRED',
  'TOKEN_INVALID',
  'USER_NOT_FOUND',
]);

/**
 * 凭证类接口的 401 含义是「用户名密码不对」或「refresh cookie 也没有」，
 * 既不能触发刷新重放（会递归），也不应触发「登录态失效」跳转。
 */
const CREDENTIAL_PATHS = new Set(['/auth/login', '/auth/register', '/auth/refresh']);

export interface AuthHandlers {
  /** 同步读取当前 access token（内存态） */
  getAccessToken: () => string | null;
  /** 用 httpOnly refresh cookie 换新 access token；失败返回 null（不抛） */
  refreshAccessToken: () => Promise<string | null>;
  /** 刷新也救不回来时调用：业务层应清理登录态并引导重新登录 */
  onAuthRequired: () => void;
}

let authHandlers: AuthHandlers | null = null;

/** 由 features/auth 在应用启动时调用一次 */
export function setAuthHandlers(handlers: AuthHandlers): void {
  authHandlers = handlers;
}

/**
 * 单飞刷新：并发请求同时 401 时只发一次 refresh，其余复用同一个 Promise。
 * 否则 N 个并行请求会打出 N 次刷新，refresh cookie 轮换场景下会互相作废。
 */
let refreshInFlight: Promise<string | null> | null = null;

function refreshOnce(): Promise<string | null> {
  if (!authHandlers) return Promise.resolve(null);
  if (refreshInFlight) return refreshInFlight;

  const pending = authHandlers.refreshAccessToken().catch(() => null);
  refreshInFlight = pending.finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return send<T>(path, options, false);
}

async function send<T>(path: string, options: RequestOptions, retried: boolean): Promise<T> {
  const { body, headers, ...rest } = options;

  const finalHeaders = new Headers(headers);
  if (body !== undefined && !finalHeaders.has('Content-Type')) {
    finalHeaders.set('Content-Type', 'application/json');
  }
  // 调用方显式传了 Authorization 就尊重它（例如启动引导阶段还没有 store 里的 token）
  const token = authHandlers?.getAccessToken() ?? null;
  if (token && !finalHeaders.has('Authorization')) {
    finalHeaders.set('Authorization', `Bearer ${token}`);
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...rest,
      headers: finalHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    throw new ApiError(0, '网络请求失败，请确认后端服务已启动', 'NETWORK_ERROR', cause);
  }

  if (response.ok) {
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  const error = await toApiError(response);

  if (response.status === 401) {
    const isCredentialCall = CREDENTIAL_PATHS.has(path);

    if (!isCredentialCall && !retried && error.isAuthExpired) {
      const freshToken = await refreshOnce();
      if (freshToken) {
        return send<T>(path, options, true);
      }
    }

    if (!isCredentialCall) {
      authHandlers?.onAuthRequired();
    }
  }

  throw error;
}

async function toApiError(response: Response): Promise<ApiError> {
  let payload: ApiErrorBody | null = null;
  try {
    payload = (await response.json()) as ApiErrorBody;
  } catch {
    payload = null;
  }

  const detailMessage = typeof payload?.detail === 'string' ? payload.detail : '';
  const message = payload?.message || detailMessage || `请求失败（HTTP ${response.status}）`;

  return new ApiError(response.status, message, payload?.code, payload?.detail);
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) => request<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'POST', body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'DELETE' }),
};
