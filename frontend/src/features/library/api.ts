/**
 * 内容溯源接口（library feature 内部使用）
 *
 * 与 `/me/*` 不同，溯源的产物是**公开的卡片元数据**，因此它天然匿名可读，契约也放在
 * 后端的 discover 域（`src/schemas/discover.py`），不混进按用户划分的 /me 域。
 * 这里单独放一份封装，是因为目前只有「我的 → 收藏」需要展示它。
 *
 * 【契约边界】类型由后端 OpenAPI 派生，勿手写字段。后端改动后执行 `npm run gen:api`。
 */

import { api } from '../../shared/api/client';
import type { components } from '../../shared/api/schema';

export type ProvenanceResponse = components['schemas']['ProvenanceResponse'];
export type ProvenanceBatch = components['schemas']['ProvenanceBatch'];

/** GET /discover/cards/{card_id}/provenance —— 哪次采集、哪个链接、摘要生成前的原文 */
export function fetchProvenance(cardId: string): Promise<ProvenanceResponse> {
  return api.get<ProvenanceResponse>(`/discover/cards/${encodeURIComponent(cardId)}/provenance`);
}
