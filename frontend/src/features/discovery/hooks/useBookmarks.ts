import { useBookmarkIds, useBookmarkToggle } from '../../../shared/hooks/useBookmarks';
import type { TechCard } from '../../../shared/types/card';

/**
 * 卡片上的收藏开关（discovery 侧的薄封装）
 *
 * 收藏本身是服务端资产（`user_bookmarks` 表），读写实现统一在
 * `shared/hooks/useBookmarks.ts`，因为它同时被发现页与「我的」页消费。
 * 这里只负责把它适配成卡片组件需要的形状：`{ saved, onToggle(card) }`。
 *
 * 未登录时不发请求（否则匿名游客会被 401 弹去登录页），点击只提示去登录。
 */
export function useBookmarks(cardId: string) {
  const ids = useBookmarkIds();
  const { toggle } = useBookmarkToggle();

  return {
    saved: ids.includes(cardId),
    onToggle: (card: TechCard) => toggle(card.id),
  };
}
