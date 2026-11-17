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
  /** 与顶栏同一组操作：窄屏时顶栏放不下，改由内容区标题行承载 */
  onShuffle: () => void;
  onRefresh: () => void;
  shuffleDisabled: boolean;
  refreshDisabled: boolean;
}

export function Toolbar({
  activeTab,
  timeRange,
  onSelectRange,
  onShuffle,
  onRefresh,
  shuffleDisabled,
  refreshDisabled,
}: ToolbarProps) {
  return (
    <div className={styles.toolbar}>
      <div className={styles.titles}>
        <h1>{TITLE[activeTab]}</h1>
        <div className={styles.meta}>{META[activeTab]}</div>
      </div>

      {/* 仅窄屏可见（桌面端这两个按钮在顶栏）。同一组处理函数，行为完全一致。
          DOM 顺序放在时间胶囊之前：胶囊是整行宽，放前面会把操作挤到第三行 */}
      <div className={styles.actions}>
        <button
          className="icon-btn"
          type="button"
          title="换一批"
          onClick={onShuffle}
          disabled={shuffleDisabled}
        >
          ↻
        </button>
        <button
          className="btn btn-primary"
          type="button"
          onClick={onRefresh}
          disabled={refreshDisabled}
        >
          🛰 抓取最新
        </button>
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
