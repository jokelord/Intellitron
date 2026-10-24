"""
Agent API 路由

提供 Agent 对话端点，支持流式 SSE 和非流式 JSON 两种响应模式。
"""

import asyncio
import json
import logging
import threading
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator

from ..agent.session_hub import AgentSessionHub
from ..config import get_config
from ..llm_service import create_request_id
from ..request_lifecycle import (
    RequestLifecycleCapacityError,
    RequestLifecycleConflict,
    RequestLifecycleValidationError,
)
from ..security.path_policy import PathPolicy, PathPolicyError
from ..session_hub_runtime import (
    SessionHubEntry,
    SessionHubRuntime,
    SessionProducerPublisher,
    subscribe_session_hub,
)
from ..vision.contracts import (
    VisionAutomationBinding,
    VisionAutomationTransportMetadata,
    fingerprint_request_payload,
    validate_automation_lifecycle_id,
)
from ..vision.validation import (
    VisionRequestValidationError,
    validate_agent_final_image_count,
    validate_agent_vision_request,
)
from ..agent.tool_configuration import (
    AgentToolConfigurationError,
    AgentToolConfigurationRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/agent", tags=["agent"])
# Kept separate so the Answer Now implementation remains available to internal
# callers and regression tests without exposing the feature through the public API.
_answer_now_router = APIRouter(prefix="/v1/agent", tags=["agent"])
_agent_producer_tasks: set[asyncio.Task] = set()


def _validate_session_id_value(value: str) -> str:
    """Reject path-like session tokens before they reach lifecycle or storage code."""
    try:
        return PathPolicy.validate_session_id(value)
    except PathPolicyError as exc:
        raise ValueError("invalid session identifier") from exc


def _require_valid_session_id(session_id: str) -> str:
    try:
        return PathPolicy.validate_session_id(session_id)
    except PathPolicyError as exc:
        raise HTTPException(status_code=422, detail="invalid session identifier") from exc


class _AnswerNowStreamingResponse(StreamingResponse):
    """Start an independently owned replacement and subscribe to its Hub."""

    def __init__(
        self,
        agent_runner,
        session,
        stream_factory,
        *,
        registry,
        request_id: str,
        payload_fingerprint: str,
        is_disconnected,
        session_hub_runtime: SessionHubRuntime | None,
    ):
        async def _unused():
            if False:
                yield b""

        super().__init__(_unused(), media_type="text/event-stream")
        self._agent_runner = agent_runner
        self._session = session
        self._stream_factory = stream_factory
        self._registry = registry
        self._request_id = request_id
        self._payload_fingerprint = payload_fingerprint
        self._is_disconnected = is_disconnected
        self._session_hub_runtime = session_hub_runtime

    async def __call__(self, scope, receive, send) -> None:
        try:
            reservation = self._registry.reserve(
                request_id=self._request_id,
                request_kind="agent_answer_now",
                context_identity=self._session.session_id,
                payload_fingerprint=self._payload_fingerprint,
            )
            while not reservation.should_start and reservation.status == "unknown":
                await asyncio.to_thread(
                    self._registry.wait_for_reservation_resolution,
                    request_id=self._request_id,
                    request_kind="agent_answer_now",
                    context_identity=self._session.session_id,
                    payload_fingerprint=self._payload_fingerprint,
                    wait_token=reservation.wait_token,
                )
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise asyncio.CancelledError
                if await self._is_disconnected():
                    return
                reservation = self._registry.reserve(
                    request_id=self._request_id,
                    request_kind="agent_answer_now",
                    context_identity=self._session.session_id,
                    payload_fingerprint=self._payload_fingerprint,
                )
        except RequestLifecycleValidationError as exc:
            await JSONResponse(
                status_code=422,
                content={"detail": "invalid request lifecycle metadata"},
            )(scope, receive, send)
            return
        except RequestLifecycleConflict as exc:
            await JSONResponse(
                status_code=409,
                content={"detail": "request ID conflict"},
            )(scope, receive, send)
            return
        except RequestLifecycleCapacityError as exc:
            await JSONResponse(
                status_code=503,
                content={"detail": "request lifecycle capacity reached"},
            )(scope, receive, send)
            return
        if not reservation.should_start:
            await JSONResponse(
                {
                    "request_id": self._request_id,
                    "status": reservation.status,
                }
            )(scope, receive, send)
            return

        lifecycle_token = reservation.registration_token
        acquisition_status, acquired_session = (
            self._agent_runner.try_acquire_answer_now(
                self._session.session_id,
                expected_session=self._session,
            )
        )
        if acquisition_status != "acquired" or acquired_session is not self._session:
            self._registry.abandon(lifecycle_token)
            await JSONResponse(
                status_code=409,
                content={"detail": "Answer Now unavailable"},
            )(scope, receive, send)
            return

        hub = getattr(self._session, "event_hub", None)
        if not isinstance(hub, AgentSessionHub):
            self._registry.abandon(lifecycle_token)
            self._session.finish_answer_now()
            await JSONResponse(
                status_code=409,
                content={"detail": "Answer Now session Hub unavailable"},
            )(scope, receive, send)
            return
        previous_producer_token = hub.capture_active_producer()
        state = _AgentProducerState(hub.last_sequence)
        entry = _resolve_agent_session_hub_entry(
            self._agent_runner,
            self._session,
            self._session_hub_runtime,
        )
        _start_answer_now_session_producer(
            state=state,
            source_factory=self._stream_factory,
            registry=self._registry,
            lifecycle_token=lifecycle_token,
            request_id=self._request_id,
            session=self._session,
            entry=entry,
            previous_producer_token=previous_producer_token,
        )
        await state.ready.wait()
        if state.error is not None:
            if (
                isinstance(state.error, RuntimeError)
                and str(state.error)
                == "Answer Now replacement was not registered"
            ):
                await JSONResponse(
                    status_code=409,
                    content={
                        "detail": "Answer Now replacement was not registered"
                    },
                )(scope, receive, send)
                return
            raise state.error
        response = StreamingResponse(
            subscribe_session_hub(state.entry, state.start_sequence),
            media_type="text/event-stream",
            headers=dict(self.headers),
        )
        await response(scope, receive, send)


class AgentChatRequest(BaseModel):
    """Agent 对话请求"""

    session_id: Optional[str] = None
    message: str
    images: Optional[list[str]] = (
        None  # base64 图片列表，如 ["data:image/jpeg;base64,..."]
    )
    context_messages: Optional[list[dict[str, Any]]] = None
    stream: bool = True
    mode: Optional[str] = None  # 仅支持 tool_calling；旧值 code 会自动兼容映射
    enable_thinking: Optional[bool] = None
    enable_local_memory: StrictBool = False
    request_id: Optional[str] = None
    vision_automation: Optional[VisionAutomationTransportMetadata] = None
    tool_configuration: AgentToolConfigurationRequest | None = None

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str | None) -> str | None:
        return None if value is None else _validate_session_id_value(value)


