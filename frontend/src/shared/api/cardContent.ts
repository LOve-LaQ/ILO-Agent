import { api } from './client';
import type {
  CardContentResponse,
  CardDigestResponse,
  SimilarResponse,
} from '../types/card';

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

/**
 * 卡片中文导读（无中文 README 时的兜底）—— 「先读原文」页里可切到的一层中文概览。
 *
 * 【为什么与 fetchCardContent 分开】两者成本量级不同：原文是公开数据直取；
 * 导读未命中缓存时后端会真实调用一次 LLM，所以它要求登录、并单独限流。
 * 前端据此把「取不到」当作可降级情形处理（401 引导登录，其余退回原文视图），
 * 而不是把整条阅读链路卡死。
 *
 * 后端始终返回 200（用 origin/reason 表达状态），故这里不需要处理业务 404：
 * - origin='cache'/'generated'  有中文导读可展示
 * - origin='unavailable'        生成不了，看 reason 并退回 fallback_description
 */
export function fetchCardDigest(cardId: string): Promise<CardDigestResponse> {
  return api.get<CardDigestResponse>(`/discover/cards/${encodeURIComponent(cardId)}/digest`);
}

/**
 * 相似项目（向量近邻）—— 「看完这个，接着该看什么」。
 *
 * 后端始终返回 200，用 `based_on` 表达可用性：
 * - based_on='vector'       正常召回（items 可能为空：语义上没有足够相近的项目）
 * - based_on='unavailable'  源卡片没有入库向量 / 向量库不可用 → 整块不显示
 *
 * 【为什么前端要把空列表也当正常态】相似推荐是「顺带看一眼」的增强，
 * 不是主链路。它拿不到结果时最差的行为是「这块什么都没有」，
 * 绝不该让整个阅读抽屉报错。
 */
export function fetchSimilarCards(
  cardId: string,
  limit: number = 5,
): Promise<SimilarResponse> {
  return api.get<SimilarResponse>(
    `/discover/cards/${encodeURIComponent(cardId)}/similar?limit=${limit}`,
  );
}
