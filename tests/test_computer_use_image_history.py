from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from smolagents.memory import AgentMemory, ActionStep, TaskStep, ToolCall
from smolagents.models import MessageRole
from smolagents.monitoring import Timing

from backend.agent.agent_runner import SmartToolCallingAgent


def _image(color):
    return Image.new("RGB", (2, 2), color=color)


def _action(number, image, observation="ok"):
    return ActionStep(
        step_number=number,
        timing=Timing(start_time=float(number), end_time=float(number) + 0.1),
        model_output=f"step-{number}",
        tool_calls=[ToolCall(name="exec_command", arguments={"command": "vision_desktop observe"}, id=f"call-{number}")],
        observations=observation,
        observations_images=[image],
    )


def _agent(steps, session):
    with patch.object(SmartToolCallingAgent, "__init__", lambda self, **kwargs: None):
        agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)
    agent.memory = AgentMemory(system_prompt="system")
    agent.memory.steps = list(steps)
    agent.logger = MagicMock()
    agent._agent_session = session
    session.agent = agent
    return agent


def _session():
    return SimpleNamespace(
        session_id="11111111-1111-4111-8111-111111111111",
        is_child_context=False,
        answering_now=False,
        answer_now=False,
        behavior_records={},
        computer_use_history_omission_stage=None,
        computer_use_history_projection_enabled=False,
    )


def _image_parts(messages):
    return [
        part["image"]
        for message in messages
        if isinstance(message.content, list)
        for part in message.content
        if isinstance(part, dict) and part.get("type") == "image"
    ]


def _texts(messages):
    return [
        part["text"]
        for message in messages
        if isinstance(message.content, list)
        for part in message.content
        if isinstance(part, dict) and part.get("type") == "text"
    ]


def test_write_memory_preserves_trusted_computer_use_image_and_tool_trace():
    from backend.agent.computer_use_history import mark_computer_use_observation

    session = _session()
    uploaded = _image("green")
    computer = _image("red")
    duplicate_task_copy = _image("blue")
    mark_computer_use_observation(
        computer,
        context_id=session.session_id,
        observation_id="obs-1",
        geometry_version=1,
    )
    action = _action(1, computer)
    action.observations_images.append(duplicate_task_copy)
    agent = _agent([TaskStep(task="task", task_images=[uploaded]), action], session)

    messages = agent.write_memory_to_messages()

    assert uploaded in _image_parts(messages)
    assert computer in _image_parts(messages)
    assert duplicate_task_copy not in _image_parts(messages)
    assert [message.role for message in messages].count(MessageRole.TOOL_CALL) == 1
    assert [message.role for message in messages].count(MessageRole.TOOL_RESPONSE) == 1


def test_staged_projection_keeps_latest_and_commits_only_after_barrier():
    from backend.agent.computer_use_history import (
        commit_computer_use_history_omission,
        mark_computer_use_observation,
        stage_computer_use_history_omission,
    )

    session = _session()
    old = _image("red")
    latest = _image("blue")
    mark_computer_use_observation(old, context_id=session.session_id, observation_id="obs-1", geometry_version=1)
    mark_computer_use_observation(latest, context_id=session.session_id, observation_id="obs-2", geometry_version=1)
    old_step = _action(1, old, "old observation")
    latest_step = _action(2, latest, "latest observation")
    agent = _agent([old_step, latest_step], session)

    stage = stage_computer_use_history_omission(session)
    session.computer_use_history_projection_enabled = True
    projected = agent.write_memory_to_messages()

    assert _image_parts(projected) == [latest]
    assert any("computer_use_observation" in text for text in _texts(projected))
    assert old_step.observations_images == [old]
    assert latest_step.observations_images == [latest]

    commit_computer_use_history_omission(session, stage)
    session.computer_use_history_projection_enabled = False
    committed = agent.write_memory_to_messages()
    assert _image_parts(committed) == [latest]
    assert any("computer_use_observation" in text for text in _texts(committed))
    assert old_step.observations_images == []
    assert latest_step.observations_images == [latest]
    assert session.behavior_records["prefix_boundaries"][-1]["reason"] == "computer_use_historical_image_omission"


