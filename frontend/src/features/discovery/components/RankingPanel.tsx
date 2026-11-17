import clsx from 'clsx';
import { useMemo } from 'react';

import { fmtRelative, fmtStars, langColor } from '../../../shared/lib/format';
import { useRanking } from '../hooks/useRanking';
import { RANKING_WINDOWS, useDiscoveryStore } from '../store';
import styles from './RankingPanel.module.css';

/** 榜单窗口 -> 口径说明。措辞固定为「新星榜」：我们拿不到涨星数，不能叫涨星榜 */
const WINDOW_HINT: Record<'week' | 'month', string> = {
  week: '近 7 天',
  month: '近 30 天',
};

/**
 * 总数格式化：2,221,736 -> 222.2 万
 *
 * 窗口内新建的仓库动辄百万级，「共 2221736 个」既读不出来也占地方。
 */
function fmtCount(value?: number | null): string {
  const n = value ?? 0;
  if (n >= 100_000_000) return `${(n / 100_000_000).toFixed(1)} 亿`;
  if (n >= 10_000) return `${(n / 10_000).toFixed(1)} 万`;
  return String(n);
}

/** 存活时长：36 小时 -> 「1.5 天」；不足一天按小时说，否则所有新项目都显示「1 天」 */
function fmtAge(hours?: number | null): string {
  if (hours === null || hours === undefined) return '';
  if (hours < 48) return `${Math.round(hours)} 小时`;
  return `${Math.round(hours / 24)} 天`;
}

/**
 * GitHub 新星榜
 *
 * 【为什么是独立面板而不是又一张卡片】卡片是「随机抽 6 条 + 中文摘要」的浏览流，
 * 榜单是「排名 + 数字」的信息密度型内容。两者塞进同一个网格会让「第 1 名」和
 * 「随便一条」在视觉上等权，榜单的意义就没了。
 *
 * 【为什么零 LLM】榜单只用 GitHub 官方 Search API 的字段（stars / 语言 / 创建时间）
 * 加本地派生指标（star 数 / 存活天数）。给 20 个仓库再跑一遍摘要，等于为了排序
 * 付一次 LLM 钱，而且摘要与排名无关。
 */
