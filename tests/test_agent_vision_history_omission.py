import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from smolagents.memory import ActionStep, AgentMemory, TaskStep, ToolCall
from smolagents.models import MessageRole
from smolagents.monitoring import Timing

from backend.agent.agent_runner import (
    AgentRunner,
    AgentSession,
    SessionStore,
    SessionBusyError,
    SmartToolCallingAgent,
)
from backend.agent.tool_configuration import build_business_tool_catalog
from backend.config import get_config
from backend.vision.contracts import (
    VisionAutomationBinding,
    VisionAutomationTransportMetadata,
)
from backend.vision.history_omission import (
    HistoricalImagePlaceholderTaskStep,
    commit_agent_history_image_omission,
    format_historical_image_placeholder,
    stage_agent_history_image_omission,
)
from backend.vision.validation import VisionRequestValidationError


SESSION_ID = "manager-session-0001"
TASK_ID = "task_01J0123456789ABCDEFGH"
CYCLE_ID = "cycle_01J0123456789ABCDEFG"
CLEANUP_TOKEN = "cleanup_01J0123456789ABCDE"
MANIFEST = """<vision_automation_manifest>
Manual images: 2, image indexes 1-2
Source 1: Desktop, image indexes 3-4
Source 2: Camera 1, image indexes 5-6
Relative times: 0.00s, 4.03s
</vision_automation_manifest>

Inspect these images exactly."""


class TrackingImage:
    def __init__(self, name: str):
        self.name = name
        self.closed = False
        self._pre_encoded_b64 = "retained-base64-payload"
        self._pre_encoded_mime = "image/jpeg"

    def close(self) -> None:
        self.closed = True


def _metadata(
    *,
    page_generation: int = 1,
    task_id: str = TASK_ID,
    cycle_id: str = CYCLE_ID,
    cleanup_token: str | None = None,
    context_id: str = SESSION_ID,
    manual_image_count: int = 0,
    source_count: int = 0,
    retained_tick_count: int = 0,
) -> VisionAutomationTransportMetadata:
    return VisionAutomationTransportMetadata(
        history_image_policy="aggregate_prior_image_blocks",
        manual_image_count=manual_image_count,
        source_count=source_count,
        retained_tick_count=retained_tick_count,
        binding=VisionAutomationBinding(
            page_generation=page_generation,
            mode="agent",
            top_level_context_id=context_id,
            automation_task_id=task_id,
            capture_cycle_id=cycle_id,
            cleanup_token=cleanup_token,
        ),
    )


def _agent_with_steps(steps):
    memory = AgentMemory(system_prompt="SYSTEM")
    memory.steps = list(steps)
    return SimpleNamespace(memory=memory)


def _session(steps, *, session_id: str = SESSION_ID, is_child: bool = False):
    return AgentSession(
        session_id=session_id,
        agent=_agent_with_steps(steps),
        mode="tool_calling",
        is_child_context=is_child,
        tool_configuration_snapshot=build_business_tool_catalog(get_config()).resolve_candidate(None),
    )


def _serialize(memory):
    with patch.object(SmartToolCallingAgent, "__init__", lambda self, **kwargs: None):
        agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)
    agent.memory = memory
    agent.logger = MagicMock()
    return agent.write_memory_to_messages()


def _runner_with_session(session):
    runner = AgentRunner.__new__(AgentRunner)
    runner.config = get_config()
    runner.tool_catalog = build_business_tool_catalog(runner.config)
    runner.default_tool_configuration_snapshot = runner.tool_catalog.resolve_candidate(None)
    runner.sessions = SessionStore()
    runner.sessions.put(session)
    runner._agent_admission_lock = threading.Lock()
    runner._pending_agent_admissions = {}
    runner._session_disposal_tasks = {}
    return runner


def test_runner_fixture_initializes_required_session_disposal_state():
    runner = _runner_with_session(_session([]))

    assert runner._session_disposal_tasks == {}


