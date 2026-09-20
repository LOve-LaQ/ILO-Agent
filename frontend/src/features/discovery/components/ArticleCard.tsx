import clsx from 'clsx';

import { cardText, catLabel, sourceLogo } from '../../../shared/lib/format';
import type { TechCard } from '../../../shared/types/card';
import { useBookmarks } from '../hooks/useBookmarks';
import styles from './Card.module.css';

interface ArticleCardProps {
  card: TechCard;
  onStartLearning: (card: TechCard) => void;
}

/** 多平台技术文章卡 */
export function ArticleCard({ card, onStartLearning }: ArticleCardProps) {
  const { saved, onToggle } = useBookmarks(card.id);
  const logo = sourceLogo(card.source);
  const summary = card.summary || card.one_liner || '';
  const link = card.link || '';
  const tags = (card.tags ?? []).slice(0, 3);

  const openOriginal = () => {
    if (link) {
      window.open(link, '_blank');
    }
  };

  return (
    <>
      <div className={styles.cardHead}>
        <span className={clsx(styles.srcLogo, styles[logo.key])}>{logo.abbr}</span>
        <span className={styles.artSource}>{card.source}</span>
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

      <div className={styles.artTitle} onClick={openOriginal}>
        <a>{card.title}</a>
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
        <span className={styles.heat}>
          <span>
            🔥 <b>{Math.max(card.score ?? 0, 0)}</b>
          </span>
          <span>
            💬 <b>{Math.max(card.comments ?? 0, 0)}</b>
          </span>
        </span>
        <span className={styles.ops}>
          <button className="mini-btn solid" type="button" onClick={() => onStartLearning(card)}>
            开始学习
          </button>
        </span>
      </div>
    </>
  );
}
