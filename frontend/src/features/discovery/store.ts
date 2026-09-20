import { create } from 'zustand';

import type { CardTab } from '../../shared/types/card';

export type TimeRange = 'day' | 'week' | 'month';

export const TIME_RANGES: ReadonlyArray<{ value: TimeRange; label: string }> = [
  { value: 'day', label: '当日' },
  { value: 'week', label: '当周' },
  { value: 'month', label: '当月' },
];

export function timeRangeLabel(range: TimeRange): string {
  return TIME_RANGES.find((item) => item.value === range)?.label ?? range;
}

interface DiscoveryState {
  /** 当前视图：仓库 / 文章 */
  activeTab: CardTab;
  /** 文章视图的时间范围（仅影响 refresh-articles） */
  timeRange: TimeRange;
  /** 各 Tab 的知识库总数，用于导航角标；未拉取过的 Tab 显示 – */
  counts: Partial<Record<CardTab, number>>;
  setActiveTab: (tab: CardTab) => void;
  setTimeRange: (range: TimeRange) => void;
  setCount: (tab: CardTab, total: number) => void;
}

export const useDiscoveryStore = create<DiscoveryState>((set) => ({
  activeTab: 'repo',
  timeRange: 'day',
  counts: {},

  setActiveTab: (tab) => set((state) => (state.activeTab === tab ? state : { activeTab: tab })),

  setTimeRange: (range) => set((state) => (state.timeRange === range ? state : { timeRange: range })),

  setCount: (tab, total) =>
    set((state) =>
      state.counts[tab] === total ? state : { counts: { ...state.counts, [tab]: total } },
    ),
}));
