# Coding Agent 任务书：Eval 评估体系 + 兴趣向量个性化推送

> 面向 coding agent 的两阶段任务。按顺序执行：先任务 A（Eval），再任务 B（个性化推送）。  
> 每个 Phase 完成后必须跑通全量测试再进入下一个；不确定的设计决策先列出选项暂停询问，不要擅自扩大改动面。

## 全局约束（两个任务共同遵守）

1. **契约边界**：所有新增 API 的响应模型必须进 `backend/src/schemas/`（Pydantic），路由一律声明 `response_model`；改了 schema 后前端执行 `npm run gen:api` 重新生成 `frontend/src/shared/api/schema.d.ts`，前端类型禁止手写。
2. **导入前缀**：后端导入统一用 `src.` 前缀，禁止混用（混用会导致模块双载、异常处理器失配）。
3. **优雅降级传统**：任何依赖（LLM / Qdrant / PostgreSQL / Redis）不可用时不能让接口 500，必须返回结构化的降级响应或回退到旧行为。
4. **测试离线可复现**：单测不得依赖真实 LLM/Qdrant 调用；embedding/LLM 用 mock 或占位实现。跑法：`cd backend && .\ilo\Scripts\python.exe -m pytest`。
5. **中文注释风格**：与现有代码一致——注释解释「为什么」，不解释「是什么」。
6. **配置**：新增配置项进 `src/core/config.py` 的 Settings + `.env.example`，不硬编码。

---

## 任务 A：输出质量 Eval 评估体系（优先级最高）

### 背景与目标

项目已有三层输出质量闸门（确定性规则校验 → prompt 硬约束 → 多 Provider 降级），但缺最后一层：**用数据集量化 LLM 输出质量，让 prompt 改动可回归验证**。本任务补齐第四层。

### 交付物

```
backend/evals/
├── __init__.py
├── datasets/                  # 评测集（JSON）
│   ├── summary_cases.json     # 摘要任务 15~20 条
│   └── digest_cases.json      # 中文导读任务 5~10 条
├── judge.py                   # LLM-as-judge：打分实现
├── runner.py                  # 编排：跑评测集 → 收集分数 → 生成报告
└── baseline.json              # 基线分数（首次跑分后固化）
```

外加：

- `backend/evals/README.md`：怎么跑、打分口径、基线怎么更新（保持纯 ASCII 无所谓，这是 md 可以中文）；
- `backend/tests/test_eval_gate.py`：eval 门禁测试（见下）。

### 详细规格

**1. 评测集**

- `summary_cases.json`：每条含 `id`、`input`（GitHub 仓库描述/标题，素材可从知识库导出真实数据，也可手写覆盖：纯英文描述、中英混合、信息极少、纯中文）、`kind`（repo/article）。
- `digest_cases.json`：每条含 `id`、`title`、`raw_description`、`readme_excerpt`（含一个「README 为空/过短」的边界样本）。
- 样本必须包含**已知的坏案例**（英文照抄、空洞开头、超长单句），用于验证 judge 能识别。

**2. Judge（judge.py）**

- 用 DeepSeek（复用 `src/modules/agent/state_machine.py` 的 `summary_llm`，或按同一 OpenAI 兼容协议新建 judge 客户端）当裁判。
- 对每个被评对象按 5 个维度打 1~5 分 + 一句话理由，输出严格 JSON：
  - `chinese`：中文化程度（无成句英文，专有名词豁免）
  - `informativeness`：信息量（讲清了是什么/解决什么）
  - `faithfulness`：无编造（只基于给定材料，没有材料外的功能/数字）
  - `format`：长度与结构合规（摘要 40~~90 字；导读 300~~500 字且分节）
  - `style`：无空洞开头、无截断、自然收尾
- judge prompt 里放正反例各 1~2 个锚定打分口径；JSON 解析失败要重试一次，仍失败则该条记 `judge_error` 不计分（不能让单条失败炸掉整轮）。

**3. Runner（runner.py）**

- 流程：读数据集 → 调真实摘要/导读链路（复用 `build_summary_prompt` / `build_digest_prompt` + LLM）→ judge 打分 → 汇总每维度均分与总均分 → 与 `baseline.json` 对比 → 输出 markdown 报告到 `backend/evals/reports/eval_<timestamp>.md`（含逐条得分表、总分、较基线的升降、不合格样本清单）。
- 支持 `--dataset summary|digest|all`、`--update-baseline`（显式更新基线）参数。
- 分数统计要区分 `judge_error`（不计入均分）和真实低分。

**4. 门禁测试（test_eval_gate.py）**

