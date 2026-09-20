import { api } from '../../shared/api/client';
import type { CardListResponse, CardTab, RefreshResponse } from '../../shared/types/card';

/** 卡片池单次拉取条数，与原 index.html 的 limit=6 保持一致 */
export const CARDS_PAGE_SIZE = 6;

/** 抓取文章时每个平台抓取条数，与原 index.html 的 per_platform=8 保持一致 */
const ARTICLES_PER_PLATFORM = 8;

/** GET /discover/news | /discover/articles */
export function fetchCards(tab: CardTab, limit: number = CARDS_PAGE_SIZE): Promise<CardListResponse> {
  const path = tab === 'article' ? '/discover/articles' : '/discover/news';
  return api.get<CardListResponse>(`${path}?limit=${limit}`);
}

/** POST /discover/refresh | /discover/refresh-articles */
export function refreshCards(tab: CardTab, timeRange: string): Promise<RefreshResponse> {
  if (tab === 'article') {
    return api.post<RefreshResponse>(
      `/discover/refresh-articles?per_platform=${ARTICLES_PER_PLATFORM}&time_range=${timeRange}`,
    );
  }
  return api.post<RefreshResponse>('/discover/refresh?limit=50');
}
