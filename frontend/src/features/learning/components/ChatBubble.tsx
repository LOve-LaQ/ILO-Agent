import clsx from 'clsx';

import { MarkdownBody } from '../../../components/ui/MarkdownBody';
import styles from './ChatBubble.module.css';

interface ChatBubbleProps {
  role: 'user' | 'assistant';
  content: string;
  /** 本地提示消息（非模型输出） */
  error?: boolean;
}

export function ChatBubble({ role, content, error }: ChatBubbleProps) {
  return (
    <div className={clsx(styles.bubble, role === 'user' ? styles.me : styles.ai, error && styles.error)}>
      <MarkdownBody content={content} />
    </div>
  );
}

/** 等待模型返回时的打字气泡 */
export function TypingBubble() {
  return (
    <div className={clsx(styles.bubble, styles.ai)}>
      <span className={styles.typing}>
        <i />
        <i />
        <i />
      </span>
    </div>
  );
}
