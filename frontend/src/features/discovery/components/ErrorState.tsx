import styles from './ErrorState.module.css';

interface ErrorStateProps {
  message: string;
  onRetry: () => void;
}

/**
 * 加载失败的显式错误态。
 * 原 index.html 在后端不可用时会静默降级到前端硬编码的 FALLBACK_NEWS，
 * 这里按计划改为「显式错误 + 重试按钮」，不再伪造数据。
 */
export function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div className={styles.error}>
      <div className={styles.big}>📡</div>
      <p className={styles.msg}>{message}</p>
      <button className="btn btn-primary" type="button" onClick={onRetry}>
        ↻ 重新加载
      </button>
    </div>
  );
}
