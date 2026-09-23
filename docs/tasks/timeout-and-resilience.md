# Coding Agent 任务书：下游调用超时与韧性加固

> 面向 coding agent 的单任务书。**先读「现状诊断」理解问题全貌，再按 Phase 1 → 4 顺序执行。**
> 每个 Phase 完成后必须跑通全量测试再进入下一个；不确定的设计决策先列出选项暂停询问，不要擅自扩大改动面。
>
> 关联文档：`docs/agent-tasks.md`（本项目既有任务书，全局约束与本文件一致）

---

## 全局约束

沿用 `docs/agent-tasks.md` 的六条，并追加本任务专属的第 7~10 条：

1. **契约边界**：新增 API 响应模型必须进 `backend/src/schemas/`，路由一律声明 `response_model`；改 schema 后前端执行 `npm run gen:api`，前端类型禁止手写。
2. **导入前缀**：后端导入统一用 `src.` 前缀，禁止混用（混用会导致模块双载、异常处理器失配）。
3. **优雅降级传统**：任何依赖（LLM / Qdrant / PostgreSQL / Redis）不可用时不能让接口 500，必须返回结构化降级响应或回退到旧行为。
4. **测试离线可复现**：单测不得依赖真实 LLM / Qdrant 调用；embedding / LLM 用 mock 或占位实现。跑法：`cd backend && .\ilo\Scripts\python.exe -m pytest`。
5. **中文注释风格**：与现有代码一致 —— 注释解释「**为什么**」，不解释「是什么」。
6. **配置**：新增配置项进 `src/core/config.py` 的 Settings + `.env.example`，不硬编码。
7. **不新增依赖**：本任务的所有能力（重试、退避、熔断、deadline）都必须用标准库 + 已有的 redis / httpx 实现。**特别地：不要引入 `tenacity` 并到处加 `@retry`**（理由见「反面清单」）。
8. **默认测试零 LLM 调用**：`pytest.ini` 的 `addopts = -m "not eval"` 这条契约不能破。
9. **超时值必须可配**：所有新增超时/重试参数走 Settings，不得在调用点写魔法数字。
10. **诚实标注**：如果某个 Phase 的某个子项无法完成（例如依赖第三方 SDK 不支持某参数），**不要用近似实现假装完成**，在汇报里明确列出「未完成项 + 原因 + 替代方案」。

---

## 一、现状诊断

> 以下全部经过代码核实，带 `文件:行号`。**这不是推测清单，是已确认的事实。**

### 1.1 问题分级总览

| 编号 | 问题 | 级别 | 影响 |
|---|---|---|---|
| **P0-1** | 5 处 `ChatOpenAI` 构造均**无 `timeout` / `max_retries`** | 🔴 P0 | 一次挂起的 LLM 调用可阻塞数分钟 |
| **P0-2** | **2 处 LLM 调用在 `async def` 里同步执行**，独占事件循环 | 🔴 P0 | 并发一上来整个服务雪崩 |
| **P1-1** | **无任何请求级超时预算**（无 deadline 传递） | 🟠 P1 | 单请求总耗时可无限增长，网关先超时，用户拿到 502 |
| **P1-2** | 重试策略**零散且不一致**：embedding / 抓取 / 讲解完全无重试 | 🟠 P1 | 瞬时抖动被当成永久失败，降级过激 |
| **P1-3** | `tenacity` 在 `requirements.txt` 里但**零使用** | 🟠 P1 | 遗留依赖，误导维护者以为有统一重试 |
| **P2-1** | **无熔断**：下游持续故障时每个请求仍会尝试一次 | 🟡 P2 | 故障期间白白消耗超时预算 |
| **P2-2** | 超时/重试/降级**无指标输出**，只靠 `logger.warning` | 🟡 P2 | 无法量化「现在降级率是多少」 |
| **P2-3** | `QdrantClient(url=...)` 未显式配置 timeout | 🟡 P2 | 使用客户端默认值，不可控 |

### 1.2 逐项证据

#### P0-1 · LLM 客户端无超时

`src/modules/agent/state_machine.py` 中 **5 处** `ChatOpenAI(...)` 构造，参数一律只有 `model` / `openai_api_key` / `openai_api_base` / `temperature` / `max_tokens`：

```
:65   create_llm() → DeepSeek 分支
:77   create_llm() → OpenAI 分支
:94   create_llm(provider="deepseek")
:109  create_llm(provider="openai")
:119  create_llm() → 自定义 provider
:140  create_qwen_llm() → Qwen
```

**后果**：`ChatOpenAI` 底层走 openai SDK，未指定时使用 SDK 默认超时（量级为**分钟**）。一次网络半开或上游挂起，调用方会一直等。

#### P0-2 · 两处 LLM 调用阻塞事件循环

全部 LLM 调用点（`grep -rn "\.invoke(" src/`）：

