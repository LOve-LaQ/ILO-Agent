# Learning Routes - Learning Session Management API
"""
学习会话 API
- POST /api/v1/learning/session: 创建新会话
- POST /api/v1/learning/push: 发送推送通知
- POST /api/v1/learning/response: 处理用户响应
- POST /api/v1/learning/quiz: 提交测验答案
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any
import os
import uuid
from datetime import datetime, timezone
from loguru import logger

from src.modules.agent import LearningStateMachine
from src.api.routes.discover import MOCK_NEWS

router = APIRouter(prefix="/learning", tags=["Learning"])


# 初始化状态机（单例）
state_machine = None


def get_state_machine():
    """获取或创建状态机实例"""
    global state_machine
    if state_machine is None:
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


def get_memory_manager():
    """获取或创建 MemoryManager 实例；Redis/Qdrant 不可用时返回 None（不阻塞主流程）"""
    global memory_manager, _memory_manager_ready
    if _memory_manager_ready:
        return memory_manager
    _memory_manager_ready = True
    try:
        from src.modules.agent.memory_manager import MemoryManager
        memory_manager = MemoryManager({
            "redis_url": os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            "qdrant_url": os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
        })
        logger.info("✅ MemoryManager initialized (Redis + Qdrant)")
    except Exception as e:
        logger.warning(f"[WARN] MemoryManager unavailable, falling back to in-memory: {e}")
        memory_manager = None
    return memory_manager


def _find_news_item(news_item_id: str):
    """根据资讯 ID 查找资讯内容（先查 Mock，再查知识库，最后查 Redis 抓取缓存）"""
    for item in MOCK_NEWS:
        if item["id"] == news_item_id:
            return item
    try:
        from src.modules.discovery.tech_knowledge import get_knowledge_base
        kb = get_knowledge_base()
        found = kb.get_by_id(news_item_id)
        if found:
            return found
    except Exception:
        pass
    try:
        from src.api.routes.discover import get_feed_cache
        pool = get_feed_cache()
        if pool:
            for item in pool:
                if item.get("id") == news_item_id:
                    return item
    except Exception:
        pass
    return None


def _generate_mock_response(question: str, context: dict) -> str:
    """生成 Mock 回答（当 LLM 不可用时）"""
    if "什么是" in question or "定义" in question:
        return f"**{context['topic']}** 是一个专注于 {', '.join(context['core_concepts'])} 的技术工具。\n\n它的核心优势在于：**性能提升 10 倍**、更好的类型提示支持和全新的 API 设计。非常适合 Python 数据处理场景。"
    elif "为什么" in question:
        return f"选择 **{context['topic']}** 的原因包括：\n\n1. **性能优化**: 解析速度比传统方式快 10 倍\n2. **类型安全**: 内置强大的类型提示支持\n3. **易于维护**: 清晰的 API 设计让代码更易读\n\n这些都是因为它的设计团队考虑了现代 Python 开发的最佳实践。"
    elif "如何" in question or "怎么" in question:
        code_example = "from pydantic import BaseModel\n\nclass MyModel(BaseModel):\n    name: str\n    age: int\n\n# 自动验证数据类型\nobj = MyModel(name=\'Alice\', age=30)"
        return f"使用 **{context['topic']}** 非常简单：\n\n```python\n{code_example}\n```\n\n你可以自定义验证器、配置模型行为，并享受无缝的序列化体验。"
    else:
        return f"这是个很好的问题！关于 **{context['topic']}**，它主要关注 {', '.join(context['core_concepts'])}，通过提供现代化的 API 和卓越的性能，成为开发者首选的数据验证工具。"



@router.post("/session")
async def create_learning_session(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    创建新的学习会话
    
    ## 请求体
    ```json
    {
      "user_id": "demo-user",
      "news_item_id": "news-001",
      "time_budget": 15,
      "preferred_depth": "medium"
    }
    ```
    
    ## 返回
    - session_id: 唯一会话标识
    - context: 初始上下文信息
    """
    
    try:
        sm = get_state_machine()
        
        # 验证必需参数
        required = ["user_id", "news_item_id"]
        for field in required:
            if field not in data:
                raise HTTPException(status_code=400, detail=f"Missing required field: {field}")
        
        # 根据资讯 ID 查找真实资讯内容
        news_item = _find_news_item(data["news_item_id"])
        if news_item is None:
            news_item = {
                "id": data["news_item_id"],
                "title": data.get("title", "未知技术主题"),
                "summary": data.get("summary", ""),
                "core_concepts": data.get("core_concepts", []),
            }

        # 创建会话
        session = await sm.create_session(
            session_id=f"sess_{uuid.uuid4().hex[:8]}",
            user_id=data["user_id"],
            news_item=news_item,
            time_budget=data.get("time_budget", 15),
            preferred_depth=data.get("preferred_depth", "medium")
        )
        
        logger.info(f"✅ Session created: {session['session_id']}")

        # 持久化会话上下文到 Redis（重启后端 / 刷新页面不丢）
        mm = get_memory_manager()
        if mm is not None:
            try:
                persistable = {
                    "session_id": session["session_id"],
                    "user_id": session["user_id"],
                    "news_item_id": session.get("news_item_id"),
                    "topic": session["topic"],
                    "summary": session["summary"],
                    "core_concepts": session["core_concepts"],
                    "time_budget_minutes": session["time_budget_minutes"],
                    "preferred_depth": session["preferred_depth"],
                }
                mm.save_session_context(session["session_id"], persistable)
                logger.info(f"💾 Session context persisted to Redis: {session['session_id']}")
            except Exception as e:
                logger.warning(f"[WARN] Failed to persist session context: {e}")

        return {
            "session_id": session["session_id"],
            "status": "created",
            "context": {
                "topic": session["topic"],
                "time_budget_minutes": session["time_budget_minutes"],
                "current_state": "idle"
            }
        }
        
    except Exception as e:
        logger.error(f"❌ Failed to create session: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/push")
