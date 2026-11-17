import type { TaskProgressResponse } from '../../../shared/types/card';
import styles from './RefreshOverlay.module.css';

interface RefreshOverlayProps {
  /** 服务端返回的任务快照；提交后到第一次轮询之间为 null */
  progress: TaskProgressResponse | null;
}

/** 把毫秒说成人话（超过一分钟要显示分钟，否则「97s」读者得自己换算） */
function formatElapsed(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) {
    return `${seconds}s`;
  }
  return `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`;
}

/**
 * 抓取进度遮罩
 *
 * 进度全部来自服务端轮询（`GET /discover/tasks/{id}`），不再由前端定时器编造 ——
 * 之前那版每 1.4 秒推进一句文案，5.6 秒就停在「正在整理技术卡片…」不动了，
 * 而真实抓取还要跑一两分钟，用户只能怀疑是不是卡死。
 */
export function RefreshOverlay({ progress }: RefreshOverlayProps) {
  // 总数还没上报时用不确定态而不是 0%：停在 0% 的进度条和「没开始」看起来一样
  const indeterminate = !progress || (progress.total === 0 && progress.status === 'running');
  const percent = progress?.percent ?? 0;
  const failed = progress?.status === 'failed';

  return (
    <div className="overlay">
      <div className={styles.progressCard}>
        <div className="spin" />
        <h3>{failed ? '抓取失败' : '正在获取最新内容'}</h3>

        <div className={styles.bar}>
          <i
            className={indeterminate ? styles.indeterminate : undefined}
            style={indeterminate ? undefined : { width: `${percent}%` }}
          />
        </div>

        <p className={styles.stage}>{progress?.stage_label ?? '正在提交任务…'}</p>

        {progress && (
          <p className={styles.meta}>
            {progress.total > 0
              ? `已处理 ${progress.processed}/${progress.total} · 已用 ${formatElapsed(progress.elapsed_ms)}`
              : `已用 ${formatElapsed(progress.elapsed_ms)}`}
          </p>
        )}

        {progress?.status === 'running' && (
          <p className={styles.hint}>抓取与摘要需要 1~3 分钟，可以先去忙别的</p>
        )}
      </div>
    </div>
  );
}