| 位置 | 方式 | 阻塞事件循环 | 超时 |
|---|---|---|---|
| `src/api/routes/learning.py:479` （`/learning/chat`） | `llm.invoke(system_prompt)` 同步调用，所在函数为 `async def chat_with_ai` | 🔴 **是** | ❌ |
| `src/modules/agent/state_machine.py:209` （`ExplanationEngine.generate`） | `llm.invoke(prompt)` 同步调用，所在函数为 `async def generate`，被 `start_learning` await | 🔴 **是** | ❌ |
| `src/modules/discovery/github_fetcher.py:290` （批量摘要） | `await asyncio.to_thread(summary_llm.invoke, prompt)` | ✅ 已卸载 | ❌ |
| `src/services/collection_service.py:629` （中文导读） | `await asyncio.to_thread(summary_llm.invoke, prompt)` | ✅ 已卸载 | ❌ |

**后果**：FastAPI 的 async 路由跑在单一事件循环上。同步 `invoke` 期间，**该 worker 上所有其他请求（包括 `/health`）全部排队**。一次 5 秒的 LLM 调用 = 5 秒内所有并发请求卡住。

**历史关联（重要，避免改回错误方案）**：`state_machine.py` 有一段注释记录了历史 bug —— 讲解原本用 `asyncio.to_thread` 包裹 `ExplanationEngine.generate`，但该函数是 **async 协程**，`to_thread` 拿到的是**未 await 的协程对象**，导致 `/learning/response` 响应校验失败（`string_type`）。当时改成直接 `await` 修好了校验，**但代价是变成了同步阻塞**。

> ⚠️ 所以本次修法的方向必须是：**保持 `generate` 是 async（调用方继续 `await`），在 `generate` 内部把同步的 `llm.invoke` 卸载到线程池**。**不要**退回「用 `to_thread` 包 `generate`」——那会把历史 bug 重新引入。

#### P1-1 · 无请求级超时预算

`grep -rn "TimeoutMiddleware|asyncio.wait_for|anyio.fail_after|deadline" backend/src/` → **零命中**。

当前已有的超时（全部是「单次调用」级别的固定值，互相独立）：

| 下游 | 现状 | 位置 |
|---|---|---|
| Redis | `socket_timeout=1.0` + `socket_connect_timeout=1.0` | `src/core/redis_client.py:43-44` |
| Qdrant 健康检查 | 2.0s | `src/api/main.py:88` |
| 验证码校验 | 5.0s（`VERIFY_TIMEOUT_SECONDS`） | `src/services/captcha_service.py:31,132` |
| SMTP | 10s | `src/services/email_service.py:189` |
| Embedding | `httpx.AsyncClient(timeout=30.0)` | `src/modules/agent/embedding_service.py:82` |
| 抓取 | httpx 20~30s | `article_fetcher.py:47`、`engine.py:38`、`github_fetcher.py:66,202,226`、`simplified_engine.py:23` |
| LLM | **无** | — |

> 上表是**诊断时的快照**。其中 `engine.py` 与 `simplified_engine.py` 后来已作为死代码删除
> （见第七节），这两行仅作历史记录保留。

**问题**：这些值是「每层各自拍的」，**没有从请求入口往下传递的预算**。`/learning/chat` 一次请求最坏情况 = Redis 1s + PG 3s + LLM（无界）→ 总耗时不可控。

另外 `httpx.AsyncClient(timeout=30.0)` 是**单值**，会同时作用于 connect / read / write / pool 四个阶段 —— 连接建立和读取响应共享同一个 30s，语义上应该拆开（连接快失败、读取给足）。

#### P1-2 · 重试策略零散

现有「重试」只有三处（`grep -rn "for _ in range"` + 人工确认）：

| 位置 | 策略 | 说明 |
|---|---|---|
| `src/modules/discovery/github_fetcher.py:301` `summarize_items` | 整批失败 → 逐条重试 1 次 | ✅ 合理，保持 |
| `backend/evals/judge.py:173` | JSON 解析失败重试 1 次 | ✅ 合理，保持 |
| `src/api/routes/learning.py:102` `get_memory_manager` | 初始化失败 → 30s 退避重试 | ✅ 是 **init 重试**，不是调用重试 |

**完全无重试的**：
- **Embedding**（`embedding_service.py:268` `generate_embedding`）：API 失败直接返回 `None` → 上层落占位向量。但 embedding 是**入库链路的关键**，瞬时抖动不该直接降级。
- **抓取**（`github_fetcher` / `article_fetcher`）：外网请求，抖动最常见，却零重试。
- **LLM 生成**（chat / 讲解）：`except Exception` → 直接降级文案。用户在前端等，一次抖动就得到「服务不可用」。

#### P2-1 · 无熔断

下游持续故障（例如 DeepSeek 挂了 30 分钟）时，**每个请求仍会发起一次调用并等到超时**。在 P0-1 未修的情况下，这会持续占用事件循环。

#### P2-3 · Qdrant 客户端未配超时

`src/modules/agent/memory_manager.py:50`：`self.qdrant_client = QdrantClient(url=qdrant_url)` —— 无 `timeout` 参数。

---

## 二、目标策略

> **一句话**：**分层超时预算 + 场景化重试 + 快速失败降级 + 熔断兜底。**
> 核心原则是「**该重试的重试，不该重试的立刻降级**」——而不是给所有调用统一加一层 retry。

### 2.1 超时预算表

