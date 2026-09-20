import styles from './EmptyState.module.css';

/** 知识库为空时的引导态 */
export function EmptyState() {
  return (
    <div className={styles.empty}>
      <div className={styles.big}>🛰</div>
      <p>知识库还是空的，点右上角「抓取最新」拉取第一批技术卡片</p>
    </div>
  );
}
