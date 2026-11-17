import { lazy, Suspense } from 'react';

import type { MarkdownBodyProps } from './MarkdownBodyImpl';

/**
 * Markdown 渲染门面 —— 刻意不含任何 markdown 依赖。
 *
 * 【为什么拆成门面 + 实现两层】react-markdown + unified/remark/rehype 是前端最大的
 * 依赖块（gzip ~96KB），但只有真正打开学习抽屉、或回看学习记录时才用得到。
 * 如果这里直接 `import ReactMarkdown`，静态引用链会一路连到首页 entry，Vite 就会
 * 给 markdown chunk 注入 modulepreload —— 首屏白下载这 96KB。
 *
 * 用 `React.lazy` 把实现挪到动态 import 后，这块只在首次渲染 MarkdownBody 时加载。
 * 抽屉本身仍常驻首页（刷新后要靠它恢复上次会话），但 closed 态 `return null`，
 * 不会渲染到 MarkdownBody，所以首屏不会触发动态加载。
 *
 * 实现在 `./MarkdownBodyImpl.tsx`，XSS 白名单在 `./markdownHtmlSchema.ts`。
 * 请勿把实现挪回本文件，否则按需加载失效。
 */
const MarkdownBodyImpl = lazy(() => import('./MarkdownBodyImpl'));

export type { MarkdownBodyProps };

export function MarkdownBody(props: MarkdownBodyProps) {
  return (
    <Suspense fallback={<span className="md-loading" aria-label="正在加载正文" />}>
      <MarkdownBodyImpl {...props} />
    </Suspense>
  );
}
