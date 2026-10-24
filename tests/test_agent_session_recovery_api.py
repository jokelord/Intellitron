import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.agent.agent_runner import AgentSession, ChildAgentRun, SessionStore
from backend.api.agent import (
    AgentChatRequest,
    _encode_agent_hub_record,
    _publish_turn_started_payload,
    _resolve_agent_session_hub_entry,
    _turn_started_payload,
    agent_chat,
    child_agent_stream,
    agent_session_events,
    router as agent_router,
)
from backend.request_lifecycle import RequestLifecycleRegistry
from backend.session_hub_runtime import SessionHubRuntime
from backend.vision.contracts import AgentRequestAdmissionStage


def _sse_payloads(messages):
    payloads = []
    for message in messages:
        if message.get("type") != "http.response.body":
            continue
        text = message.get("body", b"").decode()
        for block in text.split("\n\n"):
            if block.startswith("data: "):
                payloads.append(json.loads(block[6:]))
    return payloads


def _request_scope(app, *, method="POST", path="/v1/agent/chat"):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
        "app": app,
    }


class _DeterministicTopLevelRunner:
    def __init__(self, profile_name):
        self.sessions = SessionStore()
        agent = SimpleNamespace(
            memory=SimpleNamespace(steps=["canonical-memory"]),
            state={"canonical": "state"},
        )
        self.session = AgentSession(
            session_id=f"session-{profile_name}",
            agent=agent,
            mode="tool_calling",
            current_agent_name=profile_name,
        )
        self.sessions.put(self.session)
        self.release = asyncio.Event()
        self.source_started = asyncio.Event()
        self.source_closed = asyncio.Event()
        self.producer_reached_terminal = asyncio.Event()
        self.source_finally_count = 0
        self.received_run_kwargs = None
        self.rollback_calls = []
        self.pending_stage = None

    def stage_agent_request_admission(
        self,
        session_id,
        mode,
        metadata,
        inbound_image_count=None,
    ):
        assert session_id == self.session.session_id
        assert inbound_image_count is None or inbound_image_count >= 0
        assert self.pending_stage is None
        stage = AgentRequestAdmissionStage(
            session_id=session_id,
            projected_historical_image_count=0,
            vision_automation_metadata=metadata,
            _session=self.session,
        )
        self.pending_stage = stage
        return stage

    def rollback_agent_request_stage(self, stage):
        assert self.pending_stage is stage
        stage._rolled_back = True
        self.pending_stage = None
        self.rollback_calls.append(stage)

    def capture_agent_request_run_scopes(self, stage):
        return stage._session.capture_active_run_scopes()

    async def wait_for_agent_request_run_cleanup(self, stage, scope_ids):
        if scope_ids:
            await asyncio.to_thread(stage._session.wait_for_run_scopes, scope_ids)

    async def run_stream(self, session_id, message, mode, **kwargs):
        self.received_run_kwargs = {
            "session_id": session_id,
            "message": message,
            "mode": mode,
            **kwargs,
        }
        stage = kwargs["admission_stage"]
        scope_id = self.session.begin_run()
        try:
            kwargs["on_run_registered"](stage, frozenset({scope_id}))
            stage._accepted = True
            self.pending_stage = None
            self.source_started.set()
            yield "data: " + json.dumps(
                {"type": "session_start", "session_id": session_id}
            ) + "\n\n"
            await self.release.wait()
            yield 'data: {"type": "content", "content": "complete"}\n\n'
            yield "data: " + json.dumps(
                {"type": "done", "session_id": session_id}
            ) + "\n\n"
            self.producer_reached_terminal.set()
        finally:
            self.source_finally_count += 1
            self.source_closed.set()
            self.session.finish_run(scope_id)


async def _start_chat_response(
    runner,
    *,
    images=None,
    message="visible user request",
    request_suffix="0001",
    registry=None,
    runtime=None,
):
    app = FastAPI()
    app.include_router(agent_router)
    registry = registry or RequestLifecycleRegistry()
    runtime = runtime or SessionHubRuntime()
    app.state.agent_runner = runner
    app.state.request_lifecycle_registry = registry
    app.state.session_hub_runtime = runtime
    scope = _request_scope(app)
    raw_request = Request(scope)
    request_id = f"request-{runner.session.current_agent_name}-{request_suffix}"
    response = await agent_chat(
        AgentChatRequest(
            session_id=runner.session.session_id,
            message=message,
            images=images,
            mode="tool_calling",
            stream=True,
            request_id=request_id,
        ),
        raw_request,
    )
    return response, scope, registry, request_id


async def _consume_response_body(response):
    return [chunk async for chunk in response.body_iterator]


def test_strict_agent_publisher_refuses_unregistered_runtime_fallback():
    runner = _DeterministicTopLevelRunner("basic_agent")
    with pytest.raises(RuntimeError, match="shared Session Hub runtime"):
        _resolve_agent_session_hub_entry(runner, runner.session, None)