def test_accepted_automation_frames_enable_sticky_omission_for_later_ordinary_agent_turns():
    historical = TaskStep(task="first vision turn", task_images=[TrackingImage("first")])
    session = _session([historical])
    runner = _runner_with_session(session)

    qualifying = _metadata(source_count=1, retained_tick_count=1)
    qualifying_stage = runner.stage_agent_request_admission(
        session.session_id,
        session.mode,
        qualifying,
        inbound_image_count=1,
    )
    runner.prepare_session_for_new_turn = lambda _session_id: session
    _accepted_session, accepted_scope = runner._begin_staged_agent_request(
        qualifying_stage,
        session.mode,
        lambda _stage, _scope_ids: None,
    )
    session.finish_run(accepted_scope)

    assert getattr(session, "vision_automation_image_omission_sticky", False) is True

    later_ordinary = TaskStep(
        task="ordinary Vision turn after automation",
        task_images=[TrackingImage("later")],
    )
    session.agent.memory.steps.append(later_ordinary)
    ordinary_stage = runner.stage_agent_request_admission(
        session.session_id,
        session.mode,
        None,
    )

    assert ordinary_stage._history_omission is not None


def test_manual_only_automation_request_does_not_enable_sticky_omission():
    historical = TaskStep(task="ordinary Vision history", task_images=[TrackingImage("old")])
    session = _session([historical])
    runner = _runner_with_session(session)
    runner.config = SimpleNamespace(agent=SimpleNamespace(agent_mode="tool_calling"))
    stage = runner.stage_agent_request_admission(
        session.session_id,
        session.mode,
        _metadata(manual_image_count=1),
        inbound_image_count=1,
    )
    runner.prepare_session_for_new_turn = lambda _session_id: session

    _accepted_session, scope_id = runner._begin_staged_agent_request(
        stage,
        session.mode,
        lambda _stage, _scope_ids: None,
    )
    session.finish_run(scope_id)

    assert session.vision_automation_image_omission_sticky is False
    assert session.agent.memory.steps == [historical]
    assert historical.task_images is not None


def test_binding_contract_is_nested_strict_and_requires_high_entropy_ids():
    metadata = _metadata()
    assert metadata.binding.mode == "agent"
    assert metadata.binding.top_level_context_id == SESSION_ID

    with pytest.raises(ValidationError):
        VisionAutomationTransportMetadata(
            history_image_policy="aggregate_prior_image_blocks",
            manual_image_count=0,
            source_count=0,
            retained_tick_count=0,
        )
    with pytest.raises(ValidationError):
        VisionAutomationBinding(
            page_generation=0,
            mode="agent",
            top_level_context_id=SESSION_ID,
            automation_task_id="short",
            capture_cycle_id=CYCLE_ID,
        )
    with pytest.raises(ValidationError):
        VisionAutomationBinding(
            page_generation=-1,
            mode="agent",
            top_level_context_id=SESSION_ID,
            automation_task_id=TASK_ID,
            capture_cycle_id=CYCLE_ID,
        )


def test_placeholder_grammar_normalizes_sources_and_two_decimal_range():
    assert format_historical_image_placeholder(
        count=48,
        sources=("camera_2", "manual", "desktop", "camera_1", "manual"),
        relative_start=0,
        relative_end=15.214,
    ) == (
        "[historical images omitted: count=48; "
        "sources=manual,desktop,camera_1,camera_2; "
        "relative_range=0.00..15.21s]"
    )
    assert format_historical_image_placeholder(
        count=3,
        sources=(),
        relative_start=None,
        relative_end=None,
    ) == (
        "[historical images omitted: count=3; "
        "sources=none; relative_range=unavailable]"
    )


