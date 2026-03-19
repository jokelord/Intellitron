"""
Agent API 路由

提供 Agent 对话端点，支持流式 SSE 和非流式 JSON 两种响应模式。
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/agent", tags=["agent"])


class AgentChatRequest(BaseModel):
    """Agent 对话请求"""
    session_id: Optional[str] = None
    message: str
    images: Optional[list[str]] = None  # base64 图片列表，如 ["data:image/jpeg;base64,..."]
    stream: bool = True
    mode: Optional[str] = None  # "tool_calling" | "code"，None 使用默认值


class AgentChatResponse(BaseModel):
    """Agent 非流式对话响应"""
    session_id: str
    answer: str
    steps: list


@router.post("/chat")
async def agent_chat(request: AgentChatRequest, raw_request: Request):
    """
    Agent 对话端点

    流式模式返回 SSE 事件流：
    - {"type": "agent_step", "step_type": "thought|tool_call|observation", ...}
    - {"type": "content", "content": "最终答案"}
    - {"type": "done"}

    非流式模式返回 JSON：
    - {"session_id": "...", "answer": "...", "steps": [...]}
    """
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    session_id = request.session_id
    message = request.message
    images = request.images
    mode = request.mode

    if not message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    if request.stream:
        return StreamingResponse(
            agent_runner.run_stream(session_id, message, mode, images=images),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        result = await agent_runner.run_sync(session_id, message, mode)
        return JSONResponse(content=result)


@router.delete("/session/{session_id}")
async def delete_session(session_id: str, raw_request: Request):
    """删除指定 Agent 会话"""
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    agent_runner.sessions.delete(session_id)
    return {"status": "ok", "message": f"Session {session_id} 已删除"}


@router.get("/sessions")
async def list_sessions(raw_request: Request):
    """获取活跃 session 数量"""
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    return {"active_sessions": agent_runner.sessions.active_count}
