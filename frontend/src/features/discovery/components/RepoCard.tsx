import clsx from 'clsx';

import { cardText, catLabel, fmtStars, langColor } from '../../../shared/lib/format';
import type { TechCard } from '../../../shared/types/card';
import { useBookmarks } from '../hooks/useBookmarks';
import styles from './Card.module.css';

interface RepoCardProps {
  card: TechCard;
  onStartLearning: (card: TechCard) => void;
}

/** GitHub 仓库卡 */
export function RepoCard({ card, onStartLearning }: RepoCardProps) {
  const { saved, onToggle } = useBookmarks(card.id);
  const summary = card.summary || card.one_liner || '';
  const link = card.link || '';
  const tags = (card.tags ?? []).slice(0, 3);

  return (
    <>
      <div className={styles.cardHead}>
        <span className={styles.langDot} style={{ background: langColor(card.language) }} />
        <span className={styles.repoName}>
          <code>{card.title}</code>
        </span>
        <span className={styles.right}>
          <button
            className={clsx('tiny-btn', saved && 'on')}
            type="button"
            title="收藏"
            onClick={() => onToggle(card)}
          >
            🔖
          </button>
          <a
            className="tiny-btn"
            href={link || '#'}
            target="_blank"
            rel="noopener noreferrer"
            title="打开原文"
          >
            ↗
          </a>
        </span>
      </div>

      <p className={styles.oneLiner} title={`${summary}\n来源: ${link}`}>
        {cardText(card)}
      </p>

      <div className={styles.tags}>
        {card.category ? (
          <span className={clsx(styles.tag, styles.cat)}>{catLabel(card.category)}</span>
        ) : null}
        {tags.map((tag) => (
          <span key={tag} className={styles.tag}>
            #{tag}
          </span>
        ))}
      </div>

      <div className={styles.cardFoot}>
        <span className={styles.footMetric}>
          ⭐ <b>{fmtStars(card.stars)}</b>
        </span>
        <span>{card.language || ''}</span>
        <span className={styles.ops}>
          <span className={clsx(styles.tag, styles.cat)} style={{ fontWeight: 400 }}>
            {card.source}
          </span>
          <button className="mini-btn solid" type="button" onClick={() => onStartLearning(card)}>
            开始学习
          </button>
        </span>
      </div>
    </>
  );
}
