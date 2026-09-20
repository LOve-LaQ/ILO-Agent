import clsx from 'clsx';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { ApiError } from '../../shared/api/client';
import type { TechCard } from '../../shared/types/card';
import { ActivityTimeline } from './components/ActivityTimeline';
import { BookmarkList } from './components/BookmarkList';
import { LoadingNote, Note } from './components/Note';
import { ProfileSummary } from './components/ProfileSummary';
import { SessionDetail } from './components/SessionDetail';
import { SessionList } from './components/SessionList';
import { useActivities } from './hooks/useActivities';
import { useBookmarkList } from './hooks/useBookmarkList';
import { useProfile } from './hooks/useProfile';
import { useSessions } from './hooks/useSessions';
import styles from './LibraryPage.module.css';

type LibraryTab = 'bookmarks' | 'sessions' | 'activities';

const TABS: ReadonlyArray<{ value: LibraryTab; label: string }> = [
  { value: 'bookmarks', label: '🔖 收藏' },
  { value: 'sessions', label: '📚 学习记录' },
  { value: 'activities', label: '🧭 行为时间线' },
];

interface LibraryPageProps {
  /** 账号区域插槽，由组合根注入（本页不依赖 auth feature） */
  accountSlot?: ReactNode;
  /** 开始学习回调，由组合根注入（learning 是另一个 feature） */
  onStartLearning?: (card: TechCard) => void;
}

/** 把查询错误翻译成用户能理解的一句话；503 是「环境没配数据库」而非「你操作错了」 */
function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 503) {
      return '当前环境未启用数据库，个人中心暂不可用。';
    }
    return error.message;
  }
  return '加载失败，请稍后重试。';
}

/**
 * 「我的」页：个人概览 + 收藏 + 学习记录 + 行为时间线。
 *
 * 三块内容对应阶段 3 的两条溯源链：
 * - 收藏与学习记录是**行为溯源**（用户做过什么）
 * - 收藏行里可展开的溯源面板是**内容溯源**（这条内容从哪来）
 *
 * 本页只做「读」与「取消收藏」；开始学习需要通过组合根注入的回调，因为学习抽屉属于
 * 另一个 feature，features 之间不得互相 import。
 */
export function LibraryPage({ accountSlot, onStartLearning }: LibraryPageProps) {
  const [activeTab, setActiveTab] = useState<LibraryTab>('bookmarks');
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [actionFilter, setActionFilter] = useState<string | undefined>(undefined);

  const profile = useProfile();
  const bookmarks = useBookmarkList();
  const sessions = useSessions();
  const activities = useActivities(actionFilter);

  const sessionItems = sessions.data?.items ?? [];
  // 没有显式选择时默认展示最近一次会话：列表按 started_at 倒序，第一条就是最新的
  const effectiveSessionId = selectedSessionId ?? sessionItems[0]?.session_id ?? null;

  return (
    <>
      <nav className={styles.nav}>
        <div className={clsx('wrap', styles.navInner)}>
          <div className={styles.brand}>
            <span className={styles.brandMark}>I</span>
            <span>我的技术情报</span>
          </div>
          <div className={styles.navRight}>
            <Link className="mini-btn" to="/">
              ← 返回发现页
            </Link>
            {accountSlot}
          </div>
        </div>
      </nav>

      <main className={clsx('wrap', styles.main)}>
        {profile.isPending && <LoadingNote title="正在读取账号信息…" />}
        {profile.isError && (
          <Note
            icon="📡"
            tone="danger"
            title={errorText(profile.error)}
            onRetry={() => void profile.refetch()}
            retryLabel="↻ 重新加载"
          />
        )}
        {profile.data && <ProfileSummary profile={profile.data} />}

        <div className={styles.tabs}>
          {TABS.map((tab) => (
            <button
              key={tab.value}
              type="button"
              aria-pressed={activeTab === tab.value}
              className={clsx(styles.tab, activeTab === tab.value && styles.tabOn)}
              onClick={() => setActiveTab(tab.value)}
            >
              {tab.label}
              <span className={styles.tabCount}>
                {tab.value === 'bookmarks'
                  ? (profile.data?.stats.bookmarks_total ?? '–')
                  : tab.value === 'sessions'
                    ? (profile.data?.stats.sessions_total ?? '–')
                    : (profile.data?.stats.activities_total ?? '–')}
              </span>
            </button>
          ))}
        </div>

        <section className={styles.panel}>
          {activeTab === 'bookmarks' && (
            <>
              {bookmarks.isPending && <LoadingNote title="正在读取收藏…" />}
              {bookmarks.isError && (
                <Note
                  icon="📡"
                  tone="danger"
                  title={errorText(bookmarks.error)}
                  onRetry={() => void bookmarks.refetch()}
                />
              )}
              {bookmarks.data &&
                (bookmarks.data.items.length === 0 ? (
                  <Note
                    icon="🔖"
                    title="还没有收藏"
                    hint="在发现页点卡片上的 🔖 即可收藏，收藏会存在服务端，换设备也还在。"
                  />
                ) : (
                  <BookmarkList
                    items={bookmarks.data.items}
                    onStartLearning={onStartLearning}
                  />
                ))}
            </>
          )}

          {activeTab === 'sessions' && (
            <>
              {sessions.isPending && <LoadingNote title="正在读取学习记录…" />}
              {sessions.isError && (
                <Note
                  icon="📡"
                  tone="danger"
                  title={errorText(sessions.error)}
                  onRetry={() => void sessions.refetch()}
                />
              )}
              {sessions.data &&
                (sessionItems.length === 0 ? (
                  <Note
                    icon="📚"
                    title="还没有学习记录"
                    hint="在发现页点「开始学习」会创建一个会话，进度与对话都会存到服务端。"
                  />
                ) : (
                  <div className={styles.split}>
                    <SessionList
                      items={sessionItems}
                      selectedId={effectiveSessionId}
                      onSelect={setSelectedSessionId}
                    />
                    {effectiveSessionId && (
                      <div className={styles.splitAside}>
                        <SessionDetail sessionId={effectiveSessionId} />
                      </div>
                    )}
                  </div>
                ))}
            </>
          )}

          {activeTab === 'activities' && (
            <>
              {activities.isPending && <LoadingNote title="正在读取行为记录…" />}
              {activities.isError && (
                <Note
                  icon="📡"
                  tone="danger"
                  title={errorText(activities.error)}
                  onRetry={() => void activities.refetch()}
                />
              )}
              {activities.data &&
                (activities.data.items.length === 0 ? (
                  <Note
                    icon="🧭"
                    title={actionFilter ? '该动作下暂无记录' : '还没有行为记录'}
                    hint="登录、收藏、提问、提交测验等动作都会留下可对账的流水。"
                  />
                ) : (
                  <ActivityTimeline
                    items={activities.data.items}
                    actionType={actionFilter}
                    onChangeAction={setActionFilter}
                  />
                ))}
            </>
          )}
        </section>
      </main>
    </>
  );
}
