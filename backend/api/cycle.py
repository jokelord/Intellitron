"""
Cycle API 路由

提供 Cycle 可编程序列的 API 端点。
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import StreamingResponse

from backend.cycle.models import CycleStartRequest, CycleStopRequest
from backend.cycle.run_registry import (
    CycleRunConflict,
    CycleRunExpired,
    CycleRunNotFound,
    CycleRunUnavailable,
)

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

    if request.cycle_run_id and request.session_id and request.cycle_run_id != request.session_id:
        raise HTTPException(status_code=409, detail="cycle_run_identity_mismatch")
    try:
        record = executor.ensure_run(request)
    except CycleRunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return StreamingResponse(
        record.events(0, heartbeat_seconds=executor._heartbeat_seconds),
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

    run_id = request.cycle_run_id or request.session_id
    if request.cycle_run_id and request.session_id and request.cycle_run_id != request.session_id:
        raise HTTPException(status_code=409, detail="cycle_run_identity_mismatch")
    stopped = executor.stop(run_id)
    return {"stopped": stopped, "session_id": run_id, "cycle_run_id": run_id}


@router.get("/cameras")
async def list_cameras(raw_request: Request):
    """列出可用摄像头"""
    camera_service = raw_request.app.state.camera_service
    if camera_service is None:
        return {"cameras": []}

    cameras = camera_service.list_cameras()
    return {"cameras": cameras}


def _resolve_run(raw_request, cycle_run_id, request_fingerprint=None):
    executor = raw_request.app.state.cycle_executor
    if executor is None:
        raise HTTPException(status_code=503, detail="Cycle 服务未启用")
    try:
        record = executor.registry.resolve(cycle_run_id)
    except CycleRunExpired as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except CycleRunUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CycleRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if request_fingerprint is not None and record.request_fingerprint != request_fingerprint:
        raise HTTPException(status_code=409, detail="cycle_run_fingerprint_mismatch")
    return record


@router.get("/{cycle_run_id}")
async def cycle_status(cycle_run_id: str, raw_request: Request):
    return _resolve_run(raw_request, cycle_run_id).snapshot()


@router.get("/{cycle_run_id}/events")
async def cycle_events(cycle_run_id: str, raw_request: Request,
                       after_sequence: int = Query(default=0, ge=0),
                       request_fingerprint: str | None = None):
    record = _resolve_run(raw_request, cycle_run_id, request_fingerprint)
    return StreamingResponse(
        record.events(after_sequence, heartbeat_seconds=raw_request.app.state.cycle_executor._heartbeat_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
