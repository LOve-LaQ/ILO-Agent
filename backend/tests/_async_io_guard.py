# Tests - AST 分析器：检测「async 函数体内直接调用同步 IO」
"""为什么用 AST 而不是正则：

「别在 async 里做同步 IO」这条规则，用正则搜 `grep "kb.sample("` 会有两类必然的错误 ——
一类是**漏报**（`kb` 换个变量名就搜不到），一类是**误报**（注释、字符串、以及 `await`
的异步调用都会被算进去，比如 `await kb.upsert(...)` 里的 `upsert`）。

所以这里做三件事，都是正则做不到的：

1. **排除 `await` 的调用** —— `await` 说明被调方是协程，不阻塞事件循环。
   漏掉这一步会把 `await kb.upsert(item)` 误判为违规。
2. **资源污染跟踪** —— `kb = get_knowledge_base()` 之后，`kb` 这个变量被标记为
   「持有同步 IO 对象」，它上面的任何方法调用都要检查。这样 `kb.count()` / `kb.sample()`
   能被抓到，而不必把 `count` / `sample` 这种会与 `dict` / `list` / `random` 撞名的
   属性名写进黑名单。
   关键限定：**只有 IO 工厂的返回值才传播**。`find_cards(ids)` 也做 IO，但它返回的是
   数据（字典），`cards.get(id)` 只是查字典 —— 不区分「资源」与「数据」会制造大量误报。
3. **本地同步函数传染** —— 像 `_personalized_items()` 这种「同步 def，内部做 IO，
   被 async 路由直接调用」的写法，是最容易被漏掉的一类。把模块内所有同步函数先分类，
   再检查 async 函数是否调用了「带 IO 的同步函数」。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# 黑名单：只收「在项目里唯一指向同步 IO 实现」的名字与属性。
# 判断标准是「会不会与内置类型的方法撞名」——`count` / `get` / `set` 这类不收，
# 它们交给变量污染跟踪去抓，否则会把 `list.count()` / `dict.get()` 全误伤。
# ---------------------------------------------------------------------------

# 指向同步函数的「名字」。
#
# 收录标准：该名字在项目里唯一指向一个**同步实现**，且该实现会做阻塞 IO
# （网络 / 数据库 / Redis / 文件）。纯计算与内存操作不收 —— 它们不阻塞事件循环。
# 名单按「提供方模块」分组，便于新增服务时对照补全。
KNOWN_IO_NAMES = frozenset(
    {
        # ---- modules/discovery/tech_knowledge.py（Qdrant + Redis）----
        "get_knowledge_base",
        "get_vectors_by_ids",
        "candidate_points",
        "all_cards",
        "list_by_category",
        "get_by_id",
        "get_by_ids",
        "is_crawled",
        "mark_crawled",
        # ---- api/routes/discover.py 内的同步 Redis 缓存助手 ----
        "get_redis_client",
        "get_feed_cache",
        "save_feed_cache",
        # ---- services/interest_profile.py（PostgreSQL + Redis + Qdrant）----
        "build_user_profile",
        "collect_signals",
        # ---- services/activity_service.py（PostgreSQL）----
        "log_activity",
        "list_activities",
        "count_activities",
        # ---- services/bookmark_service.py（PostgreSQL）----
        "add_bookmark",
        "remove_bookmark",
        "is_bookmarked",
        "get_bookmarked_ids",
        "list_bookmarks",
        "count_bookmarks",
        # ---- services/learning_service.py（PostgreSQL）----
        "upsert_session",
        "update_session",
        "sync_state_from_context",
        "append_messages",
        "complete_session_record",
        "load_session",
        "list_messages",
        "count_messages",
        "list_sessions",
        "get_session",
        "count_messages_by_session",
        "session_stats",
        # ---- services/card_service.py（内部走知识库，即网络 IO）----
        "find_card",
        "find_cards",
        # ---- services/collection_service.py（PostgreSQL + Redis）----
        "start_batch",
        "finish_batch",
        "get_collected_ids",
        "is_collected",
        "get_provenance",
        "record_item",
        "backfill_records",
        "_write_content_snapshot",
        "_write_content_digest",
        # ---- 惰性单例工厂：首次构造会连下游，甚至是 8s 级的惰性 import ----
        "get_memory_manager",
        "get_state_machine",
        # ---- core/db.py ----
        "get_session_factory",
        # ---- 通用 ----
        "sleep",
    }
)

# 指向同步方法的「属性名」：只收几乎不可能与内置类型撞名的。
#
# 刻意**不**收 `sample`：`random.sample(pool, n)` 是纯计算，收进来会把首页路由的
# 随机兜底误判为违规。`kb.sample()` 由下面的「工厂污染」规则负责抓。
KNOWN_IO_ATTRS = frozenset(
    {
        "scroll",
        "upsert",
        "query_points",
        "setex",
        "candidate_points",
        "get_vectors_by_ids",
        "build_user_profile",
    }
)

# 「返回 IO 对象」的惰性工厂：只有这些函数的返回值会被标记为「持有 IO 对象」，
# 它上面的任何方法调用才算同步 IO。
#
# 为什么必须限定在工厂，而不能对所有 IO 调用都传播：`find_cards(ids)` 也做 IO，
# 但它**返回的是数据**（`{id: card}` 字典），`cards.get(id)` 只是查字典。
# 若不区分「资源」与「数据」，`session_ctx.get()` / `counts.get()` / `context.get()`
# 全会被误报 —— 误报比漏报更致命，它会让守卫被当成噪声然后被关掉。
IO_FACTORY_NAMES = frozenset(
    {
        "get_knowledge_base",
        "get_redis_client",
        "get_memory_manager",
        "get_state_machine",
    }
)

# 把同步调用卸载到线程的包装器：出现在这些调用的实参里 = 已被安全卸载
SAFE_WRAPPERS = frozenset({"to_thread", "run_in_executor"})

# 调度协程的组合器：它们的实参本身是协程，不是「同步调用」
ASYNC_COMBINATORS = frozenset(
    {"gather", "wait", "wait_for", "create_task", "ensure_future", "shield", "as_completed"}
)

# 行内豁免标记：确认某处同步 IO 是有意为之时，在该行加注释
ALLOW_MARKER = "async-io-guard: allow"


@dataclass(frozen=True)
class Violation:
    file: str
    function: str
    lineno: int
    callee: str
    reason: str

    def render(self) -> str:
        return f"{self.file}:{self.lineno}  async {self.function}()  ->  {self.callee}  [{self.reason}]"


def _callee(node: ast.Call) -> tuple[Optional[str], Optional[str]]:
    """Return (name, attr) of the called thing."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id, None
    if isinstance(func, ast.Attribute):
        return None, func.attr
    return None, None


