# RAG Chat A/B - 「注入前 vs 注入后」回答质量对照实验
"""补上 P4 门禁的第二条：**端到端回答质量真的变好了吗**。

## 为什么需要这个脚本

P4 的门禁写了两条：

1. 接进去之后**原有行为不变** —— ✅ 已锁（契约测试 + 27 例单测）。
2. **端到端回答质量优于「只有会话上下文」** —— ❌ 一直没测。

于是留下一个说不清的地方：我能证明「**检索按预期注入、失败只降级不报错**」，
证明不了「**回答变好了**」。而命中率 hit@5 = 77.8% 意味着**约 1/4 的问题正确块不在前 5**
→ 会注入不相关的资料 → 而**注入错上下文比不注入更糟**（模型会照着错料一本正经地编）。
这个风险接进 `/chat` 之后是**实际存在**的，不是理论上的。

所以这个脚本要回答的就是一句话：**把检索接进去，回答到底变好了没有？**

## 实验设计

同一批问题，跑**两组**，其余全部相同：

| 组 | 系统提示词 | 说明 |
|---|---|---|
| **A 关** | 只有【学习内容】（主题/简介/核心概念） | 等价于 P4 之前的旧行为 |
| **B 开** | 【学习内容】+【参考资料】（检索到的块） | 现在的生产行为 |

### ⚠️ 关键约束一：`temperature=0` **不等于**确定性

我最初以为把两组都按 `temperature=0` 跑，差异就只来自「有没有资料」。
**实测是错的**：同一个 prompt 连发两次，DeepSeek 给出的是两段不同的回答
（MoE 的专家路由 + 服务端批处理都会引入不确定性，`temperature=0` 只能压住采样随机性）。

所以这个脚本**不能**靠「确定性」来消噪，只能靠**重复**：

- `--repeat k`：每条题每组各跑 k 次，按**均值**比较。k=1 时只适合冒烟，别拿来下结论。
- 报告里同时给出**组内标准差**（同一组重复之间的波动）—— 它是这轮测量的**噪声地板**。
  组间差值如果没超过噪声地板，就不能说「变好了」。

### ⚠️ 关键约束二：不是每条题都会注入 —— 这既是限制，也是内置的安慰剂

没过 0.35 阈值的题，B 组的系统提示词和 A 组**逐字相同**。于是数据天然分两个子集：

- **注入子集**：A/B 有实质差异 → 效果该出现在这里；
- **未注入子集**：A/B 的系统提示词完全一样 → 两组测的**是同一个东西**，
  差值就是**纯粹的噪声**（生成噪声 + 裁判噪声）。

**安慰剂子集的作用因此是双重的**：它既验证「效果来自资料而非裁判漂移」，
又给出了一个**不依赖假设的噪声估计**。

**结论成立的判据**：注入子集的差值**明显大于**未注入子集的差值。否则不成立。

**一个能自我证伪的实验设计，比一个好看的数字值钱。**

## 判分

**裁判与生成不同源**（沿用项目既有纪律）：生成侧是 DeepSeek（和生产 `/chat` 一致），
裁判侧走 Qwen。同源裁判会带自偏好偏差。

裁判看到的是「问题 + 参考材料 + 被评回答」，**不知道哪个是 A 组哪个是 B 组**（盲评）。

### ⚠️ 关键约束三：判分依据**不能**直接用 `answer_excerpt`

第一版我拿评测集里的 `answer_excerpt` 当参考答案。**跑出来是错的**：

`answer_excerpt` 是**检索锚点**，不是完整答案 —— 它常常只是一句**引出句**。
实测 c001 问「AirCard-iOS 的 ipa 包可以用哪些工具侧载安装？」，
锚点是 `Install AirCard-iOS.ipa using your preferred sideloading method:`，
**真正的工具列表在后面几行**。拿它当唯一事实依据，**答对的模型会因为「列出了材料里
没有的工具名」被判成编造** —— 两组一起被压分，主维度直接失去区分度。

所以改成：**回原文截一个围绕答案区间的窗口**（`--gold-window`，默认前后各 600 字符）。

为什么是「原文窗口」而不是「检索到的那几块」：
- 原文窗口**与检索结果无关**，两组拿到的是同一份 → 不构成循环论证；
- 如果用 B 组检索到的块当答案依据，那就变成「B 组照着自己看到的材料答，当然一致」，
  测出来的不是能力提升，是同义反复。

三个维度（1~5）：

| 维度 | 判什么 |
|---|---|
| `correctness` | 有没有答到点上（对照参考材料；答非所问、关键结论矛盾 = 1~2 分） |
| `specificity` | 有没有给出**具体**信息（具体工具名、参数、数值、机制），还是空泛套话 |
| `no_fabrication` | 有没有**具体的**事实断言是材料里不存在的（标识符 / 配置项 / 数值 / API 名 / 行为承诺） |

> `no_fabrication` 的判据是「**具体的**」：泛泛的背景常识（「侧载需要签名」）不算编造，
> 但**捏造一个看起来像真的具体名字或数字**就是编造。
>
> 冒烟时抓到过一个真例子：c002 问「检测不到 PowerPoint 产物被标成什么」，
> 标准答案 `unverified draft`；**A 组（无检索）编出了一个不存在的标识符
> `pptx_unverified`**，B 组答对了 `unverified`。这正是「没有资料时模型会硬编一个具体名字」。

## 结果（2026-10-08 已跑，见 `rag_chat_ab_report.md`）

全量 36 条 × 重复 3 次。**注入子集（n=35）：总分 8.01 → 13.03（+5.019）**，
**B 胜 30 / 平 1 / 输 4**，精确符号检验 **p ≈ 6×10⁻⁶**；组内 σ 0.902 / 0.432（**效应 ≈ 5 倍噪声**）。
安慰剂子集（n=1）11.0 → 10.0、σ 2.944 / 2.16 —— 与纯噪声一致，设计成立。

**第二刀（`--crosstab`，零模型调用可复现）**：与 `retrieval_baseline_heading.json` 按 hit@5 关联 ——
**命中组（n=27）B 胜率 96.3% / Δ +6.210**；**未命中组（n=8）B 胜率 50.0% / Δ +0.999**；
**B 输掉的 4 条里 3 条正是检索没命中的**。→ **效果来自「检索找对了」，不是「多塞了几段字」**，
并**第三次指向「该上重排」**。

> ⚠️ 报数三条口径：① 题是读过原文出的 → **上界不是平均收益**，别说成「线上提升 33%」；
> ② n=36 小样本；③ 生成只用一个模型。**能证明「系统按设计工作、方向正确」，不能证明「线上平均收益」。**

## 运行

    cd backend

    # 0) 先确认 Qdrant 起来了（脚本会自检，不可达会直接退出而不是白跑）
    docker compose up -d qdrant

    # 1) 冒烟：3 条、重复 1 次（最省钱，先确认链路通）
    .\\ilo\\Scripts\\python.exe rag_chat_ab.py --limit 3

    # 2) 只生成不判分（更省钱，用肉眼看回答差异）
    .\\ilo\\Scripts\\python.exe rag_chat_ab.py --limit 3 --no-judge --show-answers

    # 3) 正式跑：全量 36 条 × 重复 3 次 + 落盘报告
    .\\ilo\\Scripts\\python.exe rag_chat_ab.py --repeat 3 --out rag_chat_ab_report.md

    # 4) 诊断：打印裁判理由，看它到底在扣什么
    .\\ilo\\Scripts\\python.exe rag_chat_ab.py --limit 5 --show-reasons

    # 5) 换判分依据的窗口大小（默认前后各 600 字符）
    .\\ilo\\Scripts\\python.exe rag_chat_ab.py --gold-window 1000 --limit 5

成本：每条题 2k 次生成 + 2k 次判分。`--repeat 3` 全量 36 条约 432 次调用，
粗估 **¥3~8**（DeepSeek + Qwen-plus 单价都低）。Qdrant 本地、embedding 只算 36 次，可忽略。

## 这个实验**不能**证明什么

- 它证明的是「**在这 36 条题上**，注入让回答更好」。题目是读过原文出的（agent-grounded），
  所以和命中率一样，**是乐观上界** —— 真实用户的问题不会这么贴合语料。
- 它不覆盖**追问**场景（短追问现在检索不到东西，见 `02-rag.md` §3.9）。
- 裁判是模型，不是人。**结论看的是两组的差，不是绝对分。**
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

# 与 rag_chat_probe.py 同一处理：显式锚到脚本目录，从哪运行都对。
load_dotenv(Path(__file__).resolve().parent / ".env")

from src.core.config import settings  # noqa: E402
from src.modules.agent.state_machine import create_llm, create_qwen_llm  # noqa: E402
from src.modules.rag.chat_context import (  # noqa: E402
    MAX_CONTEXT_TOKENS,
    build_chat_context,
)
from src.modules.rag.retriever import ChunkHit, hydrate_texts  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "retrieval_cases.json"
# 交叉分析的两个输入：本实验的报告 + 检索命中基线。都在仓库里，不花钱重跑。
REPORT_PATH = Path(__file__).resolve().parent / "rag_chat_ab_report.md"
BASELINE_PATH = Path(__file__).resolve().parent / "retrieval_baseline_heading.json"

# 判分维度（顺序即报告列顺序）。三个都只对照「参考答案」判，不依赖别的材料。
JUDGE_DIMENSIONS = ("correctness", "specificity", "no_fabrication")

# 单条满分（用于报告里写 x/15）
MAX_TOTAL = 5 * len(JUDGE_DIMENSIONS)

DIMENSION_LABELS = {
    "correctness": "正确性（有没有答到问题上；答非所问或关键结论与材料矛盾给 1~2 分）",
    "specificity": "具体性（给出了具体工具名/参数/数值/机制，还是只有空泛套话）",
    "no_fabrication": "无编造（没有材料里不存在的**具体**事实：标识符、配置项、数值、API 名、行为承诺）",
}


# --------------------------------------------------------------------------- #
# 提示词装配
# --------------------------------------------------------------------------- #


def _session_for(case: Dict[str, Any]) -> Dict[str, Any]:
    """把一条评测用例还原成「用户在某个卡片会话里提问」的上下文。

    `topic` 取仓库名 —— 这是用户真实会看到的卡片标题。`summary` / `core_concepts`
    留空是**故意**的：给得越少，A 组越接近「模型只知道主题、不知道内容」，
    两组差异越纯粹地归因于检索到的正文。给了摘要反而会让 A 组沾到光，
    把效果稀释掉。
    """
    return {"topic": case.get("repo", ""), "summary": "", "core_concepts": []}


def _shrink_ladder(half: int) -> List[int]:
    """窗口收窄阶梯：`[half, half//2, half//4, ..., 0]`。

    最后那个 0 不是随便加的 —— 它表示「窗口就是答案区间本身」。
    而答案区间当初是**从原文里唯一定位出来的**，所以它必然落在原文范围内，
    也就是说**阶梯的最后一级一定能取到正文**。这让函数有确定的收敛保证。
    """
    out: List[int] = []
    h = half
    while h > 0:
        out.append(h)
        h //= 2
    out.append(0)
    return out


def _gold_reference(case: Dict[str, Any], half_chars: int) -> str:
    """取判分用的参考材料：**原文里围绕答案区间的一个窗口**。

    【为什么不能直接用 answer_excerpt】它是**检索锚点**，不是完整答案。
    实测 c001 的锚点是「Install AirCard-iOS.ipa using your preferred sideloading method:」
    —— 后面那几行才是真正的工具列表。拿它当唯一事实依据，**答对的模型会因为
    「列出了材料里没有的工具名」被判成编造**，两组一起被压分，主维度失去区分度。

    【为什么要逐级收窄】答案靠近文末时，`+half` 会越过原文长度，回填会以
    `span_out_of_range` 拒绝。**一次失败就退回窄锚点是不对的** —— 那恰好会在
    最需要宽窗口的样本上退回最弱的依据。所以按 `_shrink_ladder` 逐级收窄，
    直到取到正文；阶梯最后一级（窗口 = 答案区间）必然成立。

    【为什么用原文窗口而不是检索到的块】原文窗口与检索结果无关，两组拿到同一份，
    不构成循环论证。若拿 B 组检索到的块当依据，就变成「B 照着自己看到的材料答，
    当然一致」—— 测出来的不是能力提升，是同义反复。

    Args:
        half_chars: 答案区间前后各取多少字符；<=0 表示退回用 `answer_excerpt`。

    Returns:
        参考材料文本。阶梯全部失败时**退回 `answer_excerpt`** ——
        宁可依据弱一点，也不要让这条样本因为取不到材料而整条作废。
    """
    excerpt = case.get("answer_excerpt", "")
    span = case.get("answer_span")
    if half_chars <= 0 or not span:
        return excerpt
    for half in _shrink_ladder(half_chars):
        start = max(0, int(span[0]) - half)
        end = int(span[1]) + half
        hit = ChunkHit(
            card_id=case["card_id"], ordinal=0, score=1.0, char_start=start, char_end=end
        )
        hydrated = hydrate_texts([hit])
        text = hydrated[0].text if hydrated else None
        if text:
            return text
    return excerpt


def _build_judge_prompt(question: str, gold: str, answer: str) -> str:
    """裁判 prompt：问题 + 参考材料 + 被评回答。

    **不告诉裁判这是哪一组** —— 盲评是这轮结论能站住的前提。一旦裁判知道
    「这份是有资料辅助的」，它就会不自觉地给高分（期望偏差）。
    """
    dims = "\n".join(f"- {k}: {DIMENSION_LABELS[k]}" for k in JUDGE_DIMENSIONS)
    return f"""你是严格的技术问答质量评审。请对下面这份「回答」逐维度打分。

