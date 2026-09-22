/**
 * 领域类型：技术卡片
 *
 * 【契约边界】接口形状的唯一来源是后端 OpenAPI —— 由 `npm run gen:api` 生成到
 * `shared/api/schema.d.ts`，本文件只做别名导出，禁止再手写接口字段。
 * 后端权威定义见 backend/src/schemas/card.py。
 */

import type { components } from '../api/schema';

/** 统一技术卡片（仓库卡 / 文章卡共用） */
export type TechCard = components['schemas']['TechCard'];

/** 卡片类型：仓库 / 文章 */
export type CardType = TechCard['type'];

/** 发现页视图 Tab（纯 UI 概念，与后端的 card.type 同形但不属契约） */
export type CardTab = 'repo' | 'article';

/** 受控分类词表，见 backend/src/modules/discovery/summary_spec.py 的 CATEGORIES */
export type CategoryCode = TechCard['category'];

/** GET /discover/news 与 GET /discover/articles 的响应 */
export type CardListResponse = components['schemas']['CardListResponse'];

/** GET /discover/cards/{card_id}/content 的响应：原文（README）快照 */
export type CardContentResponse = components['schemas']['CardContentResponse'];

/** 原文快照的版本信息（path / sha / 截断标记等） */
export type CardContentMeta = components['schemas']['CardContentMeta'];

/** GET /discover/cards/{card_id}/digest 的响应：README 的中文导读（需登录） */
export type CardDigestResponse = components['schemas']['CardDigestResponse'];

/** POST /discover/refresh 与 POST /discover/refresh-articles 的响应 */
export type RefreshResponse = components['schemas']['RefreshResponse'];
