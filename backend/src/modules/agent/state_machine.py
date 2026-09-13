# LearningStateMachine - 支持 OpenAI/DeepSeek/其他兼容 API
"""
LangGraph 风格的状态机编排
职责:
- 管理学习流程的状态流转
- 调用 LLM（支持 OpenAI / DeepSeek / 其他）生成真实讲解
- 维护全局上下文
"""

from enum import Enum
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import asyncio
import os
import sys
from dotenv import load_dotenv
from loguru import logger

# 设置 loguru（解决 Windows 编码问题）
logger.remove()  # 移除默认处理器
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {name} - {message}",
    level="INFO"
)

# 强制 stdout/stderr 使用 UTF-8 编码
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# 加载环境变量
load_dotenv()

try:
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import StrOutputParser
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False
    logger.warning("[WARN] LangChain not available, using fallback mode")


# 初始化 LLM 客户端（支持多种 Provider）
def create_llm(provider="auto", api_key=None, base_url=None):
    """
    创建 LLM 客户端
    
    Args:
        provider: "auto" | "openai" | "deepseek" | "other"
        api_key: API Key
        base_url: API 端点 URL (可选)
    """
    if not LLM_AVAILABLE:
        return None
    
    # 自动检测或直接指定 provider
    if provider == "auto":
        # 优先检查 DeepSeek（通常更快、更便宜）
        ds_api_key = os.getenv("DEEPSEEK_API_KEY")
        if ds_api_key:
            logger.info("Using DeepSeek API")
            return ChatOpenAI(
                model="deepseek-chat",
                openai_api_key=ds_api_key,
                openai_api_base="https://api.deepseek.com/v1",
                temperature=0.7,
                max_tokens=4096
            )
        
        # 回退到 OpenAI
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            logger.info("Using OpenAI API")
            return ChatOpenAI(
                model="gpt-3.5-turbo",
                openai_api_key=openai_key,
                temperature=0.7,
                max_tokens=4096
            )
        
        logger.warning("No API key found, using fallback mode")
        return None
    
    elif provider == "deepseek":
        api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            logger.error("DeepSeek API key not provided")
            return None
        
        logger.info("Using DeepSeek API")
        return ChatOpenAI(
            model="deepseek-chat",
            openai_api_key=api_key,
            openai_api_base="https://api.deepseek.com/v1",
            temperature=0.7,
            max_tokens=4096
        )
    
    elif provider == "openai":
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.error("OpenAI API key not provided")
            return None
        
        logger.info("Using OpenAI API")
        return ChatOpenAI(
            model="gpt-3.5-turbo",
            openai_api_key=api_key,
            temperature=0.7,
            max_tokens=4096
        )
    
    else:
        # 自定义 provider
        logger.info(f"Using custom provider at {base_url or 'default'}")
        return ChatOpenAI(
            model="gpt-3.5-turbo",
            openai_api_key=api_key or os.getenv("OPENAI_API_KEY"),
            openai_api_base=base_url,
            temperature=0.7,
            max_tokens=4096
        )


def create_qwen_llm():
    """创建通义千问 LLM 客户端（用于批量中文摘要等离线整理任务）

    DashScope 的 OpenAI 兼容模式；无 ALIYUN_API_KEY 时返回 None（调用方回退 DeepSeek）。
    """
    if not LLM_AVAILABLE:
        return None
    qwen_key = os.getenv("ALIYUN_API_KEY")
    if not qwen_key:
        logger.warning("No ALIYUN_API_KEY found, summaries will fallback to DeepSeek")
        return None
    logger.info("Using Qwen (DashScope) for summaries")
    return ChatOpenAI(
        model="qwen-plus",
        openai_api_key=qwen_key,
        openai_api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        temperature=0.3,
        max_tokens=4096,
    )


# 创建主 LLM 实例（对话用 DeepSeek）
llm = create_llm()

# 摘要/整理类任务优先用通义千问，不可用时回退到 DeepSeek
summary_llm = create_qwen_llm() or llm

