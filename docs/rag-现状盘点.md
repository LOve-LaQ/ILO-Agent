# 给 ILO-Agent 加 RAG · 代码现状盘点

> 盘的是**改造起点**，不是改造方案。
> 盘点时间：2026-10-05。结论以仓库当前代码为准（后端 296 个测试可收集，2 个 eval 用例默认 deselect）。

---

## 零、四句话结论

1. **向量侧的零件几乎全齐**，甚至「检索原语」都已经写好了 —— 但**是死代码，零调用方**。
2. **真正缺的只有三件事**：文本切分、把检索结果拼进提示词、新建块向量集合。
3. **语料只有 GitHub README**，实测 **942 篇 / 14,760 个标题 / 1,229 万字**。长度和结构都**适合按标题切分**（0.4% 零标题）。
4. ⚠️ **最大的技术风险是跨语言**：原文 **77.2% 纯英文**，摘要 **94.7% 中文**。中文提问要命中英文原文块 —— 这是本项目 RAG 最该实测、也最容易静默失效的地方。

---

## 一、已有能力清单（按 RAG 链路排列）

### 1.1 向量化层 —— 齐

| 能力 | 位置 | 说明 |
|---|---|---|
| 多 Provider 向量化 | `src/modules/agent/embedding_service.py`（390 行） | 阿里云 Qwen / Voyage / 智谱 / 百度 / OpenAI 五家 |
| Provider 探测 | 同上 `_detect_provider()` | 与环境变量**精确比对**，避免阿里云 `sk-` 开头的 Key 被误判成 OpenAI；兜底才按前缀猜 |
| 单条向量化 | `generate_embedding(text)` | 失败返回 `None`，**不在底层生成占位向量**（占位由调用方决定） |
| 批量向量化 | `generate_batch_embeddings(texts)` | ⚠️ **是 for 循环串行**，不是真批量协议 |
| 文本缓存 | `self._cache`（MD5 键） | 进程内 dict，无上限、无淘汰 |
| 重试 | `_post()` + `with_retry(attempts=2)` | **只重试网络层**瞬时故障；4xx 不重试 |
| 熔断 | `circuit_breaker.EMBEDDING` | 5xx/429 计失败，4xx 既不算成功也不算失败 |

### 1.2 向量库层 —— 齐（三个集合）

| 集合 | 归属 | 维度/距离 | 内容 |
|---|---|---|---|
| `tech_encyclopedia` | `src/modules/discovery/tech_knowledge.py` | 1536 / COSINE | 技术卡片 |
| `user_profiles` | `src/modules/agent/memory_manager.py` | 1536 / COSINE | 用户偏好画像 |
| `learning_history` | 同上 | 1536 / COSINE | 学习事件 |

**写入侧的关键设计（`TechKnowledgeBase.upsert`）**：

- 向量化输入**不是 README 原文**，而是把卡片结构化字段拼成的检索文本：
  `标题 + 语言/来源 + 一句话 + 摘要 + 问题 + 技术栈 + 亮点 + 场景`，再前置中文分类名。这就是「检索文本设计」的落地。
- **payload 显式排除 `raw_content` / `content_meta`**（`tech_knowledge.py:175-179`）—— 单条几十 KB，会撑爆索引并拖慢 scroll。
  → **推论：做块向量必须回 PostgreSQL 读原文，不能指望从向量库 payload 里拿到。**
- 点 ID = `md5(item_id)` 取前 16 位转 int，重复入库按 ID 覆盖。

**读取侧已有方法**：`count` / `sample` / `candidate_points(limit=500)` / `list_by_category` / `get_by_id` / `get_by_ids` / `get_vectors_by_ids`（批量取向量，为兴趣画像新增）。

**超时**：读档 4s / 写档 6s / 健康探测 2s，全部走 `qdrant_timeout_seconds()` 包装 —— 因为 qdrant-client 的 REST 客户端对**按次**传入的 timeout 做 `int(str(值))`，传 float 会直接 `ValueError`（这是踩过的坑）。

### 1.3 检索原语层 —— **齐，但是死代码** ⚠️

`src/modules/agent/memory_manager.py` 里已经有一套**完整的检索实现**：

```python
search_similar_learnings_async(query_text, top_k=5, user_id=None)
    → _embed_async(query_text)              # 问题向量化
    → qdrant_client.query_points(...)       # Qdrant 检索
    → 按 user_id 过滤 → 返回 top-k          # 强制按用户隔离
```