def _receiver(node: ast.Call) -> Optional[str]:
    """For `kb.count(...)` return 'kb'; for anything else return None."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    mapping: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            mapping[id(child)] = node
    return mapping


def _is_awaited(node: ast.Call, parents: dict[int, ast.AST]) -> bool:
    """`await foo()` -> the Call's parent is an Await node."""
    return isinstance(parents.get(id(node)), ast.Await)


def _is_combinator_arg(node: ast.Call, parents: dict[int, ast.AST]) -> bool:
    """`asyncio.gather(foo(), bar())` -> the calls are coroutines, not sync calls."""
    parent = parents.get(id(node))
    if isinstance(parent, ast.Call):
        _, attr = _callee(parent)
        name = attr or (parent.func.id if isinstance(parent.func, ast.Name) else None)
        return name in ASYNC_COMBINATORS
    return False


def _safe_nodes(func: ast.AST) -> set[int]:
    """Node ids sitting inside a to_thread / run_in_executor call (recursively)."""
    safe: set[int] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        _, attr = _callee(node)
        name = attr or (node.func.id if isinstance(node.func, ast.Name) else None)
        if name in SAFE_WRAPPERS:
            for arg in node.args:
                for inner in ast.walk(arg):
                    safe.add(id(inner))
    return safe


def _tainted_names(func: ast.AST) -> set[str]:
    """Fixpoint: which local variables hold an object that does sync IO?

    Only assignments fed by an IO *factory* propagate:

        kb = get_knowledge_base()   -> kb tainted (kb.count() is IO)
        mm = get_memory_manager()   -> mm tainted
        kb2 = kb                    -> kb2 tainted

        cards = find_cards(ids)     -> NOT tainted (cards is plain data,
                                       cards.get(id) is a dict lookup)
    """
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(func):
            targets: list[ast.expr] = []
            value: Optional[ast.expr] = None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.NamedExpr):  # walrus
                targets, value = [node.target], node.value
            if value is None:
                continue

            sources: set[str] = set()
            if isinstance(value, ast.Call):
                base, _ = _callee(value)
                if base:
                    sources.add(base)
            elif isinstance(value, ast.Name):
                sources.add(value.id)
            elif isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                sources.add(value.value.id)

            if sources & (IO_FACTORY_NAMES | tainted):
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in tainted:
                        tainted.add(target.id)
                        changed = True
    return tainted


def _scan_calls(func: ast.AST, io_names: set[str], parents: dict[int, ast.AST],
                source_lines: list[str]) -> Iterator[tuple[int, str, str]]:
    """Yield (lineno, callee_repr, reason) for sync IO calls inside `func`."""
    safe = _safe_nodes(func)
    tainted = _tainted_names(func)

    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or id(node) in safe:
            continue
        if _is_awaited(node, parents) or _is_combinator_arg(node, parents):
            continue

        line = source_lines[node.lineno - 1] if node.lineno - 1 < len(source_lines) else ""
        if ALLOW_MARKER in line:
            continue

        base, attr = _callee(node)
        receiver = _receiver(node)

        if receiver is not None and receiver in tainted:
            yield node.lineno, f"{receiver}.{attr}()", f"{receiver} 来自同步 IO 工厂"
        elif attr in KNOWN_IO_ATTRS:
            yield node.lineno, f".{attr}()", "同步 IO 方法"
        elif attr is None and base in io_names:
            yield node.lineno, f"{base}()", "同步 IO 函数"


def _function_has_io(func: ast.AST, io_names: set[str], parents: dict[int, ast.AST],
                      source_lines: list[str]) -> bool:
    return next(_scan_calls(func, io_names, parents, source_lines), None) is not None


def analyze_source(source: str, filename: str) -> list[Violation]:
    """Analyze one module's source text and return every violation found."""
    tree = ast.parse(source, filename=filename)
    lines = source.splitlines()
    parents = _parents(tree)

    # Pass 1: which locally-defined *sync* functions do IO?
    io_names: set[str] = set(KNOWN_IO_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not isinstance(
            node, ast.AsyncFunctionDef
        ):
            if _function_has_io(node, io_names, parents, lines):
                io_names.add(node.name)

    # Pass 2: scan every async function for sync IO it calls directly
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for lineno, callee, reason in _scan_calls(node, io_names, parents, lines):
            violations.append(
                Violation(
                    file=filename,
                    function=node.name,
                    lineno=lineno,
                    callee=callee,
                    reason=reason,
                )
            )
    return sorted(violations, key=lambda v: v.lineno)


def analyze_paths(paths: list[Path]) -> list[Violation]:
    out: list[Violation] = []
    for path in paths:
        out.extend(analyze_source(path.read_text(encoding="utf-8"), path.name))
    return out