def test_rollback_restores_unprojected_history_and_leaves_task_images_unchanged():
    from backend.agent.computer_use_history import (
        mark_computer_use_observation,
        rollback_computer_use_history_omission,
        stage_computer_use_history_omission,
    )

    session = _session()
    ordinary = _image("green")
    vision_automation = _image("yellow")
    old = _image("red")
    latest = _image("blue")
    mark_computer_use_observation(old, context_id=session.session_id, observation_id="obs-1", geometry_version=1)
    mark_computer_use_observation(latest, context_id=session.session_id, observation_id="obs-2", geometry_version=1)
    old_step, latest_step = _action(1, old), _action(2, latest)
    agent = _agent(
        [TaskStep(task="ordinary", task_images=[ordinary]), TaskStep(task="vision", task_images=[vision_automation]), old_step, latest_step],
        session,
    )

    stage = stage_computer_use_history_omission(session)
    session.computer_use_history_projection_enabled = True
    assert _image_parts(agent.write_memory_to_messages()) == [ordinary, vision_automation, latest]

    rollback_computer_use_history_omission(session, stage)
    session.computer_use_history_projection_enabled = False
    restored = agent.write_memory_to_messages()
    assert _image_parts(restored) == [ordinary, vision_automation, old, latest]
    assert old_step.observations_images == [old]
    assert latest_step.observations_images == [latest]


def test_child_context_never_stages_computer_use_omission():
    from backend.agent.computer_use_history import mark_computer_use_observation, stage_computer_use_history_omission

    session = _session()
    session.is_child_context = True
    first, second = _image("red"), _image("blue")
    mark_computer_use_observation(first, context_id=session.session_id, observation_id="obs-1", geometry_version=1)
    mark_computer_use_observation(second, context_id=session.session_id, observation_id="obs-2", geometry_version=1)
    _agent([_action(1, first), _action(2, second)], session)

    assert stage_computer_use_history_omission(session) is None


def test_staging_can_include_current_action_step_before_smolagents_appends_it():
    from backend.agent.computer_use_history import (
        commit_computer_use_history_omission,
        mark_computer_use_observation,
        stage_computer_use_history_omission,
    )

    session = _session()
    first = _image("red")
    latest = _image("blue")
    mark_computer_use_observation(
        first, context_id=session.session_id, observation_id="obs-1", geometry_version=1
    )
    mark_computer_use_observation(
        latest, context_id=session.session_id, observation_id="obs-2", geometry_version=1
    )
    first_step = _action(1, first)
    latest_step = _action(2, latest)
    _agent([first_step], session)

    stage = stage_computer_use_history_omission(session, pending_step=latest_step)
    assert stage is not None
    session.agent.memory.steps.append(latest_step)
    assert commit_computer_use_history_omission(session, stage)
    assert first_step.observations_images == []
    assert latest_step.observations_images == [latest]


def test_local_model_commits_only_nonempty_ordinary_response():
    from backend.agent.computer_use_history import mark_computer_use_observation, stage_computer_use_history_omission
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    from smolagents.models import ChatMessage

    session = _session()
    session.current_agent_name = "basic_agent"
    session.last_llm_prompt_snapshot = None
    session.last_compactable_llm_messages = None
    session.last_compactable_agent_name = None
    first, second = _image("red"), _image("blue")
    mark_computer_use_observation(first, context_id=session.session_id, observation_id="obs-1", geometry_version=1)
    mark_computer_use_observation(second, context_id=session.session_id, observation_id="obs-2", geometry_version=1)
    _agent([_action(1, first), _action(2, second)], session)
    stage = stage_computer_use_history_omission(session)

    empty_service = MagicMock()
    empty_service.chat.return_value = GenerationResult(text="")
    empty_model = LocalLLMModel(empty_service, session=session)
    empty_model.generate(_projected_messages(session.agent, session), computer_use_history_stage=stage)
    assert session.computer_use_history_omission_stage is stage
    assert first in session.agent.memory.steps[0].observations_images

    accepted_service = MagicMock()
    accepted_service.chat.return_value = GenerationResult(text="accepted")
    accepted_model = LocalLLMModel(accepted_service, session=session)
    accepted_model.generate(_projected_messages(session.agent, session), computer_use_history_stage=stage)
    assert session.computer_use_history_omission_stage is None
    assert session.agent.memory.steps[0].observations_images == []