这一条链就是 RAG 查询段的前半截：`query → embedding → 向量检索 → top-k`。

**但它零调用方。** 全仓库 `src/` 与 `tests/` grep 确认，以下方法**都没有任何调用点**：

| 方法 | 语义 |
|---|---|
| `search_similar_learnings_async` | 检索相似学习历史（按 user_id 过滤） |
| `search_similar_users_async` | 检索相似用户画像（**跨用户**，协同过滤用） |
| `record_learning_event_async` | 写学习事件向量 |
| `update_user_preferences_async` | 写用户偏好向量 |
| `recommend_with_collaborative_filtering` | 协同过滤推荐（调 `search_similar_users`） |

`learning.py` 只用了 `MemoryManager` 的 `get_session_context` / `save_session_context`（Redis 那两个方法）。

**两个后果**：
- 好的一面：RAG 的检索原语不用从零写，且有现成的**权限过滤范式**（`_search_similar_learnings` 的 docstring 明确写了「取不到 user_id 时返回空：宁可少一个功能，也不能让检索退化成越权」）。
- 坏的一面：**这是潜在风险**。评审时 grep 到 5 个零调用方法会问「这是干嘛的」。要么接进链路，要么删。RAG 正好能给它找到归宿。

### 1.4 排序层 —— 唯一真正跑起来的向量用途

| 文件 | 内容 |
|---|---|
| `src/services/recommend.py`（169 行） | **纯函数内核，无 IO**：`is_degenerate_vector` / `cosine_similarity` / `normalize_score` / `diversify_by_category` / `rank_candidates` |
| `src/services/interest_profile.py`（347 行） | `aggregate_signals`（半衰期 14 天指数衰减）/ `weighted_mean_vector` / `embedding_fingerprint` / `build_user_profile` / `_save_profile` |

- 已接进 `GET /discover/news`，契约字段 `recommend_score`（0~1）+ `recommend_reason`（中文文案，分档 0.55 / 0.40）。
- 画像存 PostgreSQL 表（迁移 `f2a7c5b9d3e1_add_user_interest_profiles.py`），记 `embedding_model`（指纹）+ 来源卡片数。
- 测试：`tests/test_recommend_ranking.py`、`tests/test_interest_profile.py`。

### 1.5 提示词层 —— 齐，但 `/chat` 是内联字符串

| 能力 | 位置 |
|---|---|
| 摘要提示词 | `src/modules/discovery/summary_spec.py:65` `build_summary_prompt(items, kind)` |
| 中文导读提示词 | 同上 `:130` `build_digest_prompt(title, raw_description, readme)` |
| 中文化闸门 | 同上 `:25` `is_chinese_text(text, min_cjk=10)` |
| 受控分类词表 | 同上 `:39` `CATEGORIES` + `:52` `CATEGORY_LABELS` |
| 导读长度约束 | `MAX_DIGEST_SOURCE_CHARS=8000` / `DIGEST_MIN_CHARS=300` / `DIGEST_MAX_CHARS=500` |
| LLM 工厂 | `src/modules/agent/state_machine.py:60` `create_llm(provider)`、`:160` `create_qwen_llm()` |
| 生产实例 | 同上 `:187` `llm`（对话，DeepSeek）、`:192` `summary_llm`（批量摘要，千问） |

⚠️ **`/chat` 的 system_prompt 是写在路由函数里的内联 f-string**（`src/api/routes/learning.py:500-518`），不是独立 builder。接入检索时这里要动刀：把内联字符串抽成 `build_chat_prompt(...)` 才好测。

### 1.6 韧性层 —— 齐，可直接复用

| 模块 | 可复用内容 |
|---|---|
| `src/core/resilience.py` | `with_retry` / `with_retry_sync` / `is_retryable` / `raise_if_transient` / `is_timeout_error` / `log_downstream_failure` / `backoff_delay` / `qdrant_timeout_seconds` |
| `src/core/deadline.py` | `clamp_timeout(本层超时, deadline)` / `remaining()` —— 请求级超时预算 |
| `src/core/circuit_breaker.py` | 已有槽位 `LLM_CHAT` / `LLM_SUMMARY` / `EMBEDDING` / **`QDRANT_READ`** ← 检索可直接复用 |
| `src/core/config.py` | `qdrant_timeout=4.0` / `qdrant_write_timeout=6.0` / `embedding_timeout=20.0` / `interest_profile_half_life_days=14` / `interest_profile_max_candidates=200` |

