import clsx from 'clsx';

import type { TechCard } from '../../../shared/types/card';
import cardStyles from './Card.module.css';
import styles from './CardGrid.module.css';
import { ArticleCard } from './ArticleCard';
import { RepoCard } from './RepoCard';

interface CardGridProps {
  items: TechCard[];
  onStartLearning: (card: TechCard) => void;
}

/** 卡片流容器：仓库卡 / 文章卡按 item.type 分派 */
export function CardGrid({ items, onStartLearning }: CardGridProps) {
  return (
    <div className={clsx(styles.grid2, styles.feed)}>
      {items.map((item) => (
        <article key={item.id} className={cardStyles.card}>
          {/*
            保留原 DOM 结构：卡片内容外层原样包一层无名 div。
            原 index.html 的 .card 声明了 display:flex; gap:10px，但只有一个子元素时 gap 不生效，
            此处必须保留这层包裹，否则子元素之间会多出 10px 间距（破坏视觉零回归）。
          */}
          <div>
            {item.type === 'article' ? (
              <ArticleCard card={item} onStartLearning={onStartLearning} />
            ) : (
              <RepoCard card={item} onStartLearning={onStartLearning} />
            )}
          </div>
        </article>
      ))}
    </div>
  );
}
