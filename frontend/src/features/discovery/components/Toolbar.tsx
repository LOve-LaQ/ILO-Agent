import clsx from 'clsx';

import type { CardTab } from '../../../shared/types/card';
import { TIME_RANGES, type TimeRange } from '../store';
import styles from './Toolbar.module.css';

const TITLE: Record<CardTab, string> = {
  repo: '今日技术仓库',
  article: '全球技术热门文章',
};

const META: Record<CardTab, string> = {
  repo: 'GitHub 近 7 天高星仓库 · 通义千问中文摘要',
  article: '聚合 Hacker News · Lobsters · dev.to · Stack Overflow · 通义千问中文摘要',
};

interface ToolbarProps {
  activeTab: CardTab;
  timeRange: TimeRange;
  onSelectRange: (range: TimeRange) => void;
}

export function Toolbar({ activeTab, timeRange, onSelectRange }: ToolbarProps) {
  return (
    <div className={styles.toolbar}>
      <div>
        <h1>{TITLE[activeTab]}</h1>
        <div className={styles.meta}>{META[activeTab]}</div>
      </div>

      {/* 时间范围仅对文章视图生效 */}
      {activeTab === 'article' && (
        <div className={styles.pillRow}>
          {TIME_RANGES.map((range) => (
            <button
              key={range.value}
              type="button"
              className={clsx(styles.pill, timeRange === range.value && styles.on)}
              onClick={() => onSelectRange(range.value)}
            >
              {range.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