**RAG 接入时不要新造轮子**：检索走 `QDRANT_READ` 熔断槽 + `clamp_timeout` 预算 + `log_downstream_failure` 观测，与现有链路一致。

### 1.7 数据层 —— 语料在这里

| 字段 | 位置 | 说明 |
|---|---|---|
| `raw_content` | `src/models/collection.py:94`（Text, nullable） | **README 快照**，「先读原文」页展示的就是它 |
| `content_meta` | 同上 `:96`（JSONB） | `{path, sha, size, source, fetched_at}` |
| `content_digest_zh` | 同上 `:103` | 中文导读，按需生成 |
| `content_digest_meta` | 同上 `:110` | 含 `source_fingerprint`，绑定生成时的原文版本 |

- **快照语义：首次写入即定稿**，`force` 重摘要不会改写已有快照（保证「采集时刻的原文」可回溯）。
- ⚠️ **`raw_content` 的唯一写入点是 `src/modules/discovery/github_fetcher.py:278`**。
  → **HN / dev.to / Lobsters / SO 的文章没有原文**，只有 GitHub 仓库有。
  → **RAG 语料 = GitHub README**，这个预期要提前定。
- 摘要等卡片字段**不在本表**，在 `card_payload`（JSONB）里：`card_payload->>'summary'` / `->>'one_liner'` / `->>'title'`。
- 迁移共 **9 个**（`alembic/versions/`），每个都保证可 upgrade / downgrade。
- 溯源接口：`GET /discover/cards/{card_id}/provenance`（`discover.py:530`）。

### 1.7b 语料实测数据（2026-10-05，本地库 942 篇）

体检脚本：`backend/audit_corpus_for_rag.py`（只读，零成本）。

| 指标 | 实测值 |
|---|---|
| 记录总数 / 有原文 | 974 / **942**（GitHub 来源 962） |
| 原文均长 / 中位数 / P90 | 13,047 / 9,217 / 26,956 字符 |
| 总字符量 | **12,290,482**（约 1,229 万字） |
| 长度分布 | `<500` 1.7% ｜ `500-2k` 5.4% ｜ **`2k-8k` 36.0%** ｜ **`8k-30k` 48.7%** ｜ `>30k` 8.2% |
| 标题总数 | **14,760**（h1 14.6% / **h2 56.8%** / h3 26.1% / h4 2.5%） |
| 每篇标题数 | 中位数 **13**，P10 6，P90 28，最大 94 |
| **零标题（只能长度兜底）** | **4 / 942（0.4%）** |
| 已有中文导读 | 仅 3 条（几乎没生成过） |

**预估块数**：

| 策略 | 预估块数 | 均块/篇 |
|---|---|---|
| 按 h1-h2 切 + 超长二次切 | ~14,128 | 15.0 |
| 按 h1-h3 切 + 超长二次切 | ~17,051 | 18.1 |
| 固定长度 400 字 | ~31,178 | 33.1 |
| 固定长度 800 字 | ~15,818 | 16.8 |
| 固定长度 1200 字 | ~10,694 | 11.4 |

**语言构成（CJK 占比）**：

| | 纯英文 `<1%` | 英文为主 `1-5%` | 中英混合 `5-30%` | 中文为主 `>30%` | 平均 CJK |
|---|---|---|---|---|---|
| README 原文 | **727（77.2%）** | 11（1.2%） | 82（8.7%） | 122（13.0%） | **6.99%** |
| 现有 summary | 4（0.4%） | 0 | 46（4.9%） | **892（94.7%）** | **57.55%** |

> 这两行是本次盘点最重要的发现，见下一节。


### 1.8 评测层 —— 齐，直接扩

| 文件 | 内容 |
|---|---|
| `backend/evals/runner.py` | 编排：读数据集 → 跑真实生产链路 → 裁判打分 → 汇总 → 出 markdown 报告 |
| `backend/evals/judge.py` | 5 维打分，输出严格 JSON，解析失败重试一次 |
| `backend/evals/datasets/` | `summary_cases.json` 18 条（含 6 条标定坏样本）/ `digest_cases.json` 12 条（含 3 条） |
| `backend/evals/baseline.json` | summary **4.783**（12 条计分）/ digest **4.533**（9 条计分） |
| `backend/evals/reports/` | 7 份历史报告（已 gitignore） |
| `tests/test_eval_gate.py` | 门禁，`@pytest.mark.eval`，默认 deselect |