@pytest.mark.anyio
async def test_nonstream_request_cancellation_keeps_exact_hub_producer_recoverable():
    class DetachedSyncRunner(_DeterministicTopLevelRunner):
        async def run_sync(self, session_id, message, mode, **kwargs):
            stage = kwargs["admission_stage"]
            scope_id = self.session.begin_run()
            try:
                kwargs["on_run_registered"](stage, frozenset({scope_id}))
                stage._accepted = True
                self.pending_stage = None
                self.source_started.set()
                await self.release.wait()
                return {"session_id": session_id, "answer": "detached answer", "steps": []}
            finally:
                self.source_closed.set()
                self.session.finish_run(scope_id)

    runner = DetachedSyncRunner("basic_agent")
    runtime = SessionHubRuntime()
    registry = RequestLifecycleRegistry()
    app = FastAPI()
    app.state.agent_runner = runner
    app.state.request_lifecycle_registry = registry
    app.state.session_hub_runtime = runtime
    request_id = "request-basic_agent-nonstream-detached"
    task = asyncio.create_task(agent_chat(
        AgentChatRequest(
            session_id=runner.session.session_id,
            message="detached request",
            mode="tool_calling",
            stream=False,
            request_id=request_id,
        ),
        Request(_request_scope(app)),
    ))
    await asyncio.wait_for(runner.source_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry.get_status(request_id) == "running"
    assert runner.session.event_hub.has_active_producer is True
    runner.release.set()
    await asyncio.wait_for(runner.source_closed.wait(), timeout=1)
    for _ in range(100):
        if registry.get_status(request_id) == "terminal":
            break
        await asyncio.sleep(0)
    entry = runtime.resolve(runner.session.session_id)
    assert entry is not None
    payloads = [dict(record.payload) async for record in entry.hub.subscribe(0)]
    assert [payload["type"] for payload in payloads] == [
        "request_accepted", "timeline_event", "session_start", "content", "done"
    ]
    assert payloads[3]["content"] == "detached answer"
    assert payloads[-1]["status"] == "completed"
    assert registry.get_status(request_id) == "terminal"


@pytest.mark.anyio
async def test_child_route_uses_runtime_entry_and_shared_subscriber_only():
    runtime = SessionHubRuntime()
    child = AgentSession(
        session_id="shared-child-session",
        agent=MagicMock(),
        mode="tool_calling",
        is_child_context=True,
    )
    store = SessionStore(session_hub_runtime=runtime)
    assert store.put_if_absent(child) is True
    child_run = ChildAgentRun(
        child_run_id=child.session_id,
        manager_session_id="manager",
        dispatch_id="dispatch",
        child_name="reader",
        profile_name="basic_agent",
        assigned_task="Inspect the independent source carefully.",
        child_session_identity=child,
    )
    runner = MagicMock()
    runner.sessions = store
    runner.get_child_agent_run.return_value = child_run
    app = FastAPI()
    app.state.agent_runner = runner
    app.state.session_hub_runtime = runtime
    entry = runtime.resolve(child.session_id)
    called = []

    async def shared_subscriber(actual_entry, cursor):
        called.append((actual_entry, cursor))
        yield 'data: {"type":"done","event_sequence":1}\n\n'

    with patch("backend.api.agent.subscribe_session_hub", new=shared_subscriber):
        response = await child_agent_stream(
            child_run.child_run_id,
            Request(_request_scope(app, method="GET", path="/v1/agent/child/stream")),
        )
        frames = [frame async for frame in response.body_iterator]
    assert called == [(entry, 0)]
    assert frames == ['data: {"type":"done","event_sequence":1}\n\n']


@pytest.mark.anyio
async def test_agent_stream_registers_exact_existing_hub_in_shared_runtime():
    runner = _DeterministicTopLevelRunner("basic_agent")
    runtime = SessionHubRuntime()
    response, _scope, registry, request_id = await _start_chat_response(
        runner,
        runtime=runtime,
    )
    runner.release.set()
    await asyncio.wait_for(_consume_response_body(response), timeout=1)

    entry = runtime.resolve(runner.session.session_id)
    assert entry is not None
    assert entry.kind == "agent"
    assert entry.hub is runner.session.event_hub
    assert registry.get_status(request_id) == "terminal"


@pytest.mark.anyio
async def test_agent_shared_publisher_compensates_one_shot_acceptance_append_failure():
    runner = _DeterministicTopLevelRunner("basic_agent")
    runtime = SessionHubRuntime()
    response, _scope, registry, request_id = await _start_chat_response(
        runner,
        runtime=runtime,
    )
    real_append_batch = runner.session.event_hub.append_batch
    append_attempts = 0

    def fail_once_then_append(*args, **kwargs):
        nonlocal append_attempts
        append_attempts += 1
        if append_attempts == 1:
            raise RuntimeError("one-shot Agent acceptance append failure")
        return real_append_batch(*args, **kwargs)

    runner.session.event_hub.append_batch = fail_once_then_append
    runner.release.set()
    await asyncio.wait_for(_consume_response_body(response), timeout=1)
    payloads = [dict(item.payload) for item in runner.session.event_hub._journal]

    assert [payload["type"] for payload in payloads] == [
        "request_accepted",
        "timeline_event",
        "error",
        "done",
    ]
    assert payloads[1]["event"]["kind"] == "turn_started"
    assert payloads[-1]["status"] == "error"
    assert registry.get_status(request_id) == "terminal"
    assert registry._records[request_id].outcome == "error"


@pytest.mark.anyio
async def test_legacy_agent_recovery_is_thin_shared_runtime_subscriber_adapter():
    runner = _DeterministicTopLevelRunner("basic_agent")
    runtime = SessionHubRuntime()
    entry = runtime.register(
        session_id=runner.session.session_id,
        kind="agent",
        hub=runner.session.event_hub,
    )
    app = FastAPI()
    app.state.agent_runner = runner
    app.state.session_hub_runtime = runtime
    called = []

    async def shared_subscriber(actual_entry, cursor):
        called.append((actual_entry, cursor))
        yield 'data: {"type":"done","event_sequence":1}\n\n'

    with patch("backend.api.agent.subscribe_session_hub", new=shared_subscriber):
        response = await agent_session_events(
            runner.session.session_id,
            Request(_request_scope(app, method="GET")),
            after_sequence="0",
        )
        frames = [frame async for frame in response.body_iterator]

    assert called == [(entry, 0)]
    assert frames == ['data: {"type":"done","event_sequence":1}\n\n']


async def _assert_subsequent_turn_succeeds(runner, registry, suffix):
    runner.release.set()
    response, _scope, _registry, request_id = await _start_chat_response(
        runner,
        registry=registry,
        request_suffix=suffix,
    )
    body = "".join(await asyncio.wait_for(_consume_response_body(response), timeout=1))
    assert '"type": "done"' in body
    assert runner.session.event_hub.has_active_producer is False
    assert registry.get_status(request_id) == "terminal"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "profile_name",
    ["coordinator_1", "manager_agent", "basic_agent", "future_profile"],
)
async def test_initiating_subscriber_detach_does_not_cancel_producer(profile_name):
    runner = _DeterministicTopLevelRunner(profile_name)
    response, scope, registry, request_id = await _start_chat_response(runner)
    acceptance_sent = asyncio.Event()
    received_request = False
    messages = []

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await acceptance_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if any(
            payload.get("type") == "request_accepted"
            for payload in _sse_payloads([message])
        ):
            acceptance_sent.set()

    with patch("backend.llm_service.abort_request_by_id") as upstream_abort:
        await asyncio.wait_for(response(scope, receive, send), timeout=1)

        assert runner.session.aborted is False
        assert upstream_abort.call_count == 0
        assert runner.source_closed.is_set() is False
        assert runner.producer_reached_terminal.is_set() is False
        assert runner.session.event_hub.subscriber_count == 0
        assert registry.get_status(request_id) == "running"

        runner.release.set()
        await asyncio.wait_for(runner.producer_reached_terminal.wait(), timeout=1)
        await asyncio.wait_for(runner.source_closed.wait(), timeout=1)
        for _ in range(100):
            if registry.get_status(request_id) == "terminal":
                break
            await asyncio.sleep(0)

    assert registry.get_status(request_id) == "terminal"
    assert runner.session.aborted is False
    assert upstream_abort.call_count == 0
    assert runner.source_finally_count == 1


