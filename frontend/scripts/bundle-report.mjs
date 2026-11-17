#!/usr/bin/env node
/**
 * 前端产物体积报告 —— 拆分前后用同一把尺子量，保证结论可回溯。
 *
 * 用法：
 *   npm run bundle:report                  # 打印当前 dist 的 raw / gzip 体积
 *   npm run bundle:report -- --save baseline   # 同时写入 docs/bundle/baseline.json
 *
 * 为什么自己算 gzip 而不依赖 vite 的控制台输出：vite 4 之后默认不再打印 gzip，
 * 且控制台数字是四舍五入的；这里用 node:zlib 按 gzip 最高压缩级别算，数字可复现。
 * 快照落进仓库，任何一次「又变大了」都能 diff 出来是哪一类 chunk。
 */
import { readdirSync, readFileSync, mkdirSync, writeFileSync, statSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const assetsDir = join(root, 'dist', 'assets');
const saveAs = process.argv.includes('--save') ? process.argv[process.argv.indexOf('--save') + 1] : null;

if (!statSync(assetsDir, { throwIfNoEntry: false })) {
  console.error('找不到 dist/assets，请先 npm run build');
  process.exit(1);
}

const human = (n) => (n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`);
const gzipSize = (buf) => gzipSync(buf, { level: 9 }).length;

function classify(name) {
  if (/\.css$/.test(name)) return 'css';
  // chunk 命名约定见 vite.config.ts 的 manualChunks；未归类的落到 entry/other
  if (/^vendor-/.test(name)) return 'vendor';
  if (/^markdown-/.test(name)) return 'markdown';
  if (/^router-/.test(name)) return 'router';
  if (/^state-/.test(name)) return 'state';
  if (/^index-/.test(name) && /\.js$/.test(name)) return 'entry';
  return 'other';
}

const files = readdirSync(assetsDir)
  .filter((f) => /\.(js|css)$/.test(f))
  .map((name) => {
    const buf = readFileSync(join(assetsDir, name));
    return { name, kind: classify(name), raw: buf.length, gzip: gzipSize(buf) };
  })
  .sort((a, b) => b.gzip - a.gzip);

const byKind = {};
for (const f of files) {
  const bucket = (byKind[f.kind] ??= { raw: 0, gzip: 0, files: 0 });
  bucket.raw += f.raw;
  bucket.gzip += f.gzip;
  bucket.files += 1;
}
const total = files.reduce(
  (acc, f) => ({ raw: acc.raw + f.raw, gzip: acc.gzip + f.gzip }),
  { raw: 0, gzip: 0 },
);

console.log('文件'.padEnd(34), '类型'.padEnd(10), 'raw'.padStart(10), 'gzip'.padStart(10));
console.log('-'.repeat(68));
for (const f of files) {
  console.log(
    f.name.padEnd(34),
    f.kind.padEnd(10),
    human(f.raw).padStart(10),
    human(f.gzip).padStart(10),
  );
}
console.log('-'.repeat(68));
console.log('合计'.padEnd(34), ''.padEnd(10), human(total.raw).padStart(10), human(total.gzip).padStart(10));
console.log('\n按类型：');
for (const [kind, v] of Object.entries(byKind).sort((a, b) => b[1].gzip - a[1].gzip)) {
  console.log(`  ${kind.padEnd(10)} ${String(v.files).padStart(2)} 个文件  raw ${human(v.raw).padStart(10)}  gzip ${human(v.gzip).padStart(10)}`);
}

const snapshot = {
  generatedAt: new Date().toISOString(),
  total,
  byKind,
  files: files.map((f) => ({ name: f.name, kind: f.kind, raw: f.raw, gzip: f.gzip })),
};

if (saveAs) {
  const dir = join(root, '..', 'docs', 'bundle');
  mkdirSync(dir, { recursive: true });
  const out = join(dir, `${saveAs}.json`);
  writeFileSync(out, JSON.stringify(snapshot, null, 2) + '\n');
  console.log(`\n快照已写入 ${out}`);
}
