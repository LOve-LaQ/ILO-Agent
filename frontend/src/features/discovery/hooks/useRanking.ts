import { keepPreviousData, useQuery } from '@tanstack/react-query';

import type { RankingWindow } from '../../../shared/types/card';
import { fetchRanking, RANKING_LIMIT } from '../api';

/** 榜单 query key：窗口与语言都必须进 key，否则切标签会看到上一个标签的数据 */
export function rankingQueryKey(window: RankingWindow, language: string) {
  return ['ranking', window, language] as const;
}

/**
 * 拉取 GitHub 新星榜
 *
 * - `staleTime` 与后端缓存时长（30 分钟）对齐：服务端已经把「同一份数据」压到
 *   半小时粒度，前端再更频繁地请求只会多打几次接口、看到同一个结果
 * - `placeholderData: keepPreviousData`：切窗口/切语言时保留上一份结果，
 *   避免整页闪成骨架屏。数据是「榜单」不是「表单」，短暂显示旧排名比空白好
 */
export function useRanking(window: RankingWindow, language: string) {
  return useQuery({
    queryKey: rankingQueryKey(window, language),
    queryFn: () => fetchRanking(window, language, RANKING_LIMIT),
    staleTime: 30 * 60_000,
    placeholderData: keepPreviousData,
  });
}
