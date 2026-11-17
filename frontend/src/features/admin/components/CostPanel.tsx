import clsx from 'clsx';
import { useMemo, useState } from 'react';

import { ApiError } from '../../../shared/api/client';
import type { DailyCostPoint } from '../api';
import { useLlmUsage } from '../hooks/useAdmin';
import styles from './CostPanel.module.css';

const WINDOWS = [7, 14, 30] as const;

const CONFIDENCE_TEXT: Record<string, string> = {
  high: '可信度高',
  medium: '可信度中等',
  low: '趋势不稳',
  insufficient: '样本不足',
};

/** 金额格式化：小额保留 4 位，否则 ¥0.0001 全被抹成 0 */
function money(value: number): string {
  const abs = Math.abs(value);
  if (abs === 0) return '¥0';
  if (abs < 0.01) return `¥${value.toFixed(4)}`;
  if (abs < 1) return `¥${value.toFixed(3)}`;
  return `¥${value.toFixed(2)}`;
}

function compactTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

function shortDate(iso: string): string {
  // "2026-11-16" → "11-16"
  return iso.slice(5);
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return '该操作需要管理员权限。';
    return error.message;
  }
  return '读取失败，请稍后重试。';
}

interface KpiProps {
  label: string;
  value: string;
  hint: string;
  emphasis?: boolean;
}

function Kpi({ label, value, hint, emphasis }: KpiProps) {
  return (
    <div className={clsx(styles.kpi, emphasis && styles.kpiEmphasis)}>
      <span className={styles.kpiLabel}>{label}</span>
      <strong className={styles.kpiValue}>{value}</strong>
      <span className={styles.kpiHint}>{hint}</span>
    </div>
  );
}

/** 日成本柱状图：用 div 高度做图，不用 SVG —— 窄屏下宽度自适应不需要重算坐标 */
function CostChart({ series }: { series: DailyCostPoint[] }) {
  const max = Math.max(...series.map((point) => point.cost_cny), 0.0001);
  const first = series[0];
  const middle = series[Math.floor(series.length / 2)];
  const last = series[series.length - 1];

  return (
    <div className={styles.chartWrap}>
      <div className={styles.chart} role="img" aria-label="每日 LLM 花费柱状图">
        {series.map((point) => {
          // 有值至少 3%（否则小额日看起来像「没数据」）；0 值画一根 2px 的浅色短柱，
          // 让「这天没有调用」和「图表没渲染出来」在视觉上可区分
          const height = point.cost_cny > 0 ? Math.max(3, (point.cost_cny / max) * 100) : 1.6;
          return (
            <div key={point.date} className={styles.barCol}>
              <div
                className={clsx(styles.barFill, point.cost_cny === 0 && styles.barEmpty)}
                style={{ height: `${height}%` }}
                title={`${point.date}｜${money(point.cost_cny)}｜${point.calls} 次调用｜${compactTokens(point.tokens)} token`}
              />
            </div>
          );
        })}
      </div>
      {/* 【为什么轴只给三个刻度】柱子上逐根挂日期，在 390px 宽 × 30 根柱时必然叠字；
          改成「首 / 中 / 末」三个刻度，任何宽度、任何窗口长度都不会挤压。
          想看某一天的具体数字，桌面端悬停柱子有 title，移动端看下方用途拆分的量级即可。 */}
      <div className={styles.axis}>
        <span>{first ? shortDate(first.date) : ''}</span>
        <span>{middle ? shortDate(middle.date) : ''}</span>
        <span>{last ? shortDate(last.date) : ''}</span>
      </div>
    </div>
  );
}

/**
 * LLM 成本面板（阶段 7）。
 *
 * 【为什么放在管理后台、和功能开关同一页】两者回答同一个问题的两半：
 * 「现在花了多少、接下来会花多少」（面板）与「要不要立刻停掉某个花钱的功能」（开关）。
 * 分开会让管理员在两个标签页之间对照，止血时最容易关错开关。
 *
 * 【为什么预测只展示不解释公式】面板上给出 R² 与样本数，低可信度时明确写
 * 「趋势不稳」——比给一个精确到分、却没人知道怎么来的数字诚实。
 */
