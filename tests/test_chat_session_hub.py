"""Canonical Session Hub behavior for ordinary Chat turns."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from backend.api.chat import ChatCompletionRequest, create_chat_completion
from backend.api.chat_session_store import ChatSessionStore
from backend.compact.types import CompactEvent
from backend.request_lifecycle import RequestLifecycleRegistry
from backend.session_hub_runtime import SessionHubRuntime, subscribe_session_hub
from backend.vision.contracts import fingerprint_request_payload


REQUEST_ID_1 = "018f6f44-75f4-7def-b746-01a31bc3c201"
REQUEST_ID_2 = "018f6f44-75f4-7def-b746-01a31bc3c202"
SESSION_ID = "chat-session-stable"


class _ReadyService:
    is_ready = True


def _request(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
            "app": app,
        }
    )


def _app() -> tuple[FastAPI, RequestLifecycleRegistry, SessionHubRuntime, ChatSessionStore]:
    app = FastAPI()
    registry = RequestLifecycleRegistry()
    runtime = SessionHubRuntime()
    store = ChatSessionStore()
    app.state.request_lifecycle_registry = registry
    app.state.session_hub_runtime = runtime
    app.state.chat_session_store = store
    app.state.compact_orchestrator = None
    return app, registry, runtime, store


def _automation_metadata(session_id: str = SESSION_ID) -> dict:
    return {
        "history_image_policy": "aggregate_prior_image_blocks",
        "manual_image_count": 0,
        "source_count": 0,
        "retained_tick_count": 0,
        "binding": {
            "page_generation": 7,
            "mode": "chat",
            "top_level_context_id": session_id,
            "automation_task_id": "task_01J0123456789ABCDEFGH",
            "capture_cycle_id": "cycle_01J0123456789ABCDEFG",
        },
    }


def _chat_request(
    *, request_id: str = REQUEST_ID_1, session_id: str = SESSION_ID
) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="test-model",
        messages=[{"role": "user", "content": "visible user turn"}],
        stream=True,
        request_id=request_id,
        session_id=session_id,
        vision_automation=_automation_metadata(session_id),
    )


def _source(*frames: dict, gate: asyncio.Event | None = None) -> AsyncIterator[str]:
    async def generate() -> AsyncIterator[str]:
        if gate is not None:
            await gate.wait()
        for frame in frames:
            yield f"data: {json.dumps(frame)}\n\n"
        yield "data: [DONE]\n\n"

    return generate()


def _delta(*, content: str | None = None, reasoning: str | None = None) -> dict:
    value: dict[str, str] = {}
    if content is not None:
        value["content"] = content
    if reasoning is not None:
        value["reasoning_content"] = reasoning
    return {"choices": [{"delta": value}]}


def _decode_sse(frame: str | bytes) -> dict:
    if isinstance(frame, bytes):
        frame = frame.decode()
    assert frame.startswith("data: ")
    return json.loads(frame[6:])


async def _start(
    app: FastAPI,
    request: ChatCompletionRequest,
    source: AsyncIterator[str],
):
    def fake_generate_stream_response(**_kwargs):
        return source

    with (
        patch("backend.api.chat.get_llm_service", return_value=_ReadyService()),
        patch(
            "backend.api.chat._generate_stream_response",
            new=fake_generate_stream_response,
        ),
    ):
        return await create_chat_completion(request, _request(app))


async def _start_real(
    app: FastAPI,
    request: ChatCompletionRequest,
    service,
):
    with patch("backend.api.chat.get_llm_service", return_value=service):
        return await create_chat_completion(request, _request(app))


def _reservation_was_abandoned(
    registry: RequestLifecycleRegistry,
    request: ChatCompletionRequest,
) -> bool:
    replacement = registry.reserve(
        request_id=request.request_id,
        request_kind="chat",
        context_identity=request.session_id,
        payload_fingerprint=fingerprint_request_payload(
            request.model_dump(mode="json", exclude={"request_id"})
        ),
    )
    return replacement.should_start


@pytest.mark.asyncio
async def test_chat_hub_vision_acceptance_is_first_canonical_record():
    app, registry, runtime, _store = _app()
    response = await _start(
        app,
        _chat_request(),
        _source(_delta(content="answer")),
    )

    payloads = [_decode_sse(frame) async for frame in response.body_iterator]

    assert payloads[0] == {
        "event_sequence": 1,
        "type": "request_accepted",
        "request_id": REQUEST_ID_1,
        "session_id": SESSION_ID,
        "status": "accepted",
    }
    assert payloads[1]["type"] == "timeline_event"
    assert payloads[1]["event"]["kind"] == "turn_started"
    assert payloads[1]["event"]["session_id"] == SESSION_ID
    delta_index = next(
        index
        for index, payload in enumerate(payloads)
        if payload.get("event", {}).get("kind") == "message_delta"
    )
    assert delta_index > 1
    assert registry.get_status(REQUEST_ID_1) == "terminal"
    assert runtime.resolve(SESSION_ID) is not None


@pytest.mark.asyncio
async def test_chat_hub_detach_keeps_producer_and_delta_only_recovery():
    app, registry, runtime, store = _app()
    gate = asyncio.Event()
    response = await _start(
        app,
        _chat_request(),
        _source(
            _delta(content="a"),
            _delta(content="b"),
            _delta(content="c"),
            _delta(reasoning="r1"),
            _delta(reasoning="r2"),
            gate=gate,
        ),
    )
    iterator = response.body_iterator

    accepted = _decode_sse(await anext(iterator))
    assert accepted["type"] == "request_accepted"
    await iterator.aclose()
    record = store.get(SESSION_ID)
    assert record is not None
    assert record.hub.subscriber_count == 0
    assert record.hub.has_active_producer is True

    gate.set()
    await asyncio.wait_for(record.produce_task, timeout=2)

    assert registry.get_status(REQUEST_ID_1) == "terminal"
    assert record.hub.has_active_producer is False
    entry = runtime.resolve(SESSION_ID)
    assert entry is record.entry
    recovered = [
        _decode_sse(frame)
        async for frame in subscribe_session_hub(entry, after_sequence=0)
    ]
    payload_types = [payload["type"] for payload in recovered]
    assert "wire" not in payload_types
    deltas = [
        payload["event"]
        for payload in recovered
        if payload.get("event", {}).get("kind") == "message_delta"
    ]
    assert [event["text"] for event in deltas] == ["a", "b", "c", "r1", "r2"]
    assert [event["reasoning"] for event in deltas] == [False, False, False, True, True]
    assert all(event["source"] == "chat" for event in deltas)
    assert len({event["message_id"] for event in deltas}) == 1
    assert not any(
        event["text"] in {"ab", "abc", "r1r2"} for event in deltas
    )
    assert recovered[-1] == {
        "event_sequence": len(recovered),
        "type": "done",
        "session_id": SESSION_ID,
        "request_id": REQUEST_ID_1,
        "status": "completed",
    }


@pytest.mark.asyncio
async def test_chat_hub_reuses_session_journal_for_sequential_turns():
    app, _registry, runtime, store = _app()
    first = await _start(
        app,
        _chat_request(request_id=REQUEST_ID_1),
        _source(_delta(content="first")),
    )
    first_payloads = [_decode_sse(frame) async for frame in first.body_iterator]
    record = store.get(SESSION_ID)
    assert record is not None
    first_entry = record.entry
    first_last_sequence = record.hub.last_sequence

    second = await _start(
        app,
        ChatCompletionRequest(
            model="test-model",
            messages=[
                {"role": "user", "content": "visible user turn"},
                {"role": "assistant", "content": "first"},
                {"role": "user", "content": "second visible turn"},
            ],
            stream=True,
            request_id=REQUEST_ID_2,
            session_id=SESSION_ID,
        ),
        _source(_delta(content="second")),
    )
    second_payloads = [_decode_sse(frame) async for frame in second.body_iterator]

    assert store.get(SESSION_ID) is record
    assert runtime.resolve(SESSION_ID) is first_entry
    assert first_payloads[1]["event"]["turn_index"] == 1
    assert second_payloads[0]["event_sequence"] == first_last_sequence + 1
    assert second_payloads[1]["event"]["turn_index"] == 2
    assert second_payloads[1]["event"]["user_content"] == "second visible turn"
    assert second_payloads[1]["event"]["message_id"] != first_payloads[1]["event"]["message_id"]


@pytest.mark.asyncio
async def test_chat_hub_rejects_concurrent_turn_before_acceptance():
    app, registry, _runtime, store = _app()
    gate = asyncio.Event()
    first = await _start(
        app,
        _chat_request(request_id=REQUEST_ID_1),
        _source(_delta(content="first"), gate=gate),
    )
    assert _decode_sse(await anext(first.body_iterator))["type"] == "request_accepted"
    record = store.get(SESSION_ID)
    assert record is not None
    accepted_count = sum(
        item.payload["type"] == "request_accepted" for item in record.hub._journal
    )

    with pytest.raises(HTTPException) as exc_info:
        await _start(
            app,
            _chat_request(request_id=REQUEST_ID_2),
            _source(_delta(content="must-not-start")),
        )

    assert exc_info.value.status_code == 409
    assert registry.get_status(REQUEST_ID_2) == "unknown"
    assert sum(
        item.payload["type"] == "request_accepted" for item in record.hub._journal
    ) == accepted_count
    gate.set()
    await asyncio.wait_for(record.produce_task, timeout=2)
    await first.body_iterator.aclose()


def test_chat_hub_request_uses_one_canonical_session_identity():
    canonical = ChatCompletionRequest(messages=[], session_id="s")
    legacy = ChatCompletionRequest(messages=[], chat_session_id="s")

    assert canonical.session_id == "s"
    assert legacy.session_id == "s"
    assert "chat_session_id" not in legacy.model_dump()
    with pytest.raises(ValueError, match="session_id"):
        ChatCompletionRequest(
            messages=[], session_id="canonical", chat_session_id="different"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_mode", "expected_status", "expected_error_count"),
    [
        ("abort", "aborted", 0),
        ("error", "error", 1),
    ],
)
async def test_chat_hub_real_generator_preserves_provider_terminal_kind(
    provider_mode,
    expected_status,
    expected_error_count,
):
    app, registry, _runtime, store = _app()

    class Service:
        is_ready = True
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=4096))

        async def chat_stream_async(self, **_kwargs):
            if provider_mode == "abort":
                yield {"aborted": True}
                return
            raise RuntimeError("provider exploded")
            yield

    response = await _start_real(app, _chat_request(), Service())
    payloads = [_decode_sse(frame) async for frame in response.body_iterator]

    assert payloads[-1]["type"] == "done"
    assert payloads[-1]["status"] == expected_status
    assert sum(payload["type"] == "error" for payload in payloads) == expected_error_count
    assert registry.get_status(REQUEST_ID_1) == "terminal"
    assert store.get(SESSION_ID).finished is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_mode", "expected_outcome"),
    [("abort", "aborted"), ("error", "error")],
)
async def test_chat_legacy_stream_real_generator_preserves_terminal_outcome(
    provider_mode,
    expected_outcome,
):
    class RecordingRegistry(RequestLifecycleRegistry):
        def __init__(self):
            super().__init__()
            self.terminal_outcomes = []

        def mark_terminal(self, reservation_token, outcome):
            self.terminal_outcomes.append(outcome)
            return super().mark_terminal(reservation_token, outcome)

    app, _registry, _runtime, _store = _app()
    registry = RecordingRegistry()
    app.state.request_lifecycle_registry = registry

    class Service:
        is_ready = True
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=4096))

        async def chat_stream_async(self, **_kwargs):
            if provider_mode == "abort":
                yield {"aborted": True}
                return
            raise RuntimeError("legacy provider exploded")
            yield

    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "legacy stream"}],
        stream=True,
        request_id=REQUEST_ID_1,
    )
    response = await _start_real(app, request, Service())
    frames = [frame async for frame in response.body_iterator]

    assert frames[-1] == "data: [DONE]\n\n"
    assert registry.terminal_outcomes == [expected_outcome]
    assert registry.get_status(REQUEST_ID_1) == "terminal"


@pytest.mark.asyncio
async def test_chat_hub_real_generator_journals_auto_compact_projection_events():
    app, _registry, _runtime, store = _app()

    class Service:
        is_ready = True
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=4096))

        async def chat_stream_async(self, **_kwargs):
            yield {"content": "answer"}

    class Orchestrator:
        async def run_compact(self, _request):
            yield CompactEvent.started("summary-1", "auto", "chat")
            yield CompactEvent.delta("summary-1", "summary delta")
            yield CompactEvent.completed("summary-1", "summary")

    app.state.compact_orchestrator = Orchestrator()
    request = ChatCompletionRequest(
        messages=[
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "current user"},
        ],
        stream=True,
        request_id=REQUEST_ID_1,
        session_id=SESSION_ID,
    )
    with patch("backend.api.chat._should_auto_compact_chat", return_value=True):
        response = await _start_real(app, request, Service())
        await response.body_iterator.aclose()
        record = store.get(SESSION_ID)
        await record.produce_task

    kinds = [
        payload["event"]["kind"]
        for payload in (item.payload for item in record.hub._journal)
        if payload.get("type") == "timeline_event"
        and isinstance(payload.get("event"), dict)
    ]
    assert kinds[:4] == [
        "turn_started",
        "compact_started",
        "compact_delta",
        "compact_completed",
    ]
    assert "message_delta" in kinds


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["released", "stale"])
async def test_chat_hub_preaccept_owner_failure_abandons_exact_lifecycle_reservation(
    failure_mode,
):
    app, registry, _runtime, store = _app()
    request = _chat_request()
    response = await _start(app, request, _source(_delta(content="never")))
    record = store.get(SESSION_ID)
    original_token = record.hub.capture_active_producer()
    replacement_token = None
    if failure_mode == "released":
        record.hub.release()
    else:
        assert record.hub.release_producer(original_token) is True
        replacement_token = record.hub.claim_producer()

    await record.produce_task

    assert _reservation_was_abandoned(registry, request) is True
    if replacement_token is not None:
        assert record.hub.capture_active_producer() is replacement_token
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_chat_hub_old_completion_cannot_finish_replacement_turn():
    app, _registry, _runtime, store = _app()
    response = await _start(
        app,
        _chat_request(),
        _source(_delta(content="first")),
    )
    record = store.get(SESSION_ID)
    original_mark = store.mark_finished_if_same
    replacement = {}

    def interleaving_mark(target, reason, *args, **kwargs):
        token = target.hub.claim_producer()
        assert token is not None
        replacement["token"] = token
        replacement["turn_index"] = target.begin_turn(request_id=REQUEST_ID_2)
        return original_mark(target, reason, *args, **kwargs)

    store.mark_finished_if_same = interleaving_mark
    await record.produce_task

    assert record.request_id == REQUEST_ID_2
    assert record.turn_index == replacement["turn_index"]
    assert record.finished is False
    assert record.hub.capture_active_producer() is replacement["token"]
    await response.body_iterator.aclose()


def test_chat_hub_store_sweep_never_releases_active_producer():
    runtime = SessionHubRuntime()
    store = ChatSessionStore(ttl_seconds=0, max_sessions=1)
    active = store.get_or_create("active-session", runtime=runtime)
    token = active.hub.claim_producer()
    active.finished = True
    active.last_activity = 0

    store.get_or_create("other-session", runtime=runtime)

    assert runtime.resolve("active-session") is active.entry
    assert active.hub.capture_active_producer() is token


@pytest.mark.asyncio
async def test_chat_hub_session_nonstream_interaction_is_recoverable():
    app, registry, runtime, store = _app()

    class Result:
        text = "nonstream answer"
        reasoning_content = "nonstream reasoning"
        finish_reason = "stop"
        generation_time = 0.01
        usage = {}

    class Service:
        is_ready = True

        async def chat_async(self, **_kwargs):
            return Result()

    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "nonstream user"}],
        stream=False,
        request_id=REQUEST_ID_1,
        session_id=SESSION_ID,
    )
    response = await _start_real(app, request, Service())
    entry = runtime.resolve(SESSION_ID)

    assert response.choices[0].message.content == "nonstream answer"
    assert entry is store.get(SESSION_ID).entry
    payloads = [
        _decode_sse(frame)
        async for frame in subscribe_session_hub(entry, after_sequence=0)
    ]
    assert payloads[0]["type"] == "request_accepted"
    assert payloads[1]["event"]["kind"] == "turn_started"
    assert [payload["event"]["text"] for payload in payloads if payload.get("event", {}).get("kind") == "message_delta"] == [
        "nonstream reasoning",
        "nonstream answer",
    ]
    assert payloads[-1]["status"] == "completed"
    assert registry.get_status(REQUEST_ID_1) == "terminal"


@pytest.mark.asyncio
async def test_chat_hub_legacy_recovery_route_is_thin_shared_subscriber_adapter():
    from backend.api.chat import chat_session_events_endpoint

    app, _registry, runtime, _store = _app()
    entry = runtime.register(
        session_id=SESSION_ID,
        kind="chat",
        hub=__import__(
            "backend.agent.session_hub",
            fromlist=["AgentSessionHub"],
        ).AgentSessionHub(),
    )
    called = []

    async def shared_subscriber(actual_entry, cursor):
        called.append((actual_entry, cursor))
        yield 'data: {"type":"done","event_sequence":1}\n\n'

    with patch("backend.api.chat.subscribe_session_hub", new=shared_subscriber):
        response = await chat_session_events_endpoint(
            SESSION_ID,
            _request(app),
            after_sequence="0",
        )
        frames = [frame async for frame in response.body_iterator]

    assert called == [(entry, 0)]
    assert frames == ['data: {"type":"done","event_sequence":1}\n\n']
