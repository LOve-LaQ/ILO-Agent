import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useToastStore } from '../../../components/ui/toastStore';
import { ApiError } from '../../../shared/api/client';
import {
  adminKeys,
  fetchAdminUsers,
  fetchFeatureFlags,
  fetchLlmUsage,
  updateFeatureFlag,
  updateUserRole,
  type FeatureFlagListResponse,
} from '../api';

/** 把接口错误翻译成一句用户能看懂的话 */
function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'ADMIN_REQUIRED') return '该操作需要管理员权限。';
    if (error.code === 'CANNOT_CHANGE_OWN_ROLE') return '不能修改自己的角色。';
    if (error.code === 'ROLE_MANAGED_BY_CONFIG') return error.message;
    return error.message;
  }
  return '操作失败，请稍后重试。';
}

/** 全部功能开关 */
export function useFeatureFlags() {
  return useQuery({
    queryKey: adminKeys.features(),
    queryFn: fetchFeatureFlags,
    // 后台数据变动只来自本页操作，成功后会主动失效缓存，不需要轮询
    staleTime: 30_000,
  });
}

/**
 * 切换开关。
 *
 * 不用乐观更新：开关的作用是「立刻止血」，界面必须显示**服务端确认过**的真实状态。
 * 乐观更新在失败时会先闪成新状态再弹回，管理员会误以为已经关掉了。
 */
export function useToggleFlag() {
  const queryClient = useQueryClient();
  const showToast = useToastStore((state) => state.show);

  return useMutation({
    mutationFn: ({ key, enabled }: { key: string; enabled: boolean }) =>
      updateFeatureFlag(key, enabled),
    onSuccess: (item) => {
      // 直接写回列表，避免整页重新拉取时的闪烁
      queryClient.setQueryData<FeatureFlagListResponse>(adminKeys.features(), (prev) =>
        prev
          ? {
              ...prev,
              items: prev.items.map((flag) => (flag.key === item.key ? item : flag)),
            }
          : prev,
      );
      showToast(`${item.description || item.key} 已${item.enabled ? '开启' : '关闭'}`);
    },
    onError: (error) => showToast(errorText(error)),
  });
}

/** 用户列表（带搜索） */
export function useAdminUsers(search: string) {
  return useQuery({
    queryKey: adminKeys.users(search),
    queryFn: () => fetchAdminUsers(search),
    staleTime: 15_000,
  });
}

/** 调整某个用户的角色 */
export function useUpdateRole() {
  const queryClient = useQueryClient();
  const showToast = useToastStore((state) => state.show);

  return useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: string }) =>
      updateUserRole(userId, role),
    onSuccess: (item) => {
      // 用户列表按搜索词分了多个缓存 key，全部失效最稳妥
      void queryClient.invalidateQueries({ queryKey: adminKeys.all });
      showToast(`${item.username} 的角色已改为 ${item.role}`);
    },
    onError: (error) => showToast(errorText(error)),
  });
}

/**
 * LLM 成本面板数据。
 *
 * 【为什么 staleTime 只有 30s 且不做轮询】成本不是实时指标：账本按次写入、
 * 分钟级延迟完全可以接受。轮询只会让管理页在后台持续打请求（还可能顺带
 * 触发限流），换来的却是一个变来变去的数字。需要最新值时点「刷新」。
 */
export function useLlmUsage(days: number) {
  return useQuery({
    queryKey: adminKeys.llmUsage(days),
    queryFn: () => fetchLlmUsage(days),
    staleTime: 30_000,
  });
}
