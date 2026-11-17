import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * 手动分包：按「变更频率」与「依赖边界」切，而不是按体积硬切。
 *
 * 【为什么用 advancedChunks / codeSplitting 而不是 manualChunks】
 * `manualChunks` 只给一个归属建议，Rolldown 会把被多个组引用的共享模块（典型是 react）
 * 归到「最先引用它的组」——react-markdown 依赖 react，导致整个 react 被塞进 markdown 块，
 * 于是 router/state/vendor 全都反向依赖 markdown，markdown 永远被 modulepreload。
 * `codeSplitting.groups` 有 `priority`，高优先级组能把模块从低优先级组里抢回来，
 * 才能真正保证 react 只在 vendor 块、markdown 是干净叶子。
 *
 * 收益：
 * 1. 框架核心（react / router / 状态库）几乎不改，业务发版后用户无需重下；
 * 2. markdown 解析链独立成块，配合 MarkdownBody 的 React.lazy 实现首屏按需加载；
 * 3. entry 只剩业务代码，单个 chunk 不再触发 500KB 警告。
 *
 * 【命名约定】组名直接成为 chunk 文件名前缀，必须与
 * `scripts/bundle-report.mjs` 的 classify() 对齐，否则前后对比会用两把尺子。
 *
 * 【正则用 [\\/] 而非 /】Rolldown 官方建议，兼容 Windows 反斜杠路径。
 */
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // 注：Vite 8 里 advancedChunks 与 codeSplitting 等价，后者是新名字
        codeSplitting: {
          groups: [
            // 框架核心优先级最高：react 绝不能被 markdown 组抢走。
            // react-dom 与 scheduler 必须同块（react-dom 依赖 react internals）。
            {
              name: 'vendor',
              test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/,
              priority: 3,
            },
            {
              name: 'router',
              test: /node_modules[\\/](react-router|react-router-dom|@remix-run)[\\/]/,
              priority: 2,
            },
            {
              name: 'state',
              test: /node_modules[\\/](zustand|@tanstack)[\\/]/,
              priority: 2,
            },
            // markdown 链：react-markdown + unified/remark/rehype/micromark 全家桶。
            // 优先级低于框架组，确保 react 被抢回 vendor。
            {
              name: 'markdown',
              test: /node_modules[\\/](react-markdown|unified|remark-|rehype-|micromark|mdast-|hast-|vfile|parse5|hastscript|unist-|property-information|character-entities|character-reference|comma-separated-tokens|space-separated-tokens|html-void-elements|html-url-attributes|web-namespaces|inline-style-parser|style-to-|parse-entities|stringify-entities|decode-named|escape-string-regexp|longest-streak|trim-lines|is-plain-obj|estree-util|devlop|bail|trough|zwitch|ccount|extend|entities|markdown-table)[\\/]/,
              priority: 1,
            },
          ],
        },
      },
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
