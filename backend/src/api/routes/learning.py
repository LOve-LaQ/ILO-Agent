# Learning Routes - Learning Session Management API
"""
学习会话 API
- POST /api/v1/learning/session: 创建新会话
- POST /api/v1/learning/push: 发送推送通知
- POST /api/v1/learning/response: 处理用户响应
- POST /api/v1/learning/chat: 基于当前学习内容的问答
- POST /api/v1/learning/quiz: 提交测验答案
- POST /api/v1/learning/complete: 完成学习会话

契约：
- 请求/响应统一使用 src/schemas/learning.py 的模型
- 会话上下文按「内存 → Redis → PostgreSQL」三级读取，读不到返回 SESSION_NOT_FOUND，
  不再硬编码占位上下文
- 会话/对话/测验/完成统一落 PostgreSQL（真相源），Redis 仅作 TTL 1h 的热缓存
"""

from datetime import datetime, timedelta, timezone
import os
import time
import uuid

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Request
from loguru import logger

from src.api.deps import CurrentUser
from src.core.errors import ERROR_RESPONSES, ILOException
from src.core.rate_limit import LEARNING_CHAT_IP, rate_limit
from src.schemas.learning import (
    ChatRequest,
    ChatResponse,
    CompleteRequest,
    CompleteResponse,
    LearningHealthResponse,
    PushRequest,
    PushResponse,
    QuizRequest,
    QuizResponse,
    ResponseProcessRequest,
    ResponseProcessResponse,
    SessionContext,
    SessionCreateRequest,
    SessionCreateResponse,
)
from src.services.activity_service import log_activity
from src.services.learning_service import (
    append_messages,
    complete_session_record,
    load_session,
    parse_datetime,
    state_value,
    sync_state_from_context,
    upsert_session,
)

load_dotenv()

router = APIRouter(prefix="/learning", tags=["Learning"], responses=ERROR_RESPONSES)


# 初始化状态机（单例）
state_machine = None


def get_state_machine():
    """获取或创建状态机实例

    延迟导入 src.modules.agent：它会连带拉起 langchain / transformers / torch
    （约 8s），而 /auth、/discover 等启动路径并不需要它。放到函数体内后，只有真正
    调用学习接口时才付出这次导入成本，应用启动不再为它买单。
    """
    global state_machine
    if state_machine is None:
        from src.modules.agent import LearningStateMachine

        config = {
            "max_tokens": 4096,
            "num_questions": 3
        }
        state_machine = LearningStateMachine(config)
        logger.info("✅ LearningStateMachine initialized")

    return state_machine


# 记忆管理器（单例，用于会话上下文持久化）
memory_manager = None
_memory_manager_ready = False
# 初始化失败后允许重试的最早时刻（monotonic 秒）。
# 【为什么必须允许重试】Redis / Qdrant 晚于应用就绪是常态（本机 Docker 尤其如此）。
# 若一次失败就永久置位「已初始化」，那么即使依赖随后恢复，会话上下文的 Redis
# 热缓存也会一直失效到下次重启进程 —— 等于把「依赖暂时没起来」放大成「整个进程
# 生命周期降级」。退避 30s 既让恢复能自愈，又不会每请求都去重连。
_memory_manager_retry_at = 0.0
_MEMORY_MANAGER_RETRY_SECONDS = 30.0

# 进程启动时刻：健康检查上报**真实**运行时长（此前写死 "24h"，没有任何信息量）
_PROCESS_STARTED_AT = datetime.now(timezone.utc)