def test_stage_is_immutable_and_commit_preserves_task_tool_trace_and_order():
    images = [TrackingImage(str(index)) for index in range(6)]
    historical = TaskStep(task=MANIFEST, task_images=images)
    action = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
        model_output="I will inspect them.",
        tool_calls=[ToolCall(name="read_file", arguments={"path": "/tmp/a"}, id="call-1")],
        observations="tool observation remains user-role text",
    )
    current = TaskStep(task="Current text-only task", task_images=None)
    session = _session([historical, action, current])
    original_steps = session.agent.memory.steps

    staged = stage_agent_history_image_omission(session, _metadata())

    assert session.agent.memory.steps is original_steps
    assert historical.task_images is images
    assert not any(image.closed for image in images)

    commit_agent_history_image_omission(session, staged)

    committed = session.agent.memory.steps
    assert len(committed) == 3
    assert isinstance(committed[0], HistoricalImagePlaceholderTaskStep)
    assert committed[0].task == MANIFEST
    assert committed[0].task_images is None
    assert committed[1] is action
    assert committed[2] is current
    assert all(image.closed for image in images)
    assert all(not hasattr(image, "_pre_encoded_b64") for image in images)
    assert all(not hasattr(image, "_pre_encoded_mime") for image in images)

    messages = _serialize(session.agent.memory)
    task_message = next(
        message
        for message in messages
        if message.role == MessageRole.USER
        and isinstance(message.content, list)
        and any(MANIFEST in part.get("text", "") for part in message.content)
    )
    assert task_message.content[-1] == {
        "type": "text",
        "text": (
            "[historical images omitted: count=6; "
            "sources=manual,desktop,camera_1; relative_range=0.00..4.03s]"
        ),
    }
    roles = [message.role for message in messages]
    assert roles.index(MessageRole.ASSISTANT) < roles.index(MessageRole.TOOL_CALL)
    assert roles.index(MessageRole.TOOL_CALL) < roles.index(MessageRole.TOOL_RESPONSE)
    tool_response = next(message for message in messages if message.role == MessageRole.TOOL_RESPONSE)
    assert "tool observation remains user-role text" in tool_response.content[0]["text"]


@pytest.mark.parametrize(
    "task",
    [
        "No project manifest here",
        MANIFEST.replace("Camera 1", "Camera A"),
        MANIFEST.replace("image indexes 5-6", "image indexes 5-7"),
        MANIFEST.replace("4.03s", "4.0s"),
        (
            "<vision_automation_manifest>\nManual images: "
            + "9" * 5000
            + ", image indexes 1-1\nRelative times: \n"
            "</vision_automation_manifest>\n\nInspect."
        ),
    ],
)
def test_invalid_or_missing_manifest_uses_non_guessing_fallback(task):
    session = _session([TaskStep(task=task, task_images=[TrackingImage("one")])])

    staged = stage_agent_history_image_omission(session, _metadata())
    commit_agent_history_image_omission(session, staged)

    message = session.agent.memory.steps[0].to_messages()[0]
    assert message.content[-1]["text"] == (
        "[historical images omitted: count=1; "
        "sources=none; relative_range=unavailable]"
    )


def test_commit_rejects_changed_memory_without_mutation_or_resource_release():
    image = TrackingImage("original")
    original = TaskStep(task="old", task_images=[image])
    session = _session([original])
    staged = stage_agent_history_image_omission(session, _metadata())
    replacement = TaskStep(task="concurrent", task_images=None)
    session.agent.memory.steps = [replacement]

    with pytest.raises(RuntimeError, match="changed"):
        commit_agent_history_image_omission(session, staged)

    assert session.agent.memory.steps == [replacement]
    assert image.closed is False


def test_commit_rejects_in_place_task_text_change_without_closing_images():
    image = TrackingImage("original")
    original = TaskStep(task="old", task_images=[image])
    session = _session([original])
    staged = stage_agent_history_image_omission(session, _metadata())
    original.task = "concurrent edit"

    with pytest.raises(RuntimeError, match="changed"):
        commit_agent_history_image_omission(session, staged)

    assert session.agent.memory.steps == [original]
    assert original.task == "concurrent edit"
    assert original.task_images == [image]
    assert image.closed is False


