import { keepPreviousData, useQuery } from '@tanstack/react-query';

import type { SearchSort } from '../../../shared/types/card';
import { SEARCH_LIMIT, searchRepos } from '../api';

export interface SearchArgs {
  q: string;
  language: string;
  minStars: number;
  sort: SearchSort;
}

/** 四个条件都必须进 key：少一个就会在切筛选时看到上一个条件的结果 */
export function searchQueryKey({ q, language, minStars, sort }: SearchArgs) {
  return ['search', q, language, minStars, sort] as const;
}

/**
 * 仓库搜索
 *
 * - `enabled`：空关键词不发请求。后端对空词直接 422，前端先挡住能省一次往返
 * - `retry: false`：这一条和其它 query 不同。默认的 3 次重试对搜索是**有害**的 ——
 *   422 是用户检索词语法错误（重试还是错），429/403 是 GitHub 配额耗尽
 *   （未认证仅 10 次/分钟），重试只会把配额烧得更快、让全站搜索更晚恢复
 * - `staleTime` 与后端缓存（15 分钟）方向一致但更短：用户点「再搜一次」时应当
 *   真的发一次请求，而不是永远命中前端缓存
 * - `placeholderData: keepPreviousData`：切筛选/语言时保留上一份结果避免整页闪烁，
 *   但调用方必须配「更新中」提示，否则会把旧结果当成新条件的答案
 */
export function useSearch({ q, language, minStars, sort }: SearchArgs) {
  const keyword = q.trim();
  return useQuery({
    queryKey: searchQueryKey({ q: keyword, language, minStars, sort }),
    queryFn: () =>
      searchRepos({
        q: keyword,
        language,
        minStars,
        sort,
        limit: SEARCH_LIMIT,
      }),
    enabled: keyword.length > 0,
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
    retry: false,
  });
}
