# RAG 任务书：块索引 → 离线评测 → 接入问答 → 工具化

> 前置阅读：`docs/rag-现状盘点.md`（家底与坑）。
> 本任务书相对原始方案做了三处修正，**以本任务书为准**：
> 1. **顺序**：先离线拿数字（任务 A + B），数字成立才接入产品（任务 C）。重排、引用准确率放到最后，时间不够就砍。
> 2. **评测标签锚点**：原始方案的例子里写「答案块 = 块3」是错的 —— 换切分策略「块3」就不存在，对比表根本跑不出来。标签必须锚定在**原文的字符区间**上。
> 3. **跨语言**：实测原文 77.2% 纯英文、摘要 94.7% 中文。中文提问命中英文原文是本项目最大的风险，评测集必须单独分组。
>
> **顺序执行 A → B → C → D。每个任务跑通全量测试再进下一个。不确定的设计决策先列选项暂停询问，不要擅自扩大改动面。**

---

> 📌 **落地状态（2026-10-08）**：**任务 A、B 已完成**（C、D 未开始）。本任务书是**规格**，不是进度表 ——
> 实现时有两处偏离，以代码为准，避免按本文找文件找不到：
>
> | 本文写的 | 实际落地 | 说明 |
> |---|---|---|
> | `backend/evals/datasets/retrieval_cases.json` | `backend/retrieval_cases.json` | 放在 `backend/` 根，与 `backfill_card_chunks.py` 同级（切分脚本不在 `evals/` 下） |
> | `backend/evals/retrieval_runner.py` | `backend/retrieval_runner.py` | 同上；另多出 `build_retrieval_cases.py`（出题）与 `backfill_chunks_alt.py`（固定长度对照索引） |
> | `backend/evals/retrieval_baseline.json` | `backend/retrieval_baseline_heading.json` | **按 tag 分开存**，因为要跑多套策略对比，单文件会互相覆盖 |
> | `retrieve(query, *, top_k=20, card_id=None)` | `search(query, *, top_k=5, min_score=0.35, deadline, embed_service, collection)` | ① 阈值过滤在**检索之后**做（不用服务端 `score_threshold`，否则「库里没有」和「不够像」塌成一个观测）；② 加 `collection` 参数以支持多策略对比；③ `card_id` 场景暂未需要，未实现 |
> | `top_k` 默认 20 | 默认 5、上限 50 | 20 对「直接进提示词」太宽；要重排时再调大 |
>
> 其余约束（不复用 `tech_encyclopedia`、指纹、`QDRANT_READ` 熔断、`clamp_timeout` 预算、离线可复现单测）**全部遵守**。
> 进度与数字见 `docs/rag-开发计划.md`。

---

## 全局约束（四个任务共同遵守）

1. **不许复用 `tech_encyclopedia`**。卡片向量来自「结构化字段拼接」，块向量来自原文片段，两者不是同一语义空间。必须新建集合。
2. **块向量必须记模型指纹**。payload 里带 `embedding_fingerprint`（复用 `interest_profile.embedding_fingerprint()` 的格式 `provider:model:dim`）。现有卡片向量没有指纹，这是既有债，块这边不要再欠 —— 因为跨语言问题很可能逼你换模型。
3. **契约边界**：所有新增 API 的响应模型必须进 `src/schemas/`（Pydantic），路由一律声明 `response_model`；改了 schema 后前端执行 `npm run gen:api`，前端类型禁止手写。
4. **导入前缀**：后端导入统一用 `src.` 前缀，禁止混用。
5. **优雅降级**：检索不可用（Qdrant / embedding / 熔断打开）时，`/chat` 必须回退到**现有的「只注入会话上下文」行为**，接口不许 500，也不许编造。
6. **测试离线可复现**：单测不得依赖真实 LLM / Qdrant 调用；embedding 用 mock 或占位实现。跑法：`cd backend && .\ilo\Scripts\python.exe -m pytest`。
7. **中文注释风格**：与现有代码一致 —— 注释解释「为什么」，不解释「是什么」。
8. **配置**：新增配置项进 `src/core/config.py` 的 Settings + `.env.example`，不硬编码。
9. **复用既有韧性设施**，不要新造：检索走 `circuit_breaker.QDRANT_READ` 槽位 + `deadline.clamp_timeout` 预算 + `resilience.log_downstream_failure` 观测。

---

## 任务 A：块索引（切分 + 批量向量化 + 新集合）

### 背景

`raw_content` 里有 942 篇 README（1,229 万字、14,760 个标题）。它们现在**完全没有被切分**，只作为「先读原文」页的展示内容存在。本任务把它们切成块、向量化、写进新集合，为检索提供数据基础。