export function CostPanel() {
  const [days, setDays] = useState<number>(14);
  const usage = useLlmUsage(days);

  const data = usage.data;
  const totals = data?.totals;
  const forecast = data?.forecast;

  const today = useMemo(() => data?.series[data.series.length - 1], [data]);
  const budgetDays = forecast?.days_until_budget ?? null;
  const maxPurpose = useMemo(
    () => Math.max(...(data?.by_purpose.map((item) => item.cost_cny) ?? [0]), 0.0001),
    [data],
  );

  return (
    <section className={styles.section}>
      <header className={styles.head}>
        <div>
          <h2 className={styles.title}>LLM 成本</h2>
          <p className={styles.hint}>
            每次真实 LLM 调用都会记一行账（模型、token、耗时、金额）。金额按
            <strong>调用当时</strong>
            的单价写入，上游调价不会改写历史。
          </p>
        </div>
        <div className={styles.actions}>
          <div className={styles.windowGroup} role="group" aria-label="统计窗口">
            {WINDOWS.map((value) => (
              <button
                key={value}
                type="button"
                className={clsx(styles.windowBtn, days === value && styles.windowActive)}
                onClick={() => setDays(value)}
              >
                {value} 天
              </button>
            ))}
          </div>
          <button
            type="button"
            className="mini-btn"
            onClick={() => void usage.refetch()}
            disabled={usage.isFetching}
          >
            {usage.isFetching ? '刷新中…' : '刷新'}
          </button>
        </div>
      </header>

      {usage.isPending && <div className={styles.placeholder}>正在汇总账本…</div>}

      {usage.isError && (
        <div className={styles.notice}>
          <div className={styles.noticeTitle}>读取失败</div>
          <p className={styles.noticeBody}>{errorText(usage.error)}</p>
        </div>
      )}

      {data && totals && forecast && (
        <>
          <div className={styles.kpiGrid}>
            <Kpi
              label="今日花费"
              value={money(today?.cost_cny ?? 0)}
              hint={`${today?.calls ?? 0} 次调用 · ${compactTokens(today?.tokens ?? 0)} token`}
            />
            <Kpi
              label={`近 ${data.window_days} 天合计`}
              value={money(totals.cost_cny)}
              hint={`${totals.calls} 次 · 失败 ${totals.calls - totals.success_calls} 次 · 平均 ${totals.avg_latency_ms}ms`}
            />
            <Kpi
              label="未来 7 天预测"
              value={money(forecast.next_7d_cny)}
              hint={`日均 ${money(forecast.daily_avg_cny)} · ${CONFIDENCE_TEXT[forecast.confidence] ?? forecast.confidence}`}
              emphasis
            />
            <Kpi
              label="本月预测"
              value={money(forecast.month_projection_cny)}
              hint={
                forecast.budget_cny > 0
                  ? `已花 ${money(forecast.mtd_cny)} / 预算 ${money(forecast.budget_cny)}`
                  : `已花 ${money(forecast.mtd_cny)} · 未设预算`
              }
            />
          </div>

          {forecast.budget_cny > 0 && (
            <div className={styles.budgetRow}>
              <div className={styles.budgetTrack}>
                <div
                  className={clsx(
                    styles.budgetFill,
                    (forecast.budget_used_ratio ?? 0) >= 0.9 && styles.budgetDanger,
                  )}
                  style={{ width: `${Math.min(100, (forecast.budget_used_ratio ?? 0) * 100)}%` }}
                />
              </div>
              <span className={styles.budgetText}>
                预算已用 {Math.round((forecast.budget_used_ratio ?? 0) * 100)}%
                {budgetDays === null
                  ? ' · 暂无花费可外推'
                  : budgetDays > 365
                    ? ' · 预算远未构成约束'
                    : ` · 按当前速度还能撑 ${budgetDays} 天`}
              </span>
            </div>
          )}

          <div className={styles.chartBlock}>
            <div className={styles.blockHead}>
              <span className={styles.blockTitle}>每日花费</span>
              <span className={styles.blockMeta}>
                峰值 {money(Math.max(...data.series.map((point) => point.cost_cny), 0))}
              </span>
            </div>
            <CostChart series={data.series} />
          </div>

          <div className={styles.splitGrid}>
            <div className={styles.block}>
              <div className={styles.blockHead}>
                <span className={styles.blockTitle}>花在哪：按用途</span>
                <span className={styles.blockMeta}>{data.by_purpose.length} 类</span>
              </div>
              {data.by_purpose.length === 0 && (
                <div className={styles.placeholder}>窗口内没有调用记录。</div>
              )}
              <ul className={styles.purposeList}>
                {data.by_purpose.map((item) => (
                  <li key={item.purpose} className={styles.purposeRow}>
                    <div className={styles.purposeTop}>
                      <span className={styles.purposeLabel}>{item.label}</span>
                      <span className={styles.purposeCost}>{money(item.cost_cny)}</span>
                    </div>
                    <div className={styles.purposeTrack}>
                      <div
                        className={styles.purposeFill}
                        style={{ width: `${Math.max(2, (item.cost_cny / maxPurpose) * 100)}%` }}
                      />
                    </div>
                    <div className={styles.purposeMeta}>
                      {item.calls} 次 · 均 {money(item.avg_cost_cny)} · 占{' '}
                      {Math.round(item.share * 100)}%
                    </div>
                  </li>
                ))}
              </ul>
            </div>

            <div className={styles.block}>
              <div className={styles.blockHead}>
                <span className={styles.blockTitle}>用的是什么：按模型</span>
                <span className={styles.blockMeta}>
                  合计 {compactTokens(totals.total_tokens)} token
                </span>
              </div>
              {data.by_model.length === 0 && (
                <div className={styles.placeholder}>窗口内没有调用记录。</div>
              )}
              <ul className={styles.modelList}>
                {data.by_model.map((item) => (
                  <li key={item.model} className={styles.modelRow}>
                    <div className={styles.modelMain}>
                      <code className={styles.modelName}>{item.model}</code>
                      <span className={styles.modelProvider}>{item.provider}</span>
                      {!item.price_known && (
                        <span className={styles.modelWarn}>按兜底价估算</span>
                      )}
                    </div>
                    <div className={styles.modelSide}>
                      <span className={styles.modelCost}>{money(item.cost_cny)}</span>
                      <span className={styles.modelCalls}>{item.calls} 次</span>
                    </div>
                  </li>
                ))}
              </ul>
              <p className={styles.blockFoot}>
                平均 {Math.round(totals.total_tokens / Math.max(1, totals.calls))} token/次 ·
                成功率 {Math.round(totals.success_rate * 100)}%
                {totals.estimated_calls > 0
                  ? ` · ${totals.estimated_calls} 次为估算用量`
                  : ''}
              </p>
            </div>
          </div>

          <div className={styles.forecastBox}>
            <div className={styles.forecastHead}>
              <span className={styles.blockTitle}>怎么算出「未来 7 天」</span>
              <span
                className={clsx(
                  styles.confidence,
                  forecast.confidence === 'low' && styles.confidenceWarn,
                  forecast.confidence === 'insufficient' && styles.confidenceWarn,
                )}
              >
                {CONFIDENCE_TEXT[forecast.confidence] ?? forecast.confidence} · R² {forecast.r2}
              </span>
            </div>
            <p className={styles.forecastBody}>
              用最近 {forecast.samples} 天的日花费做最小二乘线性回归，按拟合出的斜率外推
              7 天（负值裁剪到 0）。日变化 {forecast.slope_cny_per_day >= 0 ? '+' : ''}
              {money(forecast.slope_cny_per_day)}，本月剩余 {forecast.month_remaining_days} 天。
            </p>
            {(data.notes?.length ?? 0) > 0 && (
              <ul className={styles.notes}>
                {(data.notes ?? []).map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            )}
            <p className={styles.disclaimer}>{data.price_table_note}</p>
          </div>
        </>
      )}
    </section>
  );
}
