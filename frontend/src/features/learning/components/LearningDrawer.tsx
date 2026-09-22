import clsx from 'clsx';
import { useEffect, useRef } from 'react';

import { MarkdownBody } from '../../../components/ui/MarkdownBody';
import { useLearningSession } from '../hooks/useLearningSession';
import { ChatBubble, TypingBubble } from './ChatBubble';
import styles from './LearningDrawer.module.css';

/**
 * 学习会话抽屉：四态 reading（先读原文）/ loading（建会话）/ explanation（卡片要点）
 * / chatting（问答）。
 * 对应原 index.html 的 .drawer 区块，并新增「原文优先」的阅读态。
 *
 * 阅读态内还有一层视图切换：`original`（英文原文快照）↔ `digest`（AI 中文导读）。
 * 大量仓库 README 只有英文，导读是给中文读者的「不读原文也能判断值不值得看」的兜底。
 */

/** 中文导读生成不了时的原因 → 面向用户的中文提示（后端 reason 见 CardDigestResponse） */
const DIGEST_REASON_TEXT: Record<string, string> = {
  no_readme: '这个仓库暂时取不到 README 原文，无法生成中文导读。',
  generation_failed: '中文导读生成失败，请稍后重试。',
  not_found: '找不到这张卡片的采集记录，无法生成中文导读。',
  db_unavailable: '数据服务暂时不可用，请稍后重试。',
  db_error: '读取数据失败，请稍后重试。',
};

export function LearningDrawer() {
  const {
    open,
    state,
    sessionId,
    card,
    content,
    digest,
    digestPending,
    readView,
    setReadView,
    topic,
    explanation,
    messages,
    pending,
    question,
    setQuestion,
    startChatting,
    loadDigest,
    showCardPoints,
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
  // 原文来源：优先用快照记录的 source_url，其次退回卡片自身的 link
  const sourceUrl = content?.source_url || card?.link || '';
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
              {state === 'reading' ? (
                readView === 'digest'
                  ? 'AI 中文导读 · 依据 README 生成，仅供参考'
                  : '原文快照 · 先读一读，有疑问再提问'
              ) : (
                <>
                  会话 <span>{sessionId || '…'}</span> · DeepSeek 驱动
                </>
              )}
            </div>
          </div>
          <button className={clsx('tiny-btn', styles.close)} type="button" onClick={close}>
            ✕
          </button>
        </div>

        {state === 'loading' && (
          <div className={styles.chatLoading}>
            <div className="spin" />
            <p>正在创建学习会话…</p>
          </div>
        )}

        {state === 'reading' && (
          <div className={styles.reading}>
            {readView === 'digest' ? (
              <div className={styles.sourceBar}>
                <span>AI 中文导读</span>
                {digest?.origin === 'generated' ? <code>本次生成</code> : null}
                {digest?.origin === 'cache' ? <code>已缓存</code> : null}
                {sourceUrl ? (
                  <a href={sourceUrl} target="_blank" rel="noopener noreferrer">
                    打开仓库 ↗
                  </a>
                ) : null}
              </div>
            ) : content ? (
              <div className={styles.sourceBar}>
                {content.origin === 'unavailable' ? (
                  <span className={styles.sourceWarn}>
                    未取到原文快照，展示采集时的原始简介
                  </span>
                ) : (
                  <>
                    <span>原文快照</span>
                    {content.meta?.path ? <code>{content.meta.path}</code> : null}
                    {content.meta?.sha ? <code>{content.meta.sha.slice(0, 7)}</code> : null}
                    {content.meta?.fetched_at ? (
                      <span>{content.meta.fetched_at.slice(0, 10)}</span>
                    ) : null}
                  </>
                )}
                {sourceUrl ? (
                  <a href={sourceUrl} target="_blank" rel="noopener noreferrer">
                    打开仓库 ↗
                  </a>
                ) : null}
              </div>
            ) : null}

            <div className={styles.readBody}>
              {readView === 'digest' ? (
                digestPending ? (
                  <p className={styles.readPlaceholder}>正在生成中文导读…（首次会调用一次模型）</p>
                ) : digest?.digest ? (
                  <>
                    <MarkdownBody content={digest.digest} allowImages={false} />
                    <p className={styles.readNote}>
                      AI 依据 README 生成的中文导读，仅供快速了解，细节以原文为准。
                    </p>
                  </>
                ) : (
                  <>
                    <p className={styles.readPlaceholder}>
                      {DIGEST_REASON_TEXT[digest?.reason ?? ''] ||
                        '中文导读暂时不可用，请稍后重试。'}
                    </p>
                    {digest?.fallback_description ? (
                      <p className={styles.readNote}>{digest.fallback_description}</p>
                    ) : null}
                  </>
                )
              ) : !content ? (
                <p className={styles.readPlaceholder}>正在获取原文…</p>
              ) : content.content ? (
                <>
                  {/* 原文来自外部仓库且不受我们控制，故不允许加载远程图片 */}
                  <MarkdownBody content={content.content} allowImages={false} />
                  {content.truncated ? (
                    <p className={styles.readNote}>
                      原文过长，已截断展示。
                      {sourceUrl ? (
                        <>
                          {' '}
                          <a href={sourceUrl} target="_blank" rel="noopener noreferrer">
                            查看完整版本 ↗
                          </a>
                        </>
                      ) : null}
                    </p>
                  ) : null}
                </>
              ) : (
                <p className={styles.readPlaceholder}>
                  {content.fallback_description || '这个仓库暂时取不到可展示的原文。'}
                </p>
              )}
            </div>

            <div className={styles.readActions}>
              {readView === 'digest' ? (
                <button className="btn" type="button" onClick={() => setReadView('original')}>
                  📄 看原文
                </button>
              ) : (
                <button className="btn" type="button" onClick={() => void loadDigest()}>
                  🌐 中文导读
                </button>
              )}
              <button className="btn" type="button" onClick={showCardPoints}>
                📌 卡片要点
              </button>
              <button
                className={clsx('btn', 'btn-primary')}
                type="button"
                onClick={() => void startChatting()}
              >
                💬 直接提问
              </button>
            </div>
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
