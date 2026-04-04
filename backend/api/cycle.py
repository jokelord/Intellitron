"""
Cycle API 路由

提供 Cycle 可编程序列的 API 端点。
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from backend.cycle.models import CycleStartRequest, CycleStopRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/cycle", tags=["cycle"])


@router.post("/start")
async def cycle_start(request: CycleStartRequest, raw_request: Request):
    """
    启动 Cycle 序列执行

    返回 SSE 事件流。
    """
    executor = raw_request.app.state.cycle_executor
    if executor is None:
        raise HTTPException(status_code=503, detail="Cycle 服务未启用")

    session_id = request.session_id or str(uuid.uuid4())

    return StreamingResponse(
        executor.start(
            session_id=session_id,
            steps=request.steps,
            agent_mode=request.agent_mode,
            share_context=request.share_context,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/stop")
async def cycle_stop(request: CycleStopRequest, raw_request: Request):
    """停止正在运行的 Cycle"""
    executor = raw_request.app.state.cycle_executor
    if executor is None:
        raise HTTPException(status_code=503, detail="Cycle 服务未启用")

    stopped = executor.stop(request.session_id)
    return {"stopped": stopped, "session_id": request.session_id}


@router.get("/cameras")
async def list_cameras(raw_request: Request):
    """列出可用摄像头"""
    camera_service = raw_request.app.state.camera_service
    if camera_service is None:
        return {"cameras": []}

    cameras = camera_service.list_cameras()
    return {"cameras": cameras}