class AgentChatResponse(BaseModel):
    """Agent 非流式对话响应"""

    session_id: str
    answer: str
    steps: list


class ChildAgentAbortRequest(BaseModel):
    child_run_id: str

    @field_validator("child_run_id")
    @classmethod
    def _validate_child_run_id(cls, value: str) -> str:
        return _validate_session_id_value(value)


class AgentVisionAutomationAbortRequest(BaseModel):
    """Register browser-local Abort cleanup authorization for one Agent context."""

    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str
    binding: VisionAutomationBinding
    cleanup_token: str

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id_value(value)

    @field_validator("cleanup_token")
    @classmethod
    def _validate_cleanup_token(cls, value: str) -> str:
        return validate_automation_lifecycle_id(value)


class AgentAnswerNowStreamRequest(BaseModel):
    """Lifecycle identity for one streaming Answer Now replacement."""

    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str
    request_id: Any = None

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id_value(value)


def _reserve_agent_lifecycle(raw_request: Request, request, request_id: str):
    registry = getattr(raw_request.app.state, "request_lifecycle_registry", None)
    if registry is None:
        return None, None, None
    try:
        reservation = registry.reserve(
            request_id=request_id,
            request_kind="agent",
            context_identity=request.session_id,
            payload_fingerprint=fingerprint_request_payload(
                request.model_dump(mode="json", exclude={"request_id"})
            ),
        )
    except RequestLifecycleValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail="invalid request lifecycle metadata",
        ) from exc
    except RequestLifecycleConflict as exc:
        raise HTTPException(status_code=409, detail="request ID conflict") from exc
    except RequestLifecycleCapacityError as exc:
        raise HTTPException(
            status_code=503,
            detail="request lifecycle capacity reached",
        ) from exc
    if not reservation.should_start:
        return registry, None, JSONResponse(
            {"request_id": request_id, "status": reservation.status}
        )
    return registry, reservation.registration_token, None


def _top_level_agent_historical_image_count(agent_runner, session_id: str | None) -> int:
    """Count canonical task images without scanning child or sibling sessions."""

    if session_id is None:
        return 0
    sessions = getattr(agent_runner, "sessions", None)
    if sessions is None:
        raise VisionRequestValidationError("cannot inspect canonical image shape")
    try:
        session = sessions.get(session_id)
    except Exception:
        raise VisionRequestValidationError(
            "cannot inspect canonical image shape"
        ) from None
    if session is None:
        return 0
    memory = getattr(getattr(session, "agent", None), "memory", None)
    steps = getattr(memory, "steps", None)
    if steps is None:
        raise VisionRequestValidationError("cannot inspect canonical image shape")
    count = 0
    try:
        for step in steps:
            task_images = getattr(step, "task_images", None)
            if task_images is None:
                continue
            if not isinstance(task_images, (list, tuple)):
                raise VisionRequestValidationError(
                    "cannot inspect canonical image shape"
                )
            count += len(task_images)
    except VisionRequestValidationError:
        raise
    except Exception:
        raise VisionRequestValidationError(
            "cannot inspect canonical image shape"
        ) from None
    return count


class _AgentProducerState:
    """Subscriber-visible startup state for one independently owned producer."""

    def __init__(self, start_sequence: int) -> None:
        self.start_sequence = start_sequence
        self.ready = asyncio.Event()
        self.session = None
        self.hub = None
        self.entry: SessionHubEntry | None = None
        self.error: BaseException | None = None


class _AgentProducerStartup:
    """Failure-atomic ownership state for one producer startup transaction."""

    def __init__(self) -> None:
        self.hub_claimed = False
        self.lifecycle_accept_attempted = False
        self.lifecycle_accepted = False
        self.lifecycle_running = False
        self.outbound_acceptance_published = False
        self.ready_resolved = False

    def resolve_ready(self, state, *, session, hub, entry, error) -> None:
        if self.ready_resolved:
            return
        state.session = session
        state.hub = hub
        state.entry = entry
        state.error = error
        self.ready_resolved = True
        state.ready.set()


def _decode_agent_sse_payload(event: str | bytes) -> dict[str, Any]:
    if isinstance(event, bytes):
        event = event.decode("utf-8")
    if not isinstance(event, str):
        raise TypeError("Agent stream event must be text or bytes")
    data_lines = [line[6:] for line in event.splitlines() if line.startswith("data: ")]
    if len(data_lines) != 1:
        raise ValueError("Agent stream event must contain exactly one SSE data record")
    payload = json.loads(data_lines[0])
    if not isinstance(payload, dict):
        raise ValueError("Agent stream event payload must be an object")
    return payload


def _encode_agent_hub_record(record, session=None) -> str:
    # Transport metadata belongs to a new top-level envelope. Never mutate the
    # recursively frozen canonical Hub payload.
    payload = dict(record.payload)
    event = payload.get("event")
    if isinstance(event, dict) and "image_asset_ids" in event:
        event = dict(event)
        asset_ids = tuple(event.get("image_asset_ids") or ())
        resolved_images = (
            session.resolve_event_hub_images(asset_ids)
            if session is not None and hasattr(session, "resolve_event_hub_images")
            else None
        )
        event.pop("image_asset_ids", None)
        event["images"] = resolved_images if resolved_images is not None else []
        if resolved_images is None and event.get("historical_image_placeholder"):
            event["historical_image_omitted"] = True
        else:
            event.pop("historical_image_placeholder", None)
        payload["event"] = event
    payload["event_sequence"] = record.sequence
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _subscribe_started_agent_producer(state: _AgentProducerState):
    await state.ready.wait()
    if state.error is not None:
        raise state.error
    if state.entry is None:
        raise RuntimeError("Agent Session Hub entry is unavailable")
    async for event in subscribe_session_hub(state.entry, state.start_sequence):
        yield event


class _StartedAgentStreamingResponse(StreamingResponse):
    """Delay response headers until Agent pre-acceptance startup settles.

    The producer owns admission and prompt construction in a background task.
    Waiting inside a normal StreamingResponse generator is too late: Starlette
    may already have sent ``200`` headers before the generator reports a
    prompt-render failure.  This wrapper waits before delegating to the Hub
    stream, so pre-acceptance domain failures retain their stable HTTP status.
    """

    def __init__(self, state: _AgentProducerState, *, headers: dict[str, str]):
        async def _unused():
            if False:
                yield b""

        super().__init__(
            _unused(),
            media_type="text/event-stream",
            headers=headers,
        )
        self._producer_state = state

    async def __call__(self, scope, receive, send) -> None:
        state = self._producer_state
        await state.ready.wait()
        if state.error is not None:
            error = state.error
            if isinstance(error, AgentToolConfigurationError):
                await JSONResponse(
                    status_code=error.status_code,
                    content={
                        "detail": {
                            "code": error.code,
                            "message": error.message,
                        }
                    },
                )(scope, receive, send)
                return
            if isinstance(error, RuntimeError):
                await JSONResponse(
                    status_code=409,
                    content={"detail": str(error)},
                )(scope, receive, send)
                return
            await JSONResponse(
                status_code=500,
                content={"detail": "Agent producer failed before acceptance"},
            )(scope, receive, send)
            return
        if state.entry is None:
            await JSONResponse(
                status_code=500,
                content={"detail": "Agent Session Hub entry is unavailable"},
            )(scope, receive, send)
            return
        response = StreamingResponse(
            subscribe_session_hub(state.entry, state.start_sequence),
            media_type="text/event-stream",
            headers=dict(self.headers),
        )
        await response(scope, receive, send)