def test_stage_rejects_malformed_canonical_image_container():
    task = TaskStep(task="old", task_images=None)
    task.task_images = "not-a-canonical-container"
    session = _session([task])

    with pytest.raises(VisionRequestValidationError, match="image block"):
        stage_agent_history_image_omission(session, _metadata())


def test_omission_is_scoped_to_one_top_level_session_and_rejects_child_target():
    target_image = TrackingImage("target")
    sibling_image = TrackingImage("sibling")
    child_image = TrackingImage("child")
    target = _session([TaskStep(task="target", task_images=[target_image])])
    sibling = _session(
        [TaskStep(task="sibling", task_images=[sibling_image])],
        session_id="manager-session-0002",
    )
    child = _session(
        [TaskStep(task="child", task_images=[child_image])],
        session_id="child-session-0001",
        is_child=True,
    )

    commit_agent_history_image_omission(
        target,
        stage_agent_history_image_omission(target, _metadata()),
    )

    assert target.agent.memory.steps[0].task_images is None
    assert sibling.agent.memory.steps[0].task_images == [sibling_image]
    assert child.agent.memory.steps[0].task_images == [child_image]
    with pytest.raises(VisionRequestValidationError, match="child"):
        stage_agent_history_image_omission(
            child,
            _metadata(context_id="child-session-0001"),
        )


def test_cleanup_requires_exact_registered_pending_token_and_consumes_once():
    first = TrackingImage("first")
    session = _session([TaskStep(task="first", task_images=[first])])
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, _metadata()),
    )
    second = TrackingImage("second")
    session.agent.memory.steps.append(TaskStep(task="second", task_images=[second]))
    cleanup = _metadata(
        cycle_id="cycle_01J0123456789ABCDEFG2",
        cleanup_token=CLEANUP_TOKEN,
    )

    with pytest.raises(VisionRequestValidationError, match="registered"):
        stage_agent_history_image_omission(session, cleanup)

    runner = _runner_with_session(session)
    registered = runner.register_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )
    assert registered.consumed is False
    assert registered.token_digest == hashlib.sha256(CLEANUP_TOKEN.encode()).hexdigest()
    assert CLEANUP_TOKEN not in repr(registered)
    prompt_text = str(_serialize(session.agent.memory))
    assert CLEANUP_TOKEN not in prompt_text
    assert registered.token_digest not in prompt_text

    with pytest.raises(VisionRequestValidationError, match="registered"):
        stage_agent_history_image_omission(
            session,
            _metadata(
                cycle_id="cycle_01J0123456789ABCDEFG2",
                cleanup_token="cleanup_01J0123456789ABCDF",
            ),
        )
    for generation in (0, 2):
        with pytest.raises(VisionRequestValidationError, match="generation"):
            stage_agent_history_image_omission(
                session,
                _metadata(
                    page_generation=generation,
                    cycle_id="cycle_01J0123456789ABCDEFG2",
                    cleanup_token=CLEANUP_TOKEN,
                ),
            )

    staged = stage_agent_history_image_omission(session, cleanup)
    assert session.vision_automation_cleanup_token is registered
    assert session.vision_automation_cleanup_token.consumed is False
    assert second.closed is False

    commit_agent_history_image_omission(session, staged)

    assert session.vision_automation_cleanup_token is not None
    assert session.vision_automation_cleanup_token.consumed is True
    assert second.closed is True
    session.agent.memory.steps.append(
        TaskStep(task="must remain", task_images=[TrackingImage("third")])
    )
    with pytest.raises(VisionRequestValidationError, match="cleanup token"):
        stage_agent_history_image_omission(session, cleanup)


