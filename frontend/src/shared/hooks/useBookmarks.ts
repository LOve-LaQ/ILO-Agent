import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';

import { useToastStore } from '../../components/ui/toastStore';
import { useAccessToken } from '../api/authToken';
import { ApiError } from '../api/client';
import {
  addBookmark,
  bookmarkIds,
  fetchBookmarks,
  meKeys,
  removeBookmark,
  type BookmarkListResponse,
} from '../api/me';

/**
 * 收藏能力（shared 层）
 *
 * 【为什么放在 shared】
 * 收藏被两个 feature 同时消费：`features/discovery` 的卡片 🔖 开关、
 * `features/library` 的收藏列表。features 之间不得互相 import，故能力下沉到这里；
 * 两侧各自只保留薄封装（发现的开关还要额外处理「未登录点击」）。
 *
 * 【为什么写操作只有一份实现】
 * 乐观更新、失败回滚、结果提示三件事只要有一处写错，就会出现「界面显示已收藏但服务端
 * 没有」这类静默不一致 —— 宁可让两个入口共用同一段代码，也不要各写一遍。
 */

/** 收藏 id 集合；未登录（含启动引导未完成）时不发请求，返回空集 */
export function useBookmarkIds(): string[] {
  const token = useAccessToken();

  const query = useQuery({
    queryKey: meKeys.bookmarks(),
    queryFn: () => fetchBookmarks(),
    // 匿名拉取 /me/* 会吃 401，客户端会把它判成「登录态失效」并把游客弹去登录页
    enabled: token !== null,
    staleTime: 30_000,
  });

  return bookmarkIds(query.data);
}

interface BookmarkToggleResult {
  /** 切换某张卡片的收藏状态（按当前缓存取反，连点也不会算错方向） */
  toggle: (itemId: string) => void;
  pending: boolean;
}

/** 收藏写操作：乐观更新 + 失败回滚 + 结果提示 */
export function useBookmarkToggle(): BookmarkToggleResult {
  const queryClient = useQueryClient();
  const token = useAccessToken();
  const showToast = useToastStore((state) => state.show);

  const { mutate, isPending } = useMutation({
    mutationFn: ({ itemId, next }: { itemId: string; next: boolean }) =>
      next ? addBookmark(itemId) : removeBookmark(itemId),

    onMutate: async ({ itemId, next }) => {
      // 先掐掉在途的列表请求，否则它返回的旧数据会覆盖这次乐观更新
      await queryClient.cancelQueries({ queryKey: meKeys.bookmarks() });
      const previous = queryClient.getQueryData<BookmarkListResponse>(meKeys.bookmarks());

      if (previous) {
        const current = bookmarkIds(previous);
        const ids = next
          ? Array.from(new Set([...current, itemId]))
          : current.filter((value) => value !== itemId);
        queryClient.setQueryData<BookmarkListResponse>(meKeys.bookmarks(), {
          ...previous,
          item_ids: ids,
          total: ids.length,
        });
      }

      return { previous };
    },

    onError: (error, _variables, context) => {
      // 回滚到写前快照，避免界面停留在「看起来成功」的状态
      if (context?.previous) {
        queryClient.setQueryData(meKeys.bookmarks(), context.previous);
      }
      showToast(
        error instanceof ApiError && error.status === 503
          ? '收藏失败：当前环境未启用服务端存储'
          : '收藏失败，请稍后重试',
      );
    },

    onSuccess: (_data, variables) => {
      showToast(variables.next ? '🔖 已收藏，可在「我的 → 收藏」查看' : '已取消收藏');
    },

    // 无论成败都以服务端为准回填（items 里的卡片快照与 profile 统计也跟着刷新）
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: meKeys.all });
    },
  });

  const toggle = useCallback(
    (itemId: string) => {
      if (!token) {
        showToast('请先登录后再收藏');
        return;
      }
      const cached = queryClient.getQueryData<BookmarkListResponse>(meKeys.bookmarks());
      mutate({ itemId, next: !bookmarkIds(cached).includes(itemId) });
    },
    [token, queryClient, mutate, showToast],
  );

  return { toggle, pending: isPending };
}
