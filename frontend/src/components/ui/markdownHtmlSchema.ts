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