def test_cleanup_registration_requires_exact_prior_top_level_binding():
    session = _session([TaskStep(task="first", task_images=None)])
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, _metadata()),
    )
    runner = _runner_with_session(session)
    original = _metadata().binding

    for changed in (
        original.model_copy(update={"page_generation": 0}),
        original.model_copy(update={"page_generation": 2}),
        original.model_copy(update={"mode": "chat"}),
        original.model_copy(update={"top_level_context_id": "manager-session-0002"}),
        original.model_copy(update={"capture_cycle_id": "cycle_01J0123456789ABCDEFG2"}),
    ):
        with pytest.raises(VisionRequestValidationError):
            runner.register_agent_vision_automation_cleanup(
                SESSION_ID,
                changed,
                CLEANUP_TOKEN,
            )

    child = _session(
        [TaskStep(task="child", task_images=None)],
        session_id="child-session-0001",
        is_child=True,
    )
    child.vision_automation_binding = original.model_copy(
        update={"top_level_context_id": "child-session-0001"}
    )
    child_runner = _runner_with_session(child)
    with pytest.raises(VisionRequestValidationError, match="child"):
        child_runner.register_agent_vision_automation_cleanup(
            "child-session-0001",
            child.vision_automation_binding,
            CLEANUP_TOKEN,
        )


def test_pending_cleanup_without_historical_block_is_not_consumed():
    session = _session([TaskStep(task="first", task_images=None)])
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, _metadata()),
    )
    runner = _runner_with_session(session)
    registered = runner.register_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )

    staged = stage_agent_history_image_omission(
        session,
        _metadata(cleanup_token=CLEANUP_TOKEN),
    )
    commit_agent_history_image_omission(session, staged)

    assert session.vision_automation_cleanup_token is registered
    assert session.vision_automation_cleanup_token.consumed is False


def test_consumed_cleanup_token_cannot_be_registered_again_for_retriggered_task():
    next_task_id = "task_01J0123456789ABCDEFG2"
    next_cycle_id = "cycle_01J0123456789ABCDEFG2"
    session = _session(
        [TaskStep(task="first", task_images=[TrackingImage("first")])]
    )
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, _metadata()),
    )
    session.agent.memory.steps.append(
        TaskStep(task="cleanup", task_images=[TrackingImage("cleanup")])
    )
    runner = _runner_with_session(session)
    runner.register_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )
    retriggered_cleanup = _metadata(
        task_id=next_task_id,
        cycle_id=next_cycle_id,
        cleanup_token=CLEANUP_TOKEN,
    )
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, retriggered_cleanup),
    )
    assert session.vision_automation_binding.automation_task_id == next_task_id
    assert session.vision_automation_cleanup_token.consumed is True

    with pytest.raises(VisionRequestValidationError, match="consumed"):
        runner.register_agent_vision_automation_cleanup(
            SESSION_ID,
            retriggered_cleanup.binding.model_copy(update={"cleanup_token": None}),
            CLEANUP_TOKEN,
        )


def test_cleanup_cancel_clears_exact_pending_token_idempotently_without_history_change():
    image = TrackingImage("history")
    session = _session([TaskStep(task="history", task_images=[image])])
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, _metadata()),
    )
    current_step = TaskStep(task="current", task_images=[TrackingImage("current")])
    session.agent.memory.steps.append(current_step)
    memory_steps = session.agent.memory.steps
    runner = _runner_with_session(session)
    runner.register_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )

    cancelled = runner.cancel_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )
    repeated = runner.cancel_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )

    assert cancelled is repeated
    assert cancelled.cancelled is True
    assert cancelled.consumed is False
    assert session.agent.memory.steps is memory_steps
    assert session.agent.memory.steps == [session.agent.memory.steps[0], current_step]
    assert current_step.task_images[0].closed is False
    with pytest.raises(VisionRequestValidationError, match="cancelled"):
        stage_agent_history_image_omission(
            session,
            _metadata(cleanup_token=CLEANUP_TOKEN),
        )


