# Evals - LLM-as-judge 打分实现
"""按 5 个维度给模型输出打 1~5 分，输出严格 JSON。

两个刻意的设计：
1. **裁判与生成不同源**：生成侧是通义千问（`summary_llm`），裁判固定走 DeepSeek。
   同源裁判会带自偏好偏差，让 chinese/style 维度系统性偏高，评测就失去意义。
2. **客户端惰性创建**：模块级不建连。pytest 收集期会 import 本模块，若在顶层就
   `create_llm()`，等于在「默认 pytest 零 LLM 调用」这条契约上开一个口子。

单条判分失败不能炸整轮：JSON 解析失败重试一次，仍失败记 `judge_error` 不计分，
由调用方在汇总时排除。
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# 评测维度（顺序即报告列顺序）
JUDGE_DIMENSIONS = ("chinese", "informativeness", "faithfulness", "format", "style")

DIMENSION_LABELS = {
    "chinese": "中文化程度（无成句英文，专有名词与通用技术术语豁免）",
    "informativeness": "信息量（是否讲清是什么 / 解决什么问题）",
    "faithfulness": "无编造（只基于给定材料，没有材料外的功能、数字或对比结论）",
    "format": "格式合规（长度与结构符合该任务的硬约束）",
    "style": "文风（无空洞开头、无截断、自然收尾）",
}


class JudgeUnavailableError(RuntimeError):
    """裁判模型不可用（无 DEEPSEEK_API_KEY / 未安装 LangChain）时抛出，由上层降级为 skip"""


@dataclass(frozen=True)
class TaskSpec:
    """被评任务的规格：素材怎么交给裁判、格式硬约束、以及锚定打分口径的正反例"""

    name: str
    format_rule: str
    material_label: str
    examples: str


TASK_SPECS: Dict[str, TaskSpec] = {
    "summary": TaskSpec(
        name="summary",
        format_rule="摘要 40~90 个中文字符（按中文字数计），1~2 句、完整成句、自然收尾；素材确实单薄时略短可接受，但不得为凑字数编造。",
        material_label="GitHub 仓库 / 技术文章的一行原始描述（可能为英文）",
        examples=(
            "正例（chinese 5 分）：『AirCard 是一款面向 iOS 18+ 的 Apple Wallet 卡片皮肤工具，"
            "无需越狱即可自定义卡片外观。』——中文成句、讲清是什么与亮点。\n"
            "反例（chinese 1 分）：『Apple Wallet Card Skinner for iOS 18+ (No Jailbreak Required)』"
            "——直接照抄英文原文，未中文化。\n"
            "反例（informativeness/style 2 分）：『这是一个很棒的项目，可以帮助你提高效率。』"
            "——空洞开头、无信息量。\n"
            "反例（faithfulness 1 分）：写入材料中并不存在的性能数字或对比结论。"
        ),
    ),
    "digest": TaskSpec(
        name="digest",
        format_rule="导读总长 300~500 个中文字符，用 Markdown 组织（开头段总述 + 至少一个实质性小节）；素材不足而省略部分小节不扣分，但长度明显不足 300 字或完全未分节应扣分。",
        material_label="仓库标题、GitHub 描述与 README 原文摘录",
        examples=(
            "正例（5 分）：分节清晰、只基于 README、无编造数字。\n"
            "反例（chinese 1 分）：通篇英文或整段照抄 README 原文。\n"
            "反例（faithfulness 1 分）：写出 README 未提及的性能数字或对比结论（编造）。\n"
            "反例（format 2 分）：只有一段话、未分节，或长度远低于 300 字。"
        ),
    ),
}


# 裁判客户端惰性单例（模块级不建连）
_judge_llm = None


def get_judge_llm():
    """惰性创建独立裁判（DeepSeek）。不可用时抛 JudgeUnavailableError。"""
    global _judge_llm
    if _judge_llm is not None:
        return _judge_llm
    try:
        from src.modules.agent.state_machine import create_llm
    except Exception as e:  # noqa: BLE001 - 导入失败同样是「裁判不可用」
        raise JudgeUnavailableError(f"无法导入 LLM 客户端: {e}")

    llm = create_llm("deepseek")
    if llm is None:
        raise JudgeUnavailableError("裁判模型不可用：未配置 DEEPSEEK_API_KEY")
    # 判分要的是稳定性：create_llm 默认 temperature=0.7 会让同一段输出每次打分漂移，
    # 把门禁噪声放大到掩盖信号。这里只把这条【裁判实例】降到 0，不动生产默认值。
    try:
        llm.temperature = 0
    except Exception:  # noqa: BLE001 - 设置失败不应阻断判分
        pass
    _judge_llm = llm
    return _judge_llm


def build_judge_prompt(task: str, material: str, output: str) -> str:
    """构建裁判 prompt：素材与被评输出必须同时给出，否则 faithfulness 无从判断"""
    spec = TASK_SPECS[task]
    dims = "\n".join(f"- {k}: {DIMENSION_LABELS[k]}" for k in JUDGE_DIMENSIONS)
    return f"""你是严格的中文技术内容质量评审。请对下面这份「{spec.name}」输出逐维度打分。

