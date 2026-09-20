import { useEffect, useState } from 'react';

/**
 * 抓取进度阶段轮播
 * 抽自原 index.html refreshNews() 中的 setInterval 逻辑：
 * 运行期间每 intervalMs 前进一个阶段，停在最后一个阶段不越界。
 */
export function useStagedProgress(stageCount: number, running: boolean, intervalMs = 1400): number {
  const [stage, setStage] = useState(0);

  useEffect(() => {
    if (!running) {
      return;
    }

    setStage(0);
    const timer = setInterval(() => {
      setStage((prev) => Math.min(prev + 1, stageCount - 1));
    }, intervalMs);

    return () => clearInterval(timer);
  }, [running, stageCount, intervalMs]);

  return stage;
}