**实测约束（切分策略的初始依据）**：

| 事实 | 值 | 对策略的含义 |
|---|---|---|
| 零标题文档 | 4 / 942（0.4%） | 按结构切对 99.6% 的文档可行，**首选** |
| 标题层级 | h2 56.8% / h3 26.1% | 主切分边界取 **h1-h2**，超长再降 h3 |
| 每篇标题数 | 中位数 13，P90 28 | 按 h1-h2 切约 15 块/篇 |
| 长度分布 | 2k-8k 36.0%、8k-30k 48.7% | 单块 >2000 字要二次切，否则块太大主题混杂 |
| 原文有截断 | 最大正好 60000 | 采集侧有上限，见「已知限制」 |

### 交付物

```
backend/src/modules/rag/
├── __init__.py
├── chunker.py          # 切分：Markdown 标题层级 → 段落 → 长度兜底
└── chunk_spec.py       # 块集合常量 + payload 组装（与 tech_knowledge 的 COLLECTION 同风格）
backend/backfill_card_chunks.py   # 一次性入库脚本
backend/tests/test_chunker.py
```

### 详细规格

**1. `chunker.py`**

纯函数，无 IO，便于离线单测（与 `services/recommend.py` 的定位一致）。

- 三层逐级降级，**尽量晚地动用长度硬切**：
  1. 按 Markdown 标题切（主边界 h1-h2）；
  2. 单块超 `max_chars` 再降一级（h3），仍超则按段落（空行分隔）；
  3. 仍超才按长度硬切，**但必须在句子边界断开**（中文找 `。！？`、英文找 `. ! ?` 后的换行，向后找最近断点）。
