import clsx from 'clsx';
import { useEffect, useRef } from 'react';

import { useLearningSession } from '../hooks/useLearningSession';
import { ChatBubble, TypingBubble } from './ChatBubble';
import styles from './LearningDrawer.module.css';

/**
 * 学习会话抽屉：三态 loading / explanation / chatting。
 * 对应原 index.html 的 .drawer 区块。
 */
export function LearningDrawer() {
  const {
    open,
    state,
    sessionId,
    card,
    topic,
    explanation,
    messages,
    pending,
    question,
    setQuestion,
    startChatting,
    send,
    close,
    restore,
  } = useLearningSession();
  const chatBoxRef = useRef<HTMLDivElement>(null);

  // 刷新或换设备后，从服务端续上最近一次未完成的会话（每次登录态只尝试一次）
  useEffect(() => {
    void restore();
  }, [restore]);

  // Esc 关闭（对应原 body 上的 @keydown.escape.window）
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        close();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, close]);

  // 新消息或等待回复时自动滚到底部
  useEffect(() => {
    const box = chatBoxRef.current;
    if (box && state === 'chatting') {
      box.scrollTop = box.scrollHeight;
    }
  }, [messages, pending, state]);

  if (!open) return null;

  // 从服务端恢复的会话没有卡片对象，退回用会话主题；两者都没有才用兜底文案
  const title = card?.title || topic || '深度讲解';
  const canSend = !pending && question.trim().length > 0;

  return (
    <>
      {/* 半透明底 */}
      <div className="overlay" style={{ zIndex: 45 }} onClick={close} />
      <aside className={styles.drawer}>
        <div className={styles.drawerHead}>
          <div style={{ minWidth: 0 }}>
            <div className={styles.title}>{title}</div>
            <div className={styles.sub}>
              会话 <span>{sessionId || '…'}</span> · DeepSeek 驱动
            </div>
          </div>
          <button className={clsx('tiny-btn', styles.close)} type="button" onClick={close}>
            ✕
          </button>
        </div>

        {state === 'loading' && (
          <div className={styles.chatLoading}>
            <div className="spin" />
            <p>正在准备讲解内容…</p>
          </div>
        )}

        {state === 'explanation' && (
          <div className={styles.chatBody}>
            <ChatBubble role="assistant" content={explanation} />
            <button
              className={clsx('btn', 'btn-primary', styles.startChat)}
              type="button"
              onClick={startChatting}
            >
              🚀 开始提问
            </button>
          </div>
        )}

        {state === 'chatting' && (
          <div className={styles.chatting}>
            <div className={styles.chatBody} ref={chatBoxRef}>
              {/*
                key 不能只用 timestamp：一条 INSERT 落库的 user + assistant 拿到的是
                同一个事务时间戳（`now()`），恢复历史会话时会撞 key，React 会丢掉其中一条。
                列表只追加、不重排，因此「序号 + 角色」就是稳定且唯一的键。
              */}
              {messages.map((message, index) => (
                <ChatBubble
                  key={`${index}-${message.role}`}
                  role={message.role}
                  content={message.content}
                  error={message.error}
                />
              ))}
              {pending && <TypingBubble />}
            </div>
            <div className={styles.drawerInput}>
              <input
                type="text"
                value={question}
                disabled={pending}
                placeholder="追问细节，例如：它的核心设计思路是什么？"
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    void send();
                  }
                }}
              />
              <button
                className={styles.send}
                type="button"
                disabled={!canSend}
                onClick={() => void send()}
              >
                ↑
              </button>
            </div>
          </div>
        )}
      </aside>
    </>
  );
}
