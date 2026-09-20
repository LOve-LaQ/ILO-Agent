import clsx from 'clsx';

import { MarkdownBody } from '../../../components/ui/MarkdownBody';
import { fmtDateTime } from '../../../shared/lib/format';
import { useSessionDetail } from '../hooks/useSessions';
import styles from '../LibraryPage.module.css';
import { LoadingNote, Note } from './Note';

interface SessionDetailProps {
  sessionId: string;
}

/**
 * 对话回看：与学习抽屉里的气泡同源渲染，保证同一批消息在两处长成一样。
 *
 * 和列表一样不展示测验得分与 FSRS 复习计划（理由见 SessionList）—— 这里只回放聊了什么。
 */
export function SessionDetail({ sessionId }: SessionDetailProps) {
  const { data, isPending, isError, refetch } = useSessionDetail(sessionId);

  if (isPending) {
    return <LoadingNote title="正在读取对话记录…" />;
  }

  if (isError || !data) {
    return (
      <Note
        icon="📡"
        tone="danger"
        title="对话记录读取失败"
        onRetry={() => void refetch()}
        retryLabel="↻ 重新读取"
      />
    );
  }

  const { session, messages } = data;

  return (
    <div className={styles.detail}>
      <div className={styles.detailHead}>
        <h3 className={styles.detailTitle}>{session.topic}</h3>
        <p className={styles.detailMeta}>
          会话 <code>{session.session_id}</code> · 开始 {fmtDateTime(session.started_at)}
          {session.completed_at ? ` · 完成 ${fmtDateTime(session.completed_at)}` : ''}
        </p>
      </div>

      {messages.length === 0 ? (
        <Note
          icon="💬"
          title="这次会话还没有对话记录"
          hint="只创建了会话、还没进入问答，因此没有可回看的内容。"
        />
      ) : (
        <div className={styles.transcript}>
          {messages.map((message, index) => (
            <div
              key={`${message.created_at}-${index}`}
              className={clsx(
                styles.turn,
                message.role === 'user' ? styles.turnUser : styles.turnAssistant,
              )}
            >
              <div className={styles.turnBody}>
                <MarkdownBody content={message.content} />
              </div>
              <span className={styles.turnTime}>{fmtDateTime(message.created_at)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
