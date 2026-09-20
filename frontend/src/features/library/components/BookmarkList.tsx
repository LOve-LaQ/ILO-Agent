import clsx from 'clsx';
import { useState } from 'react';

import type { BookmarkItem } from '../../../shared/api/me';
import { useBookmarkToggle } from '../../../shared/hooks/useBookmarks';
import { cardText, catLabel, fmtRelative } from '../../../shared/lib/format';
import type { TechCard } from '../../../shared/types/card';
import { useProvenance } from '../hooks/useProvenance';
import styles from '../LibraryPage.module.css';
import { ProvenancePanel } from './ProvenancePanel';

interface BookmarkListProps {
  items: BookmarkItem[];
  /** 由组合根注入：learning 是另一个 feature，library 不得 import 它 */
  onStartLearning?: (card: TechCard) => void;
}

/**
 * 收藏列表。
 *
 * 每条收藏的卡片快照来自服务端（`BookmarkItem.card`），但**可能是 null** —— 卡片被
 * 清理或知识库不可用时，收藏本身不该消失。这时照常列出这条收藏并说明原因，
 * 而不是把它藏起来假装不存在。
 *
 * 溯源面板按需展开：默认折叠，展开哪一条才查哪一条，不做 N 次预取。
 */
export function BookmarkList({ items, onStartLearning }: BookmarkListProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const { toggle, pending } = useBookmarkToggle();
  const provenance = useProvenance(expandedId);

  return (
    <div className={styles.list}>
      {items.map((item) => {
        const card = item.card ?? null;
        const expanded = expandedId === item.item_id;
        const title = card?.title || item.item_id;

        return (
          <article key={item.item_id} className={styles.row}>
            <div className={styles.rowMain}>
              <div className={styles.rowHead}>
                {card?.category && (
                  <span className={clsx(styles.chip, styles.chipCat)}>{catLabel(card.category)}</span>
                )}
                <h3 className={styles.rowTitle}>{title}</h3>
                {!card && <span className={styles.chipWarn}>卡片已失效</span>}
              </div>

              <p className={styles.rowSummary}>
                {card ? cardText(card) : '这张卡片已不在知识库中，仅保留了收藏记录本身。'}
              </p>

              <div className={styles.rowFoot}>
                <span className={styles.rowTime}>收藏于 {fmtRelative(item.created_at)}</span>

                <span className={styles.rowOps}>
                  <button
                    className="mini-btn"
                    type="button"
                    aria-expanded={expanded}
                    onClick={() => setExpandedId(expanded ? null : item.item_id)}
                  >
                    {expanded ? '收起溯源' : '🔍 溯源'}
                  </button>

                  {card?.link && (
                    <a
                      className="mini-btn"
                      href={card.link}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      打开原文
                    </a>
                  )}

                  {card && onStartLearning && (
                    <button
                      className={clsx('mini-btn', 'solid')}
                      type="button"
                      onClick={() => onStartLearning(card)}
                    >
                      继续学习
                    </button>
                  )}

                  <button
                    className="mini-btn"
                    type="button"
                    disabled={pending}
                    onClick={() => toggle(item.item_id)}
                  >
                    取消收藏
                  </button>
                </span>
              </div>
            </div>

            {expanded && (
              <div className={styles.pvWrap}>
                <ProvenancePanel
                  data={provenance.data}
                  loading={provenance.isPending}
                  error={provenance.isError}
                  onRetry={() => void provenance.refetch()}
                />
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}
