import { useQuery } from '@tanstack/react-query';

import { fetchProvenance } from '../api';

/**
 * 一张卡片的内容溯源
 *
 * 只在用户主动展开溯源面板时才请求（`enabled` 由是否展开决定）：收藏列表上的
 * 每一行都发一次溯源请求，等于把「按需查看」变成「必然发生的 N 次请求」。
 */
export function useProvenance(cardId: string | null) {
  return useQuery({
    queryKey: ['provenance', cardId ?? ''],
    queryFn: () => fetchProvenance(cardId as string),
    enabled: cardId !== null,
    // 溯源记录是一经写入就不再变化的事实，缓存久一点
    staleTime: 10 * 60_000,
  });
}
