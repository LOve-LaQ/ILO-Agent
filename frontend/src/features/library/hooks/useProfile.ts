import { useQuery } from '@tanstack/react-query';

import { fetchProfile, meKeys } from '../../../shared/api/me';

/** 个人概览：账号信息 + 学习 / 收藏 / 行为汇总 */
export function useProfile() {
  return useQuery({
    queryKey: meKeys.profile(),
    queryFn: () => fetchProfile(),
    // 统计数字随收藏、学习、行为实时变化，收回焦点时重算一次即可，不必轮询
    staleTime: 15_000,
  });
}