**单次调用超时**（新增配置项，全部进 Settings）：

| 下游 | connect | read | 单次总超时 | 配置键 |
|---|---|---|---|---|
| Redis | 0.5s | 0.5s | 1.0s（保持） | `redis_socket_timeout` |
| PostgreSQL | — | — | 3.0s | `db_statement_timeout_ms` |
| Qdrant 读 | 1.0s | 3.0s | 4.0s | `qdrant_timeout` |
| Qdrant 写 | 1.0s | 5.0s | 6.0s | `qdrant_write_timeout` |
| Embedding | 5.0s | 15.0s | 20.0s（从 30s 收紧） | `embedding_timeout` |
| 抓取（外网） | 5.0s | 20.0s | 25.0s（从 30s 收紧） | `fetch_timeout` |
| **LLM 对话 / 讲解** | 5.0s | 45.0s | **50.0s** | `llm_chat_timeout` |
| **LLM 批量摘要** | 5.0s | 90.0s | **95.0s** | `llm_summary_timeout` |
| **LLM judge** | 5.0s | 30.0s | **35.0s** | `llm_judge_timeout` |
| 验证码 | 2.0s | 3.0s | 5.0s（保持） | `captcha_timeout` |
| SMTP | 3.0s | 7.0s | 10.0s（保持） | `smtp_timeout` |

> **为什么 LLM 摘要的 read 给到 90s**：`summarize_items` 的 `batch_size=20`，一批 20 条要生成结构化 JSON，长是正常的。**但必须有界** —— 90s 是「最长合理时间」，不是「随便等」。

**请求级总预算**（新增，从入口往下传）：

| 端点 | 总预算 | 说明 |
|---|---|---|
| `GET /discover/news` | 5s | 只读缓存 + 向量排序，超了就该降级 |
| `GET /discover/cards/{id}/digest` | 40s | 含一次 LLM 导读生成 |
| `POST /learning/chat` | 60s | LLM 50s + 余量 |
| `POST /learning/session` | 15s | 只建会话，不该调 LLM |
| `POST /learning/response` | 60s | 含讲解 LLM 调用 |
| `POST /discover/refresh` | **不设长预算** | 见 2.5，这类长任务应改为异步队列 |

### 2.2 重试矩阵

> **判定三问**：① 幂等吗？② 是瞬时错误吗？③ 重试的代价可接受吗？三个都是「是」才重试。

| 场景 | 重试 | 策略 | 理由 |
|---|---|---|---|
| LLM 对话 / 讲解 | ✅ | 1 次，退避 0.5s | 用户在等，重试必须有界 |
| LLM 批量摘要 | ✅ 已有 | 整批失败 → 逐条 1 次 | 保持现状，不重复实现 |
| LLM judge | ✅ 已有 | JSON 解析失败 1 次 | 保持现状 |
| **Embedding** | ✅ **新增** | 2 次，退避 0.5s / 1.5s | 入库链路关键，抖动不该直接降级 |
| **抓取（外网）** | ✅ **新增** | 2 次，退避 1s / 3s | 外网抖动最常见 |
| Qdrant 读 | ⚠️ | 1 次，然后降级到随机抽样 | 个性化是尽力而为 |
| Qdrant 写 | ❌ | 记 warning，靠下一轮采集补 | 写入失败不阻塞主流程 |
| Redis | ❌ | 直接降级（内存 / PG） | 热缓存，降级比重试划算 |
| PostgreSQL | ❌ | 直接抛错 | **真相源不可用是严重故障，不该被重试掩盖** |
| 验证码校验 | ❌ | 直接 fail-closed 拒绝 | 安全优先，重试会拉长攻击窗口 |

**只对以下错误重试**（其余一律不重试）：

```
✅ httpx.ConnectError / httpx.ConnectTimeout / httpx.ReadTimeout
✅ HTTP 429 / 500 / 502 / 503 / 504
✅ asyncio.TimeoutError
❌ HTTP 4xx（除 429）—— Key 错了、参数错了，重试一万次也一样
❌ 任何 JSON 解析错误（重试只会得到同样的坏输出，除非是 judge 那种瞬时截断）
❌ 业务异常（ILOException）
```

**退避必须带抖动**：`delay = base * (2 ** attempt) + random(0, base)`。没有抖动的话，一批并发请求会同时重试，形成惊群。

### 2.3 deadline 传递（P1-1 的解法）

不要在每层各设一个固定超时。正确做法：

```
请求入口 → 创建 deadline（monotonic 时间点）→ 存入 request.state
   ↓ 逐层传递「剩余预算」
每一层调用前：remaining = deadline - now()
   if remaining <= 0: 直接走降级路径（不发起调用）
   else: 用 min(该层配置超时, remaining) 作为本次超时
```

**为什么必须传剩余预算**：如果每层都用「自己的配置值」，Redis 1s + PG 3s + LLM 50s = 最坏 54s，而且这是**没考虑重试**的数字。有了 deadline，重试也受同一个总预算约束 —— 重试不会把总耗时撑爆。

### 2.4 熔断（P2-1 的解法）

