import { useQuery } from '@tanstack/react-query';

import { fetchSimilarCards } from '../../../shared/api/cardContent';

/** 相似推荐的 query key：卡片一变就必须换 key，否则会看到上一张卡片的推荐 */
export function similarQueryKey(cardId: string | null) {
  return ['similar', cardId] as const;
}

/**
 * 拉取当前卡片的相似项目
 *
 * - `enabled` 由调用方控制：只在「阅读态」打开时才请求。用户点开抽屉是为了
 *   读原文，推荐是顺带的 —— 不该在抽屉刚弹出、原文还没到手时就多打一个接口。
 * - `staleTime` 10 分钟：相似度的变化只来自知识库新增，分钟级波动没有意义；
 *   在同一次阅读里来回切「原文 / 导读」也不该重复请求。
 * - `retry: false`：这是增强内容，失败就整块不显示。默认的 3 次重试会让
 *   用户在一个已经降级的区块上等上十几秒，还平白多打三次接口。
 */
export function useSimilarCards(cardId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: similarQueryKey(cardId),
    queryFn: () => fetchSimilarCards(cardId as string),
    enabled: enabled && Boolean(cardId),
    staleTime: 10 * 60_000,
    retry: false,
  });
}