被评对象是模型依据给定素材生成的中文内容，不是原始素材本身。

评分维度（每项给 1~5 的整数）：
{dims}

格式硬约束：{spec.format_rule}

判分锚点：
{spec.examples}

输出要求：
1. 只输出一个 JSON 对象，不要任何解释文字，不要 Markdown 代码块。
2. 严格格式：{{"chinese": 4, "informativeness": 4, "faithfulness": 5, "format": 4, "style": 4, "reason": "一句话总评"}}
3. faithfulness 只以「给定素材」为唯一依据：素材没提到的功能、数字、对比结论都算编造，必须扣分。

给定素材（{spec.material_label}）：
{material or "（无）"}

被评输出：
{output}
"""


def _parse_judge_json(text: str) -> Optional[Dict[str, Any]]:
    """从裁判输出里提取 JSON 对象；容忍代码块包裹与轻微格式噪声"""
    if not text:
        return None
    blob_text = text.strip()
    # 容忍 ```json ... ``` 包裹
    if blob_text.startswith("```"):
        blob_text = blob_text.strip("`")
        newline = blob_text.find("\n")
        if newline != -1:
            blob_text = blob_text[newline + 1:]
    start = blob_text.find("{")
    end = blob_text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    blob = blob_text[start:end + 1]
    try:
        data = json.loads(blob)
    except Exception:
        try:
            from json_repair import loads as repair_loads

            data = repair_loads(blob)
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _invoke_judge(prompt: str) -> str:
    """同步调用裁判模型，返回文本内容"""
    llm = get_judge_llm()
    result = llm.invoke(prompt)
    return getattr(result, "content", "") or ""


def judge_case(task: str, material: str, output: str) -> Dict[str, Any]:
    """给单条输出判分。

    返回 `{"scores": {维度: 分}, "reason": str}`；
    判分失败返回 `{"judge_error": str}`（调用方在汇总时排除，不让单条失败炸整轮）。
    维度缺失/非法也按失败处理——宁可记 judge_error，也不要把半截分数混进均分。
    """
    prompt = build_judge_prompt(task, material, output)
    last_err = ""
    for _ in range(2):  # 解析失败重试一次
        try:
            raw = _invoke_judge(prompt)
        except JudgeUnavailableError:
            raise
        except Exception as e:  # noqa: BLE001 - 模型/网络异常重试一次
            last_err = f"调用失败: {e}"
            continue

        data = _parse_judge_json(raw)
        if not data:
            last_err = f"JSON 解析失败: {(raw or '')[:120]!r}"
            continue

        scores: Dict[str, int] = {}
        invalid = None
        for dim in JUDGE_DIMENSIONS:
            val = data.get(dim)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                invalid = f"维度 {dim} 缺失或非数字: {val!r}"
                break
            value = int(round(float(val)))
            if not 1 <= value <= 5:
                invalid = f"维度 {dim} 超出 1~5: {val!r}"
                break
            scores[dim] = value
        if invalid:
            last_err = invalid
            continue

        return {"scores": scores, "reason": str(data.get("reason") or "").strip()}

    return {"judge_error": last_err or "未知错误"}


def aggregate(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总逐条判分：每维度均分 + 总均分。

    `judge_error` 与生成失败都不计入均分（否则「判分失败」会被误当成「质量差」）。
    """
    scored = [r for r in results if "scores" in r]
    errors = [r for r in results if "judge_error" in r]

    dimension_means: Dict[str, Optional[float]] = {}
    for dim in JUDGE_DIMENSIONS:
        vals = [r["scores"][dim] for r in scored]
        dimension_means[dim] = round(sum(vals) / len(vals), 3) if vals else None

    all_vals = [v for r in scored for v in r["scores"].values()]
    overall = round(sum(all_vals) / len(all_vals), 3) if all_vals else None

    return {
        "dimension_means": dimension_means,
        "overall": overall,
        "scored": len(scored),
        "judge_errors": len(errors),
    }