def test_cleanup_cancel_before_registration_creates_exact_idempotent_tombstone():
    current_step = TaskStep(task="current", task_images=[TrackingImage("current")])
    session = _session([current_step])
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, _metadata()),
    )
    memory_steps = session.agent.memory.steps
    canonical_steps = tuple(memory_steps)
    runner = _runner_with_session(session)

    cancelled = runner.cancel_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )
    repeated = runner.cancel_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )
    registered_late = runner.register_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )

    assert cancelled is repeated is registered_late
    assert cancelled.cancelled is True
    assert cancelled.consumed is False
    assert cancelled.token_digest == hashlib.sha256(CLEANUP_TOKEN.encode()).hexdigest()
    assert CLEANUP_TOKEN not in repr(cancelled)
    assert session.agent.memory.steps is memory_steps
    assert tuple(session.agent.memory.steps) == canonical_steps


def test_cleanup_cancel_before_registration_rejects_later_mismatched_token():
    session = _session([TaskStep(task="current", task_images=None)])
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, _metadata()),
    )
    runner = _runner_with_session(session)
    runner.cancel_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )

    with pytest.raises(VisionRequestValidationError, match="different|match"):
        runner.register_agent_vision_automation_cleanup(
            SESSION_ID,
            _metadata().binding,
            "cleanup_01J0123456789ABCDF",
        )


def test_cleanup_cancel_rejects_mismatch_consumed_and_child_tokens():
    session = _session([TaskStep(task="first", task_images=None)])
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, _metadata()),
    )
    runner = _runner_with_session(session)
    runner.register_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )

    with pytest.raises(VisionRequestValidationError, match="match"):
        runner.cancel_agent_vision_automation_cleanup(
            SESSION_ID,
            _metadata().binding,
            "cleanup_01J0123456789ABCDF",
        )
    with pytest.raises(VisionRequestValidationError, match="binding"):
        runner.cancel_agent_vision_automation_cleanup(
            SESSION_ID,
            _metadata().binding.model_copy(
                update={"capture_cycle_id": "cycle_01J0123456789ABCDEFG2"}
            ),
            CLEANUP_TOKEN,
        )

    session.agent.memory.steps.append(
        TaskStep(task="cleanup", task_images=[TrackingImage("cleanup")])
    )
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(
            session,
            _metadata(cleanup_token=CLEANUP_TOKEN),
        ),
    )
    with pytest.raises(VisionRequestValidationError, match="consumed"):
        runner.cancel_agent_vision_automation_cleanup(
            SESSION_ID,
            _metadata().binding,
            CLEANUP_TOKEN,
        )

    child = _session(
        [TaskStep(task="child", task_images=None)],
        session_id="child-session-0001",
        is_child=True,
    )
    child_runner = _runner_with_session(child)
    with pytest.raises(VisionRequestValidationError, match="child"):
        child_runner.cancel_agent_vision_automation_cleanup(
            "child-session-0001",
            _metadata(context_id="child-session-0001").binding,
            CLEANUP_TOKEN,
        )


def test_cleanup_cancel_and_staged_admission_have_exact_single_owner():
    session = _session([TaskStep(task="first", task_images=None)])
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, _metadata()),
    )
    runner = _runner_with_session(session)
    runner.config = SimpleNamespace(agent=SimpleNamespace(agent_mode="tool_calling"))
    runner.register_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )
    admission = runner.stage_agent_request_admission(
        SESSION_ID,
        "tool_calling",
        _metadata(cleanup_token=CLEANUP_TOKEN),
    )

    with pytest.raises(SessionBusyError):
        runner.cancel_agent_vision_automation_cleanup(
            SESSION_ID,
            _metadata().binding,
            CLEANUP_TOKEN,
        )

    runner.rollback_agent_request_stage(admission)
    cancelled = runner.cancel_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )
    assert cancelled.cancelled is True


