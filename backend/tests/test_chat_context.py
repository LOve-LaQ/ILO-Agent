# Tests - RAG 接进问答（chat_context）
"""`src/modules/rag/chat_context.py` + `/chat` 提示词组装的离线单测。

这一层守着 RAG 里**唯一直接决定回答质量**的一步，而它做错了**不会有任何报错**：
注入错了不报错、注入空了不报错、引用编号对不上也不报错 —— 只会让回答悄悄变差。
所以每条性质都单独钉：

1. **查询必须就是用户原话** —— 曾经拼过会话主题做锚点，实测把语料外的短问题
   从 0/4 误纳变成 4/4 误纳（见 `build_retrieval_query` 的实测表），已撤掉。
   这条是**回归守卫**：谁再把锚点加回来，这里必须红。
2. **取不到正文的命中要丢弃并重新编号** —— 留占位会让模型以为有 N 条资料。
3. **超预算丢尾部命中，不截断单块正文** —— 半块正文会制造「有依据但读不通」的段落。
4. **`degraded` 要能区分「系统坏了」和「确实没资料」** —— 前者该告警，后者是正常业务。
5. **提示词里的参考资料段是 RAG 的唯一生效点** —— LLM 在测试里被强制降级，
   接口层断言不到提示词，只能靠这里的纯函数。
6. **回填必须在别的线程里跑** —— 同步 DB 查询占住事件循环，同 worker 上所有请求排队。

全部用例零真实 API / Qdrant / PostgreSQL。
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from src.modules.rag import chat_context as C
from src.modules.rag import retriever as R
from src.modules.rag.retriever import ChunkHit, HydratedChunk, RetrievalOutcome


# ==================== 构造小工具 ====================


def _hit(
    card_id: str = "card-1",
    ordinal: int = 0,
    score: float = 0.60,
    title: str = "标题",
    **kw: Any,
) -> ChunkHit:
    return ChunkHit(card_id=card_id, ordinal=ordinal, score=score, title=title, **kw)


def _outcome(
    hits: Tuple[ChunkHit, ...] = (),
    reason: str = R.REASON_OK,
    best: float = 0.0,
    raw: int = 0,
) -> RetrievalOutcome:
    return RetrievalOutcome(
        query="q", hits=hits, best_score=best, reason=reason, raw_hits=raw
    )


def _searcher(outcome: RetrievalOutcome, calls: Optional[List[Any]] = None):
    """假检索器：记录调用参数，返回固定结果。"""

    async def _search(query: str, **kwargs: Any) -> RetrievalOutcome:
        if calls is not None:
            calls.append((query, kwargs))
        return outcome

    return _search


def _hydrator(texts: Dict[str, Optional[str]], calls: Optional[List[Any]] = None):
    """假回填：按 card_id 给正文，`None` 表示取不到。"""

    def _hydrate(hits: Any, **kwargs: Any) -> List[HydratedChunk]:
        if calls is not None:
            calls.append(list(hits))
        out = []
        for hit in hits:
            text = texts.get(hit.card_id)
            out.append(
                HydratedChunk(hit=hit, text=text, problem="" if text else "card_missing")
            )
        return out

    return _hydrate


def _boom_hydrator():
    def _hydrate(hits: Any, **kwargs: Any) -> List[HydratedChunk]:
        raise RuntimeError("postgres 挂了")

    return _hydrate


# ==================== 1. 查询构造（回归守卫：不许再加锚点） ====================


def test_query_is_the_raw_message_unchanged():
    """查询就是原话 —— 不截断、不改写、不拼任何前缀。"""
    assert C.build_retrieval_query("地震数据去噪用了什么方法") == "地震数据去噪用了什么方法"
    assert C.build_retrieval_query("再举个例子") == "再举个例子"


def test_short_question_is_not_anchored_with_topic():
    """**回归守卫**：短问题曾被拼上会话主题，实测把误纳从 0/4 推到 4/4。

    主题短语与语料高度相似，拼进去后不管问什么都自带约 0.2 的「像」，
    语料外的短问题被系统性抬过阈值。谁要把锚点加回来，先看
    `build_retrieval_query()` 里那张实测表，然后改这个用例。
    """
    assert C.build_retrieval_query("再举个例子") == "再举个例子"
    assert "地震数据去噪" not in C.build_retrieval_query("再举个例子")


def test_query_is_stripped():
    assert C.build_retrieval_query("  再举个例子  ") == "再举个例子"


def test_blank_message_yields_empty_query():
    assert C.build_retrieval_query("   ") == ""
    assert C.build_retrieval_query(None) == ""
    assert C.build_retrieval_query("") == ""


# ==================== 2. 参考资料段拼装 ====================


def test_block_skips_textless_hits_and_renumbers():
    """取不到正文的命中丢弃后**重新编号**，不留空号。"""
    hits = [_hit("c1"), _hit("c2"), _hit("c3")]
    chunks = _hydrator({"c1": "第一段正文", "c3": "第三段正文"})(hits)
    block, refs = C.build_reference_block(chunks)

    assert [r.ordinal for r in refs] == [1, 2]
    assert [r.card_id for r in refs] == ["c1", "c3"]
    assert "[1]" in block and "[2]" in block
    assert "[3]" not in block


def test_block_empty_when_nothing_usable():
    chunks = _hydrator({"c1": None})([_hit("c1")])
    block, refs = C.build_reference_block(chunks)
    assert block == ""
    assert refs == ()


def test_block_empty_for_empty_input():
    assert C.build_reference_block([]) == ("", ())


def test_block_drops_tail_hits_over_budget():
    """超预算丢**尾部**命中（保留分数最高的），而不是截断某一条。"""
    hits = [_hit("c1", score=0.9), _hit("c2", score=0.8)]
    long_text = "中" * 4000  # 估算远超预算
    chunks = _hydrator({"c1": long_text, "c2": "第二段"})(hits)
    block, refs = C.build_reference_block(chunks, max_tokens=1500)

    assert len(refs) == 1
    assert refs[0].card_id == "c1"
    assert "第二段" not in block


def test_block_truncates_when_first_hit_alone_exceeds_budget():
    """第一条就超预算是唯一例外：截断也要给一条，否则检索白做。"""
    chunks = _hydrator({"c1": "中" * 8000})([_hit("c1")])
    block, refs = C.build_reference_block(chunks, max_tokens=300)

    assert len(refs) == 1
    assert block
    assert C.estimate_tokens(block) <= 300 + 20  # 估算有 +1 常数项


def test_reference_carries_traceable_fields():
    """引用必须能回到「哪张卡、哪一节、哪个链接」—— 这是可溯源的最小集。"""
    hit = _hit(
        "card-9",
        score=0.7123,
        title="Scalpel",
        source_url="https://example.com/s",
        heading_path=("Usage", "Install"),
    )
    chunks = _hydrator({"card-9": "正文"})([hit])
    _, refs = C.build_reference_block(chunks)

    ref = refs[0]
    assert ref.ordinal == 1
    assert ref.card_id == "card-9"
    assert ref.title == "Scalpel"
    assert ref.source_url == "https://example.com/s"
    assert ref.heading_path == ("Usage", "Install")
    assert ref.score == 0.7123
    assert ref.excerpt == "正文"


def test_block_is_token_bounded_for_normal_case():
    """正常规模的 5 条必须落在预算内 —— 否则提示词会挤掉回答空间。"""
    hits = [_hit(f"c{i}") for i in range(5)]
    texts = {f"c{i}": "内容" * 300 for i in range(5)}  # 每条约 600 字符
    chunks = _hydrator(texts)(hits)
    block, _ = C.build_reference_block(chunks)
    assert C.estimate_tokens(block) <= C.MAX_CONTEXT_TOKENS


# ==================== 3. 编排（build_chat_context） ====================


def test_blank_message_never_calls_searcher():
    calls: List[Any] = []
    ctx = asyncio.run(
        C.build_chat_context("  ", searcher=_searcher(_outcome(), calls))
    )
    assert ctx.reason == R.REASON_EMPTY_QUERY
    assert ctx.injected is False
    assert calls == []


def test_no_hits_passes_reason_through_and_skips_hydrate():
    """没命中就不该去 PostgreSQL 取正文 —— 白跑一次查询。"""
    hcalls: List[Any] = []
    ctx = asyncio.run(
        C.build_chat_context(
            "一个足够长的自足问题",
            searcher=_searcher(_outcome(reason=R.REASON_NO_HITS, raw=0)),
            hydrator=_hydrator({}, hcalls),
        )
    )
    assert ctx.reason == R.REASON_NO_HITS
    assert ctx.injected is False
    assert hcalls == []


def test_below_threshold_is_not_degraded():
    """「有返回但都不够像」是正常业务，不该告警。"""
    ctx = asyncio.run(
        C.build_chat_context(
            "一个足够长的自足问题",
            searcher=_searcher(
                _outcome(reason=R.REASON_BELOW_THRESHOLD, best=0.28, raw=5)
            ),
            hydrator=_hydrator({}),
        )
    )
    assert ctx.reason == R.REASON_BELOW_THRESHOLD
    assert ctx.degraded is False


def test_system_failure_is_degraded():
    """Qdrant 挂了要能被识别成降级，否则告警口径分不清。"""
    ctx = asyncio.run(
        C.build_chat_context(
            "一个足够长的自足问题",
            searcher=_searcher(_outcome(reason=R.REASON_QDRANT_ERROR)),
            hydrator=_hydrator({}),
        )
    )
    assert ctx.degraded is True


def test_hydrate_exception_degrades_instead_of_raising():
    """取正文抛异常必须被吞掉 —— 检索是锦上添花，不能把问答带崩。"""
    ctx = asyncio.run(
        C.build_chat_context(
            "一个足够长的自足问题",
            searcher=_searcher(_outcome(hits=(_hit("c1"),))),
            hydrator=_boom_hydrator(),
        )
    )
    assert ctx.reason == C.REASON_HYDRATE_FAILED
    assert ctx.degraded is True
    assert ctx.injected is False


def test_all_textless_degrades_with_dedicated_reason():
    """命中都在但正文全取不到：这是数据问题，要和「语料里没有」区分开。"""
    ctx = asyncio.run(
        C.build_chat_context(
            "一个足够长的自足问题",
            searcher=_searcher(_outcome(hits=(_hit("c1"), _hit("c2")))),
            hydrator=_hydrator({"c1": None, "c2": None}),
        )
    )
    assert ctx.reason == C.REASON_NO_USABLE_TEXT
    assert ctx.degraded is True


def test_happy_path_injects_and_keeps_reason_ok():
    calls: List[Any] = []
    ctx = asyncio.run(
        C.build_chat_context(
            "再举个例子",
            searcher=_searcher(_outcome(hits=(_hit("c1"),)), calls),
            hydrator=_hydrator({"c1": "块正文"}),
        )
    )
    assert ctx.injected is True
    assert ctx.reason == R.REASON_OK
    assert ctx.degraded is False
    assert len(ctx.references) == 1
    assert "块正文" in ctx.block
    # 送去检索的必须是原话本身（加了锚点会让误纳率从 0/4 变 4/4）
    assert calls[0][0] == "再举个例子"


def test_hydrate_runs_in_thread_not_on_event_loop():
    """回填是同步 DB 查询：必须在**别的线程**里跑，否则会独占事件循环。"""
    import threading

    seen: Dict[str, int] = {}

    def _hydrate(hits: Any, **kwargs: Any) -> List[HydratedChunk]:
        seen["thread"] = threading.get_ident()
        return [HydratedChunk(hit=h, text="正文") for h in hits]

    main_thread = threading.get_ident()
    asyncio.run(
        C.build_chat_context(
            "一个足够长的自足问题",
            searcher=_searcher(_outcome(hits=(_hit("c1"),))),
            hydrator=_hydrate,
        )
    )
    assert seen["thread"] != main_thread


def test_searcher_receives_deadline_and_threshold():
    """闸门参数必须真的传下去 —— 传丢了不会报错，只是保护失效。"""
    calls: List[Any] = []
    asyncio.run(
        C.build_chat_context(
            "一个足够长的自足问题",
            deadline=123.0,
            top_k=3,
            min_score=0.42,
            searcher=_searcher(_outcome(), calls),
        )
    )
    _, kwargs = calls[0]
    assert kwargs["deadline"] == 123.0
    assert kwargs["top_k"] == 3
    assert kwargs["min_score"] == 0.42


# ==================== 4. /chat 提示词组装 ====================


def test_system_prompt_without_reference_has_no_grounding_rule():
    """没有资料时不能要求「资料里没有就直说」—— 那会逼模型对不存在的资料表态。"""
    from src.api.routes.learning import _build_chat_system_prompt

    prompt = _build_chat_system_prompt(
        {"topic": "T", "summary": "S", "core_concepts": ["a", "b"]}
    )
    assert "【参考资料】" not in prompt
    assert "资料里没有" not in prompt
    assert "主题：T" in prompt
    assert "核心概念：a, b" in prompt


def test_system_prompt_with_reference_adds_grounding_rule():
    from src.api.routes.learning import _build_chat_system_prompt

    prompt = _build_chat_system_prompt(
        {"topic": "T", "summary": "S", "core_concepts": []},
        reference_block="【参考资料】（来自项目知识库，按相关度从高到低）\n[1] 标题\n正文",
    )
    assert "【参考资料】" in prompt
    assert "资料里没有" in prompt
    # 引用编号要在要求里点明，否则模型不知道 [1] 从哪来
    assert "`[1]`" in prompt


def test_system_prompt_does_not_embed_history():
    """历史按真实消息角色发送，不再压进系统提示词。"""
    from src.api.routes.learning import _build_chat_system_prompt

    prompt = _build_chat_system_prompt({"topic": "T"})
    assert "【当前对话历史】" not in prompt


def test_messages_roles_and_current_question_appended():
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from src.api.routes.learning import _build_chat_messages

    history = [
        {"role": "user", "content": "第一个问题"},
        {"role": "assistant", "content": "第一个回答"},
    ]
    messages = _build_chat_messages("SYS", history, "第二个问题")

    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert isinstance(messages[2], AIMessage)
    assert isinstance(messages[-1], HumanMessage)
    assert messages[-1].content == "第二个问题"


def test_messages_dedupe_trailing_current_question():
    """前端会先把用户气泡 append 进 history，不去重模型会看到两遍同一个问题。"""
    from src.api.routes.learning import _build_chat_messages

    history = [
        {"role": "user", "content": "老问题"},
        {"role": "assistant", "content": "老回答"},
        {"role": "user", "content": "本轮问题"},
    ]
    messages = _build_chat_messages("SYS", history, "本轮问题")
    contents = [m.content for m in messages]
    assert contents.count("本轮问题") == 1
    assert contents[-1] == "本轮问题"


def test_messages_keep_current_question_when_history_lacks_it():
    """history 里没有本轮提问时（纯 API 调用方），必须由我们补上。"""
    from src.api.routes.learning import _build_chat_messages

    messages = _build_chat_messages("SYS", [], "本轮问题")
    assert len(messages) == 2
    assert messages[-1].content == "本轮问题"


def test_messages_cap_history_turns():
    """历史必须封顶，否则长历史会把参考资料和回答一起挤出上下文。"""
    from src.api.routes.learning import _CHAT_HISTORY_TURNS, _build_chat_messages

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(50)
    ]
    messages = _build_chat_messages("SYS", history, "本轮问题")
    # 1 条 system + 封顶的历史 + 1 条本轮提问
    assert len(messages) == 1 + _CHAT_HISTORY_TURNS + 1
