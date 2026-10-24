import asyncio
import hashlib
import json
import threading
from datetime import datetime
from copy import deepcopy
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.agent.agent_runner import (
    AgentRunner,
    AgentSession,
    ChildAgentRun,
    SessionDisposalError,
)
from backend.agent.session_hub import AgentSessionHub
from backend.api.agent import (
    AgentAnswerNowStreamRequest,
    AgentStopRequest,
    _abort_agent_session_request,
    answer_now,
    answer_now_stream,
    child_agent_stream,
    stop_agent,
    _answer_now_router,
    router as agent_router,
)
from backend.request_lifecycle import RequestLifecycleRegistry
from backend.api.request_status import router as request_status_router
from backend.config import get_config
from backend.session_hub_runtime import SessionHubRuntime


ANSWER_NOW_REQUEST_ID = "answer-now-request-01J0123456789ABCDEF"


class _RecordingLifecycleRegistry(RequestLifecycleRegistry):
    def __init__(self, events, **kwargs):
        super().__init__(**kwargs)
        self.events = events
        self.terminal_reached = threading.Event()

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
        result = super().mark_terminal(token, outcome)
        self.terminal_reached.set()
        return result


class _ObservedWaitLifecycleRegistry(RequestLifecycleRegistry):
    def __init__(self):
        super().__init__()
        self.wait_started = threading.Event()
        self.wait_finished = threading.Event()

    def wait_for_reservation_resolution(self, **kwargs):
        self.wait_started.set()
        try:
            return super().wait_for_reservation_resolution(**kwargs)
        finally:
            self.wait_finished.set()


class _FailingMarkRunningRegistry(RequestLifecycleRegistry):
    def mark_running(self, token):
        raise RuntimeError("mark_running failed")


def _answer_now_stream_app(runner, registry):
    app = FastAPI()
    app.include_router(agent_router)
    app.include_router(_answer_now_router)
    app.state.agent_runner = runner
    app.state.request_lifecycle_registry = registry
    runtime = SessionHubRuntime()
    session = runner.sessions.get.return_value
    session_id = getattr(session, "session_id", None)
    if isinstance(session_id, str) and session_id:
        hub = getattr(session, "event_hub", None)
        if not isinstance(hub, AgentSessionHub):
            hub = AgentSessionHub()
            session.event_hub = hub
        runtime.register(session_id=session_id, kind="agent", hub=hub)
    app.state.session_hub_runtime = runtime
    return app


def _use_session_answer_now_acquisition(runner, session):
    def _try_acquire(session_id, *, expected_session=None):
        if expected_session is not None and expected_session is not session:
            return "not_found", None
        if not session.try_begin_answer_now():
            return "busy", None
        return "acquired", session

    runner.try_acquire_answer_now.side_effect = _try_acquire


def test_stop_agent_prefers_request_id_abort_over_abort_all():
    app = FastAPI()
    app.include_router(agent_router)

    mock_runner = MagicMock()
    mock_runner.abort_session.return_value = True
    mock_runner.get_current_request_id.return_value = "req-agent-123"
    app.state.agent_runner = mock_runner

    client = TestClient(app)

    with (
        patch("backend.config.get_config") as mock_get_config,
        patch("backend.llm_service.abort_request_by_id") as mock_abort_request_by_id,
        patch("backend.llm_service.abort_sglang_all") as mock_abort_all,
    ):
        mock_get_config.return_value = MagicMock(
            sglang=MagicMock(host="127.0.0.1", port=30000)
        )

        response = client.post("/v1/agent/stop", json={"session_id": "agent-session"})

    assert response.status_code == 200
    assert response.json() == {"status": "stopped", "session_id": "agent-session"}
    mock_runner.abort_session.assert_called_once_with("agent-session")
    mock_runner.get_current_request_id.assert_called_once_with("agent-session")
    mock_abort_request_by_id.assert_called_once_with(
        "req-agent-123",
        "http://127.0.0.1:30000",
        fallback_to_abort_all=False,
        require_provider_abort=True,
    )
    mock_abort_all.assert_not_called()


def test_stop_agent_captures_request_before_session_abort_clears_it():
    mock_runner = MagicMock()
    active_request = ["req-agent-before-disconnect"]
    mock_runner.get_current_request_id.side_effect = lambda _session_id: active_request[0]

    def abort_session(_session_id):
        active_request[0] = None
        return True

    mock_runner.abort_session.side_effect = abort_session

    with (
        patch("backend.config.get_config") as mock_get_config,
        patch("backend.llm_service.abort_request_by_id") as targeted_abort,
    ):
        mock_get_config.return_value = MagicMock(
            sglang=MagicMock(host="127.0.0.1", port=30000)
        )
        request_id = _abort_agent_session_request(mock_runner, "agent-session")

    assert request_id == "req-agent-before-disconnect"
    targeted_abort.assert_called_once_with(
        "req-agent-before-disconnect",
        "http://127.0.0.1:30000",
        fallback_to_abort_all=False,
    )


def _active_agent_request(registry, request_id, session_id):
    reservation = registry.reserve(
        request_id=request_id,
        request_kind="agent",
        context_identity=session_id,
        payload_fingerprint="a" * 64,
    )
    registry.accept(reservation.registration_token)
    return reservation.registration_token


def test_agent_stop_accepts_outer_turn_id_and_targets_inner_provider_id():
    runner = MagicMock()
    runner.get_current_request_id.return_value = "inner-provider-request"
    runner.sessions.get.return_value.capture_active_run_scopes.return_value = frozenset({"active-run"})
    raw_request = MagicMock()
    raw_request.app.state.agent_runner = runner
    registry = RequestLifecycleRegistry()
    _active_agent_request(registry, "outer-turn-request", "same-session")
    raw_request.app.state.request_lifecycle_registry = registry
    with (
        patch("backend.config.get_config") as config,
        patch("backend.llm_service.abort_request_by_id", return_value=True) as targeted_abort,
    ):
        config.return_value = MagicMock(sglang=MagicMock(host="127.0.0.1", port=30000))
        result = asyncio.run(stop_agent(
            AgentStopRequest(session_id="same-session", expected_request_id="outer-turn-request"),
            raw_request,
        ))
    assert result["status"] == "stopped"
    targeted_abort.assert_called_once_with(
        "inner-provider-request",
        "http://127.0.0.1:30000",
        fallback_to_abort_all=False,
        require_provider_abort=True,
    )
    runner.abort_session.assert_called_once_with("same-session")


def test_agent_stop_route_rejects_stale_expected_request():
    runner = MagicMock()
    runner.get_current_request_id.return_value = "new-inner-provider-request"
    raw_request = MagicMock()
    raw_request.app.state.agent_runner = runner
    registry = RequestLifecycleRegistry()
    token = _active_agent_request(registry, "old-outer-request", "same-session")
    registry.mark_terminal(token, "completed")
    raw_request.app.state.request_lifecycle_registry = registry
    with pytest.raises(HTTPException) as error:
        asyncio.run(stop_agent(
            AgentStopRequest(session_id="same-session", expected_request_id="old-outer-request"),
            raw_request,
        ))
    assert error.value.status_code == 409
    runner.abort_session.assert_not_called()


def test_agent_stop_route_rejects_outer_request_from_another_session():
    runner = MagicMock()
    raw_request = MagicMock()
    raw_request.app.state.agent_runner = runner
    registry = RequestLifecycleRegistry()
    _active_agent_request(registry, "other-outer-request", "other-session")
    raw_request.app.state.request_lifecycle_registry = registry
    with pytest.raises(HTTPException) as error:
        asyncio.run(stop_agent(
            AgentStopRequest(session_id="same-session", expected_request_id="other-outer-request"),
            raw_request,
        ))
    assert error.value.status_code == 409
    runner.abort_session.assert_not_called()


def test_agent_stop_targets_next_model_step_in_same_run():
    runner = MagicMock()
    current_request = ["request-a"]
    runner.get_current_request_id.side_effect = lambda _session_id: current_request[0]
    runner.sessions.get.return_value.capture_active_run_scopes.return_value = frozenset({"same-run"})

    def targeted_abort(request_id, _url, **_kwargs):
        if request_id == "request-a":
            current_request[0] = "request-b"
        return True

    with (
        patch("backend.config.get_config") as config,
        patch("backend.llm_service.abort_request_by_id", side_effect=targeted_abort) as abort,
    ):
        config.return_value = MagicMock(sglang=MagicMock(host="127.0.0.1", port=30000))
        _abort_agent_session_request(
            runner,
            "same-session",
            expected_request_id="outer-turn-request",
            require_provider_abort=True,
        )

    assert [call.args[0] for call in abort.call_args_list] == ["request-a", "request-b"]
    runner.abort_session.assert_called_once_with("same-session")


