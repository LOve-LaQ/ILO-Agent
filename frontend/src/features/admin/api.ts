import { api } from '../../shared/api/client';
import type { components } from '../../shared/api/schema';

/**
 * 管理后台接口。
 *
 * 【契约边界】类型全部由后端 OpenAPI 派生，勿手写字段。
 * 权威定义见 backend/src/schemas/admin.py，改动后端后执行 `npm run gen:api`。
 */

export type FeatureFlagItem = components['schemas']['FeatureFlagItem'];
export type FeatureFlagListResponse = components['schemas']['FeatureFlagListResponse'];
export type AdminUserItem = components['schemas']['AdminUserItem'];
export type AdminUserListResponse = components['schemas']['AdminUserListResponse'];
export type LlmUsageResponse = components['schemas']['LlmUsageResponse'];
export type DailyCostPoint = components['schemas']['DailyCostPoint'];
export type PurposeCostItem = components['schemas']['PurposeCostItem'];
export type CostForecast = components['schemas']['CostForecast'];

export const adminKeys = {
  all: ['admin'] as const,
  features: () => ['admin', 'features'] as const,
  users: (search: string) => ['admin', 'users', search] as const,
  llmUsage: (days: number) => ['admin', 'llm-usage', days] as const,
};

/** GET /admin/features —— 全部功能开关（含代码默认值） */
export function fetchFeatureFlags(): Promise<FeatureFlagListResponse> {
  return api.get<FeatureFlagListResponse>('/admin/features');
}

/** PATCH /admin/features/{key} —— 开关某个模块，立即生效 */
export function updateFeatureFlag(key: string, enabled: boolean): Promise<FeatureFlagItem> {
  return api.patch<FeatureFlagItem>(`/admin/features/${encodeURIComponent(key)}`, { enabled });
}

/** GET /admin/users —— 用户列表（邮箱 / 用户名模糊搜索） */
export function fetchAdminUsers(search: string): Promise<AdminUserListResponse> {
  const query = search.trim() ? `?search=${encodeURIComponent(search.trim())}` : '';
  return api.get<AdminUserListResponse>(`/admin/users${query}`);
}

/** PATCH /admin/users/{id}/role —— 调整角色 */
export function updateUserRole(userId: string, role: string): Promise<AdminUserItem> {
  return api.patch<AdminUserItem>(`/admin/users/${encodeURIComponent(userId)}/role`, { role });
}

/**
 * GET /admin/llm/usage —— 成本面板
 *
 * 预测值一律由后端给（`forecast`），前端**不做二次计算**：两边各算一遍必然出现
 * 「面板说 12 元、账单说 9 元」这种谁也解释不清的差异。
 */
export function fetchLlmUsage(days: number): Promise<LlmUsageResponse> {
  return api.get<LlmUsageResponse>(`/admin/llm/usage?days=${days}`);
}