- **适用范围**：LLM、Embedding、Qdrant（外部依赖，故障可持续）
- **不适用**：PostgreSQL（真相源，熔断没有意义，直接报错更诚实）
- **实现**：**不引入新依赖**，用 Redis 计数器（与 `src/core/rate_limit.py` 同款模式）
- **状态机**：
  ```
  CLOSED  → 连续失败 >= N（默认 5）→ OPEN
  OPEN    → 直接降级，不再发起调用；M 秒（默认 30）后 → HALF_OPEN
  HALF_OPEN → 放行 1 个探测请求；成功 → CLOSED；失败 → OPEN
  ```
- **为什么用 Redis 而不是进程内变量**：多实例部署时熔断状态必须共享，否则每个实例各自熔断、各自半开，等于没有熔断。

### 2.5 关于 `/discover/refresh` 这类长任务（重要判断）

抓取 + 多批摘要，单次请求可能要几分钟。**不要靠「把超时设得足够长」来解决** —— 那只是把问题推给网关和用户。

正确方向：改为**异步任务**（接口立即返回 `batch_id`，前端轮询批次状态）。本项目已经有 `collection_service.start_batch` / `finish_batch` / 批次表，**状态机已经具备**，只需要把执行挪到后台。

> 本任务书**不要求**实现这个改造（改动面太大），但要求在 `refresh` 路由的 docstring 里**如实标注**「当前为同步执行，长任务应改为异步队列」，避免后续维护者误以为这是最终形态。

---

## 三、Phase 划分与详细规格

### Phase 1（P0）：LLM 超时 + 事件循环卸载

**这是最高优先级，必须单独提交、单独验证。**

**1.1 给 LLM 客户端加超时**

改 `src/modules/agent/state_machine.py`：

- `create_llm()` 的 5 个 `ChatOpenAI(...)` 构造点，全部加 `timeout=` 与 `max_retries=0`
- `create_qwen_llm()` 同理
- `create_llm` 增加 `timeout: Optional[float] = None` 形参，未传时读 Settings 默认值

> **为什么 `max_retries=0`**：openai SDK 自己带重试（默认 2 次）。如果 SDK 偷偷重试，我们的超时预算就失控了 —— **重试必须由我们统一控制**，否则「50s 超时」实际可能是 150s。

**1.2 卸载事件循环阻塞**

| 文件:行 | 现在 | 改为 |
|---|---|---|
| `src/api/routes/learning.py:479` | `response = llm.invoke(system_prompt)` | `response = await asyncio.to_thread(llm.invoke, system_prompt)` |
| `src/modules/agent/state_machine.py:209` | `result = llm.invoke(prompt)` | `result = await asyncio.to_thread(llm.invoke, prompt)` |

> ⚠️ **`state_machine.py:209` 这一处务必看清上下文**：`generate` 本身是 `async def`，调用方 `start_learning` 用 `await self.explanation_engine.generate(...)`。**只改函数体内部这一行**，不要改调用方，不要把 `generate` 变回同步 —— 那会重新引入 1.2 节记录的历史 bug。
>
> 改完后必须验证：`generate` 返回的是 `str` 而不是协程对象（历史 bug 的表征是响应校验报 `string_type`）。

**1.3 配置项**

`src/core/config.py` Settings 新增：

```
llm_chat_timeout: float = 50.0
llm_summary_timeout: float = 95.0
llm_judge_timeout: float = 35.0
```

同步更新 `backend/.env.example`。

**Phase 1 验收**

- [ ] `grep -rn "ChatOpenAI(" backend/src/` 每处都有 `timeout=` 和 `max_retries=0`
- [ ] `grep -rn "\.invoke(" backend/src/` 确认**没有任何**调用点在 `async def` 里裸调同步 `invoke`
- [ ] `python -m pytest` 全量通过
- [ ] `state_machine.py` 的 `__main__` 自测块仍能跑通（`python src/modules/agent/state_machine.py`）
- [ ] `/learning/response` 的 `explanation` 字段仍能通过 Pydantic 响应校验（**重点回归历史 bug**）

---

### Phase 2（P1）：统一重试与退避

**2.1 新建 `src/core/resilience.py`**

纯函数 + 无 IO 的编排层，便于离线单测：

```python
async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    jitter: bool = True,
    retry_on: tuple[type[BaseException], ...] = (),
    deadline: Optional[float] = None,   # monotonic 时间点
    label: str = "",
) -> T
```

必须满足：
- 只对 `retry_on` 里列出的异常重试，其余原样抛出
- 退避带抖动
- **每次重试前检查 `deadline`，剩余不足则不再重试**（与 Phase 3 联动）
- 重试过程输出结构化日志（`label`、第几次、原因）

**2.2 接入点**

| 位置 | attempts | base_delay | retry_on |
|---|---|---|---|
| `embedding_service._request_embedding` | 2 | 0.5 | `httpx.TransportError`, `asyncio.TimeoutError` |
| `github_fetcher` 抓取 | 2 | 1.0 | 同上 + HTTP 5xx |
| `article_fetcher` 抓取 | 2 | 1.0 | 同上 |
| Qdrant 读（`interest_profile.build_user_profile` 取向量） | 1 | 0.3 | Qdrant 客户端异常 |

