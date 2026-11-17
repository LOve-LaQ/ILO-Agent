import { api } from '../../shared/api/client';
import type {
  CardListResponse,
  CardTab,
  RankingResponse,
  RankingWindow,
  SearchResponse,
  SearchSort,
  TaskAcceptedResponse,
  TaskProgressResponse,
} from '../../shared/types/card';

/** 卡片池单次拉取条数，与原 index.html 的 limit=6 保持一致 */
export const CARDS_PAGE_SIZE = 6;

/** 抓取文章时每个平台抓取条数，与原 index.html 的 per_platform=8 保持一致 */
const ARTICLES_PER_PLATFORM = 8;

/** GET /discover/news | /discover/articles */
export function fetchCards(tab: CardTab, limit: number = CARDS_PAGE_SIZE): Promise<CardListResponse> {
  const path = tab === 'article' ? '/discover/articles' : '/discover/news';
  return api.get<CardListResponse>(`${path}?limit=${limit}`);
}

/**
 * POST /discover/refresh | /discover/refresh-articles
 *
 * 语义已改为**异步受理**：抓取 + 数十次 LLM 摘要要跑 1~3 分钟，同步接口会被
 * 浏览器与反向代理先超时切断（用户看到网络错误并重试 = 服务端再付一遍 LLM 钱）。
 * 这里拿到的是 task_id（= 采集批次号），进度用 fetchTaskProgress 轮询。
 */
export function refreshCards(tab: CardTab, timeRange: string): Promise<TaskAcceptedResponse> {
  if (tab === 'article') {
    return api.post<TaskAcceptedResponse>(
      `/discover/refresh-articles?per_platform=${ARTICLES_PER_PLATFORM}&time_range=${timeRange}`,
    );
  }
  return api.post<TaskAcceptedResponse>('/discover/refresh?limit=50');
}

/** GET /discover/tasks/{task_id}：采集任务的真实进度 */
export function fetchTaskProgress(taskId: string): Promise<TaskProgressResponse> {
  return api.get<TaskProgressResponse>(`/discover/tasks/${taskId}`);
}

/** 榜单默认取前 20 名：再长就不像「榜」而像列表，而单次 GitHub 请求只抓这一页 */
export const RANKING_LIMIT = 20;

/**
 * GET /discover/rankings：GitHub 新星榜（窗口内创建、按 star 总数排序）
 *
 * 参数直接用后端的白名单值：`language` 传非法值会被 422 拒绝，而可选值本身
 * 由响应里的 `languages` 下发，所以前端不该硬编码任何语言名。
 */
export function fetchRanking(
  window: RankingWindow,
  language: string,
  limit: number = RANKING_LIMIT,
): Promise<RankingResponse> {
  const query = new URLSearchParams({ window, language, limit: String(limit) });
  return api.get<RankingResponse>(`/discover/rankings?${query.toString()}`);
}

/** 搜索单次取 15 条：再多就不像「结果」而像列表，且每次穿透都有配额成本 */
export const SEARCH_LIMIT = 15;

export interface SearchParams {
  q: string;
  language?: string;
  minStars?: number;
  sort?: SearchSort;
  limit?: number;
}

/**
 * GET /discover/search：按关键词检索 GitHub 仓库
 *
 * 后端把 `q` 限制在 64 字、`limit` 在 5..30、`sort` 只认 stars|updated，
 * 越界一律 422 —— 这里不重复校验，让契约说话（前端的校验永远会和后端漂移）。
 */
export function searchRepos({
  q,
  language = 'all',
  minStars = 0,
  sort = 'stars',
  limit = SEARCH_LIMIT,
}: SearchParams): Promise<SearchResponse> {
  const query = new URLSearchParams({
    q,
    language,
    min_stars: String(minStars),
    sort,
    limit: String(limit),
  });
  return api.get<SearchResponse>(`/discover/search?${query.toString()}`);
}
