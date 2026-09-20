import { useCallback, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { useToastStore } from '../../../components/ui/toastStore';
import { refreshCards } from '../api';
import { useDiscoveryStore } from '../store';
import { cardsQueryKey } from './useCards';
import { useStagedProgress } from './useStagedProgress';

/** 进度遮罩最短展示时长，与原 index.html 的 900ms 下限一致 */
const MIN_OVERLAY_MS = 900;

const REPO_STAGES = [
  '正在连接 GitHub…',
  '正在翻页抓取热门仓库…',
  '通义千问提炼中文摘要…',
  '正在整理技术卡片…',
];

const ARTICLE_STAGES = [
  '正在连接技术社区…',
  '正在抓取热门文章…',
  '通义千问提炼中文摘要…',
  '正在整理文章卡片…',
];

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export interface RefreshCardsController {
  refreshing: boolean;
  stage: number;
  stages: string[];
  run: () => Promise<void>;
}

/**
 * 「抓取最新」流程编排
 * 对应原 index.html refreshNews()：阶段文案轮播 → POST 抓取 → 重新拉卡片 → 保证遮罩最短展示 900ms。
 * 与原来的差异：抓取失败不再静默，而是通过 Toast 明确反馈。
 */
export function useRefreshCards(): RefreshCardsController {
  const activeTab = useDiscoveryStore((state) => state.activeTab);
  const timeRange = useDiscoveryStore((state) => state.timeRange);
  const showToast = useToastStore((state) => state.show);
  const queryClient = useQueryClient();

  const [refreshing, setRefreshing] = useState(false);
  const stages = activeTab === 'article' ? ARTICLE_STAGES : REPO_STAGES;
  const stage = useStagedProgress(stages.length, refreshing);

  const run = useCallback(async () => {
    if (refreshing) {
      return;
    }
    setRefreshing(true);

    const startedAt = Date.now();

    try {
      const result = await refreshCards(activeTab, timeRange);
      if (result.status === 'ok') {
        showToast(`✅ 新增 ${result.new_count ?? 0} 条，跳过 ${result.skipped_count ?? 0} 条重复`);
      } else {
        showToast('本次没有抓到新内容');
      }
    } catch (error) {
      console.warn('抓取失败:', error);
      showToast('⚠️ 抓取失败，已展示当前内容');
    }

    await queryClient.invalidateQueries({ queryKey: cardsQueryKey(activeTab) });

    const rest = MIN_OVERLAY_MS - (Date.now() - startedAt);
    if (rest > 0) {
      await delay(rest);
    }

    setRefreshing(false);
  }, [refreshing, activeTab, timeRange, showToast, queryClient]);

  return { refreshing, stage, stages, run };
}