async def send_push_notification(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    发送推送通知
    
    ## 请求体
    ```json
    {"session_id": "sess_abc123"}
    ```
    """
    
    try:
        sm = get_state_machine()
        session_id = data.get("session_id")
        
        # TODO: 从 Redis 获取 session 上下文
        context = {
            "session_id": session_id,
            "topic": "Rust 内存安全",
            "time_budget_minutes": 15
        }
        
        result = await sm.push_notification(context)
        
        logger.info(f"💬 Push notification sent for {session_id}")
        
        return result
        
    except Exception as e:
        logger.error(f"❌ Push failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat")
async def chat_with_ai(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    AI 聊天助手 - 基于当前学习内容的问答
    
    ## 请求体
    ```json
    {
      "session_id": "sess_abc123",
      "message": "什么是 Pydantic？",        # 用户问题
      "conversation_history": [...]           # 历史对话（可选）
    }
    ```
    """
    
    try:
        from src.modules.agent.state_machine import llm, LLM_AVAILABLE
        import os
        from dotenv import load_dotenv
        load_dotenv()
        
        session_id = data.get("session_id")
        user_message = data.get("message", "").strip()
        conversation_history = data.get("conversation_history", []) or []
        
        if not user_message:
            raise HTTPException(status_code=400, detail="Message is required")
        
        if not session_id:
            # 如果没提供 session_id，创建一个临时的
            session_id = f"chat_{os.urandom(4).hex()}"
        
        # 从状态机获取当前会话上下文
        sm = get_state_machine()

        # 优先从 Redis 恢复会话上下文（重启后端 / 刷新页面不丢），内存兜底
        news_context = None
        mm = get_memory_manager()
        if mm is not None:
            try:
                saved = mm.get_session_context(session_id)
                if saved:
                    news_context = {
                        "topic": saved.get("topic", ""),
                        "summary": saved.get("summary", ""),
                        "core_concepts": saved.get("core_concepts", []),
                    }
            except Exception as e:
                logger.warning(f"[WARN] Failed to load session context from Redis: {e}")

        if news_context is None:
            session_context = sm.active_sessions.get(session_id) if session_id else None
            if session_context:
                news_context = {
                    "topic": session_context.get("topic", ""),
                    "summary": session_context.get("summary", ""),
                    "core_concepts": session_context.get("core_concepts", []),
                }
            else:
                # 没有会话上下文时，退化为通用说明
                news_context = {
                    "topic": "当前技术主题",
                    "summary": "请在资讯卡片上点击「开始讲解」创建学习会话后再提问，以便获得针对该技术的回答。",
                    "core_concepts": [],
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
{chr(10).join([f"{msg['role']}: {msg['content']}" for msg in conversation_history[-10:]]) if conversation_history else "暂无历史"}
"""
        
        # 调用 LLM
        if LLM_AVAILABLE and llm is not None:
            try:
                response = llm.invoke(system_prompt)
                ai_response = response.content
                logger.info(f"✅ LLM generated response for session {session_id}")
            except Exception as llm_error:
                logger.error(f"❌ LLM generation failed: {llm_error}")
                ai_response = _generate_mock_response(user_message, news_context)
        else:
            # Mock 模式
            ai_response = _generate_mock_response(user_message, news_context)
        
        # 更新对话历史
        updated_history = conversation_history + [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": ai_response}
        ]
        
        return {
            "session_id": session_id,
            "response": ai_response,
            "conversation_history": updated_history,
            "sources": news_context
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Chat processing failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/response")
async def process_user_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    处理用户响应
    
    ## 请求体
    ```json
    {
      "session_id": "sess_abc123",
      "user_input": "展开讲讲"
    }
    ```
    """
    
    try:
        sm = get_state_machine()
        
        # TODO: 从 Redis 获取 session 上下文
        context = {
            "session_id": data["session_id"],
            "topic": "Rust 内存安全",
            "time_budget_minutes": 15,
            "core_concepts": ["ownership", "borrowing"]
        }
        
        result = await sm.process_user_response(
            context=context,
            user_input=data.get("user_input", "")
        )
        
        logger.info(f"📖 Processing response for {data['session_id']}")
        
        return result
        
    except Exception as e:
        logger.error(f"❌ Response processing failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/quiz")
async def submit_quiz_answer(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    提交测验答案
    
    ## 请求体
    ```json
    {
      "session_id": "sess_abc123",
      "user_answers": [3, 0]
    }
    ```
    """
    
    try:
        sm = get_state_machine()
        
        # TODO: 从 Redis 获取 session 上下文
        context = {
            "session_id": data["session_id"],
            "quiz_questions": [
                {"correct_answer": 3},
                {"correct_answer": 0}
            ]
        }
        
        result = await sm.submit_quiz(
            context=context,
            user_answers=data.get("user_answers", [])
        )
        
        logger.info(f"📊 Quiz submitted and evaluated for {data['session_id']}")
        
        return result
        
    except Exception as e:
        logger.error(f"❌ Quiz submission failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/complete")
async def complete_session(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    完成学习会话
    
    ## 请求体
    ```json
    {"session_id": "sess_abc123"}
    ```
    """
    
    try:
        sm = get_state_machine()
        
        # TODO: 从 Redis 获取 session 上下文
        context = {
            "session_id": data["session_id"],
            "start_time": datetime.now(timezone.utc),
            "quiz_score": 0.8,
            "fsrs_rating": 4,
            "fsrs_stability": 14.0
        }
        
        result = await sm.complete_session(context)
        
        logger.info(f"✨ Session completed: {data['session_id']}")
        
        return result
        
    except Exception as e:
        logger.error(f"❌ Completion failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# 健康检查
@router.get("/health")
async def health_check():
    """学习模块健康状态"""
    return {
        "status": "healthy",
        "state_machine_ready": state_machine is not None,
        "uptime": "24h"
    }
