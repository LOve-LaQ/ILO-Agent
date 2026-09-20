# Learning Schemas - 学习会话接口契约
"""
学习会话全链路模型：
- SessionCreate / Chat / Quiz / Complete 为核心链路
- Push / ResponseProcess 保留（状态机推送与响应处理）
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ==================== 会话创建 ====================


class SessionCreateRequest(BaseModel):
    """POST /learning/session 请求体

    注意：不含 user_id。阶段 2 起用户身份一律取自 access token 的 `sub`，
    请求体里的任何 user_id 都不再被信任（权限边界的核心约束）。
    """

    news_item_id: str
    # 知识库查不到卡片时的兜底字段
    title: Optional[str] = None
    summary: Optional[str] = None
    core_concepts: List[str] = Field(default_factory=list)
    time_budget: int = 15
    preferred_depth: str = "medium"


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
    """单条对话消息"""

    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """POST /learning/chat 请求体"""

    session_id: Optional[str] = None
    message: str
    conversation_history: List[ChatMessage] = Field(default_factory=list)


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
