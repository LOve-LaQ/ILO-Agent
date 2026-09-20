import { useQuery } from '@tanstack/react-query';

import { useAccessToken } from '../../../shared/api/authToken';
import { fetchBookmarks, meKeys } from '../../../shared/api/me';

/**
 * 收藏列表（含卡片快照）
 *
 * 与发现页的 `useBookmarkIds` 共用同一个 query key —— 同一个接口、同一份缓存，
 * 区别只是这里要完整数据（快照 + 收藏时间），发现页只要 id 集合。
 * 所以这里保留原始响应，不做 `select` 变形。
 */
export function useBookmarkList() {
  const token = useAccessToken();

  return useQuery({
    queryKey: meKeys.bookmarks(),
    queryFn: () => fetchBookmarks(),
    enabled: token !== null,
    staleTime: 30_000,
  });
}
