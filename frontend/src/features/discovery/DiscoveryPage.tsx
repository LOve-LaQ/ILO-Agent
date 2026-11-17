import { useCallback, useEffect, type ReactNode } from 'react';

import { useToastStore } from '../../components/ui/toastStore';
import { ApiError } from '../../shared/api/client';
import type { CardTab, TechCard } from '../../shared/types/card';
import { LearningDrawer } from '../learning/components/LearningDrawer';
import { useLearningSession } from '../learning/hooks/useLearningSession';
import { CardGrid } from './components/CardGrid';
import { EmptyState } from './components/EmptyState';
import { ErrorState } from './components/ErrorState';
import { NavBar } from './components/NavBar';
import { RankingPanel } from './components/RankingPanel';
import { SearchPanel } from './components/SearchPanel';
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
 * 发现页：导航 + 操作条 + 卡片流（仓库 / 文章 / 榜单）+ 抓取进度 + 学习抽屉。
 *
 * 【为什么榜单和卡片共用一个页面而不是新开路由】三者共用同一套导航、账号区、
 * 抓取进度与学习抽屉，新开路由会让「切页」变成整页重挂载（抽屉关闭、滚动丢失）。
 * 而且它们回答的是同一个问题：**现在有什么值得学** —— 卡片是随机抽样，
 * 榜单是排名，只是同一件事的两种看法。
 */
export function DiscoveryPage({ accountSlot }: DiscoveryPageProps) {
  const activeView = useDiscoveryStore((state) => state.activeView);
  const timeRange = useDiscoveryStore((state) => state.timeRange);
  const counts = useDiscoveryStore((state) => state.counts);
  const setActiveView = useDiscoveryStore((state) => state.setActiveView);
  const setTimeRange = useDiscoveryStore((state) => state.setTimeRange);
  const setCount = useDiscoveryStore((state) => state.setCount);
  const showToast = useToastStore((state) => state.show);

  // 榜单与搜索不参与卡片请求：`activeView` 是四个视图，`tab` 只是其中属于卡片池的两个。
  // 分开类型（HomeView / CardTab）是为了让 fetchCards('ranking') 在编译期就不成立。
  const tab: CardTab = activeView === 'repo' || activeView === 'article' ? activeView : 'repo';
  // 两个「辅助视图」都自带刷新入口，且都不属于知识库（不参与总数角标）
  const isAuxView = activeView === 'ranking' || activeView === 'search';

  const { data, isPending, isError, error, isFetching, refetch } = useCards(tab, {
    enabled: !isAuxView,
  });
  const { refreshing, progress, run: runRefresh } = useRefreshCards(tab);
  const { startLearning } = useLearningSession();

  const items = data?.items ?? [];
  const total = data?.total;

  // 把知识库总数回填到导航角标（未拉取过的 Tab 保持 –）
  useEffect(() => {
    if (!isAuxView && typeof total === 'number') {
      setCount(tab, total);
    }
  }, [isAuxView, tab, total, setCount]);

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
        activeView={activeView}
        counts={counts}
        onSwitchView={setActiveView}
        onShuffle={() => void handleShuffle()}
        onRefresh={() => void runRefresh()}
        shuffleDisabled={isFetching}
        refreshDisabled={refreshing}
        actionsVisible={!isAuxView}
        accountSlot={accountSlot}
      />

      <main className="wrap">
        {activeView === 'ranking' ? (
          <RankingPanel />
        ) : activeView === 'search' ? (
          <SearchPanel />
        ) : (
          <>
            <Toolbar
              activeTab={tab}
              timeRange={timeRange}
              onSelectRange={handleSelectRange}
              onShuffle={() => void handleShuffle()}
              onRefresh={() => void runRefresh()}
              shuffleDisabled={isFetching}
              refreshDisabled={refreshing}
            />

            {showSkeleton && <SkeletonGrid />}
            {!isPending && !isError && hasItems && (
              <CardGrid items={items} onStartLearning={handleStartLearning} />
            )}
            {!isPending && !isError && !hasItems && <EmptyState />}
            {isError && <ErrorState message={errorMessage} onRetry={() => void refetch()} />}
          </>
        )}

        <div className={styles.footNote}>
          ILO 技术情报官 · 内容由 DeepSeek 讲解 · 通义千问摘要/向量化
        </div>
      </main>

      {refreshing && <RefreshOverlay progress={progress} />}
      <LearningDrawer />
    </>
  );
}
