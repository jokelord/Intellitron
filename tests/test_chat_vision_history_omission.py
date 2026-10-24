"""Chat Vision omission projection stays outside canonical model input."""

from __future__ import annotations

import base64
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from PIL import Image

from backend.api.chat import ChatCompletionRequest, create_chat_completion
from backend.api.chat_session_store import ChatSessionRecord
from backend.session_hub_runtime import subscribe_session_hub
from tests.test_chat_session_hub import (
    REQUEST_ID_1,
    REQUEST_ID_2,
    SESSION_ID,
    _ReadyService,
    _app,
    _automation_metadata,
    _decode_sse,
    _request,
    _source,
    _start,
    _start_real,
)


def _data_url() -> str:
    image = Image.new("RGB", (2, 2), color="blue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


@pytest.mark.asyncio
async def test_chat_hub_committed_vision_omission_is_projection_only():
    app, _registry, runtime, _store = _app()
    image = _data_url()
    initial_request = ChatCompletionRequest(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "historical frame"},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            }
        ],
        stream=True,
        request_id=REQUEST_ID_1,
        session_id=SESSION_ID,
    )
    initial = await _start(app, initial_request, _source())
    await initial.body_iterator.aclose()
    initial_record = _store.get(SESSION_ID)
    assert initial_record is not None
    await initial_record.produce_task

    model_messages = [
        {
            "role": "user",
            "content": (
                "[historical images omitted: count=1; sources=screen; "
                "relative_range=prior]"
            ),
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "current frame"},
                {"type": "image_url", "image_url": {"url": image}},
            ],
        },
    ]
    metadata = _automation_metadata()
    metadata["source_count"] = 1
    metadata["retained_tick_count"] = 1
    request = ChatCompletionRequest(
        messages=model_messages,
        stream=True,
        request_id=REQUEST_ID_2,
        session_id=SESSION_ID,
        vision_automation=metadata,
    )
    captured = {}

    def fake_generate_stream_response(**kwargs):
        captured["messages"] = kwargs["messages"]
        captured["original_chat_messages"] = kwargs["original_chat_messages"]
        return _source()

    with (
        patch("backend.api.chat.get_llm_service", return_value=_ReadyService()),
        patch(
            "backend.api.chat._generate_stream_response",
            new=fake_generate_stream_response,
        ),
    ):
        response = await create_chat_completion(request, _request(app))
    payloads = [_decode_sse(frame) async for frame in response.body_iterator]

    turn = payloads[1]["event"]
    assert turn["kind"] == "turn_started"
    assert turn["historical_image_omission"] is True
    assert turn["vision_automation_manual_image_count"] == 0
    assert turn["user_content"] == "current frame"
    entry = runtime.resolve(SESSION_ID)
    assert entry is not None
    assert "data:image" not in json.dumps(
        [record.payload for record in entry.hub._journal]
    )
    assert [
        message.model_dump() for message in captured["original_chat_messages"]
    ] == [message.model_dump() for message in request.messages]
    assert [message.content for message in captured["messages"]] == [
        message.content for message in request.messages
    ]
    assert "event_sequence" not in json.dumps(
        [message.model_dump() for message in captured["original_chat_messages"]]
    )
    recovered = [
        _decode_sse(frame)
        async for frame in subscribe_session_hub(entry, after_sequence=0)
    ]
    turns = [
        payload["event"]
        for payload in recovered
        if payload.get("event", {}).get("kind") == "turn_started"
    ]
    assert turns[0]["images"] == []
    assert "historical_image_placeholder" in turns[0]
    assert turns[1]["images"] == [image]


@pytest.mark.asyncio
async def test_chat_hub_preaccept_vision_rejection_has_no_turn_or_omission_projection():
    app, registry, runtime, store = _app()
    metadata = _automation_metadata()
    metadata["source_count"] = 1
    metadata["retained_tick_count"] = 1
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "no actual image"}],
        stream=True,
        request_id=REQUEST_ID_1,
        session_id=SESSION_ID,
        vision_automation=metadata,
    )

    with patch("backend.api.chat.get_llm_service", return_value=_ReadyService()):
        with pytest.raises(HTTPException) as exc_info:
            await create_chat_completion(request, _request(app))

    assert exc_info.value.status_code == 422
    assert runtime.resolve(SESSION_ID) is None
    assert store.get(SESSION_ID) is None
    assert registry.get_status(REQUEST_ID_1) == "unknown"