**设计要点**：
- 裁判用 **DeepSeek**，与生成侧（千问）**故意不同源**，避免自偏好偏差；裁判 `temperature=0`（判分要可重复）。
- 门禁两条线：**性质硬线**（`chinese` / `faithfulness` 不得跌破 3.0）+ **回归线**（其余维度 ≥ 基线 − 0.4）。0.4 是连跑 3 次实测噪声（σ≈0~0.19）取 ~2σ 定的，不是拍脑袋。
- `pytest.ini` 里 `addopts = -m "not eval"` → **默认 `pytest` 零 LLM 调用**。
- 报告里区分 `judge_error`（不计入均分）与真实低分。

**RAG 评测可以直接挂进来**：检索命中率是「检索段」的指标，与「生成段」的五维打分是同一套 runner 的两条腿 —— 这正是 `02-rag.md` §Q7 那张「检索 / 生成四象限归因表」的落地方式。

### 1.9 脚本层 —— 灌库范式现成

| 脚本 | 用途 |
|---|---|
| `backfill_repos_30d.py` | 近 30 天 GitHub 仓库一次性灌库。**块入库脚本可直接抄它的范式**：`--dry-run` 先看数、幂等可重跑、复用 `collect_items` 不另写采集逻辑、批次溯源记 `trigger=script` |
| `backfill_provenance.py` | 补溯源数据 |
| `backfill_summaries.py` | 存量摘要回填 |
| `audit_kb_vectors.py` | **知识库向量健康度审计**（查退化向量） |
| `news_scheduler.py` / `retention_cleanup.py` | 定时采集 / 数据清理 |

---

## 二、本次盘点最重要的两个发现

### 2.1 ⚠️ 跨语言检索风险（新发现，之前所有文档都没提）

实测：**原文 77.2% 是纯英文（平均 CJK 6.99%），摘要 94.7% 是中文（平均 CJK 57.55%）。**

这个组合意味着：**用户会用中文提问，但资料库是英文的。**

```
用户：「这个项目的向量维度上限是多少？」
          ↓ 中文问题向量化
       Qdrant 检索
          ↓ 要命中
     英文块：「Max vector dimension is 65536...」
```

这就要求向量模型**在中文与英文之间对齐**（跨语言语义空间）。做不到的话，后果是：

- 相似度算出来**看起来正常**（0.4~0.6 都有），不会报错；
- 但召回的内容**系统性偏离**；
- 而且「答案块没进前几名但模型答对了」这种最危险的格子会出现得特别频繁 —— 因为模型用训练数据里的英文先验知识答对了，你完全看不出检索是坏的。

**这是本项目 RAG 最大的技术风险，也是最有价值的实验题材。** 三个应对方向（必须实测，不要预设结论）：

1. **混合检索**：技术问题的提问里通常带英文专有名词（`Qdrant` / `dimension` / `API`），关键词检索在英文原文上**天然可用**，能补上向量检索的跨语言缺口。**预期这一项的收益在本语料上会明显大于通用语料。**
2. **中文桥接**：用卡片已有的中文 `summary` / `one_liner` 作为块的检索锚（块向量 = 原文块向量 + 所属卡片中文摘要向量 的拼接或加权）。代价是块向量不再是纯原文语义。
3. **换向量模型**：`EmbeddingService` 已支持五家，阿里云 Qwen 是中文优化，跨语言对齐通常好于 Voyage（英文优化）。**换模型时必须重算全库向量**（见坑 3）。

### 2.2 ✅ 切分可行性比我上一轮估计的好（推翻上一轮判断）

我上一轮说「README 里大量 badge / License / Contributing，按标题切会切出一堆垃圾块，命中率不要指望高」。**实测数据不支持这个判断**：

- **零标题的只有 4 / 942（0.4%）** —— 按 Markdown 标题切分**对 99.6% 的文档可行**；
- 标题总数 14,760，**h2 占 56.8%**，每篇中位数 13 个标题 —— 结构充足，能切出粒度合理的块；
- badge 密集（≥5 行）的样本在抽样 60 条里只有 1 条 —— 噪声没那么严重；
- 长度 2k~30k 占 **84.7%** —— 这是最适合切分的区间，既不碎也不至于无法处理。

**修正后的预期**：按标题切分是**可行且应该是首选**的，`02-rag.md` §Q7 那张「按标题切 92% > 固定长度 80%」的对比表在本语料上**大概率能复现**。真正需要担心的是跨语言（2.1），不是语料质量。

