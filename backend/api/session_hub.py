"""Canonical recovery subscription for shared Chat and Agent Session Hubs."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from backend.session_hub_runtime import SessionHubRuntime, subscribe_session_hub
from backend.api.chat_session_store import ChatSessionStore
from backend.config import get_config

router = APIRouter(prefix="/v1", tags=["session-hub"])


def _unavailable_session() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail="Session is unavailable or expired",
    )


def _validate_session_hub_cursor(value: Any) -> int:
    if isinstance(value, bool):
        raise HTTPException(
            status_code=422,
            detail="after_sequence must be a non-negative integer",
        )
    if isinstance(value, int):
        cursor = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            cursor = int(value)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="after_sequence must be a non-negative integer",
            ) from None
    else:
        raise HTTPException(
            status_code=422,
            detail="after_sequence must be a non-negative integer",
        )
    if cursor < 0:
        raise HTTPException(
            status_code=422,
            detail="after_sequence must be a non-negative integer",
        )
    return cursor


async def _targeted_abort_chat_request(session_id: str, request_id: str) -> None:
    """Abort exactly one Chat upstream request; never use abort-all fallback."""

    config = get_config()
    from backend.llm_service import abort_request_by_id

    await asyncio.to_thread(
        abort_request_by_id,
        request_id,
        f"http://{config.sglang.host}:{config.sglang.port}",
        fallback_to_abort_all=False,
    )


async def _wait_for_chat_cleanup(
    record,
    request_id: str | None,
    turn_index: int,
    initial_task: Any,
) -> BaseException | None:
    """Wait for this turn's cleanup, tolerating producer failure after cleanup."""

    task = initial_task
    failure: BaseException | None = None
    while True:
        # A concurrent exact-store replacement may release this pinned Hub;
        # its old producer can no longer affect the replacement entry.
        if record.hub.released:
            return failure
        if record.finished and record.request_id == request_id and record.turn_index == turn_index:
            return failure
        if task is None:
            task = record.current_producer_task(
                request_id=request_id, turn_index=turn_index
            )
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                # A timeout/cancellation of this DELETE task must remain
                # observable; only cancellation owned by the producer task is
                # reconciled here.
                if not task.cancelled():
                    raise
                failure = exc
            except Exception as exc:
                failure = exc
            task = None
            if record.finished and record.request_id == request_id and record.turn_index == turn_index:
                return failure
            if record.hub.has_active_producer:
                return failure
        # Poll briefly rather than blocking an executor thread: a producer
        # task may be attached in the narrow admission/install gap.
        await asyncio.sleep(0.001)


@router.get("/session/{session_id}/events")
async def session_hub_events(
    session_id: str,
    raw_request: Request,
    after_sequence: Any = Query(default="0"),
):
    """Replay canonical records, then follow the active producer generation."""

    cursor = _validate_session_hub_cursor(after_sequence)
    runtime = getattr(raw_request.app.state, "session_hub_runtime", None)
    if not isinstance(runtime, SessionHubRuntime):
        raise HTTPException(status_code=503, detail="Session Hub is unavailable")
    entry = runtime.resolve(session_id)
    if entry is None or entry.hub.released:
        raise HTTPException(
            status_code=404,
            detail="Session is unavailable or expired",
        )
    return StreamingResponse(
        subscribe_session_hub(entry, cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _delete_chat_session(
    raw_request: Request,
    runtime: SessionHubRuntime,
    entry,
) -> None:
    store = getattr(raw_request.app.state, "chat_session_store", None)
    if not isinstance(store, ChatSessionStore):
        raise HTTPException(status_code=503, detail="Chat session store is unavailable")
    pinned = store.begin_delete_if_entry(entry)
    if pinned is None:
        raise _unavailable_session()
    record, request_id, produce_task, finished = pinned
    active = entry.hub.has_active_producer or not finished
    config = get_config()
    timeout_seconds = config.agent.session_disposal_timeout_seconds

    if active:
        try:
            async with asyncio.timeout(timeout_seconds):
                if request_id:
                    try:
                        await _targeted_abort_chat_request(
                            entry.session_id, request_id
                        )
                    except Exception:
                        # Cleanup ownership remains authoritative if transport abort fails.
                        pass
                failure = await _wait_for_chat_cleanup(
                    record,
                    request_id,
                    record.deletion_turn_index or record.turn_index,
                    produce_task,
                )
        except TimeoutError as exc:
            from backend.agent.agent_runner import SessionDisposalError

            error = SessionDisposalError(
                "session_disposal_timeout",
                pending_scope_count=1,
                pending_child_count=0,
            )
            raise HTTPException(status_code=503, detail=error.as_detail()) from exc

        if failure is not None and entry.hub.has_active_producer:
            from backend.agent.agent_runner import SessionDisposalError

            error = SessionDisposalError(
                "session_disposal_failed",
                pending_scope_count=1,
                pending_child_count=0,
            )
            raise HTTPException(status_code=503, detail=error.as_detail())

    if entry.hub.has_active_producer:
        from backend.agent.agent_runner import SessionDisposalError

        error = SessionDisposalError(
            "session_disposal_failed",
            pending_scope_count=1,
            pending_child_count=0,
        )
        raise HTTPException(status_code=503, detail=error.as_detail())
    if not store.release_if_same(record, entry):
        runtime.release_if_same(entry)


async def _delete_agent_session(
    raw_request: Request,
    runtime: SessionHubRuntime,
    entry,
) -> None:
    runner = getattr(raw_request.app.state, "agent_runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="Agent service is unavailable")
    session = runner.sessions.get(entry.session_id)
    if (
        session is None
        or session.event_hub is not entry.hub
        or session.session_hub_entry is not entry
    ):
        raise _unavailable_session()

    from backend.agent.agent_runner import SessionDisposalError

    try:
        await runner.dispose_session_exact(
            entry.session_id,
            expected_session=session,
        )
    except SessionDisposalError as exc:
        raise HTTPException(status_code=503, detail=exc.as_detail()) from exc
    runtime.release_if_same(entry)


@router.delete("/session/{session_id}")
async def delete_session_hub(session_id: str, raw_request: Request):
    """Explicitly delete one exact recoverable Chat or Agent session."""

    runtime = getattr(raw_request.app.state, "session_hub_runtime", None)
    if not isinstance(runtime, SessionHubRuntime):
        raise HTTPException(status_code=503, detail="Session Hub is unavailable")
    entry = runtime.resolve(session_id)
    if entry is None or entry.hub.released:
        raise _unavailable_session()
    if entry.kind == "chat":
        await _delete_chat_session(raw_request, runtime, entry)
    elif entry.kind == "agent":
        await _delete_agent_session(raw_request, runtime, entry)
    else:
        raise _unavailable_session()
    return {"status": "deleted", "session_id": session_id, "kind": entry.kind}
