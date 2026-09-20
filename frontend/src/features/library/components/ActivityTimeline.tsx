import type { ActivityItem } from '../../../shared/api/me';
import { fmtDateTime } from '../../../shared/lib/format';
import styles from '../LibraryPage.module.css';

interface ActivityTimelineProps {
  items: ActivityItem[];
  /** 当前过滤的动作类型；undefined 表示全部 */
  actionType: string | undefined;
  onChangeAction: (actionType: string | undefined) => void;
}

/**
 * 受控动作词表的展示名。取值必须与后端 `src/models/engagement.py` 的 `ACTION_TYPES`
 * 一致 —— 这里是展示层，不是真相源；多出或漏掉都会让筛选器与后端对不上。
 */
const ACTION_LABELS: Record<string, string> = {
  register: '注册',
  login: '登录',
  logout: '退出登录',
  view_card: '查看卡片',
  bookmark: '收藏',
  unbookmark: '取消收藏',
  start_session: '开始学习',
  push_notification: '推送讲解',
  process_response: '处理回答',
  ask_question: '提问',
  submit_quiz: '提交测验',
  complete_session: '完成学习',
};

/** 时间线里的动作图标：只是为了快速扫读，不承载语义 */
const ACTION_ICONS: Record<string, string> = {
  register: '🌱',
  login: '🔑',
  logout: '🚪',
  view_card: '👀',
  bookmark: '🔖',
  unbookmark: '🔓',
  start_session: '🚀',
  push_notification: '📣',
  process_response: '🧩',
  ask_question: '❓',
  submit_quiz: '📝',
  complete_session: '🏁',
};

/**
 * 行为时间线（行为溯源的对外视图）。
 *
 * 每条流水都带 `request_id`，它与后端日志里的 request_id 是同一个值 —— 也就是说
 * 「用户在界面上看到的这一条」可以直接拿去和后端日志对账，而不是只有一行时间戳。
 */
export function ActivityTimeline({ items, actionType, onChangeAction }: ActivityTimelineProps) {
  return (
    <div>
      <div className={styles.timelineFilter}>
        <label className={styles.filterLabel} htmlFor="activity-filter">
          动作筛选
        </label>
        <select
          id="activity-filter"
          className={styles.select}
          value={actionType ?? ''}
          onChange={(event) => onChangeAction(event.target.value || undefined)}
        >
          <option value="">全部动作</option>
          {Object.entries(ACTION_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </div>

      <ol className={styles.timeline}>
        {items.map((item, index) => (
          <li key={`${item.created_at}-${index}`} className={styles.timelineItem}>
            <span className={styles.timelineIcon} aria-hidden="true">
              {ACTION_ICONS[item.action_type] ?? '•'}
            </span>
            <div className={styles.timelineBody}>
              <div className={styles.timelineHead}>
                <span className={styles.timelineAction}>
                  {ACTION_LABELS[item.action_type] ?? item.action_type}
                </span>
                {item.target_type && (
                  <span className={styles.timelineTarget}>
                    {item.target_type}
                    {item.target_id ? `: ${item.target_id}` : ''}
                  </span>
                )}
                <span className={styles.timelineTime}>{fmtDateTime(item.created_at)}</span>
              </div>

              {item.metadata && Object.keys(item.metadata).length > 0 && (
                <code className={styles.timelineMeta}>{JSON.stringify(item.metadata)}</code>
              )}

              <div className={styles.timelineTrace}>
                <span>
                  request_id <code>{item.request_id ?? '—'}</code>
                </span>
                {item.ip && <span>IP {item.ip}</span>}
              </div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