---

## 三、缺口清单（只有 3 项）

| # | 缺口 | 证据 | 预估 |
|---|---|---|---|
| 1 | **文本切分** | `chunk` / `split_text` / `TextSplitter` / `text_splitter` 在 `src/` 全项目**零命中** | 新建 `modules/rag/chunker.py`，~150 行 |
| 2 | **检索 → 拼进提示词** | `/chat` 只注入会话上下文（`learning.py:493-518`）；`search_similar_learnings_async` 零调用 | 接通 + 抽 prompt builder，~180 行 |
| 3 | **块向量集合** | 卡片向量是「结构化字段拼接」，与 README 原文**不是同一语义**，不能复用 `tech_encyclopedia` | 新建集合 + 入库脚本，~120 行 |

**另外确认零命中的**：`bind_tools` / `tool_calls` / `function_call` —— Function Calling 确实完全没碰过。

---

## 四、动手前必须知道的 7 个坑

1. **README 不在 Qdrant 里。** `upsert()` 显式排除了 `raw_content`（`tech_knowledge.py:175-179`）。做块向量必须回 PostgreSQL 读原文。
2. **卡片向量 ≠ 块向量。** 一个来自摘要字段拼接，一个来自原文片段，语义空间不同。**必须新建集合**，混用会得到静默错误的相关性。
3. **指纹机制只覆盖画像。** `embedding_fingerprint()` 在 `interest_profile.py`，只写进用户画像表（`row.embedding_model`）。**Qdrant 里的卡片向量和未来的块向量都没有记模型指纹** —— 换 embedding 模型时不会自动重算，这是既有的隐藏债，也是 RAG 接入时该顺手补上的（尤其因为跨语言问题很可能逼你换模型）。
4. **批量向量化是串行的。** `generate_batch_embeddings` 是 for 循环逐个 `await`，且五家 Provider 的批量协议各不相同（阿里云是 `input.texts` 数组、OpenAI 是 `input` 数组）。按 14,128 块估：串行按每次 ~0.2s 算是 **~47 分钟**；改成真批量（每次 25 条）约 **~5 分钟**，**差一个数量级**。这是接入时第一个要面对的工程问题，也是最有说服力的工程证据。
5. **死代码是潜在风险。** `memory_manager.py` 里 5 个零调用方法。RAG 正好给 `search_similar_learnings_async` 找到归宿；剩下 `search_similar_users_async` / `recommend_with_collaborative_filtering` 若仍不用，**考虑删掉**（与之前清 3 个零引用依赖是同一类动作）。
6. **原文有截断上限。** 抽样里出现长度正好 60000 的记录 —— 采集侧对 README 有截断。做块评测时要意识到**「答案不在库里」和「检索没召回」是两回事**，标注样本必须避开被截断的尾部。
7. **服务状态（已恢复）**：Qdrant 原本就在跑；Redis 容器 `Exited (255) 13 days ago`，已 `docker start` 拉起；PostgreSQL 不在 `docker-compose.yml` 里，是宿主机本地实例（`127.0.0.1:5432`，库 `ilo_agent`）。三个端口现已全部可达。

---

## 五、新代码量估算

| 模块 | 行数 |
|---|---|
| `modules/rag/chunker.py`（按 Markdown 标题切 → 段落 → 长度兜底 + 重叠） | ~150 |
| 块入库脚本（回读 `raw_content` → 切 → 批量向量化 → 写新集合） | ~120 |
| `modules/rag/retriever.py`（问题向量化 → Qdrant top-k → 复用 `QDRANT_READ` 熔断） | ~100 |
| `/chat` 改造（抽 `build_chat_prompt` + 注入片段 + 引用来源） | ~80 |
| 检索评测集 + 命中率指标（挂进 `evals/`） | ~200 |
| 测试 | ~150 |
| **合计** | **≈ 800 行** |

**占现有约 2 万行的 4%。**

这个数字是这次盘点最有价值的一条信息：**你缺的不是工程量，是那 800 行里「调参」的那部分经验** —— 切分长度调到多少、重叠留多少、为什么。这恰好是 `02-rag.md` §Q5 那个「你最后定的多少」追问唯一没法背的答案。

而现在你手上已经比「拍脑袋」多了一组真实约束：**14,760 个标题、84.7% 的文档落在 2k~30k 字、0.4% 零标题、77.2% 纯英文**。切分策略的初始值可以据此定，剩下的才是调。

