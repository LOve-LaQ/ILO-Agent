import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../shared/api/client';
import { useToastStore } from '../../../components/ui/toastStore';
import type { CardTab, TaskProgressResponse } from '../../../shared/types/card';
import { fetchTaskProgress, refreshCards } from '../api';
import { useDiscoveryStore } from '../store';
import { cardsQueryKey } from './useCards';

/** 进度遮罩最短展示时长，与原 index.html 的 900ms 下限一致（避免一闪而过） */
const MIN_OVERLAY_MS = 900;

/** 轮询间隔：一次抓取 1~3 分钟，1 秒一次的请求量可以忽略，但换来连续的进度 */
const POLL_INTERVAL_MS = 1000;

/** 轮询总超时：正常情况下后端最多 3 分钟。超过这个时长还没终态就是异常，
 *  不能无限轮询把用户钉在遮罩上。 */
const POLL_TIMEOUT_MS = 6 * 60 * 1000;

/** 失败提示的展示时长：比默认的 2.2s 长，让用户来得及读完条数 */
const FAILURE_TOAST_MS = 6000;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 从 409 的 detail 里取出正在跑的任务号 */
function runningTaskIdOf(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.code !== 'TASK_ALREADY_RUNNING') {
    return null;
  }
  const detail = error.detail as { task_id?: unknown } | null | undefined;
  return typeof detail?.task_id === 'string' ? detail.task_id : null;
}

export interface RefreshCardsController {
  refreshing: boolean;
  /** 当前任务快照；请求刚发出、还没拿到第一次进度时为 null */
  progress: TaskProgressResponse | null;
  run: () => Promise<void>;
}

/**
 * 「抓取最新」流程编排（异步任务版）
 *
 * 之前的实现是在客户端用 useStagedProgress 轮播四句文案，1.4 秒一句、5.6 秒
 * 走完，之后一直停在「正在整理技术卡片…」—— 与后端真实进度毫无关系，用户
 * 看到进度条不动只能怀疑卡死。现在改为提交任务后轮询 GET /discover/tasks/{id}，
 * 阶段、已处理条数、耗时全部来自服务端。
 *
 * 并发触发（409 TASK_ALREADY_RUNNING）不当错误处理：直接接管正在跑的那个任务
 * 的进度，用户看到的是"我点的这次其实已经在跑了"，而不是一句红字报错。
 */
export function useRefreshCards(tab: CardTab): RefreshCardsController {
  const timeRange = useDiscoveryStore((state) => state.timeRange);
  const showToast = useToastStore((state) => state.show);
  const queryClient = useQueryClient();

  const [refreshing, setRefreshing] = useState(false);
  const [progress, setProgress] = useState<TaskProgressResponse | null>(null);

  // 组件卸载后继续轮询只会白白发请求，还可能在卸载的组件上 setState。
  // **必须在 effect 里显式复位**：React StrictMode 开发模式下会「挂载→卸载→再挂载」，
  // 而 ref 对象跨这两次挂载是同一个 —— 只在 cleanup 里置 true 会让它永久为 true，
  // 后续所有进度更新都被静默丢掉（表现为遮罩永远停在「正在提交任务…」）。
  const unmountedRef = useRef(false);
  useEffect(() => {
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
    };
  }, []);

  const run = useCallback(async () => {
    if (refreshing) {
      return;
    }
    setRefreshing(true);
    setProgress(null);

    const startedAt = Date.now();

    try {
      let taskId: string;
      try {
        const accepted = await refreshCards(tab, timeRange);
        taskId = accepted.task_id;
      } catch (error) {
        const runningId = runningTaskIdOf(error);
        if (!runningId) {
          throw error;
        }
        // 已有任务在跑：接管它的进度，而不是让用户对着报错发呆
        taskId = runningId;
        showToast('已有一个抓取任务在跑，正在显示它的进度');
      }

      let final = await pollUntilDone(taskId, (snapshot) => {
        if (!unmountedRef.current) {
          setProgress(snapshot);
        }
      });

      // progress_lost=true 意味着进程重启过，过程看不到但结局可信
      if (final.progress_lost) {
        showToast('服务重启过，任务已结束（过程进度不可得）', FAILURE_TOAST_MS);
      }
      reportResult(final, showToast);
    } catch (error) {
      console.warn('抓取失败:', error);
      const message =
        error instanceof Error && error.message
          ? error.message
          : '抓取失败，已展示当前内容';
      showToast(`⚠️ ${message}`, FAILURE_TOAST_MS);
    }

    await queryClient.invalidateQueries({ queryKey: cardsQueryKey(tab) });

    const rest = MIN_OVERLAY_MS - (Date.now() - startedAt);
    if (rest > 0) {
      await delay(rest);
    }

    if (!unmountedRef.current) {
      setProgress(null);
    }
    setRefreshing(false);
  }, [refreshing, tab, timeRange, showToast, queryClient]);

  return { refreshing, progress, run };
}

/** 轮询到终态；超时抛错，不让用户无限期停在遮罩上 */
async function pollUntilDone(
  taskId: string,
  onUpdate: (snapshot: TaskProgressResponse) => void,
): Promise<TaskProgressResponse> {
  const startedAt = Date.now();

  for (;;) {
    const snapshot = await fetchTaskProgress(taskId);
    onUpdate(snapshot);

    if (snapshot.status !== 'running') {
      return snapshot;
    }
    if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
      throw new Error('任务超过 6 分钟仍未结束，请稍后刷新页面查看结果');
    }
    await delay(POLL_INTERVAL_MS);
  }
}

/** 四态如实区分：partial / failed 都不能显示成绿色成功提示 */
function reportResult(
  result: TaskProgressResponse,
  showToast: (message: string, duration?: number) => void,
): void {
  switch (result.status) {
    case 'succeeded':
      showToast(`✅ 新增 ${result.new_count} 条，跳过 ${result.skipped_count} 条重复`);
      break;
    case 'partial':
      showToast(
        `⚠️ 新增 ${result.new_count} 条，另有 ${result.failed_count} 条未能入库`,
        FAILURE_TOAST_MS,
      );
      break;
    case 'failed':
      showToast(
        `⚠️ ${result.error || '本次抓取失败，请查看服务端日志'}`,
        FAILURE_TOAST_MS,
      );
      break;
    default:
      showToast(result.message || '本次没有抓到新内容');
  }
}