class _PrefetchedAgentStreamingResponse(StreamingResponse):
    """Prefetch standalone Agent startup before sending stream headers."""

    def __init__(self, stream, *, admission_stage, agent_runner, headers):
        async def _unused():
            if False:
                yield b""

        super().__init__(_unused(), media_type="text/event-stream", headers=headers)
        self._source = stream
        self._admission_stage = admission_stage
        self._agent_runner = agent_runner

    async def __call__(self, scope, receive, send) -> None:
        async def _cleanup_unaccepted_source() -> None:
            close = getattr(self._source, "aclose", None)
            if callable(close):
                try:
                    await close()
                except BaseException:
                    # Preserve the original startup failure; rollback below
                    # still owns the admission cleanup obligation.
                    pass
            if not getattr(self._admission_stage, "_accepted", False):
                self._agent_runner.rollback_agent_request_stage(
                    self._admission_stage
                )

        try:
            first_event = await anext(self._source)
        except StopAsyncIteration:
            await _cleanup_unaccepted_source()
            await JSONResponse(
                status_code=500,
                content={"detail": "Agent stream ended before acceptance"},
            )(scope, receive, send)
            return
        except AgentToolConfigurationError as exc:
            await _cleanup_unaccepted_source()
            await JSONResponse(
                status_code=exc.status_code,
                content={
                    "detail": {"code": exc.code, "message": exc.message}
                },
            )(scope, receive, send)
            return
        except RuntimeError as exc:
            await _cleanup_unaccepted_source()
            await JSONResponse(status_code=409, content={"detail": str(exc)})(
                scope, receive, send
            )
            return
        except Exception:
            await _cleanup_unaccepted_source()
            raise

        async def _prefetched_source():
            yield first_event
            async for event in self._source:
                yield event

        response = StreamingResponse(
            _prefetched_source(),
            media_type="text/event-stream",
            headers=dict(self.headers),
        )
        await response(scope, receive, send)


def _resolve_agent_session_hub_entry(
    agent_runner,
    session,
    runtime: SessionHubRuntime | None,
) -> SessionHubEntry:
    """Pin the session's one existing Hub without creating another journal."""

    if not isinstance(runtime, SessionHubRuntime):
        raise RuntimeError("shared Session Hub runtime is unavailable")
    existing = runtime.resolve(session.session_id)
    if existing is not None:
        if existing.kind != "agent" or existing.hub is not session.event_hub:
            raise RuntimeError("Agent Session Hub registration changed")
        return existing
    store = getattr(agent_runner, "sessions", None)
    if not callable(getattr(type(store), "ensure_session_hub_registered", None)):
        raise RuntimeError("Agent session is not registered in the shared runtime")
    return store.ensure_session_hub_registered(session, runtime)


def _turn_started_payload(
    session,
    message: str,
    images: list[str] | None,
    vision_automation_metadata: VisionAutomationTransportMetadata | None = None,
) -> dict:
    turn_index = session.next_event_hub_turn_index()
    sticky_omission = bool(
        not getattr(session, "is_child_context", False)
        and getattr(session, "vision_automation_image_omission_sticky", False)
    )
    image_asset_ids = session.stage_event_hub_images(
        turn_index,
        images,
        evict_prior=sticky_omission,
    ).asset_ids
    return {
        "type": "timeline_event",
        "event": {
            "kind": "turn_started",
            "session_id": session.session_id,
            "turn_index": turn_index,
            "user_content": message,
            "images": [],
            "image_asset_ids": image_asset_ids,
            **(
                {
                    "historical_image_placeholder": (
                        "[historical images omitted: count="
                        f"{len(images or ())}; sources=none; relative_range=unavailable]"
                    )
                }
                if images
                else {}
            ),
            "mode": session.mode,
            "historical_image_omission": sticky_omission,
            **(
                {
                    "vision_automation_manual_image_count": (
                        vision_automation_metadata.manual_image_count
                    ),
                }
                if vision_automation_metadata is not None
                else {}
            ),
        },
    }


def _publish_turn_started_payload(
    session,
    payload: dict,
    images,
    publish,
    acceptance_published=lambda: False,
):
    """Atomically commit event assets with the paired Hub append.

    The asset lock is a visibility barrier: encoders resolving an older record
    either finish before the new staged map is installed, or wait until the
    append has committed (or rolled back).  It must therefore cover the Hub
    append itself, not merely the asset-map mutation.
    """

    event = payload.get("event")
    if not isinstance(event, dict):
        raise RuntimeError("turn_started payload is malformed")
    asset_stage = session.stage_event_hub_images(
        event["turn_index"],
        images,
        evict_prior=bool(event.get("historical_image_omission")),
    )
    if tuple(event.get("image_asset_ids") or ()) != asset_stage.asset_ids:
        raise RuntimeError("turn_started asset IDs do not match the session stage")
    with session.vision_event_asset_lock:
        try:
            # Commit while holding the shared resolver barrier, then append by
            # the ordinary Hub API.  The Hub has no callback into this lock,
            # so there is no generic Hub-lock -> asset-lock inversion surface.
            asset_stage.commit_locked()
            return publish(payload)
        except BaseException:
            # This runs before releasing the visibility barrier, including if
            # a partial map update or Hub append itself raises.
            if not acceptance_published():
                asset_stage.rollback_locked()
            raise


