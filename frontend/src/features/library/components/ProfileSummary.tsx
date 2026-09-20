import type { LearningStats, MeProfileResponse } from '../../../shared/api/me';
import { fmtDateTime } from '../../../shared/lib/format';
import styles from '../LibraryPage.module.css';

interface ProfileSummaryProps {
  profile: MeProfileResponse;
}

interface StatCell {
  label: string;
  value: string;
}

/**
 * 概览只放「用过多少」这类使用痕迹，不放「完成率 / 平均分」。
 *
 * 这是技术情报推送工具，深度学习是**可选动作**，不是必须修完的课程。把完成度和分数
 * 摆在首页，等于把使用痕迹变成待考核的 KPI —— 那正是「学习软件」的语汇。
 */
function statCells(stats: LearningStats): StatCell[] {
  return [
    { label: '学习会话', value: String(stats.sessions_total) },
    { label: '收藏', value: String(stats.bookmarks_total) },
    { label: '行为记录', value: String(stats.activities_total) },
  ];
}

/**
 * 个人概览：账号信息 + 汇总统计。
 *
 * 这里的数字全部来自服务端聚合（`/me/profile` 一条 SQL 出全部数字），
 * 不是前端拿列表长度凑的 —— 收藏与行为都可能超过一页，凑出来的数会是错的。
 */
export function ProfileSummary({ profile }: ProfileSummaryProps) {
  const { user, stats } = profile;

  return (
    <section className={styles.profile}>
      <div className={styles.profileMain}>
        <div className={styles.avatar} aria-hidden="true">
          {user.username.slice(0, 1).toUpperCase()}
        </div>
        <div className={styles.profileMeta}>
          <h1 className={styles.profileName}>{user.username}</h1>
          <p className={styles.profileSub}>
            {user.email} · 注册于 {fmtDateTime(user.created_at)}
          </p>
        </div>
      </div>

      <dl className={styles.stats}>
        {statCells(stats).map((cell) => (
          <div key={cell.label} className={styles.stat}>
            <dt className={styles.statLabel}>{cell.label}</dt>
            <dd className={styles.statValue}>{cell.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
