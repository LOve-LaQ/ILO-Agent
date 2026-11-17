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
- `/chat` 会做一次块级检索并注入【参考资料】，检索失败只降级为「不注入」，
  不影响接口可用性
"""

from datetime import datetime, timedelta, timezone
import asyncio
import threading
import time
import uuid

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Request
from loguru import logger

from src.api.deps import CurrentUser
from src.core import circuit_breaker
from src.core.config import settings
from src.core.deadline import clamp_timeout
from src.core.errors import ERROR_RESPONSES, ILOException
from src.core.rate_limit import LEARNING_CHAT_IP, rate_limit
from src.core.resilience import log_downstream_failure
from src.services.feature_flags import require_feature
from src.services.llm_usage import tracked_invoke
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
from src.services.activity_service import log_activity_async
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
# 见 get_state_machine() 的说明：卸载到线程池后，并发首调需要锁来收敛构造。
_state_machine_lock = threading.Lock()


def get_state_machine():
    """获取或创建状态机实例（线程安全）

    延迟导入 src.modules.agent：它会连带拉起 langchain / transformers / torch
    （约 8s），而 /auth、/discover 等启动路径并不需要它。放到函数体内后，只有真正
    调用学习接口时才付出这次导入成本，应用启动不再为它买单。

    ⚠️ **首次调用是一次 8s 级的同步阻塞**（惰性 import + 构造状态机）。
    调用方必须走 `await _get_state_machine()`（内部 `asyncio.to_thread`），
    直接调这个函数会把事件循环按住 8 秒 —— 冷启动后第一个学习请求会连
    /health 一起拖住。

    加锁的原因：卸载到线程池后不再是「事件循环内天然串行」，并发首调会各自看到
    `state_machine is None` 并各自构造一次（每次都要付 8s 的 import 成本）。
    双重检查 + 锁把构造收敛成一次。
    """
    global state_machine
    if state_machine is None:
        with _state_machine_lock:
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
_memory_manager_lock = threading.Lock()
_MEMORY_MANAGER_RETRY_SECONDS = 30.0

# 进程启动时刻：健康检查上报**真实**运行时长（此前写死 "24h"，没有任何信息量）
_PROCESS_STARTED_AT = datetime.now(timezone.utc)


def get_memory_manager():
    """获取或创建 MemoryManager 实例；Redis/Qdrant 不可用时返回 None（不阻塞主流程）

    失败后按 `_MEMORY_MANAGER_RETRY_SECONDS` 退避重试，依赖恢复后自动自愈。
    """
    global memory_manager, _memory_manager_ready, _memory_manager_retry_at
    # 锁的范围覆盖「检查标记 + 构造 + 记录重试时刻」：卸载到线程池后并发首调会
    # 同时穿过 `_memory_manager_ready` 检查，各自构造一个 MemoryManager
    # （每个都连 Redis + Qdrant 并建 collection）。
    with _memory_manager_lock:
        if _memory_manager_ready:
            return memory_manager

        now = time.monotonic()
        if now < _memory_manager_retry_at:
            return None

        try:
            from src.modules.agent.memory_manager import MemoryManager
            # 走 settings 而不是 os.getenv：pydantic-settings 同时覆盖进程环境与
            # .env 文件，是 os.getenv 的超集（与 tech_knowledge 的取法保持一致，
            # 避免同一个 Redis 在两处用不同来源解析出不同地址）。
            memory_manager = MemoryManager({
                "redis_url": settings.redis_url,
                "qdrant_url": settings.qdrant_url,
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


async def _get_state_machine():
    """异步获取状态机单例：首次构造含 8s 级惰性 import，必须卸载到线程池。"""
    return await asyncio.to_thread(get_state_machine)


async def _get_memory_manager():
    """异步获取记忆管理器单例：首次构造会连 Redis + Qdrant。"""
    return await asyncio.to_thread(get_memory_manager)


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


async def _cache_session_context(context: dict) -> None:
    """把上下文写回 Redis（best-effort，缓存不可用不影响学习流程）"""
    mm = await _get_memory_manager()
    if mm is None:
        return
    try:
        await asyncio.to_thread(
            mm.save_session_context, context["session_id"], _redis_persistable(context)
        )
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


async def _load_session_context(session_id: str, user_id: str) -> dict:
    """加载会话上下文：内存优先（含 quiz_questions / start_time），其次 Redis，
    最后回落 PostgreSQL 真相源。

    读取不到时抛 SESSION_NOT_FOUND 业务错误，取代此前硬编码的占位上下文
    （topic="Rust 内存安全"），避免用假数据误导用户。

    权限边界：会话归属者与当前登录用户不一致时抛 FORBIDDEN，
    防止拿到别人的 session_id 就能读到其学习内容。

    是 `async def`：三级读取里后两级（Redis / PostgreSQL）都是同步 IO，
    必须卸载到线程池。内存那一级是纯字典查找，留在事件循环里。
    """
    if not session_id:
        raise ILOException("SESSION_ID_REQUIRED", "缺少 session_id。", status_code=400)

    sm = await _get_state_machine()
    ctx = sm.active_sessions.get(session_id)

    if ctx is None:
        mm = await _get_memory_manager()
        if mm is not None:
            try:
                ctx = await asyncio.to_thread(mm.get_session_context, session_id)
            except Exception as e:
                logger.warning(f"[WARN] Failed to load session context from Redis: {e}")
                ctx = None
            if ctx:
                ctx.setdefault("session_id", session_id)

    if ctx is None:
        # 第三级：PostgreSQL 真相源。Redis 重启 / TTL 到期不再等于「学习记录丢失」。
        ctx = await asyncio.to_thread(load_session, session_id)
        # 已完成的会话按「不存在」处理：与「完成后清 Redis」保持同一语义，
        # 否则会复活 —— 再次 /complete 返回 200、/quiz 走奇怪的降级分支。
        if ctx is not None and state_value(ctx.get("current_state")) == "completed":
            ctx = None
        if ctx is not None:
            _revive_context(ctx)
            # 回填热缓存，让后续请求重新走快速路径
            await _cache_session_context(ctx)
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


async def _find_news_item(news_item_id: str):
    """根据资讯 ID 查找资讯内容（三级降级：内置示例 → 知识库 → 抓取缓存）

    实现已收敛到 `src.services.card_service`：收藏列表、卡片溯源都要同一套语义，
    三处各自演化很容易出现「这里查得到、那里查不到」的不一致。

    是 `async def`：降级链的第二级会走知识库（Qdrant 网络查询），是同步 IO。
    """
    from src.services.card_service import find_card

    return await asyncio.to_thread(find_card, news_item_id)


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


# 拼进提示词的对话历史轮数上限。与 `ChatRequest.conversation_history` 的
# `MAX_CHAT_HISTORY_TURNS` 是两个不同的约束：那个管**请求体大小**（防注入超长文本），
# 这个管**提示词长度**（防历史挤掉回答空间）。两者都不设，一次超长历史就能
# 把参考资料和回答一起挤出上下文窗口。
_CHAT_HISTORY_TURNS = 10


def _build_chat_system_prompt(news_context: dict, reference_block: str = "") -> str:
    """拼 `/chat` 的系统提示词。

    **不含对话历史** —— 历史按真实的消息角色发送（见 `_build_chat_messages`）。
    把历史压成一段文本塞进系统提示词，等于让模型分不清「系统约束」和
    「用户说过的话」，而多轮对话本来就该用多轮消息表达。

    【为什么要抽成纯函数】LLM 在测试里是被强制降级的（conftest 的
    `_force_llm_fallback` 把 `llm` 置 None），所以「提示词到底长什么样」
    在接口层永远断言不到。而提示词正是 **RAG 注入的唯一生效点** ——
    它错了不会有任何报错，只会让回答变差。抽出来才能单测。
    """
    parts = [
        "你是一位耐心友好的技术导师，正在帮助用户理解一个技术概念。",
        "",
        "【学习内容】",
        f"主题：{news_context.get('topic', '')}",
        f"简介：{news_context.get('summary', '')}",
        f"核心概念：{', '.join(news_context.get('core_concepts') or [])}",
    ]

    if reference_block:
        parts += ["", reference_block]

    parts += [
        "",
        "【回答要求】",
        "1. 用简洁易懂的语言回答，避免过于学术化",
        "2. 结合用户的背景知识，循序渐进",
        "3. 适当举例子，帮助理解抽象概念",
        "4. 如果用户问到代码实现，给出具体示例",
        "5. 每次回答控制在 200-300 字以内",
        "6. 使用 Markdown 格式，支持 **粗体**、`代码块`",
    ]

    if reference_block:
        # 这条只在**真的注入了资料**时才加。没有资料却要求「资料里没有就直说」，
        # 会逼模型对着一份不存在的资料表态，比不加更糟。
        parts.append(
            "7. **优先依据【参考资料】回答**，并在句末标出来源编号（如 `[1]`）；"
            "资料里没提到的内容直接说「资料里没有」，不要凭印象补充"
        )

    return "\n".join(parts)


def _build_chat_messages(system_prompt: str, history: list, user_message: str) -> list:
    """组装成 LangChain 的消息列表。

    【为什么不再传单个字符串】此前调用是 `llm.invoke(system_prompt)`，传字符串时
    LangChain 会把它包成 **`HumanMessage`** —— 变量名叫 `system_prompt`，实际发出去
    却是用户消息。角色错了不报错，只是让系统约束和用户输入在模型眼里同级。

    【为什么要去掉历史尾部那条重复提问】前端在发起请求前会先把用户气泡 append
    进本地消息列表（`useLearningSession.ts`），所以 `conversation_history` 的最后
    一条通常**就是本轮提问**。不去重的话，模型会连着看到两遍同一个问题。
    接口对外的契约是「`message` 是本轮提问」，所以这里以 `message` 为准。
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    trimmed = list(history or [])
    if (
        trimmed
        and trimmed[-1].get("role") == "user"
        and (trimmed[-1].get("content") or "").strip() == user_message
    ):
        trimmed.pop()

    messages: list = [SystemMessage(content=system_prompt)]
    for msg in trimmed[-_CHAT_HISTORY_TURNS:]:
        content = msg.get("content") or ""
        if msg.get("role") == "assistant":
            messages.append(AIMessage(content=content))
        else:
            messages.append(HumanMessage(content=content))
    messages.append(HumanMessage(content=user_message))
    return messages



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
    sm = await _get_state_machine()

    # 根据资讯 ID 查找真实资讯内容；查不到时用请求体兜底字段构造
    news_item = await _find_news_item(data.news_item_id)
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
    await _cache_session_context(session)

    # 落库：PostgreSQL 才是真相源，Redis 只是 TTL 1h 的热缓存。
    # 没有这一步，一小时后就再也答不上「这个用户学过什么、考了多少分」。
    await asyncio.to_thread(
        upsert_session,
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

    await log_activity_async(
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
    """发送推送通知（基于真实会话上下文）

    【功能开关】`push_notification` 会生成讲解，属于 `learning_chat` 模块。
    模块关闭时整个「讲解 → 对话 → 追问」链路一起停，而不是只停其中几个接口：
    半开半关会让用户点完「开始讲解」才在下一步撞墙，比直接说明更难理解。
    """
    require_feature("learning_chat")

    sm = await _get_state_machine()
    context = await _load_session_context(data.session_id, str(user.id))

    result = await sm.push_notification(context)

    await asyncio.to_thread(sync_state_from_context, context, state=result.get("state"))

    await log_activity_async(
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

    ## 检索增强（RAG）
    在拼提示词之前会先做一次块级检索（`modules/rag/chat_context.py`），命中的块
    以【参考资料】的形式注入，并在响应 `sources.references` 里给出**块级**引用。
    三道闸门保证它「只会加分、不会减分」：请求预算 → `QDRANT_READ` 熔断 → 相似度阈值。
    **任何一环失败都只是不注入**，问答照常返回，接口不会因为向量库挂掉而 5xx。

    引用只到**块**、不到**句子**：句子级溯源要模型逐句输出编号再回校验，代价是
    多一轮解析且不可靠（模型会编编号）。给块级引用是诚实的。

    【功能开关】`learning_chat` 关闭时 503，而不是退回 `_generate_fallback_response`
    的固定话术：那段 fallback 的直接使用者是「LLM 不可用时的降级」，它给出的答案
    与用户的问题无关，作为「管理员主动关闭」的表现只会让人以为系统坏了。
    关闭就要明确说「已关闭」。
    """
    require_feature("learning_chat")

    from src.modules.agent.state_machine import llm, LLM_AVAILABLE

    session_id = data.session_id
    user_message = data.message.strip()
    history = [{"role": m.role, "content": m.content} for m in data.conversation_history]

    if not user_message:
        raise ILOException("MESSAGE_REQUIRED", "请输入你的问题。", status_code=400)

    # 会话上下文：内存优先，其次 Redis（重启后端 / 刷新页面不丢），
    # 最后回落 PostgreSQL 真相源
    sm = await _get_state_machine()
    session_ctx = sm.active_sessions.get(session_id)
    if session_ctx is None:
        mm = await _get_memory_manager()
        if mm is not None:
            try:
                session_ctx = await asyncio.to_thread(mm.get_session_context, session_id)
            except Exception as e:
                logger.warning(f"[WARN] Failed to load session context from Redis: {e}")
                session_ctx = None

    if session_ctx is None:
        # 第三级：Redis 过期后仍能给出「在学什么」的准确上下文。
        # 刻意不排除已完成会话 —— 学完之后继续追问是合理用法。
        session_ctx = await asyncio.to_thread(load_session, session_id)
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

    # ---- 检索增强：从语料库里找出与本轮提问相关的块，拼进提示词 ----
    #
    # 三道闸门都在 `build_chat_context` / `retriever` 内部（预算 → 熔断 → 阈值），
    # 这里只负责「拿不到就什么都不注入」：检索是锦上添花的一环，它挂掉
    # （Qdrant 连不上、预算耗尽、没有块过阈值）都不该让问答 500，更不该让用户
    # 看到报错。失败原因留在 `chat_ctx.reason` 里，通过日志与响应体的
    # `sources.references` 体现。
    #
    # 【为什么惰性 import】`chat_context` → `retriever` → `chunk_store` 会把
    # `qdrant_client` 一起拉起来。放在模块顶层，等于让 `/session`、`/quiz` 这些
    # 完全不碰向量的接口也付这份导入成本（与 `state_machine` 惰性导入同理）。
    from src.modules.rag.chat_context import ChatContext, build_chat_context

    chat_ctx = ChatContext()
    if settings.rag_chat_enabled:
        chat_ctx = await build_chat_context(
            user_message,
            deadline=getattr(request.state, "deadline", None),
            max_tokens=settings.rag_chat_context_tokens,
        )
        if chat_ctx.injected:
            logger.info(
                "event=rag_chat_injected refs={} query_chars={} reason={}",
                len(chat_ctx.references),
                len(chat_ctx.query),
                chat_ctx.reason,
            )
        elif chat_ctx.degraded:
            # 只有「系统原因没拿到」才告警；「确实没有相关资料」是正常业务，
            # 走「我没有相关资料」的话术即可（见 retriever 的原因码分类）。
            logger.warning("event=rag_chat_degraded reason={}", chat_ctx.reason)

    system_prompt = _build_chat_system_prompt(news_context, chat_ctx.block)

    # 请求级预算：把「这次请求还剩多少时间」夹到本次 LLM 调用上。
    # 剩余不足就直接降级 —— 让网关先超时、用户拿到 502，比拿到一段降级文案更糟。
    llm_timeout = clamp_timeout(
        settings.llm_chat_timeout, getattr(request.state, "deadline", None)
    )

    # 调用 LLM
    if llm_timeout <= 0:
        logger.warning(
            "event=downstream_fallback downstream=llm_chat reason=deadline_exhausted"
        )
        ai_response = _generate_fallback_response(user_message, news_context)
    elif LLM_AVAILABLE and llm is not None and circuit_breaker.is_open(
        circuit_breaker.LLM_CHAT
    ):
        # 熔断打开：下游已连续失败，本请求直接降级 —— 不再去撞一次，
        # 也不占用本请求的预算（撞上去只会把「快降级」拖成「等满超时再降级」）。
        logger.warning(
            "event=circuit_open downstream=llm_chat action=fallback scope=chat"
        )
        ai_response = _generate_fallback_response(user_message, news_context)
    elif LLM_AVAILABLE and llm is not None:
        _started = time.monotonic()
        try:
            # 同步 invoke 在 async 路由里会独占事件循环：一次几十秒的 LLM 调用期间，
            # 同 worker 上所有请求（含 /health）全部排队。卸载到线程池后立即归还。
            # timeout 按次传入：客户端默认值只作兜底，本次真正可用的时间由预算决定。
            #
            # 【为什么传 messages 而不是一个字符串】见 `_build_chat_messages`：
            # 传字符串时 LangChain 会包成 HumanMessage，系统提示词其实没走 system 角色；
            # 而且历史里通常已经含本轮提问，不组装消息列表就会重复发一遍。
            messages = _build_chat_messages(system_prompt, history, user_message)
            # tracked_invoke 与裸 `to_thread(llm.invoke, ...)` 完全同构（同样的线程池、
            # 同样的 timeout、异常同样上抛），额外按「学习对话」记一行用量账。
            response = await tracked_invoke(
                llm,
                messages,
                purpose="chat",
                user_id=str(user.id),
                timeout=llm_timeout,
            )
            ai_response = (response.content or "").strip()
            if not ai_response:
                # 空内容也是失败：若原样返回，前端会渲染出一个空气泡，
                # 被当成「AI 回答了但没说话」而不是「这轮没答上来」
                raise ValueError("LLM 返回空内容")
            circuit_breaker.record_success(circuit_breaker.LLM_CHAT)
            logger.info(f"✅ LLM generated response for session {session_id}")
        except Exception as llm_error:
            # 只有「真的调用了且失败」才计数。预算耗尽与熔断打开都走上面的分支 ——
            # 若把「我们主动跳过」也算成下游失败，熔断会被自己顶在 OPEN 永不恢复。
            circuit_breaker.record_failure(circuit_breaker.LLM_CHAT)
            # 超时与非超时会拆成两个事件：前者指向「预算配得合不合理」，
            # 后者指向「下游是不是挂了」，混在一起两类问题都看不清。
            log_downstream_failure(
                downstream="llm_chat",
                exc=llm_error,
                elapsed_ms=(time.monotonic() - _started) * 1000,
                timeout_s=llm_timeout,
                scope="chat",
            )
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
    await asyncio.to_thread(
        append_messages,
        session_id,
        [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": ai_response},
        ],
    )

    await log_activity_async(
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
        # `references` 为空表示本轮没有注入资料（总开关关闭 / 没有块过阈值 / 降级）。
        # 给前端一个可判定的信号，而不是让它猜「这次答案是不是有依据」。
        "sources": {
            **news_context,
            "references": [
                {
                    "ordinal": ref.ordinal,
                    "card_id": ref.card_id,
                    "title": ref.title,
                    "source_url": ref.source_url,
                    "score": ref.score,
                    "heading_path": list(ref.heading_path),
                }
                for ref in chat_ctx.references
            ],
        },
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

    【功能开关】与 /push、/chat 共用 `learning_chat`：这一步会生成下一段讲解
    与测验题，是整条链路里 LLM 调用最密集的一步。
    """
    require_feature("learning_chat")

    sm = await _get_state_machine()
    context = await _load_session_context(data.session_id, str(user.id))

    result = await sm.process_user_response(
        context=context,
        user_input=data.user_input,
        deadline=getattr(request.state, "deadline", None),
    )

    # 这一步可能刚生成 quiz_questions，是整条链路里最需要落库的一次同步
    await asyncio.to_thread(sync_state_from_context, context, state=result.get("state"))

    await log_activity_async(
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
    sm = await _get_state_machine()
    context = await _load_session_context(data.session_id, str(user.id))

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
    await asyncio.to_thread(sync_state_from_context, context, state=result.get("state"))

    await log_activity_async(
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
    sm = await _get_state_machine()
    context = await _load_session_context(data.session_id, str(user.id))

    # 从 Redis 恢复的上下文可能缺 start_time，兜底避免 KeyError
    context.setdefault("start_time", datetime.now(timezone.utc))

    result = await sm.complete_session(context)

    # 终态落库（状态 + 最终分数 + 复习计划 + 完成时间）
    await asyncio.to_thread(
        complete_session_record,
        data.session_id,
        quiz_score=context.get("quiz_score"),
        fsrs_rating=context.get("fsrs_rating"),
        next_review_at=context.get("next_review_date"),
    )

    await log_activity_async(
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
    mm = await _get_memory_manager()
    if mm is not None:
        try:
            await asyncio.to_thread(mm.delete_session, data.session_id)
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
