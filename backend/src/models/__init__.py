# Models - 导出
"""ORM 模型统一出口：Alembic 的 target_metadata 依赖此处把模型全部注册到 Base.metadata。"""

from src.models.base import Base, utcnow
from src.models.collection import CollectionBatch, CollectionRecord
from src.models.consent import ConsentRecord
from src.models.engagement import UserActivity, UserBookmark
from src.models.interest import UserInterestProfile
from src.models.learning import ChatMessage, LearningSession
from src.models.notification import EmailOutbox
from src.models.password_reset import PasswordResetToken
from src.models.session import UserSession
from src.models.user import User

__all__ = [
    "Base",
    "utcnow",
    "User",
    # 内容溯源（阶段 3）
    "CollectionBatch",
    "CollectionRecord",
    # 行为溯源（阶段 3）
    "LearningSession",
    "ChatMessage",
    "UserBookmark",
    "UserActivity",
    # 个性化推送
    "UserInterestProfile",
    # 会话与账号生命周期（阶段 5）
    "UserSession",
    "PasswordResetToken",
    "EmailOutbox",
    "ConsentRecord",
]
