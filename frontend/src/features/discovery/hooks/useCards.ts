import { useQuery } from '@tanstack/react-query';

import type { CardTab } from '../../../shared/types/card';
import { CARDS_PAGE_SIZE, fetchCards } from '../api';

/** 某个 Tab 的卡片列表 query key */
export function cardsQueryKey(tab: CardTab) {
  return ['cards', tab] as const;
}

interface UseCardsOptions {
  /** 榜单视图里卡片池不必请求（省一次 Qdrant 检索 + 首屏流量） */
  enabled?: boolean;
}

/**
 * 拉取指定 Tab 的卡片池
 *
 * 【为什么 tab 由调用方传入，而不是从 store 里读】store 里现在有三个视图
 * （仓库 / 文章 / 榜单），而卡片池只有两个。让 hook 自己去读 `activeView`
 * 就必然要写 `activeView === 'ranking' ? 'repo' : activeView` 这种兜底 ——
 * 兜底意味着「在榜单视图下偷偷请求仓库卡」，用户看不到，配额照样花。
 * 参数化之后，「要不要请求」这个决定留在页面层，一眼可见。
 */
export function useCards(tab: CardTab, options: UseCardsOptions = {}) {
  const { enabled = true } = options;

  return useQuery({
    queryKey: cardsQueryKey(tab),
    queryFn: () => fetchCards(tab, CARDS_PAGE_SIZE),
    staleTime: 30_000,
    enabled,
  });
}
