# RAG Chat Probe - 「检索结果拼进提示词」这一步的可复现验证
"""把固定的几个提问跑一遍 `build_chat_context`，看**最终拼出来的提示词**。

## 为什么需要这个脚本

`rag_search.py` 验的是「检索找没找到」，`retrieval_runner.py` 量的是「命中率」。
但**接进问答**这件事还有一个环节谁都没验过：**命中之后拼进提示词长什么样**。

这一步是 RAG 的**唯一生效点** —— 找得再准，拼错了模型也看不到。而它拼错了
**不会有任何报错**：块没进去就是「回答变差」，编号对不上就是「模型瞎标引用」。
所以这里把最终提示词**原样打出来**，肉眼可查。

## 四个用例不是随便挑的

| 用例 | 它验证什么 |
|---|---|
| 自足问题 | 正常路径：命中、注入、引用可溯源 |
| 短追问 | 短问题**不拼主题锚点**（撤掉的原因见 `--anchor-experiment`） |
| 语料外问题 | 阈值闸门该把它挡掉，一条都不注入 |
| 另一个主题 | 换主题要换出**不同**的卡，防止「永远命中同一张」的假通过 |

## 运行

    cd backend

    # 跑内置四个用例，打印每例的参考资料块
    .\\ilo\\Scripts\\python.exe rag_chat_probe.py

    # 打印**完整提示词**（这才是模型真正看到的东西）
    .\\ilo\\Scripts\\python.exe rag_chat_probe.py --show-prompt

    # 复现「主题锚点为什么被撤掉」的实测表
    .\\ilo\\Scripts\\python.exe rag_chat_probe.py --anchor-experiment

    # 自己的提问
    .\\ilo\\Scripts\\python.exe rag_chat_probe.py --message "它怎么解决内存安全"

成本：每个用例一次 embedding 调用，四条约几分钱。Qdrant 本地。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import List, Sequence, Tuple

from dotenv import load_dotenv

# 与 rag_search.py 同一处理：显式锚到脚本目录，从哪运行都对。
load_dotenv(Path(__file__).resolve().parent / ".env")

from src.modules.rag.chat_context import (  # noqa: E402
    MAX_CONTEXT_TOKENS,
    build_chat_context,
)
from src.modules.rag.chunk_spec import estimate_tokens  # noqa: E402
from src.modules.rag.retriever import (  # noqa: E402
    DEFAULT_MIN_SCORE,
    search,
)

CASES_PATH = Path(__file__).resolve().parent / "retrieval_cases.json"

# (提问, 这一例在验什么)
CASES: Sequence[Tuple[str, str]] = (
    ("地震数据去噪用了什么方法", "自足问题 —— 正常路径"),
    ("再举个例子", "短追问 —— 不拼主题锚点（见 --anchor-experiment）"),
    ("今天晚饭吃什么比较好", "语料外问题 —— 阈值闸门该把它挡掉"),
    ("它怎么裁剪视觉语言模型", "换主题 —— 应命中另一张卡"),
)

# 锚点实验用的两个「会话主题」——都是语料里真实存在的卡片主题
ANCHOR_TOPICS = ("3D 地震数据去噪", "Scalpel 视觉语言模型裁剪")

# 锚点实验的追问侧：这些是**真追问**，不锚就检索不到东西
FOLLOWUPS = (
    ("3D 地震数据去噪", "再举个例子"),
    ("Scalpel 视觉语言模型裁剪", "它怎么裁剪视觉语言模型"),
    ("3D 地震数据去噪", "地震数据去噪用了什么方法"),
)


def _rule(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


async def run_case(message: str, note: str, show_prompt: bool) -> bool:
    """跑一个用例，返回「是否注入了资料」。"""
    ctx = await build_chat_context(message, max_tokens=MAX_CONTEXT_TOKENS)

    print(f"\n提问      : {message}")
    print(f"检索用的查询: {ctx.query!r}")
    print(f"验的是    : {note}")
    print(f"结果      : reason={ctx.reason!r}  降级={ctx.degraded}  注入={ctx.injected}")

    if not ctx.injected:
        print("→ 未注入任何资料（问答退回「只喂会话上下文」，这是正确行为）")
        return False

    print(f"注入 {len(ctx.references)} 条，参考资料段约 {estimate_tokens(ctx.block)} token")
    if not show_prompt:
        print("-" * 78)
        print(ctx.block)
        print("-" * 78)
    print("引用（给前端的可溯源信息）:")
    for ref in ctx.references:
        print(
            f"  [{ref.ordinal}] {ref.title or '（无标题）'}"
            f"  score={ref.score:.4f}"
            f"  card={ref.card_id}"
            f"  heading={'/'.join(ref.heading_path) or '（无）'}"
        )
        if ref.source_url:
            print(f"       {ref.source_url}")

    if show_prompt:
        from src.api.routes.learning import _build_chat_system_prompt

        print()
        print("-" * 78)
        print("完整系统提示词（模型真正看到的东西）")
        print("-" * 78)
        print(
            _build_chat_system_prompt(
                {
                    "topic": "（当前学习会话的主题）",
                    "summary": "（会话摘要，此处省略）",
                    "core_concepts": [],
                },
                ctx.block,
            )
        )
    return True


async def run_anchor_experiment() -> int:
    """复现「主题锚点为什么被撤掉」的两张实测表。

    结论写进 `chat_context.build_retrieval_query()` 的 docstring。改锚点策略前
    先跑这个，别凭直觉。
    """
    _rule("实验一：锚点救不救得了追问？（这是加锚点的初衷）")
    print(f"阈值 {DEFAULT_MIN_SCORE}\n")
    print(f"{'追问':<24} {'裸查':>8} {'加锚点':>8}   判定")
    print("-" * 70)
    for topic, msg in FOLLOWUPS:
        plain = (await search(msg)).best_score
        anchored = (await search(f"{topic} {msg}")).best_score
        verdict = f"{'被挡' if plain < DEFAULT_MIN_SCORE else '过阈值'} → " \
                  f"{'被挡' if anchored < DEFAULT_MIN_SCORE else '过阈值'}"
        print(f"{msg:<24} {plain:>8.4f} {anchored:>8.4f}   {verdict}")

    _rule("实验二：锚点会不会打穿拒答闸门？（这是撤掉它的原因）")
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    # 只看在真实链路里**会被加锚点**的负样本（按当时的阈值：短于 12 字符）
    short_negs: List[str] = [c["query"] for c in data["negatives"] if len(c["query"]) < 12]
    print(f"语料外问题 {len(short_negs)} 条（其余负样本够长，本来就不会被加锚点）\n")
    header = f"{'语料外问题':<24} {'裸查':>8}" + "".join(f"{'锚'+str(i+1):>10}" for i in range(len(ANCHOR_TOPICS)))
    print(header)
    print("-" * 70)
    plain_false = anchored_false = 0
    for q in short_negs:
        plain = (await search(q)).best_score
        anchored = [(await search(f"{t} {q}")).best_score for t in ANCHOR_TOPICS]
        if plain >= DEFAULT_MIN_SCORE:
            plain_false += 1
        if plain >= DEFAULT_MIN_SCORE or any(s >= DEFAULT_MIN_SCORE for s in anchored):
            anchored_false += 1
        row = f"{q:<24} {plain:>8.4f}" + "".join(f"{s:>10.4f}" for s in anchored)
        print(row)

    _rule("结论")
    print(f"裸查误纳 {plain_false}/{len(short_negs)}   加锚点后误纳 {anchored_false}/{len(short_negs)}")
    print()
    print("锚点确实救活了追问（实验一），但代价是把语料外的短问题全部抬过阈值")
    print("（实验二）—— 主题短语与语料高度相似，拼进去后不管问什么都自带约 0.2 的「像」。")
    print("锚点的收益只是「追问多拿几段正文」，而会话上下文本来就已经注入提示词；")
    print("代价却是「注入 5 段无关正文」，模型会照着错料编。**正确性 > 丰富度** → 撤掉。")
    for i, t in enumerate(ANCHOR_TOPICS):
        print(f"  锚{i+1} = {t}")
    return 0


async def run(args) -> int:
    if args.anchor_experiment:
        return await run_anchor_experiment()

    if args.message:
        _rule("自定义用例")
        await run_case(args.message, "（自定义）", args.show_prompt)
        return 0

    injected = 0
    for message, note in CASES:
        _rule(f"用例：{message}")
        if await run_case(message, note, args.show_prompt):
            injected += 1

    _rule("小结")
    print(f"{injected} / {len(CASES)} 个用例注入了资料。")
    print()
    print("看点：")
    print("  1. 第 2 例（短追问）**不拼主题锚点** —— 撤掉的原因跑 --anchor-experiment 看；")
    print("  2. 第 3 例（语料外）必须**零注入**：靠阈值挡掉，不需要模型来判断；")
    print("  3. 第 4 例应命中与第 1 例**不同**的卡片，说明检索真的跟着问题走。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="验证「检索结果拼进提示词」这一步（打印最终提示词）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--message", default="", help="自定义提问")
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="打印完整系统提示词，而不只是参考资料段",
    )
    parser.add_argument(
        "--anchor-experiment",
        action="store_true",
        help="复现「主题锚点为什么被撤掉」的实测表",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