@pytest.mark.anyio
async def test_turn_started_and_sequences_are_outbound_only():
    runner = _DeterministicTopLevelRunner("basic_agent")
    original_memory = list(runner.session.agent.memory.steps)
    original_state = dict(runner.session.agent.state)
    runner.session.last_llm_prompt_snapshot = {"prompt": "canonical"}
    display_image = "data:image/png;base64,AAAA"
    with patch(
        "backend.api.agent.validate_agent_vision_request",
        return_value=SimpleNamespace(image_count=1),
    ):
        response, scope, registry, request_id = await _start_chat_response(
            runner,
            images=[display_image],
        )
    messages = []
    received_request = False

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    runner.release.set()
    await asyncio.wait_for(response(scope, receive, send), timeout=1)
    payloads = _sse_payloads(messages)

    assert [payload["event_sequence"] for payload in payloads] == list(
        range(1, len(payloads) + 1)
    )
    turn_started = next(
        payload
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "turn_started"
    )
    assert turn_started["event"] == {
        "kind": "turn_started",
        "session_id": runner.session.session_id,
        "turn_index": 1,
            "user_content": "visible user request",
            "images": [display_image],
            "mode": "tool_calling",
            "historical_image_omission": False,
        }
    assert runner.received_run_kwargs["message"] == "visible user request"
    assert runner.received_run_kwargs["images"] == [display_image]
    assert "turn_started" not in json.dumps(
        runner.received_run_kwargs, default=str
    )
    assert runner.session.agent.memory.steps == original_memory
    assert runner.session.agent.state == original_state
    assert runner.session.last_llm_prompt_snapshot == {"prompt": "canonical"}
    assert runner.session.timeline_events == []
    assert registry.get_status(request_id) == "terminal"


