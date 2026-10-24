import asyncio
import base64
import hashlib
import io
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from backend.api.agent import AgentChatRequest, router as agent_router
from backend.api.chat import _generate_stream_response, router as chat_router
from backend.config import get_config
from backend.llm_service import (
    GenerationResult,
    LLMService,
    Message,
    OpenAICompatibleProxyProvider,
    SGLangProvider,
    VLLMProvider,
)
from backend.request_lifecycle import RequestLifecycleRegistry
from backend.vision.contracts import (
    VisionAutomationBinding,
    VisionAutomationTransportMetadata,
    fingerprint_request_payload,
)
from backend.vision.validation import VisionRequestValidationError


def _data_url() -> str:
    image = Image.new("RGB", (2, 2), color="blue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


class _RecordingRegistry(RequestLifecycleRegistry):
    def __init__(self, events, **kwargs):
        super().__init__(**kwargs)
        self.events = events

    def reserve(self, **kwargs):
        self.events.append("reserved")
        return super().reserve(**kwargs)

    def accept(self, token):
        self.events.append("accepted")
        return super().accept(token)

    def mark_running(self, token):
        self.events.append("running")
        return super().mark_running(token)

    def mark_terminal(self, token, outcome):
        self.events.append(f"terminal:{outcome}")
        return super().mark_terminal(token, outcome)


def _agent_app(registry, runner):
    app = FastAPI()
    app.include_router(agent_router)
    app.state.agent_runner = runner
    app.state.request_lifecycle_registry = registry
    return app


def _chat_app(registry):
    app = FastAPI()
    app.include_router(chat_router)
    app.state.request_lifecycle_registry = registry
    app.state.compact_orchestrator = None
    return app


def test_agent_stream_accepts_after_run_registration_and_terminals_after_cleanup():
    events = []
    registry = _RecordingRegistry(events)
    runner = MagicMock()
    runner.get_or_create_session.return_value = MagicMock(session_id="agent-session-1")

    async def stream():
        events.append("run_registered")
        try:
            yield 'data: {"type":"session_start","session_id":"agent-session-1"}\n\n'
            yield 'data: {"type":"done","session_id":"agent-session-1"}\n\n'
        finally:
            events.append("run_cleanup")

    runner.run_stream.return_value = stream()
    request_id = "018f6f44-75f4-7def-b746-01a31bc3c001"

    with TestClient(_agent_app(registry, runner)).stream(
        "POST",
        "/v1/agent/chat",
        json={
            "request_id": request_id,
            "session_id": "agent-session-1",
            "message": "inspect",
            "images": [_data_url()],
            "stream": True,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"type": "request_accepted"' in body
    assert events.index("run_registered") < events.index("accepted")
    assert events.index("accepted") < events.index("run_cleanup")
    assert events.index("run_cleanup") < events.index("terminal:completed")
    assert registry.get_status(request_id) == "terminal"


def test_duplicate_agent_request_does_not_start_second_worker():
    registry = RequestLifecycleRegistry()
    runner = MagicMock()
    runner.get_or_create_session.return_value = MagicMock(session_id="agent-session-1")

    async def stream():
        yield 'data: {"type":"session_start","session_id":"agent-session-1"}\n\n'
        yield 'data: {"type":"done","session_id":"agent-session-1"}\n\n'

    runner.run_stream.side_effect = lambda *args, **kwargs: stream()
    app = _agent_app(registry, runner)
    body = {
        "request_id": "018f6f44-75f4-7def-b746-01a31bc3c002",
        "session_id": "agent-session-1",
        "message": "inspect",
        "stream": True,
    }
    client = TestClient(app)
    first = client.post("/v1/agent/chat", json=body)
    second = client.post("/v1/agent/chat", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "terminal"
    assert runner.run_stream.call_count == 1


@pytest.mark.parametrize(
    ("lifecycle_state", "expected_status", "request_suffix"),
    [
        ("reserved", "unknown", "21"),
        ("accepted", "accepted", "22"),
        ("running", "running", "23"),
    ],
)
def test_duplicate_agent_status_is_returned_before_busy_staging(
    lifecycle_state, expected_status, request_suffix
):
    body = {
        "request_id": f"018f6f44-75f4-7def-b746-01a31bc3d0{request_suffix}",
        "session_id": "busy-agent-session",
        "message": "inspect",
        "stream": True,
    }
    parsed = AgentChatRequest(**body)
    registry = RequestLifecycleRegistry()
    reservation = registry.reserve(
        request_id=parsed.request_id,
        request_kind="agent",
        context_identity=parsed.session_id,
        payload_fingerprint=fingerprint_request_payload(
            parsed.model_dump(mode="json", exclude={"request_id"})
        ),
    )
    if lifecycle_state in {"accepted", "running"}:
        registry.accept(reservation.registration_token)
    if lifecycle_state == "running":
        registry.mark_running(reservation.registration_token)

    class BusyRunner(_BoundaryRunner):
        def stage_agent_request_admission(self, *args, **kwargs):
            raise AssertionError("duplicate request must not stage the busy session")

    runner = BusyRunner([])
    response = TestClient(_agent_app(registry, runner)).post(
        "/v1/agent/chat", json=body
    )

    assert response.status_code == 200
    assert response.json() == {
        "request_id": body["request_id"],
        "status": expected_status,
    }
    assert runner.events == []


def test_matching_duplicate_still_validates_inbound_images_before_recognition():
    body = {
        "request_id": "018f6f44-75f4-7def-b746-01a31bc3d100",
        "session_id": "busy-agent-session",
        "message": "inspect",
        "images": ["data:image/png;base64,%%%"],
        "stream": True,
    }
    parsed = AgentChatRequest(**body)
    registry = RequestLifecycleRegistry()
    registry.reserve(
        request_id=parsed.request_id,
        request_kind="agent",
        context_identity=parsed.session_id,
        payload_fingerprint=fingerprint_request_payload(
            parsed.model_dump(mode="json", exclude={"request_id"})
        ),
    )
    runner = _BoundaryRunner([])

    response = TestClient(_agent_app(registry, runner)).post(
        "/v1/agent/chat", json=body
    )

    assert response.status_code == 422
    assert runner.events == []


def test_agent_preaccept_failure_abandons_reservation_for_retry():
    registry = RequestLifecycleRegistry(capacity=1)
    runner = MagicMock()
    runner.get_or_create_session.side_effect = RuntimeError("busy")
    request_id = "018f6f44-75f4-7def-b746-01a31bc3c003"
    response = TestClient(_agent_app(registry, runner)).post(
        "/v1/agent/chat",
        json={
            "request_id": request_id,
            "session_id": "agent-session-1",
            "message": "inspect",
            "stream": True,
        },
    )

    assert response.status_code == 409
    replacement = registry.reserve(
        request_id=request_id,
        request_kind="agent",
        context_identity="agent-session-1",
        payload_fingerprint=hashlib.sha256(b"replacement").hexdigest(),
    )
    assert replacement.should_start is True


def test_lifecycle_validation_conflict_and_capacity_map_to_generic_statuses():
    runner = MagicMock()
    runner.run_sync = AsyncMock(
        return_value={"session_id": "agent-session-1", "answer": "ok", "steps": []}
    )
    runner.get_or_create_session.return_value = MagicMock(session_id="agent-session-1")

    invalid = TestClient(_agent_app(RequestLifecycleRegistry(), runner)).post(
        "/v1/agent/chat",
        json={"request_id": "short", "message": "inspect", "stream": False},
    )
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "invalid request lifecycle metadata"}

    registry = RequestLifecycleRegistry()
    client = TestClient(_agent_app(registry, runner))
    request_id = "018f6f44-75f4-7def-b746-01a31bc3c004"
    first = client.post(
        "/v1/agent/chat",
        json={"request_id": request_id, "message": "first", "stream": False},
    )
    conflict = client.post(
        "/v1/agent/chat",
        json={"request_id": request_id, "message": "different", "stream": False},
    )
    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "request ID conflict"}

    full_registry = RequestLifecycleRegistry(capacity=1)
    full_registry.reserve(
        request_id="018f6f44-75f4-7def-b746-01a31bc3c099",
        request_kind="agent",
        context_identity=None,
        payload_fingerprint=hashlib.sha256(b"occupied").hexdigest(),
    )
    capacity = TestClient(_agent_app(full_registry, runner)).post(
        "/v1/agent/chat",
        json={
            "request_id": "018f6f44-75f4-7def-b746-01a31bc3c005",
            "message": "inspect",
            "stream": False,
        },
    )
    assert capacity.status_code == 503
    assert capacity.json() == {"detail": "request lifecycle capacity reached"}


@pytest.mark.asyncio
async def test_chat_nonstream_terminal_follows_worker_completion():
    events = []
    registry = _RecordingRegistry(events)
    service = MagicMock(is_ready=True)

    async def chat_async(**kwargs):
        events.append("worker_started")
        await asyncio.sleep(0)
        events.append("worker_cleanup")
        return GenerationResult(text="ok")

    service.chat_async = chat_async
    request_id = "018f6f44-75f4-7def-b746-01a31bc3c006"
    with patch("backend.api.chat.get_llm_service", return_value=service):
        response = TestClient(_chat_app(registry)).post(
            "/v1/chat/completions",
            json={
                "request_id": request_id,
                "model": "test",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert events.index("accepted") < events.index("worker_cleanup")
    assert events.index("worker_cleanup") < events.index("terminal:completed")
    assert registry.get_status(request_id) == "terminal"


def test_responses_route_cannot_bypass_strict_image_validation():
    registry = RequestLifecycleRegistry()
    service = MagicMock(is_ready=True)
    service.chat_async = AsyncMock(return_value=GenerationResult(text="unsafe"))
    request_id = "018f6f44-75f4-7def-b746-01a31bc3c007"
    with patch("backend.api.chat.get_llm_service", return_value=service):
        response = TestClient(_chat_app(registry)).post(
            "/v1/responses",
            json={
                "request_id": request_id,
                "model": "test",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,%%%"},
                            }
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert service.chat_async.await_count == 0
    assert registry.get_status(request_id) == "unknown"


@pytest.mark.asyncio
async def test_chat_stream_close_waits_for_provider_worker_cleanup():
    cleaned = threading.Event()

    class Provider:
        def is_loaded(self):
            return True

        def generate(self, **kwargs):
            def chunks():
                try:
                    yield {"content": "first"}
                    threading.Event().wait(0.1)
                finally:
                    cleaned.set()

            return chunks()

    service = LLMService(config=get_config())
    service._provider = Provider()
    stream = service.chat_stream_async([Message(role="user", content="hello")])
    await anext(stream)
    await stream.aclose()

    assert cleaned.is_set()


def test_vllm_output_budget_does_not_count_base64_bytes_as_text_tokens():
    small = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    ]
    large = [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + "A" * 100_000},
        }
    ]

    assert VLLMProvider._estimate_message_tokens(small) == VLLMProvider._estimate_message_tokens(large)


def test_sglang_request_logging_never_contains_data_urls():
    provider = SGLangProvider(get_config())
    provider.debug = MagicMock()
    image = _data_url()
    with patch.object(provider, "_sync_generate", return_value=GenerationResult(text="ok")):
        provider.generate(
            messages=[
                Message(
                    role="user",
                    content=[{"type": "image_url", "image_url": {"url": image}}],
                )
            ]
        )

    logged = " ".join(str(call) for call in provider.debug.inference.call_args_list)
    assert "data:image" not in logged
    assert image not in logged
    assert "images=1" in logged


@pytest.mark.parametrize(
    "provider_type",
    [SGLangProvider, VLLMProvider, OpenAICompatibleProxyProvider],
)
def test_provider_stream_errors_redact_data_urls_from_logs_and_chunks(provider_type):
    provider = provider_type(get_config())
    provider.debug = MagicMock()
    image = _data_url()
    client = MagicMock()
    client.__enter__.return_value = client
    client.stream.side_effect = RuntimeError(f"upstream rejected {image}")

    with patch("httpx.Client", return_value=client):
        chunks = list(provider._stream_generate({"messages": []}))

    observed = f"{provider.debug.mock_calls} {chunks}"
    assert "data:image" not in observed
    assert image not in observed


@pytest.mark.asyncio
async def test_chat_stream_exception_redacts_malformed_data_url_and_suffix(caplog):
    leaked = "data:image/png;base64,%%% leaked-chat-stream-suffix"

    class Service:
        async def chat_stream_async(self, **kwargs):
            raise RuntimeError(f"upstream failed {leaked}")
            yield

    with patch("backend.api.chat.debug") as debug:
        events = [
            event
            async for event in _generate_stream_response(
                service=Service(),
                messages=[Message(role="user", content="hello")],
                model="test",
                max_tokens=None,
                temperature=None,
                top_p=None,
            )
        ]

    observed = f"{caplog.text} {debug.mock_calls} {events}"
    assert "data:image" not in observed
    assert "leaked-chat-stream-suffix" not in observed


@pytest.mark.asyncio
async def test_chat_stream_diagnostic_exception_is_redacted(caplog):
    leaked = "data:image/png;base64,%%% leaked-diagnostic-suffix"

    class Service:
        async def chat_stream_async(self, **kwargs):
            if False:
                yield

    with patch(
        "backend.api.chat.logger.info",
        side_effect=RuntimeError(f"diagnostic failed {leaked}"),
    ):
        events = [
            event
            async for event in _generate_stream_response(
                service=Service(),
                messages=[Message(role="user", content="hello")],
                model="test",
                max_tokens=None,
                temperature=None,
                top_p=None,
            )
        ]

    observed = f"{caplog.text} {events}"
    assert "data:image" not in observed
    assert "leaked-diagnostic-suffix" not in observed


def test_generated_request_ids_are_high_entropy_compatible():
    from backend.llm_service import create_request_id

    request_id = create_request_id()
    assert len(request_id) >= 32
    assert request_id != create_request_id()


@pytest.mark.asyncio
async def test_chat_hub_lifecycle_acceptance_precedes_model_delta_and_reaches_terminal():
    from tests.test_chat_session_hub import (
        REQUEST_ID_1,
        _app,
        _chat_request,
        _decode_sse,
        _delta,
        _start,
    )

    app, _default_registry, _runtime, store = _app()
    events = []
    registry = _RecordingRegistry(events)
    app.state.request_lifecycle_registry = registry

    async def source():
        events.append("source_read")
        yield f'data: {json.dumps(_delta(content="visible"))}\n\n'
        yield "data: [DONE]\n\n"

    response = await _start(app, _chat_request(), source())
    payloads = [_decode_sse(frame) async for frame in response.body_iterator]

    assert payloads[0]["type"] == "request_accepted"
    assert payloads[1]["event"]["kind"] == "turn_started"
    assert events.index("accepted") < events.index("source_read")
    assert events.index("running") < events.index("source_read")
    assert events[-1] == "terminal:completed"
    assert registry.get_status(REQUEST_ID_1) == "terminal"
    assert store.get(payloads[0]["session_id"]).finished is True


class _AdmissionStage:
    def __init__(self, events, *, projected_images=0, session_id="agent-session-1"):
        self.events = events
        self.projected_historical_image_count = projected_images
        self.session_id = session_id
        self.rolled_back = False


class _BoundaryRunner:
    def __init__(self, events, *, projected_images=0):
        self.events = events
        self.stage = _AdmissionStage(events, projected_images=projected_images)
        self.active_scopes = set()
        self.worker_done = threading.Event()
        self.received_metadata = None
        self.stage_calls = 0

    def stage_agent_request_admission(
        self,
        session_id,
        mode,
        vision_automation_metadata,
        inbound_image_count=None,
    ):
        self.stage_calls += 1
        self.events.append("staged")
        self.received_metadata = vision_automation_metadata
        return self.stage

    def rollback_agent_request_stage(self, stage):
        stage.rolled_back = True
        self.events.append("rolled_back")

    def capture_agent_request_run_scopes(self, stage):
        return frozenset(self.active_scopes)

    async def wait_for_agent_request_run_cleanup(self, stage, scope_ids):
        self.events.append("barrier_wait")
        await asyncio.to_thread(self.worker_done.wait, 1)
        self.events.append("barrier_done")

    def run_stream(self, *args, admission_stage=None, on_run_registered=None, **kwargs):
        async def stream():
            self.active_scopes.add("outer")
            self.events.append("run_scope_registered")
            self.events.append("omission_committed")
            on_run_registered(admission_stage, frozenset(self.active_scopes))
            yield 'data: {"type":"session_start","session_id":"agent-session-1"}\n\n'
            self.active_scopes.add("inner")
            yield 'data: {"type":"done","session_id":"agent-session-1"}\n\n'

            def cleanup():
                threading.Event().wait(0.05)
                self.active_scopes.clear()
                self.worker_done.set()
                self.events.append("inner_worker_cleanup")

            threading.Thread(target=cleanup, daemon=True).start()

        return stream()

    async def run_sync(self, *args, admission_stage=None, on_run_registered=None, **kwargs):
        self.active_scopes.add("outer")
        self.events.append("run_scope_registered")
        self.events.append("omission_committed")
        on_run_registered(admission_stage, frozenset(self.active_scopes))
        self.events.append("inner_worker_cleanup")
        self.active_scopes.clear()
        self.worker_done.set()
        return {"session_id": admission_stage.session_id, "answer": "ok", "steps": []}


def _automation_metadata():
    return {
        "history_image_policy": "aggregate_prior_image_blocks",
        "manual_image_count": 0,
        "source_count": 0,
        "retained_tick_count": 0,
        "binding": {
            "page_generation": 1,
            "mode": "agent",
            "top_level_context_id": "agent-session-1",
            "automation_task_id": "task_01J0123456789ABCDEFGH",
            "capture_cycle_id": "cycle_01J0123456789ABCDEFG",
        },
    }


def test_agent_staged_admission_passes_metadata_and_orders_commit_before_acceptance():
    events = []
    registry = _RecordingRegistry(events)
    runner = _BoundaryRunner(events)
    response = TestClient(_agent_app(registry, runner)).post(
        "/v1/agent/chat",
        json={
            "request_id": "018f6f44-75f4-7def-b746-01a31bc3c010",
            "session_id": "agent-session-1",
            "message": "inspect",
            "stream": True,
            "vision_automation": _automation_metadata(),
        },
    )

    assert response.status_code == 200
    assert isinstance(runner.received_metadata, VisionAutomationTransportMetadata)
    assert events.index("reserved") < events.index("staged")
    assert events.index("run_scope_registered") < events.index("omission_committed")
    assert events.index("omission_committed") < events.index("accepted")


def test_agent_automation_binding_rejects_cross_context_before_reservation():
    events = []
    registry = _RecordingRegistry(events)
    runner = _BoundaryRunner(events)
    metadata = _automation_metadata()
    metadata["binding"]["top_level_context_id"] = "other-agent-session"

    response = TestClient(_agent_app(registry, runner)).post(
        "/v1/agent/chat",
        json={
            "request_id": "018f6f44-75f4-7def-b746-01a31bc3c012",
            "session_id": "agent-session-1",
            "message": "inspect",
            "stream": True,
            "vision_automation": metadata,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Agent vision automation top-level context mismatch"
    )
    assert "reserved" not in events


def test_agent_automation_rejects_legacy_route_without_lifecycle_registry():
    events = []
    runner = _BoundaryRunner(events)

    response = TestClient(_agent_app(None, runner)).post(
        "/v1/agent/chat",
        json={
            "request_id": "018f6f44-75f4-7def-b746-01a31bc3c013",
            "session_id": "agent-session-1",
            "message": "inspect",
            "stream": True,
            "vision_automation": _automation_metadata(),
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "vision automation requires strict request lifecycle admission"
    )
    assert events == []


def test_agent_cleanup_registration_route_passes_strict_model_to_runner():
    class Runner:
        def register_agent_vision_automation_cleanup(self, session_id, binding, cleanup_token):
            self.received = (session_id, binding, cleanup_token)

    runner = Runner()
    response = TestClient(_agent_app(None, runner)).post(
        "/v1/agent/vision-automation/abort",
        json={
            "session_id": "agent-session-1",
            "binding": _automation_metadata()["binding"],
            "cleanup_token": "cleanup_01J0123456789ABCDE",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "cleanup_pending",
        "session_id": "agent-session-1",
    }
    session_id, binding, cleanup_token = runner.received
    assert session_id == "agent-session-1"
    assert isinstance(binding, VisionAutomationBinding)
    assert cleanup_token == "cleanup_01J0123456789ABCDE"


def test_agent_cleanup_registration_reports_prior_cancel_tombstone():
    class Runner:
        def register_agent_vision_automation_cleanup(
            self, session_id, binding, cleanup_token
        ):
            return type("CleanupState", (), {"cancelled": True})()

    response = TestClient(_agent_app(None, Runner())).post(
        "/v1/agent/vision-automation/abort",
        json={
            "session_id": "agent-session-1",
            "binding": _automation_metadata()["binding"],
            "cleanup_token": "cleanup_01J0123456789ABCDE",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "cleanup_cancelled",
        "session_id": "agent-session-1",
    }


def test_agent_cleanup_cancel_route_reuses_registration_model_and_response():
    class Runner:
        def cancel_agent_vision_automation_cleanup(self, session_id, binding, cleanup_token):
            self.received = (session_id, binding, cleanup_token)

    runner = Runner()
    response = TestClient(_agent_app(None, runner)).post(
        "/v1/agent/vision-automation/abort/cancel",
        json={
            "session_id": "agent-session-1",
            "binding": _automation_metadata()["binding"],
            "cleanup_token": "cleanup_01J0123456789ABCDE",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "cleanup_cancelled",
        "session_id": "agent-session-1",
    }
    session_id, binding, cleanup_token = runner.received
    assert session_id == "agent-session-1"
    assert isinstance(binding, VisionAutomationBinding)
    assert cleanup_token == "cleanup_01J0123456789ABCDE"


def test_duplicate_cleanup_admission_request_id_stages_only_once():
    events = []
    registry = _RecordingRegistry(events)
    runner = _BoundaryRunner(events)
    metadata = _automation_metadata()
    metadata["binding"]["cleanup_token"] = "cleanup_01J0123456789ABCDE"
    payload = {
        "request_id": "018f6f44-75f4-7def-b746-01a31bc3c014",
        "session_id": "agent-session-1",
        "message": "cleanup",
        "stream": True,
        "vision_automation": metadata,
    }

    first = TestClient(_agent_app(registry, runner)).post("/v1/agent/chat", json=payload)
    second = TestClient(_agent_app(registry, runner)).post("/v1/agent/chat", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "terminal"
    assert runner.stage_calls == 1


def test_agent_stream_terminal_waits_for_inner_worker_and_captured_scope_barrier():
    events = []
    registry = _RecordingRegistry(events)
    runner = _BoundaryRunner(events)
    response = TestClient(_agent_app(registry, runner)).post(
        "/v1/agent/chat",
        json={
            "request_id": "018f6f44-75f4-7def-b746-01a31bc3c011",
            "session_id": "agent-session-1",
            "message": "inspect",
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert events.index("inner_worker_cleanup") < events.index("barrier_done")
    assert events.index("barrier_done") < events.index("terminal:completed")


def test_agent_nonstream_acceptance_uses_runner_registration_handshake():
    events = []
    registry = _RecordingRegistry(events)
    runner = _BoundaryRunner(events)
    response = TestClient(_agent_app(registry, runner)).post(
        "/v1/agent/chat",
        json={
            "request_id": "018f6f44-75f4-7def-b746-01a31bc3c012",
            "session_id": "agent-session-1",
            "message": "inspect",
            "stream": False,
        },
    )

    assert response.status_code == 200
    assert events.index("run_scope_registered") < events.index("accepted")
    assert events.index("accepted") < events.index("inner_worker_cleanup")


def test_agent_staged_admission_failure_rolls_back_preaccept_context():
    events = []
    registry = RequestLifecycleRegistry()
    runner = _BoundaryRunner(events, projected_images=250)
    response = TestClient(_agent_app(registry, runner)).post(
        "/v1/agent/chat",
        json={
            "request_id": "018f6f44-75f4-7def-b746-01a31bc3c013",
            "session_id": "agent-session-1",
            "message": "inspect",
            "images": [_data_url()],
            "stream": True,
        },
    )

    assert response.status_code == 422
    assert runner.stage.rolled_back is True
    assert registry.get_status("018f6f44-75f4-7def-b746-01a31bc3c013") == "unknown"


def test_agent_historical_shape_inspection_failure_is_fail_closed():
    class Runner(_BoundaryRunner):
        def stage_agent_request_admission(self, *args, **kwargs):
            raise VisionRequestValidationError("cannot inspect canonical image shape")

    response = TestClient(_agent_app(RequestLifecycleRegistry(), Runner([]))).post(
        "/v1/agent/chat",
        json={
            "request_id": "018f6f44-75f4-7def-b746-01a31bc3c014",
            "session_id": "agent-session-1",
            "message": "inspect",
            "stream": True,
        },
    )
    assert response.status_code == 422


def test_responses_input_image_is_translated_to_canonical_chat_image_url():
    registry = RequestLifecycleRegistry()
    service = MagicMock(is_ready=True)
    captured = {}

    async def chat_async(**kwargs):
        captured["messages"] = kwargs["messages"]
        return GenerationResult(text="ok")

    service.chat_async = chat_async
    image = _data_url()
    with patch("backend.api.chat.get_llm_service", return_value=service):
        response = TestClient(_chat_app(registry)).post(
            "/v1/responses",
            json={
                "request_id": "018f6f44-75f4-7def-b746-01a31bc3c015",
                "model": "test",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "inspect"},
                            {"type": "input_image", "image_url": image},
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert captured["messages"][0].content[1] == {
        "type": "image_url",
        "image_url": {"url": image},
    }


def test_responses_direct_messages_input_image_is_never_ignored():
    service = MagicMock(is_ready=True)
    captured = {}

    async def chat_async(**kwargs):
        captured["messages"] = kwargs["messages"]
        return GenerationResult(text="ok")

    service.chat_async = chat_async
    image = _data_url()
    with patch("backend.api.chat.get_llm_service", return_value=service):
        response = TestClient(_chat_app(RequestLifecycleRegistry())).post(
            "/v1/responses",
            json={
                "request_id": "018f6f44-75f4-7def-b746-01a31bc3c017",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "inspect"},
                            {"type": "input_image", "image_url": image},
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert captured["messages"][0].content[1] == {
        "type": "image_url",
        "image_url": {"url": image},
    }


def test_responses_direct_messages_rejects_unsupported_multimodal_part(caplog):
    service = MagicMock(is_ready=True)
    service.chat_async = AsyncMock(return_value=GenerationResult(text="unsafe"))
    with patch("backend.api.chat.get_llm_service", return_value=service):
        response = TestClient(_chat_app(RequestLifecycleRegistry())).post(
            "/v1/responses",
            json={
                "request_id": "018f6f44-75f4-7def-b746-01a31bc3c018",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_audio", "audio": "direct-secret"}
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 400
    assert service.chat_async.await_count == 0
    assert "direct-secret" not in response.text
    assert "direct-secret" not in caplog.text


def test_responses_rejects_unsupported_multimodal_dictionary_without_stringifying(caplog):
    service = MagicMock(is_ready=True)
    service.chat_async = AsyncMock(return_value=GenerationResult(text="unsafe"))
    with patch("backend.api.chat.get_llm_service", return_value=service):
        response = TestClient(_chat_app(RequestLifecycleRegistry())).post(
            "/v1/responses",
            json={
                "request_id": "018f6f44-75f4-7def-b746-01a31bc3c016",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_audio", "audio": "secret"}],
                    }
                ],
            },
        )

    assert response.status_code == 400
    assert service.chat_async.await_count == 0
    assert "secret" not in response.text
    assert "secret" not in caplog.text
