"""POST /v1/compact/run — manual /compact endpoint.

Wraps CompactOrchestrator and yields compact_* events as SSE lines.
The frontend parses these the same way it parses timeline events.

Routing rules:

* mode='agent' (with session_id) — routes to
  ``AgentRunner.run_compact_for_session`` so the agent session's
  ``memory.steps`` is actually replaced by the envelope. Without this
  routing, manual /compact in agent mode would generate a summary in
  the chat context but leave the agent's memory untouched (see
  AGENTS.md trap #21 + the post-/compact regression series in
  踩坑记录3.md for the full history).
* mode='chat' (and any other mode) — falls through to the chat
  CompactOrchestrator with the request body messages.
* mode='cycle' — same fallthrough; the cycle executor reads the
  result from the same SSE stream.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..config import get_config
from ..compact.types import CompactEvent, CompactEventKind, CompactRequest
from ..compact.chat_message_filter import filter_messages_for_llm
from ..llm_service import Message
from backend.agent.canonical_messages import canonicalize_model_messages

router = APIRouter(prefix="/v1", tags=["compact"])


class CompactRunMessage(BaseModel):
    role: str
    content: Any
    reasoning_content: Optional[str] = None
    tool_calls: Optional[list[dict]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    metadata: Optional[dict] = None


class CompactRunRequest(BaseModel):
    mode: str = Field(..., description="chat | agent | cycle")
    session_id: Optional[str] = None
    messages: Optional[list[CompactRunMessage]] = None
    chat_history_start_ref: Optional[str] = None
    custom_instructions: Optional[str] = None
    tools: Optional[list[dict]] = None


def _event_to_dict(event: CompactEvent) -> dict:
    """Flatten CompactEvent to an SSE-friendly dict.

    In addition to the base fields (kind / summary_message_id / text /
    partial / metadata), we also expose trigger / mode / original_tokens /
    threshold / strategy at the TOP LEVEL when they are present, so the
    frontend's projectTimelineEvent can read them without having to know
    about the metadata nesting convention.
    """
    out: dict = {
        "kind": event.kind.value if isinstance(event.kind, CompactEventKind) else str(event.kind),
        "summary_message_id": event.summary_message_id,
        "text": event.text,
        "partial": event.partial,
        "metadata": event.metadata,
    }
    meta = event.metadata or {}
    for key in ("trigger", "mode", "original_tokens", "threshold", "strategy"):
        if key in meta and meta[key] is not None:
            out[key] = meta[key]
    return out


def _sse_line(event: CompactEvent) -> str:
    return f"data: {json.dumps(_event_to_dict(event), ensure_ascii=False)}\n\n"


def _sse_done() -> str:
    return "data: [DONE]\n\n"


@router.post("/compact/run")
async def run_compact(request: CompactRunRequest, raw_request: Request):
    # ----- Agent-mode routing -----
    # Manual /compact in agent mode MUST be processed by the AgentRunner,
    # not by the chat compact orchestrator. The chat orchestrator only
    # has access to the request-body messages and can't touch
    # session.agent.memory.steps. Routing to run_compact_for_session
    # makes the session's memory actually get replaced.
    if request.mode == "agent":
        agent_runner = getattr(raw_request.app.state, "agent_runner", None)
        if agent_runner is None:
            async def _err():
                err = CompactEvent.failed(
                    "compact-unavailable", "service_unavailable",
                    "agent runner not initialized; cannot compact agent session",
                )
                yield _sse_line(err)
                yield _sse_done()
            return StreamingResponse(
                _err(), media_type="text/event-stream", status_code=503
            )
        if not request.session_id:
            async def _err():
                err = CompactEvent.failed(
                    "compact-bad-request", "missing_session_id",
                    "mode='agent' requires session_id in the request body",
                )
                yield _sse_line(err)
                yield _sse_done()
            return StreamingResponse(
                _err(), media_type="text/event-stream", status_code=400
            )

        async def stream_agent() -> AsyncGenerator[str, None]:
            try:
                async for ev in agent_runner.run_compact_for_session(
                    request.session_id,
                    mode="agent",
                    trigger="manual",
                    custom_instructions=request.custom_instructions,
                ):
                    yield _sse_line(ev)
            except Exception as exc:  # safety net
                failed = CompactEvent.failed(
                    "compact-unknown", "internal_error", str(exc),
                )
                yield _sse_line(failed)
            yield _sse_done()

        return StreamingResponse(stream_agent(), media_type="text/event-stream")

    # ----- Chat / cycle fallthrough -----
    orchestrator = raw_request.app.state.compact_orchestrator
    if orchestrator is None:
        # compact module not initialized
        async def _err():
            err = CompactEvent.failed("compact-unavailable", "service_unavailable",
                                      "compact orchestrator not initialized")
            yield _sse_line(err)
            yield _sse_done()
        return StreamingResponse(_err(), media_type="text/event-stream",
                                status_code=503)

    body_messages = None
    cache_first_body_messages = None
    memory_record = None
    memory_source_snapshot = {}
    memory_bootstrap_snapshot = {}
    if request.messages:
        cfg = get_config()
        prompts_cfg = cfg.agent_prompts.compaction
        compact_cfg = cfg.compact
        source_messages = list(request.messages)
        if request.mode == "chat" and request.session_id:
            store = getattr(raw_request.app.state, "chat_session_store", None)
            memory_record = store.get(request.session_id) if store is not None else None
            if memory_record is not None:
                try:
                    source_messages, _bootstrap_updates = memory_record.resolve_chat_bootstrap(
                        source_messages, request.chat_history_start_ref, current_user_is_new=False)
                    source_messages, _updates, _retiring = memory_record.resolve_local_memory_history(
                        source_messages, current_user_is_new=False)
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                memory_source_snapshot = memory_record.local_memory_source_snapshot()
                memory_bootstrap_snapshot = memory_record.local_memory_bootstrap_snapshot()
        filtered_messages = filter_messages_for_llm(
            source_messages,
            prompts_cfg,
        )
        body_messages = canonicalize_model_messages(filtered_messages)
        cache_first_source = body_messages
        if (
            compact_cfg.prompt_shape == "cache_first_tail_with_fallback"
            and compact_cfg.prompt_control_enabled
            and cache_first_source
            and cache_first_source[0].role == "system"
        ):
            cache_first_body_messages = cache_first_source

    req = CompactRequest(
        mode=request.mode,
        session_id=request.session_id,
        body_messages=body_messages,  # v2: turn-shape prompt for SGLang radix cache reuse
        messages=None,                 # v1 field is deprecated; force v2 path
        cache_first_body_messages=cache_first_body_messages,
        reference_messages=cache_first_body_messages,
        tools=request.tools,
        trigger="manual",
        custom_instructions=request.custom_instructions,
    )

    async def stream() -> AsyncGenerator[str, None]:
        try:
            async for ev in orchestrator.run_compact(req):
                if memory_record is not None and ev.kind == CompactEventKind.COMPLETED and ev.text:
                    if not memory_record.register_local_memory_compaction(ev.summary_message_id, ev.text, memory_source_snapshot, memory_bootstrap_snapshot):
                        yield _sse_line(CompactEvent.failed(ev.summary_message_id, "compact_source_changed"))
                        break
                yield _sse_line(ev)
        except Exception as exc:  # safety net
            failed = CompactEvent.failed("compact-unknown", "internal_error", str(exc))
            yield _sse_line(failed)
        yield _sse_done()

    return StreamingResponse(stream(), media_type="text/event-stream")