def test_agent_stop_targets_model_step_registered_during_session_abort():
    runner = MagicMock()
    current_request = ["request-a"]
    runner.get_current_request_id.side_effect = lambda _session_id: current_request[0]
    runner.sessions.get.return_value.capture_active_run_scopes.return_value = frozenset({"same-run"})

    def abort_session(_session_id):
        current_request[0] = "request-b"
        return True

    runner.abort_session.side_effect = abort_session
    with (
        patch("backend.config.get_config") as config,
        patch("backend.llm_service.abort_request_by_id", return_value=True) as abort,
    ):
        config.return_value = MagicMock(sglang=MagicMock(host="127.0.0.1", port=30000))
        _abort_agent_session_request(
            runner,
            "same-session",
            expected_request_id="outer-turn-request",
            require_provider_abort=True,
        )
    assert [call.args[0] for call in abort.call_args_list] == ["request-a", "request-b"]


def test_agent_stop_does_not_abort_new_run_after_old_run_finishes():
    runner = MagicMock()
    current_request = ["old-request"]
    current_scopes = [frozenset({"old-run"})]
    runner.get_current_request_id.side_effect = lambda _session_id: current_request[0]
    runner.sessions.get.return_value.capture_active_run_scopes.side_effect = lambda: current_scopes[0]

    def targeted_abort(request_id, _url, **_kwargs):
        current_request[0] = "new-request"
        current_scopes[0] = frozenset({"new-run"})
        return True

    with (
        patch("backend.config.get_config") as config,
        patch("backend.llm_service.abort_request_by_id", side_effect=targeted_abort) as abort,
    ):
        config.return_value = MagicMock(sglang=MagicMock(host="127.0.0.1", port=30000))
        _abort_agent_session_request(
            runner,
            "same-session",
            expected_request_id="old-outer-turn-request",
            require_provider_abort=True,
        )

    abort.assert_called_once()
    runner.abort_session.assert_not_called()


def test_agent_stop_reports_targeted_provider_abort_failure():
    from types import SimpleNamespace
    from backend.api.agent import AgentStopRequest

    runner = MagicMock()
    runner.get_current_request_id.return_value = "req-agent-failed-stop"
    raw_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent_runner=runner)))
    with (
        patch("backend.config.get_config") as mock_get_config,
        patch("backend.llm_service.abort_request_by_id", return_value=False),
    ):
        mock_get_config.return_value = MagicMock(
            sglang=MagicMock(host="127.0.0.1", port=30000)
        )
        with pytest.raises(HTTPException) as caught:
            asyncio.run(stop_agent(AgentStopRequest(session_id="agent-session"), raw_request))

    assert caught.value.status_code == 502
    runner.abort_session.assert_not_called()


def test_stop_agent_records_pending_abort_even_before_session_exists():
    app = FastAPI()
    app.include_router(agent_router)

    mock_runner = MagicMock()
    mock_runner.abort_session.return_value = False
    mock_runner.get_current_request_id.return_value = None
    app.state.agent_runner = mock_runner

    client = TestClient(app)

    with patch("backend.config.get_config") as mock_get_config:
        mock_get_config.return_value = MagicMock(
            sglang=MagicMock(host="127.0.0.1", port=30000)
        )
        response = client.post("/v1/agent/stop", json={"session_id": "agent-session"})

    assert response.status_code == 200
    assert response.json() == {"status": "stopped", "session_id": "agent-session"}
    mock_runner.abort_session.assert_called_once_with("agent-session")


def test_abort_child_endpoint_targets_one_child_and_is_idempotent():
    app = FastAPI()
    app.include_router(agent_router)
    mock_runner = MagicMock()
    mock_runner.abort_child_agent_run = AsyncMock(
        side_effect=[
            {"status": "aborted", "child_run_id": "child-run-1"},
            {"status": "already_terminal", "child_run_id": "child-run-1"},
        ]
    )
    app.state.agent_runner = mock_runner
    client = TestClient(app)

    first = client.post("/v1/agent/child/abort", json={"child_run_id": "child-run-1"})
    second = client.post("/v1/agent/child/abort", json={"child_run_id": "child-run-1"})

    assert first.status_code == 200
    assert first.json()["status"] == "aborted"
    assert second.status_code == 200
    assert second.json()["status"] == "already_terminal"


def test_abort_child_endpoint_returns_404_for_unknown_child():
    app = FastAPI()
    app.include_router(agent_router)
    mock_runner = MagicMock()
    mock_runner.abort_child_agent_run = AsyncMock(side_effect=KeyError("missing"))
    app.state.agent_runner = mock_runner

    response = TestClient(app).post(
        "/v1/agent/child/abort", json={"child_run_id": "missing"}
    )

    assert response.status_code == 404


def test_delete_session_awaits_lifecycle_safe_runner_disposal():
    app = FastAPI()
    app.include_router(agent_router)
    runner = MagicMock()
    runner.dispose_session = AsyncMock(
        return_value={"status": "disposed", "session_id": "agent-session"}
    )
    app.state.agent_runner = runner

    response = TestClient(app).delete("/v1/agent/session/agent-session")

    assert response.status_code == 200
    assert response.json() == {
        "status": "disposed",
        "session_id": "agent-session",
    }
    runner.dispose_session.assert_awaited_once_with("agent-session")
    runner.sessions.delete.assert_not_called()


def test_delete_session_returns_idempotent_already_disposed_status():
    app = FastAPI()
    app.include_router(agent_router)
    runner = MagicMock()
    runner.dispose_session = AsyncMock(
        return_value={"status": "already_disposed", "session_id": "missing-session"}
    )
    app.state.agent_runner = runner

    response = TestClient(app).delete("/v1/agent/session/missing-session")

    assert response.status_code == 200
    assert response.json()["status"] == "already_disposed"


def test_delete_session_maps_bounded_disposal_diagnostic():
    app = FastAPI()
    app.include_router(agent_router)
    runner = MagicMock()
    runner.dispose_session = AsyncMock(
        side_effect=SessionDisposalError(
            "session_disposal_timeout",
            pending_scope_count=2,
            pending_child_count=1,
        )
    )
    app.state.agent_runner = runner

    response = TestClient(app).delete("/v1/agent/session/agent-session")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "session_disposal_timeout",
            "pending_scope_count": 2,
            "pending_child_count": 1,
        }
    }


