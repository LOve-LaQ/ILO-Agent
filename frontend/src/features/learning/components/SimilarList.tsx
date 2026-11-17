import clsx from 'clsx';
import { useState } from 'react';

import { useToastStore } from '../../../components/ui/toastStore';
import type { SimilarItem, TechCard } from '../../../shared/types/card';
import { useSimilarCards } from '../hooks/useSimilarCards';
import styles from './SimilarList.module.css';

interface SimilarListProps {
  cardId: string;
  /** 需要时才请求（抽屉处于阅读态） */
  active: boolean;
  /** 点某一条 → 打开那条的学习抽屉，形成「一路看下去」的链路 */
  onPick: (card: TechCard) => void;
}

/**
 * 抽屉底部的「相似项目」折叠条。
 *
 * 【为什么默认折叠】抽屉的主体是原文 / 中文导读。一个常驻的推荐列表会实打实地
 * 挤掉正文的可读高度（抽屉只有 440px 宽、单屏高），而用户点开抽屉的第一需求
 * 是「读这篇」。折叠成一行 44px 的入口：不打扰阅读，想继续探索时随手可达。
 *
 * 【为什么点了直接换卡片而不是跳走】这里换的是同一个抽屉的内容（`onPick` 会
 * 重置会话状态并加载那条的原文），用户看完 A 看 B、看完 B 看 C 是连贯的，
 * 不该被迫关掉抽屉再回首页找。这也正是「相似推荐」这个功能存在的理由。
 *
 * 【四种状态都不报错】加载中给骨架、可用但为空、不可用、请求失败 —— 全部
 * 只是「这块不显示或显示一行提示」。推荐是增强内容，没有资格把整页弄红。
 */
export function SimilarList({ cardId, active, onPick }: SimilarListProps) {
  const [expanded, setExpanded] = useState(false);
  const showToast = useToastStore((state) => state.show);
  const { data, isPending, isError } = useSimilarCards(cardId, active);

  const items = data?.items ?? [];
  const unavailable = data?.based_on === 'unavailable';

  // 不可用 / 失败 / 确实没有相近项目：整块不渲染。留一句「暂无相似项目」
  // 只会让人怀疑功能坏了，不如安静地什么都不显示 —— 用户本来也没在找它。
  if (isError || unavailable) return null;
  if (!isPending && items.length === 0) return null;

  const handlePick = (item: SimilarItem) => {
    // 相似条目只有展示字段，这里补成抽屉能用的最小卡片：
    // startLearning 只依赖 id 与 title（其余用于创建会话时的上下文），
    // 而 core_concepts / summary 能让随后的 AI 讲解拿到更多背景
    const card: TechCard = {
      id: item.card_id,
      type: item.type,
      title: item.title,
      category: (item.category || 'other') as TechCard['category'],
      link: item.url ?? undefined,
      summary: item.summary_cn ?? undefined,
      language: item.language ?? undefined,
      stars: item.stars ?? undefined,
      core_concepts: item.core_concepts ?? [],
    };
    setExpanded(false); // 换卡片后回到折叠态，避免列表压在下一张卡的正文上
    onPick(card);
    showToast(`继续看「${item.title}」`);
  };

  return (
    <div className={styles.wrap}>
      <button
        className={clsx(styles.toggle, expanded && styles.toggleOpen)}
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <span className={styles.toggleLabel}>🔗 相似项目</span>
        <span className={styles.toggleCount}>{isPending ? '…' : `${items.length} 个`}</span>
        <span className={styles.chevron}>{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded ? (
        <div className={styles.list}>
          {isPending ? (
            <>
              <div className={styles.skeleton} />
              <div className={styles.skeleton} />
            </>
          ) : (
            items.map((item) => (
              <button
                className={styles.row}
                key={item.card_id}
                type="button"
                onClick={() => handlePick(item)}
                title={`打开「${item.title}」的原文与讲解`}
              >
                <div className={styles.rowTop}>
                  <span className={styles.name}>{item.title}</span>
                  <span className={styles.stars}>
                    {typeof item.stars === 'number' ? `★ ${item.stars}` : ''}
                  </span>
                </div>
                {item.summary_cn ? <p className={styles.summary}>{item.summary_cn}</p> : null}
                <div className={styles.reasonRow}>
                  <span className={styles.reason}>{item.reason}</span>
                  {typeof item.similarity === 'number' ? (
                    <span className={styles.score}>相似度 {item.similarity.toFixed(2)}</span>
                  ) : null}
                </div>
              </button>
            ))
          )}
          <p className={styles.foot}>
            按语义向量近邻召回（不是关键词搜索），点一条可直接接着读它的原文与讲解。
          </p>
        </div>
      ) : null}
    </div>
  );
}