def test_turn_started_marks_only_a_sticky_top_level_session_for_ui_history_omission():
    runner = _DeterministicTopLevelRunner("basic_agent")
    runner.session.vision_automation_image_omission_sticky = True

    payload = _turn_started_payload(
        runner.session,
        "visible Vision user request",
        ["data:image/png;base64,AAAA"],
    )

    assert payload["event"]["historical_image_omission"] is True
    assert "vision_automation" not in json.dumps(payload["event"])


def test_sticky_turn_started_evicts_prior_hub_image_assets_without_rewriting_records():
    session = AgentSession(
        session_id="sticky-image-assets",
        agent=MagicMock(),
        mode="tool_calling",
    )
    first_image = "data:image/jpeg;base64," + "A" * 200_001
    latest_image = "data:image/jpeg;base64," + "B" * 200_001
    token = session.event_hub.claim_producer()
    assert token is not None
    first = _publish_turn_started_payload(
        session,
        _turn_started_payload(session, "first", [first_image]),
        [first_image],
        lambda payload: session.event_hub.append(
            payload, producer_token=token,
        ),
    )
    first_live = json.loads(_encode_agent_hub_record(first, session)[6:])
    assert first_live["event"]["images"] == [first_image]

    session.vision_automation_image_omission_sticky = True
    second = _publish_turn_started_payload(
        session,
        _turn_started_payload(session, "second", [latest_image]),
        [latest_image],
        lambda payload: session.event_hub.append(
            payload, producer_token=token,
        ),
    )
    assert second.sequence == first.sequence + 1

    retained = json.dumps([first.payload, second.payload])
    assert first_image not in retained
    assert latest_image not in retained
    first_replay = json.loads(_encode_agent_hub_record(first, session)[6:])
    second_replay = json.loads(_encode_agent_hub_record(second, session)[6:])
    assert first_replay["event"]["images"] == []
    assert "historical images omitted: count=1" in first_replay["event"]["historical_image_placeholder"]
    assert second_replay["event"]["images"] == [latest_image]

    third = _publish_turn_started_payload(
        session,
        _turn_started_payload(session, "third text only", []),
        [],
        lambda payload: session.event_hub.append(
            payload, producer_token=token,
        ),
    )
    second_after_zero_image = json.loads(_encode_agent_hub_record(second, session)[6:])
    third_replay = json.loads(_encode_agent_hub_record(third, session)[6:])
    assert second_after_zero_image["event"]["images"] == []
    assert third_replay["event"]["images"] == []
    assert session.vision_event_image_assets == {}


def test_nonsticky_vision_turns_keep_their_session_owned_assets_recoverable():
    session = AgentSession(
        session_id="ordinary-image-assets",
        agent=MagicMock(),
        mode="tool_calling",
    )
    first_image = "data:image/jpeg;base64,ordinary-first"
    second_image = "data:image/jpeg;base64,ordinary-second"
    token = session.event_hub.claim_producer()
    first = _publish_turn_started_payload(
        session,
        _turn_started_payload(session, "first", [first_image]),
        [first_image],
        lambda payload: session.event_hub.append(
            payload, producer_token=token,
        ),
    )
    second = _publish_turn_started_payload(
        session,
        _turn_started_payload(session, "second", [second_image]),
        [second_image],
        lambda payload: session.event_hub.append(
            payload, producer_token=token,
        ),
    )

    assert json.loads(_encode_agent_hub_record(first, session)[6:])["event"]["images"] == [first_image]
    assert json.loads(_encode_agent_hub_record(second, session)[6:])["event"]["images"] == [second_image]


def test_failed_turn_started_publication_restores_prior_sticky_assets_exactly():
    session = AgentSession(
        session_id="asset-publication-rollback",
        agent=MagicMock(),
        mode="tool_calling",
    )
    first_image = "data:image/jpeg;base64,first-retained"
    first_payload = _turn_started_payload(session, "first", [first_image])
    token = session.event_hub.claim_producer()
    assert token is not None
    first_record = _publish_turn_started_payload(
        session,
        first_payload,
        [first_image],
        lambda payload: session.event_hub.append(
            payload, producer_token=token,
        ),
    )
    before_assets = dict(session.vision_event_image_assets)
    before_turns = dict(session.vision_event_turn_assets)
    session.vision_automation_image_omission_sticky = True
    second_image = "data:image/jpeg;base64,second-new"
    second_payload = _turn_started_payload(session, "second", [second_image])

    def fail_before_publication(_payload):
        # Staging has committed under the outer asset lock before ordinary Hub
        # append is attempted, so the old record is already omitted here.
        prior = json.loads(_encode_agent_hub_record(first_record, session)[6:])
        assert prior["event"]["images"] == []
        raise RuntimeError("second append failed")

    with pytest.raises(RuntimeError, match="second append failed"):
        _publish_turn_started_payload(
            session,
            second_payload,
            [second_image],
            fail_before_publication,
        )

    assert session.vision_event_image_assets == before_assets
    assert session.vision_event_turn_assets == before_turns
    assert second_image not in session.vision_event_image_assets.values()
    assert session.event_hub.last_sequence == first_record.sequence


