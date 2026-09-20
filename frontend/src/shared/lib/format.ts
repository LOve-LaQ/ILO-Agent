/**
 * 展示层格式化工具
 * 从原 frontend/index.html 内联脚本中的 CAT_LABELS / LANG_COLORS / SRC_LOGO 及
 * catLabel / langColor / srcLogoCls / srcAbbr / fmtStars 迁移而来。
 */

/** 分类中文名，与后端 summary_spec.py 的 CATEGORY_LABELS 保持一致 */
export const CAT_LABELS: Record<string, string> = {
  backend: '后端',
  frontend: '前端',
  ai_ml: 'AI/ML',
  devops: '运维',
  database: '数据库',
  mobile: '移动端',
  security: '安全',
  tools: '工具',
  other: '其他',
};

/** GitHub 语言色板，用于仓库卡左侧色点 */
export const LANG_COLORS: Record<string, string> = {
  Python: '#3572A5',
  JavaScript: '#f1e05a',
  TypeScript: '#3178c6',
  Swift: '#f05138',
  Rust: '#dea584',
  Go: '#00ADD8',
  Java: '#b07219',
  C: '#555',
  'C++': '#f34b7d',
  Lean: '#6c4cc4',
  Ruby: '#701516',
  'C#': '#178600',
  Shell: '#89e051',
  Kotlin: '#A97BFF',
  Dart: '#00B4AB',
  PHP: '#4F5D95',
};

/** 来源平台标识（用于文章卡左上角 logo） */
export type SourceLogoKey = 'hn' | 'lob' | 'dev' | 'so' | 'gh';

const SOURCE_LOGO: Record<string, { key: SourceLogoKey; abbr: string }> = {
  'Hacker News': { key: 'hn', abbr: 'HN' },
  Lobsters: { key: 'lob', abbr: 'L' },
  'dev.to': { key: 'dev', abbr: 'd' },
  'Stack Overflow': { key: 'so', abbr: 'SO' },
  'GitHub Trending': { key: 'gh', abbr: 'GH' },
  GitHub: { key: 'gh', abbr: 'GH' },
};

const DEFAULT_LOGO: { key: SourceLogoKey; abbr: string } = { key: 'gh', abbr: 'GH' };

export function catLabel(category?: string | null): string {
  if (!category) return '';
  return CAT_LABELS[category] ?? category;
}

export function langColor(language?: string | null): string {
  if (!language) return '#8b8b93';
  return LANG_COLORS[language] ?? '#8b8b93';
}

export function sourceLogo(source?: string | null): { key: SourceLogoKey; abbr: string } {
  if (!source) return DEFAULT_LOGO;
  return SOURCE_LOGO[source] ?? DEFAULT_LOGO;
}

/** 星数缩写：23400 -> 23k，8900 -> 8.9k */
export function fmtStars(stars?: number | null): string {
  const n = stars ?? 0;
  if (n >= 1000) {
    return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  }
  return String(n);
}

/** 卡片主文案：优先 summary，其次 one_liner */
export function cardText(card: {
  summary?: string | null;
  one_liner?: string | null;
}): string {
  return card.summary || card.one_liner || '暂无摘要';
}

/**
 * 格式化后端返回的 ISO 时间（带时区）为本地「YYYY-MM-DD HH:mm」。
 *
 * 溯源与行为流水里的时间要能对上账，所以宁可显示完整时间也不做模糊处理；
 * 缺失或非法值返回空串，避免界面上出现 `Invalid Date`。
 */
export function fmtDateTime(value?: string | null): string {
  const date = parseDate(value);
  if (!date) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** 相对时间：刚刚 / n 分钟前 / n 小时前 / n 天前；超过 30 天回落到具体日期 */
export function fmtRelative(value?: string | null): string {
  const date = parseDate(value);
  if (!date) return '';
  const diffSeconds = Math.floor((Date.now() - date.getTime()) / 1000);

  if (diffSeconds < 60) return '刚刚';
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)} 分钟前`;
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)} 小时前`;
  if (diffSeconds < 86400 * 30) return `${Math.floor(diffSeconds / 86400)} 天前`;
  return fmtDateTime(value);
}

function parseDate(value?: string | null): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}