**2.3 明确「不重试」并写清理由**

在 `resilience.py` 顶部 docstring 里列出**不重试的场景和理由**（Redis / PG / 验证码 / Qdrant 写），避免后续维护者「顺手补上」。

**Phase 2 验收**

- [ ] `tests/test_resilience.py` 覆盖：成功首试、瞬时失败后成功、重试耗尽、**不该重试的异常不重试**、deadline 不足时不重试
- [ ] 全量 `pytest` 通过（mock 掉真实网络）
- [ ] 无新增第三方依赖（`requirements.txt` 不新增行）

---

### Phase 3（P1）：请求级 deadline

**3.1 新建 `src/core/deadline.py`**

```python
def new_deadline(budget_seconds: float) -> float   # 返回 monotonic 时间点
def remaining(deadline: Optional[float]) -> float  # 剩余秒数，无 deadline 返回 inf
def is_expired(deadline: Optional[float]) -> bool
def clamp_timeout(configured: float, deadline: Optional[float]) -> float
    # 返回 min(configured, remaining)，已过期返回 0
```

**3.2 中间件注入**

在 `src/api/middleware.py` 增加（或扩展既有 `RequestContextMiddleware`）：按路由从 Settings 取预算 → `request.state.deadline = new_deadline(budget)`。

**3.3 各层调用前检查**

- LLM 调用：`timeout = clamp_timeout(settings.llm_chat_timeout, deadline)`；为 0 则**直接走降级文案，不发起调用**
- Redis / Qdrant / Embedding：同理
- 重试：把 `deadline` 透传给 `with_retry`

**Phase 3 验收**

- [ ] 新增测试：构造「剩余预算 0.1s」的 deadline，断言 LLM 调用**没有真的发起**（用 mock 断言调用次数为 0）而是直接降级
- [ ] 新增测试：`clamp_timeout` 的边界（无 deadline / 已过期 / 剩余小于配置值）
- [ ] 全量 `pytest` 通过

---

### Phase 4（P2）：熔断与观测

**4.1 新建 `src/core/circuit_breaker.py`**

Redis 计数实现（参照 `src/core/rate_limit.py` 的固定窗口写法）。接口：

```python
def is_open(scope: str) -> bool
def record_success(scope: str) -> None
def record_failure(scope: str) -> None
```

- Redis 不可用 → **fail-closed 到「不熔断」**（即视为 CLOSED），因为熔断是保护措施，不该因保护机制本身故障而阻断主流程
- 配置项：`circuit_failure_threshold`（默认 5）、`circuit_open_seconds`（默认 30）

**4.2 接入**：LLM、Embedding、Qdrant 的调用点前置 `is_open` 检查，调用后 `record_success` / `record_failure`。

**4.3 观测**

统一结构化日志字段，便于后续接指标系统：

```
event=downstream_timeout  downstream=llm_chat  elapsed_ms=50123  timeout_s=50  request_id=...
event=downstream_retry    downstream=embedding  attempt=2  reason=ReadTimeout  request_id=...
event=downstream_fallback downstream=llm_chat  reason=timeout  request_id=...
event=circuit_open        downstream=llm_chat  failures=5  request_id=...
```

**Phase 4 验收**

- [ ] 测试：连续 N 次失败后 `is_open` 为真，且**不再发起真实调用**
- [ ] 测试：Redis 不可用时 `is_open` 返回 False（不阻断主流程）
- [ ] 测试：HALF_OPEN 探测成功后恢复 CLOSED
- [ ] 全量 `pytest` 通过

---

## 四、整体验收标准

### 4.1 必须通过

- [ ] `cd backend && .\ilo\Scripts\python.exe -m pytest` 全量通过，**且默认不产生任何 LLM 调用**
- [ ] `.\ilo\Scripts\python.exe -m pytest -m eval` 仍可跑通（本任务不应影响 eval 体系）
- [ ] `requirements.txt` **无新增依赖**
- [ ] `src/core/config.py` 与 `backend/.env.example` 配置项一一对应，无遗漏
- [ ] 所有新增超时/重试参数均可在 `.env` 覆盖，无硬编码魔法数字

### 4.2 静态自查（必须贴出命令输出作为证据）

```bash
cd backend

# ① 所有 LLM 客户端都有超时
grep -rn "ChatOpenAI(" src/ -A 10 | grep -c "timeout="     # 应等于构造点数量

# ② 没有任何 async 函数里裸调同步 invoke
grep -rn "\.invoke(" src/                                   # 逐个确认调用点所在函数

# ③ 没有残留的 tenacity
grep -rn "tenacity\|@retry" src/ requirements.txt           # 应为空
```

### 4.3 行为验证（**最关键，必须做**）

**验证 P0-2「事件循环不再被阻塞」** —— 这是本任务的核心收益，必须用实验证明：

```
步骤：
1. 启动后端（`python src/api/main.py`）
2. 并发发起 5 个 `POST /learning/chat`（可用脚本并发，或 curl 后台）
3. 在 chat 请求进行中，连续请求 `GET /health`
4. 记录 /health 的响应时间

修复前预期：/health 被阻塞，响应时间 ≈ chat 的 LLM 耗时
修复后预期：/health 保持毫秒级响应，不随 chat 并发数劣化
```

