import { defaultSchema } from 'rehype-sanitize';

/**
 * 渲染第三方 Markdown 时的 HTML 白名单（GitHub defaultSchema + 少量放开）。
 *
 * 单独成文件是为了让 `scripts/check-markdown-sanitize.mjs` 能直接 import 这一份，
 * 而不是在守卫脚本里再抄一遍 —— 抄一份就等于有了两个真相，改一处忘一处。
 *
 * 放开的三类：
 * - `align`：README 里 `<p align="center">` / `<img align="left">` 极常见
 * - `details` / `summary`：可折叠段落
 * - `script` / `style` 进 strip：白名单外的元素默认「删标签留内容」，
 *   脚本必须连内容一起删，否则 `alert(1)` 会以纯文本掉进正文
 */
export const markdownHtmlSchema = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames ?? []), 'details', 'summary'],
  strip: [...(defaultSchema.strip ?? []), 'script', 'style'],
  attributes: {
    ...defaultSchema.attributes,
    '*': [...(defaultSchema.attributes?.['*'] ?? []), 'align'],
    img: [
      ...(defaultSchema.attributes?.img ?? []),
      'align',
      'width',
      'height',
      'loading',
    ],
  },
};

/**
 * README 里常见的**块级** HTML 标签。
 *
 * 【为什么不是「有 `<` 就开 HTML 解析」】开了 rehype-raw 之后，CommonMark 会把任何
 * `<xxx>` 当标签：`vector<int>`、`List<T>`、`<Component prop={x} />` 里的尖括号部分
 * 会被解析成未知元素，再被 sanitize 白名单删掉——正文直接少一截。AI 讲解 C++ 泛型、
 * TS 泛型、JSX 时这种写法太常见，不能为了修 README 把对话内容吃掉。
 *
 * 所以只在这段文本确实出现了「README 才会有的块级标签」时才启用 HTML 解析：
 * 命中就按标签渲染（可能吞掉同文档里的 `<int>`，与 GitHub 行为一致），
 * 不命中就保持原样——`<` 老老实实当字符。
 */
const README_BLOCK_TAG =
  /<(?:p|div|span|center|img|picture|source|details|summary|table|thead|tbody|tr|td|th|br|hr|blockquote|figure|figcaption)\b[^>]*>/i;

/** 这段 Markdown 需不需要走 rehype-raw（见上面的取舍说明） */
export function looksLikeHtmlBlock(content: string): boolean {
  return README_BLOCK_TAG.test(content);
}