def test_local_model_stream_commits_only_after_stream_acceptance():
    from backend.agent.computer_use_history import (
        mark_computer_use_observation,
        stage_computer_use_history_omission,
    )
    from backend.agent.local_llm_model import LocalLLMModel
    from smolagents.models import ChatMessage

    session = _session()
    session.current_agent_name = "basic_agent"
    session.last_llm_prompt_snapshot = None
    session.last_compactable_llm_messages = None
    session.last_compactable_agent_name = None
    first, second = _image("red"), _image("blue")
    mark_computer_use_observation(
        first, context_id=session.session_id, observation_id="obs-1", geometry_version=1
    )
    mark_computer_use_observation(
        second, context_id=session.session_id, observation_id="obs-2", geometry_version=1
    )
    _agent([_action(1, first), _action(2, second)], session)
    stage = stage_computer_use_history_omission(session)

    class StreamingService:
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))

        def chat(self, **kwargs):
            yield {"reasoning": "partial"}
            assert session.computer_use_history_omission_stage is stage
            yield {"content": "accepted"}

    model = LocalLLMModel(StreamingService(), session=session)
    deltas = list(model.generate_stream(_projected_messages(session.agent, session), computer_use_history_stage=stage))
    assert deltas
    assert session.computer_use_history_omission_stage is None
    assert session.agent.memory.steps[0].observations_images == []


def _lifecycle_session():
    from backend.agent.agent_runner import AgentSession
    from backend.agent.computer_use_history import mark_computer_use_observation, stage_computer_use_history_omission
    session = AgentSession(session_id='11111111-1111-4111-8111-111111111111',
                           agent=MagicMock(), mode='tool_calling')
    session.current_agent_name = 'basic_agent'
    first, latest = _image('red'), _image('blue')
    for index, image in enumerate((first, latest)):
        mark_computer_use_observation(image, context_id=session.session_id,
                                      observation_id=f'obs-{index}', geometry_version=1)
    agent = _agent([TaskStep(task='earlier'), TaskStep(task='current'),
                    _action(1, first), _action(2, latest)], session)
    session.active_agent = agent
    return session, agent, stage_computer_use_history_omission(session)


def _projected_messages(agent, session):
    session.computer_use_history_projection_enabled = True
    try:
        return agent.write_memory_to_messages()
    finally:
        session.computer_use_history_projection_enabled = False


def test_aborted_reasoning_worker_cleanup_then_resume_preserves_observed_steps():
    from backend.agent.agent_runner import current_session_context
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    session, agent, stage = _lifecycle_session()
    steps = list(agent.memory.steps)
    containers = [step.observations_images for step in steps if isinstance(step, ActionStep)]
    scope = session.begin_run()
    current_session_context.set(session.session_id, scope)

    class AbortedService:
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))
        def chat(self, **kwargs):
            yield {'reasoning': 'partial thought'}
            yield {'aborted': True}
    try:
        model = LocalLLMModel(AbortedService(), session=session)
        stream = model.generate_stream(_projected_messages(agent, session), request_id='aborted-worker',
                                       computer_use_history_stage=stage)
        assert next(stream).reasoning
        session.abort_active_runs()
        assert list(stream) == []
        agent.memory.steps.append(ActionStep(step_number=3, model_output='partial thought',
            observations=None, tool_calls=[], timing=Timing(start_time=3)))
        session.rollback_run_state(1, True, {})
        session.finish_run(scope)
        assert session.computer_use_history_omission_stage is stage
        assert agent.memory.steps[2] is steps[2] and agent.memory.steps[3] is steps[3]
        assert agent.memory.steps[2].observations_images is containers[0]
        assert agent.memory.steps[3].observations_images is containers[1]
        assert getattr(agent.memory.steps[-1], '_interrupted_by_stop', False)
        resumed_scope = session.begin_run()
        current_session_context.set(session.session_id, resumed_scope)
        service = MagicMock()
        service.chat.return_value = GenerationResult(text='resumed')
        LocalLLMModel(service, session=session).generate(_projected_messages(agent, session),
                                                        computer_use_history_stage=stage)
        assert session.computer_use_history_omission_stage is None
        assert agent.memory.steps[2].observations_images == []
        assert agent.memory.steps[3].observations_images is containers[1]
        session.finish_run(resumed_scope)
    finally:
        current_session_context.clear()
        session.finish_run(scope)


