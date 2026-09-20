import { useQuery } from '@tanstack/react-query';

import { fetchSessionDetail, fetchSessions, meKeys } from '../../../shared/api/me';

/** 学习记录列表；`state` 为 undefined 表示不过滤 */
export function useSessions(state?: string) {
  return useQuery({
    queryKey: meKeys.sessions(state),
    queryFn: () => fetchSessions({ state, limit: 50 }),
    staleTime: 30_000,
  });
}

/**
 * 单个学习会话详情（含完整对话历史）
 *
 * `enabled` 用「有没有 id」而不是「列表里有没有这条」来判定：列表接口的 `total` 可能
 * 大于本页 `items`，没出现在列表里不代表取不到。id 为空时才真的没必要发请求。
 */
export function useSessionDetail(sessionId: string | null) {
  return useQuery({
    queryKey: meKeys.session(sessionId ?? ''),
    queryFn: () => fetchSessionDetail(sessionId as string),
    enabled: sessionId !== null,
    staleTime: 5 * 60_000,
  });
}