def test_partial_event_asset_commit_restores_both_maps_inside_publication_barrier():
    class FailOnceDict(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.failed = False

        def update(self, *args, **kwargs):
            super().update(*args, **kwargs)
            if not self.failed:
                self.failed = True
                raise RuntimeError("turn asset update failed")

    session = AgentSession(
        session_id="partial-event-asset-rollback",
        agent=MagicMock(),
        mode="tool_calling",
    )
    session.vision_event_image_assets = {"old": "data:image/jpeg;base64,old"}
    session.vision_event_turn_assets = FailOnceDict({1: ("old",)})
    before_assets = dict(session.vision_event_image_assets)
    before_turns = dict(session.vision_event_turn_assets)
    token = session.event_hub.claim_producer()
    assert token is not None
    session.vision_automation_image_omission_sticky = True
    payload = _turn_started_payload(session, "new", ["data:image/jpeg;base64,new"])

    with pytest.raises(RuntimeError, match="turn asset update failed"):
        _publish_turn_started_payload(
            session,
            payload,
            ["data:image/jpeg;base64,new"],
            lambda payload: session.event_hub.append(
                payload, producer_token=token,
            ),
        )

    assert session.vision_event_image_assets == before_assets
    assert session.vision_event_turn_assets == before_turns
    assert session.event_hub.last_sequence == 0


def test_turn_started_asset_visibility_barrier_blocks_old_record_encoding_until_outcome():
    """Asset readers cannot observe the staged map between append and rollback."""

    def prepare(session_id: str):
        session = AgentSession(
            session_id=session_id,
            agent=MagicMock(),
            mode="tool_calling",
        )
        token = session.event_hub.claim_producer()
        assert token is not None
        old_image = f"data:image/jpeg;base64,{session_id}-old"
        old_record = _publish_turn_started_payload(
            session,
            _turn_started_payload(session, "old", [old_image]),
            [old_image],
            lambda payload: session.event_hub.append(
                payload, producer_token=token,
            ),
        )
        session.vision_automation_image_omission_sticky = True
        latest_image = f"data:image/jpeg;base64,{session_id}-latest"
        next_payload = _turn_started_payload(session, "latest", [latest_image])
        return session, token, old_image, old_record, latest_image, next_payload

    def encode_old(session, record, done, images):
        payload = json.loads(_encode_agent_hub_record(record, session)[6:])
        images.append(payload["event"]["images"])
        done.set()

    # A failed append must keep the old record resolvable throughout the
    # publication attempt, not only after its caller has rolled back.
    (
        failed_session,
        failed_token,
        failed_old_image,
        failed_old_record,
        _failed_latest,
        failed_payload,
    ) = prepare("asset-visibility-failure")
    entered_failure = threading.Event()
    release_failure = threading.Event()
    failed_done = threading.Event()
    failed_images: list[list[str]] = []
    failures: list[BaseException] = []

    def failing_publish(_payload):
        entered_failure.set()
        assert release_failure.wait(timeout=2)
        raise RuntimeError("injected append failure")

    def publish_failure():
        try:
            _publish_turn_started_payload(
                failed_session,
                failed_payload,
                [_failed_latest],
                failing_publish,
            )
        except BaseException as error:
            failures.append(error)

    publisher = threading.Thread(target=publish_failure)
    publisher.start()
    assert entered_failure.wait(timeout=2)
    reader = threading.Thread(
        target=encode_old,
        args=(failed_session, failed_old_record, failed_done, failed_images),
    )
    reader.start()
    try:
        assert not failed_done.wait(timeout=0.15)
    finally:
        release_failure.set()
        publisher.join(timeout=2)
        reader.join(timeout=2)
    assert not publisher.is_alive()
    assert not reader.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert failed_images == [[failed_old_image]]
    assert failed_session.event_hub.last_sequence == failed_old_record.sequence

    # A successful append has the inverse atomic result: the reader remains
    # blocked until publication, then sees the old record omitted and the
    # current record's assets resolvable at its assigned sequence.
    (
        success_session,
        success_token,
        _success_old_image,
        success_old_record,
        success_latest,
        success_payload,
    ) = prepare("asset-visibility-success")
    entered_success = threading.Event()
    release_success = threading.Event()
    success_done = threading.Event()
    success_images: list[list[str]] = []
    published: list[object] = []

    def successful_publish(payload):
        entered_success.set()
        assert release_success.wait(timeout=2)
        return success_session.event_hub.append(payload, producer_token=success_token)

    def publish_success():
        published.append(
            _publish_turn_started_payload(
                success_session,
                success_payload,
                [success_latest],
                successful_publish,
            )
        )

    publisher = threading.Thread(target=publish_success)
    publisher.start()
    assert entered_success.wait(timeout=2)
    reader = threading.Thread(
        target=encode_old,
        args=(success_session, success_old_record, success_done, success_images),
    )
    reader.start()
    try:
        assert not success_done.wait(timeout=0.15)
    finally:
        release_success.set()
        publisher.join(timeout=2)
        reader.join(timeout=2)
    assert not publisher.is_alive()
    assert not reader.is_alive()
    assert success_images == [[]]
    assert len(published) == 1
    success_record = published[0]
    assert success_record.sequence == success_old_record.sequence + 1
    assert success_session.event_hub.last_sequence == success_record.sequence
    latest_payload = json.loads(_encode_agent_hub_record(success_record, success_session)[6:])
    assert latest_payload["event"]["images"] == [success_latest]


def test_retained_sessions_endpoint_lists_top_level_only():
    top = AgentSession(
        session_id="top-1",
        agent=MagicMock(),
        mode="tool_calling",
    )
    child = AgentSession(
        session_id="child-1",
        agent=MagicMock(),
        mode="tool_calling",
        is_child_context=True,
    )
    runner = MagicMock()
    runtime = SessionHubRuntime()
    runner.sessions = SessionStore(session_hub_runtime=runtime)
    runner.sessions.put(child)
    runner.sessions.put(top)
    app = FastAPI()
    app.include_router(agent_router)
    app.state.agent_runner = runner
    app.state.session_hub_runtime = runtime

    response = TestClient(app).get("/v1/agent/sessions/retained")

    assert response.status_code == 200
    body = response.json()
    assert [entry["session_id"] for entry in body["sessions"]] == ["top-1"]
    assert body["sessions"][0]["run_in_progress"] is False
    assert "last_activity" in body["sessions"][0]


def test_recovery_endpoint_replays_terminal_journal_with_sequences():
    session = AgentSession(
        session_id="terminal-recovery",
        agent=MagicMock(),
        mode="tool_calling",
    )
    token = session.event_hub.claim_producer()
    first = session.event_hub.append({"type": "content", "content": "one"}, producer_token=token)
    second = session.event_hub.append({"type": "done"}, producer_token=token)
    session.event_hub.close("completed", producer_token=token)
    session.event_hub.release_producer(token)
    runner = MagicMock()
    runtime = SessionHubRuntime()
    runner.sessions = SessionStore(session_hub_runtime=runtime)
    runner.sessions.put(session)
    app = FastAPI()
    app.include_router(agent_router)
    app.state.agent_runner = runner
    app.state.session_hub_runtime = runtime

    response = TestClient(app).get(
        "/v1/agent/session/terminal-recovery/events?after_sequence=0"
    )

    assert response.status_code == 200
    payloads = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert payloads == [
        {"type": "content", "content": "one", "event_sequence": first.sequence},
        {"type": "done", "event_sequence": second.sequence},
    ]
    assert "event_sequence" not in first.payload
    assert "event_sequence" not in second.payload


@pytest.mark.anyio
async def test_recovery_replay_to_live_cursor_race_is_gap_free():
    session = AgentSession(
        session_id="live-recovery",
        agent=MagicMock(),
        mode="tool_calling",
    )
    token = session.event_hub.claim_producer()
    session.event_hub.append({"type": "content", "content": "one"}, producer_token=token)
    runner = MagicMock()
    runtime = SessionHubRuntime()
    runner.sessions = SessionStore(session_hub_runtime=runtime)
    runner.sessions.put(session)
    app = FastAPI()
    app.include_router(agent_router)
    app.state.agent_runner = runner
    app.state.session_hub_runtime = runtime
    path = "/v1/agent/session/live-recovery/events"
    scope = _request_scope(app, method="GET", path=path)
    raw_request = Request(scope)
    response = await agent_session_events(
        "live-recovery", raw_request, after_sequence=0
    )
    messages = []
    first_sent = asyncio.Event()
    received_request = False

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)
        if _sse_payloads([message]):
            first_sent.set()

    response_task = asyncio.create_task(response(scope, receive, send))
    await asyncio.wait_for(first_sent.wait(), timeout=1)
    session.event_hub.append({"type": "content", "content": "two"}, producer_token=token)
    session.event_hub.append({"type": "done"}, producer_token=token)
    session.event_hub.close("completed", producer_token=token)
    session.event_hub.release_producer(token)
    await asyncio.wait_for(response_task, timeout=1)

    payloads = _sse_payloads(messages)
    assert [payload["event_sequence"] for payload in payloads] == [1, 2, 3]
    assert [payload["content"] for payload in payloads[:2]] == ["one", "two"]