def test_consumed_cleanup_rejects_delayed_old_task_but_allows_new_task():
    session = _session(
        [TaskStep(task="first", task_images=[TrackingImage("first")])]
    )
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, _metadata()),
    )
    session.agent.memory.steps.append(
        TaskStep(task="cleanup", task_images=[TrackingImage("cleanup")])
    )
    cleanup = _metadata(
        cycle_id="cycle_01J0123456789ABCDEFG2",
        cleanup_token=CLEANUP_TOKEN,
    )
    runner = _runner_with_session(session)
    runner.register_agent_vision_automation_cleanup(
        SESSION_ID,
        _metadata().binding,
        CLEANUP_TOKEN,
    )
    commit_agent_history_image_omission(
        session,
        stage_agent_history_image_omission(session, cleanup),
    )
    assert session.vision_automation_binding is None
    session.agent.memory.steps.append(
        TaskStep(task="new image", task_images=[TrackingImage("new")])
    )

    with pytest.raises(VisionRequestValidationError, match="stale"):
        stage_agent_history_image_omission(
            session,
            _metadata(
                cycle_id="cycle_01J0123456789ABCDEFG3",
            ),
        )

    fresh = stage_agent_history_image_omission(
        session,
        _metadata(
            task_id="task_01J0123456789ABCDEFG2",
            cycle_id="cycle_01J0123456789ABCDEFG4",
        ),
    )
    assert fresh.omitted_image_count == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "image_free_container",
        "image_free_task_text",
        "tool_arguments",
        "tool_calls_container",
        "observations",
        "model_output",
        "planning_step",
        "memory_container",
        "ordering",
    ],
)
def test_commit_detects_full_memory_step_drift_without_closing_images(mutation):
    from smolagents.memory import PlanningStep
    from smolagents.models import ChatMessage

    image = TrackingImage("historical")
    image_task = TaskStep(task="image task", task_images=[image])
    text_task = TaskStep(task="text task", task_images=None)
    action = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
        model_output="model output",
        tool_calls=[ToolCall(name="read_file", arguments={"path": "/tmp/a"}, id="call-1")],
        observations="observation",
    )
    planning = PlanningStep(
        model_input_messages=[ChatMessage(role=MessageRole.USER, content="input")],
        model_output_message=ChatMessage(role=MessageRole.ASSISTANT, content="output"),
        plan="preserved plan",
        timing=Timing(start_time=0.0, end_time=0.1),
    )
    session = _session([image_task, text_task, action, planning])
    staged = stage_agent_history_image_omission(session, _metadata())

    if mutation == "image_free_container":
        text_task.task_images = [TrackingImage("new")]
    elif mutation == "image_free_task_text":
        text_task.task = "changed text"
    elif mutation == "tool_arguments":
        action.tool_calls[0].arguments["path"] = "/tmp/b"
    elif mutation == "tool_calls_container":
        action.tool_calls = list(action.tool_calls)
    elif mutation == "observations":
        action.observations = "changed observation"
    elif mutation == "model_output":
        action.model_output = "changed output"
    elif mutation == "planning_step":
        planning.plan = "changed plan"
    elif mutation == "memory_container":
        session.agent.memory.steps = list(session.agent.memory.steps)
    elif mutation == "ordering":
        session.agent.memory.steps[1], session.agent.memory.steps[2] = (
            session.agent.memory.steps[2],
            session.agent.memory.steps[1],
        )

    with pytest.raises(RuntimeError, match="changed"):
        commit_agent_history_image_omission(session, staged)
    assert image.closed is False