评分维度（每项给 1~5 的整数）：
{dims}

判分口径（请严格按此执行）：
- 【参考材料】是从原文里截取的一段，它是**事实依据**，但**不一定涵盖问题的全部答案**
  —— 它可能只是一句引出句，答案在后文；也可能混有与本题无关的相邻段落。
- `correctness`：回答有没有答到这个问题上。答非所问、把问题理解错了、
  或给出的关键结论与参考材料**矛盾**，给 1~2 分。
- `specificity`：回答有没有给出**具体**信息（具体工具名、参数、数值、机制、步骤），
  还是只有「视情况而定」「建议查阅文档」这类空泛套话。
- `no_fabrication`：回答里有没有**具体的**事实断言是参考材料中不存在的 ——
  例如具体标识符、配置项名、数值、API 名、明确的行为承诺。
  **泛泛的背景常识陈述不算编造**（例如「侧载需要签名」）；
  但**捏造一个看起来像真的具体名字或数字**就是编造，必须扣分。
- 用不同措辞表达同一个意思，**不扣分**。

输出要求：
1. 只输出一个 JSON 对象，不要任何解释文字，不要 Markdown 代码块。
2. 严格格式：{{"correctness": 4, "specificity": 4, "no_fabrication": 5, "reason": "一句话总评"}}

