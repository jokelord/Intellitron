from copy import deepcopy
from datetime import datetime
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch

import pytest
import backend.agent.agent_runner as agent_runner_module
from smolagents.memory import ActionStep
from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
)
from smolagents.monitoring import Timing

from backend.agent.agent_runner import (
    AgentRunner,
    AgentSession,
    ChildAgentRun,
    ManagerChildDispatchOutcome,
    PROFILE_TOOL_ALLOWLIST,
    SessionBusyError,
    abort_manager,
    session_request_manager,
)
from backend.config import get_config
from backend.security.capabilities import build_session_capability
from backend.agent.orchestrator import ToolResult
from backend.agent.text_tool_protocol import TextToolProtocolError
from backend.session_hub_runtime import SessionHubRuntime


def _runner(limit: int = 4) -> AgentRunner:
    config = deepcopy(get_config())
    config.model.model_type = "text"
    config.agent.entry_agent_name = "manager_agent"
    config.agent.enabled_agent_profiles = ["manager_agent", "basic_agent"]
    config.agent.manager_agent_max_parallel_children = limit
    config.agent.manager_agent_child_event_buffer_limit = 5000
    return AgentRunner(
        MagicMock(),
        config,
        session_hub_runtime=SessionHubRuntime(),
    )


def _session(*args, **kwargs) -> AgentSession:
    if args and isinstance(args[0], AgentRunner) and "agent" not in kwargs:
        runner = args[0]
        session_id = (
            kwargs.get("session_id")
            or (args[1] if len(args) > 1 else "manager-session")
        )
        manager = MagicMock()
        manager.memory.steps = []
        session = _session(
            session_id=session_id,
            agent=manager,
            mode="tool_calling",
            tool_configuration_snapshot=runner.default_tool_configuration_snapshot,
            current_agent_name="manager_agent",
        )
        runner.sessions.put(session)
        return session
    session_id = kwargs.get("session_id", args[0] if args else None)
    kwargs.setdefault(
        "capability",
        build_session_capability(
            get_config(), session_id=session_id, mode=kwargs.get("mode", "tool_calling")
        ),
    )
    return AgentSession(*args, **kwargs)


def _child_hub_payloads(runner: AgentRunner, child_run: ChildAgentRun) -> list[dict]:
    session = runner.sessions.get(child_run.child_run_id)
    assert session is not None
    return [dict(record.payload) for record in session.event_hub._journal]


