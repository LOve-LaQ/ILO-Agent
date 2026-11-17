import type { ComponentProps } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize from 'rehype-sanitize';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';
import type { PluggableList } from 'unified';

import { looksLikeHtmlBlock, markdownHtmlSchema } from './markdownHtmlSchema';

export interface MarkdownBodyProps {
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
 * 【为什么要碰原始 HTML】仓库 README 里满是 `<p align="center">`、shields.io 徽章、
 * `<details>/<summary>` 折叠块、`<picture>` 响应式图。只挂 remark 插件时 react-markdown
 * 不做 HTML 解析，这些标签会被当**纯文本**原样吐出来——抽屉里的「原文快照」出现过
 * 一整坨 `<a href="..."><img src="...shields.io..."></a>` 直接显示给用户。
 *
 * 【为什么 raw 与 sanitize 必须成对】这段内容是任意第三方仓库的 README，不受我们控制。
 * rehype-raw 会连 `<script>`、`onerror=`、`javascript:` 一起放进来，等于把 XSS 入口开到
 * 内容区。白名单见 `./markdownHtmlSchema`，`npm run check:sanitize` 守着这条约束。
 *
 * 【为什么独立成文件】react-markdown + unified 全家桶是前端最大的一块依赖。放在这里
 * 由 `MarkdownBody` 门面用 `React.lazy` 动态加载，静态 import 链在此断开，首屏不再
 * modulepreload 这 96KB。改动实现时请勿把它挪回门面文件。
 */
export default function MarkdownBodyImpl({ content, allowImages = true }: MarkdownBodyProps) {
  // 关掉图片时连 `<picture>` 一起去掉：它本身不渲染内容，留着只会剩下空的 source 节点
  const components = allowImages
    ? undefined
    : ({ img: () => null, picture: () => null } satisfies ComponentProps<
        typeof ReactMarkdown
      >['components']);

  // 挂上 rehype-raw 的代价见 `looksLikeHtmlBlock`：任何 `<xxx>` 都会被当标签，
  // 所以只在确实有 README 块级 HTML 时才开，避免吃掉 `vector<int>` 这类正文。
  //
  // sanitize 必须跟 raw **同时**出现，不能单独挂：它会把尚未解析的 raw 节点
  // （也就是 react-markdown 默认当纯文本输出的那些 `<int>`）整块删掉。
  const rehypePlugins: PluggableList = looksLikeHtmlBlock(content)
    ? [rehypeRaw, [rehypeSanitize, markdownHtmlSchema]]
    : [];

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkBreaks]}
      rehypePlugins={rehypePlugins}
      components={components}
    >
      {content}
    </ReactMarkdown>
  );
}