if llm is None:
    logger.warning("=" * 50)
    logger.warning("LLM MODE NOT AVAILABLE!")
    logger.warning("")
    logger.warning("To use real LLM, set one of these in .env:")
    logger.warning("  - DEEPSEEK_API_KEY (recommended - fast & cheap)")
    logger.warning("  - OPENAI_API_KEY")
    logger.warning("")
    logger.warning("LLM not configured; running in fallback mode.")
    logger.warning("=" * 50)


class LearningState(Enum):
    """学习状态枚举"""
    IDLE = "idle"                      # 空闲状态
    PUSHED = "pushed"                  # 已推送通知
    LEARNING = "learning"              # 讲解中
    QUIZ = "quiz"                      # 测验中
    FSRS_UPDATE = "fsrs_update"        # 记忆更新中
    COMPLETED = "completed"            # 已完成


class ExplanationEngine:
    """讲解引擎 - 使用真实 LLM"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.use_fallback = llm is None or not LLM_AVAILABLE
        
    async def generate(self, topic: str, context: dict) -> str:
        """生成讲解内容（使用 OpenAI LLM）"""
        if self.use_fallback:
            # 降级模式
            return self._generate_fallback_explanation(topic, context)
        
        try:
            # 真实 LLM 模式
            prompt = f"""你是一个人工智能技术导师，擅长用简洁易懂的方式讲解技术概念。

请为以下技术主题编写一份 15 分钟内可以读完的讲解文档：

主题：{topic}
摘要：{context.get('summary', '')}
核心概念：{', '.join(context.get('core_concepts', []))}
学习深度：{context.get('preferred_depth', 'medium')}（surface=浅层了解，medium=中等深度，deep=深入原理）

要求：
1. 结构清晰，包含背景知识、核心要点、代码示例、实践建议
2. 语言简洁生动，适合技术人员快速理解
3. 提供实际可操作的代码示例
4. 总字数控制在 800 字以内

使用 Markdown 格式输出："""
            
            result = llm.invoke(prompt)
            return result.content
            
        except Exception as e:
            print(f"❌ LLM generation failed: {e}")
            return self._generate_fallback_explanation(topic, context)
    
    def strip_formatting(self, text: str) -> str:
        """移除 Markdown 格式用于 LLM 调用"""
        import re
        text = re.sub(r'#+\s*', '', text)
        text = re.sub(r'\*\*|\`{3}', '', text)
        return text.strip()
    
    def _generate_fallback_explanation(self, topic: str, context: dict) -> str:
        """降级方案：生成内置示例讲解内容（当 LLM 不可用时）"""
        depth_indicators = {
            "surface": ["基本概念:", "快速了解:"],
            "medium": ["核心原理:", "深入分析:"],
            "deep": ["底层机制:", "源码级解读:"]
        }
        
        indicator = depth_indicators.get(context.get("preferred_depth", "medium"), ["核心原理:"])[0]
        
        return f"""
{indicator} {topic}

## 📖 背景知识

这是一个在 {context.get('time_budget_minutes', 15)} 分钟内可以掌握的技术概念...

## 🔑 核心要点

1. **要点一**: 关键概念说明
2. **要点二**: 重要特性介绍  
3. **要点三**: 实际应用价值

## 💡 代码示例

```python
# {topic} 的基本用法示例
def example():
    pass
```

## 🚀 实践建议

根据您的学习目标，建议您：
- {'深入研究其底层实现' if context.get('preferred_depth') == 'deep' else '动手实践基础应用'}
"""


class QuizFactory:
    """测验工厂（简化版）"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.num_questions = config.get("num_questions", 3)
    
    def generate(self, topic: str, core_concepts: list) -> list:
        """生成降级测验题目"""
        fallback_quiz = [
            {
                "question": f"{topic} 的核心优势是什么？",
                "options": [
                    "性能提升 10 倍",
                    "内存安全性增强",
                    "开发效率提高",
                    "全部都有"
                ],
                "correct_answer": 3,
                "explanation": f"{topic} 在设计时综合考虑了性能、安全性和效率..."
            },
            {
                "question": "以下哪个是 {topic} 的关键概念？",
                "options": core_concepts[:4] if len(core_concepts) >= 4 else core_concepts + ["其他"],
                "correct_answer": 0,
                "explanation": f"核心概念之一是：{core_concepts[0]}"
            }
        ]
        
        return fallback_quiz[:self.num_questions]
    
    def evaluate(self, user_answers: list[int], correct_answers: list[int]) -> tuple[float, list[str]]:
        """评估测验结果"""
        if not correct_answers:
            return 0.0, []
        
        correct_count = sum(1 for u, c in zip(user_answers, correct_answers) if u == c)
        score = correct_count / len(correct_answers)
        
        explanations = [
            f"第{i+1}题: {'✅正确!' if u == c else f'❌错误，答案是选项{c+1}'}"
            for i, (u, c) in enumerate(zip(user_answers, correct_answers))
        ]
        
        return score, explanations


