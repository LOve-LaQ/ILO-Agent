import type { ComponentProps } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize from 'rehype-sanitize';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';

import { markdownHtmlSchema } from './markdownHtmlSchema';

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
 * 【为什么要放开原始 HTML】仓库 README 里满是 `<p align="center">`、shields.io 徽章、
 * `<details>/<summary>` 折叠块、`<picture>` 响应式图。只挂 remark 插件时 react-markdown
 * 不做 HTML 解析，这些标签会被当**纯文本**原样吐出来——抽屉里的「原文快照」出现过
 * 一整坨 `<a href="..."><img src="...shields.io..."></a>` 直接显示给用户。
 *
 * 【为什么不只是加 rehype-raw】这段内容是任意第三方仓库的 README，不受我们控制。
 * rehype-raw 会连 `<script>`、`onerror=`、`javascript:` 一起放进来，等于把 XSS 入口开到
 * 内容区。所以必须紧跟 rehype-sanitize，白名单见 `./markdownHtmlSchema`。
 */

/**
 * Markdown 渲染，替代原 index.html 的手写 formatMessage 正则。
 * - remark-gfm：表格 / 任务列表 / 自动链接等 GFM 扩展
 * - remark-breaks：复现原实现「单个换行 => <br>」的行为
 * - rehype-raw → rehype-sanitize：解析并白名单过滤内联 HTML（顺序不能反）
 *
 * 【为什么放共享 UI 层】学习抽屉的对话气泡与「我的 → 学习记录」里的对话回看都需要
 * 渲染同一批模型输出（同一份 `chat_messages`），两处必须长成一样；放在某个 feature 里
 * 就会被另一个 feature 反向依赖。
 */
export function MarkdownBody({ content, allowImages = true }: MarkdownBodyProps) {
  // 关掉图片时连 `<picture>` 一起去掉：它本身不渲染内容，留着只会剩下空的 source 节点
  const components = allowImages
    ? undefined
    : ({ img: () => null, picture: () => null } satisfies ComponentProps<
        typeof ReactMarkdown
      >['components']);

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkBreaks]}
      rehypePlugins={[rehypeRaw, [rehypeSanitize, markdownHtmlSchema]]}
      components={components}
    >
      {content}
    </ReactMarkdown>
  );
}
