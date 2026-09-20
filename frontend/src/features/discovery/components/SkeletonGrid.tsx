import styles from './SkeletonGrid.module.css';

/** 与原 index.html 一致：首屏展示 4 张骨架卡 */
const SKELETON_COUNT = 4;

export function SkeletonGrid() {
  return (
    <div className={styles.skeleton}>
      {Array.from({ length: SKELETON_COUNT }, (_, index) => (
        <div key={index} className={styles.skCard} />
      ))}
    </div>
  );
}
