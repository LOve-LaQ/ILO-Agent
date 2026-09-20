import { useQuery } from '@tanstack/react-query';

import type { CardTab } from '../../../shared/types/card';
import { CARDS_PAGE_SIZE, fetchCards } from '../api';
import { useDiscoveryStore } from '../store';

/** 当前 Tab 的卡片列表 query key */
export function cardsQueryKey(tab: CardTab) {
  return ['cards', tab] as const;
}

/**
 * 拉取当前 Tab 的卡片池
 * 只请求激活的 Tab（与原 index.html 每次仅拉一个端点的行为一致），
 * 切换 Tab 时 queryKey 变化会自动触发一次新请求。
 */
export function useCards() {
  const activeTab = useDiscoveryStore((state) => state.activeTab);

  return useQuery({
    queryKey: cardsQueryKey(activeTab),
    queryFn: () => fetchCards(activeTab, CARDS_PAGE_SIZE),
    staleTime: 30_000,
  });
}