def test_recovery_endpoint_validates_cursor_and_unknown_session():
    runner = MagicMock()
    runtime = SessionHubRuntime()
    runner.sessions = SessionStore(session_hub_runtime=runtime)
    app = FastAPI()
    app.include_router(agent_router)
    app.state.agent_runner = runner
    app.state.session_hub_runtime = runtime
    client = TestClient(app)

    assert client.get(
        "/v1/agent/session/missing/events?after_sequence=-1"
    ).status_code == 422
    assert client.get(
        "/v1/agent/session/missing/events?after_sequence=abc"
    ).status_code == 422
    assert client.get(
        "/v1/agent/session/missing/events?after_sequence=" + "9" * 5000
    ).status_code == 422
    missing = client.get(
        "/v1/agent/session/missing/events?after_sequence=0"
    )
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Agent session not found or expired"}


@pytest.mark.anyio
async def test_post_captures_pre_turn_cursor_without_replaying_older_record():
    runner = _DeterministicTopLevelRunner("basic_agent")
    old = runner.session.event_hub.append(
        {"type": "content", "content": "older retained turn"}
    )
    response, scope, _registry, _request_id = await _start_chat_response(runner)
    messages = []
    received_request = False

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    runner.release.set()
    await asyncio.wait_for(response(scope, receive, send), timeout=1)
    payloads = _sse_payloads(messages)

    assert old.sequence == 1
    assert [payload["event_sequence"] for payload in payloads] == list(
        range(2, 2 + len(payloads))
    )
    assert all(payload.get("content") != "older retained turn" for payload in payloads)


