import { api } from './client';
import type { CardContentResponse } from '../types/card';

/**
 * 卡片原文（仓库 README）快照 —— 「先读原文」页的数据来源。
 *
 * 【为什么放 shared/api 而不是 discovery/learning 各自的 feature】
 * 端点在 /discover 下，但消费方是 learning；feature 之间不允许互相 import
 * （边界靠组合根注入，见 routes.tsx），所以跨 feature 共用的接口一律放这里，
 * 与 me.ts 同类。
 *
 * 后端保证「总是给得出一个响应」，前端不需要处理 404：
 * - origin='snapshot'    本地已有快照
 * - origin='on_demand'   本次实时补抓并已回写，下次即变 snapshot
 * - origin='unavailable' 抓不到原文，退回 fallback_description 展示
 */
export function fetchCardContent(cardId: string): Promise<CardContentResponse> {
  return api.get<CardContentResponse>(`/discover/cards/${encodeURIComponent(cardId)}/content`);
}