@pytest.mark.asyncio
async def test_chat_hub_real_provider_receives_exact_final_vision_history():
    app, _registry, _runtime, _store = _app()
    image = _data_url()
    placeholder = (
        "[historical images omitted: count=1; sources=screen; "
        "relative_range=prior]"
    )
    metadata = _automation_metadata()
    metadata["source_count"] = 1
    metadata["retained_tick_count"] = 1
    request = ChatCompletionRequest(
        messages=[
            {"role": "user", "content": placeholder},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "current frame"},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            },
        ],
        stream=True,
        request_id=REQUEST_ID_1,
        session_id=SESSION_ID,
        vision_automation=metadata,
    )
    original_dump = [message.model_dump() for message in request.messages]
    captured = {}

    class Service:
        is_ready = True
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=4096))

        async def chat_stream_async(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            yield {"content": "answer"}

    response = await _start_real(app, request, Service())
    await response.body_iterator.aclose()
    await _store.get(SESSION_ID).produce_task

    provider_messages = captured["messages"]
    assert [message.role for message in provider_messages] == ["user", "user"]
    assert provider_messages[0].content == placeholder
    assert provider_messages[1].content == request.messages[1].content
    assert [message.model_dump() for message in request.messages] == original_dump
    assert "event_sequence" not in json.dumps(
        [message.content for message in provider_messages]
    )


@pytest.mark.asyncio
async def test_chat_hub_stale_preaccept_vision_turn_restores_prior_assets_exactly():
    app, _registry, _runtime, store = _app()
    image = _data_url()
    first = ChatCompletionRequest(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            }
        ],
        stream=True,
        request_id=REQUEST_ID_1,
        session_id=SESSION_ID,
    )
    first_response = await _start(app, first, _source())
    await first_response.body_iterator.aclose()
    record = store.get(SESSION_ID)
    await record.produce_task
    prior_assets = dict(record._display_assets)

    metadata = _automation_metadata()
    metadata["source_count"] = 1
    metadata["retained_tick_count"] = 1
    second = ChatCompletionRequest(
        messages=[
            {"role": "user", "content": "historical placeholder"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "second"},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            },
        ],
        stream=True,
        request_id=REQUEST_ID_2,
        session_id=SESSION_ID,
        vision_automation=metadata,
    )
    second_response = await _start(app, second, _source())
    assert record._display_assets == prior_assets
    old_token = record.hub.capture_active_producer()
    assert record.hub.release_producer(old_token) is True
    replacement = record.hub.claim_producer()
    await record.produce_task

    assert record._display_assets == prior_assets
    assert record.hub.capture_active_producer() is replacement
    await second_response.body_iterator.aclose()


def test_chat_display_asset_acceptance_hides_preaccept_omission_from_concurrent_resolver():
    record = ChatSessionRecord(session_id=SESSION_ID)
    prior_image = "data:image/png;base64,prior"
    next_image = "data:image/png;base64,next"
    prior = record.stage_display_assets(
        turn_index=1,
        images=[prior_image],
        evict_prior=False,
    )
    record.commit_display_assets(prior)
    prior_payload = {
        "type": "timeline_event",
        "event": {
            "kind": "turn_started",
            "image_asset_ids": prior.asset_ids,
        },
    }
    staged = record.stage_display_assets(
        turn_index=2,
        images=[next_image],
        evict_prior=True,
    )
    accept_entered = threading.Event()
    allow_accept_failure = threading.Event()

    def failing_accept():
        accept_entered.set()
        assert allow_accept_failure.wait(timeout=2)
        raise RuntimeError("preaccept failure")

    acceptance_errors = []
    resolved_payloads = []
    resolver_finished = threading.Event()

    def run_acceptance():
        try:
            record.accept_display_assets(staged, failing_accept, lambda: False)
        except BaseException as exc:
            acceptance_errors.append(exc)

    def run_resolver():
        resolved_payloads.append(record.resolve_payload(prior_payload))
        resolver_finished.set()

    acceptance = threading.Thread(target=run_acceptance)
    resolver = threading.Thread(target=run_resolver)
    acceptance.start()
    try:
        assert accept_entered.wait(timeout=2)
        resolver.start()
        assert resolver_finished.wait(timeout=0.05) is False
    finally:
        allow_accept_failure.set()
        acceptance.join(timeout=2)
        if resolver.ident is not None:
            resolver.join(timeout=2)

    assert acceptance.is_alive() is False
    assert resolver.is_alive() is False
    assert len(acceptance_errors) == 1
    assert isinstance(acceptance_errors[0], RuntimeError)
    assert str(acceptance_errors[0]) == "preaccept failure"
    resolved = resolved_payloads[0]

    assert resolved["event"]["images"] == [prior_image]
    assert "historical_image_placeholder" not in resolved["event"]


