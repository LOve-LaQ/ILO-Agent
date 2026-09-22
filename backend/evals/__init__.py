# Evals - 输出质量评测体系
"""用数据集量化 LLM 输出质量，让 prompt 改动可回归验证（任务 A）。

这一层是项目中「三层输出质量闸门」之外的第四层：
- 确定性规则校验（summary_spec.is_chinese_text 等）
- prompt 硬约束（build_summary_prompt / build_digest_prompt 的写作要求）
- 多 Provider 降级（embedding / LLM 不可用时回退）
- **本层：把输出交给独立裁判打分，与基线对比，分数掉了不许合并**

目录约定：
- datasets/ 冻结的评测集 JSON（入库）
- judge.py   LLM-as-judge 打分实现
- runner.py  编排：跑评测集 -> 打分 -> 汇总 -> 产出报告
- baseline.json 基线分数（首跑后固化，入库）
- reports/   逐次报告产物（gitignore）

跑法见 README.md。默认 `pytest` 不会触发本目录的任何 LLM 调用。
"""