- 相邻块留重叠，默认 15%（`overlap_ratio`）。重叠在**块尾**追加下一块的开头。
- 代码围栏（```）内的内容**不得作为切分边界** —— 按行切代码块会切出无法阅读的碎片。
- 每块返回元数据：`{text, heading_path, char_start, char_end, ordinal}`。
  - `heading_path`：如 `["快速开始", "安装"]`，喂模型时能说明「这段在原文哪个位置」。
  - `char_start` / `char_end`：**原文的字符区间**，这是任务 B 评测标签能跨策略对齐的前提，必须准确。
- 函数签名建议：
  ```python
  def chunk_markdown(text: str, *, max_chars: int = 2000,
                     overlap_ratio: float = 0.15,
                     min_chars: int = 100) -> list[dict]
  ```
- 过短的块（< `min_chars`）**合并到相邻块**，不要单独成块（避免「## License」这种孤立块污染召回）。

**2. 真批量向量化**

现状：`EmbeddingService.generate_batch_embeddings` 是 for 循环逐个 `await`，14,128 块按每次 ~0.2s 算要 **~47 分钟**。

- 新增 `embed_texts(texts: list[str]) -> list[list[float] | None]`，按 **Provider 各自的批量协议**发请求：
  - 阿里云：`input.texts` 是数组，**单次上限 25 条**；
  - OpenAI / Voyage / 智谱 / 百度：`input` 是数组，上限更大但**统一按 25 条切片**最省心；
  - `fallback` provider：直接返回全 `None`。
- 返回**与入参等长**的列表，失败位置是 `None`（**不要跳过**）—— 跳过会让调用方无法把向量和块对应起来，这是现有 `generate_batch_embeddings` 的隐患。
- 复用 `_post()`（已带重试 + 熔断），不要绕过。
- 保留 `generate_batch_embeddings` 的旧签名不动（可能有其它调用方），新方法并存。

**3. 块集合与入库脚本**

- 新集合名：`card_chunks`（常量放 `chunk_spec.py`），`VECTOR_SIZE = 1536`，`Distance.COSINE`。
- payload 字段：
  ```python
  {
      "card_id": str,            # 对应 collection_records.item_id
      "ordinal": int,            # 该卡内的块序号
      "heading_path": list[str],
      "char_start": int,
      "char_end": int,
      "source_url": str,         # 溯源用，复用 collection_records.source_url
      "title": str,
      "embedding_fingerprint": str,   # provider:model:dim
  }
  ```
  **不要把整块原文塞进 payload** —— 检索命中后回 PostgreSQL 按 `char_start/char_end` 取原文（或直接从返回值带出），这与 `tech_knowledge` 排除 `raw_content` 的理由一致。
- 点 ID：`md5(f"{card_id}:{ordinal}")` 前 16 位转 int，重复入库覆盖（与 `tech_knowledge.upsert` 一致）。
- 入库脚本 `backfill_card_chunks.py` **必须抄 `backfill_repos_30d.py` 的范式**：
  - `--dry-run`：只切分统计，**不调 embedding、不写库**，零成本先看数；
  - `--limit N` / `--card-id XXX`：小样本先跑通；
  - 幂等可重跑：已入库的卡按 `card_id` 跳过（除非 `--force`）；
  - `--rechunk`：换切分参数后重算（配合指纹变更）。
- 写入必须过 `qdrant_timeout_seconds(settings.qdrant_write_timeout)`，**不要按次传 float**（已知坑）。

### 已知限制（写进脚本 docstring，不要假装不存在）

- 原文最大 60000 字符，**采集侧有截断**。被截断的尾部内容不在库里。
- `>30k` 的 77 篇占字符量的大头，二次切要特别关注。

### 验收标准

- [ ] `pytest` 全量通过，新增 `test_chunker.py` 覆盖：三层降级各一条、代码围栏不切、`char_start/char_end` 与原文字符串切片一致、重叠生效、过短块合并
- [ ] `--dry-run` 输出的块数落在 **10,000~20,000** 区间（与盘点预估一致；偏离超过 30% 说明切分逻辑与预期不符，停下来查）
- [ ] `--limit 20` 小样本真实入库成功，块数与 dry-run 一致
- [ ] 全量入库完成，记录**实际耗时**与**退化向量条数**（应为 0；若不为 0 说明 embedding 有静默失败）
- [ ] 换一次 `max_chars` 参数（如 1200）重跑 `--dry-run`，确认块数变化方向符合预期

### 汇报要求

列出：改动文件清单、实际块数、入库耗时、退化向量条数、`--dry-run` 与实跑的差异。

---

## 任务 B：检索 + 离线评测（**本任务书的核心产出**）

### 背景与目标

任务 A 建好了块。本任务要回答的不是「能不能检索」，而是 **「切多长、要不要重叠、纯向量够不够」—— 用数字回答**。

`02-rag.md` §Q5 的追问「你最后定的多少」只能用真实数据回答。本任务就是产出那份数据。

### 交付物

```
backend/src/modules/rag/retriever.py
backend/evals/datasets/retrieval_cases.json     # 标注评测集
backend/evals/retrieval_runner.py               # 检索评测编排
backend/evals/retrieval_baseline.json           # 首跑基线
backend/tests/test_retriever.py
```

### 详细规格

**1. `retriever.py`**

- 接口：
  ```python
  async def retrieve(query: str, *, top_k: int = 20,
                     card_id: str | None = None) -> list[dict]
  ```
- 流程：`query` → 向量化 → Qdrant `query_points` → 返回 top-k（带 payload）。
- 复用 `circuit_breaker.QDRANT_READ`；Qdrant / embedding 不可用 → 返回 `[]` + warn 日志（**不是抛异常**）。
- `card_id` 可选：限定在某张卡内检索（用于「在这篇 README 里找」的场景，也是评测时缩小搜索空间的工具）。
- **不要**在这层做重排。重排是任务 C 之后的可选项，先证明初筛本身有效。
- 退化向量过滤：查询向量退化时直接返回 `[]`，复用 `services/recommend.is_degenerate_vector`。

**2. 标注评测集 `retrieval_cases.json`**

⚠️ **这是最容易做错的一步。标签必须锚定原文，不能锚定块号。**

```json
{
  "id": "r01",
  "card_id": "gh-1234567890",
  "question": "这个项目的向量维度上限是多少？",
  "answer_span": "Max vector dimension is 65536",
  "answer_char_start": 4210,
  "answer_char_end": 4247,
  "lang_pair": "zh_query_en_doc",
  "difficulty": "detail"
}
```

- `answer_span` 是**原文里的唯一文本片段**，`answer_char_start/end` 是它在 `raw_content` 里的位置。
  **有了这两个，换任何切分策略都能算出「检索结果里有没有覆盖这个区间」，对比表才跑得出来。**
- 命中判定：检索返回的 top-k 中，**存在任一块的 `[char_start, char_end)` 与答案区间有交集**。
- `lang_pair` 必须分组，这是本项目的核心变量：
  - `zh_query_en_doc`（中文问 / 英文原文）—— **预期是最大的一组，且最可能是弱项**；
  - `zh_query_zh_doc`（中文问 / 中文原文，122 篇里挑）；
  - `en_query_en_doc`（英文问 / 英文原文）—— 作为对照，用来隔离「跨语言」和「检索本身」两个因素。
- `difficulty`：`overview`（这项目是干什么的）/ `detail`（某个参数、某个数字）。
  **同一批数据按 difficulty 分组看命中率** —— 这是回答「切分粒度要不要跟查询粒度匹配」的唯一方式。
- **样本量：30~40 条**。不要只做 20 条：分组后每组不足 10 条，命中率的置信区间太宽，波动盖过信号。
- 抽样来源：从 942 篇里**按长度分层抽**（`2k-8k` 与 `8k-30k` 各占约一半），并**避开被截断的文档尾部**。
- 标注时**逐条人工核对 `answer_span` 确实在原文中存在且唯一**（用脚本验一遍，不要凭手感）。

**3. `retrieval_runner.py`**

- 流程：读评测集 → 对每个样本跑检索 → 按命中判定算命中率 → 分组汇总 → 输出 markdown 报告到 `backend/evals/reports/retrieval_<timestamp>.md`。
- 支持参数：
  - `--strategy {heading_h1h2, heading_h1h3, fixed_400, fixed_800, fixed_1200}`：**同一份评测集换切分策略重跑**；
  - `--top-k N`（默认 5，报告里同时输出 top-3 / top-5 / top-10）；
  - `--overlap 0|0.15`：有/无重叠对比；
  - `--mode {vector, hybrid}`：纯向量 vs 混合检索；
  - `--update-baseline`。
- **必须产出这四张表**（这就是这条链路的价值证据）：

| 表 | 变量 | 回答什么问题 |
|---|---|---|
| ① 切分策略 × 命中率 | heading_h1h2 / h1h3 / fixed 400/800/1200 | 「切多大？为什么优先按结构切？」 |
| ② 重叠 × 命中率 | 0% vs 15% | 「为什么要重叠？」 |
| ③ 纯向量 vs 混合 × 命中率 | vector / hybrid，**再按 `lang_pair` 拆** | 「只用向量够吗？」+ **跨语言差距有多大** |
| ④ 按 difficulty 拆的命中率 | overview / detail | 「切分粒度要不要匹配查询粒度？」 |

- **混合检索的关键词那一路**：用 PostgreSQL 全文检索实现（`to_tsvector` + `plainto_tsquery`），**不要引新依赖**。
  注意：英文原文用 `english` 配置；中文查询分词效果差，可对查询侧做**英文专有名词抽取**（正则提取 `[A-Za-z][A-Za-z0-9_\-.]{2,}`）后按 `simple` 配置查 —— 技术问题里的关键检索词通常是英文（`Qdrant` / `dimension` / `API`），这条路在本语料上预期收益明显。
- 两路结果融合用**按名次融合**（RRF），不要比绝对分数（量纲不同）。
- 报告里必须输出 `judge_error` 式的失败计数：**embedding 失败 / Qdrant 失败的样本数**，不能让下游故障伪装成「检索不准」。

### 验收标准

- [ ] `pytest` 全量通过，新增 `test_retriever.py`（mock Qdrant + embedding，覆盖降级返回 `[]`、退化向量、top-k 截断）
- [ ] 评测集 ≥ 30 条，三个 `lang_pair` 组都非空，`answer_span` 全部经脚本校验存在于原文
- [ ] 四张表全部产出，写入 `retrieval_<timestamp>.md`
- [ ] `retrieval_baseline.json` 已固化
- [ ] **结论段必须写清：哪一项收益最大、为什么** —— 这才是可讲的部分

### 汇报要求

把四张表 + 结论段贴出来。**不要只说「做完了」** —— 数字才是产出。

---

## 任务 C：接进 `/chat` + 引用溯源

### 前置

**只有任务 B 的四张表显示检索有效（命中率显著高于随机基线）才做本任务。** 如果命中率趴在地上，先回去修检索，接进产品只是把问题藏起来。

### 详细规格

**1. 抽 prompt builder**

现状：`/chat` 的 system_prompt 是写在路由函数里的内联 f-string（`src/api/routes/learning.py:500-518`）。

- 抽到 `src/modules/rag/chat_prompt.py` 的 `build_chat_prompt(topic, summary, core_concepts, history, chunks)`。
- **抽取后现有行为必须逐字不变**（先写一个测试锁住当前输出，再重构）。

**2. 注入检索片段**

`build_chat_prompt` 在有 `chunks` 时，按 `02-rag.md` §Q15 的三个要点组织：

- **标明边界**：每段资料标清起止 + 来源标识（`[资料1] 来自《{title}》`），否则模型分不清哪些是资料、哪些是用户的话。
- **要求引用**：明确要求回答里标注用了哪条资料。
- **给退路**（**最关键**）：「如果资料里没有答案，就直接说资料里没有，不要自己推断」。不给退路模型宁可编。

**3. 引用溯源**

- `ChatResponse` 新增 `citations`：`[{index, card_id, title, source_url, char_start, char_end}]`。
- schema 进 `src/schemas/`，挂 `response_model`，前端 `npm run gen:api`。
- 复用现有溯源能力（`GET /discover/cards/{id}/provenance`）的字段口径。

**4. 降级链（必须逐条实现）**

| 情况 | 行为 |
|---|---|
| Qdrant 不可用 / 熔断打开 | 回退「只注入会话上下文」，`citations` 为 `[]`，日志 warn |
| embedding 不可用 | 同上 |
| 检索返回空 | 同上（**不要**把空资料块拼进 prompt，那会让模型以为「资料说没有」） |
| 超时预算不足 | `clamp_timeout` 后剩余 ≤0 直接走现有 fallback |

### 验收标准

- [ ] 抽 builder 后，原有 `/chat` 行为逐字不变（有测试锁定）
- [ ] 检索可用时 prompt 含资料块；不可用时逐条走降级，接口不 500
- [ ] `citations` 进契约测试，前端类型已重新生成
- [ ] `pytest` 全量通过

---

## 任务 D：把检索器包成 Function Calling 工具（高价值，建议做）

### 为什么单列一个任务

目标 JD 里 **Function Calling 是硬缺口**，RAG 只是「优先」。而 `search_knowledge_base(query)` 正是最标准的工具形态 —— **一份工作同时补两个缺口**，且「RAG 怎么和 Agent 结合」是应用开发岗的标准考法。

**不接进主链路**，就一个脚本。

### 详细规格

| 步骤 | 内容 |
|---|---|
| 1 | 定义 2~3 个工具（`search_knowledge_base` / `get_card_detail` / `explain_concept`），写成工具清单给模型 |
| 2 | 让模型自己决定调用顺序，跑 10 个真实问题 |
| 3 | **记录失败案例**：选错工具几次、参数格式错几次、循环几次 |
| 4 | 加参数校验（模型生成的 JSON 是**不可信输入**）+ 最大轮次，再跑一遍对比 |

**复用现有资产**：`02-rag.md` / `10-jd-jiuru.md` §3.3 已指出，参数校验这套「模型返回的结构化 JSON 不可信」的处理，项目里已有四层逐级收窄的现成实现（提取 → 验结构 → 验语义 → 验完整性），直接迁移口径。

**产出的数字**：
- 不加校验时参数错误率 / 加了之后降到多少
- 最大轮次设几能覆盖全部正常问题
- 工具描述改一版，选错率从多少降到多少

### 验收标准

- [ ] 工具描述改过至少一版，有前后选错率对比
- [ ] 参数校验拦得住的错误类型有清单
- [ ] 最大轮次的确定有依据（不是拍脑袋写 5）

---

## 执行顺序与汇报要求

1. **A → B → C → D**，逐个来。**任务 B 是分水岭**：B 的数字不成立就不要做 C。
2. 每个任务完成后：列出改动文件清单 + 测试结果摘要 + 该任务的产出数字。
3. 任何「要不要新增依赖」「要不要改公开 API 形状」的决策，**先停下来问**。
4. 重排（rerank）与引用准确率**不在本任务书范围内** —— 那是 B 之后的可选延伸，时间不够就不做。省下的时间给混合检索。
5. **纪律**：任何数字必须是真跑出来的。评测集可以调，但调完要在报告里说明为什么调、调之前是多少。

---

## 附：本任务书相对 `02-rag.md` §三 的修正摘要

| # | §三 原方案 | 本任务书 | 理由 |
|---|---|---|---|
| 1 | 7 步顺序执行 | 压成 4 个任务，A+B 先行，C 以 B 的数字为前置 | 接入产品很便宜（注入点/原文/向量化都在），但**如果检索本身无效，接入只是把问题藏起来** |
| 2 | 评测标注「答案块 = 块3」 | 标注锚定**原文字符区间** | 块号随切分策略变化，用块号做标签则对比表无法产出 |
| 3 | 未提及跨语言 | 评测集按 `lang_pair` 分组，混合检索单独实测 | 实测原文 77.2% 纯英文、摘要 94.7% 中文，这是本项目最大风险 |
| 4 | 未提及块指纹 | 块 payload 必带 `embedding_fingerprint` | 跨语言问题很可能逼你换 embedding 模型，换则必须重算 |
| 5 | 未提 Function Calling | 单列任务 D | 目标 JD 的硬缺口，且检索器天然就是工具，一份工作补两个缺口 |
| 6 | 未提批量向量化 | 任务 A 明确要求真批量 | 14,128 块串行约 47 分钟，真批量约 5 分钟，差一个数量级 |
