# Evals - 评测编排与报告
"""跑评测集 -> 打分 -> 汇总 -> 与基线对比 -> 产出 markdown 报告。

生成侧复用**生产链路本身**（`build_summary_prompt` / `build_digest_prompt` +
`summary_llm`），不另写一套 prompt —— 否则评测的不是线上真实输出，改了也没意义。

数据集条目若自带 `output`（已知坏案例标定样本），则跳过生成、直接判分：这是
「验证 judge 能识别坏案例」的唯一可行路径（生产链路的闸门产不出坏样本）。

跑法：
    cd backend
    .\\ilo\\Scripts\\python.exe -m evals.runner --dataset all --limit 5
    .\\ilo\\Scripts\\python.exe -m evals.runner --dataset summary --update-baseline
"""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.modules.discovery.summary_spec import (
    build_digest_prompt,
    build_summary_prompt,
    is_chinese_text,
)

_EVAL_DIR = Path(__file__).resolve().parent
DATASETS_DIR = _EVAL_DIR / "datasets"
REPORTS_DIR = _EVAL_DIR / "reports"
BASELINE_PATH = _EVAL_DIR / "baseline.json"


# ==================== 数据集与基线 ====================


def load_dataset(name: str) -> List[Dict[str, Any]]:
    """读取冻结的评测集（name: summary | digest）"""
    path = DATASETS_DIR / f"{name}_cases.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return list(data.get("cases") or [])


def load_baseline() -> Dict[str, Any]:
    """读取基线分数；文件不存在或损坏时返回空字典（调用方据此跳过对比）"""
    if not BASELINE_PATH.exists():
        return {}
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_baseline(data: Dict[str, Any]) -> None:
    """固化基线（显式 --update-baseline 才会调用）"""
    BASELINE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ==================== 素材与被评输出的构造 ====================


def _summary_material(case: Dict[str, Any]) -> str:
    """摘要任务的素材：标题 + 原始描述 + 语言 + 相关技术主题。

    必须与 `build_summary_prompt` 实际喂给生成模型的字段**严格一致**（含
    `core_concepts`，即生成 prompt 里的 topics）——否则生成模型基于
    `core_concepts` 写出的技术点，会被裁判当成「素材未提及」而误判为编造，
    系统性压低 faithfulness。素材对被评输出是「事实边界」，两边口径必须对齐。
    """
    inp = case.get("input") or {}
    parts = [f"标题：{inp.get('title', '')}"]
    if inp.get("description"):
        parts.append(f"原始描述：{inp['description']}")
    if inp.get("language"):
        parts.append(f"语言：{inp['language']}")
    topics = (inp.get("core_concepts") or [])[:4]
    if topics:
        parts.append(f"相关技术主题：{', '.join(topics)}")
    return "\n".join(parts)


def _digest_material(case: Dict[str, Any]) -> str:
    """导读任务的素材：标题 + GitHub 描述 + README 摘录"""
    parts = [f"仓库：{case.get('title', '')}"]
    if case.get("raw_description"):
        parts.append(f"GitHub 描述：{case['raw_description']}")
    readme = (case.get("readme_excerpt") or "").strip()
    parts.append(f"README 原文（可能被截断）：\n{readme or '（未取到 README 原文）'}")
    return "\n".join(parts)


def _generate_summary(case: Dict[str, Any]) -> Optional[str]:
    """跑生产摘要链路，返回 summary 文本（同步，调用方用 to_thread 包）"""
    from src.modules.agent.state_machine import summary_llm

    if summary_llm is None:
        return None

    from src.modules.discovery.github_fetcher import _extract_json

    item = dict(case.get("input") or {})
    item.setdefault("title", case.get("id", ""))
    prompt = build_summary_prompt([item], kind=case.get("kind", "repo"))
    try:
        result = summary_llm.invoke(prompt)
    except Exception as e:  # noqa: BLE001 - 单条生成失败记 generation_error，不中断整轮
        print(f"[WARN] 摘要生成失败 {case.get('id')}: {e}")
        return None

    parsed = _extract_json(getattr(result, "content", "") or "")
    if not parsed or not isinstance(parsed[0], dict):
        return None
    return (parsed[0].get("summary") or "").strip() or None