def test_late_old_reasoning_response_cannot_commit_new_candidate_after_abort():
    from backend.agent.agent_runner import current_session_context
    from backend.agent.computer_use_history import mark_computer_use_observation, stage_computer_use_history_omission
    from backend.agent.local_llm_model import LocalLLMModel
    session, agent, old_stage = _lifecycle_session()
    scope = session.begin_run()
    current_session_context.set(session.session_id, scope)
    class LateService:
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))
        def chat(self, **kwargs):
            yield {'reasoning': 'partial thought'}
            yield {'content': 'late provider response'}
    try:
        stream = LocalLLMModel(LateService(), session=session).generate_stream(
            _projected_messages(agent, session), request_id='late-worker',
            computer_use_history_stage=old_stage)
        next(stream)
        session.abort_active_runs()
        newer = _image('yellow')
        mark_computer_use_observation(newer, context_id=session.session_id,
                                      observation_id='newer', geometry_version=1)
        agent.memory.steps.append(_action(3, newer))
        new_stage = stage_computer_use_history_omission(session)
        list(stream)
        assert session.computer_use_history_omission_stage is new_stage
        assert all(step.observations_images for step in agent.memory.steps if isinstance(step, ActionStep))
        assert not session.behavior_records.get('prefix_boundaries')
    finally:
        current_session_context.clear()
        session.finish_run(scope)


def test_response_without_serialized_projection_cannot_commit_current_candidate():
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    from smolagents.models import ChatMessage
    session, agent, stage = _lifecycle_session()
    service = MagicMock()
    service.chat.return_value = GenerationResult(text='plain unrelated request')
    LocalLLMModel(service, session=session).generate([ChatMessage(role=MessageRole.USER, content='go')])
    assert session.computer_use_history_omission_stage is stage
    assert agent.memory.steps[2].observations_images


def test_empty_stream_fallback_preserves_pending_projection():
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    session, agent, stage = _lifecycle_session()
    class FallbackService:
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))
        def chat(self, **kwargs):
            return iter(()) if kwargs['stream'] else GenerationResult(text='fallback result')
    list(LocalLLMModel(FallbackService(), session=session).generate_stream(
        _projected_messages(agent, session), computer_use_history_stage=stage))
    assert session.computer_use_history_omission_stage is stage
    assert agent.memory.steps[2].observations_images


def test_genuine_image_mutation_still_fails_closed_before_commit():
    from backend.agent.computer_use_history import commit_computer_use_history_omission
    session, agent, stage = _lifecycle_session()
    agent.memory.steps[2].observations_images = list(agent.memory.steps[2].observations_images)
    with pytest.raises(RuntimeError, match='image container changed'):
        commit_computer_use_history_omission(session, stage)
    assert agent.memory.steps[2].observations_images