def test_runner_commits_before_acceptance_and_clears_stale_compact_sources():
    image = TrackingImage("history")
    session = _session([TaskStep(task="history", task_images=[image])])
    session.last_compactable_llm_messages = [SimpleNamespace(content=image)]
    session.last_compactable_agent_name = "basic_agent"
    session.last_llm_prompt_snapshot = SimpleNamespace(content_hash="stale")
    runner = AgentRunner.__new__(AgentRunner)
    runner.config = get_config()
    runner.tool_catalog = build_business_tool_catalog(runner.config)
    runner.default_tool_configuration_snapshot = runner.tool_catalog.resolve_candidate(None)
    runner.config = SimpleNamespace(agent=SimpleNamespace(agent_mode="tool_calling"))
    runner.sessions = SessionStore()
    runner.sessions.put(session)
    runner._agent_admission_lock = __import__("threading").Lock()
    runner._pending_agent_admissions = {}
    runner._session_disposal_tasks = {}
    runner.prepare_session_for_new_turn = lambda session_id: session
    stage = runner.stage_agent_request_admission(
        SESSION_ID,
        "tool_calling",
        _metadata(source_count=1, retained_tick_count=1),
        inbound_image_count=1,
    )
    observed = []

    def on_registered(received_stage, scope_ids):
        assert received_stage is stage
        assert scope_ids
        observed.append("accepted")
        assert isinstance(
            session.agent.memory.steps[0], HistoricalImagePlaceholderTaskStep
        )
        assert session.last_compactable_llm_messages is None
        assert session.last_compactable_agent_name is None
        assert session.last_llm_prompt_snapshot is None

    assert stage.projected_historical_image_count == 0
    accepted_session, run_scope_id = runner._begin_staged_agent_request(
        stage,
        "tool_calling",
        on_registered,
    )
    accepted_session.finish_run(run_scope_id)

    assert observed == ["accepted"]
    assert stage._history_omission is None
    assert image.closed is True
    diagnostics = session.behavior_records[
        "vision_automation_historical_image_omission"
    ]
    assert diagnostics[-1]["reason"] == (
        "vision_automation_historical_image_omission"
    )
    assert diagnostics[-1]["omitted_image_count"] == 1
    prompt_text = str(_serialize(session.agent.memory))
    assert TASK_ID not in prompt_text
    assert CYCLE_ID not in prompt_text


def test_acceptance_callback_failure_rolls_back_without_closing_images():
    image = TrackingImage("history")
    original = TaskStep(task="history", task_images=[image])
    session = _session([original])
    stale_messages = [SimpleNamespace(content=image)]
    stale_snapshot = SimpleNamespace(content_hash="stale")
    session.last_compactable_llm_messages = stale_messages
    session.last_compactable_agent_name = "basic_agent"
    session.last_llm_prompt_snapshot = stale_snapshot
    runner = AgentRunner.__new__(AgentRunner)
    runner.config = get_config()
    runner.tool_catalog = build_business_tool_catalog(runner.config)
    runner.default_tool_configuration_snapshot = runner.tool_catalog.resolve_candidate(None)
    runner.config = SimpleNamespace(agent=SimpleNamespace(agent_mode="tool_calling"))
    runner.sessions = SessionStore()
    runner.sessions.put(session)
    runner._agent_admission_lock = __import__("threading").Lock()
    runner._pending_agent_admissions = {}
    runner._session_disposal_tasks = {}
    runner.prepare_session_for_new_turn = lambda session_id: session
    stage = runner.stage_agent_request_admission(
        SESSION_ID,
        "tool_calling",
        _metadata(source_count=1, retained_tick_count=1),
        inbound_image_count=1,
    )

    def reject_acceptance(received_stage, scope_ids):
        assert isinstance(
            session.agent.memory.steps[0], HistoricalImagePlaceholderTaskStep
        )
        raise RuntimeError("acceptance failed")

    with pytest.raises(RuntimeError, match="acceptance failed"):
        runner._begin_staged_agent_request(
            stage,
            "tool_calling",
            reject_acceptance,
        )

    assert session.agent.memory.steps == [original]
    assert session.agent.memory.steps[0].task_images == [image]
    assert image.closed is False
    assert session.last_compactable_llm_messages is stale_messages
    assert session.last_compactable_agent_name == "basic_agent"
    assert session.last_llm_prompt_snapshot is stale_snapshot
    assert session.vision_automation_binding is None
    assert session.vision_automation_image_omission_sticky is False
    assert "vision_automation_historical_image_omission" not in session.behavior_records
    assert stage._history_omission is None
    assert stage._rolled_back is True
    assert session.run_in_progress is False
