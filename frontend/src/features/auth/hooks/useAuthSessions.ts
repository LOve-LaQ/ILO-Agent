import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useToastStore } from '../../../components/ui/toastStore';
import { ApiError } from '../../../shared/api/client';
import {
  fetchAuthSessions,
  revokeAuthSession,
  revokeOtherAuthSessions,
  type AuthSessionListResponse,
} from '../api';

const SESSIONS_KEY = ['auth', 'sessions'] as const;

/** 登录设备列表（GET /auth/sessions） */
export function useAuthSessions() {
  return useQuery({
    queryKey: SESSIONS_KEY,
    queryFn: fetchAuthSessions,
    staleTime: 10_000,
  });
}

function errorMessage(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.message : fallback;
}

/** 下线指定设备（DELETE /auth/sessions/{id}） */
export function useRevokeSession() {
  const queryClient = useQueryClient();
  const showToast = useToastStore((state) => state.show);

  return useMutation({
    mutationFn: (sessionId: string) => revokeAuthSession(sessionId),
    onSuccess: (data) => {
      showToast(data.message);
      void queryClient.invalidateQueries({ queryKey: SESSIONS_KEY });
    },
    onError: (cause) => {
      showToast(errorMessage(cause, '下线失败，请稍后重试'));
    },
  });
}

/** 下线除当前设备外的全部设备（DELETE /auth/sessions） */
export function useRevokeOtherSessions() {
  const queryClient = useQueryClient();
  const showToast = useToastStore((state) => state.show);

  return useMutation({
    mutationFn: () => revokeOtherAuthSessions(),
    onSuccess: (data: Awaited<ReturnType<typeof revokeOtherAuthSessions>>) => {
      showToast(data.message);
      void queryClient.invalidateQueries({ queryKey: SESSIONS_KEY });
    },
    onError: (cause) => {
      showToast(errorMessage(cause, '操作失败，请稍后重试'));
    },
  });
}

export type { AuthSessionListResponse };