def test_answer_now_stops_current_run_and_generates_final_answer():
    app = FastAPI()
    app.include_router(agent_router)
    app.include_router(_answer_now_router)

    mock_runner = MagicMock()
    session = mock_runner.sessions.get.return_value
    session.try_begin_answer_now.return_value = True
    _use_session_answer_now_acquisition(mock_runner, session)
    mock_runner.abort_session.return_value = True
    mock_runner.get_current_request_id.return_value = "req-answer-now-1"
    mock_runner.answer_now_session.return_value = False
    mock_runner.sessions.get.return_value.capture_active_run_scopes.return_value = frozenset({"old-scope"})
    mock_runner.generate_answer_now_reply = AsyncMock(return_value={
        "status": "ok",
        "session_id": "agent-session",
        "answer": "final answer",
    })
    app.state.agent_runner = mock_runner

    client = TestClient(app)

    with (
        patch("backend.config.get_config") as mock_get_config,
        patch("backend.llm_service.abort_request_by_id") as mock_abort_request_by_id,
    ):
        mock_get_config.return_value = MagicMock(
            sglang=MagicMock(host="127.0.0.1", port=30000)
        )

        response = client.post("/v1/agent/answer-now", json={"session_id": "agent-session"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "session_id": "agent-session",
        "answer": "final answer",
    }
    mock_runner.abort_session.assert_called_once_with("agent-session")
    mock_runner.get_current_request_id.assert_called_once_with("agent-session")
    mock_abort_request_by_id.assert_called_once_with(
        "req-answer-now-1",
        "http://127.0.0.1:30000",
        fallback_to_abort_all=False,
    )
    mock_runner.generate_answer_now_reply.assert_called_once_with(
        "agent-session",
        captured_scope_ids=frozenset({"old-scope"}),
        single_flight_acquired=True,
        cancelled=ANY,
    )
    mock_runner.answer_now_session.assert_not_called()


def test_answer_now_non_stream_returns_generic_busy_when_disposal_owns_session():
    app = FastAPI()
    app.include_router(agent_router)
    app.include_router(_answer_now_router)
    session = MagicMock()
    session.agent.memory.steps = ["canonical-history"]
    runner = MagicMock()
    runner.sessions.get.return_value = session
    runner.try_acquire_answer_now.return_value = ("busy", None)
    app.state.agent_runner = runner

    response = TestClient(app).post(
        "/v1/agent/answer-now", json={"session_id": "agent-session"}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Answer Now unavailable"}
    runner.try_acquire_answer_now.assert_called_once_with("agent-session")
    runner.generate_answer_now_reply.assert_not_called()
    assert session.agent.memory.steps == ["canonical-history"]


def test_answer_now_stream_stops_current_run_and_returns_sse():
    app = FastAPI()
    app.include_router(agent_router)
    app.include_router(_answer_now_router)

    mock_runner = MagicMock()
    mock_runner.abort_session.return_value = True
    mock_runner.get_current_request_id.return_value = "req-answer-now-stream-1"
    session = mock_runner.sessions.get.return_value
    session.session_id = "agent-session"
    session.event_hub = AgentSession(
        session_id="answer-now-hub-owner",
        agent=MagicMock(),
        mode="tool_calling",
    ).event_hub
    session.event_hub.append({"type": "content", "content": "before answer now"})
    session.try_begin_answer_now.return_value = True
    _use_session_answer_now_acquisition(mock_runner, session)
    session.capture_active_run_scopes.side_effect = [
        frozenset({"old-scope"}),
        frozenset({"replacement-scope"}),
    ]
    session.wait_for_run_scopes.return_value = True

    async def _stream():
        yield 'data: {"type": "session_start", "session_id": "agent-session"}\n\n'
        yield 'data: {"type": "content", "content": "partial final"}\n\n'
        yield 'data: {"type": "done", "session_id": "agent-session"}\n\n'

    mock_runner.run_answer_now_stream.return_value = _stream()
    app.state.agent_runner = mock_runner
    app.state.request_lifecycle_registry = RequestLifecycleRegistry()

    client = TestClient(app)

    with (
        patch("backend.config.get_config") as mock_get_config,
        patch("backend.llm_service.abort_request_by_id") as mock_abort_request_by_id,
    ):
        mock_get_config.return_value = MagicMock(
            sglang=MagicMock(host="127.0.0.1", port=30000)
        )

        with client.stream(
                "POST",
                "/v1/agent/answer-now/stream",
                json={
                    "session_id": "agent-session",
                    "request_id": ANSWER_NOW_REQUEST_ID,
                },
        ) as response:
            body = "".join(response.iter_text())
            content_type = response.headers["content-type"]

    assert response.status_code == 200
    assert content_type.startswith("text/event-stream")
    assert '"type": "session_start"' in body
    assert '"type": "content", "content": "partial final"' in body
    assert '"type": "done", "session_id": "agent-session"' in body
    assert '"event_sequence"' in body
    assert session.event_hub.released is False
    assert session.event_hub.journal_size == 5
    replay = asyncio.run(
        _collect_hub_payloads(session.event_hub)
    )
    assert [payload["type"] for payload in replay] == [
        "content",
        "request_accepted",
        "session_start",
        "content",
        "done",
    ]
    assert replay[-2]["content"] == "partial final"
    mock_runner.abort_session.assert_called_once_with("agent-session")
    mock_runner.get_current_request_id.assert_called_once_with("agent-session")
    mock_abort_request_by_id.assert_called_once_with(
        "req-answer-now-stream-1",
        "http://127.0.0.1:30000",
        fallback_to_abort_all=False,
    )
    mock_runner.run_answer_now_stream.assert_called_once_with(
        "agent-session",
        captured_scope_ids=frozenset({"old-scope"}),
        single_flight_acquired=True,
        cancelled=ANY,
    )


async def _collect_hub_payloads(hub):
    return [dict(record.payload) async for record in hub.subscribe(0)]


@pytest.mark.anyio
async def test_stop_retains_hub_and_targeted_producer_terminal_events():
    runner = AgentRunner(MagicMock(), deepcopy(get_config()))
    session = AgentSession(
        session_id="stop-retained-session",
        agent=MagicMock(),
        mode="tool_calling",
    )
    runner.sessions.put(session)
    token = session.event_hub.claim_producer()
    session.event_hub.append(
        {"type": "content", "content": "before stop"},
        producer_token=token,
    )

    async def producer():
        while not session.aborted:
            await asyncio.sleep(0)
        session.event_hub.append(
            {"type": "aborted", "session_id": session.session_id},
            producer_token=token,
        )
        session.event_hub.append(
            {"type": "done", "session_id": session.session_id, "status": "aborted"},
            producer_token=token,
        )
        session.event_hub.close("aborted", producer_token=token)
        session.event_hub.release_producer(token)

    producer_task = asyncio.create_task(producer())
    raw_request = MagicMock()
    raw_request.app.state.agent_runner = runner
    with patch("backend.llm_service.abort_request_by_id") as targeted_abort:
        result = await stop_agent(
            MagicMock(session_id=session.session_id),
            raw_request,
        )
    await asyncio.wait_for(producer_task, timeout=1)
    replay = await _collect_hub_payloads(session.event_hub)

    assert result == {"status": "stopped", "session_id": session.session_id}
    assert [payload["type"] for payload in replay] == ["content", "aborted", "done"]
    assert session.event_hub.released is False
    assert session.event_hub.close_reason == "aborted"
    targeted_abort.assert_not_called()


def test_answer_now_stream_disposal_wins_abandons_fresh_lifecycle_id():
    registry = RequestLifecycleRegistry()
    session = MagicMock()
    session.session_id = "agent-session"
    session.agent.memory.steps = ["canonical-history"]
    session.try_begin_answer_now.return_value = True
    runner = MagicMock()
    runner.sessions.get.return_value = session
    runner.try_acquire_answer_now.return_value = ("busy", None)
    client = TestClient(_answer_now_stream_app(runner, registry))

    response = client.post(
        "/v1/agent/answer-now/stream",
        json={
            "session_id": "agent-session",
            "request_id": ANSWER_NOW_REQUEST_ID,
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Answer Now unavailable"}
    runner.try_acquire_answer_now.assert_called_once_with(
        "agent-session", expected_session=session
    )
    runner.run_answer_now_stream.assert_not_called()
    session.try_begin_answer_now.assert_not_called()
    assert session.agent.memory.steps == ["canonical-history"]
    retry = registry.reserve(
        request_id=ANSWER_NOW_REQUEST_ID,
        request_kind="agent_answer_now",
        context_identity="agent-session",
        payload_fingerprint=hashlib.sha256(
            b'{"session_id":"agent-session"}'
        ).hexdigest(),
    )
    assert retry.should_start is True
    assert registry.abandon(retry.registration_token) is True


@pytest.mark.anyio
async def test_answer_now_stream_stale_wrapper_rejects_same_id_replacement():
    registry = RequestLifecycleRegistry()
    runner = AgentRunner(MagicMock(), deepcopy(get_config()))
    old_session = AgentSession(
        session_id="agent-session",
        agent=MagicMock(),
        mode="tool_calling",
    )
    runner.sessions.put(old_session)
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = registry
    request.is_disconnected = AsyncMock(return_value=False)
    response = await answer_now_stream(
        AgentAnswerNowStreamRequest(
            session_id=old_session.session_id,
            request_id=ANSWER_NOW_REQUEST_ID,
        ),
        request,
    )

    replacement = AgentSession(
        session_id=old_session.session_id,
        agent=MagicMock(),
        mode="tool_calling",
    )
    runner.sessions.put(replacement)
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    with patch.object(runner, "run_answer_now_stream") as replacement_stream:
        await response({"type": "http", "method": "POST"}, receive, send)

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 409
    body = b"".join(message.get("body", b"") for message in sent)
    assert json.loads(body) == {"detail": "Answer Now unavailable"}
    replacement_stream.assert_not_called()
    assert old_session._answer_now_in_progress is False
    assert replacement._answer_now_in_progress is False

    retry = registry.reserve(
        request_id=ANSWER_NOW_REQUEST_ID,
        request_kind="agent_answer_now",
        context_identity=old_session.session_id,
        payload_fingerprint=hashlib.sha256(
            b'{"session_id":"agent-session"}'
        ).hexdigest(),
    )
    assert retry.should_start is True
    assert registry.abandon(retry.registration_token) is True


def test_answer_now_stream_requires_client_high_entropy_request_id_generically():
    runner = MagicMock()
    runner.sessions.get.return_value = MagicMock()
    client = TestClient(_answer_now_stream_app(runner, RequestLifecycleRegistry()))

    missing = client.post(
        "/v1/agent/answer-now/stream",
        json={"session_id": "agent-session"},
    )
    invalid = client.post(
        "/v1/agent/answer-now/stream",
        json={"session_id": "agent-session", "request_id": "short"},
    )

    assert missing.status_code == 422
    assert invalid.status_code == 422
    assert missing.json() == {"detail": "invalid request lifecycle metadata"}
    assert invalid.json() == {"detail": "invalid request lifecycle metadata"}
    runner.abort_session.assert_not_called()
    runner.run_answer_now_stream.assert_not_called()


def test_answer_now_stream_accepts_after_replacement_scope_and_suppresses_duplicate():
    events = []
    registry = _RecordingLifecycleRegistry(events)
    session = MagicMock()
    session.session_id = "agent-session"
    session.try_begin_answer_now.return_value = True
    session.capture_active_run_scopes.side_effect = [
        frozenset({"old-scope"}),
        frozenset({"replacement-scope"}),
    ]
    session.wait_for_run_scopes.side_effect = lambda scopes: events.append(
        f"barrier:{','.join(sorted(scopes))}"
    ) or True
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    runner.get_current_request_id.side_effect = lambda session_id: events.append(
        "captured-provider-request"
    ) or "old-provider-request"
    runner.abort_session.side_effect = lambda session_id: events.append("old-run-aborted") or True

    async def replacement_stream(*args, **kwargs):
        events.append("replacement-scope-registered")
        try:
            yield 'data: {"type": "session_start", "session_id": "agent-session"}\n\n'
            yield 'data: {"type": "content", "content": "final"}\n\n'
            yield 'data: {"type": "done", "session_id": "agent-session"}\n\n'
        finally:
            events.append("replacement-stream-closed")

    runner.run_answer_now_stream.side_effect = replacement_stream
    client = TestClient(_answer_now_stream_app(runner, registry))
    payload = {
        "session_id": "agent-session",
        "request_id": ANSWER_NOW_REQUEST_ID,
    }

    with patch("backend.llm_service.abort_request_by_id"):
        first = client.post("/v1/agent/answer-now/stream", json=payload)
        second = client.post("/v1/agent/answer-now/stream", json=payload)

    assert first.status_code == 200
    lines = [
        json.loads(line.removeprefix("data: "))
        for line in first.text.splitlines()
        if line.startswith("data: {")
    ]
    assert lines[0] == {
        "type": "request_accepted",
        "request_id": ANSWER_NOW_REQUEST_ID,
        "session_id": "agent-session",
        "status": "accepted",
        "event_sequence": 1,
    }
    assert lines[1]["type"] == "session_start"
    assert events.index("reserved") < events.index("old-run-aborted")
    assert events.index("replacement-scope-registered") < events.index("accepted")
    assert events.index("replacement-stream-closed") < events.index(
        "barrier:replacement-scope"
    )
    assert events.index("barrier:replacement-scope") < events.index(
        "terminal:completed"
    )
    assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "terminal"
    assert second.status_code == 200
    assert second.json() == {
        "request_id": ANSWER_NOW_REQUEST_ID,
        "status": "terminal",
    }
    assert runner.run_answer_now_stream.call_count == 1
    assert runner.abort_session.call_count == 1


@pytest.mark.anyio
async def test_answer_now_stream_disconnect_waits_for_replacement_worker_before_terminal():
    events = []
    worker_released = threading.Event()
    source_closed = asyncio.Event()
    allow_source_completion = asyncio.Event()
    registry = _RecordingLifecycleRegistry(events)
    session = MagicMock()
    session.session_id = "agent-session"
    session.try_begin_answer_now.return_value = True
    session.capture_active_run_scopes.side_effect = [
        frozenset(),
        frozenset({"replacement-scope"}),
    ]
    session.wait_for_run_scopes.side_effect = lambda scopes: worker_released.wait(2)
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    runner.get_current_request_id.return_value = None

    async def replacement_stream(*args, **kwargs):
        try:
            yield 'data: {"type": "session_start", "session_id": "agent-session"}\n\n'
            await allow_source_completion.wait()
            yield 'data: {"type": "done", "session_id": "agent-session"}\n\n'
        finally:
            source_closed.set()

    runner.run_answer_now_stream.side_effect = replacement_stream
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = registry
    request.is_disconnected = AsyncMock(return_value=False)
    response = await answer_now_stream(
        MagicMock(session_id="agent-session", request_id=ANSWER_NOW_REQUEST_ID),
        request,
    )
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        return None

    with patch("backend.api.agent._abort_agent_session_request"):
        response_task = asyncio.create_task(
            response({"type": "http", "method": "POST"}, receive, send)
        )
        await asyncio.wait_for(response_task, timeout=1)
        assert source_closed.is_set() is False
        assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "running"
        status_app = FastAPI()
        status_app.include_router(request_status_router)
        status_app.state.request_lifecycle_registry = registry
        assert TestClient(status_app).get(
            f"/v1/request-status/{ANSWER_NOW_REQUEST_ID}"
        ).json() == {
            "request_id": ANSWER_NOW_REQUEST_ID,
            "status": "running",
        }
        duplicate = TestClient(_answer_now_stream_app(runner, registry)).post(
            "/v1/agent/answer-now/stream",
            json={
                "session_id": "agent-session",
                "request_id": ANSWER_NOW_REQUEST_ID,
            },
        )
        assert duplicate.status_code == 200
        assert duplicate.json() == {
            "request_id": ANSWER_NOW_REQUEST_ID,
            "status": "running",
        }
        assert runner.run_answer_now_stream.call_count == 1
        assert response_task.done() is True
        allow_source_completion.set()
        await asyncio.wait_for(source_closed.wait(), timeout=1)
        assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "running"
        worker_released.set()
        assert await asyncio.to_thread(registry.terminal_reached.wait, 1)

    assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "terminal"
    assert TestClient(status_app).get(
        f"/v1/request-status/{ANSWER_NOW_REQUEST_ID}"
    ).json() == {
        "request_id": ANSWER_NOW_REQUEST_ID,
        "status": "terminal",
    }
    assert events[-1] == "terminal:completed"


@pytest.mark.anyio
async def test_answer_now_stream_initiating_subscriber_detach_does_not_own_producer():
    registry = RequestLifecycleRegistry()
    session = AgentSession(
        session_id="answer-now-detach", agent=MagicMock(), mode="tool_calling"
    )
    allow_completion = asyncio.Event()
    source_finished = asyncio.Event()
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    runner.get_current_request_id.return_value = "old-provider-request"

    async def replacement_stream(*args, **kwargs):
        replacement_scope = session.begin_run()
        try:
            yield (
                'data: {"type": "session_start", '
                '"session_id": "answer-now-detach"}\n\n'
            )
            await allow_completion.wait()
            yield 'data: {"type": "content", "content": "detached-final"}\n\n'
            yield (
                'data: {"type": "done", "session_id": '
                '"answer-now-detach", "status": "completed"}\n\n'
            )
        finally:
            session.finish_run(replacement_scope)
            source_finished.set()

    runner.run_answer_now_stream.side_effect = replacement_stream
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = registry
    request.is_disconnected = AsyncMock(return_value=False)
    response = await answer_now_stream(
        AgentAnswerNowStreamRequest(
            session_id=session.session_id,
            request_id="answer-now-detach-request-01J0123456789",
        ),
        request,
    )
    accepted_sent = asyncio.Event()
    sent = []
    received_request = False

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await accepted_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if b'"type": "request_accepted"' in message.get("body", b""):
            accepted_sent.set()

    with patch("backend.api.agent._abort_agent_session_request") as abort_request:
        response_task = asyncio.create_task(
            response({"type": "http", "method": "POST"}, receive, send)
        )
        await asyncio.wait_for(accepted_sent.wait(), timeout=1)
        await asyncio.wait_for(response_task, timeout=1)

        assert session.event_hub.subscriber_count == 0
        assert session.event_hub.has_active_producer is True
        assert source_finished.is_set() is False
        assert session.aborted is False
        abort_request.assert_called_once()

        allow_completion.set()
        await asyncio.wait_for(source_finished.wait(), timeout=1)
        for _ in range(100):
            if registry.get_status("answer-now-detach-request-01J0123456789") == "terminal":
                break
            await asyncio.sleep(0)

    replay = await _collect_hub_payloads(session.event_hub)
    assert [payload["type"] for payload in replay] == [
        "request_accepted",
        "session_start",
        "content",
        "done",
    ]
    assert replay[-2]["content"] == "detached-final"
    assert replay[-1]["status"] == "completed"
    assert session.event_hub.has_active_producer is False
    assert registry.get_status("answer-now-detach-request-01J0123456789") == "terminal"


@pytest.mark.anyio
async def test_answer_now_waits_for_exact_previous_hub_producer_handoff():
    request_id = "answer-now-handoff-request-01J0123456789"
    registry = RequestLifecycleRegistry()
    session = AgentSession(
        session_id="answer-now-handoff", agent=MagicMock(), mode="tool_calling"
    )
    old_token = session.event_hub.claim_producer()
    session.event_hub.append(
        {"type": "content", "content": "old partial"},
        producer_token=old_token,
    )
    old_scope = session.begin_run()
    old_scope_captured = asyncio.Event()
    replacement_registered = asyncio.Event()
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    runner.get_current_request_id.side_effect = (
        lambda session_id: old_scope_captured.set() or "old-provider-request"
    )

    async def replacement_stream(
        session_id,
        *,
        captured_scope_ids,
        single_flight_acquired,
        cancelled,
    ):
        assert captured_scope_ids == frozenset({old_scope})
        await asyncio.to_thread(session.wait_for_run_scopes, captured_scope_ids)
        replacement_scope = session.begin_run()
        replacement_registered.set()
        try:
            yield (
                'data: {"type": "session_start", '
                '"session_id": "answer-now-handoff"}\n\n'
            )
            yield 'data: {"type": "content", "content": "replacement final"}\n\n'
            yield (
                'data: {"type": "done", "session_id": '
                '"answer-now-handoff", "status": "completed"}\n\n'
            )
        finally:
            session.finish_run(replacement_scope)

    runner.run_answer_now_stream.side_effect = replacement_stream
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = registry
    request.is_disconnected = AsyncMock(return_value=False)
    response = await answer_now_stream(
        AgentAnswerNowStreamRequest(
            session_id=session.session_id,
            request_id=request_id,
        ),
        request,
    )
    sent = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    with patch("backend.api.agent._abort_agent_session_request") as abort_request:
        response_task = asyncio.create_task(
            response({"type": "http", "method": "POST"}, receive, send)
        )
        await asyncio.wait_for(old_scope_captured.wait(), timeout=1)
        session.finish_run(old_scope)
        await asyncio.wait_for(replacement_registered.wait(), timeout=1)
        await asyncio.sleep(0)
        try:
            assert response_task.done() is False
            assert session.event_hub.has_active_producer is True
            assert registry.get_status(request_id) == "unknown"
        finally:
            session.event_hub.append(
                {
                    "type": "done",
                    "session_id": session.session_id,
                    "status": "aborted",
                },
                producer_token=old_token,
            )
            session.event_hub.close("aborted", producer_token=old_token)
            session.event_hub.release_producer(old_token)

        await asyncio.wait_for(response_task, timeout=1)
        abort_request.assert_called_once()

    replay = await _collect_hub_payloads(session.event_hub)
    assert [payload["type"] for payload in replay] == [
        "content",
        "done",
        "request_accepted",
        "session_start",
        "content",
        "done",
    ]
    assert replay[-2]["content"] == "replacement final"
    assert replay[-1]["status"] == "completed"
    assert session.event_hub.has_active_producer is False
    assert registry.get_status(request_id) == "terminal"


@pytest.mark.anyio
async def test_answer_now_stream_mark_running_failure_is_failure_atomic():
    registry = _FailingMarkRunningRegistry()
    session = AgentSession(
        session_id="answer-now-startup-failure",
        agent=MagicMock(),
        mode="tool_calling",
    )
    source_closed = asyncio.Event()
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    runner.get_current_request_id.return_value = None

    async def replacement_stream(*args, **kwargs):
        replacement_scope = session.begin_run()
        try:
            yield (
                'data: {"type": "session_start", '
                '"session_id": "answer-now-startup-failure"}\n\n'
            )
            await asyncio.Event().wait()
        finally:
            session.finish_run(replacement_scope)
            source_closed.set()

    runner.run_answer_now_stream.side_effect = replacement_stream
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = registry
    request.is_disconnected = AsyncMock(return_value=False)
    response = await answer_now_stream(
        AgentAnswerNowStreamRequest(
            session_id=session.session_id,
            request_id="answer-now-startup-failure-01J0123456789",
        ),
        request,
    )
    received_request = False

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        return None

    with patch("backend.api.agent._abort_agent_session_request"):
        with pytest.raises(RuntimeError, match="mark_running failed"):
            await asyncio.wait_for(
                response({"type": "http", "method": "POST"}, receive, send),
                timeout=1,
            )

    assert source_closed.is_set()
    assert session.event_hub.has_active_producer is False
    assert registry.get_status("answer-now-startup-failure-01J0123456789") in {
        "terminal",
        "unknown",
    }
    reacquired = session.try_begin_answer_now()
    assert reacquired is True
    session.finish_answer_now()
    retry_token = session.event_hub.claim_producer()
    assert retry_token is not None
    session.event_hub.close("error", producer_token=retry_token)
    session.event_hub.release_producer(retry_token)
    await asyncio.sleep(0)
    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    assert pending == []


@pytest.mark.anyio
async def test_answer_now_stream_duplicate_waits_until_preaccept_reservation_resolves():
    registry = RequestLifecycleRegistry()
    source_entered = asyncio.Event()
    allow_registration = asyncio.Event()
    allow_completion = asyncio.Event()
    session = MagicMock()
    session.session_id = "agent-session"
    session.try_begin_answer_now.return_value = True
    session.capture_active_run_scopes.side_effect = [
        frozenset(),
        frozenset({"replacement-scope"}),
    ]
    session.wait_for_run_scopes.return_value = True
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    runner.get_current_request_id.return_value = None

    async def replacement_stream(*args, **kwargs):
        source_entered.set()
        await allow_registration.wait()
        yield 'data: {"type": "session_start", "session_id": "agent-session"}\n\n'
        await allow_completion.wait()
        yield 'data: {"type": "done", "session_id": "agent-session"}\n\n'

    runner.run_answer_now_stream.side_effect = replacement_stream
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = registry
    request.is_disconnected = AsyncMock(return_value=False)
    payload = AgentAnswerNowStreamRequest(
        session_id="agent-session",
        request_id=ANSWER_NOW_REQUEST_ID,
    )
    first_response = await answer_now_stream(payload, request)
    duplicate_response = await answer_now_stream(payload, request)
    first_sent = []
    duplicate_sent = []

    async def receive():
        await asyncio.Event().wait()

    async def send_first(message):
        first_sent.append(message)

    async def send_duplicate(message):
        duplicate_sent.append(message)

    with patch("backend.api.agent._abort_agent_session_request"):
        first_task = asyncio.create_task(
            first_response({"type": "http", "method": "POST"}, receive, send_first)
        )
        await asyncio.wait_for(source_entered.wait(), timeout=1)
        duplicate_task = asyncio.create_task(
            duplicate_response(
                {"type": "http", "method": "POST"}, receive, send_duplicate
            )
        )
        await asyncio.sleep(0.05)
        assert duplicate_task.done() is False

        allow_registration.set()
        await asyncio.wait_for(duplicate_task, timeout=1)
        duplicate_body = b"".join(
            message.get("body", b"") for message in duplicate_sent
        )
        assert json.loads(duplicate_body) == {
            "request_id": ANSWER_NOW_REQUEST_ID,
            "status": "running",
        }

        allow_completion.set()
        await asyncio.wait_for(first_task, timeout=1)

    assert runner.run_answer_now_stream.call_count == 1


@pytest.mark.anyio
async def test_answer_now_stream_duplicate_retries_after_preaccept_owner_abandons():
    registry = RequestLifecycleRegistry()
    first_source_entered = asyncio.Event()
    allow_first_abandon = asyncio.Event()
    session = AgentSession(
        session_id="agent-session", agent=MagicMock(), mode="tool_calling"
    )
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    runner.get_current_request_id.return_value = None
    attempts = 0

    async def replacement_stream(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_source_entered.set()
            await allow_first_abandon.wait()
            return
        replacement_scope = session.begin_run()
        try:
            yield 'data: {"type": "session_start", "session_id": "agent-session"}\n\n'
            yield 'data: {"type": "done", "session_id": "agent-session"}\n\n'
        finally:
            session.finish_run(replacement_scope)

    runner.run_answer_now_stream.side_effect = replacement_stream
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = registry
    request.is_disconnected = AsyncMock(return_value=False)
    payload = AgentAnswerNowStreamRequest(
        session_id="agent-session",
        request_id=ANSWER_NOW_REQUEST_ID,
    )
    first_response = await answer_now_stream(payload, request)
    duplicate_response = await answer_now_stream(payload, request)
    first_sent = []
    duplicate_sent = []

    async def receive():
        await asyncio.Event().wait()

    async def send_first(message):
        first_sent.append(message)

    async def send_duplicate(message):
        duplicate_sent.append(message)

    with patch("backend.api.agent._abort_agent_session_request"):
        first_task = asyncio.create_task(
            first_response({"type": "http", "method": "POST"}, receive, send_first)
        )
        await asyncio.wait_for(first_source_entered.wait(), timeout=1)
        duplicate_task = asyncio.create_task(
            duplicate_response(
                {"type": "http", "method": "POST"}, receive, send_duplicate
            )
        )
        await asyncio.sleep(0.05)
        assert duplicate_task.done() is False

        allow_first_abandon.set()
        await asyncio.wait_for(first_task, timeout=1)
        await asyncio.wait_for(duplicate_task, timeout=1)

    assert first_sent[0]["status"] == 409
    assert duplicate_sent[0]["status"] == 200
    duplicate_body = b"".join(
        message.get("body", b"") for message in duplicate_sent
    ).decode()
    assert '"type": "request_accepted"' in duplicate_body
    assert attempts == 2
    assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "terminal"


@pytest.mark.anyio
async def test_cancelled_duplicate_wait_thread_cannot_reserve_after_owner_abandons():
    registry = _ObservedWaitLifecycleRegistry()
    fingerprint = hashlib.sha256(
        json.dumps(
            {"session_id": "agent-session"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    original = registry.reserve(
        request_id=ANSWER_NOW_REQUEST_ID,
        request_kind="agent_answer_now",
        context_identity="agent-session",
        payload_fingerprint=fingerprint,
    )
    session = MagicMock()
    session.session_id = "agent-session"
    runner = MagicMock()
    runner.sessions.get.return_value = session
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = registry
    request.is_disconnected = AsyncMock(return_value=False)
    response = await answer_now_stream(
        AgentAnswerNowStreamRequest(
            session_id="agent-session",
            request_id=ANSWER_NOW_REQUEST_ID,
        ),
        request,
    )

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        return None

    duplicate_task = asyncio.create_task(
        response({"type": "http", "method": "POST"}, receive, send)
    )
    assert await asyncio.to_thread(registry.wait_started.wait, 1)
    duplicate_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await duplicate_task

    assert registry.abandon(original.registration_token) is True
    assert await asyncio.to_thread(registry.wait_finished.wait, 1)

    live_retry = registry.reserve(
        request_id=ANSWER_NOW_REQUEST_ID,
        request_kind="agent_answer_now",
        context_identity="agent-session",
        payload_fingerprint=fingerprint,
    )
    assert live_retry.should_start is True
    assert live_retry.registration_token is not original.registration_token
    assert registry.abandon(original.registration_token) is False
    assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "unknown"
    assert registry.abandon(live_retry.registration_token) is True


@pytest.mark.anyio
async def test_answer_now_stream_preaccept_cancellation_abandons_for_exact_retry():
    registry = RequestLifecycleRegistry()
    first_source_entered = asyncio.Event()
    allow_first_source_exit = asyncio.Event()
    first_source_closed = asyncio.Event()
    session = AgentSession(
        session_id="agent-session", agent=MagicMock(), mode="tool_calling"
    )
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    runner.get_current_request_id.return_value = None
    attempts = 0

    async def replacement_stream(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_source_entered.set()
            await allow_first_source_exit.wait()
            first_source_closed.set()
            return
        replacement_scope = session.begin_run()
        try:
            yield 'data: {"type": "session_start", "session_id": "agent-session"}\n\n'
            yield 'data: {"type": "done", "session_id": "agent-session"}\n\n'
        finally:
            session.finish_run(replacement_scope)

    runner.run_answer_now_stream.side_effect = replacement_stream
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = registry
    request.is_disconnected = AsyncMock(return_value=False)
    payload = AgentAnswerNowStreamRequest(
        session_id="agent-session",
        request_id=ANSWER_NOW_REQUEST_ID,
    )
    first_response = await answer_now_stream(payload, request)
    retry_response = await answer_now_stream(payload, request)

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        return None

    with patch("backend.api.agent._abort_agent_session_request"):
        first_task = asyncio.create_task(
            first_response({"type": "http", "method": "POST"}, receive, send)
        )
        await asyncio.wait_for(first_source_entered.wait(), timeout=1)
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task

        assert session.try_begin_answer_now() is False
        allow_first_source_exit.set()
        await asyncio.wait_for(first_source_closed.wait(), timeout=1)
        for _ in range(100):
            if session.try_begin_answer_now():
                session.finish_answer_now()
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("startup owner did not release single-flight")
        assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "unknown"
        await retry_response({"type": "http", "method": "POST"}, receive, send)

    assert attempts == 2
    assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "terminal"


def test_answer_now_stream_maps_lifecycle_conflict_and_capacity_generically():
    runner = MagicMock()
    session = MagicMock()
    session.session_id = "agent-session"
    runner.sessions.get.return_value = session
    conflict_registry = RequestLifecycleRegistry()
    conflict_registry.reserve(
        request_id=ANSWER_NOW_REQUEST_ID,
        request_kind="agent_answer_now",
        context_identity="different-session",
        payload_fingerprint=hashlib.sha256(b"different").hexdigest(),
    )
    conflict_client = TestClient(_answer_now_stream_app(runner, conflict_registry))
    conflict = conflict_client.post(
        "/v1/agent/answer-now/stream",
        json={"session_id": "agent-session", "request_id": ANSWER_NOW_REQUEST_ID},
    )

    capacity_registry = RequestLifecycleRegistry(capacity=1)
    capacity_registry.reserve(
        request_id="occupied-request-01J0123456789ABC",
        request_kind="agent_answer_now",
        context_identity="occupied-session",
        payload_fingerprint=hashlib.sha256(b"occupied").hexdigest(),
    )
    capacity_client = TestClient(_answer_now_stream_app(runner, capacity_registry))
    capacity = capacity_client.post(
        "/v1/agent/answer-now/stream",
        json={"session_id": "agent-session", "request_id": ANSWER_NOW_REQUEST_ID},
    )

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "request ID conflict"}
    assert capacity.status_code == 503
    assert capacity.json() == {"detail": "request lifecycle capacity reached"}
    runner.abort_session.assert_not_called()


@pytest.mark.anyio
async def test_answer_now_stream_body_noniteration_does_not_own_producer():
    registry = RequestLifecycleRegistry()
    source_closed = asyncio.Event()
    allow_source_completion = asyncio.Event()
    session = MagicMock()
    session.session_id = "agent-session"
    session.try_begin_answer_now.return_value = True
    session.capture_active_run_scopes.side_effect = [
        frozenset(),
        frozenset({"replacement-scope"}),
    ]
    session.wait_for_run_scopes.return_value = True
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    runner.get_current_request_id.return_value = None

    async def replacement_stream(*args, **kwargs):
        try:
            yield 'data: {"type": "session_start", "session_id": "agent-session"}\n\n'
            await allow_source_completion.wait()
            yield 'data: {"type": "done", "session_id": "agent-session"}\n\n'
        finally:
            source_closed.set()

    runner.run_answer_now_stream.side_effect = replacement_stream
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = registry
    request.is_disconnected = AsyncMock(return_value=False)
    response = await answer_now_stream(
        AgentAnswerNowStreamRequest(
            session_id="agent-session",
            request_id=ANSWER_NOW_REQUEST_ID,
        ),
        request,
    )

    class NeverIteratesBody:
        def __init__(self, *args, **kwargs):
            pass

        async def __call__(self, scope, receive, send):
            return None

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        return None

    with (
        patch("backend.api.agent._abort_agent_session_request"),
        patch("backend.api.agent.StreamingResponse", NeverIteratesBody),
    ):
        await response({"type": "http", "method": "POST"}, receive, send)

    assert source_closed.is_set() is False
    assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "running"
    allow_source_completion.set()
    await asyncio.wait_for(source_closed.wait(), timeout=1)
    for _ in range(100):
        if registry.get_status(ANSWER_NOW_REQUEST_ID) == "terminal":
            break
        await asyncio.sleep(0)
    assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "terminal"
    session.wait_for_run_scopes.assert_called_once_with(
        frozenset({"replacement-scope"})
    )


@pytest.mark.anyio
async def test_answer_now_stream_does_not_acquire_single_flight_before_body_iteration():
    session = MagicMock()
    session.session_id = "agent-session"
    runner = MagicMock()
    runner.sessions.get.return_value = session
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = RequestLifecycleRegistry()

    response = await answer_now_stream(
        AgentAnswerNowStreamRequest(
            session_id="agent-session",
            request_id=ANSWER_NOW_REQUEST_ID,
        ),
        request,
    )

    session.try_begin_answer_now.assert_not_called()
    await response.body_iterator.aclose()


@pytest.mark.anyio
async def test_answer_now_stream_duplicate_returns_http_409_from_asgi_lifecycle():
    session = MagicMock()
    session.session_id = "agent-session"
    session.try_begin_answer_now.return_value = False
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = RequestLifecycleRegistry()
    response = await answer_now_stream(
        AgentAnswerNowStreamRequest(
            session_id="agent-session",
            request_id=ANSWER_NOW_REQUEST_ID,
        ),
        request,
    )
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await response({"type": "http", "method": "POST"}, receive, send)

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 409
    runner.abort_session.assert_not_called()


@pytest.mark.anyio
async def test_answer_now_stream_immediate_disconnect_does_not_release_producer_early():
    allow_source_completion = asyncio.Event()
    source_finished = asyncio.Event()
    session = MagicMock()
    session.session_id = "agent-session"
    session.try_begin_answer_now.return_value = True
    session.capture_active_run_scopes.side_effect = [
        frozenset(),
        frozenset({"replacement-scope"}),
    ]
    session.wait_for_run_scopes.return_value = True
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)

    async def replacement_stream(*args, **kwargs):
        try:
            yield 'data: {"type": "session_start"}\n\n'
            await allow_source_completion.wait()
            yield 'data: {"type": "done"}\n\n'
        finally:
            source_finished.set()

    runner.run_answer_now_stream.side_effect = replacement_stream
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = RequestLifecycleRegistry()
    request.is_disconnected = AsyncMock(return_value=False)
    response = await answer_now_stream(
        AgentAnswerNowStreamRequest(
            session_id="agent-session",
            request_id=ANSWER_NOW_REQUEST_ID,
        ),
        request,
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        return None

    with patch("backend.api.agent._abort_agent_session_request"):
        await response({"type": "http", "method": "POST"}, receive, send)

    session.finish_answer_now.assert_not_called()
    allow_source_completion.set()
    await asyncio.wait_for(source_finished.wait(), timeout=1)
    for _ in range(100):
        if session.finish_answer_now.call_count == 1:
            break
        await asyncio.sleep(0)
    session.finish_answer_now.assert_called_once_with()


@pytest.mark.anyio
async def test_answer_now_stream_holds_single_flight_until_outer_worker_barrier_finishes():
    registry = _RecordingLifecycleRegistry([])
    source_closed = asyncio.Event()
    worker_released = threading.Event()
    allow_source_completion = asyncio.Event()
    session = AgentSession(
        session_id="agent-session", agent=MagicMock(), mode="tool_calling"
    )
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    runner.get_current_request_id.return_value = None

    async def replacement_stream(*args, **kwargs):
        replacement_scope = session.begin_run()
        try:
            yield 'data: {"type": "session_start", "session_id": "agent-session"}\n\n'
            await allow_source_completion.wait()
            yield 'data: {"type": "done", "session_id": "agent-session"}\n\n'
        finally:
            source_closed.set()

            def finish_worker():
                worker_released.wait(2)
                session.finish_run(replacement_scope)

            threading.Thread(target=finish_worker, daemon=True).start()

    runner.run_answer_now_stream.side_effect = replacement_stream
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = registry
    request.is_disconnected = AsyncMock(return_value=False)
    response = await answer_now_stream(
        AgentAnswerNowStreamRequest(
            session_id="agent-session",
            request_id=ANSWER_NOW_REQUEST_ID,
        ),
        request,
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        return None

    with patch("backend.api.agent._abort_agent_session_request"):
        response_task = asyncio.create_task(
            response({"type": "http", "method": "POST"}, receive, send)
        )
        await asyncio.wait_for(response_task, timeout=1)
        assert source_closed.is_set() is False
        assert session.try_begin_answer_now() is False
        assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "running"
        allow_source_completion.set()
        await asyncio.wait_for(source_closed.wait(), timeout=1)
        assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "running"
        worker_released.set()
        assert await asyncio.to_thread(registry.terminal_reached.wait, 1)

    assert registry.get_status(ANSWER_NOW_REQUEST_ID) == "terminal"
    assert session.try_begin_answer_now() is True
    session.finish_answer_now()


def test_duplicate_answer_now_returns_conflict_without_aborting_again():
    app = FastAPI()
    app.include_router(agent_router)
    app.include_router(_answer_now_router)
    session = MagicMock()
    session.try_begin_answer_now.return_value = False
    mock_runner = MagicMock()
    mock_runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(mock_runner, session)
    app.state.agent_runner = mock_runner

    response = TestClient(app).post(
        "/v1/agent/answer-now", json={"session_id": "agent-session"}
    )

    assert response.status_code == 409
    mock_runner.abort_session.assert_not_called()
    mock_runner.generate_answer_now_reply.assert_not_called()


@pytest.mark.anyio
async def test_answer_now_setup_exception_releases_single_flight_for_later_request():
    session = AgentSession(
        session_id="agent-session", agent=MagicMock(), mode="tool_calling"
    )
    session.capture_active_run_scopes = MagicMock(side_effect=RuntimeError("capture failed"))
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    request = MagicMock()
    request.app.state.agent_runner = runner

    with pytest.raises(RuntimeError, match="capture failed"):
        await answer_now(MagicMock(session_id=session.session_id), request)

    assert session.try_begin_answer_now() is True
    session.finish_answer_now()


@pytest.mark.anyio
async def test_answer_now_stream_setup_exception_releases_single_flight_for_later_request():
    session = AgentSession(
        session_id="agent-session", agent=MagicMock(), mode="tool_calling"
    )
    session.capture_active_run_scopes = MagicMock(side_effect=RuntimeError("capture failed"))
    runner = MagicMock()
    runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(runner, session)
    request = MagicMock()
    request.app.state.agent_runner = runner
    request.app.state.request_lifecycle_registry = RequestLifecycleRegistry()
    response = await answer_now_stream(
        AgentAnswerNowStreamRequest(
            session_id=session.session_id,
            request_id=ANSWER_NOW_REQUEST_ID,
        ),
        request,
    )

    received_request = False

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        return None

    with pytest.raises(RuntimeError, match="capture failed"):
        await response({"type": "http", "method": "POST"}, receive, send)

    assert session.try_begin_answer_now() is True
    session.finish_answer_now()


def test_answer_now_captures_scope_and_request_before_abort():
    app = FastAPI()
    app.include_router(agent_router)
    app.include_router(_answer_now_router)
    order = []
    session = MagicMock()
    session.try_begin_answer_now.return_value = True
    session.capture_active_run_scopes.side_effect = lambda: order.append("scopes") or frozenset()
    mock_runner = MagicMock()
    mock_runner.sessions.get.return_value = session
    _use_session_answer_now_acquisition(mock_runner, session)
    mock_runner.get_current_request_id.side_effect = lambda session_id: order.append("request") or None
    mock_runner.abort_session.side_effect = lambda session_id: order.append("abort") or True
    mock_runner.generate_answer_now_reply = AsyncMock(
        return_value={"status": "ok", "session_id": "agent-session", "answer": "final"}
    )
    app.state.agent_runner = mock_runner

    response = TestClient(app).post(
        "/v1/agent/answer-now", json={"session_id": "agent-session"}
    )

    assert response.status_code == 200
    assert order == ["scopes", "request", "abort"]


def test_stop_agent_stream_can_return_dispatch_aborted_event():
    app = FastAPI()
    app.include_router(agent_router)

    mock_runner = MagicMock()
    mock_runner.get_or_create_session.return_value = MagicMock()

    async def _stream():
        yield 'data: {"type": "session_start", "session_id": "agent-session"}\n\n'
        yield 'data: {"type": "agent_step", "step_type": "dispatch", "dispatch_status": "aborted", "dispatch_stage": "routing", "dispatch_message": "任务调度已中止"}\n\n'
        yield 'data: {"type": "aborted", "session_id": "agent-session"}\n\n'
        yield 'data: {"type": "done", "session_id": "agent-session"}\n\n'

    mock_runner.run_stream.return_value = _stream()
    app.state.agent_runner = mock_runner

    client = TestClient(app)
    with client.stream(
        "POST",
        "/v1/agent/chat",
        json={
            "session_id": "agent-session",
            "message": "停止这个请求",
            "stream": True,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"dispatch_status": "aborted"' in body
    assert '"dispatch_message": "任务调度已中止"' in body


def test_stop_all_agents_uses_runner_abort_session_for_each_session():
    app = FastAPI()
    app.include_router(agent_router)

    session_a = MagicMock()
    session_b = MagicMock()

    mock_runner = MagicMock()
    mock_runner.sessions.session_ids.return_value = ("session-a", "session-b")
    app.state.agent_runner = mock_runner

    client = TestClient(app)

    with (
        patch("backend.config.get_config") as mock_get_config,
        patch("backend.llm_service.abort_sglang_all") as mock_abort_all,
        patch("backend.llm_service.active_upstream_request_ids", return_value=()),
        patch("backend.llm_service.get_active_backend", return_value="sglang"),
    ):
        mock_get_config.return_value = MagicMock(
            sglang=MagicMock(host="127.0.0.1", port=30000)
        )

        response = client.post("/v1/agent/stop-all")

    assert response.status_code == 200
    assert response.json() == {
        "status": "stopped_all",
        "stopped_sessions": ["session-a", "session-b"],
    }
    mock_runner.abort_session.assert_any_call("session-a")
    mock_runner.abort_session.assert_any_call("session-b")
    assert mock_runner.abort_session.call_count == 2
    mock_abort_all.assert_called_once_with("http://127.0.0.1:30000")


def test_stop_all_closes_each_active_vllm_response_without_sglang_abort_all():
    app = FastAPI()
    app.include_router(agent_router)
    mock_runner = MagicMock()
    mock_runner.sessions.session_ids.return_value = ("session-a",)
    app.state.agent_runner = mock_runner
    client = TestClient(app)

    with (
        patch("backend.config.get_config") as mock_get_config,
        patch(
            "backend.llm_service.active_request_ids",
            return_value=("req-a", "req-b"),
        ),
        patch("backend.llm_service.abort_request_by_id") as abort_by_id,
        patch("backend.llm_service.abort_sglang_all") as abort_all,
        patch("backend.llm_service.get_active_backend", return_value="vllm"),
    ):
        mock_get_config.return_value = MagicMock(
            sglang=MagicMock(host="127.0.0.1", port=30000)
        )
        response = client.post("/v1/agent/stop-all")

    assert response.status_code == 200
    assert [call.args[0] for call in abort_by_id.call_args_list] == ["req-a", "req-b"]
    assert all(call.kwargs["fallback_to_abort_all"] is False for call in abort_by_id.call_args_list)
    abort_all.assert_not_called()


def test_stop_all_vllm_records_pending_abort_before_response_registration():
    from backend.llm_service import (
        is_request_aborted,
        register_request,
        register_upstream_stream,
        unregister_request,
        unregister_upstream_stream,
    )

    app = FastAPI()
    app.include_router(agent_router)
    runner = MagicMock()
    runner.sessions.session_ids.return_value = ()
    app.state.agent_runner = runner
    request_id = "req-vllm-registration-race"
    registration = register_request(request_id)
    closer = MagicMock()

    try:
        with (
            patch("backend.config.get_config") as config,
            patch("backend.llm_service.get_active_backend", return_value="vllm"),
        ):
            config.return_value = MagicMock(sglang=MagicMock(host="127.0.0.1", port=30000))
            response = TestClient(app).post("/v1/agent/stop-all")

        assert response.status_code == 200
        assert is_request_aborted(request_id)
        upstream_registration = register_upstream_stream(request_id, "vllm", closer)
        closer.assert_called_once_with()
        unregister_upstream_stream(request_id, upstream_registration)
    finally:
        unregister_request(request_id, registration)


def test_agent_chat_returns_409_when_session_already_has_active_run():
    app = FastAPI()
    app.include_router(agent_router)

    mock_runner = MagicMock()
    mock_runner.run_sync = AsyncMock(side_effect=RuntimeError("Session busy-session already has an active run"))
    app.state.agent_runner = mock_runner

    client = TestClient(app)

    response = client.post(
        "/v1/agent/chat",
        json={
            "session_id": "busy-session",
            "message": "hello",
            "stream": False,
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Session busy-session already has an active run"}


def test_child_agent_stream_route_exists_and_unknown_child_returns_404():
    app = FastAPI()
    app.include_router(agent_router)

    route_paths = {getattr(route, "path", None) for route in agent_router.routes}
    assert "/v1/agent/child/stream" in route_paths

    mock_runner = MagicMock()
    mock_runner.get_child_agent_run.side_effect = KeyError("missing-child")
    app.state.agent_runner = mock_runner

    client = TestClient(app)

    response = client.get(
        "/v1/agent/child/stream",
        params={"child_run_id": "missing-child"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Child agent run not found"}
    mock_runner.get_child_agent_run.assert_called_once_with("missing-child")
    mock_runner.stream_child_agent_events.assert_not_called()


def test_answer_now_routes_are_not_publicly_exposed():
    route_paths = {getattr(route, "path", None) for route in agent_router.routes}

    assert "/v1/agent/answer-now" not in route_paths
    assert "/v1/agent/answer-now/stream" not in route_paths


@pytest.mark.asyncio
async def test_child_agent_stream_is_unrecoverable_after_manager_trash_disposal():
    runner = AgentRunner(MagicMock(), deepcopy(get_config()))
    manager = AgentSession(
        session_id="http-replay-manager", agent=MagicMock(), mode="tool_calling"
    )
    child = AgentSession(
        session_id="http-replay-child", agent=MagicMock(), mode="tool_calling"
    )
    runner.sessions.put(manager)
    runner.sessions.put(child)
    child_run = ChildAgentRun(
        child_run_id=child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="http-replay-dispatch",
        child_name="reader",
        profile_name="basic_agent",
        assigned_task="Replay this completed child transcript.",
        call_id="http-replay-call",
        status="completed",
        final_report="HTTP retained report",
        completed_at=datetime.now(),
        child_session_identity=child,
    )
    child_run.terminal_event.set()
    runner.child_agent_runs[child_run.child_run_id] = child_run
    runner.child_agent_sessions_by_key[(manager.session_id, child_run.child_name)] = (
        child.session_id
    )
    await runner.dispose_session(manager.session_id)

    raw_request = MagicMock()
    raw_request.app.state.agent_runner = runner
    with pytest.raises(HTTPException) as exc_info:
        await child_agent_stream(child_run.child_run_id, raw_request)
    assert exc_info.value.status_code == 404

    assert child.event_hub.released is True
    assert child.event_hub.journal_size == 0
    assert child_run.child_run_id not in runner.child_agent_runs
    assert child_run.child_run_id not in runner._retained_child_replay_ids
