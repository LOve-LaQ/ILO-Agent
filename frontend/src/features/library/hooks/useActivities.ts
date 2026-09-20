import { useQuery } from '@tanstack/react-query';

import { fetchActivities, meKeys } from '../../../shared/api/me';

/** 行为时间线；`actionType` 为 undefined 表示全部动作 */
export function useActivities(actionType?: string) {
  return useQuery({
    queryKey: meKeys.activities(actionType),
    queryFn: () => fetchActivities({ actionType, limit: 100 }),
    staleTime: 15_000,
  });
}