**验证 P0-1「超时真的生效」**：

```
步骤：
1. 制造「连接挂起」，而不是「凭证错误」——见下方勘误
2. 请求 `POST /learning/chat`
3. 计时

修复前预期：长时间挂起（分钟级）
修复后预期：≈ 50s（配置值）内返回降级文案，接口不 500
```

> **⚠️ 勘误（实测后修正）**：本步骤最初写的是「把 DEEPSEEK_API_KEY 改成格式合法但无效的值」。
> 这条**证明不了超时** —— 无效 Key 会立刻拿到 401，请求在毫秒级就返回了，
> 无论有没有配 timeout 结果都一样。要验证超时，必须让**连接建立后迟迟不返回**。
>
> **正确做法：黑洞端口**。起一个只 `listen`、不 `accept`、不应答的本地 TCP 端口，
> 把 `openai_api_base` 指向它。连接会一直挂着，从而真正压到超时分支：
>
> ```python
> import socket, threading
> srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(8)
> port = srv.getsockname()[1]
> conns = []                      # 必须持有引用，否则 accept 结果被 GC 后连接立刻关闭
> threading.Thread(target=lambda: conns.append(srv.accept()[0]), daemon=True).start()
> ```
>
> **实测结果（客户端默认 50s）**：
>
> | 场景 | 实测耗时 | 结果 |
> |---|---|---|
> | 默认（`llm_chat_timeout=50.0`） | **50.0s** | `APITimeoutError`，返回降级文案，HTTP 200 |
> | 显式 `timeout=5.0` | **5.0s** | 同上 |
>
> 顺带确认了一条本任务的关键依赖：**langchain 的 `ChatOpenAI` 支持按次传超时**
> （`llm.invoke(prompt, timeout=4.0)` 会透传到 openai SDK 的 `create()`）。
> 没有这条，Phase 3 的 deadline 夹取就只能靠每次新建客户端，代价大得多。

### 4.4 汇报要求

每个 Phase 完成后汇报：
1. **改动文件清单**（含行号）
2. **测试结果摘要**（通过数 / 新增用例）
3. **4.2 静态自查的命令输出**
4. **未完成项**（若有）—— 必须写清原因与替代方案，不允许静默跳过

---

## 五、反面清单（不要做的事）

> 这些都是本任务中最容易被「顺手做错」的地方，每条都对应一个真实风险。

| ❌ 不要 | 为什么 |
|---|---|
| 引入 `tenacity` 然后到处 `@retry` | 会把「**哪些不该重试**」这个设计意图淹没。重试必须是**显式的、场景化的**，写在调用点旁边 |
| 用 `asyncio.wait_for` 包 `asyncio.to_thread` 就以为解决了阻塞 | `wait_for` 超时**不会杀掉正在跑的线程**，线程仍在后台消耗资源。正确做法是把超时**传进被调用的函数**（如 `llm.invoke` 的 `timeout` 参数） |
| 把 LLM 超时设成 300s「避免失败」 | 只是把雪崩推迟。超时的意义是**尽快失败并降级**，不是等到底 |
| 退回「用 `to_thread` 包 `ExplanationEngine.generate`」 | 会重新引入历史 bug（协程对象未被 await → 响应校验 `string_type` 失败）。见 1.2 节 |
| 给 PostgreSQL 加熔断 | 真相源不可用是严重故障，熔断会掩盖问题。应该直接报错 |
| 给验证码校验加重试 | 安全优先场景应 fail-closed，重试拉长攻击窗口 |
| 用 `except Exception: pass` 吞掉超时异常 | 超时是必须被观测的信号，至少 `logger.warning` + 结构化字段 |
| 重试时不检查 deadline | 重试会把总耗时撑爆，deadline 的存在意义就是约束重试 |
| 把 `/discover/refresh` 的超时改大来「解决」超时 | 长任务的正确解法是异步化，不是延长超时。本任务只要求如实标注现状 |
| 为了测试通过而 mock 掉重试逻辑本身 | 重试逻辑是本任务的交付物，必须被真实测试覆盖 |

---

## 六、执行顺序

```
Phase 1（P0）→ 全量测试 + 4.3 行为验证 → 汇报 → 【等用户确认】
Phase 2（P1）→ 全量测试 → 汇报 → 【等用户确认】
Phase 3（P1）→ 全量测试 → 汇报 → 【等用户确认】
Phase 4（P2）→ 全量测试 → 汇报
```

**每个 Phase 之间必须停下来等确认**，不要一口气做完四个 —— Phase 1 的行为验证结果可能会改变后续 Phase 的设计（例如如果实测发现 LLM 超时应该更短，配置默认值要调整）。

**任何「要不要新增依赖」「要不要改公开 API 形状」「要不要调整现有超时默认值」的决策，先停下来问。**

---

## 七、实施状态（执行后追加，2026-09-23）

> 本节记录**执行结果**，不改动上面的规格。目的是让下一个读者不必重新考古。