export function RankingPanel() {
  const window = useDiscoveryStore((state) => state.rankingWindow);
  const language = useDiscoveryStore((state) => state.rankingLanguage);
  const setWindow = useDiscoveryStore((state) => state.setRankingWindow);
  const setLanguage = useDiscoveryStore((state) => state.setRankingLanguage);

  const { data, isPending, isError, error, isFetching, refetch } = useRanking(window, language);

  const items = data?.items ?? [];
  // 每根星条按榜首对齐：绝对星数在这里没有可比性（榜首 3.9k，第 20 名可能 200）
  const maxStars = useMemo(
    () => Math.max(...items.map((item) => item.stars), 1),
    [items],
  );

  const degraded = data?.source === 'stale' || data?.source === 'knowledge_base';
  const showSkeleton = isPending && items.length === 0;

  return (
    <section className={styles.panel} aria-label="GitHub 新星榜">
      <header className={styles.head}>
        <div className={styles.headText}>
          <h1 className={styles.title}>
            <span className={styles.trophy}>🏆</span> GitHub 新星榜
          </h1>
          <div className={styles.meta}>
            {data?.window_start
              ? `统计 ${data.window_start} 之后创建的仓库 · 按 star 总数排序`
              : `${WINDOW_HINT[window]}创建的高星仓库 · 按 star 总数排序`}
          </div>
        </div>

        <div className={styles.headActions}>
          {isFetching && <span className={styles.busy}>更新中…</span>}
          <div className={styles.seg} role="group" aria-label="时间窗口">
            {RANKING_WINDOWS.map((item) => (
              <button
                key={item.value}
                type="button"
                className={clsx(styles.segBtn, window === item.value && styles.segOn)}
                onClick={() => setWindow(item.value)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <button
            className="icon-btn"
            type="button"
            title="重新拉取榜单"
            onClick={() => void refetch()}
            disabled={isFetching}
          >
            ↻
          </button>
        </div>
      </header>

      {/* 语言标签：取值来自后端响应（唯一事实源），这里只负责渲染与选中态 */}
      <div className={styles.chips} role="group" aria-label="语言筛选">
        {(data?.languages ?? [{ value: 'all', label: '全部' }]).map((option) => (
          <button
            key={option.value}
            type="button"
            className={clsx(styles.chip, language === option.value && styles.chipOn)}
            onClick={() => setLanguage(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>

      {data?.note && (
        <div className={clsx(styles.notice, degraded && styles.noticeWarn)}>{data.note}</div>
      )}

      {showSkeleton && (
        <ol className={styles.list}>
          {Array.from({ length: 6 }, (_, index) => (
            <li key={index} className={styles.skeletonRow}>
              <span className={styles.skeletonRank} />
              <span className={styles.skeletonMain} />
              <span className={styles.skeletonStars} />
            </li>
          ))}
        </ol>
      )}

      {isError && (
        <div className={styles.errorBox}>
          <p>{error instanceof Error ? error.message : '榜单加载失败'}</p>
          <button className="btn btn-primary" type="button" onClick={() => void refetch()}>
            重试
          </button>
        </div>
      )}

      {!showSkeleton && !isError && items.length === 0 && (
        <div className={styles.errorBox}>
          <p>暂时拿不到榜单数据：GitHub 可能限流，或本机缓存与知识库都为空。</p>
          <button className="btn btn-primary" type="button" onClick={() => void refetch()}>
            再试一次
          </button>
        </div>
      )}

      {items.length > 0 && (
        <ol className={clsx(styles.list, isFetching && styles.listBusy)}>
          {items.map((item) => (
            <li key={`${item.card_id}-${item.rank}`} className={styles.row}>
              {/* 星条是「相对榜首」的视觉刻度，纯装饰，不进无障碍树 */}
              <span
                className={styles.bar}
                aria-hidden="true"
                style={{ width: `${Math.max(4, (item.stars / maxStars) * 100)}%` }}
              />
              <span
                className={clsx(
                  styles.rank,
                  item.rank === 1 && styles.rank1,
                  item.rank === 2 && styles.rank2,
                  item.rank === 3 && styles.rank3,
                )}
              >
                {item.rank}
              </span>

              <div className={styles.main}>
                <div className={styles.nameRow}>
                  <a
                    className={styles.repo}
                    href={item.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    title={item.full_name}
                  >
                    {item.full_name}
                  </a>
                  {item.has_explanation && (
                    <span
                      className={styles.explained}
                      title="这个仓库已在知识库中，仓库页可以读到中文解读"
                    >
                      已解读
                    </span>
                  )}
                </div>
                {(item.summary_cn || item.description) && (
                  <p className={styles.desc}>{item.summary_cn || item.description}</p>
                )}
                <div className={styles.sub}>
                  {item.language && (
                    <span className={styles.lang}>
                      <i style={{ background: langColor(item.language) }} />
                      {item.language}
                    </span>
                  )}
                  <span>创建于 {fmtAge(item.age_hours)}前</span>
                  {item.forks > 0 && <span>Fork {fmtStars(item.forks)}</span>}
                </div>
              </div>

              <div className={styles.numbers}>
                <span className={styles.stars}>
                  <i>★</i>
                  {fmtStars(item.stars)}
                </span>
                {item.stars_per_day !== null && item.stars_per_day !== undefined && (
                  <span className={styles.rate}>▲ {fmtStars(item.stars_per_day)}/天</span>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}

      <footer className={styles.foot}>
        <span>
          数据来自 GitHub Search API ·{' '}
          {data?.fetched_at ? `更新于 ${fmtRelative(data.fetched_at)}` : '实时'}
        </span>
        {data?.total ? <span>{WINDOW_HINT[window]}新建 {fmtCount(data.total)}个仓库</span> : null}      </footer>
    </section>
  );
}

