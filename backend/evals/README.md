# 输出质量 Eval 评估体系

用数据集量化 LLM 输出质量，让 **prompt 改动可回归验证**。

这是项目「输出质量四层闸门」的最后一层：

| 层 | 机制 | 位置 |
| --- | --- | --- |
| 1 | 确定性规则校验 | `src/modules/discovery/summary_spec.is_chinese_text` 等 |
| 2 | prompt 硬约束 | `build_summary_prompt` / `build_digest_prompt` 的写作要求 |
| 3 | 多 Provider 降级 | embedding / LLM 不可用时回退 |
| 4 | **本层：独立裁判打分 + 基线对比** | `backend/evals/` |

## 怎么跑

```powershell
cd backend

# 1) 小样本冒烟（推荐先跑这个，最省钱）
.\ilo\Scripts\python.exe -m evals.runner --dataset all --limit 5 --no-report

# 2) 全量评测并产出报告（报告写到 evals/reports/eval_<timestamp>.md）
.\ilo\Scripts\python.exe -m evals.runner --dataset all

# 3) 首跑 / 承认质量变化时，固化新基线
.\ilo\Scripts\python.exe -m evals.runner --dataset all --update-baseline

# 4) CI 门禁（改 prompt 后必跑，分数掉了不许合并）
.\ilo\Scripts\python.exe -m pytest -m eval
```

- `--dataset summary|digest|all`
- `--limit N`：每个数据集最多跑 N 条（`0` = 不限）
- `--update-baseline`：把本次结果写回 `baseline.json`
- `--no-report`：不落盘，直接把报告打到 stdout

### 默认 `pytest` 不会花钱

`pytest.ini` 里配了 `addopts = -m "not eval"`，默认把 `eval` 标记的用例 **deselect**，
以维持项目「默认 `pytest` 零 LLM 调用」的既有契约。只有显式 `-m eval` 才真正调用模型。

## 裁判为什么用 DeepSeek 而不是 summary_llm

生成侧是通义千问（`summary_llm`）。若裁判也用同一个模型，会带 **自偏好偏差**，
让 `chinese` / `style` 维度系统性偏高，评测就失去意义。因此裁判固定走 DeepSeek
（复用 `state_machine.create_llm("deepseek")`），与生成模型**不同源**。

裁判不可用（无 `DEEPSEEK_API_KEY`）时，runner 与门禁测试都会 **skip**，而不是误报失败。

裁判会把 `create_llm("deepseek")` 那条**裁判实例**的 `temperature` 降到 **0**（不动生产默认 0.7）：
判分要的是可重复，temperature>0 会让同一段输出每次打分漂移，把门禁噪声放大到掩盖信号。

## 打分口径（5 个维度，各 1~5 分）

| 维度 | 含义 |
| --- | --- |
| `chinese` | 中文化程度（无成句英文，专有名词与通用技术术语豁免） |
| `informativeness` | 信息量（是否讲清是什么 / 解决什么问题） |
| `faithfulness` | 无编造（只基于给定材料，没有材料外的功能、数字、对比结论） |
| `format` | 格式合规（摘要 40~90 中文字符；导读 300~500 字且分节） |
| `style` | 文风（无空洞开头、无截断、自然收尾） |

- 裁判输出严格 JSON，解析失败重试一次；仍失败记 `judge_error`，**不计入均分**。
- 汇总时区分「判分失败」与「真实低分」——前者不能当成质量差。

## 门禁口径（`pytest -m eval`）

- 跑**全量**数据集（不用小样本）：单条换一次 judge 分，维度均分就跳 0.2+，噪声会盖过信号。
- **性质硬线**：`chinese`、`faithfulness` 两个维度不得跌破 **3.0**——语言不正确 / 在编造
  是产出直接不可用，属绝对底线，不随基线浮动。
- **回归线**：总均分与其余维度 >= 基线对应值 **− 0.4**。0.4 是连跑 3 次实测噪声定出来的
  （各维度 σ≈0.0~0.19，取 ~2σ），比拍脑袋写死更不容易假红。
- 基线本身是「当前 prompt 的真实水平」，不是理想值；门禁的职责是**防止改 prompt 改坏**，
  而不是把质量逼到某个绝对值。

## 数据集字段

`datasets/summary_cases.json`

```json
{
  "id": "s01",
  "kind": "repo",                        // repo | article
  "input": { "title": "...", "description": "...", "language": "...", "core_concepts": [] },
  "output": "（可选）",
  "label": "（可选）"
}
```

`datasets/digest_cases.json`

```json
{
  "id": "d01",
  "title": "...",
  "raw_description": "...",
  "readme_excerpt": "...",
  "output": "（可选）",
  "label": "（可选）"
}
```

- **不写 `output`**：runner 走**真实生产链路**生成（保证评的是线上真实输出）。
- **写了 `output`**：视为「已知坏案例标定样本」，跳过生成直接判分——这是验证
  「judge 能识别坏案例」的唯一可行路径（生产链路的闸门产不出坏样本）。
- `label` 非空的样本是**标定样本**：既不进基准均分、也不进「生成侧中文化闸门放行率」，
  只在报告里单独汇总（用于确认裁判区分度）。

### 判分素材必须与生成入参同一口径

裁判判 `faithfulness` 时的「事实边界」= 生成模型实际看到的素材。`runner` 里
`_summary_material` / `_digest_material` 必须与 `build_summary_prompt` /
`build_digest_prompt` 拼进去的字段**逐一对应**（摘要含 `core_concepts`，即
prompt 里的 topics）。少给一个字段，模型据此写出的技术点就会被误判为「编造」，
`faithfulness` 被系统性压低。

### 样本顺序约定

数据集**前若干条是代表性生成样本**，标定用的坏样本排在后面。门禁跑全量，
这个顺序主要留给开发时 `--limit N` 快速冒烟（先抽到可生成样本，结果才有意义）。

## 怎么加样本 / 更新基线

1. 往 `datasets/*.json` 追加条目（可从知识库导出真实素材，但导出后要**冻结进仓库**，
   保证离线可复现）。
2. 跑 `--update-baseline` 固化新基线，并把 `evals/reports/` 下的报告作为证据留存
   （报告目录已 gitignore，不入库）。
3. 提交 `datasets/` 与 `baseline.json` 的变更。

## 与「中文化闸门」的呼应

`is_chinese_text` 闸门放行的 summary，理论上 `chinese` 维度应 >= 4。runner 会在报告里
输出「生成侧中文化闸门放行 N/M」这一行，便于人工抽看确认打分口径没有漂移。