async def _run_agent_session_producer(
    *,
    state: _AgentProducerState,
    source_factory,
    registry,
    lifecycle_token,
    request_id: str,
    agent_runner,
    admission_stage,
    initial_session,
    session_hub_runtime: SessionHubRuntime | None,
    message: str,
    images: list[str] | None,
    vision_automation_metadata: VisionAutomationTransportMetadata | None,
    completion: asyncio.Future | None = None,
    result_holder: dict[str, Any] | None = None,
) -> None:
    """Own the Agent source, admission, barrier, registry, and Hub generation."""

    source = None
    session = initial_session
    hub = getattr(session, "event_hub", None)
    producer_token = None
    entry: SessionHubEntry | None = None
    publisher: SessionProducerPublisher | None = None
    startup = _AgentProducerStartup()
    outcome = "error"
    failure: BaseException | None = None
    captured_scope_ids: set[str] = set()
    source_error: BaseException | None = None
    source_done_payload: dict[str, Any] | None = None
    startup_error: BaseException | None = None
    terminal_failure: BaseException | None = None

    def _publish(payload: dict[str, Any]) -> None:
        if publisher is None:
            raise RuntimeError("Agent session publisher is unavailable")
        publisher.publish(payload)

    def _publish_source_payload(payload: dict[str, Any]) -> None:
        nonlocal source_done_payload, source_error
        if payload.get("type") == "done":
            # The runner source does not own terminal publication. Its done is
            # retained only as a template until source close and the captured
            # run-scope barrier have both completed successfully.
            if source_done_payload is None:
                source_done_payload = dict(payload)
            return
        if payload.get("type") == "error":
            if source_error is None:
                source_error = RuntimeError(
                    str(payload.get("error") or "Agent source failed")
                )
            return
        _publish(payload)

    def _accept_registered_session(registered_session, scope_ids) -> None:
        nonlocal session, hub, producer_token, entry, publisher
        if startup.hub_claimed:
            raise RuntimeError("Agent producer registered its run more than once")
        if session is not None and registered_session is not session:
            raise RuntimeError("Agent admission session changed during registration")
        session = registered_session
        hub = session.event_hub
        entry = _resolve_agent_session_hub_entry(
            agent_runner,
            session,
            session_hub_runtime,
        )
        producer_token = hub.claim_producer()
        if producer_token is None:
            raise RuntimeError("Agent session already has an active producer")
        startup.hub_claimed = True
        captured_scope_ids.update(scope_ids)
        publisher = SessionProducerPublisher(
            entry=entry,
            request_id=request_id,
            producer_token=producer_token,
            request_lifecycle_registry=registry,
            reservation_token=lifecycle_token,
        )
        turn_started = _turn_started_payload(
            session,
            message,
            images,
            vision_automation_metadata,
        )
        acceptance_metadata: dict[str, Any] = {}
        snapshot = getattr(session, "tool_configuration_snapshot", None)
        if snapshot is not None:
            acceptance_metadata["tool_configuration"] = snapshot.public_metadata(
                locked=True
            )
        try:
            _publish_turn_started_payload(
                session,
                turn_started,
                images,
                lambda payload: publisher.accept(
                    payload,
                    acceptance_metadata,
                ),
                lambda: publisher.acceptance_published,
            )
        finally:
            startup.lifecycle_accept_attempted = True
            startup.lifecycle_accepted = publisher.lifecycle_accepted
            startup.outbound_acceptance_published = publisher.acceptance_published
            if publisher.acceptance_published:
                startup.resolve_ready(
                    state,
                    session=session,
                    hub=hub,
                    entry=entry,
                    error=None,
                )

    def _on_run_registered(stage, scope_ids) -> None:
        if stage is not admission_stage:
            raise RuntimeError("Agent admission stage changed during registration")
        registered_session = getattr(stage, "_session", None)
        if registered_session is None:
            raise RuntimeError("Agent worker did not bind its session")
        _accept_registered_session(registered_session, scope_ids)

    try:
        if admission_stage is None:
            if session is None:
                raise RuntimeError("Agent session was not created before producer start")
            hub = session.event_hub

        source = source_factory(_on_run_registered)
        try:
            first_event = await anext(source)
        except StopAsyncIteration:
            first_event = None

        if admission_stage is None:
            _accept_registered_session(
                session,
                session.capture_active_run_scopes(),
            )
        elif not startup.outbound_acceptance_published:
            raise RuntimeError("Agent worker did not register its run scope")

        if first_event is not None:
            _publish_source_payload(_decode_agent_sse_payload(first_event))
        async for event in source:
            _publish_source_payload(_decode_agent_sse_payload(event))
        source_status = (
            source_done_payload.get("status")
            if source_done_payload is not None
            else None
        )
        if source_error is not None or source_status == "error":
            outcome = "error"
        elif source_status == "aborted":
            outcome = "aborted"
        else:
            outcome = "completed"
    except asyncio.CancelledError as exc:
        outcome = "aborted"
        failure = exc
        logger.warning("Agent session producer task was cancelled request_id=%s", request_id)
    except BaseException as exc:
        outcome = "error"
        failure = exc
        logger.error(
            "Agent session producer failed request_id=%s: %s",
            request_id,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    finally:
        if source is not None:
            close = getattr(source, "aclose", None)
            if close is not None:
                try:
                    await close()
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                        outcome = "error"
                        logger.error(
                            "Agent session producer source close failed request_id=%s: %s",
                            request_id,
                            exc,
                            exc_info=(type(exc), exc, exc.__traceback__),
                        )

        if admission_stage is not None:
            try:
                captured_scope_ids.update(
                    agent_runner.capture_agent_request_run_scopes(admission_stage)
                )
                await agent_runner.wait_for_agent_request_run_cleanup(
                    admission_stage, frozenset(captured_scope_ids)
                )
            except BaseException as exc:
                if failure is None:
                    failure = exc
                    outcome = "error"
                    logger.error(
                        "Agent session producer barrier failed request_id=%s: %s",
                        request_id,
                        exc,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )

        if publisher is not None and publisher.acceptance_published:
            try:
                terminal_error = failure or source_error
                if outcome == "error" and terminal_error is None:
                    terminal_error = RuntimeError("Agent source failed")
                publisher.finish(
                    outcome,
                    error=terminal_error if outcome == "error" else None,
                )
            except BaseException as exc:
                terminal_failure = exc
                logger.error(
                    "Agent producer terminalization failed request_id=%s: %s",
                    request_id,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
        else:
            startup_error = failure or RuntimeError(
                "Agent producer failed before acceptance"
            )
            try:
                if publisher is not None:
                    publisher.abandon_if_unaccepted()
                else:
                    registry.abandon(lifecycle_token)
            except BaseException as exc:
                logger.error(
                    "Agent producer startup registry cleanup failed request_id=%s: %s",
                    request_id,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            finally:
                try:
                    if (
                        admission_stage is not None
                        and not getattr(admission_stage, "_accepted", False)
                    ):
                        agent_runner.rollback_agent_request_stage(admission_stage)
                except BaseException as exc:
                    logger.error(
                        "Agent producer startup admission rollback failed request_id=%s: %s",
                        request_id,
                        exc,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                finally:
                    startup.resolve_ready(
                        state,
                        session=session,
                        hub=hub,
                        entry=entry,
                        error=startup_error,
                    )
        if completion is not None and not completion.done():
            completion_error = terminal_failure or startup_error or failure
            if completion_error is not None:
                completion.set_exception(completion_error)
            elif result_holder is None or "result" not in result_holder:
                completion.set_exception(
                    RuntimeError("Agent non-stream producer returned no result")
                )
            else:
                completion.set_result(result_holder["result"])


def _observe_agent_producer_task(task: asyncio.Task) -> None:
    _agent_producer_tasks.discard(task)
    try:
        exception = task.exception()
    except asyncio.CancelledError:
        logger.warning("Agent session producer task ended cancelled")
        return
    if exception is not None:
        logger.error(
            "Unobserved Agent session producer exception: %s",
            exception,
            exc_info=(type(exception), exception, exception.__traceback__),
        )


def _start_agent_session_producer(**kwargs) -> asyncio.Task:
    task = asyncio.create_task(_run_agent_session_producer(**kwargs))
    _agent_producer_tasks.add(task)
    task.add_done_callback(_observe_agent_producer_task)
    return task


async def _run_answer_now_session_producer(
    *,
    state: _AgentProducerState,
    source_factory,
    registry,
    lifecycle_token,
    request_id: str,
    session,
    entry: SessionHubEntry,
    previous_producer_token,
) -> None:
    """Own an accepted Answer Now replacement independently of its subscriber."""

    hub = session.event_hub
    source = None
    producer_token = None
    publisher: SessionProducerPublisher | None = None
    replacement_scope_ids: frozenset[str] = frozenset()
    ready_resolved = False
    failure: BaseException | None = None
    outcome = "error"
    source_error: BaseException | None = None
    source_done_payload: dict[str, Any] | None = None

    def _resolve_ready(error: BaseException | None) -> None:
        nonlocal ready_resolved
        if ready_resolved:
            return
        state.session = session
        state.hub = hub
        state.entry = entry
        state.error = error
        ready_resolved = True
        state.ready.set()

    def _publish(payload: dict[str, Any]) -> None:
        if publisher is None:
            raise RuntimeError("Answer Now publisher is unavailable")
        publisher.publish(payload)

    def _publish_source(payload: dict[str, Any]) -> None:
        nonlocal source_done_payload, source_error
        if payload.get("type") == "done":
            if source_done_payload is None:
                source_done_payload = dict(payload)
            return
        if payload.get("type") == "error":
            if source_error is None:
                source_error = RuntimeError(
                    str(payload.get("error") or "Answer Now source failed")
                )
            return
        _publish(payload)

    try:
        source = source_factory()
        first_event = await anext(source)
        first_payload = _decode_agent_sse_payload(first_event)
        replacement_scope_ids = session.capture_active_run_scopes()
        if first_payload.get("type") != "session_start" or not replacement_scope_ids:
            raise RuntimeError("Answer Now replacement did not register its run scope")

        producer_token = await hub.claim_producer_after(previous_producer_token)
        if producer_token is None:
            raise RuntimeError("Agent session already has an active producer")
        publisher = SessionProducerPublisher(
            entry=entry,
            request_id=request_id,
            producer_token=producer_token,
            request_lifecycle_registry=registry,
            reservation_token=lifecycle_token,
        )
        turn_index = getattr(session, "event_hub_turn_index", 0)
        if isinstance(turn_index, bool) or not isinstance(turn_index, int):
            turn_index = 0
        mode = getattr(session, "mode", "tool_calling")
        if not isinstance(mode, str):
            mode = "tool_calling"
        acceptance_metadata: dict[str, Any] = {}
        snapshot = getattr(session, "tool_configuration_snapshot", None)
        if snapshot is not None:
            acceptance_metadata["tool_configuration"] = snapshot.public_metadata(
                locked=True
            )
        publisher.accept(
            {
                "type": "timeline_event",
                "event": {
                    "kind": "turn_started",
                    "session_id": session.session_id,
                    "turn_index": turn_index,
                    "user_content": "",
                    "images": [],
                    "mode": mode,
                    "answer_now": True,
                },
            },
            acceptance_metadata,
        )
        _publish_source(first_payload)
        _resolve_ready(None)

        async for event in source:
            _publish_source(_decode_agent_sse_payload(event))
        source_status = (
            source_done_payload.get("status")
            if source_done_payload is not None
            else None
        )
        if source_error is not None or source_status == "error":
            outcome = "error"
        elif source_status == "aborted":
            outcome = "aborted"
        else:
            outcome = "completed"
    except StopAsyncIteration as exc:
        failure = RuntimeError("Answer Now replacement was not registered")
        failure.__cause__ = exc
        outcome = "error"
    except asyncio.CancelledError as exc:
        failure = exc
        outcome = "aborted"
        logger.warning("Answer Now producer task was cancelled request_id=%s", request_id)
    except BaseException as exc:
        failure = exc
        outcome = "error"
        logger.error(
            "Answer Now producer failed request_id=%s: %s",
            request_id,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    finally:
        if source is not None:
            close = getattr(source, "aclose", None)
            if close is not None:
                try:
                    await close()
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                        outcome = "error"

        try:
            if not replacement_scope_ids:
                replacement_scope_ids = session.capture_active_run_scopes()
            if replacement_scope_ids:
                waited = await asyncio.to_thread(
                    session.wait_for_run_scopes,
                    replacement_scope_ids,
                )
                if waited is False:
                    raise RuntimeError(
                        "Answer Now replacement run-scope cleanup was cancelled"
                    )
        except BaseException as exc:
            if failure is None:
                failure = exc
                outcome = "error"

        try:
            if publisher is not None and publisher.acceptance_published:
                try:
                    terminal_error = failure or source_error
                    if outcome == "error" and terminal_error is None:
                        terminal_error = RuntimeError("Answer Now source failed")
                    publisher.finish(
                        outcome,
                        error=terminal_error if outcome == "error" else None,
                    )
                except BaseException as exc:
                    logger.error(
                        "Answer Now terminalization failed request_id=%s: %s",
                        request_id,
                        exc,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
            else:
                startup_error = failure or RuntimeError(
                    "Answer Now producer failed before acceptance"
                )
                try:
                    if publisher is not None:
                        publisher.abandon_if_unaccepted()
                    else:
                        registry.abandon(lifecycle_token)
                except BaseException as exc:
                    logger.error(
                        "Answer Now startup registry cleanup failed request_id=%s: %s",
                        request_id,
                        exc,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                finally:
                    pass
        finally:
            try:
                session.finish_answer_now()
            finally:
                _resolve_ready(
                    None
                    if publisher is not None and publisher.acceptance_published
                    else startup_error
                )


def _start_answer_now_session_producer(**kwargs) -> asyncio.Task:
    task = asyncio.create_task(_run_answer_now_session_producer(**kwargs))
    _agent_producer_tasks.add(task)
    task.add_done_callback(_observe_agent_producer_task)
    return task


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
    context_messages = request.context_messages
    mode = request.mode
    enable_thinking = request.enable_thinking

    if not message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    if request.vision_automation is not None:
        binding = request.vision_automation.binding
        if binding.mode != "agent":
            raise HTTPException(
                status_code=422,
                detail="Agent vision automation binding mode must be agent",
            )
        if session_id is None or binding.top_level_context_id != session_id:
            raise HTTPException(
                status_code=422,
                detail="Agent vision automation top-level context mismatch",
            )

    registry = getattr(raw_request.app.state, "request_lifecycle_registry", None)
    if registry is None:
        if request.vision_automation is not None:
            raise HTTPException(
                status_code=503,
                detail="vision automation requires strict request lifecycle admission",
            )
        # Standalone router users still use the exact same Agent admission
        # path.  There is no lifecycle journal, but the tool snapshot must be
        # staged before the Agent is constructed and rolled back on failure.
        try:
            admission_stage = agent_runner.stage_agent_request_admission(
                session_id,
                mode,
                None,
                len(images or ()),
                tool_configuration=request.tool_configuration,
            )
        except AgentToolConfigurationError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message, "session_id": session_id},
            ) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            raise

        exact_session_id = admission_stage.session_id
        if request.stream:
            try:
                stream = agent_runner.run_stream(
                    exact_session_id,
                    message,
                    mode,
                    images=images,
                    context_messages=context_messages,
                    enable_thinking=enable_thinking,
                    enable_local_memory=request.enable_local_memory,
                    admission_stage=admission_stage,
                    on_run_registered=lambda _stage, _scope_ids: None,
                )
            except RuntimeError as exc:
                agent_runner.rollback_agent_request_stage(admission_stage)
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception:
                agent_runner.rollback_agent_request_stage(admission_stage)
                raise
            return _PrefetchedAgentStreamingResponse(
                stream,
                admission_stage=admission_stage,
                agent_runner=agent_runner,
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        try:
            result = await agent_runner.run_sync(
                exact_session_id,
                message,
                mode,
                images=images,
                context_messages=context_messages,
                enable_thinking=enable_thinking,
                enable_local_memory=request.enable_local_memory,
                admission_stage=admission_stage,
                on_run_registered=lambda _stage, _scope_ids: None,
            )
        except AgentToolConfigurationError as exc:
            agent_runner.rollback_agent_request_stage(admission_stage)
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message, "session_id": session_id},
            ) from exc
        except RuntimeError as exc:
            agent_runner.rollback_agent_request_stage(admission_stage)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            agent_runner.rollback_agent_request_stage(admission_stage)
            raise
        return JSONResponse(content=result)

    session_hub_runtime = getattr(
        raw_request.app.state,
        "session_hub_runtime",
        None,
    )
    if not isinstance(session_hub_runtime, SessionHubRuntime):
        raise HTTPException(
            status_code=503,
            detail="shared Session Hub runtime unavailable",
        )

    try:
        inbound_validation = validate_agent_vision_request(
            images,
            get_config(),
            metadata=request.vision_automation,
        )
    except VisionRequestValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    request_id = request.request_id or create_request_id()
    registry, lifecycle_token, duplicate_response = _reserve_agent_lifecycle(
        raw_request, request, request_id
    )
    if duplicate_response is not None:
        return duplicate_response

    admission_stage = None
    staged_admission = callable(
        getattr(type(agent_runner), "stage_agent_request_admission", None)
    )
    try:
        if staged_admission:
            admission_stage = agent_runner.stage_agent_request_admission(
                session_id,
                mode,
                request.vision_automation,
                len(images or ()),
                tool_configuration=request.tool_configuration,
            )
            historical_image_count = getattr(
                admission_stage, "projected_historical_image_count", None
            )
        else:
            historical_image_count = _top_level_agent_historical_image_count(
                agent_runner, session_id
            )
        validate_agent_final_image_count(
            inbound_validation.image_count,
            historical_image_count,
        )
    except VisionRequestValidationError as exc:
        if admission_stage is not None:
            agent_runner.rollback_agent_request_stage(admission_stage)
        registry.abandon(lifecycle_token)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except AgentToolConfigurationError as exc:
        if admission_stage is not None:
            agent_runner.rollback_agent_request_stage(admission_stage)
        registry.abandon(lifecycle_token)
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message, "session_id": session_id},
        ) from exc
    except RuntimeError as exc:
        if admission_stage is not None:
            agent_runner.rollback_agent_request_stage(admission_stage)
        registry.abandon(lifecycle_token)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        if admission_stage is not None:
            agent_runner.rollback_agent_request_stage(admission_stage)
        registry.abandon(lifecycle_token)
        raise

    initial_session = (
        getattr(admission_stage, "_session", None)
        if admission_stage is not None
        else None
    )
    if admission_stage is None:
        try:
            initial_session = agent_runner.get_or_create_session(session_id, mode)
        except RuntimeError as exc:
            registry.abandon(lifecycle_token)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            registry.abandon(lifecycle_token)
            raise

    if request.stream:
        def _source_factory(on_run_registered):
            run_kwargs = {
                "images": images,
                "context_messages": context_messages,
                "enable_thinking": enable_thinking,
                "enable_local_memory": request.enable_local_memory,
            }
            if admission_stage is not None:
                run_kwargs.update(
                    admission_stage=admission_stage,
                    on_run_registered=on_run_registered,
                )
            return agent_runner.run_stream(
                session_id, message, mode, **run_kwargs
            )

        # This is captured after validation/staging and immediately before the
        # background turn starts. The initiating POST must never replay earlier
        # turns already retained in this session journal.
        start_sequence = (
            initial_session.event_hub.last_sequence
            if initial_session is not None
            else 0
        )
        producer_state = _AgentProducerState(start_sequence)
        _start_agent_session_producer(
            state=producer_state,
            source_factory=_source_factory,
            registry=registry,
            lifecycle_token=lifecycle_token,
            request_id=request_id,
            agent_runner=agent_runner,
            admission_stage=admission_stage,
            initial_session=initial_session,
            session_hub_runtime=session_hub_runtime,
            message=message,
            images=images,
            vision_automation_metadata=request.vision_automation,
        )
        return _StartedAgentStreamingResponse(
            producer_state,
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        result_holder: dict[str, Any] = {}
        completion = asyncio.get_running_loop().create_future()

        def _source_factory(on_run_registered):
            run_kwargs = {
                "images": images,
                "context_messages": context_messages,
                "enable_thinking": enable_thinking,
                "enable_local_memory": request.enable_local_memory,
            }
            if admission_stage is not None:
                run_kwargs.update(
                    admission_stage=admission_stage,
                    on_run_registered=on_run_registered,
                )
            async def _source():
                result = await agent_runner.run_sync(
                    session_id,
                    message,
                    mode,
                    **run_kwargs,
                )
                result_holder["result"] = result
                yield "data: " + json.dumps(
                    {"type": "session_start", "session_id": result.get("session_id", session_id)},
                    ensure_ascii=False,
                ) + "\n\n"
                memory_snapshot = result.get("local_memory")
                if isinstance(memory_snapshot, dict) and memory_snapshot.get("chunks"):
                    yield "data: " + json.dumps({"type": "timeline_event", "event": {
                        "kind": "local_memory_retrieved", **memory_snapshot}},
                        ensure_ascii=False) + "\n\n"
                for step in result.get("steps", ()) or ():
                    if isinstance(step, dict):
                        step_payload = dict(step)
                        step_payload.setdefault("type", "agent_step")
                        yield "data: " + json.dumps(
                            step_payload,
                            ensure_ascii=False,
                        ) + "\n\n"
                yield "data: " + json.dumps(
                    {"type": "content", "content": str(result.get("answer") or "")},
                    ensure_ascii=False,
                ) + "\n\n"
                yield "data: " + json.dumps(
                    {"type": "done", "session_id": result.get("session_id", session_id)},
                    ensure_ascii=False,
                ) + "\n\n"
            return _source()

        start_sequence = (
            initial_session.event_hub.last_sequence
            if initial_session is not None
            else 0
        )
        producer_state = _AgentProducerState(start_sequence)
        _start_agent_session_producer(
            state=producer_state,
            source_factory=_source_factory,
            registry=registry,
            lifecycle_token=lifecycle_token,
            request_id=request_id,
            agent_runner=agent_runner,
            admission_stage=admission_stage,
            initial_session=initial_session,
            session_hub_runtime=session_hub_runtime,
            message=message,
            images=images,
            vision_automation_metadata=request.vision_automation,
            completion=completion,
            result_holder=result_holder,
        )
        try:
            result = await asyncio.shield(completion)
        except AgentToolConfigurationError as exc:
            # Prompt/catalog failures occur before request acceptance.  Keep
            # their stable domain code/status instead of collapsing them into
            # the generic session-conflict response.
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(content=result)


def _validate_agent_event_cursor(value: Any) -> int:
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


@router.get("/session/{session_id}/events")
async def agent_session_events(
    session_id: str,
    raw_request: Request,
    after_sequence: Any = Query(default="0"),
):
    """Replay and follow one recoverable top-level Agent session journal."""

    _require_valid_session_id(session_id)
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")
    cursor = _validate_agent_event_cursor(after_sequence)
    runtime = getattr(raw_request.app.state, "session_hub_runtime", None)
    entry = runtime.resolve(session_id) if isinstance(runtime, SessionHubRuntime) else None
    if entry is None or entry.kind != "agent" or entry.hub.released:
        raise HTTPException(
            status_code=404,
            detail="Agent session not found or expired",
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


@router.get("/child/stream")
async def child_agent_stream(child_run_id: str, raw_request: Request):
    """Replay and follow an existing child agent run."""
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    try:
        child_run = agent_runner.get_child_agent_run(child_run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Child agent run not found") from exc

    runtime = getattr(raw_request.app.state, "session_hub_runtime", None)
    entry = (
        runtime.resolve(child_run.child_run_id)
        if isinstance(runtime, SessionHubRuntime)
        else None
    )
    if entry is None or entry.kind != "agent" or entry.hub.released:
        raise HTTPException(status_code=404, detail="Child agent run not found")

    return StreamingResponse(
        subscribe_session_hub(entry, 0),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/vision-automation/abort")
async def register_vision_automation_abort(
    request: AgentVisionAutomationAbortRequest,
    raw_request: Request,
):
    """Register a hashed, one-shot post-Abort historical cleanup token."""

    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")
    try:
        cleanup_state = agent_runner.register_agent_vision_automation_cleanup(
            request.session_id,
            request.binding,
            request.cleanup_token,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent session not found") from exc
    except VisionRequestValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    status = (
        "cleanup_cancelled"
        if getattr(cleanup_state, "cancelled", False)
        else "cleanup_pending"
    )
    return {"status": status, "session_id": request.session_id}


@router.post("/vision-automation/abort/cancel")
async def cancel_vision_automation_abort(
    request: AgentVisionAutomationAbortRequest,
    raw_request: Request,
):
    """Cancel the exact unconsumed cleanup registration without touching memory."""

    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")
    try:
        agent_runner.cancel_agent_vision_automation_cleanup(
            request.session_id,
            request.binding,
            request.cleanup_token,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent session not found") from exc
    except VisionRequestValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "cleanup_cancelled", "session_id": request.session_id}


@router.post("/child/abort")
async def abort_child_agent(request: ChildAgentAbortRequest, raw_request: Request):
    """Abort only the requested child and dispose its context after cleanup."""
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")
    try:
        return await agent_runner.abort_child_agent_run(request.child_run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Child agent run not found") from exc


@router.delete("/session/{session_id}")
async def delete_session(session_id: str, raw_request: Request):
    """Target-abort and dispose one top-level Agent context safely."""
    _require_valid_session_id(session_id)
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    from ..agent.agent_runner import SessionDisposalError

    try:
        return await agent_runner.dispose_session(session_id)
    except SessionDisposalError as exc:
        raise HTTPException(status_code=503, detail=exc.as_detail()) from exc


@router.get("/sessions")
async def list_sessions(raw_request: Request):
    """获取活跃 session 数量"""
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    return {"active_sessions": agent_runner.sessions.active_count}


@router.get("/sessions/retained")
async def list_retained_sessions(raw_request: Request):
    """列出可恢复的顶层 Agent 会话，供前端跨页面关闭恢复入口使用。

    后端 Hub 是会话保留的持久真相源；localStorage 书签只是加速路径。
    子 agent 上下文（is_child_context）不是独立可恢复入口，必须排除。
    """
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    entries = []
    for _, session in agent_runner.sessions.exact_items():
        if session.is_child_context:
            continue
        entries.append(
            {
                "session_id": session.session_id,
                "created_at": session.created_at.isoformat(),
                "last_activity": session.last_activity.isoformat(),
                "run_in_progress": bool(session.run_in_progress),
            }
        )
    entries.sort(key=lambda item: (item["last_activity"], item["session_id"]), reverse=True)
    return {"sessions": entries}


class AgentStopRequest(BaseModel):
    """停止 Agent 请求"""

    session_id: str
    expected_request_id: str | None = None

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id_value(value)


def _sglang_url() -> str:
    from ..config import get_config

    config = get_config()
    return f"http://{config.sglang.host}:{config.sglang.port}"


def _abort_agent_session_request(
    agent_runner,
    session_id: str,
    *,
    captured_request_id: str | None = None,
    request_id_was_captured: bool = False,
    require_provider_abort: bool = False,
    expected_request_id: str | None = None,
) -> str | None:
    from ..llm_service import abort_request_by_id

    request_id = (
        captured_request_id
        if request_id_was_captured
        else agent_runner.get_current_request_id(session_id)
    )
    session = agent_runner.sessions.get(session_id) if expected_request_id is not None else None
    active_scopes = session.capture_active_run_scopes() if session is not None else frozenset()
    if require_provider_abort and request_id:
        if not abort_request_by_id(
            request_id,
            _sglang_url(),
            fallback_to_abort_all=False,
            require_provider_abort=True,
        ):
            raise RuntimeError("Targeted provider abort failed; retry Stop")
    if active_scopes and not active_scopes.intersection(session.capture_active_run_scopes()):
        # The old turn completed during the provider call. A new turn may now
        # own this session; never apply the old Stop to its run.
        return request_id
    next_request_id = agent_runner.get_current_request_id(session_id) if expected_request_id else None
    # Aborting can let the worker clear its active request registration before
    # this endpoint issues the targeted provider abort, so capture it first.
    agent_runner.abort_session(session_id)
    if next_request_id and next_request_id != request_id:
        if not abort_request_by_id(
            next_request_id,
            _sglang_url(),
            fallback_to_abort_all=False,
            require_provider_abort=True,
        ):
            raise RuntimeError("Targeted provider abort failed for next model step")
    if expected_request_id is not None:
        late_request_id = agent_runner.get_current_request_id(session_id)
        if (
            late_request_id
            and late_request_id not in {request_id, next_request_id}
            and (not active_scopes or active_scopes.intersection(session.capture_active_run_scopes()))
        ):
            if not abort_request_by_id(
                late_request_id,
                _sglang_url(),
                fallback_to_abort_all=False,
                require_provider_abort=True,
            ):
                raise RuntimeError("Targeted provider abort failed for late model step")
    if request_id and not require_provider_abort:
        abort_request_by_id(
            request_id,
            _sglang_url(),
            fallback_to_abort_all=False,
        )
    return request_id


@router.post("/stop")
async def stop_agent(request: AgentStopRequest, raw_request: Request):
    """停止正在进行的 Agent 执行

    优雅地中止 Agent，不会立即杀死进程，而是标记中止状态。
    同时关闭当前 provider 的上游请求；SGLang 额外调用 abort 接口，vLLM
    则通过关闭 OpenAI 流连接触发引擎取消。
    """
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    if request.expected_request_id is not None:
        registry = getattr(raw_request.app.state, "request_lifecycle_registry", None)
        if registry is None:
            raise HTTPException(status_code=503, detail="Agent request lifecycle unavailable")
        if not registry.is_active_request_for_context(
            request.expected_request_id, "agent", request.session_id
        ):
            raise HTTPException(status_code=409, detail="Expected request is no longer current")

    try:
        request_id = _abort_agent_session_request(
            agent_runner,
            request.session_id,
            require_provider_abort=True,
            expected_request_id=request.expected_request_id,
        )
    except Exception as e:
        logger.warning(f"[AGENT STOP] inference abort error: {e}")
        raise HTTPException(status_code=502, detail="Targeted provider abort failed; retry Stop") from e
    if not request_id:
        logger.info(
            f"[AGENT STOP] No active request_id for session={request.session_id}; relying on session abort only"
        )

    return {"status": "stopped", "session_id": request.session_id}


@router.post("/stop-all")
async def stop_all_agents(raw_request: Request):
    """停止所有正在进行的 Agent 执行

    不需要 session_id，直接中止所有运行中的请求。
    同时标记 SessionStore 中所有 session 为 aborted，避免旧状态泄漏到后续请求。
    """
    from ..llm_service import (
        abort_request_by_id,
        abort_sglang_all,
        active_request_ids,
        get_active_backend,
    )

    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    stopped_sessions = []
    for session_id in agent_runner.sessions.session_ids():
        agent_runner.abort_session(session_id)
        stopped_sessions.append(session_id)
    if stopped_sessions:
        logger.info(
            f"[AGENT STOP-ALL] Marked {len(stopped_sessions)} sessions as aborted: {stopped_sessions}"
        )

    sglang_url = _sglang_url()
    for request_id in active_request_ids():
        try:
            abort_request_by_id(
                request_id, sglang_url, fallback_to_abort_all=False
            )
        except Exception as e:
            logger.warning(
                f"[AGENT STOP-ALL] request abort error request_id={request_id}: {e}"
            )

    if get_active_backend() == "sglang":
        try:
            abort_sglang_all(sglang_url)
            logger.info("[AGENT STOP-ALL] SGLang abort_all completed")
        except Exception as e:
            logger.warning(f"[AGENT STOP-ALL] SGLang abort error: {e}")

    return {"status": "stopped_all", "stopped_sessions": stopped_sessions}


@_answer_now_router.post("/answer-now")
async def answer_now(request: AgentStopRequest, raw_request: Request):
    """停止当前 Agent 执行，并基于已截断历史快速生成最终答复。"""
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    acquisition_status, session = agent_runner.try_acquire_answer_now(
        request.session_id
    )
    if acquisition_status == "not_found":
        raise HTTPException(status_code=404, detail="Agent session not found")
    if acquisition_status != "acquired" or session is None:
        raise HTTPException(status_code=409, detail="Answer Now unavailable")
    try:
        captured_scope_ids = session.capture_active_run_scopes()
        captured_request_id = agent_runner.get_current_request_id(request.session_id)
        try:
            _abort_agent_session_request(
                agent_runner,
                request.session_id,
                captured_request_id=captured_request_id,
                request_id_was_captured=True,
            )
        except Exception as e:
            logger.warning(f"[AGENT ANSWER-NOW] inference abort error: {e}")

        cancellation = threading.Event()

        async def _watch_disconnect():
            while not cancellation.is_set():
                if await raw_request.is_disconnected():
                    cancellation.set()
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(_watch_disconnect())
        try:
            return await agent_runner.generate_answer_now_reply(
                request.session_id,
                captured_scope_ids=captured_scope_ids,
                single_flight_acquired=True,
                cancelled=cancellation.is_set,
            )
        finally:
            cancellation.set()
            watcher.cancel()
    finally:
        session.finish_answer_now()


@_answer_now_router.post("/answer-now/stream")
async def answer_now_stream(
    request: AgentAnswerNowStreamRequest,
    raw_request: Request,
):
    """停止当前 Agent 执行，并以 SSE 流式返回新的最终答复。"""
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")

    try:
        if not isinstance(request.request_id, str):
            raise ValueError("request_id")
        validate_automation_lifecycle_id(request.request_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="invalid request lifecycle metadata",
        ) from exc

    registry = getattr(raw_request.app.state, "request_lifecycle_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail="request lifecycle registry unavailable",
        )

    session = agent_runner.sessions.get(request.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    session_hub_runtime = getattr(
        raw_request.app.state,
        "session_hub_runtime",
        None,
    )
    entry = (
        session_hub_runtime.resolve(session.session_id)
        if isinstance(session_hub_runtime, SessionHubRuntime)
        else None
    )
    if entry is None or entry.kind != "agent" or entry.hub is not session.event_hub:
        raise HTTPException(
            status_code=503,
            detail="shared Session Hub runtime unavailable",
        )

    def _create_replacement_source():
        captured_scope_ids = session.capture_active_run_scopes()
        captured_request_id = agent_runner.get_current_request_id(request.session_id)
        try:
            _abort_agent_session_request(
                agent_runner,
                request.session_id,
                captured_request_id=captured_request_id,
                request_id_was_captured=True,
            )
        except Exception as e:
            logger.warning(f"[AGENT ANSWER-NOW/STREAM] inference abort error: {e}")
        return agent_runner.run_answer_now_stream(
            request.session_id,
            captured_scope_ids=captured_scope_ids,
            single_flight_acquired=True,
            cancelled=lambda: False,
        )

    response = _AnswerNowStreamingResponse(
        agent_runner,
        session,
        _create_replacement_source,
        registry=registry,
        request_id=request.request_id,
        payload_fingerprint=fingerprint_request_payload(
            {"session_id": request.session_id}
        ),
        is_disconnected=raw_request.is_disconnected,
        session_hub_runtime=session_hub_runtime,
    )
    response.headers.update({
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    return response
