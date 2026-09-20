# API Routes - __init__.py
from .discover import router as discover_router
from .learning import router as learning_router
from .me import router as me_router

__all__ = ["discover_router", "learning_router", "me_router"]