@pytest.mark.anyio
async def test_sequential_turns_append_turn_indexes_without_public_run_id():
    runner = _DeterministicTopLevelRunner("coordinator_1")

    async def complete_turn(message, suffix):
        response, scope, _registry, _request_id = await _start_chat_response(
            runner,
            message=message,
            request_suffix=suffix,
        )
        messages = []
        received_request = False

        async def receive():
            nonlocal received_request
            if not received_request:
                received_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def send(event):
            messages.append(event)

        await asyncio.wait_for(response(scope, receive, send), timeout=1)
        return _sse_payloads(messages)

    runner.release.set()
    first_payloads = await complete_turn("first visible request", "turn0001")
    second_payloads = await complete_turn("second visible request", "turn0002")
    combined = first_payloads + second_payloads
    starts = [
        payload["event"]
        for payload in combined
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "turn_started"
    ]

    assert [event["turn_index"] for event in starts] == [1, 2]
    assert [event["user_content"] for event in starts] == [
        "first visible request",
        "second visible request",
    ]
    assert all("run_id" not in payload for payload in combined)
    assert all("run_id" not in event for event in starts)
    assert runner.session.event_hub.last_sequence == len(combined)


@pytest.mark.anyio
async def test_producer_failure_publishes_one_error_and_one_done():
    runner = _DeterministicTopLevelRunner("future_profile")

    async def failing_stream(session_id, message, mode, **kwargs):
        stage = kwargs["admission_stage"]
        scope_id = runner.session.begin_run()
        kwargs["on_run_registered"](stage, frozenset({scope_id}))
        try:
            yield "data: " + json.dumps(
                {"type": "session_start", "session_id": session_id}
            ) + "\n\n"
            raise RuntimeError("deterministic producer failure")
        finally:
            runner.source_finally_count += 1
            runner.session.finish_run(scope_id)

    runner.run_stream = failing_stream
    response, scope, registry, request_id = await _start_chat_response(runner)
    messages = []
    received_request = False

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(response(scope, receive, send), timeout=1)
    payloads = _sse_payloads(messages)

    assert [payload["type"] for payload in payloads].count("error") == 1
    assert [payload["type"] for payload in payloads].count("done") == 1
    error = next(payload for payload in payloads if payload["type"] == "error")
    done = next(payload for payload in payloads if payload["type"] == "done")
    assert error["error"] == "deterministic producer failure"
    assert done["status"] == "error"
    assert runner.source_finally_count == 1
    assert registry.get_status(request_id) == "terminal"


@pytest.mark.anyio
async def test_barrier_failure_after_source_done_replaces_source_terminal_status():
    runner = _DeterministicTopLevelRunner("basic_agent")

    async def fail_after_barrier(stage, scope_ids):
        await _DeterministicTopLevelRunner.wait_for_agent_request_run_cleanup(
            runner, stage, scope_ids
        )
        raise RuntimeError("deterministic post-done barrier failure")

    runner.wait_for_agent_request_run_cleanup = fail_after_barrier
    runner.release.set()
    response, scope, registry, request_id = await _start_chat_response(runner)
    messages = []
    received_request = False

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(response(scope, receive, send), timeout=1)
    payloads = _sse_payloads(messages)
    terminal_payloads = [
        payload for payload in payloads if payload.get("type") in {"error", "done"}
    ]

    assert [payload["type"] for payload in terminal_payloads] == ["error", "done"]
    assert terminal_payloads[0]["error"] == "deterministic post-done barrier failure"
    assert terminal_payloads[1]["status"] == "error"
    assert registry.get_status(request_id) == "terminal"


