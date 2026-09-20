import { api } from '../../shared/api/client';
import type { components } from '../../shared/api/schema';
import type { TechCard } from '../../shared/types/card';

// 【契约边界】以下类型均由后端 OpenAPI 派生，勿手写字段。
// 权威定义见 backend/src/schemas/learning.py。

/** POST /learning/session 响应 */
export type LearningSessionResponse = components['schemas']['SessionCreateResponse'];

/** POST /learning/chat 响应 */
export type LearningChatResponse = components['schemas']['ChatResponse'];

/** 传给后端的对话历史轮次（仅 user / assistant） */
export type ChatTurn = components['schemas']['ChatMessage'];

/**
 * POST /learning/session
 *
 * 请求体不含 user_id：阶段 2 起用户身份一律由后端从 access token 的 `sub` 取，
 * 请求体里的 user_id 不再被信任（权限边界的核心约束）。
 * 未登录调用会返回 401，客户端会自动引导到登录页。
 */
export function createLearningSession(card: TechCard): Promise<LearningSessionResponse> {
  return api.post<LearningSessionResponse>('/learning/session', {
    news_item_id: card.id,
    title: card.title,
    summary: card.summary,
    core_concepts: card.core_concepts ?? [],
    time_budget: 15,
    preferred_depth: 'medium',
  });
}

/** POST /learning/chat */
export function sendLearningMessage(
  sessionId: string,
  message: string,
  history: ChatTurn[],
): Promise<LearningChatResponse> {
  return api.post<LearningChatResponse>('/learning/chat', {
    session_id: sessionId,
    message,
    conversation_history: history.slice(-10),
  });
}
