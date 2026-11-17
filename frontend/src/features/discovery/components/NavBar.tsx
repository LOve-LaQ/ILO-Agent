import clsx from 'clsx';
import type { ReactNode } from 'react';

import type { CardTab, HomeView } from '../../../shared/types/card';
import styles from './NavBar.module.css';

const TABS: ReadonlyArray<{ value: HomeView; label: string }> = [
  { value: 'repo', label: '📦 仓库' },
  { value: 'article', label: '📰 文章' },
  { value: 'ranking', label: '🏆 榜单' },
  { value: 'search', label: '🔍 搜索' },
];

/** 只有卡片视图有「知识库总数」这个概念，榜单与搜索没有（它们不是知识库的一部分） */
function isCardTab(view: HomeView): view is CardTab {
  return view === 'repo' || view === 'article';
}

interface NavBarProps {
  activeView: HomeView;
  /** 各 Tab 的知识库总数；未拉取过显示 – */
  counts: Partial<Record<CardTab, number>>;
  onSwitchView: (view: HomeView) => void;
  onShuffle: () => void;
  onRefresh: () => void;
  shuffleDisabled: boolean;
  refreshDisabled: boolean;
  /**
   * 「换一批 / 抓取最新」的作用对象是卡片池。榜单与搜索视图里这两个动作没有意义
   * （它们各有自己的刷新），显式隐藏比留着一个点了没反应的按钮诚实。
   */
  actionsVisible?: boolean;
  /**
   * 账号区域插槽（登录 / 注册 / 当前用户）。
   * 由组合根（routes.tsx）注入，NavBar 自身不 import auth feature。
   */
  accountSlot?: ReactNode;
}

export function NavBar({
  activeView,
  counts,
  onSwitchView,
  onShuffle,
  onRefresh,
  shuffleDisabled,
  refreshDisabled,
  actionsVisible = true,
  accountSlot,
}: NavBarProps) {
  return (
    <nav className={styles.nav}>
      <div className={clsx('wrap', styles.navInner)}>
        <div className={styles.brand}>
          <span className={styles.brandMark}>I</span>
          <span>ILO 技术情报官</span>
          <span className={styles.brandSub}>intelligence &amp; learning</span>
        </div>

        <div className={styles.seg}>
          {TABS.map((tab) => (
            <button
              key={tab.value}
              type="button"
              className={clsx(activeView === tab.value && styles.on)}
              onClick={() => onSwitchView(tab.value)}
            >
              {tab.label}
              {isCardTab(tab.value) && (
                <span className={styles.cnt}>{counts[tab.value] ?? '–'}</span>
              )}
            </button>
          ))}
        </div>

        {actionsVisible && (
          <div className={styles.navActions}>
            <button
              className="icon-btn"
              type="button"
              title="换一批"
              onClick={onShuffle}
              disabled={shuffleDisabled}
            >
              ↻
            </button>
            <span className={styles.dotSep} />
            <button
              className="btn btn-primary"
              type="button"
              onClick={onRefresh}
              disabled={refreshDisabled}
            >
              🛰 抓取最新
            </button>
          </div>
        )}
        {accountSlot && (
          <div className={clsx(styles.navAccount, !actionsVisible && styles.navAccountPush)}>
            {accountSlot}
          </div>
        )}
      </div>
    </nav>
  );
}