def get_memory_manager():
    """获取或创建 MemoryManager 实例；Redis/Qdrant 不可用时返回 None（不阻塞主流程）

    失败后按 `_MEMORY_MANAGER_RETRY_SECONDS` 退避重试，依赖恢复后自动自愈。
    """
    global memory_manager, _memory_manager_ready, _memory_manager_retry_at
    if _memory_manager_ready:
        return memory_manager

    now = time.monotonic()
    if now < _memory_manager_retry_at:
        return None

    try:
        from src.modules.agent.memory_manager import MemoryManager
        memory_manager = MemoryManager({
            "redis_url": os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            "qdrant_url": os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
        })
        _memory_manager_ready = True
        logger.info("✅ MemoryManager initialized (Redis + Qdrant)")
    except Exception as e:
        memory_manager = None
        _memory_manager_retry_at = now + _MEMORY_MANAGER_RETRY_SECONDS
        logger.warning(
            f"[WARN] MemoryManager unavailable, falling back to in-memory: {e}"
            f"（{int(_MEMORY_MANAGER_RETRY_SECONDS)} 秒后重试）"
        )
    return memory_manager


def _format_uptime(delta: timedelta) -> str:
    """把运行时长格式化成可读字符串（天/时/分）"""
    seconds = max(0, int(delta.total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _redis_persistable(context: dict) -> dict:
    """挑出可安全 JSON 化的会话字段，写入 Redis 热缓存。

    start_time / current_state / quiz_questions 此前没有进缓存，于是会话从 Redis
    恢复后会连坏三处：elapsed_minutes 恒为 0（起点被兜底成「现在」）、/quiz 因缺
    quiz_questions 直接 409、DB 里的状态无从镜像。这里一次性补齐。
    """
    start_time = context.get("start_time")
    return {
        "session_id": context.get("session_id"),
        "user_id": context.get("user_id"),
        "news_item_id": context.get("news_item_id"),
        "topic": context.get("topic", ""),
        "summary": context.get("summary", ""),
        "core_concepts": context.get("core_concepts") or [],
        "time_budget_minutes": context.get("time_budget_minutes", 15),
        "preferred_depth": context.get("preferred_depth", "medium"),
        # datetime 过不了 json.dumps，统一转 ISO 字符串；读回来由 _revive_context 还原
        "start_time": start_time.isoformat() if isinstance(start_time, datetime) else start_time,
        "current_state": state_value(context.get("current_state")),
        "quiz_questions": context.get("quiz_questions"),
    }


def _cache_session_context(context: dict) -> None:
    """把上下文写回 Redis（best-effort，缓存不可用不影响学习流程）"""
    mm = get_memory_manager()
    if mm is None:
        return
    try:
        mm.save_session_context(context["session_id"], _redis_persistable(context))
    except Exception as e:
        logger.warning(f"[WARN] Failed to persist session context: {e}")


def _revive_context(context: dict) -> dict:
    """把 JSON 化过的 start_time 还原成 datetime（就地修改并返回）。

    complete_session 用 `now - start_time` 算时长，字符串参与减法会抛 TypeError；
    而这一步发生在用户点「完成」之后，炸掉的代价很高，所以在会话入口统一收口。
    """
    if not isinstance(context.get("start_time"), datetime):
        context["start_time"] = parse_datetime(context.get("start_time")) or datetime.now(
            timezone.utc
        )
    return context


def _load_session_context(session_id: str, user_id: str) -> dict:
    """加载会话上下文：内存优先（含 quiz_questions / start_time），其次 Redis，
    最后回落 PostgreSQL 真相源。

    读取不到时抛 SESSION_NOT_FOUND 业务错误，取代此前硬编码的占位上下文
    （topic="Rust 内存安全"），避免用假数据误导用户。

    权限边界：会话归属者与当前登录用户不一致时抛 FORBIDDEN，
    防止拿到别人的 session_id 就能读到其学习内容。
    """
    if not session_id:
        raise ILOException("SESSION_ID_REQUIRED", "缺少 session_id。", status_code=400)

    sm = get_state_machine()
    ctx = sm.active_sessions.get(session_id)

    if ctx is None:
        mm = get_memory_manager()
        if mm is not None:
            try:
                ctx = mm.get_session_context(session_id)
            except Exception as e:
                logger.warning(f"[WARN] Failed to load session context from Redis: {e}")
                ctx = None
            if ctx:
                ctx.setdefault("session_id", session_id)

    if ctx is None:
        # 第三级：PostgreSQL 真相源。Redis 重启 / TTL 到期不再等于「学习记录丢失」。
        ctx = load_session(session_id)
        # 已完成的会话按「不存在」处理：与「完成后清 Redis」保持同一语义，
        # 否则会复活 —— 再次 /complete 返回 200、/quiz 走奇怪的降级分支。
        if ctx is not None and state_value(ctx.get("current_state")) == "completed":
            ctx = None
        if ctx is not None:
            _revive_context(ctx)
            # 回填热缓存，让后续请求重新走快速路径
            _cache_session_context(ctx)
            logger.info(f"♻️ Session context recovered from PostgreSQL: {session_id}")

    if not ctx:
        raise ILOException(
            "SESSION_NOT_FOUND",
            "学习会话不存在或已过期，请重新打开卡片发起讲解。",
            status_code=404,
        )

    _revive_context(ctx)

    owner = ctx.get("user_id")
    if owner and owner != user_id:
        raise ILOException("SESSION_FORBIDDEN", "该学习会话不属于当前账号。", status_code=403)

    return ctx


def _find_news_item(news_item_id: str):
    """根据资讯 ID 查找资讯内容（三级降级：内置示例 → 知识库 → 抓取缓存）

    实现已收敛到 `src.services.card_service`：收藏列表、卡片溯源都要同一套语义，
    三处各自演化很容易出现「这里查得到、那里查不到」的不一致。
    """
    from src.services.card_service import find_card

    return find_card(news_item_id)


def _generate_fallback_response(question: str, context: dict) -> str:
    """LLM 不可用时的回答。

    【绝不在降级路径里编造技术内容】旧实现在这里返回一段模板化的「专家回答」：
    不管问的是什么主题，都会说「**性能提升 10 倍**」「非常适合 Python 数据处理」，
    并附上与主题无关的 Pydantic 示例代码。那不是降级，那是伪造 —— 用户无法分辨
    真假，而这套系统的定位是**可溯源**的技术情报与讲解。宁可明确告知不可用，
    也不能输出看似可信的假内容。

    保留参数签名是为了让调用点不必分支；`context['topic']` 只用于让用户确认
    系统认对了主题。
    """
    topic = context.get("topic") or "当前主题"
    return (
        f"抱歉，AI 讲解服务当前不可用，我无法就「{topic}」给出可靠回答。\n\n"
        "这通常是模型服务未配置或临时故障，请稍后重试；"
        "历史对话已保存，可在「我的 → 学习记录」中回看。"
    )


@router.post("/session", response_model=SessionCreateResponse)
async def create_learning_session(
    request: Request, data: SessionCreateRequest, user: CurrentUser
) -> SessionCreateResponse:
    """
    创建新的学习会话

    ## 返回
    - session_id: 唯一会话标识
    - context: 初始上下文信息
    """
    sm = get_state_machine()

    # 根据资讯 ID 查找真实资讯内容；查不到时用请求体兜底字段构造
    news_item = _find_news_item(data.news_item_id)
    if news_item is None:
        news_item = {
            "id": data.news_item_id,
            "title": data.title or "未知技术主题",
            "summary": data.summary or "",
            "core_concepts": data.core_concepts,
        }

    try:
        session = await sm.create_session(
            session_id=f"sess_{uuid.uuid4().hex[:8]}",
            user_id=str(user.id),
            news_item=news_item,
            time_budget=data.time_budget,
            preferred_depth=data.preferred_depth
        )
    except Exception as e:
        logger.error(f"❌ Failed to create session: {e}")
        raise ILOException("SESSION_CREATE_FAILED", "创建学习会话失败，请稍后重试。", status_code=500)

    logger.info(f"✅ Session created: {session['session_id']}")

    # 持久化会话上下文到 Redis（重启后端 / 刷新页面不丢）
    _cache_session_context(session)

    # 落库：PostgreSQL 才是真相源，Redis 只是 TTL 1h 的热缓存。
    # 没有这一步，一小时后就再也答不上「这个用户学过什么、考了多少分」。
    upsert_session(
        session["session_id"],
        user_id=user.id,
        card_id=session.get("news_item_id"),
        topic=session["topic"],
        summary=session.get("summary"),
        core_concepts=session.get("core_concepts"),
        time_budget_minutes=session["time_budget_minutes"],
        preferred_depth=session["preferred_depth"],
        state=session.get("current_state"),
        started_at=session.get("start_time"),
    )

    log_activity(
        "start_session",
        user_id=user.id,
        target_type="session",
        target_id=session["session_id"],
        metadata={
            "card_id": session.get("news_item_id"),
            "topic": session["topic"],
            "time_budget_minutes": session["time_budget_minutes"],
        },
        request=request,
    )

    return SessionCreateResponse(
        session_id=session["session_id"],
        status="created",
        context=SessionContext(
            topic=session["topic"],
            time_budget_minutes=session["time_budget_minutes"],
            current_state="idle",
        ),
    )


@router.post("/push", response_model=PushResponse)
async def send_push_notification(
    request: Request, data: PushRequest, user: CurrentUser
) -> PushResponse:
    """发送推送通知（基于真实会话上下文）"""
    sm = get_state_machine()
    context = _load_session_context(data.session_id, str(user.id))

    result = await sm.push_notification(context)

    sync_state_from_context(context, state=result.get("state"))

    log_activity(
        "push_notification",
        user_id=user.id,
        target_type="session",
        target_id=data.session_id,
        request=request,
    )

    logger.info(f"💬 Push notification sent for {data.session_id}")

    return result


@router.post(
    "/chat",
    response_model=ChatResponse,
    dependencies=[Depends(rate_limit(LEARNING_CHAT_IP))],
)
async def chat_with_ai(request: Request, data: ChatRequest, user: CurrentUser) -> ChatResponse:
    """
    AI 聊天助手 - 基于当前学习内容的问答

    ## 请求体
    - session_id: 学习会话 ID（必填）
    - message: 用户问题
    - conversation_history: 历史对话（可选，最多 20 条）

    ## 语义契约（与本模块 docstring 对齐）
    会话上下文按「内存 → Redis → PostgreSQL」三级读取，**读不到即 SESSION_NOT_FOUND**。
    此前这里会退化成一段硬编码的「当前技术主题」占位上下文并照常作答 —— 接口对外
    宣称「基于当前学习内容」，实际却拿编造的上下文回答，调用方无从分辨。现在三级
    都读不到就明确 404，与 /quiz、/complete 等接口的语义一致。

    匿名不可用（`CurrentUser` 依赖）+ IP 限流：每次调用都会真实触发一次 LLM 推理，
    是明确的外部成本放大面。
    """
    from src.modules.agent.state_machine import llm, LLM_AVAILABLE

    session_id = data.session_id
    user_message = data.message.strip()
    history = [{"role": m.role, "content": m.content} for m in data.conversation_history]

    if not user_message:
        raise ILOException("MESSAGE_REQUIRED", "请输入你的问题。", status_code=400)

    # 会话上下文：内存优先，其次 Redis（重启后端 / 刷新页面不丢），
    # 最后回落 PostgreSQL 真相源
    sm = get_state_machine()
    session_ctx = sm.active_sessions.get(session_id)
    if session_ctx is None:
        mm = get_memory_manager()
        if mm is not None:
            try:
                session_ctx = mm.get_session_context(session_id)
            except Exception as e:
                logger.warning(f"[WARN] Failed to load session context from Redis: {e}")
                session_ctx = None

    if session_ctx is None:
        # 第三级：Redis 过期后仍能给出「在学什么」的准确上下文。
        # 刻意不排除已完成会话 —— 学完之后继续追问是合理用法。
        session_ctx = load_session(session_id)
        if session_ctx is not None:
            _revive_context(session_ctx)

    if not session_ctx:
        raise ILOException(
            "SESSION_NOT_FOUND",
            "学习会话不存在或已过期，请重新打开卡片发起讲解。",
            status_code=404,
        )

    # 权限边界：不允许借用别人的 session_id 提问
    owner = session_ctx.get("user_id")
    if owner and owner != str(user.id):
        raise ILOException("SESSION_FORBIDDEN", "该学习会话不属于当前账号。", status_code=403)

    news_context = {
        "topic": session_ctx.get("topic", ""),
        "summary": session_ctx.get("summary", ""),
        "core_concepts": session_ctx.get("core_concepts", []),
    }

    # 构建系统提示词
    system_prompt = f"""
你是一位耐心友好的技术导师，正在帮助用户理解一个技术概念。

【学习内容】
主题：{news_context['topic']}
简介：{news_context['summary']}
核心概念：{', '.join(news_context['core_concepts'])}

【回答要求】
1. 用简洁易懂的语言回答，避免过于学术化
2. 结合用户的背景知识，循序渐进
3. 适当举例子，帮助理解抽象概念
4. 如果用户问到代码实现，给出具体示例
5. 每次回答控制在 200-300 字以内
6. 使用 Markdown 格式，支持 **粗体**、`代码块`

【当前对话历史】
{chr(10).join([f"{msg['role']}: {msg['content']}" for msg in history[-10:]]) if history else "暂无历史"}
"""

    # 调用 LLM
    if LLM_AVAILABLE and llm is not None:
        try:
            response = llm.invoke(system_prompt)
            ai_response = (response.content or "").strip()
            if not ai_response:
                # 空内容也是失败：若原样返回，前端会渲染出一个空气泡，
                # 被当成「AI 回答了但没说话」而不是「这轮没答上来」
                raise ValueError("LLM 返回空内容")
            logger.info(f"✅ LLM generated response for session {session_id}")
        except Exception as llm_error:
            logger.error(f"❌ LLM generation failed: {llm_error}")
            ai_response = _generate_fallback_response(user_message, news_context)
    else:
        # 模型未配置：明确告知不可用，绝不编造内容（详见该函数的 docstring）
        ai_response = _generate_fallback_response(user_message, news_context)

    # 更新对话历史
    updated_history = history + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": ai_response}
    ]

    # 对话永久留存：Redis 那份 TTL 只有 1h，不落库这段历史就真的没了。
    # session_id 现在必定对应一个真实存在的学习会话（见上方 SESSION_NOT_FOUND），
    # 「临时会话」这条旁路已取消：它既不校验归属，也无法回溯到任何学习内容。
    append_messages(
        session_id,
        [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": ai_response},
        ],
    )

    log_activity(
        "ask_question",
        user_id=user.id,
        target_type="session",
        target_id=session_id,
        metadata={"message_chars": len(user_message), "known_context": bool(session_ctx)},
        request=request,
    )

    return {
        "session_id": session_id,
        "response": ai_response,
        "conversation_history": updated_history,
        "sources": news_context,
    }


@router.post("/response", response_model=ResponseProcessResponse)
async def process_user_response(
    request: Request, data: ResponseProcessRequest, user: CurrentUser
) -> ResponseProcessResponse:
    """
    处理用户响应（基于真实会话上下文）

    ## 请求体
    - session_id: 会话 ID
    - user_input: 用户输入
    """
    sm = get_state_machine()
    context = _load_session_context(data.session_id, str(user.id))

    result = await sm.process_user_response(
        context=context,
        user_input=data.user_input
    )

    # 这一步可能刚生成 quiz_questions，是整条链路里最需要落库的一次同步
    sync_state_from_context(context, state=result.get("state"))

    log_activity(
        "process_response",
        user_id=user.id,
        target_type="session",
        target_id=data.session_id,
        metadata={"action": result.get("action"), "state": result.get("state")},
        request=request,
    )

    logger.info(f"📖 Processing response for {data.session_id}")

    return result


@router.post("/quiz", response_model=QuizResponse)
async def submit_quiz_answer(
    request: Request, data: QuizRequest, user: CurrentUser
) -> QuizResponse:
    """
    提交测验答案（基于真实会话的测验题）

    ## 请求体
    - session_id: 会话 ID
    - user_answers: 用户答案数组
    """
    sm = get_state_machine()
    context = _load_session_context(data.session_id, str(user.id))

    if not context.get("quiz_questions"):
        raise ILOException(
            "QUIZ_NOT_READY",
            "该会话还没有生成测验题，请先开始讲解后再提交答案。",
            status_code=409,
        )

    result = await sm.submit_quiz(
        context=context,
        user_answers=data.user_answers
    )

    # 分数与 FSRS 复习计划落库：这两项是「学习效果」的唯一凭证，
    # 之前只活在 Redis 里，一小时后就查不到了
    sync_state_from_context(context, state=result.get("state"))

    log_activity(
        "submit_quiz",
        user_id=user.id,
        target_type="session",
        target_id=data.session_id,
        metadata={
            "score": result.get("score"),
            "grade": result.get("grade"),
            "answers": list(data.user_answers),
        },
        request=request,
    )

    logger.info(f"📊 Quiz submitted and evaluated for {data.session_id}")

    return result


@router.post("/complete", response_model=CompleteResponse)
async def complete_session(
    request: Request, data: CompleteRequest, user: CurrentUser
) -> CompleteResponse:
    """
    完成学习会话

    ## 请求体
    - session_id: 会话 ID
    """
    sm = get_state_machine()
    context = _load_session_context(data.session_id, str(user.id))

    # 从 Redis 恢复的上下文可能缺 start_time，兜底避免 KeyError
    context.setdefault("start_time", datetime.now(timezone.utc))

    result = await sm.complete_session(context)

    # 终态落库（状态 + 最终分数 + 复习计划 + 完成时间）
    complete_session_record(
        data.session_id,
        quiz_score=context.get("quiz_score"),
        fsrs_rating=context.get("fsrs_rating"),
        next_review_at=context.get("next_review_date"),
    )

    log_activity(
        "complete_session",
        user_id=user.id,
        target_type="session",
        target_id=data.session_id,
        metadata={
            "elapsed_minutes": result.get("elapsed_minutes"),
            "final_score": result.get("final_score"),
        },
        request=request,
    )

    # 会话结束后必须同时清掉持久化副本，否则 Redis 里那份会让已结束的会话“复活”：
    # 再次 /complete 会返回 200、/quiz 会走奇怪的降级分支。此处 best-effort，
    # Redis 不可用时不阻塞完成流程。
    mm = get_memory_manager()
    if mm is not None:
        try:
            mm.delete_session(data.session_id)
        except Exception as e:
            logger.warning(f"[WARN] Failed to purge session context: {e}")

    logger.info(f"✨ Session completed: {data.session_id}")

    return result


# 健康检查（刻意不加登录依赖：监控/探针需要匿名可用，且不暴露任何用户数据）
@router.get("/health", response_model=LearningHealthResponse)
async def health_check() -> LearningHealthResponse:
    """学习模块健康状态

    - `uptime` 是**进程真实运行时长**，不是写死的常量。写死 "24h" 唯一的作用是
      让人误以为探针在工作；
    - `state_machine_ready` 如实反映「状态机是否已经被拉起来过」。它是惰性初始化
      的，冷启动后到首次调用学习接口之前为 false 属正常，不代表故障。
    """
    return LearningHealthResponse(
        status="healthy",
        state_machine_ready=state_machine is not None,
        uptime=_format_uptime(datetime.now(timezone.utc) - _PROCESS_STARTED_AT),
    )
