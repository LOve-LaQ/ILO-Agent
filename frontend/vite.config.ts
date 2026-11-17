import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * 手动分包：按「变更频率」与「依赖边界」切，而不是按体积硬切。
 *
 * 三类收益：
 * 1. 框架核心（react / router / 状态库）几乎不改，业务代码发版后用户无需重下；
 * 2. markdown 解析链（unified + remark/rehype/micromark 全家桶）是最大的一块，
 *    且只有「学习抽屉」与「学习记录详情」用到，独立成块便于后续按需加载；
 * 3. entry 只剩业务代码，单个 chunk 不再触发 500KB 警告。
 *
 * 【为什么解析包名而不是子串匹配】早期版本用正则子串匹配，`extend` 这类通用名
 * 误伤了别的库；更严重的是 Rolldown 会把 react-markdown 依赖的 `react` 一起拉进
 * markdown 块，导致 router/state/vendor 全都反向依赖 markdown（等于没拆）。
 * 这里先精确取出 node_modules 后的包名（含 @scope），再按整名归类，边界清晰。
 *
 * 【命名约定】返回的名字直接成为 chunk 文件名前缀，必须与
 * `scripts/bundle-report.mjs` 的 classify() 对齐，否则前后对比会用两把尺子。
 */
function packageName(id: string): string | null {
  const p = id.replace(/\\/g, '/'); // Windows 下 id 可能是反斜杠
  const marker = '/node_modules/';
  const i = p.lastIndexOf(marker);
  if (i < 0) return null;
  const rest = p.slice(i + marker.length);
  const parts = rest.split('/');
  // scoped 包：@scope/name；普通包：name
  return parts[0].startsWith('@') ? `${parts[0]}/${parts[1]}` : parts[0];
}

// react-markdown 整条解析链（来自实际依赖树，sourcemap 逐个核对过）
const MARKDOWN_PACKAGES = new Set([
  'react-markdown', 'unified', 'vfile', 'vfile-location', 'vfile-message',
  'bail', 'trough', 'zwitch', 'is-plain-obj', 'extend', 'devlop', 'ccount',
  'trim-lines', 'longest-streak', 'escape-string-regexp', 'estree-util-is-identifier-name',
  '@ungap/structured-clone', 'entities', 'decode-named-character-reference',
  'character-entities', 'character-entities-html4', 'character-entities-legacy',
  'character-reference-invalid', 'comma-separated-tokens', 'space-separated-tokens',
  'property-information', 'html-void-elements', 'html-url-attributes', 'web-namespaces',
  'inline-style-parser', 'style-to-js', 'style-to-object', 'parse-entities',
  'stringify-entities', 'markdown-table', 'hastscript', 'parse5',
  'hast-util-from-parse5', 'hast-util-parse-selector', 'hast-util-raw',
  'hast-util-sanitize', 'hast-util-to-jsx-runtime', 'hast-util-to-parse5',
  'hast-util-whitespace', 'unist-util-is', 'unist-util-position',
  'unist-util-stringify-position', 'unist-util-visit', 'unist-util-visit-parents',
  'remark-parse', 'remark-gfm', 'remark-rehype', 'remark-breaks', 'remark-stringify',
  'rehype-raw', 'rehype-sanitize', 'mdast-util-find-and-replace',
  'mdast-util-from-markdown', 'mdast-util-to-hast', 'mdast-util-to-markdown',
  'mdast-util-to-string', 'mdast-util-phrasing', 'mdast-util-newline-to-break',
  'mdast-util-gfm', 'mdast-util-gfm-autolink-literal', 'mdast-util-gfm-footnote',
  'mdast-util-gfm-strikethrough', 'mdast-util-gfm-table', 'mdast-util-gfm-task-list-item',
  'micromark', 'micromark-core-commonmark', 'micromark-extension-gfm',
  'micromark-extension-gfm-autolink-literal', 'micromark-extension-gfm-footnote',
  'micromark-extension-gfm-strikethrough', 'micromark-extension-gfm-table',
  'micromark-extension-gfm-tagfilter', 'micromark-extension-gfm-task-list-item',
  'micromark-factory-destination', 'micromark-factory-label', 'micromark-factory-space',
  'micromark-factory-title', 'micromark-factory-whitespace', 'micromark-util-character',
  'micromark-util-chunked', 'micromark-util-classify-character',
  'micromark-util-combine-extensions', 'micromark-util-decode-numeric-character-reference',
  'micromark-util-decode-string', 'micromark-util-encode', 'micromark-util-html-tag-name',
  'micromark-util-normalize-identifier', 'micromark-util-resolve-all',
  'micromark-util-sanitize-uri', 'micromark-util-subtokenize', 'micromark-util-symbol',
  'micromark-util-types',
]);
const FRAMEWORK_PACKAGES = new Set(['react', 'react-dom', 'scheduler']);

function manualChunks(id: string): string | undefined {
  const pkg = packageName(id);
  if (!pkg) return undefined;

  // 顺序有意义：先认框架，再认 markdown，避免 react 被拉进 markdown 块。
  if (FRAMEWORK_PACKAGES.has(pkg)) return 'vendor';
  if (MARKDOWN_PACKAGES.has(pkg)) return 'markdown';
  if (pkg === 'react-router' || pkg === 'react-router-dom' || pkg.startsWith('@remix-run/')) {
    return 'router';
  }
  if (pkg === 'zustand' || pkg.startsWith('@tanstack/')) return 'state';

  // 零星小依赖（clsx 等）留在 entry，单独成块只会多一次请求。
  return undefined;
}

// 开发期通过 proxy 走同源请求，避免依赖后端 CORS 通配配置
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: { manualChunks },
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
});