@pytest.mark.parametrize('stream_outputs', [True, False])
def test_real_agent_step_binds_serialized_stage_without_provider_metadata_leak(stream_outputs):
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    session, old_agent, stage = _lifecycle_session()
    class Service:
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))
        def __init__(self):
            self.requests = []
        def get_stream_usage(self, *args, **kwargs):
            return None
        def chat(self, **kwargs):
            self.requests.append(kwargs)
            assert 'computer_use_history_stage' not in kwargs
            assert session.computer_use_history_omission_stage is stage
            assert old_agent.memory.steps[2].observations_images
            return iter([{'content': 'accepted answer'}]) if kwargs['stream'] else GenerationResult(text='accepted answer')
    service = Service()
    agent = SmartToolCallingAgent(tools=[], model=LocalLLMModel(service, session=session),
                                  stream_outputs=stream_outputs)
    agent.memory = old_agent.memory
    agent._agent_session = session
    session.agent = session.active_agent = agent
    list(agent._step_stream(ActionStep(step_number=3, timing=Timing(start_time=3))))
    assert session.computer_use_history_omission_stage is None
    assert agent.memory.steps[2].observations_images == []
    assert agent.memory.steps[3].observations_images
    assert 'historical computer-use images omitted' in repr(service.requests[0]['messages'])


def test_threaded_late_sync_response_after_run_cleanup_cannot_consume_permission():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from backend.agent.agent_runner import current_session_context
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    session, agent, stage = _lifecycle_session()
    scope = session.begin_run()
    entered, released = threading.Event(), threading.Event()
    class DelayedService:
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))
        def chat(self, **kwargs):
            entered.set()
            assert released.wait(5)
            return GenerationResult(text='late after cleanup')
    model = LocalLLMModel(DelayedService(), session=session)
    messages = _projected_messages(agent, session)
    def worker():
        current_session_context.set(session.session_id, scope)
        try:
            return model.generate(messages, computer_use_history_stage=stage)
        finally:
            current_session_context.clear()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker)
        try:
            assert entered.wait(5)
            session.abort_active_runs()
            session.rollback_run_state(1, True, {})
            session.finish_run(scope)  # Clears abort marker; captured scope is still retired.
        finally:
            released.set()
        assert future.result(timeout=5).content == 'late after cleanup'
    assert session.computer_use_history_omission_stage is stage
    assert agent.memory.steps[2].observations_images
    assert not session.behavior_records.get('prefix_boundaries')


def test_ordinary_accepted_request_still_rejects_unexpected_memory_mutation():
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    session, agent, stage = _lifecycle_session()
    class MutatingService:
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))
        def chat(self, **kwargs):
            agent.memory.steps[2].observations_images = []
            return GenerationResult(text='accepted but memory corrupted')
    with pytest.raises(RuntimeError, match='history changed before omission commit'):
        LocalLLMModel(MutatingService(), session=session).generate(
            _projected_messages(agent, session), computer_use_history_stage=stage)
    assert session.computer_use_history_omission_stage is stage


def test_replaced_memory_does_not_serialize_or_consume_old_pending_candidate():
    from backend.agent.computer_use_history import computer_use_history_stage_for_request
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    session, agent, stage = _lifecycle_session()
    old_memory = agent.memory
    agent.memory = AgentMemory(system_prompt='profile boundary')
    agent.memory.steps = [TaskStep(task='compacted replacement')]
    assert computer_use_history_stage_for_request(session) is None
    service = MagicMock()
    service.chat.return_value = GenerationResult(text='new memory accepted')
    LocalLLMModel(service, session=session).generate(agent.write_memory_to_messages(
        computer_use_history_stage=None), computer_use_history_stage=stage)
    assert session.computer_use_history_omission_stage is stage
    assert old_memory.steps[2].observations_images


def test_stop_retains_canonical_read_file_observation_and_tool_calls():
    session, agent, stage = _lifecycle_session()
    read_step = ActionStep(step_number=3, timing=Timing(start_time=3, end_time=4),
        model_output='reading file', tool_calls=[ToolCall(name='read_file', arguments={'path': '/known'}, id='read')],
        observations='immutable file contents')
    agent.memory.steps.append(read_step)
    before = agent.write_memory_to_messages(computer_use_history_stage=None)
    session.rollback_run_state(1, True, {})
    assert agent.memory.steps[-1] is read_step
    assert read_step.observations == 'immutable file contents'
    assert before == agent.write_memory_to_messages(computer_use_history_stage=None)
