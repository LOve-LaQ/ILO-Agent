import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';

interface MarkdownBodyProps {
  content: string;
  /**
   * 是否渲染远程图片。
   *
   * 阅读仓库 README 时必须关掉：外部图片会带第三方请求（追踪像素）并拖慢渲染，
   * 而这段内容完全不受我们控制。对话气泡等自有内容保持默认开启。
   */
  allowImages?: boolean;
}

/**
 * Markdown 渲染，替代原 index.html 的手写 formatMessage 正则。
 * - remark-gfm：表格 / 任务列表 / 自动链接等 GFM 扩展
 * - remark-breaks：复现原实现「单个换行 => <br>」的行为
 *
 * 【为什么放共享 UI 层】学习抽屉的对话气泡与「我的 → 学习记录」里的对话回看都需要
 * 渲染同一批模型输出（同一份 `chat_messages`），两处必须长成一样；放在某个 feature 里
 * 就会被另一个 feature 反向依赖。
 */
export function MarkdownBody({ content, allowImages = true }: MarkdownBodyProps) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkBreaks]}
      components={allowImages ? undefined : { img: () => null }}
    >
      {content}
    </ReactMarkdown>
  );
}
