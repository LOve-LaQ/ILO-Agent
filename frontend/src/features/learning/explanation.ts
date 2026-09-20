import { catLabel, fmtStars } from '../../shared/lib/format';
import type { TechCard } from '../../shared/types/card';

/**
 * 基于卡片结构化字段生成开场讲解（本地模板，零成本；提问阶段走 DeepSeek）。
 * 从原 index.html 的 buildExplanation() 迁移，并移除其中从未被使用的 tags 死变量。
 */
export function buildExplanation(item: TechCard): string {
  const title = item.title || '该技术';
  const one = item.one_liner || item.summary || '';
  const highlights = item.highlights ?? [];
  const stack = item.tech_stack ?? [];
  const useCases = item.use_cases ?? [];

  const meta: string[] = [];
  if (item.stars) meta.push(`⭐ **${fmtStars(item.stars)}** stars`);
  if (item.language) meta.push(`语言 **${item.language}**`);
  if (item.score) meta.push(`社区热度 **${item.score}**`);

  const lines: string[] = [];
  lines.push(`## ${title}`);
  lines.push('');
  if (meta.length) lines.push(`> ${meta.join(' · ')}`);
  if (one) lines.push(`> ${one}`);
  if (item.category) lines.push(`\n**分类**：${catLabel(item.category)}`);

  if (highlights.length) {
    lines.push('\n### 🔑 亮点');
    highlights.slice(0, 3).forEach((h) => lines.push(`- ${h}`));
  }
  if (stack.length) {
    lines.push('\n### 🧰 技术栈');
    lines.push(stack.slice(0, 6).join(' · '));
  }
  if (useCases.length) {
    lines.push('\n### 🎯 适合谁');
    useCases.slice(0, 2).forEach((u) => lines.push(`- ${u}`));
  }

  lines.push('\n### 🚀 下一步');
  lines.push('你可以点「开始提问」追问它的原理、代码实现或应用场景，我会结合这份卡片用大白话给你讲透。');
  return lines.join('\n');
}
