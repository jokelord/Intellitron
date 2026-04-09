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
    images: Optional[list[str]] = (
        None  # base64 图片列表，如 ["data:image/jpeg;base64,..."]
    )
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


class AgentStopRequest(BaseModel):
    """停止 Agent 请求"""

    session_id: str


@router.post("/stop")
async def stop_agent(request: AgentStopRequest, raw_request: Request):
    """停止正在进行的 Agent 执行

    优雅地中止 Agent，不会立即杀死进程，而是标记中止状态。
    同时调用 SGLang abort 停止 GPU 推理。
    """
    from ..llm_service import abort_sglang_all
    from ..config import get_config

    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    # Mark agent session as aborted
    success = agent_runner.abort_session(request.session_id)

    # Also call SGLang abort_all to stop GPU inference
    config = get_config()
    sglang_url = f"http://{config.sglang.host}:{config.sglang.port}"
    try:
        abort_sglang_all(sglang_url)
        logger.info("[AGENT STOP] SGLang abort_all completed")
    except Exception as e:
        logger.warning(f"[AGENT STOP] SGLang abort error: {e}")

    if success:
        return {"status": "stopped", "session_id": request.session_id}
    else:
        return {"status": "not_found", "session_id": request.session_id}


@router.post("/stop-all")
async def stop_all_agents(raw_request: Request):
    """停止所有正在进行的 Agent 执行

    不需要 session_id，直接中止所有运行中的请求。
    同时标记 SessionStore 中所有 session 为 aborted，避免旧状态泄漏到后续请求。
    """
    from ..llm_service import abort_sglang_all
    from ..config import get_config

    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    # Abort all sessions in SessionStore to prevent old state leakage
    from ..agent.agent_runner import abort_manager

    stopped_sessions = []
    for session_id, session in agent_runner.sessions._sessions.items():
        session.aborted = True
        abort_manager.abort(session_id)
        stopped_sessions.append(session_id)
    if stopped_sessions:
        logger.info(
            f"[AGENT STOP-ALL] Marked {len(stopped_sessions)} sessions as aborted: {stopped_sessions}"
        )

    # Call SGLang abort_all to stop all GPU inference
    config = get_config()
    sglang_url = f"http://{config.sglang.host}:{config.sglang.port}"
    try:
        abort_sglang_all(sglang_url)
        logger.info("[AGENT STOP-ALL] SGLang abort_all completed")
    except Exception as e:
        logger.warning(f"[AGENT STOP-ALL] SGLang abort error: {e}")

    return {"status": "stopped_all", "stopped_sessions": stopped_sessions}


@router.post("/answer-now")
async def answer_now(request: AgentStopRequest, raw_request: Request):
    """强制 Agent 立即返回当前累积的答案

    停止当前工具执行（如果有），并基于已有的思考过程生成最终答案。
    """
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    success = agent_runner.answer_now_session(request.session_id)

    if success:
        return {"status": "answer_now_triggered", "session_id": request.session_id}
    else:
        return {"status": "not_found", "session_id": request.session_id}
