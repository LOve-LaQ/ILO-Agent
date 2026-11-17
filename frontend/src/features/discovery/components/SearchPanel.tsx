import clsx from 'clsx';
import { useEffect, useState } from 'react';

import { ApiError } from '../../../shared/api/client';
import { fmtRelative, fmtStars, langColor } from '../../../shared/lib/format';
import { useSearch } from '../hooks/useSearch';
import { SEARCH_MIN_STARS, SEARCH_SORTS, useDiscoveryStore } from '../store';
import styles from './SearchPanel.module.css';

/** 空态示例：搜「想法」比搜「关键词」难，给几个具体例子比一句说明有用 */
const EXAMPLES = ['vector database', 'llm agent', 'topic:rag', 'rust cli'];

/** 结果数：1,234 -> 1.2k，搜索的 total 是估算值，只做量级参考 */
function fmtCount(value?: number | null): string {
  const n = value ?? 0;
  if (n >= 10_000) return `${(n / 10_000).toFixed(1)} 万`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/**
 * 仓库搜索
 *
 * 【为什么不做输入即搜】GitHub Search API 未认证只有 10 次/分钟，认证后 30 次/分钟。
 * 每敲一个字打一次请求，一句话之内就把配额烧完，全站搜索一起降级。所以这里采用
 * 「回车 / 点按钮才提交」：输入只改本地 draft，提交后才写进 store 触发请求。
 *
 * 【为什么关键词放 store、草稿放本地】切到榜单再切回来，用户不该丢掉刚搜的词；
 * 但输入过程中的每个字符都不该被当成一次已生效的查询 —— 所以两者必须分开。
 */
export function SearchPanel() {
  const keyword = useDiscoveryStore((state) => state.searchKeyword);
  const language = useDiscoveryStore((state) => state.searchLanguage);
  const minStars = useDiscoveryStore((state) => state.searchMinStars);
  const sort = useDiscoveryStore((state) => state.searchSort);
  const setKeyword = useDiscoveryStore((state) => state.setSearchKeyword);
  const setLanguage = useDiscoveryStore((state) => state.setSearchLanguage);
  const setMinStars = useDiscoveryStore((state) => state.setSearchMinStars);
  const setSort = useDiscoveryStore((state) => state.setSearchSort);

  const [draft, setDraft] = useState(keyword);

  // 关键词被外部改动（示例按钮 / 清空）时同步输入框
  useEffect(() => {
    setDraft(keyword);
  }, [keyword]);

  const { data, isPending, isError, error, isFetching, refetch } = useSearch({
    q: keyword,
    language,
    minStars,
    sort,
  });

  const items = data?.items ?? [];
  const degraded = data?.source === 'stale' || data?.source === 'knowledge_base';
  const showSkeleton = isPending && keyword.length > 0;

  function submit(value?: string) {
    const next = (value ?? draft).trim();
    if (!next) return;
    if (next === keyword) {
      void refetch();
      return;
    }
    setKeyword(next);
  }

  const errorMessage =
    error instanceof ApiError
      ? error.message
      : error instanceof Error
        ? error.message
        : '搜索失败，请稍后重试';

  return (
    <section className={styles.panel} aria-label="仓库搜索">
      <header className={styles.head}>
        <div className={styles.headText}>
          <h1 className={styles.title}>
            <span className={styles.icon}>🔍</span> 搜仓库
          </h1>
          <div className={styles.meta}>
            {data
              ? `「${data.keyword}」约 ${fmtCount(data.total)} 个结果${
                  degraded ? '' : ' · 来自 GitHub'
                }`
              : '按关键词检索 GitHub，支持 topic:llm、language:rust 这类限定符'}
          </div>
        </div>

        <div className={styles.headActions}>
          {isFetching && keyword && <span className={styles.busy}>更新中…</span>}
          <div className={styles.seg} role="group" aria-label="排序方式">
            {SEARCH_SORTS.map((item) => (
              <button
                key={item.value}
                type="button"
                className={clsx(styles.segBtn, sort === item.value && styles.segOn)}
                onClick={() => setSort(item.value)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <button
            className="icon-btn"
            type="button"
            title="重新搜索"
            onClick={() => void refetch()}
            disabled={isFetching || !keyword}
          >
            ↻
          </button>
        </div>
      </header>

      <form
        className={styles.searchRow}
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <input
          className={styles.input}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="搜索仓库，例如 vector database、topic:llm"
          maxLength={64}
          aria-label="搜索关键词"
          autoComplete="off"
        />
        <button className="btn btn-primary" type="submit" disabled={!draft.trim()}>
          搜索
        </button>
      </form>

      <div className={styles.filters}>
        <div className={styles.chips} role="group" aria-label="语言筛选">
          {(data?.languages ?? [{ value: 'all', label: '全部' }]).map((option) => (
            <button
              key={option.value}
              type="button"
              className={clsx(styles.chip, language === option.value && styles.chipOn)}
              onClick={() => setLanguage(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className={styles.chips} role="group" aria-label="星数门槛">
          {SEARCH_MIN_STARS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={clsx(styles.chip, minStars === option.value && styles.chipOn)}
              onClick={() => setMinStars(option.value)}
            >
              ★ {option.label}
            </button>
          ))}
        </div>
      </div>

      {data?.note && (
        <div className={clsx(styles.notice, degraded && styles.noticeWarn)}>{data.note}</div>
      )}

      {!keyword && (
        <div className={styles.empty}>
          <p>输入关键词开始搜索。也可以试试这几个：</p>
          <div className={styles.examples}>
            {EXAMPLES.map((item) => (
              <button
                key={item}
                type="button"
                className={styles.example}
                onClick={() => submit(item)}
              >
                {item}
              </button>
            ))}
          </div>
        </div>
      )}

      {showSkeleton && <p className={styles.loading}>正在检索 GitHub…</p>}

      {isError && (
        <div className={styles.errorBox}>
          <p>{errorMessage}</p>
          <button className="btn btn-primary" type="button" onClick={() => void refetch()}>
            重试
          </button>
        </div>
      )}

      {!isPending && !isError && keyword && items.length === 0 && (
        <div className={styles.errorBox}>
          <p>没有找到匹配的仓库。换个关键词、放宽星数门槛，或去掉语言筛选试试。</p>
        </div>
      )}

      {items.length > 0 && (
        <ul className={clsx(styles.list, isFetching && styles.listBusy)}>
          {items.map((item) => (
            <li key={`${item.card_id}-${item.rank}`} className={styles.row}>
              <div className={styles.main}>
                <div className={styles.nameRow}>
                  <a
                    className={styles.repo}
                    href={item.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    title={item.full_name}
                  >
                    {item.full_name}
                  </a>
                  {item.has_explanation && (
                    <span className={styles.explained} title="已在知识库中，可以读到中文解读">
                      已解读
                    </span>
                  )}
                </div>
                {(item.summary_cn || item.description) && (
                  <p className={styles.desc}>{item.summary_cn || item.description}</p>
                )}
                <div className={styles.sub}>
                  {item.language && (
                    <span className={styles.lang}>
                      <i style={{ background: langColor(item.language) }} />
                      {item.language}
                    </span>
                  )}
                  {item.created_at && <span>创建于 {fmtRelative(item.created_at)}</span>}
                  {item.forks > 0 && <span>Fork {fmtStars(item.forks)}</span>}
                </div>
              </div>

              <span className={styles.stars}>
                <i>★</i>
                {fmtStars(item.stars)}
              </span>
            </li>
          ))}
        </ul>
      )}

      <footer className={styles.foot}>
        <span>
          {data?.source === 'knowledge_base'
            ? '本机知识库匹配结果（非 GitHub 全量）'
            : '数据来自 GitHub Search API'}
          {data?.fetched_at ? ` · 检索于 ${fmtRelative(data.fetched_at)}` : ''}
        </span>
        {data?.github_query ? <span className={styles.query}>{data.github_query}</span> : null}
      </footer>
    </section>
  );
}