**四个 Phase 全部完成**，全量 `pytest`：`170 → 251 passed`（2 deselected 为 `-m eval`）。
`requirements.txt` 无新增依赖，并移除了遗留的 `tenacity==8.2.3`。

| Phase | 状态 | 新增文件 | 新增用例 |
|---|---|---|---|
| 1 · LLM 超时 + 事件循环卸载 | 完成 | — | — |
| 2 · 统一重试与退避 | 完成 | `src/core/resilience.py` | `tests/test_resilience.py` |
| 3 · 请求级 deadline | 完成 | `src/core/deadline.py` | `tests/test_deadline.py` |
| 4 · 熔断与观测 | 完成 | `src/core/circuit_breaker.py` | `tests/test_circuit_breaker.py` |
| 2.1 预算表收口 | 完成 | — | `tests/test_timeout_budget.py` |

**2.1 超时预算表已全部落地为配置项**（原表只有 LLM 三项有配置键，其余是散落的字面量）：

```
REDIS_SOCKET_TIMEOUT=1.0      DB_STATEMENT_TIMEOUT_MS=3000
QDRANT_TIMEOUT=4.0            QDRANT_WRITE_TIMEOUT=6.0
QDRANT_HEALTH_TIMEOUT=2.0     EMBEDDING_TIMEOUT=20.0
FETCH_TIMEOUT=25.0            CAPTCHA_TIMEOUT=5.0
SMTP_TIMEOUT=10.0
```

`tests/test_timeout_budget.py::test_no_hardcoded_timeout_literals_in_src` 会扫描 `src/`，
任何 `timeout=<数字>` 都直接判失败 —— 把 4.1「无硬编码魔法数字」变成可回归的约束。

**执行中发现、原规格未预见的问题（均已处理）**

1. **4.3 的 P0-1 验证步骤有缺陷** —— 见上方勘误。
2. **`with_retry_sync` 是必需的补充**：规格只给了 async 版签名，但 Qdrant 读在同步上下文。
3. **`should_retry` 谓词是必需的补充**：「HTTP 5xx 重试、4xx 不重试」要看 status_code。
4. **重试必须包住真正的 IO 调用**：`_request_embedding` / `get_vectors_by_ids` 自己吞异常返回
   `None` / `{}`，在它们外层加 retry 完全无效 —— 实际要下沉到 `http_client.post` / `qdrant.scroll`。
5. **`summary_llm` 回退路径会串档**：`create_qwen_llm() or llm` 在无 `ALIYUN_API_KEY` 时
   拿到对话档 50s，而批量摘要应有 95s。已改为回退时用 `create_llm(timeout=llm_summary_timeout)` 重建。

**执行中发现的既有问题**

- ✅ **已处理**：`src/modules/discovery/engine.py`（`NewsDiscoveryEngine`）与
  `simplified_engine.py`（`SimplifiedNewsDiscoveryEngine`）**全项目零引用**，且
  `engine.py` 依赖的 `feedparser` 在 Python 3.13 上已无法导入（其 `encodings.py`
  仍 `import cgi`，而 `cgi` 自 3.13 起被移除）—— 既不可用也无人用。
  两个模块连同只被它们使用的 `feedparser` / `lxml` / `beautifulsoup4` 三个依赖一并删除。
- ⬜ **未处理**：`Settings` 里 `max_tokens` / `num_questions` / `request_rate_limit`
  三个字段零引用。没有直接删，是因为 `max_tokens=4096` 与 `num_questions=3` 这两个值
  在 `api/routes/learning.py` 里是**硬编码**的（`{"max_tokens": 4096, "num_questions": 3}`），
  看起来更像是「该接线却忘了接线」而不是「该删的废字段」——
  正确的修法是让那两个值读 Settings，而不是删掉配置项。

---

## 八、独立核验结果（第三方复测，2026-09-23）

> 本节由**未参与实施**的一方复测，不采信第七节的自测数字。所有结论均来自重新执行的命令与探针。

### 8.1 静态核验（全部通过）

| 检查项 | 命令 | 结果 |
|---|---|---|
| LLM 客户端超时覆盖 | `grep -rn "ChatOpenAI(" src/` | 6 处构造，**6 个 `timeout=` + 6 个 `max_retries=0`** ✅ |
| 事件循环卸载 | `grep -rn "\.invoke(" src/` | 4 个调用点**全部** `await asyncio.to_thread(..., timeout=...)` ✅ |
| 历史 bug 未回归 | `grep -n "async def generate" src/modules/agent/state_machine.py` | 仍是 `async def`，调用方仍 `await` ✅ |
| 遗留依赖 | `grep -n tenacity requirements.txt` | 已移除 ✅ |
| 全量测试 | `pytest -q` | **251 passed, 2 deselected**（eval 门禁按契约跳过）✅ |
| 新增测试 | `pytest tests/test_{deadline,circuit_breaker,resilience,timeout_budget}.py` | **81 passed** ✅ |
| 配置落地 | `git show 683a9ee -- .env.example` | 19 个新键（3 LLM + 5 deadline + 2 熔断 + 9 超时）全部进 `.env.example` 与 Settings ✅ |

### 8.2 行为核验（独立探针，用「永不响应的本地假上游」逼出超时）

