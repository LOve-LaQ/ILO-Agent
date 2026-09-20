import type { ReactNode } from 'react';

import { fmtDateTime } from '../../../shared/lib/format';
import type { ProvenanceBatch, ProvenanceResponse } from '../api';
import styles from '../LibraryPage.module.css';
import { LoadingNote, Note } from './Note';

interface ProvenancePanelProps {
  /** 溯源查询结果；未加载完为 undefined */
  data: ProvenanceResponse | undefined;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}

/** 后端返回的是平台标识，这里翻成展示名（与卡片上的来源文案对齐） */
const PLATFORM_LABELS: Record<string, string> = {
  github: 'GitHub',
  hackernews: 'Hacker News',
  lobsters: 'Lobsters',
  devto: 'dev.to',
  stackoverflow: 'Stack Overflow',
};

const STATUS_LABELS: Record<string, string> = {
  summarized: '已生成摘要',
  deduped: '去重跳过',
  failed: '失败',
};

const TRIGGER_LABELS: Record<string, string> = {
  manual: '手动触发',
  scheduled: '定时任务',
};

const BATCH_STATUS_LABELS: Record<string, string> = {
  running: '进行中',
  succeeded: '成功',
  partial: '部分失败',
  failed: '失败',
};

const KIND_LABELS: Record<string, string> = {
  repo: '仓库',
  article: '文章',
};

function batchDuration(batch: ProvenanceBatch): string {
  if (!batch.started_at) return '—';
  const start = new Date(batch.started_at).getTime();
  const end = batch.finished_at ? new Date(batch.finished_at).getTime() : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end)) return '—';
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  return seconds < 60 ? `${seconds} 秒` : `${Math.round(seconds / 60)} 分钟`;
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.pvRow}>
      <span className={styles.pvLabel}>{label}</span>
      <span className={styles.pvValue}>{children}</span>
    </div>
  );
}

/**
 * 内容溯源面板：回答「这张卡片是从哪来的」。
 *
 * 展示的是采集当时的事实（哪次批次、哪个链接、摘要生成前的原文），而不是当前知识库里
 * 那份可能已被覆盖的内容 —— 所以批次、原文、时间缺一不可，缺了就没法核对。
 */
export function ProvenancePanel({ data, loading, error, onRetry }: ProvenancePanelProps) {
  if (loading) {
    return <LoadingNote title="正在读取溯源记录…" />;
  }

  if (error || !data) {
    return (
      <Note
        icon="📡"
        tone="danger"
        title="溯源记录读取失败"
        onRetry={onRetry}
        retryLabel="↻ 重新读取"
      />
    );
  }

  if (!data.known) {
    return (
      <Note
        icon="🕳"
        title="暂无采集溯源记录"
        hint="历史卡片与内置示例在引入采集链路之前就已入库，没有可回溯的批次与原文。"
      />
    );
  }

  const snapshotTitle = data.snapshot?.title;
  const currentTitle = data.card?.title;
  const overwritten = Boolean(snapshotTitle && currentTitle && snapshotTitle !== currentTitle);

  return (
    <div className={styles.provenance}>
      {data.backfilled && (
        <p className={styles.pvBadge}>
          ⚠️ 历史数据回填：时间段与原文为近似值，批次未知
        </p>
      )}

      <Row label="来源平台">
        {data.source_platform ? (PLATFORM_LABELS[data.source_platform] ?? data.source_platform) : '—'}
      </Row>
      <Row label="原文链接">
        {data.source_url ? (
          <a href={data.source_url} target="_blank" rel="noopener noreferrer">
            {data.source_url}
          </a>
        ) : (
          '—'
        )}
      </Row>
      <Row label="采集时间">{fmtDateTime(data.collected_at) || '—'}</Row>
      <Row label="摘要生成">{fmtDateTime(data.summary_generated_at) || '—'}</Row>
      <Row label="记录状态">
        {data.status ? (STATUS_LABELS[data.status] ?? data.status) : '—'}
      </Row>

      <div className={styles.pvBlock}>
        <span className={styles.pvLabel}>摘要前原文</span>
        {data.raw_description ? (
          <blockquote className={styles.pvRaw}>{data.raw_description}</blockquote>
        ) : (
          <p className={styles.pvMissing}>该条记录没有留存原文（采集时平台未提供描述）。</p>
        )}
      </div>

      {data.batch ? (
        <div className={styles.pvBlock}>
          <span className={styles.pvLabel}>采集批次</span>
          <div className={styles.pvBatch}>
            <Row label="批次号">
              <code>{data.batch.id}</code>
            </Row>
            <Row label="类型">
              {KIND_LABELS[data.batch.kind] ?? data.batch.kind} ·{' '}
              {TRIGGER_LABELS[data.batch.trigger] ?? data.batch.trigger}
            </Row>
            <Row label="结果">
              {BATCH_STATUS_LABELS[data.batch.status] ?? data.batch.status} · 抓取{' '}
              {data.batch.fetched_count} / 新增 {data.batch.new_count} / 去重{' '}
              {data.batch.skipped_count} / 失败 {data.batch.failed_count}
            </Row>
            <Row label="耗时">{batchDuration(data.batch)}</Row>
            <Row label="开始">{fmtDateTime(data.batch.started_at) || '—'}</Row>
            {data.batch.params && (
              <Row label="参数">
                <code>{JSON.stringify(data.batch.params)}</code>
              </Row>
            )}
            {data.batch.error && <Row label="错误">{data.batch.error}</Row>}
          </div>
        </div>
      ) : (
        <div className={styles.pvBlock}>
          <span className={styles.pvLabel}>采集批次</span>
          <p className={styles.pvMissing}>该记录未关联批次（历史数据回填只补事实，不编造批次）。</p>
        </div>
      )}

      {overwritten && (
        <div className={styles.pvBlock}>
          <span className={styles.pvLabel}>内容已更新</span>
          <p className={styles.pvMissing}>
            入库快照标题为「{snapshotTitle}」，当前知识库标题为「{currentTitle}」。
          </p>
        </div>
      )}
    </div>
  );
}
