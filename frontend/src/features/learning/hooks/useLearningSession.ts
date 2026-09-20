import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useRef } from 'react';

import { useToastStore } from '../../../components/ui/toastStore';
import { useAccessToken } from '../../../shared/api/authToken';
import { ApiError } from '../../../shared/api/client';
import {
  fetchSessionDetail,
  fetchSessions,
  meKeys,
  type SessionMessage,
} from '../../../shared/api/me';
import type { TechCard } from '../../../shared/types/card';
import { createLearningSession, sendLearningMessage, type ChatTurn } from '../api';
import { buildExplanation } from '../explanation';
import { useLearningStore, type ChatMessage } from '../store';

/**
 * 学习会话编排
 *
 * 从原 index.html 的 startLearning / startChatting / sendMessage / persistSession /
 * restoreSession 迁移而来。与原实现的两处关键差异：
 * - 删除前端硬编码的 fallbackAnswer（假回答），失败时推入显式的错误提示消息
 * - **不再用 localStorage 假装持久化**：对话与进度本来就已经由后端写进
 *   `chat_messages` / `learning_sessions`（`/learning/chat` 每轮都落库），
 *   刷新后从「我的 → 学习记录」回看即可；`restore()` 也改为读服务端而不是读浏览器，
 *   这样换设备、换浏览器都还在，也不再受浏览器清理数据的影响。
 */

/** 服务端一条对话 → 前端气泡；时间戳取 created_at，保证回看顺序与落库一致 */
function toChatMessage(message: SessionMessage): ChatMessage {
  const at = new Date(message.created_at).getTime();
  return {
    role: message.role,
    content: message.content,
    timestamp: Number.isNaN(at) ? Date.now() : at,
  };
}

export function useLearningSession() {
  const open = useLearningStore((s) => s.open);
  const state = useLearningStore((s) => s.state);
  const sessionId = useLearningStore((s) => s.sessionId);
  const card = useLearningStore((s) => s.card);
  const topic = useLearningStore((s) => s.topic);
  const explanation = useLearningStore((s) => s.explanation);
  const messages = useLearningStore((s) => s.messages);
  const pending = useLearningStore((s) => s.pending);
  const question = useLearningStore((s) => s.question);
  const setQuestion = useLearningStore((s) => s.setQuestion);
  const showToast = useToastStore((s) => s.show);

  const queryClient = useQueryClient();
  const token = useAccessToken();
  /** 同一个登录态只尝试恢复一次，避免每次重渲染都去开抽屉 */
  const restoredFor = useRef<string | null>(null);

  /** 打开抽屉并创建学习会话 */
  const startLearning = useCallback(
    async (item: TechCard) => {
      const store = useLearningStore.getState();
      store.setCard(item);
      store.setTopic(item.title);
      store.setOpen(true);
      store.setState('loading');
      store.setMessages([]);
      store.setPending(false);
      store.setQuestion('');

      let createdSessionId: string | null = null;
      try {
        const data = await createLearningSession(item);
        createdSessionId = data.session_id;
      } catch (error) {
        // 未登录：后端 /learning/* 一律 401，此刻客户端已把用户引导去登录页。
        // 这种情况绝不能退回「本地卡片要点讲解」——那等于假装成功，误导用户。
        if (error instanceof ApiError && error.status === 401) {
          store.setOpen(false);
          store.setState('idle');
          store.setSessionId(null);
          showToast('请先登录再开始学习');
          return;
        }
        console.warn('创建学习会话失败，已回退为本地卡片要点讲解', error);
        showToast('⚠️ 会话创建失败，已使用本地卡片要点讲解');
      }

      store.setSessionId(createdSessionId);
      store.setExplanation(buildExplanation(item));
      store.setState('explanation');
    },
    [showToast],
  );

  /** 从讲解态进入问答态 */
  const startChatting = useCallback(() => {
    useLearningStore.getState().setState('chatting');
  }, []);

  const send = useCallback(async () => {
    const store = useLearningStore.getState();
    const q = store.question.trim();
    if (!q || store.pending) return;

    store.setQuestion('');
    store.setPending(true);
    store.appendMessage({ role: 'user', content: q, timestamp: Date.now() });

    try {
      if (!store.sessionId) {
        throw new Error('学习会话未建立');
      }
      const history: ChatTurn[] = useLearningStore
        .getState()
        .messages.map((message) => ({ role: message.role, content: message.content }));
      const data = await sendLearningMessage(store.sessionId, q, history);
      store.appendMessage({ role: 'assistant', content: data.response, timestamp: Date.now() });
      // 这轮对话已落库，让「学习记录」立刻能看到新内容
      void queryClient.invalidateQueries({ queryKey: meKeys.all });
    } catch (error) {
      console.warn('AI 回答失败:', error);
      store.appendMessage({
        role: 'assistant',
        content: '⚠️ 回答失败：暂时无法连接讲解服务，请稍后重试。',
        timestamp: Date.now(),
        error: true,
      });
    } finally {
      store.setPending(false);
    }
  }, [queryClient]);

  /** 关闭抽屉（保留会话数据，与原行为一致） */
  const close = useCallback(() => {
    const store = useLearningStore.getState();
    store.setOpen(false);
    store.setState('idle');
  }, []);

  /**
   * 恢复上一次未完成的学习会话（刷新页面 / 换设备后都能续上）。
   *
   * 只恢复**未完成**的最近一次会话：已完成的会话属于「回看」，那是「我的 → 学习记录」
   * 的职责，没必要每次打开页面都把抽屉弹出来。
   *
   * 讲解文本不在恢复范围内 —— 它是本地模板产物（`buildExplanation`），后端只存对话与
   * 进度，所以从服务端续上时直接进问答态，不伪造一段讲解气泡。
   */
  const restore = useCallback(async () => {
    if (!token || restoredFor.current === token) return;
    restoredFor.current = token;

    const store = useLearningStore.getState();
    if (store.open) return;

    try {
      const list = await queryClient.fetchQuery({
        queryKey: meKeys.sessions(),
        queryFn: () => fetchSessions({ limit: 1 }),
      });
      const latest = list.items[0];
      if (!latest || latest.state === 'completed') return;

      const detail = await queryClient.fetchQuery({
        queryKey: meKeys.session(latest.session_id),
        queryFn: () => fetchSessionDetail(latest.session_id),
      });
      if (detail.messages.length === 0) return;

      // 请求回来时用户可能已经自己打开了别的会话，别把人家正在看的覆盖掉
      if (useLearningStore.getState().open) return;

      store.setSessionId(latest.session_id);
      store.setTopic(latest.topic);
      store.setCard(null);
      store.setExplanation('');
      store.setMessages(detail.messages.map(toChatMessage));
      store.setState('chatting');
      store.setOpen(true);
    } catch {
      /* 恢复只是锦上添花：失败就安静地不恢复，不打断用户 */
    }
  }, [token, queryClient]);

  return {
    open,
    state,
    sessionId,
    card,
    topic,
    explanation,
    messages,
    pending,
    question,
    setQuestion,
    startLearning,
    startChatting,
    send,
    close,
    restore,
  };
}