class FSRSCalculator:
    """FSRS 算法计算器（简化版）"""
    
    @staticmethod
    def calculate_next_interval(stability: float, rating: int, desired_retention: float = 0.9) -> int:
        """计算下次复习间隔（天数）"""
        rating_weights = {1: 0.0, 2: 0.5, 3: 1.0, 4: 1.5}
        weight = rating_weights.get(rating, 1.0)
        
        new_stability = stability * (1 + 0.1 * weight)
        
        if desired_retention < 1.0 and stability > 0:
            interval = int(-stability * (1 - desired_retention) * 10)
        else:
            interval = max(1, int(stability * 0.5))
        
        return max(1, interval)
    
    @staticmethod
    def predict_retrievability(stability: float, days_elapsed: float) -> float:
        """预测回忆概率"""
        import math
        lambda_param = 0.6
        retention = math.exp(-lambda_param * days_elapsed / stability) if stability > 0 else 0
        return max(0.0, min(1.0, retention))


class LearningStateMachine:
    """主状态机"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.explanation_engine = ExplanationEngine(config)
        self.quiz_factory = QuizFactory(config)
        self.fsrs_calculator = FSRSCalculator()
        
        # 运行时状态存储
        self.active_sessions: Dict[str, dict] = {}
    
    async def create_session(self, session_id: str, user_id: str, news_item: dict, 
                            time_budget: int = 15, preferred_depth: str = "medium") -> dict:
        """创建新的学习会话"""
        context = {
            "session_id": session_id,
            "user_id": user_id,
            "news_item_id": news_item.get("id"),
            "topic": news_item.get("title", ""),
            "summary": news_item.get("summary", ""),
            "core_concepts": news_item.get("core_concepts", []),
            "time_budget_minutes": time_budget,
            "preferred_depth": preferred_depth,
            "start_time": datetime.now(timezone.utc),
            "current_state": LearningState.IDLE
        }
        
        self.active_sessions[session_id] = context
        return context
    
    async def push_notification(self, context: dict) -> dict:
        """发送推送通知"""
        context["current_state"] = LearningState.PUSHED
        
        message = f"早上有个关于 {context['topic']} 的内容要了解一下吗？"
        
        return {
            "state": LearningState.PUSHED,
            "message": message,
            "actions": ["展开讲讲", "太累了明天再说", "加入复习队列"]
        }
    
    async def process_user_response(self, context: dict, user_input: str) -> dict:
        """处理用户响应"""
        intent = self._detect_intent(user_input)
        
        if intent == "skip":
            context["current_state"] = LearningState.IDLE
            return {"state": LearningState.IDLE, "action": "skip", "message": "好的，下次再聊~"}
        
        elif intent == "bookmark":
            context["current_state"] = LearningState.IDLE
            return {"state": LearningState.IDLE, "action": "bookmark", "message": "已添加到收藏夹📚"}
        
        else:
            return await self.start_learning(context)
    
    async def start_learning(self, context: dict) -> dict:
        """开始深入学习（异步调用 LLM）"""
        context["current_state"] = LearningState.LEARNING
        
        # 异步生成讲解内容
        explanation = await asyncio.to_thread(
            self.explanation_engine.generate,
            context["topic"],
            context
        )
        
        quiz_questions = self.quiz_factory.generate(
            context["topic"],
            context["core_concepts"]
        )
        
        context["quiz_questions"] = quiz_questions
        
        return {
            "state": LearningState.QUIZ,
            "explanation": explanation,
            "quiz": {
                "questions": quiz_questions,
                "total": len(quiz_questions)
            }
        }
    
    async def submit_quiz(self, context: dict, user_answers: list[int]) -> dict:
        """提交测验答案"""
        correct_answers = [q["correct_answer"] for q in context["quiz_questions"]]
        score, explanations = self.quiz_factory.evaluate(user_answers, correct_answers)
        
        context["quiz_score"] = score
        
        # 映射到 FSRS rating
        if score >= 0.8:
            fsrs_rating = 4  # Easy
        elif score >= 0.5:
            fsrs_rating = 3  # Good
        elif score >= 0.2:
            fsrs_rating = 2  # Hard
        else:
            fsrs_rating = 1  # Again
        
        context["fsrs_rating"] = fsrs_rating
        
        # 计算下次复习间隔
        review_data = {
            "new_interval": self.fsrs_calculator.calculate_next_interval(
                context.get("fsrs_stability", 1.0),
                fsrs_rating
            ),
            "next_review_date": datetime.now(timezone.utc).isoformat()
        }
        
        context["fsrs_stability"] = review_data["new_interval"]
        context["next_review_date"] = review_data["next_review_date"]
        
        return {
            "state": LearningState.FSRS_UPDATE,
            "score": score,
            "grade": "优秀" if score >= 0.8 else "良好" if score >= 0.5 else "加油",
            "explanations": explanations,
            "review_data": review_data
        }
    
    async def complete_session(self, context: dict) -> dict:
        """完成学习会话"""
        end_time = datetime.now(timezone.utc)
        elapsed = (end_time - context["start_time"]).total_seconds() / 60
        
        result = {
            "session_id": context["session_id"],
            "status": "completed",
            "topic": context["topic"],
            "final_score": context.get("quiz_score", 0),
            "elapsed_minutes": round(elapsed, 2),
            "fsrs_update": {
                "rating": context.get("fsrs_rating"),
                "next_review_date": context.get("next_review_date")
            }
        }
        
        del self.active_sessions[context["session_id"]]
        
        return result
    
    def _detect_intent(self, user_input: str) -> str:
        """检测用户意图（简化版）"""
        skip_keywords = ["累", "明天", "稍后", "跳过"]
        bookmark_keywords = ["收藏", "保存", "标记", "书签"]
        learn_keywords = ["讲", "学", "解释", "开始"]
        
        for word in skip_keywords:
            if word in user_input:
                return "skip"
        
        for word in bookmark_keywords:
            if word in user_input:
                return "bookmark"
        
        for word in learn_keywords:
            if word in user_input:
                return "learn"
        
        return "learn"  # 默认选择学习


if __name__ == "__main__":
    # 测试运行
    test_config = {"max_tokens": 4096, "num_questions": 3}
    
    async def test_flow():
        state_machine = LearningStateMachine(test_config)
        
        # Step 1: 创建会话
        session = await state_machine.create_session(
            session_id="test-session-123",
            user_id="test-user",
            news_item={
                "id": "news-001",
                "title": "Python Async/Await 详解",
                "summary": "深入了解 Python 异步编程",
                "core_concepts": ["asyncio", "coroutine", "event_loop"]
            },
            time_budget=15,
            preferred_depth="medium"
        )
        
        print(f"✅ 创建会话：{session['session_id']}")
        print(f"📝 主题：{session['topic']}")
        
        # Step 2: 推送
        push_result = await state_machine.push_notification(session)
        print(f"\n💬 推送消息：{push_result['message'][:30]}...")
        
        # Step 3: 用户响应
        response_result = await state_machine.process_user_response(session, "展开讲讲")
        print(f"\n📖 进入学习状态：{response_result['state']}")
        
        # Step 4: 测验
        quiz_result = await state_machine.submit_quiz(session, user_answers=[3, 0])
        print(f"\n📊 测验结果：{quiz_result['grade']} (得分：{quiz_result['score']:.2f})")
        
        # Step 5: 完成
        completion = await state_machine.complete_session(session)
        print(f"\n✨ 会话完成！总耗时：{completion['elapsed_minutes']:.1f}分钟")
        
        return True
    
    try:
        asyncio.run(test_flow())
        print("\n🎉 状态机测试通过!")
    except Exception as e:
        print(f"\n❌ 测试失败：{e}")
        import traceback
        traceback.print_exc()
