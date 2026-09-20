import clsx from 'clsx';

import type { SessionItem } from '../../../shared/api/me';
import { fmtDateTime, fmtRelative } from '../../../shared/lib/format';
import styles from '../LibraryPage.module.css';

interface SessionListProps {
  items: SessionItem[];
  selectedId: string | null;
  onSelect: (sessionId: string) => void;
}

/** 会话状态 → 人话。取值见后端 learning_service.SESSION_STATES */
const STATE_LABELS: Record<string, string> = {
  idle: '已创建',
  pushed: '已推送',
  learning: '学习中',
  quiz: '测验中',
  fsrs_update: '复习计划',
  completed: '已完成',
};

/** 状态色调：完成是正向，未完成是中性的「进行中」 */
function isDone(state: string): boolean {
  return state === 'completed';
}

/**
 * 学习记录列表。
 *
 * 这些会话的真相源在 PostgreSQL，Redis 里的上下文过期后依然能回看 —— 这正是阶段 3
 * 要解决的问题（此前对话历史一小时后就蒸发）。
 *
 * 刻意不展示测验分数：这是技术情报推送工具，深度学习是可选动作，不是需要按分数验收
 * 的课程。列表只回答「我深度看过哪些内容」。
 */
export function SessionList({ items, selectedId, onSelect }: SessionListProps) {
  return (
    <div className={styles.list}>
      {items.map((session) => (
        <button
          key={session.session_id}
          type="button"
          aria-pressed={session.session_id === selectedId}
          className={clsx(styles.sessionRow, session.session_id === selectedId && styles.sessionOn)}
          onClick={() => onSelect(session.session_id)}
        >
          <span className={styles.sessionMain}>
            <span className={styles.rowHead}>
              <span className={clsx(styles.chip, isDone(session.state) && styles.chipOk)}>
                {STATE_LABELS[session.state] ?? session.state}
              </span>
              <span className={styles.sessionTopic}>{session.topic}</span>
            </span>
            <span className={styles.sessionMeta}>
              {fmtDateTime(session.started_at)} · {session.message_count} 条对话
              {session.card_id ? '' : ' · 无关联卡片'}
            </span>
          </span>
          <span className={styles.sessionAside}>
            {session.completed_at ? fmtRelative(session.completed_at) : '进行中'}
          </span>
        </button>
      ))}
    </div>
  );
}