def _generate_digest(case: Dict[str, Any]) -> Optional[str]:
    """跑生产导读链路，返回导读正文（同步，调用方用 to_thread 包）"""
    from src.modules.agent.state_machine import summary_llm

    if summary_llm is None:
        return None

    prompt = build_digest_prompt(
        title=case.get("title", ""),
        raw_description=case.get("raw_description", ""),
        readme=case.get("readme_excerpt", ""),
    )
    try:
        result = summary_llm.invoke(prompt)
    except Exception as e:  # noqa: BLE001 - 单条生成失败记 generation_error
        print(f"[WARN] 导读生成失败 {case.get('id')}: {e}")
        return None
    return (getattr(result, "content", "") or "").strip() or None


_GENERATORS = {"summary": _generate_summary, "digest": _generate_digest}
_MATERIALS = {"summary": _summary_material, "digest": _digest_material}


# ==================== 单数据集执行 ====================


async def run_dataset(name: str, limit: int = 0) -> Dict[str, Any]:
    """跑一个数据集并返回汇总结果（含逐条 records，供报告渲染）"""
    from .judge import aggregate, judge_case

    cases = load_dataset(name)
    if limit > 0:
        cases = cases[:limit]

    records: List[Dict[str, Any]] = []
    for case in cases:
        record: Dict[str, Any] = {
            "id": case.get("id"),
            "task": name,
            "label": case.get("label"),
        }

        # 自带 output = 已知坏案例标定样本，跳过生成直接判分
        output = case.get("output")
        if not output:
            output = await asyncio.to_thread(_GENERATORS[name], case)

        if not output:
            record["generation_error"] = True
            records.append(record)
            continue

        record["output"] = output
        record["chinese_gate_passed"] = is_chinese_text(output)
        material = _MATERIALS[name](case)
        record.update(await asyncio.to_thread(judge_case, name, material, output))
        records.append(record)

    # 基准均分只统计「真实生产样本」：标定用的坏案例（label 非空）是拿来验证
    # 裁判区分度的，拿故意写坏的样本去算基线会把基线拉低、让门禁名存实亡。
    benchmark = [r for r in records if not r.get("label")]
    calibration = [r for r in records if r.get("label")]
    summary = aggregate(benchmark)

    # 生成侧中文化闸门放行率：只统计「真实生成」样本，标定样本（label 非空）不算
    generated = [r for r in benchmark if "output" in r]
    summary["chinese_gate"] = {
        "total": len(generated),
        "passed": sum(1 for r in generated if r.get("chinese_gate_passed")),
    }
    summary["calibration"] = aggregate(calibration)
    summary["records"] = records
    return summary


async def run(dataset: str, limit: int = 0) -> Dict[str, Dict[str, Any]]:
    """跑一个或多个数据集（dataset: summary | digest | all）"""
    names = ["summary", "digest"] if dataset == "all" else [dataset]
    results: Dict[str, Dict[str, Any]] = {}
    for name in names:
        print(f"[INFO] 评测数据集：{name}")
        results[name] = await run_dataset(name, limit=limit)
    return results


# ==================== 报告 ====================