- 整个模块打 `@pytest.mark.eval` 标记，并在 `pyproject.toml`/`pytest.ini` 注册该 marker，**默认跳过**（Deselect），只有显式 `pytest -m eval` 才执行——避免日常 CI 花钱。
- 测试逻辑：跑小样本（≤5 条）→ 断言均分 ≥ baseline 总均分（容差 -0.2，允许 judge 波动）→ 断言没有任何维度均分跌破 3.0。
- README 里写清楚：改 prompt 后先 `pytest -m eval`，分数掉了不许合并。

**5. 自验证要求**

- eval 体系完成后，用它验证一条**已知结论**：现有中文化闸门放行的 summary 全部在 `chinese` 维度得 4 分以上；并人工抽看 3 份 judge 输出确认打分口径没漂移。

### 验收标准

- [ ] `pytest`（默认）不触发任何 LLM 调用，全量测试通过
- [ ] `pytest -m eval` 能跑通并产出 markdown 报告
- [ ] baseline.json 已固化首跑分数
- [ ] README 说明完整跑法

---

## 任务 B：Qdrant 兴趣向量个性化推送

### 背景与目标

当前推送/卡片列表的排序是随机抽样，Qdrant 向量检索没有真正进入主链路。本任务把「用户兴趣画像向量 → 候选卡片相似度排序」接入推送链路，并用受控分类标签（`CATEGORIES`）做辅助打散，让向量检索成为产品里的真实能力。

### 详细规格

**1. 用户兴趣画像向量**

- 新建 `src/services/interest_profile.py`（或合适位置）：
  - 画像来源：用户**已学习/已讲解的卡片**的 summary 向量，做加权聚合（近期的权重更高，简单做法：指数衰减或按时间窗截断，取均值/质心）；
  - 向量化复用 `src/modules/agent/embedding_service.py`（含占位向量兜底路径，勿绕开）；
  - 持久化：画像向量存 PostgreSQL（新表或新列，走 Alembic 迁移，命名规范跟随 `Base.metadata` 约定），并记录 `updated_at` 与来源卡片数；**不要**把画像只放 Redis（重启即失）也不放 Qdrant payload（它是标量对象的属性，不是检索集合成员）。
  - 冷启动：无任何学习记录 → 画像为空，推送回退到现有随机抽样行为（原路径保留，不许删）。

**2. 推送排序改造**

- 定位现在的随机抽样逻辑（推送/列表链路），改造为：
  1. 召回：候选卡片（当日抓取 + 库内近期卡片）；
  2. 排序：候选卡片的 summary 向量与用户画像向量算余弦相似度，取 top-k；
  3. 打散：用卡片的 `category`（受控词表）做同分类去重/间隔打散——相邻两张不同类，避免推送页全是同一技术栈；
  4. 可选加权：相似度分 × 类别新鲜度权重（用户该分类近期看多了就降权），实现从简，能讲清逻辑即可。
- 性能要求：排序在内存中对召回集算余弦即可（候选量小，不需要 Qdrant ANN 做服务端检索；Qdrant 的角色定位为「向量存储与批量查询」，在 README/注释里如实表述，**不要夸大成 ANN 服务检索**）。
- Qdrant 不可用 → 降级随机抽样 + 日志 warn；画像为空 → 同上。

**3. 契约与前端**

- 推送/列表响应模型新增字段：`recommend_score`（0~1 归一化）、`recommend_reason`（简短中文，如「与你最近学习的 RAG 方向相关」；随机回退时为 null）。
- schema 进 `src/schemas/discover.py`，挂 `response_model`，前端 `npm run gen:api` 重新生成。
- 前端卡片列表：有 `recommend_reason` 时在卡片上显示一行小字（样式从简）。

**4. 权限边界**

- 画像归属 `user_id` 一律取 access token 的 `sub`，与项目现有规则一致；未登录用户直接走随机抽样路径。

**5. 测试（全部离线可复现）**

- 画像聚合：mock embedding，验证加权/衰减逻辑与空画像降级；
- 排序：构造已知向量的候选集，断言排序结果与打散规则（相邻不同类）；
- 降级：Qdrant/embedding 不可用时回退随机抽样且接口不报错；
- 契约：新字段进 `tests/test_api_contract.py` 的既有断言风格。

### 验收标准

- [ ] 登录用户推送列表按画像相似度排序，未登录/画像为空回退随机
- [ ] Qdrant 或 embedding 服务不可用时接口正常返回（随机回退）
- [ ] Alembic 迁移可 upgrade/downgrade
- [ ] 全量测试通过，含新增测试
- [ ] `npm run gen:api` 后前端类型同步，卡片可展示推荐理由

---

## 执行顺序与汇报要求

1. 任务 A 全部验收 → 汇报报告样例与首跑分数 → 用户确认后再做任务 B。
2. 每完成一个 Phase：列出改动文件清单 + 测试结果摘要。
3. 任何「要不要新增依赖」「要不要改公开 API 形状」的决策，先停下来问。

