/**
 * 「原文快照」渲染守卫：确认 MarkdownBody 的 HTML 白名单既**渲染**了 README 里的
 * 内联 HTML，又没有把脚本 / 事件属性 / javascript: 链接放进 DOM。
 *
 * 为什么要有这条脚本：`rehype-raw` 是被引入来修「`<p align="center">` 当纯文本显示」
 * 的，但它是把任意第三方 README 的 HTML 直接喂进渲染树。加它而不加 sanitize 等于
 * 开 XSS 口子，两者必须成对出现，且顺序不能反。这条脚本把这对约束钉死。
 *
 * 运行：`npm run check:sanitize`（schema 直接从 src 里 import，不另抄一份）
 */
import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import remarkRehype from 'remark-rehype';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize from 'rehype-sanitize';

import { markdownHtmlSchema } from '../src/components/ui/markdownHtmlSchema.ts';

const sample = `# 标题

<p align="center"><a href="javascript:alert(1)"><img src="https://img.shields.io/x.svg" onerror="alert(2)" width="120"></a></p>

<script>alert(3)</script>
<style>body{display:none}</style>
<iframe src="https://evil.example"></iframe>
<div onclick="alert(4)">div 内容</div>
<details><summary>折叠标题</summary>折叠内容</details>
`;

const processor = unified()
  .use(remarkParse)
  .use(remarkGfm)
  .use(remarkRehype, { allowDangerousHtml: true })
  .use(rehypeRaw)
  .use(rehypeSanitize, markdownHtmlSchema);

const tree = processor.runSync(processor.parse(sample));
const dump = JSON.stringify(tree);

const tags = new Set();
const props = [];
(function walk(node) {
  if (node.type === 'element') {
    tags.add(node.tagName);
    for (const [key, value] of Object.entries(node.properties ?? {})) {
      props.push(`${node.tagName}.${key}=${JSON.stringify(value)}`);
    }
  }
  for (const child of node.children ?? []) walk(child);
})(tree);

const mustBeGone = [
  ['<script> 标签', tags.has('script')],
  ['<style> 标签', tags.has('style')],
  ['<iframe> 标签', tags.has('iframe')],
  ['onerror 事件属性', /onerror/i.test(dump)],
  ['onclick 事件属性', /onclick/i.test(dump)],
  ['javascript: 协议', /javascript:/i.test(dump)],
  ['脚本内容以纯文本残留', dump.includes('alert(3)')],
];

const mustStay = [
  ['<p align="center"> 对齐属性', props.includes('p.align="center"')],
  ['<details> 折叠块', tags.has('details') && tags.has('summary')],
  ['<img width> 尺寸属性', props.some((p) => p.startsWith('img.width='))],
  ['徽章图片本身', tags.has('img')],
];

let failed = 0;
console.log('渲染结果标签:', [...tags].join(' '));
console.log('\n必须被清除：');
for (const [name, hit] of mustBeGone) {
  console.log(hit ? `  FAIL 仍存在 -> ${name}` : `  ok   已清除 -> ${name}`);
  if (hit) failed += 1;
}
console.log('\n必须保留：');
for (const [name, ok] of mustStay) {
  console.log(ok ? `  ok   保留   -> ${name}` : `  FAIL 丢失   -> ${name}`);
  if (!ok) failed += 1;
}

if (failed > 0) {
  console.error(`\n${failed} 项不符合预期`);
  process.exit(1);
}
console.log('\n全部通过');
