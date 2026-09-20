import styles from './RefreshOverlay.module.css';

interface RefreshOverlayProps {
  /** 当前阶段下标 */
  stage: number;
  /** 阶段文案列表 */
  stages: string[];
}

/** 抓取进度遮罩：对应原 index.html 的 .overlay + .progress-card */
export function RefreshOverlay({ stage, stages }: RefreshOverlayProps) {
  const percent = stages.length > 0 ? (stage / stages.length) * 100 : 0;

  return (
    <div className="overlay">
      <div className={styles.progressCard}>
        <div className="spin" />
        <h3>正在获取最新内容</h3>
        <div className={styles.bar}>
          <i style={{ width: `${percent}%` }} />
        </div>
        <p>{stages[stage] ?? ''}</p>
      </div>
    </div>
  );
}