【问题】
{question}

【参考材料（原文片段，唯一事实依据）】
{gold}

【被评回答】
{answer}
"""


def _parse_judge_json(text: str) -> Optional[Dict[str, Any]]:
    """从裁判输出里提取 JSON；容忍代码块包裹与轻微格式噪声。

    与 `evals/judge.py` 的解析逻辑保持一致 —— 裁判模型的行为是一样的，
    没理由在这里写一套不同的容错。
    """
    if not text:
        return None
    blob_text = text.strip()
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


# --------------------------------------------------------------------------- #
# 单次调用
# --------------------------------------------------------------------------- #


def _invoke(llm, messages, timeout: float) -> str:
    """同步调用，由调用方丢线程池 —— 与 `/chat` 的调用方式一致。"""
    result = llm.invoke(messages, timeout=timeout)
    return getattr(result, "content", "") or ""


async def _answer(llm, system_prompt: str, question: str) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=question),
    ]
    return await asyncio.to_thread(_invoke, llm, messages, settings.llm_chat_timeout)


def _judge(judge_llm, question: str, gold: str, answer: str) -> Dict[str, Any]:
    """判一条；解析失败重试一次，仍失败记 judge_error（不炸整轮）。

    「判分失败」和「质量差」必须分开 —— 混在一起会把裁判的锅算到模型头上，
    和 `evals/judge.py` 里 aggregate 排除 judge_error 是同一个道理。
    """
    prompt = _build_judge_prompt(question, gold, answer)
    last_err = ""
    for _ in range(2):
        try:
            raw = _invoke(judge_llm, prompt, settings.llm_judge_timeout)
        except Exception as e:  # noqa: BLE001 - 网络/模型异常重试一次
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


# --------------------------------------------------------------------------- #
# 单条实验
# --------------------------------------------------------------------------- #


@dataclass
class CaseResult:
    """一条题的两组结果。每组保存**所有重复轮次**的分数，不预先平均。

    保留明细是为了能算组内标准差 —— 没有它，就分不清「B 比 A 高 0.3 分」
    是真的有效果，还是 DeepSeek 在 0 温度下的正常抖动。
    """

    case_id: str
    query: str
    repo: str
    injected: bool
    reason: str
    refs: int
    answers_off: List[str] = field(default_factory=list)
    answers_on: List[str] = field(default_factory=list)
    scores_off: List[Dict[str, int]] = field(default_factory=list)
    scores_on: List[Dict[str, int]] = field(default_factory=list)
    reasons_off: List[str] = field(default_factory=list)
    reasons_on: List[str] = field(default_factory=list)
    gold_chars: int = 0
    errors: List[str] = field(default_factory=list)

    @staticmethod
    def _mean_total(scores: Sequence[Dict[str, int]]) -> Optional[float]:
        totals = [sum(s.values()) for s in scores]
        return sum(totals) / len(totals) if totals else None

    @property
    def total_off(self) -> Optional[float]:
        return self._mean_total(self.scores_off)

    @property
    def total_on(self) -> Optional[float]:
        return self._mean_total(self.scores_on)

    @staticmethod
    def _std_total(scores: Sequence[Dict[str, int]]) -> Optional[float]:
        totals = [sum(s.values()) for s in scores]
        return statistics.pstdev(totals) if len(totals) > 1 else None

    @property
    def std_off(self) -> Optional[float]:
        return self._std_total(self.scores_off)

    @property
    def std_on(self) -> Optional[float]:
        return self._std_total(self.scores_on)


async def run_case(
    llm, judge_llm, case: Dict[str, Any], do_judge: bool, repeat: int, gold_window: int
) -> CaseResult:
    """跑一条题：A 组（关检索）/ B 组（开检索），各重复 `repeat` 次。"""
    from src.api.routes.learning import _build_chat_system_prompt

    question = case["query"]
    session = _session_for(case)

    # ---- 检索只跑一次：它决定 B 组的提示词，本身不随重复变（embedding 是确定的） ----
    ctx = await build_chat_context(question, max_tokens=MAX_CONTEXT_TOKENS)

    # ---- 两组的系统提示词各拼一次，之后所有重复轮次复用同一份 ----
    sys_off = _build_chat_system_prompt(session, "")
    sys_on = _build_chat_system_prompt(session, ctx.block)

    result = CaseResult(
        case_id=case["id"],
        query=question,
        repo=case.get("repo", ""),
        injected=ctx.injected,
        reason=ctx.reason,
        refs=len(ctx.references),
    )

    if not do_judge:
        for _ in range(repeat):
            result.answers_off.append(await _answer(llm, sys_off, question))
            result.answers_on.append(await _answer(llm, sys_on, question))
        return result

    # 判分依据回原文取窗口（同步 IO，丢线程池）。
    gold = await asyncio.to_thread(_gold_reference, case, gold_window)
    result.gold_chars = len(gold)

    for _ in range(repeat):
        ans_off = await _answer(llm, sys_off, question)
        ans_on = await _answer(llm, sys_on, question)
        result.answers_off.append(ans_off)
        result.answers_on.append(ans_on)

        j_off = await asyncio.to_thread(_judge, judge_llm, question, gold, ans_off)
        j_on = await asyncio.to_thread(_judge, judge_llm, question, gold, ans_on)
        if "scores" in j_off:
            result.scores_off.append(j_off["scores"])
            result.reasons_off.append(j_off.get("reason", ""))
        else:
            result.errors.append(f"A 判分失败: {j_off.get('judge_error', '')}")
        if "scores" in j_on:
            result.scores_on.append(j_on["scores"])
            result.reasons_on.append(j_on.get("reason", ""))
        else:
            result.errors.append(f"B 判分失败: {j_on.get('judge_error', '')}")

    return result


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


def _sign_test_p(wins: int, losses: int) -> float:
    """配对符号检验的**精确**双侧 p 值（小样本不用近似正态）。

    n=36 这种量级上，正态近似的误差不可忽略 —— 而这里恰恰是「样本小」
    才需要小心的地方。直接算二项分布。
    """
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, tail * 2)


def _group_stats(results: Sequence[CaseResult], title: str) -> Dict[str, Any]:
    """按维度汇总一组：A/B 均分、逐条胜负、符号检验、**组内标准差**。"""
    paired = [r for r in results if r.total_off is not None and r.total_on is not None]
    wins = sum(1 for r in paired if r.total_on > r.total_off)
    losses = sum(1 for r in paired if r.total_on < r.total_off)
    ties = len(paired) - wins - losses

    dims: Dict[str, Dict[str, Optional[float]]] = {}
    for dim in JUDGE_DIMENSIONS:
        off = [s[dim] for r in paired for s in r.scores_off]
        on = [s[dim] for r in paired for s in r.scores_on]
        dims[dim] = {
            "off": round(sum(off) / len(off), 3) if off else None,
            "on": round(sum(on) / len(on), 3) if on else None,
        }

    tot_off = [r.total_off for r in paired if r.total_off is not None]
    tot_on = [r.total_on for r in paired if r.total_on is not None]

    # 组内标准差：把每条题的重复波动汇总成一个「噪声地板」。
    # 取所有题 std 的均值；k=1 时没有 std，返回 None（报告里会提示 k=1 不可下结论）。
    stds_off = [r.std_off for r in paired if r.std_off is not None]
    stds_on = [r.std_on for r in paired if r.std_on is not None]

    return {
        "title": title,
        "n": len(paired),
        "dims": dims,
        "total_off": round(sum(tot_off) / len(tot_off), 3) if tot_off else None,
        "total_on": round(sum(tot_on) / len(tot_on), 3) if tot_on else None,
        "noise_off": round(sum(stds_off) / len(stds_off), 3) if stds_off else None,
        "noise_on": round(sum(stds_on) / len(stds_on), 3) if stds_on else None,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "p_value": round(_sign_test_p(wins, losses), 4) if paired else None,
        "judge_errors": sum(1 for r in results if r.errors),
    }


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #


def _fmt_delta(off: Optional[float], on: Optional[float]) -> str:
    return f"{on - off:+.3f}" if (off is not None and on is not None) else "-"


def _render_group(g: Dict[str, Any], lines: List[str]) -> None:
    lines.append("-" * 78)
    lines.append(f"{g['title']}   n={g['n']}   判分失败 {g['judge_errors']} 条")
    lines.append("-" * 78)
    if g["n"] == 0:
        lines.append("  （无有效配对，跳过）")
        lines.append("")
        return
    lines.append(f"  {'维度':<16} {'A 关检索':>10} {'B 开检索':>10} {'差值':>8}")
    for dim in JUDGE_DIMENSIONS:
        off, on = g["dims"][dim]["off"], g["dims"][dim]["on"]
        lines.append(f"  {dim:<16} {off!s:>10} {on!s:>10} {_fmt_delta(off, on):>8}")
    lines.append(
        f"  {'总分(/15)':<16} {g['total_off']!s:>10} {g['total_on']!s:>10} "
        f"{_fmt_delta(g['total_off'], g['total_on']):>8}"
    )
    lines.append(
        f"  {'组内标准差':<16} {g['noise_off']!s:>10} {g['noise_on']!s:>10} "
        f"{'（噪声地板）' if g['noise_off'] is not None else '（k=1，无法估计）':>8}"
    )
    lines.append("")
    lines.append(
        f"  逐条胜负：B 胜 {g['wins']} / 平 {g['ties']} / A 胜 {g['losses']}"
        f"    配对符号检验 p = {g['p_value']}"
    )
    lines.append("")


def _render(results: Sequence[CaseResult], do_judge: bool, repeat: int) -> str:
    lines: List[str] = []
    injected = [r for r in results if r.injected]
    not_injected = [r for r in results if not r.injected]

    lines.append("=" * 78)
    lines.append("注入前 / 后对照实验（A=关检索，B=开检索）")
    lines.append("=" * 78)
    lines.append("")
    lines.append(
        f"共 {len(results)} 条题 × 重复 {repeat} 次："
        f"注入 {len(injected)} 条 / 未注入 {len(not_injected)} 条"
    )
    if repeat == 1:
        lines.append(
            "⚠️ repeat=1：无法估计生成噪声（DeepSeek 在 temperature=0 下也不确定），"
            "只适合冒烟，**不要用来下结论**。"
        )
    lines.append("")

    if do_judge:
        groups = [
            _group_stats(injected, "注入子集（效果应该出现在这里）"),
            _group_stats(not_injected, "未注入子集（安慰剂：两组提示词逐字相同，差值 = 纯噪声）"),
            _group_stats(results, "全量"),
        ]
        for g in groups:
            _render_group(g, lines)

    lines.append("=" * 78)
    lines.append("逐条明细")
    lines.append("=" * 78)
    for r in results:
        flag = "注入" if r.injected else "未注入"
        lines.append(f"{r.case_id}  [{flag}]  refs={r.refs}  reason={r.reason}  {r.query[:32]}")
        if do_judge and r.total_off is not None and r.total_on is not None:
            mark = (
                "B 胜" if r.total_on > r.total_off
                else ("A 胜" if r.total_on < r.total_off else "平")
            )
            noise = ""
            if r.std_off is not None and r.std_on is not None:
                noise = f"  组内σ={r.std_off:.2f}/{r.std_on:.2f}"
            lines.append(
                f"      A={r.total_off:.2f}/{MAX_TOTAL}  B={r.total_on:.2f}/{MAX_TOTAL}"
                f"   {mark}{noise}"
            )
        for err in r.errors:
            lines.append(f"      {err}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 交叉分析：注入效果 × 检索有没有命中
# --------------------------------------------------------------------------- #


def _parse_report(text: str) -> List[Tuple[str, bool, float, float]]:
    """从报告文本里解析出 `(case_id, 是否注入, A 总分, B 总分)`。

    【为什么要解析自己的报告】交叉分析要的是「每条题 A/B 各多少分」，
    而那批数据是**花了 432 次模型调用**换来的。让分析走一个**不花钱的重解析**，
    就能在不重跑实验的前提下反复换切法看 —— 这比把结果写死在文档里可靠得多。
    """
    rows: List[Tuple[str, bool, float, float]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(c\d+)\s+\[(注入|未注入)\]", line)
        if not m or i + 1 >= len(lines):
            continue
        m2 = re.search(r"A=([\d.]+)/\d+\s+B=([\d.]+)/\d+", lines[i + 1])
        if not m2:
            continue
        rows.append((m.group(1), m.group(2) == "注入", float(m2.group(1)), float(m2.group(2))))
    return rows


def run_crosstab() -> int:
    """把「注入效果」按「检索有没有命中」分组 —— 回答「效果从哪来」。"""
    if not REPORT_PATH.exists():
        print(f"找不到报告：{REPORT_PATH}\n先跑一次实验（--repeat 3 --out rag_chat_ab_report.md）。")
        return 1
    if not BASELINE_PATH.exists():
        print(f"找不到检索基线：{BASELINE_PATH}")
        return 1

    rows = _parse_report(REPORT_PATH.read_text(encoding="utf-8"))
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    hit5 = {p["id"]: bool(p["hit"]["5"]) for p in baseline["positives"]}

    injected = [r for r in rows if r[1]]
    print("=" * 78)
    print("交叉分析：注入效果 × 检索有没有命中")
    print("=" * 78)
    print()
    print(f"报告：{REPORT_PATH.name}（{len(rows)} 条，其中注入 {len(injected)} 条）")
    print(f"基线：{BASELINE_PATH.name}（hit@5 判定）")
    print()
    if not injected:
        print("报告里没有注入的样本，无法交叉分析。")
        return 1

    header = f"{'分组':<20}{'n':>4}{'B 胜':>7}{'平':>5}{'A 胜':>7}{'B 胜率':>9}{'总分差':>10}"
    print(header)
    print("-" * len(header))
    for label, keep in (("检索命中 hit@5", True), ("检索未命中 hit@5", False)):
        sub = [r for r in injected if hit5.get(r[0]) is keep]
        if not sub:
            print(f"{label:<20}{0:>4}   （无样本）")
            continue
        wins = sum(1 for _, _, a, b in sub if b > a)
        losses = sum(1 for _, _, a, b in sub if b < a)
        ties = len(sub) - wins - losses
        delta = sum(b - a for _, _, a, b in sub) / len(sub)
        print(
            f"{label:<20}{len(sub):>4}{wins:>7}{ties:>5}{losses:>7}"
            f"{wins / len(sub) * 100:>8.1f}%{delta:>+10.3f}"
        )
    print()
    print("B 输掉的题（看它们是不是检索本身就没命中）：")
    for cid, _, a, b in injected:
        if b < a:
            print(f"  {cid}  A={a:.2f}  B={b:.2f}   hit@5={hit5.get(cid)}")
    print()
    print("读法：如果**命中组**的差值和胜率明显高于**未命中组**，说明效果来自")
    print("「检索找对了」而不是「多塞了几段字」—— 也就同时说明：把命中率提上去")
    print("（上重排）会直接放大注入的收益。")
    return 0


# --------------------------------------------------------------------------- #
# 预检
# --------------------------------------------------------------------------- #


async def _preflight() -> Optional[str]:
    """探一次检索，确认 Qdrant 活着。返回错误说明，正常则 None。

    【为什么必须先探】Qdrant 没起来时，36 条题会全部走 `qdrant_error` 降级路径 ——
    两组提示词逐字相同，**实验变成空转**，但脚本不会报任何错，只会输出一张
    「两组几乎一样」的表。那看起来像「注入没效果」，其实是「检索根本没跑」。
    **静默降级在这里会伪装成实验结论**，所以宁可先失败一次。
    """
    ctx = await build_chat_context("连通性自检", max_tokens=MAX_CONTEXT_TOKENS)
    if ctx.reason == "qdrant_error":
        return (
            "Qdrant 不可达（检索返回 qdrant_error）。\n"
            f"  配置的地址：{settings.qdrant_url}\n"
            "  请先启动：docker compose up -d qdrant\n"
            "  （若确实要在检索不可用的前提下跑，用 --allow-qdrant-down 跳过本检查，"
            "但那时两组提示词相同，跑出来的只是噪声。）"
        )
    return None


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


async def run(args) -> int:
    if args.crosstab:
        return run_crosstab()

    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases: List[Dict[str, Any]] = list(data["cases"])

    if args.ids:
        wanted = {s.strip() for s in args.ids.split(",") if s.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]

    if not cases:
        print("没有可跑的用例。")
        return 1

    if not args.allow_qdrant_down:
        problem = await _preflight()
        if problem:
            print(f"预检失败：\n  {problem}")
            return 2

    llm = create_llm(timeout=settings.llm_chat_timeout)
    if llm is None:
        print("生成模型不可用（未配置 API Key），无法跑实验。")
        return 1
    # 两组都按 temperature=0 跑。注意：这只是压住采样随机性，
    # **不等于**确定性（见模块 docstring）—— 真正的消噪靠 --repeat。
    try:
        llm.temperature = 0
    except Exception:  # noqa: BLE001
        pass

    judge_llm = None
    if not args.no_judge:
        judge_llm = create_qwen_llm(timeout=settings.llm_judge_timeout)
        if judge_llm is None:
            print("裁判模型不可用（未配置 ALIYUN_API_KEY），退化为 --no-judge。")
            args.no_judge = True
        else:
            try:
                judge_llm.temperature = 0
            except Exception:  # noqa: BLE001
                pass

    repeat = max(1, args.repeat)
    print(
        f"开始跑 {len(cases)} 条题 × 重复 {repeat} 次"
        f"（判分={'开' if not args.no_judge else '关'}）…\n"
    )
    results: List[CaseResult] = []
    for i, case in enumerate(cases, 1):
        r = await run_case(
            llm,
            judge_llm,
            case,
            do_judge=not args.no_judge,
            repeat=repeat,
            gold_window=args.gold_window,
        )
        results.append(r)
        tail = ""
        if r.total_off is not None and r.total_on is not None:
            tail = f"  A={r.total_off:.2f} B={r.total_on:.2f}"
        print(
            f"  [{i:>2}/{len(cases)}] {r.case_id} "
            f"{'注入' if r.injected else '未注入'}({r.reason}){tail}"
        )

    report = _render(results, do_judge=not args.no_judge, repeat=repeat)
    print()
    print(report)

    if args.show_reasons:
        print("=" * 78)
        print("裁判理由（诊断用：看它到底在扣什么）")
        print("=" * 78)
        for r in results:
            print(f"\n### {r.case_id}  {r.query}")
            for i, (ro, rn) in enumerate(zip(r.reasons_off, r.reasons_on), 1):
                print(f"  第{i}轮  A: {ro}")
                print(f"  第{i}轮  B: {rn}")

    if args.show_answers:
        print("=" * 78)
        print("完整回答（肉眼核对用）")
        print("=" * 78)
        for r in results:
            print(f"\n### {r.case_id}  {r.query}")
            for idx, (a_off, a_on) in enumerate(zip(r.answers_off, r.answers_on), 1):
                print(f"--- 第 {idx} 轮 · A 关检索 ---\n{a_off}")
                print(f"--- 第 {idx} 轮 · B 开检索 ---\n{a_on}")

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = Path(__file__).resolve().parent / out_path
        out_path.write_text(report, encoding="utf-8")
        print(f"\n报告已写入：{out_path}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="注入前/后回答质量对照实验（补 P4 门禁的第二条）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--limit", type=int, default=0, help="最多跑 N 条（0=不限）")
    parser.add_argument("--ids", default="", help="只跑指定 id，逗号分隔，如 c001,c002")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每条每组重复次数（默认 1=冒烟；下结论建议 3）",
    )
    parser.add_argument("--no-judge", action="store_true", help="只生成不判分（更省钱）")
    parser.add_argument("--show-answers", action="store_true", help="打印两组的完整回答")
    parser.add_argument("--show-reasons", action="store_true", help="打印裁判理由（诊断用）")
    parser.add_argument(
        "--gold-window",
        type=int,
        default=600,
        help="判分依据取原文里答案区间前后各多少字符（0=退回用 answer_excerpt）",
    )
    parser.add_argument("--out", default="", help="把报告写到指定文件")
    parser.add_argument(
        "--allow-qdrant-down",
        action="store_true",
        help="跳过 Qdrant 预检（检索不可用时两组提示词相同，跑出来的只是噪声）",
    )
    parser.add_argument(
        "--crosstab",
        action="store_true",
        help="不跑实验，只把已有报告按「检索有没有命中」交叉分析（不花钱）",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
