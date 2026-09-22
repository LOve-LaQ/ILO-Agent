# Audit KB Vectors - 知识库向量健康度审计
"""一次性离线审计：知识库里的向量到底能不能用来做相似度排序。

为什么必须先做这一步：
个性化推送的核心假设是「卡片向量能表达卡片语义，余弦相似度就是兴趣接近度」。
但写入侧有一条**紧急降级**分支 —— embedding 失败或维度不匹配时，`upsert()` 会写入
`[0.1] * 1536` 这个常量向量。常量向量与任何向量的余弦相似度都相同（方向一致），
一旦占比不小，排序结果就是噪声，而我们却会以为「个性化生效了」。本脚本把这件事
量化出来，决定排序策略要不要先过滤退化向量。

只读，不写任何数据：
    cd backend
    .\\ilo\\Scripts\\python.exe audit_kb_vectors.py

统计口径：
- 维度分布：正常应全部 = VECTOR_SIZE，出现其他值说明写入侧发生过维度漂移；
- 退化向量：标准方差 ≈ 0（含全常量向量与全零向量），它们不携带任何语义；
- 唯一向量数：去重后剩多少个不同方向，是「有效信息量」的上界。
"""

import argparse
from collections import Counter, defaultdict
from typing import Any, Iterator, List, Optional, Sequence

from loguru import logger

from src.modules.discovery.tech_knowledge import (
    COLLECTION,
    VECTOR_SIZE,
    get_knowledge_base,
)
from src.services.recommend import is_degenerate_vector

# 唯一向量判重时对浮点取整，避免归一化噪声把同一条向量数成两条
_ROUND_DIGITS = 4


def _extract_vector(point: Any) -> Optional[Sequence[float]]:
    """取单条 point 的向量；命名向量配置下取第一个（本库是单向量，仅作防御）"""
    vector = getattr(point, "vector", None)
    if isinstance(vector, dict):
        vector = next(iter(vector.values()), None)
    return vector


def _iter_points(kb, batch: int = 500) -> Iterator[Any]:
    """翻页拿全量 point（含向量）"""
    offset = None
    while True:
        points, offset = kb.qdrant.scroll(
            collection_name=COLLECTION,
            limit=batch,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            return
        yield from points
        if offset is None:
            return


def _classify(vector: Optional[Sequence[float]]) -> str:
    """把一条向量归类：missing / empty / degenerate / ok

    «退化» 的判定复用排序内核的同名函数，避免审计口径与排序过滤口径各说各话。
    """
    if vector is None:
        return "missing"
    if len(vector) == 0:
        return "empty"
    return "degenerate" if is_degenerate_vector(vector) else "ok"


def audit() -> dict:
    """跑一遍审计，返回统计结果（纯读）"""
    kb = get_knowledge_base()
    dim_counter: Counter = Counter()
    kind_counter: Counter = Counter()
    type_counter: Counter = Counter()
    # 按 type 分别统计有效向量，便于判断「是某一类内容整体退化」还是全局
    per_type: dict = defaultdict(lambda: Counter())
    unique_vectors = set()
    total = 0
    sampled_ids: List[str] = []

    for point in _iter_points(kb):
        total += 1
        payload = point.payload or {}
        item_type = str(payload.get("type") or "unknown")
        type_counter[item_type] += 1

        vector = _extract_vector(point)
        kind = _classify(vector)
        kind_counter[kind] += 1
        per_type[item_type][kind] += 1

        if vector is not None:
            dim_counter[len(vector)] += 1
            if kind == "ok":
                unique_vectors.add(tuple(round(float(x), _ROUND_DIGITS) for x in vector))
        if kind in {"missing", "empty", "degenerate"} and len(sampled_ids) < 10:
            sampled_ids.append(str(payload.get("id") or point.id))

    return {
        "total": total,
        "dim_counter": dict(dim_counter),
        "kind_counter": dict(kind_counter),
        "type_counter": dict(type_counter),
        "per_type": {k: dict(v) for k, v in per_type.items()},
        "unique_vectors": len(unique_vectors),
        "sampled_ids": sampled_ids,
    }


def _pct(part: int, whole: int) -> str:
    return f"{(part / whole * 100):.1f}%" if whole else "-"


def _print_report(stats: dict) -> None:
    total = stats["total"]
    kinds = stats["kind_counter"]
    usable = kinds.get("ok", 0)
    bad = total - usable

    print("-" * 60)
    print(f"卡片总数            : {total}")
    print(f"向量维度分布        : {stats['dim_counter'] or '（无向量）'}"
          f"   [期望全部 {VECTOR_SIZE}]")
    print(f"按 type 分布        : {stats['type_counter'] or '（空库）'}")
    print("-" * 60)
    print(f"可用向量 (ok)       : {usable}  ({_pct(usable, total)})")
    print(f"退化向量 (常量/全零) : {kinds.get('degenerate', 0)}  "
          f"({_pct(kinds.get('degenerate', 0), total)})")
    print(f"零长向量 (empty)    : {kinds.get('empty', 0)}")
    print(f"缺向量 (missing)    : {kinds.get('missing', 0)}")
    print(f"不可用合计          : {bad}  ({_pct(bad, total)})")
    print("-" * 60)
    print(f"唯一向量数          : {stats['unique_vectors']}")

    if stats["per_type"]:
        print("-" * 60)
        print("按 type 明细:")
        for item_type, counter in sorted(stats["per_type"].items()):
            ok = counter.get("ok", 0)
            type_total = sum(counter.values())
            print(f"  {item_type:8s} 总 {type_total:4d} / ok {ok:4d} "
                  f"({_pct(ok, type_total)}) / 退化 {counter.get('degenerate', 0)}")

    if stats["sampled_ids"]:
        print("-" * 60)
        print(f"退化/缺失样本 id: {', '.join(stats['sampled_ids'])}")

    print("=" * 60)
    if total == 0:
        print("结论: 知识库为空，无法评估（先跑一次 /discover/refresh 采集）。")
    elif bad == 0:
        print("结论: 全部向量健康，排序可直接使用全量候选，无需额外过滤。")
    else:
        share = bad / total * 100
        verdict = "显著" if share >= 5 else "可控但不可忽略"
        print(f"结论: 不可用向量占比 {share:.1f}%（{verdict}）。")
        print("      排序前必须过滤 ok 之外的向量，否则会把「降级向量」当成兴趣信号。")
        print("      全库无可用向量时，必须回退随机，不要假装个性化生效。")


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库向量健康度审计（只读）")
    parser.add_argument("--json", action="store_true", help="额外输出原始统计 JSON")
    args = parser.parse_args()

    print("=" * 60)
    print("Audit KB Vectors - 知识库向量健康度审计")
    print("=" * 60)

    try:
        stats = audit()
    except Exception as e:  # noqa: BLE001 - 顶层失败要给出非零退出码
        logger.error(f"审计失败: {e}")
        print(f"✗ 审计失败（Qdrant 未启动？）: {e}")
        return 1

    _print_report(stats)

    if args.json:
        import json

        print(json.dumps(stats, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
