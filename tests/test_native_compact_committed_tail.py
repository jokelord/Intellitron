"""Manual compact preserves the accepted prefix and committed execution tails."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from smolagents.memory import ActionStep, TaskStep, ToolCall
from smolagents.monitoring import Timing
from smolagents.tools import Tool

from backend.agent.agent_runner import AgentRunner, AgentSession, SmartToolCallingAgent, SecondaryHandoffRequest
from backend.agent.local_llm_model import LocalLLMModel
from backend.config import get_config
from backend.llm_service import GenerationResult
from backend.security.capabilities import build_session_capability
from backend.compact.types import CompactEventKind


CALL = '<tool_call><function=literal_read><parameter=file_path>/tmp/a</parameter></function></tool_call>'
SWITCH = '<tool_call><function=switch_agent><parameter=target_agent>basic_agent</parameter></function></tool_call>'
OBSERVATION = '  def f():\n    return True\n\n '


class LiteralRead(Tool):
    name = 'literal_read'
    description = 'Read the fixture file.'
    inputs = {'file_path': {'type': 'string', 'description': 'Exact path.'}}
    output_type = 'string'

    def forward(self, file_path: str) -> str:
        assert file_path == '/tmp/a'
        return OBSERVATION


class Transport:
    config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))

    def __init__(self, answer):
        self.answer = answer
        self.compacts = []

    def chat(self, **kwargs):
        return iter([{'content': self.answer}]) if kwargs['stream'] else GenerationResult(text=self.answer)

    def get_stream_usage(self, *args, **kwargs):
        return None

    def discard_stream_usage(self, *args, **kwargs):
        return None

    async def chat_stream_async(self, **kwargs):
        self.compacts.append(kwargs)
        # Deliberately invalid summary leaves canonical memory available to inspect.
        yield {'content': 'invalid summary'}


def context(tmp_path, answer=CALL, profile='basic_agent'):
    config = deepcopy(get_config())
    config.inference.tool_protocol = 'qwen35-native'
    config.model.model_type = 'text'
    config.agent.entry_agent_name = 'basic_agent'
    config.agent.enabled_agent_profiles = ['basic_agent', 'coordinator_1', 'coordinator_2']
    config.agent.session_temp_base_dir = str(tmp_path)
    config.compact.prompt_shape = 'cache_first_tail_with_fallback'
    config.compact.prompt_control_enabled = True
    config.compact.reliable_fallback_enabled = False
    service = Transport(answer)
    runner = AgentRunner(service, config)
    sid = '31313131-3131-4131-8131-313131313131'
    session = AgentSession(session_id=sid, agent=None, mode='tool_calling', tool_protocol='qwen35-native',
        current_agent_name=profile, tool_configuration_snapshot=runner.default_tool_configuration_snapshot,
        capability=build_session_capability(config, session_id=sid, mode='tool_calling'))
    agent = SmartToolCallingAgent(tools=[LiteralRead()], model=LocalLLMModel(service, session=session),
        tool_protocol='qwen35-native', allowed_switch_targets={'basic_agent'} if profile=='coordinator_2' else ())
    agent._agent_session = session
    session.agent = session.active_agent = agent
    agent.memory.steps.append(TaskStep(task='Keep every exact observation.'))
    runner.sessions.put(session)
    return runner, session, agent, service


def ordinary_step(agent):
    step = ActionStep(step_number=1, timing=Timing(start_time=1., end_time=1.1))
    events = list(agent._step_stream(step))
    agent.memory.steps.append(step)
    return step, events


async def manual_request(runner, session, service):
    events = [event async for event in runner.run_compact_for_session(session.session_id)]
    assert events[-1].kind == CompactEventKind.FAILED
    assert len(service.compacts) == 1
    return service.compacts[0]


def assert_prefix_and_schema(request, snapshot, schema):
    assert request['messages'][:len(snapshot)] == snapshot
    assert request['tools'] == schema
    return request['messages'][len(snapshot):-1]


@pytest.mark.asyncio
async def test_manual_compact_after_actual_tool_appends_committed_result_once(tmp_path):
    runner, session, agent, service = context(tmp_path)
    step, events = ordinary_step(agent)
    snapshot = deepcopy(session.last_compactable_llm_messages)
    tools = deepcopy(session.last_compactable_llm_tools)
    request = await manual_request(runner, session, service)
    tail = assert_prefix_and_schema(request, snapshot, tools)
    assert [(m.role, m.tool_call_id, m.name, m.content) for m in tail] == [
        ('tool', step.tool_calls[0].id, 'literal_read', OBSERVATION)]
    assert session.last_compactable_llm_messages == snapshot
    assert agent.memory.steps[-1] is step


@pytest.mark.asyncio
async def test_manual_compact_after_stop_keeps_tool_observation_and_retained_partial_tail(tmp_path):
    runner, session, agent, service = context(tmp_path)
    step, events = ordinary_step(agent)
    snapshot = deepcopy(session.last_compactable_llm_messages)
    tools = deepcopy(session.last_compactable_llm_tools)
    scope = session.begin_run()
    partial = ActionStep(step_number=2, timing=Timing(start_time=2.), model_output='retained partial thought')
    agent.memory.steps.append(partial)
    session.abort_active_runs()
    session.rollback_run_state(1, True, {})
    session.finish_run(scope)
    assert getattr(agent.memory.steps[-1], '_interrupted_by_stop', False)
    request = await manual_request(runner, session, service)
    tail = assert_prefix_and_schema(request, snapshot, tools)
    assert [(m.role, m.content) for m in tail] == [('tool', OBSERVATION), ('assistant', 'retained partial thought')]
    assert tail[0].tool_call_id == step.tool_calls[0].id
    assert tail[1].tool_calls is None


@pytest.mark.asyncio
async def test_manual_compact_after_real_secondary_handoff_retains_old_prefix_and_receipt(tmp_path):
    runner, session, agent, service = context(tmp_path, SWITCH, 'coordinator_2')
    step, events = ordinary_step(agent)
    handoff = next(event for event in events if isinstance(event, SecondaryHandoffRequest))
    snapshot = deepcopy(session.last_compactable_llm_messages)
    tools = deepcopy(session.last_compactable_llm_tools)
    runner._activate_secondary_handoff(session, handoff, source_agent='coordinator_2', fallback_user_goal='Keep inspecting.')
    assert session.agent is not agent
    request = await manual_request(runner, session, service)
    tail = assert_prefix_and_schema(request, snapshot, tools)
    assert len(tail) == 1
    assert (tail[0].role, tail[0].name, tail[0].tool_call_id) == ('tool', 'switch_agent', step.tool_calls[0].id)
    assert 'Handoff request accepted' in tail[0].content
    assert session.last_compactable_llm_messages == snapshot


@pytest.mark.asyncio
async def test_prior_plain_snapshot_appends_actual_answer_now_without_rewriting_prefix(tmp_path):
    runner, session, agent, service = context(tmp_path, 'Normal accepted answer.')
    ordinary_step(agent)
    snapshot = deepcopy(session.last_compactable_llm_messages)
    tools = deepcopy(session.last_compactable_llm_tools)
    service.answer = 'Immediate accepted answer.'
    response = await runner.generate_answer_now_reply(session.session_id)
    assert response['status'] == 'ok'
    request = await manual_request(runner, session, service)
    tail = assert_prefix_and_schema(request, snapshot, tools)
    assert [(m.role, m.content) for m in tail] == [('assistant', 'Immediate accepted answer.')]
    assert session.last_compactable_llm_messages == snapshot


def test_accepted_but_uncommitted_call_does_not_invent_empty_receipt(tmp_path):
    runner, session, agent, service = context(tmp_path)
    response = agent.model.generate(agent.write_memory_to_messages(), tools_to_call_from=agent.tools_and_managed_agents)
    call = response.tool_calls[0]
    # The accepted response exists, but its execution has not committed a result.
    agent.memory.steps.append(ActionStep(step_number=1, timing=Timing(start_time=1.), model_output='',
        tool_calls=[ToolCall(name=call.function.name, arguments=call.function.arguments, id=call.id)]))
    snapshot = deepcopy(session.last_compactable_llm_messages)
    with pytest.raises(ValueError, match='committed.*result|incomplete.*call'):
        runner._build_cache_first_compact_body_messages(session)
    assert session.last_compactable_llm_messages == snapshot


@pytest.mark.asyncio
async def test_manual_compact_changed_anchor_fails_before_inference_and_preserves_memory(tmp_path):
    runner, session, agent, service = context(tmp_path)
    ordinary_step(agent)
    snapshot = deepcopy(session.last_compactable_llm_messages)
    agent.memory.steps[0] = TaskStep(task='unrelated replacement history')
    steps = list(agent.memory.steps)
    events = [event async for event in runner.run_compact_for_session(session.session_id)]
    assert events[-1].kind == CompactEventKind.FAILED
    assert events[-1].metadata['reason'] == 'source_reconciliation_failed'
    assert service.compacts == []
    assert agent.memory.steps == steps
    assert session.last_compactable_llm_messages == snapshot
    assert not session.run_in_progress


def test_native_compact_reconstructed_sources_do_not_project_pending_screenshots_or_consume_ipc(tmp_path, monkeypatch):
    from PIL import Image
    from backend.agent.computer_use_history import mark_computer_use_observation, stage_computer_use_history_omission
    runner, session, agent, service = context(tmp_path, 'no inference yet')
    images = [Image.new('RGB', (3, 3), color) for color in ('red', 'blue')]
    for index, image in enumerate(images, 1):
        mark_computer_use_observation(image, context_id=session.session_id, observation_id=f'observation-{index}', geometry_version=1)
        agent.memory.steps.append(ActionStep(step_number=index, timing=Timing(start_time=float(index)),
            tool_calls=[ToolCall(name='exec_command', arguments={'command':'vision_desktop observe'}, id=f'image-{index}')],
            observations=f'original observation {index}', observations_images=[image]))
    pending = stage_computer_use_history_omission(session)
    session.computer_use_history_projection_enabled = True

    def forbidden_consume(*args, **kwargs):
        raise AssertionError('compact must not consume tool-image IPC')

    monkeypatch.setattr('backend.agent.agent_runner._read_and_consume_tool_images', forbidden_consume)
    sources = [runner._build_compact_body_messages(session, runner.config.agent_prompts.compaction),
        runner._build_cache_first_compact_body_messages(session)]
    for source in sources:
        assert sum(part.get('type') == 'image_url' for message in source if isinstance(message.content, list) for part in message.content) == 2
        assert 'historical computer-use images omitted' not in repr(source)
        assert [m.tool_call_id for m in source if m.role == 'tool'] == ['image-1','image-2']
    assert session.computer_use_history_omission_stage is pending
    assert agent.memory.steps[1].observations_images == [images[0]]
    assert agent.memory.steps[2].observations_images == [images[1]]
    assert not session.behavior_records.get('prefix_boundaries')

@pytest.mark.asyncio
async def test_immediate_tool_image_compact_encodes_trusted_screenshot_without_rewriting_snapshot(tmp_path):
    import base64
    import io
    import json
    from PIL import Image
    from backend.agent.computer_use_history import mark_computer_use_observation
    from backend.agent.canonical_messages import message_to_wire

    runner, session, agent, service = context(tmp_path)
    step, _ = ordinary_step(agent)
    snapshot = deepcopy(session.last_compactable_llm_messages)
    tools = deepcopy(session.last_compactable_llm_tools)
    image = Image.new('RGB', (12, 8), 'blue')
    mark_computer_use_observation(image, context_id=session.session_id, observation_id='accepted-screen', geometry_version=1)
    step.observations_images = [image]
    service.config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=4))
    request = await manual_request(runner, session, service)
    tail = assert_prefix_and_schema(request, snapshot, tools)
    json.dumps([message_to_wire(message) for message in request['messages']])
    image_parts = [part for message in tail if isinstance(message.content, list)
                   for part in message.content if part.get('type') == 'image_url']
    assert len(image_parts) == 1
    url = image_parts[0]['image_url']['url']
    assert url.startswith('data:image/png;base64,')
    assert Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))).size == (12, 8)
    assert session.last_compactable_llm_messages == snapshot
    assert step.observations_images == [image]
