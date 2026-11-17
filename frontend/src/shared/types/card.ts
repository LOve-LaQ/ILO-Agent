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

/**
 * 首页的四个视图（含榜单与搜索）
 *
 * `ranking` / `search` 都不参与卡片请求：它们是另一套数据（GitHub Search API
 * 的只读结果，零 LLM 成本、可长缓存），所以只能出现在视图层，绝不能混进
 * `CardTab` —— 否则 `fetchCards('ranking')` 这种不安全调用在类型上就合法了。
 */
export type HomeView = CardTab | 'ranking' | 'search';

/** 榜单窗口：近 7 天 / 近 30 天 */
export type RankingWindow = 'week' | 'month';

/** GET /discover/rankings 的响应：GitHub 新星榜 */
export type RankingResponse = components['schemas']['RankingResponse'];

/** 榜单一行 */
export type RankingItem = components['schemas']['RankingItem'];

/** GET /discover/search 的响应：按关键词检索仓库 */
export type SearchResponse = components['schemas']['SearchResponse'];

/** 搜索实际生效的筛选条件（后端回显） */
export type SearchFilters = components['schemas']['SearchFilters'];

/** 搜索排序：按星数 / 按最近更新 */
export type SearchSort = SearchFilters['sort'];

/** 榜单语言筛选项（后端是唯一事实源） */
export type RankingLanguage = components['schemas']['RankingLanguage'];

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

/** POST /discover/refresh 与 POST /discover/refresh-articles 的响应（异步受理，202） */
export type TaskAcceptedResponse = components['schemas']['TaskAcceptedResponse'];

/** GET /discover/cards/{card_id}/similar 的响应：按向量近邻召回的相似项目 */
export type SimilarResponse = components['schemas']['SimilarResponse'];

/** 相似推荐的一条（带上理由文案与共享概念） */
export type SimilarItem = components['schemas']['SimilarItem'];

/** GET /discover/tasks/{task_id} 的响应：采集任务的真实进度 */
export type TaskProgressResponse = components['schemas']['TaskProgressResponse'];
