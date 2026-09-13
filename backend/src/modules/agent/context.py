# LearningContext - 学习会话上下文
"""
学习上下文数据结构设计

包含单次学习互动的所有关键信息:
- 基本标识 (session_id, user_id, news_item_id)
- 资讯内容 (topic, summary, core_concepts)
- 时间感知 (time_budget_minutes, elapsed_seconds)
- 用户偏好 (preferred_depth, quiz_preference)
- 记忆数据 (fsrs_rating, fsrs_stability, next_review_date)
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List


@dataclass
class LearningContext:
    """学习上下文数据类"""
    
    # ==================== 基本标识 ====================
    session_id: str                      # 会话唯一标识
    user_id: str                        # 用户 ID
    
    # 资讯内容
    news_item_id: str                   # 资讯条目 ID
    topic: str                          # 技术主题
    summary: str                        # 摘要
    core_concepts: List[str]            # 核心概念列表
    
    # ==================== 时间感知 ====================
    time_budget_minutes: int = 15       # 可用时间 (5/15/30 min 可选)
    start_time: Optional[datetime] = None
    elapsed_seconds: int = 0            # 已耗时秒数
    
    # ==================== 用户偏好 ====================
    preferred_depth: str = "medium"     # deep/medium/surface
    quiz_preference: str = "interactive"# interactive/text-based
    
    # ==================== 记忆数据 (FSRS) ====================
    initial_difficulty: float = 0.5     # 初始难度 (0-1)
    fsrs_stability: float = 0.0         # 当前记忆稳定性
    fsrs_durability: float = 0.0        # 记忆持久度
    
    fsrs_rating: Optional[int] = None   # 1-5 (Again/Hard/Good/Easy/VeryEasy)
    
    # 测验结果
    quiz_score: float = 0.0             # 得分 (0-1)
    quiz_questions: List[dict] = field(default_factory=list)
    
    # 下次复习时间
    next_review_date: Optional[datetime] = None
    
    # ==================== 元数据 ====================
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> dict:
        """转换为字典（序列化用）"""
        data = {
            k: v.isoformat() if isinstance(v, datetime) else v
            for k, v in self.__dict__.items()
        }
        return data
    
    @classmethod
    def from_dict(cls, data: dict) -> "LearningContext":
        """从字典重建实例（反序列化用）"""
        # 恢复 datetime 字段
        for key in ['start_time', 'next_review_date']:
            if key in data and data[key]:
                data[key] = datetime.fromisoformat(data[key])
        
        return cls(**data)
    
    def update_timestamp(self):
        """更新最后修改时间"""
        self.updated_at = datetime.now(timezone.utc)
    
    def get_elapsed_minutes(self) -> float:
        """计算已消耗的时间（分钟）"""
        if not self.start_time:
            return 0.0
        
        # 确保 start_time 有时区信息
        if self.start_time.tzinfo is None:
            current_time = datetime.now(timezone.utc)
        else:
            current_time = datetime.now(self.start_time.tzinfo)
        
        elapsed = (current_time - self.start_time).total_seconds()
        return round(elapsed / 60, 2)
    
    def is_time_exceeded(self) -> bool:
        """判断是否超时"""
        elapsed = self.get_elapsed_minutes()
        return elapsed > self.time_budget_minutes


if __name__ == "__main__":
    # 测试示例
    ctx = LearningContext(
        session_id="test-session-123",
        user_id="user-456",
        news_item_id="news-789",
        topic="Python Async/Await 详解",
        summary="深入了解 Python 异步编程模型",
        core_concepts=["asyncio", "coroutine", "event_loop"],
        time_budget_minutes=15,
        preferred_depth="deep"
    )
    
    print(f"✅ 创建上下文：{ctx.session_id}")
    print(f"📝 主题：{ctx.topic}")
    print(f"⏱️  时间预算：{ctx.time_budget_minutes}分钟")
    print(f"🧠 核心概念：{', '.join(ctx.core_concepts)}")
