import clsx from 'clsx';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { ApiError } from '../../shared/api/client';
import type { AdminUserItem } from './api';
import { CostPanel } from './components/CostPanel';
import {
  useAdminUsers,
  useFeatureFlags,
  useToggleFlag,
  useUpdateRole,
} from './hooks/useAdmin';
import styles from './AdminPage.module.css';

interface AdminPageProps {
  /** 账号区域插槽，由组合根注入（本页不依赖 auth feature） */
  accountSlot?: ReactNode;
  /** 是否管理员：真正的边界在后端 require_admin，这里只决定是否渲染内容 */
  isAdmin: boolean;
  /** 当前登录用户 id：用于禁用「修改自己角色」那一路操作 */
  currentUserId?: string;
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return '该操作需要管理员权限。';
    return error.message;
  }
  return '加载失败，请稍后重试。';
}

function formatTime(value?: string | null): string {
  if (!value) return '未修改';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '未修改';
  return date.toLocaleString('zh-CN', { hour12: false });
}

/**
 * 管理后台：LLM 成本 + 功能开关 + 用户角色。
 *
 * 【为什么三件事放一页】它们服务同一个目的 —— 控制「谁能让系统花多少钱」：
 * 成本面板给出**花了多少、还会花多少**，开关决定**功能是否可用**（止血的手），
 * 角色决定**谁能改开关**。拆成三个页面会让「为什么贵」「该不该关」「为什么我关不掉」
 * 这三类排查各自缺一半信息。
 *
 * 【为什么开关不显示乐观状态】见 hooks/useAdmin.ts：管理员必须看到服务端
 * 确认过的真实状态，否则会误以为已经止血。
 */
export function AdminPage({ accountSlot, isAdmin, currentUserId }: AdminPageProps) {
  const flags = useFeatureFlags();
  const toggle = useToggleFlag();
  const [search, setSearch] = useState('');
  const users = useAdminUsers(search);
  const updateRole = useUpdateRole();

  if (!isAdmin) {
    return (
      <>
        <nav className={styles.nav}>
          <div className={clsx('wrap', styles.navInner)}>
            <div className={styles.brand}>
              <span className={styles.brandMark}>I</span>
              <span>管理后台</span>
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
          <div className={styles.notice}>
            <div className={styles.noticeTitle}>需要管理员权限</div>
            <p className={styles.noticeBody}>
              当前账号不是管理员。管理员身份由服务端决定：在 backend/.env 的
              ADMIN_EMAILS 中配置你的邮箱，或用已有管理员在下方列表里授权。
            </p>
          </div>
        </main>
      </>
    );
  }

  return (
    <>
      <nav className={styles.nav}>
        <div className={clsx('wrap', styles.navInner)}>
          <div className={styles.brand}>
            <span className={styles.brandMark}>I</span>
            <span>管理后台</span>
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
        <CostPanel />

        <section className={styles.section}>
          <header className={styles.sectionHead}>
            <h2 className={styles.sectionTitle}>功能开关</h2>
            <p className={styles.sectionHint}>
              关闭后相关接口立即返回「功能已关闭」。开关只影响 LLM 与抓取这类
              会产生外部成本的模块，首页资讯与原文阅读始终可用。
            </p>
          </header>

          {flags.isPending && <div className={styles.placeholder}>正在读取开关…</div>}
          {flags.isError && (
            <div className={styles.notice}>
              <div className={styles.noticeTitle}>读取失败</div>
              <p className={styles.noticeBody}>{errorText(flags.error)}</p>
            </div>
          )}

          <div className={styles.flagList}>
            {flags.data?.items.map((flag) => {
              const pending = toggle.isPending && toggle.variables?.key === flag.key;
              return (
                <div key={flag.key} className={styles.flagRow}>
                  <div className={styles.flagMain}>
                    <div className={styles.flagHead}>
                      <code className={styles.flagKey}>{flag.key}</code>
                      <span
                        className={clsx(
                          styles.badge,
                          flag.enabled ? styles.badgeOn : styles.badgeOff,
                        )}
                      >
                        {flag.enabled ? '已开启' : '已关闭'}
                      </span>
                    </div>
                    <p className={styles.flagDesc}>{flag.description}</p>
                    <p className={styles.flagMeta}>
                      默认值：{flag.default_enabled ? '开启' : '关闭'} · 最近修改：
                      {formatTime(flag.updated_at)}
                    </p>
                  </div>

                  <button
                    type="button"
                    role="switch"
                    aria-checked={flag.enabled}
                    aria-label={`${flag.key} 开关`}
                    disabled={pending}
                    className={clsx(styles.switch, flag.enabled && styles.switchOn)}
                    onClick={() => toggle.mutate({ key: flag.key, enabled: !flag.enabled })}
                  >
                    <span className={styles.knob} />
                  </button>
                </div>
              );
            })}
          </div>
        </section>

        <section className={styles.section}>
          <header className={styles.sectionHead}>
            <h2 className={styles.sectionTitle}>用户角色</h2>
            <p className={styles.sectionHint}>
              管理员可切换他人的角色；不能修改自己的角色（避免最后一个管理员
              把自己锁在门外）。
            </p>
          </header>

          <div className={styles.searchRow}>
            <input
              className={styles.search}
              type="search"
              value={search}
              placeholder="按邮箱或用户名搜索"
              onChange={(event) => setSearch(event.target.value)}
            />
            <span className={styles.countText}>
              {users.data ? `共 ${users.data.count} 位用户` : ''}
            </span>
          </div>

          {users.isPending && <div className={styles.placeholder}>正在读取用户…</div>}
          {users.isError && (
            <div className={styles.notice}>
              <div className={styles.noticeTitle}>读取失败</div>
              <p className={styles.noticeBody}>{errorText(users.error)}</p>
            </div>
          )}

          {users.data && users.data.items.length === 0 && (
            <div className={styles.placeholder}>没有匹配的用户。</div>
          )}

          {users.data && users.data.items.length > 0 && (
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>用户</th>
                    <th>邮箱</th>
                    <th>角色</th>
                    <th>状态</th>
                    <th>注册时间</th>
                  </tr>
                </thead>
                <tbody>
                  {users.data.items.map((item: AdminUserItem) => {
                    const isSelf = item.id === currentUserId;
                    const busy =
                      updateRole.isPending && updateRole.variables?.userId === item.id;
                    return (
                      <tr key={item.id}>
                        <td className={styles.cellUser} data-label="用户">
                          {item.username}
                          {isSelf && <span className={styles.selfTag}>自己</span>}
                        </td>
                        <td className={styles.mono} data-label="邮箱">
                          {item.email}
                        </td>
                        <td data-label="角色">
                          <select
                            className={styles.roleSelect}
                            value={item.role}
                            disabled={isSelf || busy}
                            aria-label={`${item.username} 的角色`}
                            onChange={(event) =>
                              updateRole.mutate({ userId: item.id, role: event.target.value })
                            }
                          >
                            <option value="user">user</option>
                            <option value="admin">admin</option>
                          </select>
                        </td>
                        <td data-label="状态">{item.is_active ? '正常' : '已停用'}</td>
                        <td className={styles.mono} data-label="注册时间">
                          {formatTime(item.created_at)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </main>
    </>
  );
}
