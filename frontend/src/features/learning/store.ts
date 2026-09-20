import { create } from 'zustand';

import { onSessionReset } from '../../shared/lib/sessionBus';
import type { CardContentResponse, TechCard } from '../../shared/types/card';

export type LearningState = 'idle' | 'loading' | 'reading' | 'explanation' | 'chatting';

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  /** 本地提示消息（请求失败），并非模型输出 */
  error?: boolean;
}

interface LearningStoreState {
  /** 抽屉是否可见 */
  open: boolean;
  /**
   * 五态：reading（先读原文）/ loading（创建会话中）/ explanation（卡片要点）
   * / chatting（问答）/ idle（关闭）
   */
  state: LearningState;
  sessionId: string | null;
  card: TechCard | null;
  /** 原文快照（reading 态展示）；会话创建被延后，所以它早于 sessionId 存在 */
  content: CardContentResponse | null;
  /**
   * 会话主题。
   *
   * 从服务端恢复历史会话时未必拿得到卡片（卡片可能已不在知识库里），
   * 所以标题不能只依赖 `card.title`，得有一份独立于卡片的主题。
   */
  topic: string;
  explanation: string;
  messages: ChatMessage[];
  /** 是否正在等待模型回复 */
  pending: boolean;
  /** 输入框内容 */
  question: string;

  setOpen: (open: boolean) => void;
  setState: (state: LearningState) => void;
  setSessionId: (sessionId: string | null) => void;
  setCard: (card: TechCard | null) => void;
  setContent: (content: CardContentResponse | null) => void;
  setTopic: (topic: string) => void;
  setExplanation: (explanation: string) => void;
  setMessages: (messages: ChatMessage[]) => void;
  appendMessage: (message: ChatMessage) => void;
  setPending: (pending: boolean) => void;
  setQuestion: (question: string) => void;
  reset: () => void;
}

const INITIAL_STATE = {
  open: false,
  state: 'idle' as LearningState,
  sessionId: null,
  card: null,
  content: null,
  topic: '',
  explanation: '',
  messages: [] as ChatMessage[],
  pending: false,
  question: '',
};

/** 学习会话抽屉状态 */
export const useLearningStore = create<LearningStoreState>((set) => ({
  ...INITIAL_STATE,

  setOpen: (open) => set({ open }),
  setState: (state) => set({ state }),
  setSessionId: (sessionId) => set({ sessionId }),
  setCard: (card) => set({ card }),
  setContent: (content) => set({ content }),
  setTopic: (topic) => set({ topic }),
  setExplanation: (explanation) => set({ explanation }),
  setMessages: (messages) => set({ messages }),
  appendMessage: (message) => set((prev) => ({ messages: [...prev.messages, message] })),
  setPending: (pending) => set({ pending }),
  setQuestion: (question) => set({ question }),
  reset: () => set(INITIAL_STATE),
}));

/**
 * 订阅「会话已重置」（登出 / 换账号）：丢弃上一个账号的学习会话。
 *
 * 【为什么在 store 模块里订阅】
 * auth 视图需要清掉的是本地抽屉状态（开着的话题、对话消息），而 auth 不得 import
 * learning。这里在模块加载期挂到 shared 总线上，由 learning 自己清理自己。
 * 该模块被 useLearningSession 引入，随路由一起加载，订阅在整个生命周期内有效。
 */
onSessionReset(() => {
  useLearningStore.getState().reset();
});
