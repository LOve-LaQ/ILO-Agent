import clsx from 'clsx';
import type { ReactNode } from 'react';

import type { CardTab } from '../../../shared/types/card';
import styles from './NavBar.module.css';

const TABS: ReadonlyArray<{ value: CardTab; label: string }> = [
  { value: 'repo', label: '📦 仓库' },
  { value: 'article', label: '📰 文章' },
];

interface NavBarProps {
  activeTab: CardTab;
  /** 各 Tab 的知识库总数；未拉取过显示 – */
  counts: Partial<Record<CardTab, number>>;
  onSwitchTab: (tab: CardTab) => void;
  onShuffle: () => void;
  onRefresh: () => void;
  shuffleDisabled: boolean;
  refreshDisabled: boolean;
  /**
   * 账号区域插槽（登录 / 注册 / 当前用户）。
   * 由组合根（routes.tsx）注入，NavBar 自身不 import auth feature。
   */
  accountSlot?: ReactNode;
}

export function NavBar({
  activeTab,
  counts,
  onSwitchTab,
  onShuffle,
  onRefresh,
  shuffleDisabled,
  refreshDisabled,
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
              className={clsx(activeTab === tab.value && styles.on)}
              onClick={() => onSwitchTab(tab.value)}
            >
              {tab.label}
              <span className={styles.cnt}>{counts[tab.value] ?? '–'}</span>
            </button>
          ))}
        </div>

        <div className={styles.navRight}>
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
          {accountSlot && (
            <>
              <span className={styles.dotSep} />
              {accountSlot}
            </>
          )}
        </div>
      </div>
    </nav>
  );
}
