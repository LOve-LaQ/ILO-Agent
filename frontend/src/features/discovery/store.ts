import { create } from 'zustand';

import type { CardTab, HomeView, RankingWindow, SearchSort } from '../../shared/types/card';

export type TimeRange = 'day' | 'week' | 'month';

export const TIME_RANGES: ReadonlyArray<{ value: TimeRange; label: string }> = [
  { value: 'day', label: '当日' },
  { value: 'week', label: '当周' },
  { value: 'month', label: '当月' },
];

export function timeRangeLabel(range: TimeRange): string {
  return TIME_RANGES.find((item) => item.value === range)?.label ?? range;
}

/** 榜单窗口只有两档：窗口数一多，缓存键与「口径」文案都会跟着膨胀 */
export const RANKING_WINDOWS: ReadonlyArray<{ value: RankingWindow; label: string }> = [
  { value: 'week', label: '本周' },
  { value: 'month', label: '本月' },
];

/** 搜索排序只有两档：多一个值就多一倍后端缓存键与 GitHub 请求 */
export const SEARCH_SORTS: ReadonlyArray<{ value: SearchSort; label: string }> = [
  { value: 'stars', label: '按星数' },
  { value: 'updated', label: '按更新' },
];

/** 星数门槛（0 表示不限制）。用固定档位而不是自由输入：自由输入会让缓存键爆炸 */
export const SEARCH_MIN_STARS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 0, label: '不限' },
  { value: 100, label: '100+' },
  { value: 1000, label: '1k+' },
  { value: 10000, label: '10k+' },
];

const VIEWS: readonly HomeView[] = ['repo', 'article', 'ranking', 'search'];

/**
 * 从 URL 读初始视图与关键词，支持 `/?view=search&q=llm agent` 直接落地。
 *
 * 只做「URL -> state」单向：搜索和榜单都是轻交互，双向同步会把每次点标签都
 * 变成一次 history push（浏览器后退键开始在四个视图之间来回跳，反而更难用）。
 * 单向读取解决的是两个真实场景：把某次搜索的链接发给别人、演示时一步到位。
 */
function initialView(): HomeView {
  const view = new URLSearchParams(window.location.search).get('view');
  return VIEWS.find((item) => item === view) ?? 'repo';
}

function initialKeyword(): string {
  return (new URLSearchParams(window.location.search).get('q') ?? '').trim().slice(0, 64);
}

interface DiscoveryState {
  /** 当前视图：仓库 / 文章 / 榜单 */
  activeView: HomeView;
  /** 文章视图的时间范围（仅影响 refresh-articles） */
  timeRange: TimeRange;
  /** 榜单窗口。与文章的时间胶囊分开存：两者语义不同（一个决定抓取范围，
   *  一个只是展示口径），共用一个字段迟早会互相污染 */
  rankingWindow: RankingWindow;
  /** 榜单语言筛选，'all' 为全部；可选值由后端响应下发，前端不维护列表 */
  rankingLanguage: string;
  /** 搜索条件统一放 store：切到榜单再切回搜索时，用户不该丢掉刚输入的关键词 */
  searchKeyword: string;
  searchLanguage: string;
  searchMinStars: number;
  searchSort: SearchSort;
  /** 各 Tab 的知识库总数，用于导航角标；未拉取过的 Tab 显示 – */
  counts: Partial<Record<CardTab, number>>;
  setActiveView: (view: HomeView) => void;
  setTimeRange: (range: TimeRange) => void;
  setRankingWindow: (window: RankingWindow) => void;
  setRankingLanguage: (language: string) => void;
  setSearchKeyword: (keyword: string) => void;
  setSearchLanguage: (language: string) => void;
  setSearchMinStars: (minStars: number) => void;
  setSearchSort: (sort: SearchSort) => void;
  setCount: (tab: CardTab, total: number) => void;
}

export const useDiscoveryStore = create<DiscoveryState>((set) => ({
  activeView: initialView(),
  timeRange: 'day',
  rankingWindow: 'week',
  rankingLanguage: 'all',
  searchKeyword: initialKeyword(),
  searchLanguage: 'all',
  searchMinStars: 0,
  searchSort: 'stars',
  counts: {},

  setActiveView: (view) =>
    set((state) => (state.activeView === view ? state : { activeView: view })),

  setTimeRange: (range) => set((state) => (state.timeRange === range ? state : { timeRange: range })),

  setRankingWindow: (window) =>
    set((state) => (state.rankingWindow === window ? state : { rankingWindow: window })),

  setRankingLanguage: (language) =>
    set((state) =>
      state.rankingLanguage === language ? state : { rankingLanguage: language },
    ),

  setSearchKeyword: (keyword) =>
    set((state) => (state.searchKeyword === keyword ? state : { searchKeyword: keyword })),

  setSearchLanguage: (language) =>
    set((state) => (state.searchLanguage === language ? state : { searchLanguage: language })),

  setSearchMinStars: (minStars) =>
    set((state) => (state.searchMinStars === minStars ? state : { searchMinStars: minStars })),

  setSearchSort: (sort) =>
    set((state) => (state.searchSort === sort ? state : { searchSort: sort })),

  setCount: (tab, total) =>
    set((state) =>
      state.counts[tab] === total ? state : { counts: { ...state.counts, [tab]: total } },
    ),
}));