| 验证项 | 期望 | 实测 | 判定 |
|---|---|---|---|
| 构造期 `timeout=2` 生效 | ≈2s 失败 | `APITimeoutError` @ **2.05s** | ✅ |
| **按次传 `timeout=2` 生效** | ≈2s 失败 | `APITimeoutError` @ **2.01s** | ✅ |
| **LLM 调用期间事件循环不被阻塞** | 心跳不中断 | LLM 调用 **50.02s** 期间，最大卡顿 **38ms**，心跳 **1614** 次 | ✅ |
| deadline 预算 5s | ≈5s 返回降级 | **5.10s** 返回降级文案 | ✅ |
| deadline 预算 0 | 立即降级、不发起调用 | **0.002s**，未撞下游 | ✅ |
| 熔断（阈值 3） | 前 3 次撞下游，之后短路 | 第 1~3 次 2.01/2.02/2.02s 且撞下游；第 4~6 次 **0.00s 且不撞下游** | ✅ |

**关于「按次传 `timeout`」这一项的意义**：实施说明里提到「langchain 的 `ChatOpenAI` 支持按次传超时」。
这是本方案的关键技术依赖 —— 若该假设不成立，`clamp_timeout` 算出的动态预算就完全无效，
全部退化为「客户端构造期的固定超时」。本项已独立证实成立，**该假设可以放心依赖**。

**关于事件循环的量化意义**：修复前，一次 LLM 调用会独占事件循环，最大卡顿 ≈ 调用总耗时（本例为 50000ms 量级）。
修复后最大卡顿 **38ms**（仅为 asyncio 调度粒度 + GC），**改善了约 1300 倍**。这是本任务的核心收益。

### 8.3 观测事件核验（Phase 4 交付物）

探针运行期间实际落地的结构化事件（日志原文）：

```
event=downstream_timeout  downstream=llm_chat scope=explanation elapsed_ms=50006 timeout_s=50.0
event=downstream_fallback downstream=llm_chat scope=explanation reason=timeout elapsed_ms=50006
event=downstream_retry_exhausted downstream=... attempts=... reason=...
event=circuit_open downstream=llm_chat failures=3 open_seconds=30
event=circuit_open downstream=llm_chat action=fallback scope=explanation
```

四类事件（`downstream_timeout` / `downstream_retry*` / `downstream_fallback` / `circuit_open`）均按设计触发 ✅

### 8.4 ⚠️ 复测新发现：`/discover/news` 仍有同步阻塞（本次未修）

**这是第三方复测发现的、原规格与实施都未覆盖的问题。**

完整调用链（全部为同步，且**没有任何 `to_thread`**）：

```
async def get_recommended_news        (api/routes/discover.py:195)
  └─ _personalized_items(...)          (api/routes/discover.py:157)  ← 同步 def，非 async
       ├─ build_user_profile(...)      (services/interest_profile.py:276)  ← 同步
       │    └─ get_vectors_by_ids(...) (modules/discovery/tech_knowledge.py:306)  ← 同步
       │         └─ with_retry_sync(...)                                    ← time.sleep(0.3) 重试
       └─ kb.candidate_points(...)     (modules/discovery/tech_knowledge.py:226)  ← 同步 Qdrant 调用
```

`grep -n "to_thread" api/routes/discover.py` → **零命中**。

**影响**：与 P0-2 是同一类问题（async 路由里做同步 IO），只是量级更小 ——
单次约 0.1~1s（DB 查询 + Qdrant scroll），若触发重试再叠加 ~0.3~0.45s 的 `time.sleep`。
它不会像 LLM 那样阻塞几十秒，但 `/discover/news` 是首页接口、QPS 最高，**阻塞会直接体现为并发下的 P95 劣化**。

**责任归属（如实记录）**：这是**本任务书 Phase 1.2 的范围缺口** ——
该节的表格只列了 2 个 LLM 调用点，没有覆盖「非 LLM 的同步 IO 也在 async 路由里」这一类。
实施方按规格执行，无过失。

**建议修法（Phase 5 候选）**：
1. 把 `_personalized_items` 改为 `async def`，内部用 `await asyncio.to_thread(build_user_profile, ...)`；
2. `candidate_points` 同理；
3. 或更彻底：`/discover/news` 的整体数据获取（含 `kb.sample`）统一走 `to_thread`；
4. 加一条与 `test_timeout_budget.py` 同风格的**静态守卫**：扫描 `api/routes/*.py`，
   在 `async def` 内出现的已知同步 IO 调用（`build_user_profile` / `candidate_points` /
   `get_vectors_by_ids` / `\.scroll(` / `\.sample(`）直接判失败 —— 把「别在 async 里同步 IO」
   变成可回归约束，而不是靠人记得。

> **通用教训**：这次修的是「LLM 调用阻塞事件循环」，但同类问题在项目里**不止 LLM 一处**。
> 只按「LLM 调用点」这个维度去搜，必然会漏掉同步的 DB / 向量库 / 文件 IO。
> 正确的搜索维度是「**`async def` 函数体内出现了哪些同步 IO 调用**」，而不是「哪些地方调了 LLM」。
