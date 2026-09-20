import styles from './Note.module.css';

interface NoteProps {
  /** 空态 / 错误态的大图标 */
  icon?: string;
  title: string;
  hint?: string;
  /** 有重试动作时渲染按钮；没有就是纯提示 */
  onRetry?: () => void;
  retryLabel?: string;
  /** 错误态用一点红色强调，普通空态保持中性 */
  tone?: 'quiet' | 'danger';
}

/**
 * 区块级状态提示（空态 / 错误态 / 加载文案）。
 *
 * 不借用发现页的 EmptyState / ErrorState：那两处是发现页的视觉组成部分，features 之间
 * 不得互相 import；这里是「我的」页自己的一套提示，形态相同但相互独立。
 */
export function Note({ icon, title, hint, onRetry, retryLabel = '↻ 重试', tone = 'quiet' }: NoteProps) {
  return (
    <div className={tone === 'danger' ? styles.danger : styles.note}>
      {icon && <div className={styles.big}>{icon}</div>}
      <p className={styles.title}>{title}</p>
      {hint && <p className={styles.hint}>{hint}</p>}
      {onRetry && (
        <button className="mini-btn" type="button" onClick={onRetry}>
          {retryLabel}
        </button>
      )}
    </div>
  );
}

/** 骨架占位：形状固定，避免加载完成前后页面高度跳动 */
export function LoadingNote({ title = '正在加载…' }: { title?: string }) {
  return (
    <div className={styles.loading}>
      <div className="spin" />
      <p>{title}</p>
    </div>
  );
}