@pytest.mark.asyncio
async def test_child_run_uses_one_canonical_session_identity():
    runner = _runner()
    child_id = "canonical-child"
    child_session = _session(
        session_id=child_id,
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    runner.sessions.put(child_session)
    child_run = ChildAgentRun(
        child_run_id=child_id,
        manager_session_id="manager-session",
        dispatch_id="dispatch-1",
        child_name="reader",
        profile_name="basic_agent",
        assigned_task="Inspect the independent source carefully.",
        child_session_identity=child_session,
    )
    runner.child_agent_runs[child_id] = child_run
    token = child_session.event_hub.claim_producer()
    assert token is not None
    child_session.event_hub.append(
        {"type": "done", "child_run_id": child_id},
        producer_token=token,
    )
    child_session.event_hub.close("completed", producer_token=token)
    child_session.event_hub.release_producer(token)

    assert runner.get_child_agent_run(child_run.child_run_id) is child_run
    assert child_run.child_run_id == child_session.session_id
    replay = "".join(
        [event async for event in runner.stream_child_agent_events(child_run.child_run_id)]
    )
    assert '"child_run_id": "canonical-child"' in replay


def test_stale_child_run_object_cannot_append_to_replacement_producer():
    runner = _runner()
    child_id = "stable-child"
    child_session = _session(
        session_id=child_id,
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    runner.sessions.put(child_session)
    old_run = ChildAgentRun(
        child_run_id=child_id,
        manager_session_id="manager-session",
        dispatch_id="dispatch-old",
        child_name="reader",
        profile_name="basic_agent",
        assigned_task="Inspect the old independent source carefully.",
        child_session_identity=child_session,
    )
    old_token = child_session.event_hub.claim_producer()
    assert old_token is not None
    old_run.event_hub_producer_token = old_token
    child_session.event_hub.close("completed", producer_token=old_token)
    assert child_session.event_hub.release_producer(old_token) is True

    replacement_run = ChildAgentRun(
        child_run_id=child_id,
        manager_session_id="manager-session",
        dispatch_id="dispatch-new",
        child_name="reader",
        profile_name="basic_agent",
        assigned_task="Inspect the replacement independent source carefully.",
        child_session_identity=child_session,
    )
    replacement_token = child_session.event_hub.claim_producer()
    assert replacement_token is not None
    replacement_run.event_hub_producer_token = replacement_token
    runner.child_agent_runs[child_id] = replacement_run

    with pytest.raises(RuntimeError, match="stale"):
        runner._record_child_agent_event(
            old_run,
            {"type": "content", "content": "stale payload"},
        )

    assert not any(
        record.payload.get("content") == "stale payload"
        for record in child_session.event_hub._journal
    )
    assert child_session.event_hub.capture_active_producer() is replacement_token
    child_session.event_hub.close("completed", producer_token=replacement_token)
    child_session.event_hub.release_producer(replacement_token)


def _call(call_id: str, name: str, arguments: dict) -> ChatMessageToolCall:
    return ChatMessageToolCall(
        id=call_id,
        type="function",
        function=ChatMessageToolCallFunction(name=name, arguments=arguments),
    )


def _step() -> ActionStep:
    return ActionStep(step_number=1, timing=Timing(start_time=0.0, end_time=0.1))


def _tool_block(name: str, arguments: str) -> str:
    return (
        f'<<TOOL_CALL>>\n{{"name":"{name}","arguments":{arguments}}}\n'
        "<<END_TOOL_CALL>>"
    )


def test_manager_allowlist_has_only_singular_dispatch_name():
    assert "dispatching_parallel_agent" in PROFILE_TOOL_ALLOWLIST["manager_agent"]
    assert "dispatching_parallel_agents" not in PROFILE_TOOL_ALLOWLIST["manager_agent"]


def test_child_events_have_no_duplicate_retained_buffer_or_subscriber_registry():
    runner = _runner()
    assert not hasattr(runner, "child_agent_event_buffers")
    assert not hasattr(runner, "child_agent_subscribers")


def test_dispatching_parallel_agent_schema_is_singular_and_minimal():
    runner = _runner()
    agent = runner._build_profile_agent("manager_agent", session=_session(runner))

    assert "dispatching_parallel_agent" in agent.tools
    assert "dispatching_parallel_agents" not in agent.tools
    tool = agent.tools["dispatching_parallel_agent"]
    assert set(tool.inputs) == {"child_id", "task"}
    assert tool.inputs["child_id"]["type"] == "string"
    assert tool.inputs["task"]["type"] == "string"


def test_dispatching_parallel_agent_wire_schema_rejects_extra_arguments():
    runner = _runner()
    agent = runner._build_profile_agent("manager_agent", session=_session(runner))
    runner.llm_service.chat.return_value = MagicMock(text="ok", usage=None)

    agent.model.generate(
        [ChatMessage(role=MessageRole.USER, content="delegate this task")],
        tools_to_call_from=agent.tools_and_managed_agents,
    )

    tools = runner.llm_service.chat.call_args.kwargs["tools"]
    dispatch_schema = next(
        tool for tool in tools
        if tool["function"]["name"] == "dispatching_parallel_agent"
    )
    parameters = dispatch_schema["function"]["parameters"]
    assert parameters["additionalProperties"] is False


@pytest.mark.parametrize(
    ("child_id", "task", "message"),
    [
        (" x ", "A sufficiently detailed task briefing.", "trimmed"),
        ("ok", "A sufficiently detailed task briefing.", "3..128"),
        ("valid-child", "too short", "10 non-space"),
    ],
)
def test_dispatch_validation_is_strict(child_id, task, message):
    with pytest.raises(ValueError, match=message):
        _runner()._validate_manager_child_dispatch(child_id, task)


def test_dispatch_tool_rejects_removed_extra_fields():
    runner = _runner()
    tool = runner._build_profile_agent(
        "manager_agent", session=_session(runner)
    ).tools["dispatching_parallel_agent"]

    with pytest.raises(TypeError):
        tool.forward(
            child_id="reader-a",
            task="Read source A carefully and report findings.",
            may_write=False,
        )


def test_manager_prompt_uses_singular_parallel_future_turn_contract():
    runner = _runner(limit=2)
    prompt = runner._build_profile_agent(
        "manager_agent", session=_session(runner)
    ).initialize_system_prompt()

    assert '<<TOOL_CALL>>\n{\n  "name": "dispatching_parallel_agent"' in prompt
    assert '    "child_id": "researcher-a"' in prompt
    assert '    "child_id": "researcher-b"' in prompt
    assert prompt.count("<<END_TOOL_CALL>>") >= 3
    assert "dispatching_parallel_agents" not in prompt
    assert '"subtasks"' not in prompt
    assert "may_write" not in prompt
    assert "reserved_files" not in prompt
    assert "future turn" in prompt.lower()
    assert "run concurrently" in prompt.lower()
    assert "maximum of 2" in prompt


@pytest.mark.asyncio
async def test_singular_dispatch_runs_exactly_one_child():
    runner = _runner()
    session = _session(runner)
    runner._run_child_agent_to_final_report = MagicMock()

    async def run_child(child_run, child_session):
        return "poem report"

    runner._run_child_agent_to_final_report = run_child
    outcome = await runner._dispatching_parallel_agent_for_session(
        session,
        "poet_a",
        "Compose one classical poem and return the requested format.",
        turn_dispatch_id="turn-dispatch-1",
        call_id="call_1",
    )

    assert outcome == ManagerChildDispatchOutcome(
        call_id="call_1",
        child_id="poet_a",
        status="completed",
        final_report="poem report",
    )
    assert len(runner.child_agent_runs) == 1


@pytest.mark.asyncio
async def test_distinct_children_enter_runner_concurrently_without_manager_lock():
    runner = _runner()
    session = _session(runner)
    child_a_entered = threading.Event()
    child_b_entered = threading.Event()
    release = threading.Event()

    async def run_child(child_run, child_session):
        if child_run.child_name == "child-a":
            child_a_entered.set()
        else:
            child_b_entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return f"report {child_run.child_name}"

    runner._run_child_agent_to_final_report = run_child
    first_task = asyncio.create_task(
        runner._dispatching_parallel_agent_for_session(
            session, "child-a", "Investigate independent source A carefully.",
            turn_dispatch_id="turn-1", call_id="call_1"
        )
    )
    second_task: asyncio.Task | None = None
    try:
        deadline = asyncio.get_running_loop().time() + 1
        while not child_a_entered.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert child_a_entered.is_set()

        second_task = asyncio.create_task(
            runner._dispatching_parallel_agent_for_session(
                session, "child-b", "Investigate independent source B carefully.",
                turn_dispatch_id="turn-1", call_id="call_2"
            )
        )
        deadline = asyncio.get_running_loop().time() + 1
        while not child_b_entered.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert child_b_entered.is_set()
        assert first_task.done() is False
        release.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(first_task, second_task), timeout=2
        )
    finally:
        release.set()
        if second_task is not None and not second_task.done():
            second_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second_task
        if not first_task.done():
            await asyncio.wait_for(first_task, timeout=2)

    assert [item.status for item in outcomes] == ["completed", "completed"]


@pytest.mark.asyncio
async def test_child_timeout_waits_for_worker_barrier_and_targets_request():
    runner = _runner()
    runner.config.agent.manager_agent_child_timeout_seconds = 0.01
    manager_session = _session(runner, "timeout-manager")
    child_name = "timeout-child"
    stable_id = runner._make_child_identity_id(manager_session.session_id, child_name)
    worker_gate = threading.Event()
    worker_started = threading.Event()
    worker_finished = threading.Event()
    worker_threads: list[threading.Thread] = []
    session_request_manager.set(stable_id, "req-child-timeout")

    async def blocked_child(child_run, child_session):
        scope_id = child_session.begin_run()

        def finish_scope() -> None:
            worker_started.set()
            worker_gate.wait(2)
            child_session.finish_run(scope_id)
            worker_finished.set()

        worker = threading.Thread(target=finish_scope, daemon=True)
        worker_threads.append(worker)
        worker.start()
        await asyncio.sleep(30)

    runner._run_child_agent_to_final_report = blocked_child
    dispatch_task = asyncio.create_task(
        runner._dispatching_parallel_agent_for_session(
            manager_session,
            child_name,
            "Investigate the timeout barrier source carefully.",
            turn_dispatch_id="timeout-dispatch",
            call_id="timeout-call",
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while not worker_started.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert worker_started.is_set()
        child_run = runner.child_agent_runs[stable_id]
        child_session = runner.sessions.get(stable_id)
        assert child_session is not None

        with patch("backend.llm_service.abort_request_by_id") as targeted_abort:
            deadline = asyncio.get_running_loop().time() + 2
            while (
                not child_session.aborted or not targeted_abort.called
            ) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)

            assert dispatch_task.done() is False
            assert child_session.run_in_progress is True
            assert child_session.event_hub.has_active_producer is True
            assert child_run.terminal_event.is_set() is False
            assert not any(
                record.payload.get("type") in {"done", "error", "aborted"}
                for record in child_session.event_hub._journal
            )
            targeted_abort.assert_called_once_with(
                "req-child-timeout",
                f"http://{runner.config.sglang.host}:{runner.config.sglang.port}",
                fallback_to_abort_all=False,
            )

            worker_gate.set()
            worker_threads[0].join(2)
            assert worker_finished.is_set()
            outcome = await asyncio.wait_for(dispatch_task, timeout=2)

        assert outcome.status == "timeout"
        assert child_session.capture_active_run_scopes() == frozenset()
        assert child_session.run_in_progress is False
        assert child_session.event_hub.has_active_producer is False
        assert child_run.terminal_event.is_set() is True
        terminal = _child_hub_payloads(runner, child_run)[-1]
        expected_timeout_message = (
            "Child agent timed out after "
            f"{runner.config.agent.manager_agent_child_timeout_seconds:g} seconds"
        )
        assert terminal["type"] == "error"
        assert terminal["status"] == "timeout"
        assert terminal["child_run_id"] == stable_id
        assert terminal["call_id"] == "timeout-call"
        assert terminal["error"] == expected_timeout_message
    finally:
        worker_gate.set()
        for worker in worker_threads:
            worker.join(2)
        session_request_manager.clear(stable_id, "req-child-timeout")
        if not dispatch_task.done():
            dispatch_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await dispatch_task


@pytest.mark.asyncio
async def test_same_child_busy_admission_preserves_old_abort_and_producer():
    runner = _runner()
    manager_session = _session(runner, "busy-manager")
    child_name = "busy-child"
    stable_id = runner._make_child_identity_id(manager_session.session_id, child_name)
    worker_gate = threading.Event()
    worker_started = threading.Event()
    worker_finished = threading.Event()
    worker_threads: list[threading.Thread] = []

    async def old_child(child_run, child_session):
        scope_id = child_session.begin_run()

        def finish_scope() -> None:
            worker_started.set()
            worker_gate.wait(2)
            child_session.finish_run(scope_id)
            worker_finished.set()

        worker = threading.Thread(target=finish_scope, daemon=True)
        worker_threads.append(worker)
        worker.start()
        while not worker_finished.is_set():
            await asyncio.sleep(0.01)
        return "old report"

    runner._run_child_agent_to_final_report = old_child
    old_dispatch = asyncio.create_task(
        runner._dispatching_parallel_agent_for_session(
            manager_session,
            child_name,
            "Run the original busy child task independently and report.",
            turn_dispatch_id="old-dispatch",
            call_id="old-call",
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while not worker_started.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert worker_started.is_set()
        old_run = runner.child_agent_runs[stable_id]
        child_session = runner.sessions.get(stable_id)
        assert child_session is not None
        old_token = old_run.event_hub_producer_token
        assert old_token is not None
        child_session.abort()
        abort_manager.abort(stable_id)

        with pytest.raises(SessionBusyError):
            await runner._dispatching_parallel_agent_for_session(
                manager_session,
                child_name,
                "Attempt a same-child redispatch while the old run is active.",
                turn_dispatch_id="new-dispatch",
                call_id="new-call",
            )

        assert child_session.check_aborted() is True
        assert abort_manager.is_aborted(stable_id) is True
        assert runner.child_agent_runs[stable_id] is old_run
        assert old_run.event_hub_producer_token is old_token
        assert child_session.event_hub.capture_active_producer() is old_token
        assert child_session.run_in_progress is True
        assert child_session.capture_active_run_scopes()
    finally:
        worker_gate.set()
        for worker in worker_threads:
            worker.join(2)
        if not old_dispatch.done():
            await asyncio.wait_for(old_dispatch, timeout=2)
        session_request_manager.cleanup(stable_id)
        abort_manager.cleanup(stable_id)


@pytest.mark.asyncio
async def test_streamed_child_failure_uses_barrier_only_without_abort_mutation():
    runner = _runner()
    manager_session = _session(runner, "stream-error-manager")
    child_name = "stream-error-child"
    stable_id = runner._make_child_identity_id(manager_session.session_id, child_name)

    async def streamed_error(child_session, *_args, **_kwargs):
        scope_id = child_session.begin_run()
        try:
            child_session.finish_run(scope_id)
            yield f'data: {json.dumps({"type": "error", "error": "upstream ended"})}\n\n'
        finally:
            pass

    runner._run_basic_stream = streamed_error
    with patch("backend.llm_service.abort_request_by_id") as targeted_abort:
        outcome = await runner._dispatching_parallel_agent_for_session(
            manager_session,
            child_name,
            "Investigate the naturally terminal streamed failure carefully.",
            turn_dispatch_id="stream-error-dispatch",
            call_id="stream-error-call",
        )

    child_run = runner.child_agent_runs[stable_id]
    child_session = runner.sessions.get(stable_id)
    assert child_session is not None
    assert outcome.status == "failed"
    assert child_run.status == "failed"
    assert child_session.check_aborted() is False
    assert abort_manager.is_aborted(stable_id) is False
    targeted_abort.assert_not_called()
    assert child_session.capture_active_run_scopes() == frozenset()
    assert child_session.event_hub.has_active_producer is False
    assert child_run.terminal_event.is_set() is True


@pytest.mark.asyncio
async def test_second_cancellation_during_child_barrier_still_settles_terminal_state():
    runner = _runner()
    runner.config.agent.manager_agent_child_timeout_seconds = 0.01
    manager_session = _session(runner, "second-cancel-manager")
    child_name = "second-cancel-child"
    stable_id = runner._make_child_identity_id(manager_session.session_id, child_name)
    worker_gate = threading.Event()
    worker_started = threading.Event()
    worker_finished = threading.Event()
    barrier_entered = threading.Event()
    worker_threads: list[threading.Thread] = []

    async def blocked_child(child_run, child_session):
        scope_id = child_session.begin_run()

        def finish_scope() -> None:
            worker_started.set()
            worker_gate.wait(2)
            child_session.finish_run(scope_id)
            worker_finished.set()

        worker = threading.Thread(target=finish_scope, daemon=True)
        worker_threads.append(worker)
        worker.start()
        await asyncio.sleep(30)

    runner._run_child_agent_to_final_report = blocked_child
    original_wait = runner._wait_for_child_run_quiescence

    async def observed_wait(child_run, child_session, *, request_abort=True):
        barrier_entered.set()
        return await original_wait(
            child_run,
            child_session,
            request_abort=request_abort,
        )

    runner._wait_for_child_run_quiescence = observed_wait
    dispatch_task = asyncio.create_task(
        runner._dispatching_parallel_agent_for_session(
            manager_session,
            child_name,
            "Investigate the second cancellation barrier carefully.",
            turn_dispatch_id="second-cancel-dispatch",
            call_id="second-cancel-call",
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while not worker_started.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert worker_started.is_set()
        child_run = runner.child_agent_runs[stable_id]
        child_session = runner.sessions.get(stable_id)
        assert child_session is not None

        deadline = asyncio.get_running_loop().time() + 2
        while not barrier_entered.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert barrier_entered.is_set()
        assert dispatch_task.done() is False
        dispatch_task.cancel()
        await asyncio.sleep(0.05)
        assert dispatch_task.done() is False
        assert child_run.terminal_event.is_set() is False
        assert child_session.event_hub.has_active_producer is True
        assert child_session.capture_active_run_scopes()

        worker_gate.set()
        worker_threads[0].join(2)
        assert worker_finished.is_set()
        try:
            outcome = await asyncio.wait_for(dispatch_task, timeout=2)
        except asyncio.CancelledError:
            pytest.fail("second cancellation escaped before child settlement")

        assert outcome.status == "timeout"
        assert child_session.capture_active_run_scopes() == frozenset()
        assert child_session.run_in_progress is False
        assert child_session.event_hub.has_active_producer is False
        assert child_run.terminal_event.is_set() is True

        runner._run_child_agent_to_final_report = AsyncMock(return_value="fresh report")
        fresh = await runner._dispatching_parallel_agent_for_session(
            manager_session,
            child_name,
            "Retry the same child identity after complete settlement.",
            turn_dispatch_id="second-cancel-retry",
            call_id="second-cancel-retry-call",
        )
        assert fresh.status == "completed"
    finally:
        worker_gate.set()
        for worker in worker_threads:
            worker.join(2)
        if not dispatch_task.done():
            dispatch_task.cancel()
            try:
                await dispatch_task
            except asyncio.CancelledError:
                pass
        session_request_manager.cleanup(stable_id)
        abort_manager.cleanup(stable_id)


@pytest.mark.asyncio
async def test_child_targeted_abort_runs_off_event_loop_and_keeps_heartbeat_alive():
    runner = _runner()
    runner.config.agent.manager_agent_child_timeout_seconds = 0.01
    manager_session = _session(runner, "slow-abort-manager")
    child_name = "slow-abort-child"
    stable_id = runner._make_child_identity_id(manager_session.session_id, child_name)
    worker_gate = threading.Event()
    worker_started = threading.Event()
    worker_finished = threading.Event()
    abort_started = threading.Event()
    abort_finished = threading.Event()
    abort_release = threading.Event()
    worker_threads: list[threading.Thread] = []
    abort_thread_ids: list[int] = []
    heartbeat_ticks: list[float] = []
    session_request_manager.set(stable_id, "req-slow-abort")

    async def blocked_child(child_run, child_session):
        scope_id = child_session.begin_run()

        def finish_scope() -> None:
            worker_started.set()
            worker_gate.wait(2)
            child_session.finish_run(scope_id)
            worker_finished.set()

        worker = threading.Thread(target=finish_scope, daemon=True)
        worker_threads.append(worker)
        worker.start()
        await asyncio.sleep(30)

    runner._run_child_agent_to_final_report = blocked_child

    def slow_abort(*_args, **_kwargs):
        abort_thread_ids.append(threading.get_ident())
        abort_started.set()
        abort_release.wait(2)
        abort_finished.set()

    async def heartbeat() -> None:
        while True:
            heartbeat_ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.01)

    abort_patcher = patch(
        "backend.llm_service.abort_request_by_id", side_effect=slow_abort
    )
    targeted_abort = abort_patcher.start()
    heartbeat_task = asyncio.create_task(heartbeat())
    dispatch_task = asyncio.create_task(
        runner._dispatching_parallel_agent_for_session(
            manager_session,
            child_name,
            "Investigate the slow targeted abort path carefully.",
            turn_dispatch_id="slow-abort-dispatch",
            call_id="slow-abort-call",
        )
    )
    release_timer = threading.Timer(0.2, abort_release.set)
    release_timer.daemon = True
    release_timer.start()
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while not worker_started.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert worker_started.is_set()

        deadline = asyncio.get_running_loop().time() + 2
        while not abort_finished.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)

        assert abort_finished.is_set()
        assert abort_started.is_set()
        assert abort_thread_ids
        assert abort_thread_ids[0] != threading.get_ident()
        assert heartbeat_ticks
        targeted_abort.assert_called_once_with(
            "req-slow-abort",
            f"http://{runner.config.sglang.host}:{runner.config.sglang.port}",
            fallback_to_abort_all=False,
        )

        worker_gate.set()
        worker_threads[0].join(2)
        assert worker_finished.is_set()
        outcome = await asyncio.wait_for(dispatch_task, timeout=2)
        assert outcome.status == "timeout"
    finally:
        worker_gate.set()
        abort_release.set()
        release_timer.cancel()
        for worker in worker_threads:
            worker.join(2)
        if not dispatch_task.done():
            dispatch_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await dispatch_task
        heartbeat_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await heartbeat_task
        session_request_manager.clear(stable_id, "req-slow-abort")
        abort_manager.cleanup(stable_id)
        abort_patcher.stop()


def test_parser_to_process_tool_calls_produces_one_ordered_shared_blackboard():
    runner = _runner()
    session = _session(runner)
    agent = runner._build_profile_agent("manager_agent", session=session)
    content = '''<<TOOL_CALL>>
{
  "name": "dispatching_parallel_agent",
  "arguments": {
  "child_id": "poet_a",
  "task": "Compose poem A with a self-contained detailed briefing."
}
}
<<END_TOOL_CALL>>

<<TOOL_CALL>>
{
  "name": "dispatching_parallel_agent",
  "arguments": {
  "child_id": "poet_b",
  "task": "Compose poem B with a self-contained detailed briefing."
}
}
<<END_TOOL_CALL>>'''
    resolution = runner.model._resolve_assistant_control(
        content,
        None,
        agent.tools_and_managed_agents,
    )
    calls = list(resolution.tool_calls)
    call_ids = [call.id for call in calls]
    assert resolution.source == "text"
    assert len(set(call_ids)) == 2

    async def run_child(child_run, child_session):
        return f"report from {child_run.child_name}"

    runner._run_child_agent_to_final_report = run_child
    message = MagicMock(tool_calls=calls)
    step = _step()
    events = list(agent.process_tool_calls(message, step))

    plans = [
        event
        for event in events
        if isinstance(event, dict) and event.get("type") == "orchestration_plan"
    ]
    assert len(plans) == 1
    assert len(plans[0]["stages"]) == 1
    assert plans[0]["stages"][0]["stage_id"] == 0
    assert plans[0]["stages"][0]["parallel"] is True
    assert [
        tool["call_id"] for tool in plans[0]["stages"][0]["tools"]
    ] == call_ids
    assert [
        tool["name"] for tool in plans[0]["stages"][0]["tools"]
    ] == ["dispatching_parallel_agent", "dispatching_parallel_agent"]
    assert "Shared child-agent blackboard for this turn." in step.observations
    assert step.observations.index(f"Call: {call_ids[0]}") < step.observations.index(
        f"Call: {call_ids[1]}"
    )
    assert "Child: poet_a" in step.observations
    assert "Child: poet_b" in step.observations
    assert len(step.tool_calls) == 2
    dispatch_ids = {
        event["dispatch_id"]
        for event in session.timeline_events
        if event.get("kind") == "manager_child_agent_update"
    }
    assert len(dispatch_ids) == 1
    child_updates = [
        event
        for event in session.timeline_events
        if event.get("kind") == "manager_child_agent_update"
    ]
    assert {
        event["call_id"]
        for event in child_updates
        if event.get("status") == "running"
    } == set(call_ids)


@pytest.mark.parametrize(
    "content",
    [
        (
            _tool_block(
                "dispatching_parallel_agent",
                '{\n  "child_id": "valid",\n  "task": "Inspect source A independently and report details."\n}',
            )
            + "\n\n<<TOOL_CALL>>\n{\"name\":\"dispatching_parallel_agent\",\"arguments\":{\n"
        ),
        (
            _tool_block(
                "dispatching_parallel_agent",
                '{\n  "child_id": "valid-literal-separator",\n  "task": "Inspect source A independently and report details."\n}',
            )
            + r"\n"
            + "<<TOOL_CALL>> dispatching_parallel_agent\n{\n"
        ),
        _tool_block(
            "dispatching_parallel_agent",
            '{\n  "child_id": "missing-task"\n}',
        ),
        _tool_block(
            "dispatching_parallel_agent",
            '{\n  "child_id": "extra-key",\n  "task": "Inspect source independently and report details.",\n  "priority": "high"\n}',
        ),
        _tool_block("unknown_manager_tool", "{}"),
        "<<TOOL_CALL>> dispatching_parallel_agent\n[]}\n<<END_TOOL_CALL>>",
        "Calling tools:\n[]",
    ],
)
def test_invalid_manager_text_batch_rejects_before_any_child_launch(content):
    runner = _runner()
    session = _session(runner)
    agent = runner._build_profile_agent("manager_agent", session=session)
    runner._run_child_agent_to_final_report = AsyncMock()

    with pytest.raises(TextToolProtocolError) as exc_info:
        resolution = runner.model._resolve_assistant_control(
            content,
            None,
            agent.tools_and_managed_agents,
        )
        list(
            agent.process_tool_calls(
                MagicMock(tool_calls=list(resolution.tool_calls)),
                _step(),
            )
        )

    error = str(exc_info.value)
    assert "<<TOOL_CALL>>" in error
    assert "two-space indentation" in error
    assert "No tool was executed" in error
    runner._run_child_agent_to_final_report.assert_not_called()


def test_manager_dual_encoding_and_invalid_switch_target_execute_nothing():
    runner = _runner()
    session = _session(runner)
    agent = runner._build_profile_agent("manager_agent", session=session)
    runner._run_child_agent_to_final_report = AsyncMock()
    valid_native = _call(
        "provider_1",
        "dispatching_parallel_agent",
        {
            "child_id": "native-child",
            "task": "Inspect source independently and return a detailed report.",
        },
    )

    for content, native_calls in (
        (
            _tool_block(
                "dispatching_parallel_agent",
                '{\n  "child_id": "text-child",\n  "task": "Inspect source independently and return a detailed report."\n}',
            ),
            [valid_native],
        ),
        ("<<SWITCH_AGENT>> missing_agent", None),
    ):
        with pytest.raises(TextToolProtocolError) as exc_info:
            runner.model._resolve_assistant_control(
                content,
                native_calls,
                agent.tools_and_managed_agents,
                allowed_agent_names=set(runner.config.agent.enabled_agent_profiles),
            )
        assert "No tool was executed" in str(exc_info.value)

    runner._run_child_agent_to_final_report.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_child_subscriber_detach_preserves_worker_and_hub_replay():
    runner = _runner()
    manager_session = _session(runner)
    agent = runner._build_profile_agent("manager_agent", session=manager_session)
    entered = threading.Event()
    release = threading.Event()
    full_report = "child report suffix::" + "观" * 200_001

    async def blocked_child(child_run, child_session):
        entered.set()
        await asyncio.to_thread(release.wait)
        runner._record_child_agent_event(
            child_run,
            {"type": "content", "content": full_report},
        )
        return full_report

    runner._run_child_agent_to_final_report = blocked_child
    calls = [
        _call(
            "call_0",
            "dispatching_parallel_agent",
            {
                "child_id": "reader_detach",
                "task": "Inspect the complete independent source and return the full report.",
            },
        )
    ]
    step = _step()

    processing = asyncio.create_task(
        asyncio.to_thread(
            lambda: list(
                agent.process_tool_calls(MagicMock(tool_calls=calls), step)
            )
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    child_run = next(iter(runner.child_agent_runs.values()))
    child_session = runner.sessions.get(child_run.child_run_id)
    assert child_session is not None
    try:
        subscriber = runner.stream_child_agent_events(child_session.session_id)
        first = await asyncio.wait_for(anext(subscriber), timeout=1)
        assert "manager_assigned_task" in first
        assert '"event_sequence"' in first
        assert child_session.event_hub.journal_size >= 1
        await subscriber.aclose()

        assert child_session.event_hub.subscriber_count == 0
        assert child_session.aborted is False
        assert manager_session.aborted is False
        assert processing.done() is False
    finally:
        release.set()
    await asyncio.wait_for(processing, timeout=2)
    replay = "".join(
        [event async for event in runner.stream_child_agent_events(child_session.session_id)]
    )

    assert full_report in replay
    assert replay.endswith("\n\n")
    assert child_session.event_hub.has_active_producer is False
    assert child_session.event_hub.close_reason == "completed"


def test_mixed_normal_and_dispatch_calls_keep_observations_separate():
    runner = _runner()
    session = _session(runner)
    agent = runner._build_profile_agent("manager_agent", session=session)
    normal = MagicMock()
    normal.name = "read_file"
    normal.forward.return_value = "normal output"
    normal._injected_session_id = session.session_id
    agent.tools["read_file"] = normal

    async def run_child(child_run, child_session):
        return f"report from {child_run.child_name}"

    runner._run_child_agent_to_final_report = run_child
    message = MagicMock(tool_calls=[
        _call("read_1", "read_file", {"file_path": "/tmp/a"}),
        _call("call_1", "dispatching_parallel_agent", {
            "child_id": "reader-a", "task": "Investigate independent source A carefully."
        }),
        _call("call_2", "dispatching_parallel_agent", {
            "child_id": "reader-b", "task": "Investigate independent source B carefully."
        }),
    ])
    step = _step()
    list(agent.process_tool_calls(message, step))

    assert "[Tool read_file] (call_id=read_1):\nnormal output" in step.observations
    assert step.observations.count("Shared child-agent blackboard for this turn.") == 1
    assert "[Tool dispatching_parallel_agent]" not in step.observations


def test_dispatch_cap_skips_only_excess_dispatch_calls_in_order():
    runner = _runner(limit=2)
    session = _session(runner)
    agent = runner._build_profile_agent("manager_agent", session=session)
    launched = []

    async def run_child(child_run, child_session):
        launched.append(child_run.child_name)
        return child_run.child_name

    runner._run_child_agent_to_final_report = run_child
    calls = [
        _call(f"call_{index}", "dispatching_parallel_agent", {
            "child_id": child, "task": f"Investigate independent source {child} carefully."
        })
        for index, child in enumerate(("a-child", "b-child", "c-child", "d-child"), 1)
    ]
    step = _step()
    events = list(agent.process_tool_calls(MagicMock(tool_calls=calls), step))

    assert sorted(launched) == ["a-child", "b-child"]
    assert "Configured maximum: 2" in step.observations
    assert "Requested child calls: 4" in step.observations
    assert "Executed child calls: 2" in step.observations
    assert "Not executed: call_3/c-child, call_4/d-child" in step.observations
    assert len(step.tool_calls) == 4
    updates = [
        event
        for event in events
        if isinstance(event, dict) and event.get("type") == "orchestration_update"
    ]
    assert [(event["call_id"], event["status"]) for event in updates if event["status"] == "skipped"] == [
        ("call_3", "skipped"), ("call_4", "skipped")
    ]


def test_duplicate_child_id_is_rejected_before_launch_while_normal_tool_runs():
    runner = _runner()
    session = _session(runner)
    agent = runner._build_profile_agent("manager_agent", session=session)
    normal = MagicMock()
    normal.name = "read_file"
    normal.forward.return_value = "normal output"
    normal._injected_session_id = session.session_id
    agent.tools["read_file"] = normal
    launched = []

    async def run_child(child_run, child_session):
        launched.append(child_run.child_name)
        return "unexpected"

    runner._run_child_agent_to_final_report = run_child
    calls = [
        _call("read_1", "read_file", {"file_path": "/tmp/a"}),
        _call("call_1", "dispatching_parallel_agent", {"child_id": "duplicate", "task": "Investigate source A independently and report."}),
        _call("call_2", "dispatching_parallel_agent", {"child_id": "duplicate", "task": "Investigate source B independently and report."}),
    ]
    step = _step()
    list(agent.process_tool_calls(MagicMock(tool_calls=calls), step))

    assert launched == []
    assert normal.forward.called
    assert "duplicate child_id" in step.observations


def test_child_failure_does_not_cancel_sibling_and_done_counts_terminal_status():
    runner = _runner()
    session = _session(runner)
    agent = runner._build_profile_agent("manager_agent", session=session)

    async def run_child(child_run, child_session):
        if child_run.child_name == "bad-child":
            raise RuntimeError("child exploded")
        return "successful report"

    runner._run_child_agent_to_final_report = run_child
    calls = [
        _call("call_1", "dispatching_parallel_agent", {"child_id": "bad-child", "task": "Investigate source A independently and report."}),
        _call("call_2", "dispatching_parallel_agent", {"child_id": "good-child", "task": "Investigate source B independently and report."}),
    ]
    step = _step()
    events = list(agent.process_tool_calls(MagicMock(tool_calls=calls), step))
    done = next(
        event for event in events
        if isinstance(event, dict) and event.get("type") == "orchestration_done"
    )

    assert "Status: failed" in step.observations
    assert "child exploded" in step.observations
    assert "Status: completed" in step.observations
    assert "successful report" in step.observations
    assert done["failed"] == 1
    assert done["completed"] == 1
    terminal_updates = {
        event["call_id"]: event["status"]
        for event in events
        if isinstance(event, dict)
        and event.get("type") == "orchestration_update"
        and event.get("status") != "running"
    }
    assert terminal_updates == {"call_1": "failed", "call_2": "completed"}


@pytest.mark.asyncio
async def test_child_timeout_and_replay_metadata_remain_terminal():
    runner = _runner()
    runner.config.agent.manager_agent_child_timeout_seconds = 0.01
    session = _session(runner)

    async def run_child(child_run, child_session):
        await asyncio.sleep(10)
        return "late"

    runner._run_child_agent_to_final_report = run_child
    outcome = await runner._dispatching_parallel_agent_for_session(
        session, "slow-child", "Investigate one slow independent source carefully.",
        turn_dispatch_id="turn-1", call_id="call_1"
    )

    assert outcome.status == "timeout"
    child_run = next(iter(runner.child_agent_runs.values()))
    assert child_run.status == "timeout"
    assert _child_hub_payloads(runner, child_run)[-1]["type"] == "error"


def test_abort_manager_session_propagates_to_active_child_sessions():
    runner = _runner()
    manager = _session(runner)
    child_session = _session(
        session_id="child-session", agent=MagicMock(), mode="tool_calling",
        current_agent_name="basic_agent"
    )
    runner.sessions.put(child_session)
    runner.child_agent_runs[child_session.session_id] = ChildAgentRun(
        child_run_id=child_session.session_id, manager_session_id=manager.session_id,
        dispatch_id="turn-1",
        child_name="reader", profile_name="basic_agent",
        assigned_task="Investigate a source carefully.", status="running",
        child_session_identity=child_session,
    )

    try:
        assert runner.abort_session(manager.session_id) is True
        assert child_session.aborted is True
    finally:
        for session_id in (manager.session_id, child_session.session_id):
            session_request_manager.cleanup(session_id)
            abort_manager.cleanup(session_id)


def test_abort_manager_session_targets_active_child_request_ids():
    runner = _runner()
    manager = _session(runner)
    sibling_manager = _session(runner, session_id="sibling-manager")
    child_session = _session(
        session_id="child-session", agent=MagicMock(), mode="tool_calling",
        current_agent_name="basic_agent", is_child_context=True,
    )
    idle_child_session = _session(
        session_id="idle-child-session", agent=MagicMock(), mode="tool_calling",
        current_agent_name="basic_agent", is_child_context=True,
    )
    sibling_child_session = _session(
        session_id="sibling-child-session", agent=MagicMock(), mode="tool_calling",
        current_agent_name="basic_agent", is_child_context=True,
    )
    for session in (child_session, idle_child_session, sibling_child_session):
        runner.sessions.put(session)
    runner.child_agent_runs[child_session.session_id] = ChildAgentRun(
        child_run_id=child_session.session_id, manager_session_id=manager.session_id,
        dispatch_id="turn-1",
        child_name="reader", profile_name="basic_agent",
        assigned_task="Investigate one independent source carefully.", status="running",
        child_session_identity=child_session,
    )
    runner.child_agent_runs[idle_child_session.session_id] = ChildAgentRun(
        child_run_id=idle_child_session.session_id, manager_session_id=manager.session_id,
        dispatch_id="turn-1",
        child_name="writer", profile_name="basic_agent",
        assigned_task="Summarize another independent source carefully.", status="running",
        child_session_identity=idle_child_session,
    )
    runner.child_agent_runs[sibling_child_session.session_id] = ChildAgentRun(
        child_run_id=sibling_child_session.session_id, manager_session_id=sibling_manager.session_id,
        dispatch_id="turn-2",
        child_name="sibling-reader", profile_name="basic_agent",
        assigned_task="Investigate a sibling source carefully.", status="running",
        child_session_identity=sibling_child_session,
    )
    session_request_manager.set(child_session.session_id, "req-child-inflight")
    session_request_manager.set(sibling_child_session.session_id, "req-sibling-inflight")

    try:
        with patch("backend.llm_service.abort_request_by_id") as targeted_abort:
            assert runner.abort_session(manager.session_id) is True

        targeted_abort.assert_called_once_with(
            "req-child-inflight",
            f"http://{runner.config.sglang.host}:{runner.config.sglang.port}",
            fallback_to_abort_all=False,
        )
        assert child_session.aborted is True
        assert idle_child_session.aborted is True
        assert sibling_child_session.aborted is False
    finally:
        for session_id in (
            manager.session_id,
            sibling_manager.session_id,
            child_session.session_id,
            idle_child_session.session_id,
            sibling_child_session.session_id,
        ):
            session_request_manager.cleanup(session_id)
            abort_manager.cleanup(session_id)


def test_abort_manager_cascades_owned_registration_window_child_and_targets_known_requests():
    runner = _runner()
    manager = _session(runner, "registration-window-manager")
    mapped_child = _session(
        session_id="mapped-child-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    registration_window_child = _session(
        session_id="registration-window-child-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    sibling_manager = _session(runner, "registration-window-sibling-manager")
    sibling_child = _session(
        session_id="registration-window-sibling-child-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    for session in (mapped_child, registration_window_child, sibling_child):
        runner.sessions.put(session)

    runner.child_agent_runs[mapped_child.session_id] = ChildAgentRun(
        child_run_id=mapped_child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="registration-window-dispatch",
        child_name="mapped-child",
        profile_name="basic_agent",
        assigned_task="Inspect the mapped child source carefully.",
        status="running",
        child_session_identity=mapped_child,
    )
    runner.child_agent_sessions_by_key[
        (manager.session_id, "registration-window-child")
    ] = registration_window_child.session_id
    runner.child_agent_runs[sibling_child.session_id] = ChildAgentRun(
        child_run_id=sibling_child.session_id,
        manager_session_id=sibling_manager.session_id,
        dispatch_id="registration-window-sibling-dispatch",
        child_name="sibling-child",
        profile_name="basic_agent",
        assigned_task="Inspect the sibling child source carefully.",
        status="running",
        child_session_identity=sibling_child,
    )

    registration_window_scope = registration_window_child.begin_run()
    session_request_manager.set(mapped_child.session_id, "req-mapped-child")
    session_request_manager.set(
        registration_window_child.session_id,
        "req-registration-window-child",
    )
    session_request_manager.set(sibling_child.session_id, "req-sibling-child")

    try:
        with patch("backend.llm_service.abort_request_by_id") as targeted_abort:
            assert runner.abort_session(manager.session_id) is True

        targeted_request_ids = {
            call.args[0] for call in targeted_abort.call_args_list
        }
        assert targeted_request_ids == {
            "req-mapped-child",
            "req-registration-window-child",
        }
        assert all(
            call.kwargs["fallback_to_abort_all"] is False
            for call in targeted_abort.call_args_list
        )
        assert mapped_child.aborted is True
        assert registration_window_child.aborted is True
        assert sibling_child.aborted is False
        assert sibling_manager.aborted is False
    finally:
        if registration_window_scope in registration_window_child.capture_active_run_scopes():
            registration_window_child.finish_run(registration_window_scope)
        for session_id in (
            manager.session_id,
            sibling_manager.session_id,
            mapped_child.session_id,
            registration_window_child.session_id,
            sibling_child.session_id,
        ):
            session_request_manager.cleanup(session_id)
            abort_manager.cleanup(session_id)


def test_abort_manager_ignores_terminal_timeout_child():
    runner = _runner()
    manager = _session(runner, "timeout-manager")
    timeout_child = _session(
        session_id="timeout-child-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    running_child = _session(
        session_id="running-child-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    runner.sessions.put(timeout_child)
    runner.sessions.put(running_child)
    runner.child_agent_runs[timeout_child.session_id] = ChildAgentRun(
        child_run_id=timeout_child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="timeout-dispatch",
        child_name="timeout-child",
        profile_name="basic_agent",
        assigned_task="The timeout run has already settled.",
        status="timeout",
        child_session_identity=timeout_child,
    )
    runner.child_agent_runs[running_child.session_id] = ChildAgentRun(
        child_run_id=running_child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="running-dispatch",
        child_name="running-child",
        profile_name="basic_agent",
        assigned_task="Keep this run cancellable.",
        status="running",
        child_session_identity=running_child,
    )
    session_request_manager.set(timeout_child.session_id, "req-timeout-child")
    session_request_manager.set(running_child.session_id, "req-running-child")

    try:
        with patch("backend.llm_service.abort_request_by_id") as targeted_abort:
            assert runner.abort_session(manager.session_id) is True

        assert timeout_child.aborted is False
        assert session_request_manager.get(timeout_child.session_id) == "req-timeout-child"
        assert running_child.aborted is True
        targeted_abort.assert_called_once_with(
            "req-running-child",
            f"http://{runner.config.sglang.host}:{runner.config.sglang.port}",
            fallback_to_abort_all=False,
        )
    finally:
        session_request_manager.clear(timeout_child.session_id, "req-timeout-child")
        session_request_manager.clear(running_child.session_id, "req-running-child")


@pytest.mark.asyncio
async def test_abort_child_waits_for_worker_then_disposes_only_target_context():
    runner = _runner()
    manager = _session(runner)
    sibling = _session(session_id="sibling-session", agent=MagicMock(), mode="tool_calling")
    target = _session(
        session_id="target-session",
        agent=MagicMock(),
        mode="tool_calling",
        is_child_context=True,
    )
    target.agent.memory.steps = ["private child history"]
    runner.sessions.put(sibling)
    runner.sessions.put(target)
    child_run = ChildAgentRun(
        child_run_id=target.session_id, manager_session_id=manager.session_id,
        dispatch_id="dispatch-1",
        child_name="target-child", profile_name="basic_agent",
        assigned_task="Investigate the target task carefully.", status="running",
        child_session_identity=target,
    )
    child_run.event_hub_producer_token = target.event_hub.claim_producer()
    runner.child_agent_runs[child_run.child_run_id] = child_run
    runner.child_agent_sessions_by_key[(manager.session_id, "target-child")] = target.session_id
    runner._record_child_agent_event(
        child_run,
        {"type": "manager_assigned_task", "task": child_run.assigned_task},
    )
    scope_id = target.begin_run()
    session_request_manager.set(target.session_id, "req-target-child")

    with patch("backend.llm_service.abort_request_by_id") as targeted_abort:
        abort_task = asyncio.create_task(runner.abort_child_agent_run(child_run.child_run_id))
        for _ in range(50):
            if target.aborted:
                break
            await asyncio.sleep(0.01)

        assert target.aborted is True
        assert runner.sessions.get(target.session_id) is target
        assert manager.aborted is False
        assert sibling.aborted is False
        assert abort_task.done() is False
        targeted_abort.assert_called_once()
        assert targeted_abort.call_args.args[0] == "req-target-child"
        assert targeted_abort.call_args.kwargs["fallback_to_abort_all"] is False

        session_request_manager.clear(target.session_id, "req-target-child")
        target.finish_run(scope_id)
        child_run.status = "aborted"
        child_run.error = "The user has aborted this child agent run."
        child_run.completed_at = datetime.now()
        runner._record_child_terminal_event(
            child_run,
            {"type": "aborted", "error": child_run.error},
        )
        child_run.terminal_event.set()
        result = await asyncio.wait_for(abort_task, timeout=1)

    assert result["status"] == "aborted"
    assert runner.sessions.get(target.session_id) is target
    assert target.event_hub.released is False
    replay = "".join(
        [event async for event in runner.stream_child_agent_events(child_run.child_run_id)]
    )
    assert '"type": "aborted"' in replay
    assert runner.child_agent_sessions_by_key[(manager.session_id, "target-child")] == (
        target.session_id
    )
    assert runner.child_agent_runs[child_run.child_run_id] is child_run
    assert _child_hub_payloads(runner, child_run)[-1]["type"] == "aborted"


@pytest.mark.asyncio
async def test_original_dispatch_worker_returns_user_aborted_blackboard_outcome_and_fresh_retry():
    runner = _runner()
    manager = _session(runner)

    async def run_child(child_run, child_session):
        scope_id = child_session.begin_run()
        try:
            while not child_session.aborted:
                await asyncio.sleep(0.01)
            child_run.status = "aborted"
            child_run.error = child_run.abort_reason
            return ""
        finally:
            child_session.finish_run(scope_id)

    runner._run_child_agent_to_final_report = run_child
    dispatch_task = asyncio.create_task(runner._dispatching_parallel_agent_for_session(
        manager, "reusable-child", "Investigate this independent source carefully.",
        turn_dispatch_id="dispatch-1", call_id="call-1",
    ))
    for _ in range(50):
        child_run = runner.child_agent_runs.get(
            runner._make_child_identity_id(manager.session_id, "reusable-child")
        )
        if child_run is not None and child_run.status == "running":
            break
        await asyncio.sleep(0.01)
    assert child_run is not None

    original_child_session = runner.sessions.get(child_run.child_run_id)
    assert original_child_session is not None
    original_agent = original_child_session.agent
    original_model = original_agent.model
    original_hub = original_child_session.event_hub
    original_hub_entry = original_child_session.session_hub_entry
    original_runtime_context = original_child_session.runtime_context
    original_agent.memory.steps = ["old child memory"]
    original_agent.state = {"old": "state"}
    original_child_session.last_llm_prompt_snapshot = {"old": "snapshot"}
    original_child_session.last_compactable_llm_messages = ["old prompt"]
    original_child_session.behavior_records = {"old": "record"}
    original_child_session.timeline_events = [{"kind": "old"}]

    abort_result, outcome = await asyncio.gather(
        runner.abort_child_agent_run(child_run.child_run_id), dispatch_task
    )

    assert abort_result["status"] == "aborted"
    assert outcome.status == "aborted"
    assert outcome.error == "The user has aborted this child agent run."
    aborted_child_session = runner.sessions.get(child_run.child_run_id)
    assert aborted_child_session is original_child_session
    assert aborted_child_session.event_hub is original_hub
    assert aborted_child_session.session_hub_entry is original_hub_entry
    assert aborted_child_session.runtime_context == original_runtime_context
    assert aborted_child_session.agent is not original_agent
    assert aborted_child_session.agent.model is not original_model
    assert aborted_child_session.agent.model._session is aborted_child_session
    assert aborted_child_session.agent.memory.steps == []
    assert aborted_child_session.agent.state == {}
    assert aborted_child_session.last_llm_prompt_snapshot is None
    assert aborted_child_session.last_compactable_llm_messages is None
    assert aborted_child_session.behavior_records == {}
    assert aborted_child_session.timeline_events == []
    assert aborted_child_session.run_in_progress is False
    assert aborted_child_session.capture_active_run_scopes() == frozenset()
    assert aborted_child_session.event_hub.has_active_producer is False
    assert aborted_child_session.aborted is False
    assert aborted_child_session.answer_now is False
    assert session_request_manager.get(aborted_child_session.session_id) is None
    assert abort_manager.is_aborted(aborted_child_session.session_id) is False
    assert runner.child_agent_sessions_by_key[(manager.session_id, "reusable-child")] == (
        child_run.child_run_id
    )
    assert any(
        record.payload.get("type") == "aborted"
        for record in original_hub._journal
    )
    assert original_hub.released is False
    dispatch_call = _call("call-1", "dispatching_parallel_agent", {
        "child_id": "reusable-child", "task": "Investigate this independent source carefully."
    })
    blackboard = runner._build_profile_agent(
        "manager_agent", session=manager
    )._format_manager_turn_blackboard(
        [dispatch_call],
        {"call-1": ToolResult(
            call_id="call-1", tool_name="dispatching_parallel_agent",
            status="aborted", output=outcome, error=outcome.error,
        )},
        configured_maximum=4,
        executed_count=1,
    )
    assert "Status: aborted" in blackboard
    assert "The user has aborted this child agent run." in blackboard
    assert "reused_context" not in blackboard

    runner._run_child_agent_to_final_report = AsyncMock(return_value="fresh report")
    fresh_outcome = await runner._dispatching_parallel_agent_for_session(
        manager, "reusable-child", "Start the same child identity with fresh context.",
        turn_dispatch_id="dispatch-2", call_id="call-2",
    )
    assert fresh_outcome.status == "completed"
    fresh_run = next(
        run for run in runner.child_agent_runs.values() if run.call_id == "call-2"
    )
    assert fresh_run.child_run_id == child_run.child_run_id
    assert runner.sessions.get(fresh_run.child_run_id) is original_child_session
    assert original_child_session.event_hub is original_hub
    assert original_child_session.agent is not original_agent
    anchors = [
        event for event in _child_hub_payloads(runner, fresh_run)
        if event.get("type") == "manager_assigned_task"
    ]
    assert anchors[-1]["reused_context"] is False
    assert anchors[-1]["prior_step_count"] == 0


@pytest.mark.asyncio
async def test_completed_child_turns_reuse_the_same_agent_shell_and_memory():
    runner = _runner()
    manager = _session(runner)
    first_agent = None
    first_shell = None
    first_hub = None

    async def run_child(child_run, child_session):
        nonlocal first_agent, first_shell, first_hub
        scope_id = child_session.begin_run()
        try:
            if first_agent is None:
                first_agent = child_session.agent
                first_shell = child_session
                first_hub = child_session.event_hub
                child_session.agent.memory.steps.append("canonical completed step")
            return f"report for {child_run.call_id}"
        finally:
            child_session.finish_run(scope_id)

    runner._run_child_agent_to_final_report = run_child
    first_outcome = await runner._dispatching_parallel_agent_for_session(
        manager,
        "reused-child",
        "Complete the first independent task carefully and report.",
        turn_dispatch_id="dispatch-1",
        call_id="call-1",
    )
    second_outcome = await runner._dispatching_parallel_agent_for_session(
        manager,
        "reused-child",
        "Continue the same independent task with the existing context.",
        turn_dispatch_id="dispatch-2",
        call_id="call-2",
    )

    assert first_outcome.status == "completed"
    assert second_outcome.status == "completed"
    second_run = next(
        child_run
        for child_run in runner.child_agent_runs.values()
        if child_run.call_id == "call-2"
    )
    assert second_run.child_run_id == first_shell.session_id
    assert runner.sessions.get(first_shell.session_id) is first_shell
    assert first_shell.agent is first_agent
    assert first_shell.event_hub is first_hub
    assert first_shell.agent.memory.steps == ["canonical completed step"]
    anchor = [
        event
        for event in _child_hub_payloads(runner, second_run)
        if event.get("type") == "manager_assigned_task"
        and event.get("call_id") == "call-2"
    ][-1]
    assert anchor["reused_context"] is True
    assert anchor["prior_step_count"] > 0


@pytest.mark.asyncio
async def test_explicit_abort_reset_rejects_redispatch_until_worker_quiesces():
    runner = _runner()
    manager = _session(runner)
    worker_started = asyncio.Event()
    allow_worker_exit = asyncio.Event()

    async def run_child(child_run, child_session):
        scope_id = child_session.begin_run()
        try:
            worker_started.set()
            await allow_worker_exit.wait()
            if child_session.aborted:
                child_run.status = "aborted"
                child_run.error = child_run.abort_reason
            return ""
        finally:
            child_session.finish_run(scope_id)

    runner._run_child_agent_to_final_report = run_child
    dispatch_task = asyncio.create_task(
        runner._dispatching_parallel_agent_for_session(
            manager,
            "racy-child",
            "Run one gated child task and report.",
            turn_dispatch_id="dispatch-1",
            call_id="call-1",
        )
    )
    await asyncio.wait_for(worker_started.wait(), timeout=1)
    stable_id = runner._make_child_identity_id(manager.session_id, "racy-child")
    old_run = runner.child_agent_runs[stable_id]
    old_session = runner.sessions.get(stable_id)
    assert old_session is not None
    old_agent = old_session.agent

    abort_task = asyncio.create_task(runner.abort_child_agent_run(stable_id))
    for _ in range(50):
        if old_session.aborted:
            break
        await asyncio.sleep(0.01)
    assert old_session.aborted is True

    runner._run_child_agent_to_final_report = AsyncMock(return_value="must not race")
    with pytest.raises(SessionBusyError):
        await runner._dispatching_parallel_agent_for_session(
            manager,
            "racy-child",
            "Do not claim the old context while explicit abort is pending.",
            turn_dispatch_id="dispatch-race",
            call_id="call-race",
        )
    assert runner.child_agent_runs[stable_id] is old_run
    assert old_session.agent is old_agent
    assert old_session.event_hub.has_active_producer is True

    allow_worker_exit.set()
    assert (await asyncio.wait_for(abort_task, timeout=2))["status"] == "aborted"
    assert (await asyncio.wait_for(dispatch_task, timeout=2)).status == "aborted"

    runner._run_child_agent_to_final_report = AsyncMock(return_value="retry report")
    retry_outcome = await runner._dispatching_parallel_agent_for_session(
        manager,
        "racy-child",
        "Retry only after the explicit reset has completed.",
        turn_dispatch_id="dispatch-2",
        call_id="call-2",
    )
    retry_run = runner.child_agent_runs[stable_id]
    retry_session = runner.sessions.get(stable_id)
    assert retry_outcome.status == "completed"
    assert retry_run.child_run_id == stable_id
    assert retry_session is old_session
    assert retry_session.agent is not old_agent



@pytest.mark.asyncio
async def test_terminal_update_failure_still_settles_run_and_reset_rejects_unsettled_run():
    runner = _runner()
    manager = _session(runner, "terminal-update-manager")
    runner._run_child_agent_to_final_report = AsyncMock(return_value="completed report")
    original_update = runner._append_manager_child_update

    def fail_terminal_update(manager_session, child_run, status, **kwargs):
        if status == "completed":
            raise RuntimeError("manager terminal update failed")
        return original_update(manager_session, child_run, status, **kwargs)

    runner._append_manager_child_update = fail_terminal_update
    dispatch_task = asyncio.create_task(
        runner._dispatching_parallel_agent_for_session(
            manager,
            "terminal-child",
            "Complete the child task and publish a terminal report.",
            turn_dispatch_id="dispatch-1",
            call_id="call-1",
        )
    )
    with pytest.raises(RuntimeError, match="manager terminal update failed"):
        await asyncio.wait_for(dispatch_task, timeout=2)

    stable_id = runner._make_child_identity_id(manager.session_id, "terminal-child")
    child_run = runner.child_agent_runs[stable_id]
    child_session = runner.sessions.get(stable_id)
    assert child_session is not None
    assert child_run.status == "completed"
    assert child_session.event_hub.has_active_producer is False
    assert child_run.terminal_event.is_set() is True

    child_run.terminal_event.clear()
    with pytest.raises(SessionBusyError):
        runner._reset_child_model_context(child_session, child_run)
    child_run.terminal_event.set()

    abort_result = await asyncio.wait_for(
        runner.abort_child_agent_run(stable_id), timeout=1
    )
    assert abort_result["status"] == "already_terminal"


def test_explicit_reset_binds_fresh_model_when_runtime_time_context_is_disabled():
    runner = _runner()
    runner.config.runtime_prompts.enable_runtime_time_context = False
    manager = _session(runner, "runtime-off-manager")
    child_id = runner._make_child_identity_id(manager.session_id, "runtime-off-child")
    child_session = _session(
        session_id=child_id,
        agent=runner._build_profile_agent("basic_agent", session=None),
        mode=manager.mode,
        tool_configuration_snapshot=manager.tool_configuration_snapshot,
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    runner.sessions.put(child_session)
    child_run = ChildAgentRun(
        child_run_id=child_id,
        manager_session_id=manager.session_id,
        dispatch_id="dispatch-1",
        child_name="runtime-off-child",
        profile_name="basic_agent",
        assigned_task="Reset the child model context without runtime time injection.",
        status="aborted",
        child_session_identity=child_session,
    )
    child_run.terminal_event.set()
    runner.child_agent_runs[child_id] = child_run
    runner.child_agent_sessions_by_key[(manager.session_id, "runtime-off-child")] = child_id
    old_model = child_session.agent.model

    runner._reset_child_model_context(child_session, child_run)

    assert child_session.agent.model is not old_model
    assert child_session.agent.model is not runner.model
    assert child_session.agent.model._session is child_session
    assert child_session.runtime_context is None


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_enabled", [True, False])
async def test_initial_child_agent_model_is_bound_to_exact_shell(runtime_enabled):
    runner = _runner()
    runner.config.runtime_prompts.enable_runtime_time_context = runtime_enabled
    manager = _session(runner, f"initial-model-manager-{runtime_enabled}")
    runner._run_child_agent_to_final_report = AsyncMock(
        side_effect=["first report", "second report"]
    )

    first = await runner._dispatching_parallel_agent_for_session(
        manager,
        "session-bound-child",
        "Complete the first ordinary child turn.",
        turn_dispatch_id="dispatch-1",
        call_id="call-1",
    )
    stable_id = runner._make_child_identity_id(
        manager.session_id, "session-bound-child"
    )
    child_session = runner.sessions.get(stable_id)
    assert first.status == "completed"
    assert child_session is not None
    first_agent = child_session.agent
    assert isinstance(first_agent.model, agent_runner_module.LocalLLMModel)
    assert first_agent.model._session is child_session

    second = await runner._dispatching_parallel_agent_for_session(
        manager,
        "session-bound-child",
        "Complete the second ordinary child turn using the same shell.",
        turn_dispatch_id="dispatch-2",
        call_id="call-2",
    )
    assert second.status == "completed"
    assert runner.sessions.get(stable_id) is child_session
    assert child_session.agent is first_agent
    assert child_session.agent.model._session is child_session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_target", ["_build_runtime_context_for_profile", "_build_profile_agent"]
)
async def test_child_construction_failure_rolls_back_mapping_and_blocks_unrelated_reuse(
    failure_target,
):
    runner = _runner()
    manager = _session(runner, f"construction-failure-manager-{failure_target}")
    child_name = "construction-failure-child"
    stable_id = runner._make_child_identity_id(manager.session_id, child_name)

    with patch.object(
        runner,
        failure_target,
        side_effect=RuntimeError(f"{failure_target} failed before insert"),
    ):
        with pytest.raises(RuntimeError, match="failed before insert"):
            await runner._dispatching_parallel_agent_for_session(
                manager,
                child_name,
                "Fail before exposing a proposed child shell.",
                turn_dispatch_id="dispatch-failure",
                call_id="call-failure",
            )

    child_key = (manager.session_id, child_name)
    assert runner.child_agent_sessions_by_key.get(child_key) is None
    assert runner.sessions.get(stable_id) is None
    assert stable_id not in runner.child_agent_runs

    unrelated = _session(
        session_id=stable_id,
        agent=SimpleNamespace(
            memory=SimpleNamespace(steps=["unrelated history"]),
            state={"owner": "unrelated"},
        ),
        mode="tool_calling",
    )
    runner.sessions.put(unrelated)
    with pytest.raises(SessionBusyError):
        await runner._dispatching_parallel_agent_for_session(
            manager,
            child_name,
            "Reject canonical-ID reuse while an unrelated session owns it.",
            turn_dispatch_id="dispatch-reuse",
            call_id="call-reuse",
        )

    assert runner.sessions.get(stable_id) is unrelated
    assert unrelated.agent.memory.steps == ["unrelated history"]
    assert unrelated.agent.state == {"owner": "unrelated"}
    assert runner.child_agent_sessions_by_key.get(child_key) is None
    assert stable_id not in runner.child_agent_runs


@pytest.mark.asyncio
async def test_existing_child_mapping_to_non_child_session_is_busy_without_mutation():
    runner = _runner()
    manager = _session(runner, "mapped-non-child-manager")
    child_name = "mapped-non-child"
    stable_id = runner._make_child_identity_id(manager.session_id, child_name)
    unrelated = _session(
        session_id=stable_id,
        agent=SimpleNamespace(
            memory=SimpleNamespace(steps=["unrelated history"]),
            state={"owner": "unrelated"},
        ),
        mode="tool_calling",
    )
    runner.sessions.put(unrelated)
    child_key = (manager.session_id, child_name)
    runner.child_agent_sessions_by_key[child_key] = stable_id

    with pytest.raises(SessionBusyError):
        await runner._dispatching_parallel_agent_for_session(
            manager,
            child_name,
            "Reject a mapping that points at a top-level session.",
            turn_dispatch_id="dispatch-1",
            call_id="call-1",
        )

    assert runner.child_agent_sessions_by_key[child_key] == stable_id
    assert runner.sessions.get(stable_id) is unrelated
    assert unrelated.agent.memory.steps == ["unrelated history"]
    assert unrelated.agent.state == {"owner": "unrelated"}
    assert stable_id not in runner.child_agent_runs


@pytest.mark.asyncio
async def test_existing_child_run_with_wrong_owner_is_busy_without_replacement():
    runner = _runner()
    manager = _session(runner, "mapped-owner-manager")
    child_name = "mapped-owner-child"
    stable_id = runner._make_child_identity_id(manager.session_id, child_name)
    child_session = _session(
        session_id=stable_id,
        agent=SimpleNamespace(
            memory=SimpleNamespace(steps=["child history"]), state={}
        ),
        mode="tool_calling",
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    runner.sessions.put(child_session)
    child_key = (manager.session_id, child_name)
    runner.child_agent_sessions_by_key[child_key] = stable_id
    old_run = ChildAgentRun(
        child_run_id=stable_id,
        manager_session_id="different-manager",
        dispatch_id="old-dispatch",
        child_name="different-child",
        profile_name="basic_agent",
        assigned_task="Owned by a different manager and child name.",
        status="completed",
        child_session_identity=child_session,
    )
    old_run.terminal_event.set()
    runner.child_agent_runs[stable_id] = old_run

    with pytest.raises(SessionBusyError):
        await runner._dispatching_parallel_agent_for_session(
            manager,
            child_name,
            "Reject reuse when the retained run owner does not match.",
            turn_dispatch_id="dispatch-new",
            call_id="call-new",
        )

    assert runner.child_agent_runs[stable_id] is old_run
    assert runner.sessions.get(stable_id) is child_session
    assert runner.child_agent_sessions_by_key[child_key] == stable_id
    assert child_session.agent.memory.steps == ["child history"]


@pytest.mark.asyncio
async def test_claim_producer_none_retains_new_shell_for_safe_retry_without_touching_other_producer():
    runner = _runner()
    manager = _session(runner, "claim-none-manager")
    unrelated = _session(
        session_id="unrelated-producer-session",
        agent=SimpleNamespace(memory=SimpleNamespace(steps=[]), state={}),
        mode="tool_calling",
    )
    runner.sessions.put(unrelated)
    unrelated_token = unrelated.event_hub.claim_producer()
    assert unrelated_token is not None
    child_name = "claim-none-child"
    stable_id = runner._make_child_identity_id(manager.session_id, child_name)

    with patch(
        "backend.agent.session_hub.AgentSessionHub.claim_producer",
        return_value=None,
    ):
        with pytest.raises(SessionBusyError):
            await runner._dispatching_parallel_agent_for_session(
                manager,
                child_name,
                "Retain the valid stable shell when producer admission is unavailable.",
                turn_dispatch_id="dispatch-1",
                call_id="call-1",
            )

    child_session = runner.sessions.get(stable_id)
    child_key = (manager.session_id, child_name)
    assert child_session is not None
    assert child_session.is_child_context is True
    assert isinstance(child_session.agent.model, agent_runner_module.LocalLLMModel)
    assert child_session.agent.model._session is child_session
    assert runner.child_agent_sessions_by_key[child_key] == stable_id
    assert stable_id not in runner.child_agent_runs
    assert unrelated.event_hub.capture_active_producer() is unrelated_token

    runner._run_child_agent_to_final_report = AsyncMock(return_value="retry report")
    retry = await runner._dispatching_parallel_agent_for_session(
        manager,
        child_name,
        "Retry after producer admission becomes available.",
        turn_dispatch_id="dispatch-2",
        call_id="call-2",
    )
    assert retry.status == "completed"
    assert runner.sessions.get(stable_id) is child_session
    assert unrelated.event_hub.capture_active_producer() is unrelated_token
    assert unrelated.event_hub.release_producer(unrelated_token) is True


@pytest.mark.asyncio
async def test_child_admission_rolls_back_when_assignment_append_fails_before_mutation():
    runner = _runner()
    manager = _session(runner, "admission-before-anchor-manager")
    worker = AsyncMock(return_value="must not start")
    runner._run_child_agent_to_final_report = worker
    stable_id = runner._make_child_identity_id(manager.session_id, "before-anchor-child")

    with patch(
        "backend.agent.session_hub.AgentSessionHub.append",
        side_effect=RuntimeError("assignment append failed before mutation"),
    ):
        with pytest.raises(RuntimeError, match="assignment append failed"):
            await runner._dispatching_parallel_agent_for_session(
                manager,
                "before-anchor-child",
                "Fail before making the child assignment anchor durable.",
                turn_dispatch_id="dispatch-1",
                call_id="call-1",
            )

    child_run = runner.child_agent_runs[stable_id]
    child_session = runner.sessions.get(stable_id)
    assert child_session is not None
    assert worker.await_count == 0
    assert child_run.status == "failed"
    assert child_run.completed_at is not None
    assert child_run.terminal_event.is_set() is True
    assert child_session.capture_active_run_scopes() == frozenset()
    assert child_session.event_hub.has_active_producer is False
    assert child_session.event_hub.journal_size == 0
    assert runner.child_agent_sessions_by_key[(manager.session_id, "before-anchor-child")] == stable_id

    runner._run_child_agent_to_final_report = AsyncMock(return_value="retry report")
    retry = await runner._dispatching_parallel_agent_for_session(
        manager,
        "before-anchor-child",
        "Retry the same stable child after admission rollback.",
        turn_dispatch_id="dispatch-2",
        call_id="call-2",
    )
    assert retry.status == "completed"
    assert runner.sessions.get(stable_id) is child_session


@pytest.mark.asyncio
async def test_child_admission_rolls_back_after_manager_pending_projection_failure():
    runner = _runner()
    manager = _session(runner, "admission-pending-manager")
    worker = AsyncMock(return_value="must not start")
    runner._run_child_agent_to_final_report = worker
    stable_id = runner._make_child_identity_id(manager.session_id, "pending-failure-child")
    original_update = runner._append_manager_child_update

    def fail_pending_projection(manager_session, child_run, status, **kwargs):
        if status == "pending":
            raise RuntimeError("manager pending projection failed")
        return original_update(manager_session, child_run, status, **kwargs)

    with patch.object(runner, "_append_manager_child_update", side_effect=fail_pending_projection):
        with pytest.raises(RuntimeError, match="pending projection failed"):
            await runner._dispatching_parallel_agent_for_session(
                manager,
                "pending-failure-child",
                "Fail after the child assignment anchor is durable.",
                turn_dispatch_id="dispatch-1",
                call_id="call-1",
            )

    child_run = runner.child_agent_runs[stable_id]
    child_session = runner.sessions.get(stable_id)
    assert child_session is not None
    assert worker.await_count == 0
    assert child_run.status == "failed"
    assert child_run.error == "manager pending projection failed"
    assert child_run.completed_at is not None
    assert child_run.terminal_event.is_set() is True
    assert child_session.event_hub.has_active_producer is False
    payloads = _child_hub_payloads(runner, child_run)
    assert [payload["type"] for payload in payloads] == [
        "manager_assigned_task",
        "error",
    ]

    runner._run_child_agent_to_final_report = AsyncMock(return_value="retry report")
    retry = await runner._dispatching_parallel_agent_for_session(
        manager,
        "pending-failure-child",
        "Retry the same stable child after pending rollback.",
        turn_dispatch_id="dispatch-2",
        call_id="call-2",
    )
    assert retry.status == "completed"
    assert runner.sessions.get(stable_id) is child_session


@pytest.mark.asyncio
async def test_child_admission_reconciles_assignment_append_that_mutates_then_raises():
    runner = _runner()
    manager = _session(runner, "admission-mutated-anchor-manager")
    worker = AsyncMock(return_value="must not start")
    runner._run_child_agent_to_final_report = worker
    stable_id = runner._make_child_identity_id(manager.session_id, "mutated-anchor-child")
    from backend.agent.session_hub import AgentSessionHub

    real_append = AgentSessionHub.append
    assignment_attempts = 0

    def append_then_raise(hub, payload, **kwargs):
        nonlocal assignment_attempts
        record = real_append(hub, payload, **kwargs)
        if payload.get("type") == "manager_assigned_task":
            assignment_attempts += 1
            raise RuntimeError("assignment append failed after mutation")
        return record

    with patch("backend.agent.session_hub.AgentSessionHub.append", new=append_then_raise):
        with pytest.raises(RuntimeError, match="after mutation"):
            await runner._dispatching_parallel_agent_for_session(
                manager,
                "mutated-anchor-child",
                "Reconcile an assignment anchor that was appended before failure.",
                turn_dispatch_id="dispatch-1",
                call_id="call-1",
            )

    child_run = runner.child_agent_runs[stable_id]
    child_session = runner.sessions.get(stable_id)
    assert child_session is not None
    assert worker.await_count == 0
    assert assignment_attempts == 1
    assert child_run.status == "failed"
    assert child_run.terminal_event.is_set() is True
    assert child_session.event_hub.has_active_producer is False
    payloads = _child_hub_payloads(runner, child_run)
    assert [payload["type"] for payload in payloads] == [
        "manager_assigned_task",
        "error",
    ]
    assert sum(payload["type"] == "manager_assigned_task" for payload in payloads) == 1
    assert sum(payload["type"] == "error" for payload in payloads) == 1


def test_stale_child_admission_rollback_cannot_touch_replacement_run_or_producer():
    runner = _runner()
    manager = _session(runner, "stale-admission-manager")
    stable_id = runner._make_child_identity_id(manager.session_id, "stale-admission-child")
    child_session = _session(
        session_id=stable_id,
        agent=MagicMock(memory=SimpleNamespace(steps=["replacement memory"]), state={"owner": "replacement"}),
        mode="tool_calling",
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    runner.sessions.put(child_session)
    old_run = ChildAgentRun(
        child_run_id=stable_id,
        manager_session_id=manager.session_id,
        dispatch_id="dispatch-old",
        child_name="stale-admission-child",
        profile_name="basic_agent",
        assigned_task="Old failed admission.",
        status="failed",
        error="old admission failure",
        completed_at=datetime.now(),
        child_session_identity=child_session,
    )
    old_token = child_session.event_hub.claim_producer()
    assert old_token is not None
    old_run.event_hub_producer_token = old_token
    assert child_session.event_hub.close("failed", producer_token=old_token) is True
    assert child_session.event_hub.release_producer(old_token) is True

    replacement_run = ChildAgentRun(
        child_run_id=stable_id,
        manager_session_id=manager.session_id,
        dispatch_id="dispatch-new",
        child_name="stale-admission-child",
        profile_name="basic_agent",
        assigned_task="Replacement live admission.",
        status="running",
        child_session_identity=child_session,
    )
    replacement_token = child_session.event_hub.claim_producer()
    assert replacement_token is not None
    replacement_run.event_hub_producer_token = replacement_token
    runner.child_agent_runs[stable_id] = replacement_run
    runner.child_agent_sessions_by_key[(manager.session_id, "stale-admission-child")] = stable_id
    child_session.aborted = True

    runner._rollback_child_admission(old_run, child_session, anchor_visible=False)

    assert runner.child_agent_runs[stable_id] is replacement_run
    assert replacement_run.event_hub_producer_token is replacement_token
    assert child_session.event_hub.capture_active_producer() is replacement_token
    assert child_session.event_hub.has_active_producer is True
    assert child_session.agent.memory.steps == ["replacement memory"]
    assert child_session.agent.state == {"owner": "replacement"}
    assert child_session.aborted is True
    assert runner.child_agent_sessions_by_key[(manager.session_id, "stale-admission-child")] == stable_id
    assert child_session.event_hub.release_producer(replacement_token) is True


@pytest.mark.asyncio
async def test_disposed_manager_identity_reuse_restarts_child_after_release_boundary():
    runner = _runner()
    manager = _session(runner, "reused-public-manager")
    runner._run_child_agent_to_final_report = AsyncMock(return_value="old report")

    await runner._dispatching_parallel_agent_for_session(
        manager,
        "stable-child",
        "Investigate the old independent task and report carefully.",
        turn_dispatch_id="old-dispatch",
        call_id="old-call",
    )
    old_run_id = next(iter(runner.child_agent_runs))
    old_run = runner.child_agent_runs[old_run_id]
    old_child_session = runner.sessions.get(old_run.child_run_id)
    assert old_child_session is not None
    await runner.dispose_session(manager.session_id)
    replacement_manager = _session(runner, manager.session_id)
    runner._run_child_agent_to_final_report = AsyncMock(return_value="new report")

    outcome = await runner._dispatching_parallel_agent_for_session(
        replacement_manager,
        "stable-child",
        "Investigate the new independent task with a fresh context.",
        turn_dispatch_id="new-dispatch",
        call_id="new-call",
    )

    new_run = next(
        child_run
        for child_run in runner.child_agent_runs.values()
        if child_run.call_id == "new-call"
    )
    assert outcome.status == "completed"
    assert new_run.child_run_id == old_run_id
    assert new_run.child_run_id == old_run.child_run_id
    anchor = _child_hub_payloads(runner, new_run)[0]
    assert anchor["reused_context"] is False
    assert anchor["prior_step_count"] == 0
    replay = [
        json.loads(event.removeprefix("data: ").strip())
        async for event in runner.stream_child_agent_events(old_run_id)
    ]
    assert any(event.get("final_report") == "new report" for event in replay)
    assert all(event.get("final_report") != "old report" for event in replay)
    assert old_child_session.event_hub.released is True
    assert old_child_session.event_hub.journal_size == 0


@pytest.mark.asyncio
async def test_fresh_child_identity_skips_live_session_occupying_first_candidate():
    runner = _runner()
    manager = _session(runner, "collision-manager")
    runner._run_child_agent_to_final_report = AsyncMock(return_value="old report")
    await runner._dispatching_parallel_agent_for_session(
        manager,
        "stable-child",
        "Complete the old child task before manager disposal.",
        turn_dispatch_id="old-dispatch",
        call_id="old-call",
    )
    old_run_id = next(iter(runner.child_agent_runs))
    await runner.dispose_session(manager.session_id)

    replacement_manager = _session(runner, manager.session_id)
    first_suffix = "111111111111"
    first_candidate = old_run_id
    unrelated = _session(
        session_id=first_candidate,
        agent=SimpleNamespace(memory=SimpleNamespace(steps=["unrelated history"])),
        mode="tool_calling",
    )
    runner.sessions.put(unrelated)
    runner._run_child_agent_to_final_report = AsyncMock(return_value="new report")

    with patch(
        "backend.agent.agent_runner.uuid.uuid4",
        side_effect=[
            SimpleNamespace(hex=first_suffix),
        ],
    ):
        with pytest.raises(SessionBusyError):
            await runner._dispatching_parallel_agent_for_session(
                replacement_manager,
                "stable-child",
                "Reject the new child task instead of changing its canonical identity.",
                turn_dispatch_id="new-dispatch",
                call_id="new-call",
            )

    assert runner.sessions.get(first_candidate) is unrelated
    assert unrelated.agent.memory.steps == ["unrelated history"]
    assert runner.child_agent_sessions_by_key.get(
        (replacement_manager.session_id, "stable-child")
    ) is None
    assert all(
        not session_id.startswith(f"{old_run_id}-")
        for session_id in runner.sessions.session_ids()
    )
    assert all(
        not run_id.startswith(f"{old_run_id}-")
        for run_id in runner.child_agent_runs
    )


@pytest.mark.asyncio
async def test_fresh_child_identity_retries_when_candidate_is_claimed_before_insert():
    runner = _runner()
    manager = _session(runner, "toctou-manager")
    runner._run_child_agent_to_final_report = AsyncMock(return_value="old report")
    await runner._dispatching_parallel_agent_for_session(
        manager,
        "stable-child",
        "Complete the old child task before manager disposal.",
        turn_dispatch_id="old-dispatch",
        call_id="old-call",
    )
    old_run_id = next(iter(runner.child_agent_runs))
    await runner.dispose_session(manager.session_id)

    replacement_manager = _session(runner, manager.session_id)
    first_suffix = "aaaaaaaaaaaa"
    first_candidate = old_run_id
    unrelated = _session(
        session_id=first_candidate,
        agent=SimpleNamespace(memory=SimpleNamespace(steps=["unrelated history"])),
        mode="tool_calling",
    )
    build_count = 0

    def build_child_agent(*args, **kwargs):
        nonlocal build_count
        build_count += 1
        if build_count == 1:
            runner.sessions.put(unrelated)
        return SimpleNamespace(
            memory=SimpleNamespace(steps=[]),
            state={},
        )

    runner._run_child_agent_to_final_report = AsyncMock(return_value="new report")
    proposed_sessions = []
    original_release = runner._release_session_resources

    def release_probe(session, seen_images, *, release_hub=True):
        proposed_sessions.append(session)
        return original_release(session, seen_images, release_hub=release_hub)

    runner._release_session_resources = release_probe
    try:
        with patch.object(runner, "_build_profile_agent", side_effect=build_child_agent):
            with pytest.raises(SessionBusyError):
                await runner._dispatching_parallel_agent_for_session(
                    replacement_manager,
                    "stable-child",
                    "Reject the deterministic ID race without retrying a suffix.",
                    turn_dispatch_id="new-dispatch",
                    call_id="new-call",
                )
    finally:
        runner._release_session_resources = original_release

    assert build_count == 1
    assert proposed_sessions
    assert proposed_sessions[-1].session_id == old_run_id
    assert proposed_sessions[-1].event_hub.released is True
    assert runner.sessions.get(first_candidate) is unrelated
    assert unrelated.agent.memory.steps == ["unrelated history"]
    assert unrelated.is_child_context is False
    assert runner.child_agent_sessions_by_key.get(
        (replacement_manager.session_id, "stable-child")
    ) is None
    assert all(
        not session_id.startswith(f"{old_run_id}-")
        for session_id in runner.sessions.session_ids()
    )
    assert all(
        not run_id.startswith(f"{old_run_id}-")
        for run_id in runner.child_agent_runs
    )


@pytest.mark.asyncio
async def test_top_level_creation_cannot_overwrite_child_that_claims_explicit_id():
    runner = _runner()
    manager = _session(runner, "claim-race-manager")
    target_session_id = "child-wins-explicit-id"
    top_level_build_started = threading.Event()
    allow_top_level_insert = threading.Event()
    top_level_results = []
    top_level_errors = []

    def build_agent(profile_name, *args, **kwargs):
        if profile_name == runner.config.agent.entry_agent_name:
            top_level_build_started.set()
            if not allow_top_level_insert.wait(2):
                raise TimeoutError("top-level insert gate timed out")
        return SimpleNamespace(memory=SimpleNamespace(steps=[]), state={})

    def create_top_level():
        try:
            top_level_results.append(
                runner.get_or_create_session(target_session_id, "tool_calling")
            )
        except BaseException as exc:
            top_level_errors.append(exc)

    runner._run_child_agent_to_final_report = AsyncMock(return_value="child report")
    with (
        patch.object(runner, "_build_profile_agent", side_effect=build_agent),
        patch.object(
            runner, "_make_child_identity_id", return_value=target_session_id
        ),
    ):
        top_level_thread = threading.Thread(target=create_top_level, daemon=True)
        top_level_thread.start()
        assert top_level_build_started.wait(1)
        try:
            outcome = await runner._dispatching_parallel_agent_for_session(
                manager,
                "stable-child",
                "Claim the explicit identity before top-level insertion resumes.",
                turn_dispatch_id="claim-race-dispatch",
                call_id="claim-race-call",
            )
            child_run = next(
                child_run
                for child_run in runner.child_agent_runs.values()
                if child_run.call_id == "claim-race-call"
            )
            child_session = runner.sessions.get(child_run.child_run_id)
            assert outcome.status == "completed"
            assert child_session is not None
            assert child_session.is_child_context is True
        finally:
            allow_top_level_insert.set()
            top_level_thread.join(2)

    assert top_level_thread.is_alive() is False
    assert top_level_results == []
    assert len(top_level_errors) == 1
    assert isinstance(top_level_errors[0], SessionBusyError)
    assert runner.sessions.get(target_session_id) is child_session


@pytest.mark.asyncio
async def test_replacement_manager_disposal_ignores_prior_generation_replay_context_id():
    runner = _runner()
    public_manager_id = "generation-manager"
    first_manager = _session(runner, public_manager_id)
    runner._run_child_agent_to_final_report = AsyncMock(return_value="old report")
    await runner._dispatching_parallel_agent_for_session(
        first_manager,
        "stable-child",
        "Complete the first generation child task before disposal.",
        turn_dispatch_id="first-dispatch",
        call_id="first-call",
    )
    old_run = next(iter(runner.child_agent_runs.values()))
    await runner.dispose_session(first_manager.session_id)
    assert old_run.child_run_id not in runner.child_agent_runs

    unrelated = _session(
        session_id=old_run.child_run_id,
        agent=SimpleNamespace(
            memory=SimpleNamespace(steps=["unrelated exact history"]),
            state={"owner": "unrelated"},
        ),
        mode="tool_calling",
    )
    runner.sessions.put(unrelated)
    replacement_manager = _session(runner, public_manager_id)
    runner._run_child_agent_to_final_report = AsyncMock(return_value="new report")
    await runner._dispatching_parallel_agent_for_session(
        replacement_manager,
        "stable-child",
        "Complete the replacement generation child task before disposal.",
        turn_dispatch_id="second-dispatch",
        call_id="second-call",
    )
    new_run = next(
        child_run
        for child_run in runner.child_agent_runs.values()
        if child_run.call_id == "second-call"
    )
    new_child_session = runner.sessions.get(new_run.child_run_id)
    assert new_child_session is not None
    assert new_child_session is not unrelated

    await runner.dispose_session(replacement_manager.session_id)

    assert runner.sessions.get(unrelated.session_id) is unrelated
    assert unrelated.agent.memory.steps == ["unrelated exact history"]
    assert unrelated.agent.state == {"owner": "unrelated"}
    assert unrelated.aborted is False
    assert abort_manager.is_aborted(unrelated.session_id) is False
    assert runner.sessions.get(new_run.child_run_id) is None
    assert old_run.child_run_id not in runner.child_agent_runs
    assert new_run.child_run_id not in runner.child_agent_runs


def test_agent_admission_rollback_keeps_event_unset_until_durable_terminal_retry():
    runner, child_session, child_run = _terminal_phase_fixture(
        "durable-rollback-retry-child"
    )
    runner._record_child_agent_event(
        child_run,
        {
            "type": "manager_assigned_task",
            "child_run_id": child_run.child_run_id,
            "child_name": child_run.child_name,
            "call_id": child_run.call_id,
            "task": child_run.assigned_task,
        },
    )
    original_terminal = runner._record_child_terminal_event
    terminal_calls = 0

    def persistent_terminal_failure(*_args, **_kwargs):
        nonlocal terminal_calls
        terminal_calls += 1
        raise RuntimeError("terminal recorder remains unavailable")

    with patch.object(
        runner,
        "_record_child_terminal_event",
        side_effect=persistent_terminal_failure,
    ):
        runner._rollback_child_admission(
            child_run,
            child_session,
            error="admission terminal failure",
        )

    assert terminal_calls == 2
    assert child_run.terminal_event.is_set() is False
    assert child_session.event_hub.capture_active_producer() is child_run.event_hub_producer_token
    assert child_session.event_hub.has_active_producer is True

    runner._record_child_terminal_event = original_terminal
    runner._rollback_child_admission(
        child_run,
        child_session,
        error="admission terminal failure",
    )
    assert child_run.terminal_event.is_set() is True
    assert child_session.event_hub.has_active_producer is False
    assert [record.payload["type"] for record in child_session.event_hub._journal] == [
        "manager_assigned_task",
        "error",
    ]


def test_agent_admission_rollback_keeps_event_unset_until_close_release_retry():
    runner, child_session, child_run = _terminal_phase_fixture(
        "no-anchor-rollback-retry-child"
    )
    real_release = child_session.event_hub.release_producer

    with patch.object(
        child_session.event_hub,
        "release_producer",
        side_effect=RuntimeError("producer release remains unavailable"),
    ):
        runner._rollback_child_admission(
            child_run,
            child_session,
            anchor_visible=False,
            error="admission close failure",
        )

    assert child_run.terminal_event.is_set() is False
    assert child_session.event_hub.capture_active_producer() is child_run.event_hub_producer_token
    assert child_session.event_hub.has_active_producer is True
    assert child_session.event_hub.journal_size == 0

    child_session.event_hub.release_producer = real_release
    runner._rollback_child_admission(
        child_run,
        child_session,
        anchor_visible=False,
        error="admission close failure",
    )
    assert child_run.terminal_event.is_set() is True
    assert child_session.event_hub.has_active_producer is False
    assert child_session.event_hub.journal_size == 0


def test_agent_assignment_durability_uses_locked_hub_payload_query():
    runner, child_session, child_run = _terminal_phase_fixture(
        "locked-query-admission-child"
    )
    expected = {
        "type": "manager_assigned_task",
        "child_run_id": child_run.child_run_id,
        "child_name": child_run.child_name,
        "call_id": child_run.call_id,
        "task": child_run.assigned_task,
    }
    query_result = SimpleNamespace(payload=expected)
    query = MagicMock(return_value=(query_result,))
    child_session.event_hub.find_payloads_by_fields = query

    class JournalForbidden(list):
        def __iter__(self):
            raise AssertionError("AgentRunner must use the Hub locked query")

    child_session.event_hub._journal = JournalForbidden()
    assert runner._child_assignment_anchor_is_durable(child_run, child_session) is True
    query.assert_called_once_with(expected)


@pytest.mark.asyncio
async def test_no_anchor_close_failure_preserves_token_until_close_retry():
    runner = _runner()
    manager = _session(runner, "close-failure-manager")
    child_name = "close-failure-child"
    stable_id = runner._make_child_identity_id(manager.session_id, child_name)
    child_session = _session(
        session_id=stable_id,
        agent=MagicMock(),
        mode="tool_calling",
        tool_configuration_snapshot=manager.tool_configuration_snapshot,
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    runner.sessions.put(child_session)
    child_run = ChildAgentRun(
        child_run_id=stable_id,
        manager_session_id=manager.session_id,
        dispatch_id="dispatch-1",
        child_name=child_name,
        profile_name="basic_agent",
        assigned_task="Retry after no-anchor close failure.",
        status="pending",
        child_session_identity=child_session,
    )
    token = child_session.event_hub.claim_producer()
    assert token is not None
    child_run.event_hub_producer_token = token
    runner.child_agent_runs[stable_id] = child_run
    runner.child_agent_sessions_by_key[(manager.session_id, child_name)] = stable_id
    real_close = child_session.event_hub.close
    real_release = child_session.event_hub.release_producer

    with (
        patch.object(
            child_session.event_hub,
            "close",
            side_effect=RuntimeError("close remains unavailable"),
        ),
        patch.object(
            child_session.event_hub,
            "release_producer",
            wraps=real_release,
        ) as release_attempt,
    ):
        runner._rollback_child_admission(
            child_run,
            child_session,
            anchor_visible=False,
            error="close failure",
        )

    assert release_attempt.call_count == 0
    assert child_run.terminal_event.is_set() is False
    assert child_run.event_hub_producer_token is token
    assert child_session.event_hub.capture_active_producer() is token
    assert child_session.event_hub.has_active_producer is True

    child_session.event_hub.close = real_close
    runner._rollback_child_admission(
        child_run,
        child_session,
        anchor_visible=False,
        error="close failure",
    )
    assert child_run.terminal_event.is_set() is True
    assert child_session.event_hub.has_active_producer is False

    runner._run_child_agent_to_final_report = AsyncMock(return_value="retry report")
    retry = await runner._dispatching_parallel_agent_for_session(
        manager,
        child_name,
        "Retry the same stable child after close recovery.",
        turn_dispatch_id="dispatch-2",
        call_id="call-2",
    )
    assert retry.status == "completed"
    assert runner.sessions.get(stable_id) is child_session


def test_child_hub_is_the_only_retained_event_source():
    runner = _runner()
    child_run_id = "bounded-child"
    child_session = _session(
        session_id=child_run_id,
        agent=MagicMock(),
        mode="tool_calling",
        is_child_context=True,
    )
    runner.sessions.put(child_session)
    child_run = ChildAgentRun(
        child_run_id=child_run_id,
        manager_session_id="manager",
        dispatch_id="dispatch",
        child_name="reader",
        profile_name="basic_agent",
        assigned_task="Inspect the independent source carefully.",
        child_session_identity=child_session,
    )
    child_run.event_hub_producer_token = child_session.event_hub.claim_producer()
    runner.child_agent_runs[child_run_id] = child_run
    runner._record_child_agent_event(child_run, {
        "type": "manager_assigned_task", "task": "Previous turn task",
        "reused_context": False, "prior_step_count": 0,
    })
    runner._record_child_agent_event(child_run, {"type": "content", "content": "old"})
    runner._record_child_agent_event(child_run, {
        "type": "manager_assigned_task", "task": "Current turn task",
        "reused_context": True, "prior_step_count": 12,
    })
    for index in range(8):
        runner._record_child_agent_event(child_run, {"type": "content", "content": str(index)})
    runner._record_child_terminal_event(child_run, {
        "type": "aborted", "error": "The user has aborted this child agent run."
    })

    payloads = _child_hub_payloads(runner, child_run)
    assert len(payloads) == 12
    assert payloads[0]["type"] == "manager_assigned_task"
    assert payloads[-1]["type"] == "aborted"
    assert all(payload.get("type") != "truncated" for payload in payloads)
    sequences = [event["seq"] for event in payloads]
    assert sequences == sorted(sequences)


def _terminal_phase_fixture(session_id: str):
    runner = _runner()
    child_session = _session(
        session_id=session_id,
        agent=MagicMock(),
        mode="tool_calling",
        is_child_context=True,
    )
    runner.sessions.put(child_session)
    child_run = ChildAgentRun(
        child_run_id=session_id,
        manager_session_id="manager",
        dispatch_id="dispatch",
        child_name="reader",
        profile_name="basic_agent",
        assigned_task="Inspect the independent source carefully.",
        child_session_identity=child_session,
    )
    child_run.event_hub_producer_token = child_session.event_hub.claim_producer()
    runner.child_agent_runs[child_run.child_run_id] = child_run
    return runner, child_session, child_run


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "aborted", "timeout"])
def test_child_terminal_payloads_include_explicit_status(terminal_status):
    runner, _child_session, child_run = _terminal_phase_fixture(
        f"status-{terminal_status}-child"
    )
    child_run.status = terminal_status
    child_run.final_report = "completed report"
    child_run.error = f"{terminal_status} error"

    runner._ensure_disposed_child_terminal_replay(child_run)

    terminal = _child_hub_payloads(runner, child_run)[-1]
    assert terminal["status"] == terminal_status
    assert terminal["child_run_id"] == child_run.child_run_id
    assert terminal["call_id"] == child_run.call_id


def test_child_terminal_statuses_are_canonical():
    assert agent_runner_module.CHILD_TERMINAL_STATUSES == frozenset(
        {"completed", "failed", "aborted", "timeout"}
    )


def test_child_terminal_retry_resumes_after_close_mutated_then_raised():
    runner, child_session, child_run = _terminal_phase_fixture("close-fault-child")
    real_close = child_session.event_hub.close
    attempts = 0

    def close_then_raise(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        result = real_close(*args, **kwargs)
        if attempts == 1:
            raise RuntimeError("one-shot close fault after mutation")
        return result

    child_session.event_hub.close = close_then_raise
    payload = {"type": "done", "child_run_id": child_run.child_run_id}
    with pytest.raises(RuntimeError, match="close fault"):
        runner._record_child_terminal_event(child_run, payload)

    assert child_session.event_hub.journal_size == 1
    assert child_session.event_hub.has_active_producer is True
    runner._record_child_terminal_event(child_run, payload)

    assert child_session.event_hub.journal_size == 1
    assert child_session.event_hub.has_active_producer is False
    assert child_session.event_hub.close_reason == "completed"
    assert child_run.event_hub_terminal_appended is True
    assert child_run.event_hub_terminal_closed is True
    assert child_run.event_hub_producer_released is True


@pytest.mark.asyncio
async def test_child_terminal_retry_does_not_touch_replacement_after_release_fault():
    runner, child_session, child_run = _terminal_phase_fixture("release-fault-child")
    old_token = child_run.event_hub_producer_token
    handoff = asyncio.create_task(child_session.event_hub.claim_producer_after(old_token))
    await asyncio.sleep(0)
    real_release = child_session.event_hub.release_producer
    attempts = 0

    def release_then_raise(token):
        nonlocal attempts
        attempts += 1
        result = real_release(token)
        if attempts == 1:
            raise RuntimeError("one-shot release fault after mutation")
        return result

    child_session.event_hub.release_producer = release_then_raise
    payload = {"type": "done", "child_run_id": child_run.child_run_id}
    with pytest.raises(RuntimeError, match="release fault"):
        runner._record_child_terminal_event(child_run, payload)
    replacement_token = await asyncio.wait_for(handoff, timeout=1)
    assert replacement_token is child_session.event_hub.capture_active_producer()

    runner._record_child_terminal_event(child_run, payload)

    assert child_session.event_hub.journal_size == 1
    assert child_session.event_hub.capture_active_producer() is replacement_token
    assert child_run.event_hub_terminal_appended is True
    assert child_run.event_hub_terminal_closed is True
    assert child_run.event_hub_producer_released is True
    assert child_session.event_hub.release_producer(replacement_token) is True


def test_concurrent_child_terminal_calls_append_exactly_one_terminal_record():
    runner, child_session, child_run = _terminal_phase_fixture("concurrent-terminal-child")
    real_append_batch = child_session.event_hub.append_batch
    append_entered = threading.Event()
    allow_append = threading.Event()
    append_calls = 0

    def synchronized_append(*args, **kwargs):
        nonlocal append_calls
        append_calls += 1
        append_entered.set()
        assert allow_append.wait(timeout=2)
        return real_append_batch(*args, **kwargs)

    child_session.event_hub.append_batch = synchronized_append
    failures = []

    def terminalize():
        try:
            runner._record_child_terminal_event(
                child_run,
                {"type": "done", "child_run_id": child_run.child_run_id},
            )
        except BaseException as exc:
            failures.append(exc)

    callers = [threading.Thread(target=terminalize) for _ in range(2)]
    for caller in callers:
        caller.start()
    assert append_entered.wait(timeout=2)
    allow_append.set()
    for caller in callers:
        caller.join(timeout=2)

    assert all(not caller.is_alive() for caller in callers)
    assert failures == []
    assert append_calls == 1
    assert child_session.event_hub.journal_size == 1
    assert child_run.event_hub_producer_released is True


def test_child_terminal_append_mutated_then_raised_reconciles_exact_record():
    runner, child_session, child_run = _terminal_phase_fixture("append-fault-child")
    real_append_batch = child_session.event_hub.append_batch
    attempts = 0

    def append_then_raise(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        records = real_append_batch(*args, **kwargs)
        if attempts == 1:
            raise RuntimeError("one-shot append fault after mutation")
        return records

    child_session.event_hub.append_batch = append_then_raise
    payload = {"type": "done", "child_run_id": child_run.child_run_id}
    with pytest.raises(RuntimeError, match="append fault"):
        runner._record_child_terminal_event(child_run, payload)

    assert child_session.event_hub.journal_size == 1
    assert child_run.event_hub_terminal_appended is True
    runner._record_child_terminal_event(child_run, payload)

    assert attempts == 1
    assert child_session.event_hub.journal_size == 1
    assert child_run.event_hub_terminal_closed is True
    assert child_run.event_hub_producer_released is True
