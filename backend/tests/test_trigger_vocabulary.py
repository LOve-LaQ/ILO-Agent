# Tests - 受控词表与常量同步
"""守住「同一个词表散落在四处」这件事。

`trigger` 与 `status` 是受控词表，但它们的定义分散在四个地方：

    models/collection.py         BATCH_TRIGGERS / BATCH_STATUSES  ← 词表本身
    services/collection_service.py  BATCH_TRIGGER_* 常量 + 终态映射  ← 运行时的值
    schemas/discover.py          字段 description                ← 对外契约（前端照着写分支）
    models/collection.py          列注释                          ← 给人看的

只改其中一处不会报错、不会崩测试，只会在某个时刻悄悄失配：加了 `script` 却忘了写进
`BATCH_TRIGGERS`，或者后端多了一个 status 而前端 `switch` 落进 else 分支把失败当成功
显示。这类 bug 没有异常、没有日志，只能靠静态守卫。

## 为什么用 AST 而不是正则

正则分不清「代码」和「解释这件事的散文」。本仓库的注释里就大量出现
`manual | scheduled | script` 这类字面量，正则守卫会把注释也当成代码来比对 —— 而
**守卫的误报比漏报更致命**：一旦开始误报，人就会把它当噪声关掉，等于没守卫。
"""

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
MODELS_PY = BACKEND / "src" / "models" / "collection.py"
SERVICE_PY = BACKEND / "src" / "services" / "collection_service.py"


def _literal_assignment(path: Path, name: str):
    """取出模块级 `NAME = <字面量>` 的值（不 import，避免拉起整个应用）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} 里找不到模块级赋值 {name}")


def _string_constants(path: Path, prefix: str) -> dict:
    """取出所有 `NAME = "值"` 且名字以 prefix 开头的模块级字符串常量。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or not target.id.startswith(prefix):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                found[target.id] = node.value.value
    return found


# ---------------------------------------------------------------- 词表 vs 常量

def test_trigger_constants_match_vocabulary():
    """`BATCH_TRIGGER_*` 常量的值必须恰好等于 `BATCH_TRIGGERS` 词表。"""
    vocabulary = set(_literal_assignment(MODELS_PY, "BATCH_TRIGGERS"))
    constants = _string_constants(SERVICE_PY, "BATCH_TRIGGER_")

    # 守卫自检：解析不出常量说明 AST 提取逻辑失效了，不能让测试假绿
    assert constants, "没能从 collection_service.py 提取到任何 BATCH_TRIGGER_* 常量"

    assert set(constants.values()) == vocabulary, (
        f"trigger 词表与常量失配：\n"
        f"  词表 BATCH_TRIGGERS = {sorted(vocabulary)}\n"
        f"  常量 = {dict(sorted(constants.items()))}"
    )


def test_every_trigger_has_a_constant():
    """词表里的每个触发方式都要有对应的常量，否则调用点只能硬编码字符串。"""
    vocabulary = set(_literal_assignment(MODELS_PY, "BATCH_TRIGGERS"))
    constant_values = set(_string_constants(SERVICE_PY, "BATCH_TRIGGER_").values())
    assert vocabulary == constant_values, (
        f"这些 trigger 没有常量可用（只能写裸字符串）：{sorted(vocabulary - constant_values)}"
    )


def test_trigger_vocabulary_has_no_duplicates():
    """元组写重了会让「集合相等」类断言失真，先钉住它本身是干净的。"""
    raw = _literal_assignment(MODELS_PY, "BATCH_TRIGGERS")
    assert len(raw) == len(set(raw)), f"BATCH_TRIGGERS 有重复项：{raw}"


# ---------------------------------------------------------------- 词表 vs 注释 / 契约

def test_trigger_column_comment_lists_all_triggers():
    """列注释是给人看的词表，加了 trigger 却漏改注释会让人以为只有两种。"""
    source = MODELS_PY.read_text(encoding="utf-8")
    vocabulary = _literal_assignment(MODELS_PY, "BATCH_TRIGGERS")

    marker = "trigger: Mapped[str]"
    assert marker in source, "trigger 列定义变了，本测试需要同步更新"
    # 取列定义正上方那一行注释
    above = source[: source.index(marker)].rstrip().splitlines()[-1]

    missing = [t for t in vocabulary if t not in above]
    assert not missing, f"trigger 列注释 {above.strip()!r} 漏了 {missing}"


def test_provenance_schema_description_lists_all_triggers():
    """前端照着 description 写分支，漏一个就会落进 else 分支被当默认值。"""
    from src.models.collection import BATCH_TRIGGERS
    from src.schemas.discover import ProvenanceBatch

    description = ProvenanceBatch.model_fields["trigger"].description or ""
    missing = [t for t in BATCH_TRIGGERS if t not in description]
    assert not missing, f"ProvenanceBatch.trigger 描述 {description!r} 漏了 {missing}"


# ---------------------------------------------------------------- 终态映射

def _terminal_status_map() -> set:
    """从 `collect_items` 里取出 `{...}[batch_status]` 那个字面量字典的键。"""
    tree = ast.parse(SERVICE_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        # 只认「终态 -> 对外 status」这个映射，靠键集合特征识别
        if {"succeeded", "partial", "failed"} <= keys:
            return keys
    raise AssertionError("没能从 collect_items 里找到批次终态 -> 对外 status 的映射")


def test_batch_status_map_covers_every_terminal_status():
    """批次终态一旦多一个（比如加 `cancelled`），映射查不到键会直接 KeyError 崩在线上。"""
    terminal = set(_literal_assignment(MODELS_PY, "BATCH_STATUSES")) - {"running"}
    mapped = _terminal_status_map()
    assert mapped == terminal, (
        f"终态映射与 BATCH_STATUSES 失配：\n"
        f"  词表（去掉 running）= {sorted(terminal)}\n"
        f"  映射键 = {sorted(mapped)}"
    )


def test_running_is_not_a_terminal_status():
    """`running` 是「进行中」，不该出现在终态映射里 —— 否则 finish 时可能落回 running。"""
    assert "running" not in _terminal_status_map()


@pytest.mark.parametrize("constant", ["BATCH_TRIGGER_MANUAL", "BATCH_TRIGGER_SCHEDULED", "BATCH_TRIGGER_SCRIPT"])
def test_trigger_constants_are_exported(constant):
    """常量要能从 services 包直接拿到，否则脚本只能从子模块深挖。"""
    import src.services as services

    assert hasattr(services, constant), f"src.services 未导出 {constant}"
    assert constant in services.__all__, f"{constant} 不在 src.services.__all__ 里"


def test_batch_trigger_script_is_wired_to_backfill_script():
    """新加的 `script` 若没有任何调用点，说明常量是死的 —— 要么接线要么删掉。"""
    script = BACKEND / "backfill_repos_30d.py"
    assert script.exists(), "backfill_repos_30d.py 不见了"
    source = script.read_text(encoding="utf-8")
    assert "BATCH_TRIGGER_SCRIPT" in source, (
        "灌库脚本没有用 BATCH_TRIGGER_SCRIPT，批次表里就分不出「谁触发的」"
    )
