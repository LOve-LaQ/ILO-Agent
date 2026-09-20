import { useCallback, useEffect, type ReactNode } from 'react';

import { useToastStore } from '../../components/ui/toastStore';
import { ApiError } from '../../shared/api/client';
import type { TechCard } from '../../shared/types/card';
import { LearningDrawer } from '../learning/components/LearningDrawer';
import { useLearningSession } from '../learning/hooks/useLearningSession';
import { CardGrid } from './components/CardGrid';
import { EmptyState } from './components/EmptyState';
import { ErrorState } from './components/ErrorState';
import { NavBar } from './components/NavBar';
import { RefreshOverlay } from './components/RefreshOverlay';
import { SkeletonGrid } from './components/SkeletonGrid';
import { Toolbar } from './components/Toolbar';
import styles from './DiscoveryPage.module.css';
import { useCards } from './hooks/useCards';
import { useRefreshCards } from './hooks/useRefreshCards';
import { timeRangeLabel, useDiscoveryStore, type TimeRange } from './store';

interface DiscoveryPageProps {
  /** 账号区域插槽，由组合根注入（发现页本身不依赖 auth feature） */
  accountSlot?: ReactNode;
}

/**
 * 发现页：导航 + 操作条 + 卡片流（仓库 / 文章）+ 抓取进度 + 学习抽屉。
 * 对应原 index.html 的 <main class="wrap"> 及全局遮罩 / 抽屉。
 */
export function DiscoveryPage({ accountSlot }: DiscoveryPageProps) {
  const activeTab = useDiscoveryStore((state) => state.activeTab);
  const timeRange = useDiscoveryStore((state) => state.timeRange);
  const counts = useDiscoveryStore((state) => state.counts);
  const setActiveTab = useDiscoveryStore((state) => state.setActiveTab);
  const setTimeRange = useDiscoveryStore((state) => state.setTimeRange);
  const setCount = useDiscoveryStore((state) => state.setCount);
  const showToast = useToastStore((state) => state.show);

  const { data, isPending, isError, error, isFetching, refetch } = useCards();
  const { refreshing, stage, stages, run: runRefresh } = useRefreshCards();
  const { startLearning } = useLearningSession();

  const items = data?.items ?? [];
  const total = data?.total;

  // 把知识库总数回填到导航角标（未拉取过的 Tab 保持 –）
  useEffect(() => {
    if (typeof total === 'number') {
      setCount(activeTab, total);
    }
  }, [activeTab, total, setCount]);

  const handleShuffle = useCallback(async () => {
    await refetch();
    showToast('已为你换一批内容');
  }, [refetch, showToast]);

  const handleSelectRange = useCallback(
    (range: TimeRange) => {
      if (range === timeRange) return;
      setTimeRange(range);
      showToast(`已切换为「${timeRangeLabel(range)}」热榜`);
    },
    [timeRange, setTimeRange, showToast],
  );

  const handleStartLearning = useCallback(
    (card: TechCard) => {
      void startLearning(card);
    },
    [startLearning],
  );

  const showSkeleton = isPending && !refreshing;
  const hasItems = items.length > 0;
  const errorMessage = error instanceof ApiError ? error.message : '加载失败，请稍后重试';

  return (
    <>
      <NavBar
        activeTab={activeTab}
        counts={counts}
        onSwitchTab={setActiveTab}
        onShuffle={() => void handleShuffle()}
        onRefresh={() => void runRefresh()}
        shuffleDisabled={isFetching}
        refreshDisabled={refreshing}
        accountSlot={accountSlot}
      />

      <main className="wrap">
        <Toolbar activeTab={activeTab} timeRange={timeRange} onSelectRange={handleSelectRange} />

        {showSkeleton && <SkeletonGrid />}
        {!isPending && !isError && hasItems && (
          <CardGrid items={items} onStartLearning={handleStartLearning} />
        )}
        {!isPending && !isError && !hasItems && <EmptyState />}
        {isError && <ErrorState message={errorMessage} onRetry={() => void refetch()} />}

        <div className={styles.footNote}>
          ILO 技术情报官 · 内容由 DeepSeek 讲解 · 通义千问摘要/向量化
        </div>
      </main>

      {refreshing && <RefreshOverlay stage={stage} stages={stages} />}
      <LearningDrawer />
    </>
  );
}
