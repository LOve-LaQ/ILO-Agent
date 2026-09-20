/**
 * `/me/*` 契约封装（shared 层）
 *
 * 【为什么放在 shared 而不是某个 feature】
 * 收藏这张卡片的能力被两个 feature 同时消费：
 * - `features/discovery`：卡片上的 🔖 开关（只要 id 集合，卡片不重渲染）
 * - `features/library`：收藏列表、学习记录、行为时间线、个人概览
 * 按「features 之间不互相 import」的边界约定，两端共用的能力下沉到 shared。
 *
 * 【契约边界】以下类型全部由后端 OpenAPI 派生，勿手写字段。
 * 权威定义见 backend/src/schemas/me.py，后端改动后执行 `npm run gen:api` 同步。
 */

import { api } from './client';
import type { components } from './schema';

export type MeProfileResponse = components['schemas']['MeProfileResponse'];
export type LearningStats = components['schemas']['LearningStats'];
export type BookmarkItem = components['schemas']['BookmarkItem'];
export type BookmarkListResponse = components['schemas']['BookmarkListResponse'];
export type BookmarkMutationResponse = components['schemas']['BookmarkMutationResponse'];
export type SessionItem = components['schemas']['SessionItem'];
export type SessionListResponse = components['schemas']['SessionListResponse'];
export type SessionDetailResponse = components['schemas']['SessionDetailResponse'];
export type SessionMessage = components['schemas']['SessionMessage'];
export type ActivityItem = components['schemas']['ActivityItem'];
export type ActivityListResponse = components['schemas']['ActivityListResponse'];
export type DataExportResponse = components['schemas']['DataExportResponse'];

/** 卡片收藏态一次拉全的条数（与后端 limit 上限一致） */
const BOOKMARK_PAGE_SIZE = 200;

/**
 * 查询键统一从这里出。
 *
 * 全部挂在 `['me']` 之下，于是收藏的增删只需 `invalidateQueries({queryKey: meKeys.all})`
 * 就能同时刷新收藏列表与个人概览里的统计数字，不必逐处列举。
 */
export const meKeys = {
  all: ['me'] as const,
  profile: () => [...meKeys.all, 'profile'] as const,
  bookmarks: () => [...meKeys.all, 'bookmarks'] as const,
  sessions: (state?: string) => [...meKeys.all, 'sessions', state ?? 'all'] as const,
  session: (sessionId: string) => [...meKeys.all, 'session', sessionId] as const,
  activities: (actionType?: string) => [...meKeys.all, 'activities', actionType ?? 'all'] as const,
};

/** GET /me/profile —— 账号信息 + 汇总统计 */
export function fetchProfile(): Promise<MeProfileResponse> {
  return api.get<MeProfileResponse>('/me/profile');
}

/**
 * GET /me/bookmarks
 *
 * 返回 `items`（含卡片快照，供列表直接渲染）与 `item_ids`（全部收藏 id，
 * 供发现页一次性渲染所有卡片的收藏态，不必按卡片逐个查）。
 */
export function fetchBookmarks(limit: number = BOOKMARK_PAGE_SIZE): Promise<BookmarkListResponse> {
  return api.get<BookmarkListResponse>(`/me/bookmarks?limit=${limit}`);
}

/** POST /me/bookmarks/{item_id} —— 幂等，重复收藏返回 changed=false */
export function addBookmark(itemId: string): Promise<BookmarkMutationResponse> {
  return api.post<BookmarkMutationResponse>(`/me/bookmarks/${encodeURIComponent(itemId)}`);
}

/** DELETE /me/bookmarks/{item_id} —— 幂等，没收藏过也返回成功 */
export function removeBookmark(itemId: string): Promise<BookmarkMutationResponse> {
  return api.delete<BookmarkMutationResponse>(`/me/bookmarks/${encodeURIComponent(itemId)}`);
}

/**
 * 取出收藏 id 集合。
 *
 * `item_ids` 在 Pydantic 里带默认值，于是 OpenAPI 把它标成可选字段；后端实际每次都显式
 * 赋值。这里统一收口成「拿到的一定是数组」，免得每个调用点各写一遍 `?? []`（写漏一处
 * 就会把「未知」误当成「没收藏」）。
 */
export function bookmarkIds(data: BookmarkListResponse | undefined): string[] {
  return data?.item_ids ?? [];
}

/** GET /me/sessions —— 学习记录列表（Redis 过期后仍可回看，真相源在 PostgreSQL） */
export function fetchSessions(options: {
  limit?: number;
  offset?: number;
  state?: string;
} = {}): Promise<SessionListResponse> {
  const params = new URLSearchParams();
  params.set('limit', String(options.limit ?? 20));
  if (options.offset) params.set('offset', String(options.offset));
  // 后端按 state 下推 SQL 过滤：空字符串会让「全部」变成「state = ''」，必须省略该参数
  if (options.state) params.set('state', options.state);
  return api.get<SessionListResponse>(`/me/sessions?${params.toString()}`);
}

/** GET /me/sessions/{session_id} —— 会话详情（含完整对话历史） */
export function fetchSessionDetail(sessionId: string): Promise<SessionDetailResponse> {
  return api.get<SessionDetailResponse>(`/me/sessions/${encodeURIComponent(sessionId)}`);
}

/** GET /me/activities —— 行为时间线；action_type 取值见后端 ACTION_TYPES */
export function fetchActivities(options: {
  limit?: number;
  offset?: number;
  actionType?: string;
} = {}): Promise<ActivityListResponse> {
  const params = new URLSearchParams();
  params.set('limit', String(options.limit ?? 50));
  if (options.offset) params.set('offset', String(options.offset));
  if (options.actionType) params.set('action_type', options.actionType);
  return api.get<ActivityListResponse>(`/me/activities?${params.toString()}`);
}

/**
 * GET /me/export —— 全量数据导出（资料 + 收藏 + 学习会话含对话 + 行为记录）。
 *
 * 后端以 `Content-Disposition: attachment` 返回 JSON；此处拿到的仍是可解析的对象，
 * 由前端再构造成可下载文件（见 shared/lib/download.ts）。
 */
export function fetchMyExport(): Promise<DataExportResponse> {
  return api.get<DataExportResponse>('/me/export');
}
