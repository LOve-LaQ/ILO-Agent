# Learning Schemas - 学习会话接口契约
"""
学习会话全链路模型：
- SessionCreate / Chat / Quiz / Complete 为核心链路
- Push / ResponseProcess 保留（状态机推送与响应处理）
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# ==================== 入参边界（阶段 5 边界治理） ====================
# 【为什么必须有】这些字段此前全是裸 `str` / `int` / `List`：
# - `message` 与 `conversation_history` 会**原样拼进系统提示词**，不设上限就等于
#   把「提示词长度 / 上下文成本 / 单次调用耗时」的控制权交给调用方；
# - `time_budget` 直接参与会话时长计算，天文数字会污染进度与统计；
# - `session_id` 会被拼进 Redis key 与 SQL 条件，长度失控是无谓的攻击面扩展。
# 上限取「远超正常使用、又足以挡住滥用」的量级：正常一轮提问只有几十到几百字。
MAX_CHAT_CONTENT_CHARS = 2000
MAX_CHAT_HISTORY_TURNS = 20
MAX_SESSION_ID_CHARS = 64
MAX_TOPIC_TEXT_CHARS = 2000
MAX_CORE_CONCEPTS = 20
MAX_TIME_BUDGET_MINUTES = 480  # 单次学习 8 小时，超过必然是异常输入


# ==================== 会话创建 ====================


class SessionCreateRequest(BaseModel):
    """POST /learning/session 请求体

    注意：不含 user_id。阶段 2 起用户身份一律取自 access token 的 `sub`，
    请求体里的任何 user_id 都不再被信任（权限边界的核心约束）。
    """

    news_item_id: str = Field(..., min_length=1, max_length=MAX_SESSION_ID_CHARS)
    # 知识库查不到卡片时的兜底字段
    title: Optional[str] = Field(None, max_length=MAX_TOPIC_TEXT_CHARS)
    summary: Optional[str] = Field(None, max_length=MAX_TOPIC_TEXT_CHARS)
    core_concepts: List[str] = Field(
        default_factory=list,
        max_length=MAX_CORE_CONCEPTS,
        description=f"核心概念（最多 {MAX_CORE_CONCEPTS} 个）",
    )
    time_budget: int = Field(
        15,
        ge=1,
        le=MAX_TIME_BUDGET_MINUTES,
        description=f"本次学习预算（分钟，1-{MAX_TIME_BUDGET_MINUTES}）",
    )
    # 取值必须与 state_machine 的 depth_indicators（surface/medium/deep）对齐，
    # 否则会静默落到默认分支，「用户选的深度」与「实际讲解深度」不一致
    preferred_depth: Literal["surface", "medium", "deep"] = "medium"


class SessionContext(BaseModel):
    """会话初始上下文"""

    topic: str
    time_budget_minutes: int
    current_state: str


class SessionCreateResponse(BaseModel):
    """POST /learning/session 响应"""

    session_id: str
    status: str
    context: SessionContext


# ==================== 对话 ====================


class ChatMessage(BaseModel):
    """单条对话消息（请求历史与响应回放共用）

    只限上限不加下限：这个模型同时用于**响应**里的 `conversation_history`，
    若给 content 设 `min_length=1`，模型偶发返回空串时会把响应序列化变成 500 ——
    输入严格、输出宽松，边界要加在正确的一侧。
    """

    role: Literal["user", "assistant"]
    content: str = Field(
        ...,
        max_length=MAX_CHAT_CONTENT_CHARS,
        description=f"单条消息文本（上限 {MAX_CHAT_CONTENT_CHARS} 字符）",
    )


class ChatRequest(BaseModel):
    """POST /learning/chat 请求体

    `session_id` 必填且必须能被三级链路（内存 → Redis → PostgreSQL）读到：
    这个接口的语义是「基于当前学习内容问答」，没有会话就没有「当前学习内容」，
    此时应当明确失败，而不是拿一段编造的通用上下文糊弄过去。
    """

    session_id: str = Field(
        ...,
        min_length=1,
        max_length=MAX_SESSION_ID_CHARS,
        description="学习会话 ID（必须已存在）",
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=MAX_CHAT_CONTENT_CHARS,
        description=f"用户提问（上限 {MAX_CHAT_CONTENT_CHARS} 字符）",
    )
    conversation_history: List[ChatMessage] = Field(
        default_factory=list,
        max_length=MAX_CHAT_HISTORY_TURNS,
        description=(
            f"历史对话（最多 {MAX_CHAT_HISTORY_TURNS} 条）。"
            "会被拼进系统提示词，故必须封顶"
        ),
    )


class ChatSources(BaseModel):
    """回答所依据的会话上下文"""

    topic: str = ""
    summary: str = ""
    core_concepts: List[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """POST /learning/chat 响应"""

    session_id: str
    response: str
    conversation_history: List[ChatMessage]
    sources: ChatSources


# ==================== 推送 / 响应处理 ====================


class PushRequest(BaseModel):
    """POST /learning/push 请求体"""

    session_id: str


class PushResponse(BaseModel):
    """POST /learning/push 响应"""

    state: str
    message: str
    actions: List[str]


class ResponseProcessRequest(BaseModel):
    """POST /learning/response 请求体"""

    session_id: str
    user_input: str = ""


class QuizQuestion(BaseModel):
    """测验题目"""

    question: str
    options: List[str]
    correct_answer: int
    explanation: Optional[str] = None


class QuizPayload(BaseModel):
    """测验题组"""

    questions: List[QuizQuestion]
    total: int


class ResponseProcessResponse(BaseModel):
    """POST /learning/response 响应（可能是跳转/收藏，也可能是进入讲解）"""

    state: str
    action: Optional[str] = None
    message: Optional[str] = None
    explanation: Optional[str] = None
    quiz: Optional[QuizPayload] = None


# ==================== 测验 ====================


class QuizRequest(BaseModel):
    """POST /learning/quiz 请求体"""

    session_id: str
    user_answers: List[int] = Field(default_factory=list)


class ReviewData(BaseModel):
    """FSRS 复习计划"""

    new_interval: int
    next_review_date: str


class QuizResponse(BaseModel):
    """POST /learning/quiz 响应"""

    state: str
    score: float
    grade: str
    explanations: List[str]
    review_data: ReviewData


# ==================== 完成 ====================


class CompleteRequest(BaseModel):
    """POST /learning/complete 请求体"""

    session_id: str


class FsrsUpdate(BaseModel):
    """FSRS 更新结果"""

    rating: Optional[int] = None
    next_review_date: Optional[str] = None


class CompleteResponse(BaseModel):
    """POST /learning/complete 响应"""

    session_id: str
    status: str
    topic: str
    final_score: float
    elapsed_minutes: float
    fsrs_update: FsrsUpdate


# ==================== 健康检查 ====================


class LearningHealthResponse(BaseModel):
    """GET /learning/health 响应"""

    status: str
    state_machine_ready: bool
    uptime: str