def test_chat_display_asset_stale_commit_and_rollback_cannot_touch_replacement():
    record = ChatSessionRecord(session_id=SESSION_ID)
    base = record.stage_display_assets(
        turn_index=1,
        images=["data:image/png;base64,base"],
        evict_prior=False,
    )
    record.commit_display_assets(base)
    stale = record.stage_display_assets(
        turn_index=2,
        images=["data:image/png;base64,stale"],
        evict_prior=True,
    )
    replacement = record.stage_display_assets(
        turn_index=3,
        images=["data:image/png;base64,replacement"],
        evict_prior=True,
    )
    record.commit_display_assets(replacement)
    replacement_state = dict(record._display_assets)

    with pytest.raises(RuntimeError, match="stale display asset generation"):
        record.commit_display_assets(stale)
    assert record.rollback_display_assets(stale) is False
    assert record._display_assets == replacement_state


@pytest.mark.asyncio
async def test_chat_display_assets_remain_committed_after_publisher_compensates_acceptance():
    app, registry, _runtime, store = _app()
    prior_image = _data_url()
    first = ChatCompletionRequest(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "prior frame"},
                    {"type": "image_url", "image_url": {"url": prior_image}},
                ],
            }
        ],
        stream=True,
        request_id=REQUEST_ID_1,
        session_id=SESSION_ID,
    )
    first_response = await _start(app, first, _source())
    await first_response.body_iterator.aclose()
    record = store.get(SESSION_ID)
    await record.produce_task
    prior_turn = next(
        item.payload
        for item in record.hub._journal
        if item.payload.get("event", {}).get("kind") == "turn_started"
    )
    start_sequence = record.hub.last_sequence

    metadata = _automation_metadata()
    metadata["source_count"] = 1
    metadata["retained_tick_count"] = 1
    current_image = _data_url()
    second = ChatCompletionRequest(
        messages=[
            {"role": "user", "content": "historical placeholder"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "current frame"},
                    {"type": "image_url", "image_url": {"url": current_image}},
                ],
            },
        ],
        stream=True,
        request_id=REQUEST_ID_2,
        session_id=SESSION_ID,
        vision_automation=metadata,
    )
    real_append_batch = record.hub.append_batch
    append_attempts = 0

    def fail_once_then_append(*args, **kwargs):
        nonlocal append_attempts
        append_attempts += 1
        if append_attempts == 1:
            raise RuntimeError("one-shot acceptance append failure")
        return real_append_batch(*args, **kwargs)

    record.hub.append_batch = fail_once_then_append
    second_response = await _start(app, second, _source())
    await record.produce_task
    await second_response.body_iterator.aclose()

    second_payloads = [
        item.payload for item in record.hub._journal if item.sequence > start_sequence
    ]
    assert [payload["type"] for payload in second_payloads] == [
        "request_accepted",
        "timeline_event",
        "error",
        "done",
    ]
    assert second_payloads[1]["event"]["kind"] == "turn_started"
    assert second_payloads[-1]["status"] == "error"
    assert registry.get_status(REQUEST_ID_2) == "terminal"
    assert registry._records[REQUEST_ID_2].outcome == "error"

    resolved_current = record.resolve_payload(second_payloads[1])
    resolved_prior = record.resolve_payload(prior_turn)
    assert resolved_current["event"]["images"] == [current_image]
    assert resolved_prior["event"]["images"] == []
    assert "historical_image_placeholder" in resolved_prior["event"]