def render_report(results: Dict[str, Dict[str, Any]], baseline: Dict[str, Any]) -> str:
    """渲染 markdown 报告：逐条得分表 + 总均分 + 较基线升降 + 不合格清单"""
    from .judge import JUDGE_DIMENSIONS

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: List[str] = [f"# Eval 报告（{now}）", ""]

    for name, result in results.items():
        lines.append(f"## 数据集：{name}")
        base_overall = (baseline.get(name) or {}).get("overall")
        overall = result.get("overall")
        if overall is None or base_overall is None:
            delta = "—"
        else:
            delta = f"{overall - base_overall:+.3f}"

        lines.append(
            f"- 计分条数 {result.get('scored', 0)}；judge_error {result.get('judge_errors', 0)}"
        )
        lines.append(
            f"- 总均分 **{overall if overall is not None else '—'}**"
            f"（基线 {base_overall if base_overall is not None else '—'}，Δ {delta}）"
        )
        gate = result.get("chinese_gate") or {}
        if gate:
            lines.append(f"- 生成侧中文化闸门放行 {gate.get('passed', 0)}/{gate.get('total', 0)}")
        cal = result.get("calibration") or {}
        if cal.get("scored"):
            lines.append(
                f"- 标定坏案例 {cal['scored']} 条（故意写坏，**不计入基准均分**）："
                f"总均分 {cal.get('overall')}"
            )
        lines.append("")

        lines.append("| 维度 | 均分 |")
        lines.append("| --- | --- |")
        for dim in JUDGE_DIMENSIONS:
            value = (result.get("dimension_means") or {}).get(dim)
            lines.append(f"| {dim} | {value if value is not None else '—'} |")
        lines.append("")

        lines.append(
            "| case | 总分 | " + " | ".join(JUDGE_DIMENSIONS) + " | 备注 |"
        )
        lines.append("| --- | --- | " + " | ".join("---" for _ in JUDGE_DIMENSIONS) + " | --- |")
        for record in result.get("records", []):
            if "scores" in record:
                scores = record["scores"]
                total = round(sum(scores.values()) / len(scores), 2)
                cells = " | ".join(str(scores[d]) for d in JUDGE_DIMENSIONS)
                note = record.get("label") or ""
                lines.append(f"| {record['id']} | {total} | {cells} | {note} |")
            elif "judge_error" in record:
                lines.append(
                    f"| {record['id']} | — | " + " | ".join("—" for _ in JUDGE_DIMENSIONS)
                    + f" | judge_error: {record['judge_error']} |"
                )
            elif record.get("generation_error"):
                lines.append(
                    f"| {record['id']} | — | " + " | ".join("—" for _ in JUDGE_DIMENSIONS)
                    + " | 生成失败 |"
                )
        lines.append("")

        bad = [
            r
            for r in result.get("records", [])
            if "scores" in r and min(r["scores"].values()) < 3
        ]
        if bad:
            lines.append("**不合格样本（存在维度 < 3）：**")
            for record in bad:
                lines.append(f"- {record['id']}：{record['scores']} — {record.get('reason', '')}")
            lines.append("")

    return "\n".join(lines)


# ==================== CLI ====================


def main() -> int:
    parser = argparse.ArgumentParser(description="输出质量 Eval（LLM-as-judge）")
    parser.add_argument("--dataset", choices=["summary", "digest", "all"], default="all")
    parser.add_argument("--limit", type=int, default=0, help="每个数据集最多跑多少条（0 = 不限）")
    parser.add_argument(
        "--update-baseline", action="store_true", help="把本次结果固化为新基线"
    )
    parser.add_argument("--no-report", action="store_true", help="不写文件，直接把报告打到 stdout")
    args = parser.parse_args()

    from .judge import JudgeUnavailableError

    try:
        results = asyncio.run(run(args.dataset, limit=args.limit))
    except JudgeUnavailableError as e:
        print(f"[SKIP] 裁判模型不可用：{e}")
        return 2

    baseline = load_baseline()
    report = render_report(results, baseline)

    if args.no_report:
        print(report)
    else:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = REPORTS_DIR / f"eval_{timestamp}.md"
        out_path.write_text(report, encoding="utf-8")
        print(f"[OK] 报告已写入 {out_path}")

    if args.update_baseline:
        new_baseline = dict(baseline)
        for name, result in results.items():
            new_baseline[name] = {
                "overall": result.get("overall"),
                "dimension_means": result.get("dimension_means"),
                "scored": result.get("scored"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        save_baseline(new_baseline)
        print(f"[OK] 基线已更新 {BASELINE_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