@pytest.mark.anyio
async def test_registry_terminal_failure_still_releases_hub_producer():
    class RaisingTerminalRegistry(RequestLifecycleRegistry):
        def mark_terminal(self, reservation_token, outcome):
            raise RuntimeError("deterministic registry terminal failure")

    runner = _DeterministicTopLevelRunner("basic_agent")
    runner.release.set()
    registry = RaisingTerminalRegistry()
    response, scope, _registry, _request_id = await _start_chat_response(
        runner,
        registry=registry,
    )
    messages = []
    received_request = False

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(response(scope, receive, send), timeout=1)
    for _ in range(20):
        if not runner.session.event_hub.has_active_producer:
            break
        await asyncio.sleep(0)

    assert any(payload.get("type") == "done" for payload in _sse_payloads(messages))
    assert runner.session.event_hub.has_active_producer is False


@pytest.mark.anyio
@pytest.mark.parametrize("fail_after_mutation", [False, True])
async def test_registry_accept_failure_is_startup_atomic_for_existing_session(
    fail_after_mutation,
):
    class FailOnceAcceptRegistry(RequestLifecycleRegistry):
        def __init__(self):
            super().__init__()
            self.failed = False

        def accept(self, reservation_token):
            if not self.failed and not fail_after_mutation:
                self.failed = True
                raise RuntimeError("accept failed before mutation")
            super().accept(reservation_token)
            if not self.failed:
                self.failed = True
                raise RuntimeError("accept failed after mutation")

    runner = _DeterministicTopLevelRunner("basic_agent")
    original_session = runner.session
    registry = FailOnceAcceptRegistry()
    response, _scope, _registry, request_id = await _start_chat_response(
        runner,
        registry=registry,
        request_suffix="acceptfail0001",
    )

    with pytest.raises(RuntimeError, match="accept failed"):
        await asyncio.wait_for(_consume_response_body(response), timeout=0.5)

    assert runner.sessions.get(original_session.session_id) is original_session
    assert runner.session.event_hub.has_active_producer is False
    assert registry.get_status(request_id) not in {"accepted", "running"}
    assert len(runner.rollback_calls) == 1
    assert runner.rollback_calls[0]._rolled_back is True
    assert runner.pending_stage is None
    await _assert_subsequent_turn_succeeds(
        runner, registry, "acceptfail-recovery-0002"
    )


@pytest.mark.anyio
async def test_registry_mark_running_failure_is_startup_atomic_for_existing_session():
    class FailOnceRunningRegistry(RequestLifecycleRegistry):
        def __init__(self):
            super().__init__()
            self.failed = False

        def mark_running(self, reservation_token):
            if not self.failed:
                self.failed = True
                raise RuntimeError("mark running failed after accept")
            super().mark_running(reservation_token)

    runner = _DeterministicTopLevelRunner("basic_agent")
    original_session = runner.session
    registry = FailOnceRunningRegistry()
    response, _scope, _registry, request_id = await _start_chat_response(
        runner,
        registry=registry,
        request_suffix="runningfail0001",
    )

    with pytest.raises(RuntimeError, match="mark running failed after accept"):
        await asyncio.wait_for(_consume_response_body(response), timeout=0.5)

    assert runner.sessions.get(original_session.session_id) is original_session
    assert runner.session.event_hub.has_active_producer is False
    assert registry.get_status(request_id) not in {"accepted", "running"}
    assert len(runner.rollback_calls) == 1
    assert runner.rollback_calls[0]._rolled_back is True
    assert runner.pending_stage is None
    await _assert_subsequent_turn_succeeds(
        runner, registry, "runningfail-recovery-0002"
    )


@pytest.mark.anyio
async def test_first_hub_append_failure_resolves_startup_and_allows_next_turn():
    runner = _DeterministicTopLevelRunner("basic_agent")
    original_session = runner.session
    registry = RequestLifecycleRegistry()
    original_append = runner.session.event_hub.append
    append_calls = 0

    def fail_first_append(payload, *, producer_token=None):
        nonlocal append_calls
        append_calls += 1
        if append_calls == 1:
            raise RuntimeError("first startup Hub append failed")
        return original_append(payload, producer_token=producer_token)

    with patch.object(runner.session.event_hub, "append", fail_first_append):
        response, _scope, _registry, request_id = await _start_chat_response(
            runner,
            registry=registry,
            request_suffix="appendfail0001",
        )
        with pytest.raises(RuntimeError, match="first startup Hub append failed"):
            await asyncio.wait_for(_consume_response_body(response), timeout=0.5)

    assert runner.sessions.get(original_session.session_id) is original_session
    assert runner.session.event_hub.has_active_producer is False
    assert registry.get_status(request_id) not in {"accepted", "running"}
    assert len(runner.rollback_calls) == 1
    assert runner.rollback_calls[0]._rolled_back is True
    assert runner.pending_stage is None
    await _assert_subsequent_turn_succeeds(
        runner, registry, "appendfail-recovery-0002"
    )
