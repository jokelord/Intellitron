"""
API 路由模块

提供 RESTful API 接口，包括聊天、健康检查等功能。
"""

from .chat import router as chat_router

__all__ = ["chat_router"]
