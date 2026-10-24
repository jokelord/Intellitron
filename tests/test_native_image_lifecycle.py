"""Native canonical metadata and image permissions cross real request barriers."""
from copy import deepcopy
from types import SimpleNamespace
import base64
import io

from PIL import Image
import pytest
from smolagents.memory import ActionStep, TaskStep, ToolCall
from smolagents.models import ChatMessage
from smolagents.monitoring import Timing

from backend.agent.agent_runner import AgentRunner, AgentSession, SmartToolCallingAgent, SessionBusyError, current_session_context
from backend.agent.computer_use_history import computer_use_provenance, mark_computer_use_observation, stage_computer_use_history_omission
from backend.agent.local_llm_model import LocalLLMModel
from backend.config import get_config
from backend.llm_service import GenerationResult, Message
from backend.security.capabilities import build_session_capability
from backend.session_hub_runtime import SessionHubRuntime
from backend.vision.contracts import VisionAutomationBinding, VisionAutomationTransportMetadata


SESSION_ID = '11111111-1111-4111-8111-111111111111'
TASK_ID = 'task_01J0123456789ABCDEFGH'
CYCLE_ID = 'cycle_01J0123456789ABCDEFG'
CLEANUP = 'cleanup_01J0123456789ABCDE'


class CaptureInference:
    """Only the inference transport is substituted; preparation remains real."""
    config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=4))

    def __init__(self, mode='accepted'):
        self.mode = mode
        self.requests = []

    def get_stream_usage(self, *args, **kwargs):
        return None

    def discard_stream_usage(self, *args, **kwargs):
        return None

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        if self.mode == 'failure':
            raise RuntimeError('inference transport failed before acceptance')
        if kwargs['stream']:
            if self.mode == 'fallback':
                return iter(())
            if self.mode == 'stop':
                return iter([{'reasoning': 'partial thought'}, {'content': 'late answer'}])
            return iter([{'content': 'accepted answer'}])
        return GenerationResult(text='accepted answer')


def native_context(tmp_path, *, child=False, session_id=SESSION_ID, mode='accepted'):
    config = deepcopy(get_config())
    config.model.model_type = 'vision'
    config.inference.tool_protocol = 'qwen35-native'
    config.agent.entry_agent_name = 'basic_agent'
    config.agent.enabled_agent_profiles = ['basic_agent', 'coordinator_1', 'coordinator_2']
    config.agent.session_temp_base_dir = str(tmp_path / session_id)
    service = CaptureInference(mode)
    runner = AgentRunner(service, config, session_hub_runtime=SessionHubRuntime())
    session = AgentSession(session_id=session_id, agent=None, mode='tool_calling',
        tool_protocol='qwen35-native', current_agent_name='basic_agent', is_child_context=child,
        capability=build_session_capability(config, session_id=session_id, mode='tool_calling'),
        tool_configuration_snapshot=runner.default_tool_configuration_snapshot)
    agent = SmartToolCallingAgent(tools=[], model=LocalLLMModel(service, session=session),
        stream_outputs=True, tool_protocol='qwen35-native')
    agent._agent_session = session
    session.agent = session.active_agent = agent
    runner.sessions.put(session)
    return runner, session, agent, service


def observation(number, image):
    return ActionStep(step_number=number, timing=Timing(start_time=float(number), end_time=number+.1),
        model_output='inspect-' + str(number), observations='  observation ' + str(number) + '\n  ',
        tool_calls=[ToolCall(name='exec_command', arguments={'command': 'vision_desktop observe'}, id='observe-' + str(number))],
        observations_images=[image])


def computer_history(agent, session):
    upload = Image.new('RGB', (6, 6), 'green')
    old = Image.new('RGB', (12, 8), 'red')
    latest = Image.new('RGB', (12, 8), 'blue')
    for number, image in enumerate((old, latest), 1):
        mark_computer_use_observation(image, context_id=session.session_id,
            observation_id='observation-' + str(number), geometry_version=4)
    first, second = observation(1, old), observation(2, latest)
    agent.memory.steps = [TaskStep(task='ordinary user upload', task_images=[upload]), first, second]
    stage = stage_computer_use_history_omission(session)
    return upload, old, latest, first, second, stage


def image_objects(messages):
    return [part['image'] for message in messages if isinstance(message.content, list)
        for part in message.content if isinstance(part, dict) and part.get('type') == 'image']


def image_dimensions(messages):
    sizes = []
    for message in messages:
        if isinstance(message.content, list):
            for part in message.content:
                if part.get('type') == 'image_url':
                    url = part['image_url']['url']
                    sizes.append(Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))).size)
    return sizes


def assert_tool_trace(messages):
    assistants = [m for m in messages if m.role == 'assistant' and m.tool_calls]
    responses = [m for m in messages if m.role == 'tool']
    assert [m.tool_calls[0]['id'] for m in assistants] == ['observe-1', 'observe-2']
    assert [(m.tool_call_id, m.name, m.content) for m in responses] == [
        ('observe-1', 'exec_command', '  observation 1\n  '),
        ('observe-2', 'exec_command', '  observation 2\n  ')]


@pytest.mark.parametrize('stream_outputs', [False, True])
def test_native_ordinary_step_commits_only_accepted_trusted_image_projection(tmp_path, stream_outputs):
    runner, session, agent, service = native_context(tmp_path)
    upload, old, latest, first, second, stage = computer_history(agent, session)
    agent.stream_outputs = stream_outputs
    canonical = agent.write_memory_to_messages(computer_use_history_stage=None)
    assert image_objects(canonical) == [upload, old, latest]
    assert_tool_trace(canonical)
    list(agent._step_stream(ActionStep(step_number=3, timing=Timing(start_time=3.))))
    request = service.requests[0]
    assert 'computer_use_history_stage' not in request
    assert_tool_trace(request['messages'])
    assert image_dimensions(request['messages']) == [(4, 4), (12, 8)]
    assert 'provenance=computer_use_observation' in repr(request['messages'])
    assert session.computer_use_history_omission_stage is None
    assert first.observations_images == []
    assert second.observations_images == [latest]
    assert computer_use_provenance(latest, session.session_id).observation_id == 'observation-2'
    assert image_objects(agent.write_memory_to_messages()) == [upload, latest]
    assert session.behavior_records['prefix_boundaries'][-1]['reason'] == 'computer_use_historical_image_omission'


@pytest.mark.parametrize('mode', ['failure', 'fallback'])
def test_native_failed_transport_and_empty_stream_fallback_do_not_consume_projection(tmp_path, mode):
    runner, session, agent, service = native_context(tmp_path, mode=mode)
    upload, old, latest, first, second, stage = computer_history(agent, session)
    messages = agent.write_memory_to_messages(computer_use_history_stage=stage)
    if mode == 'failure':
        with pytest.raises(RuntimeError, match='transport failed'):
            agent.model.generate(messages, computer_use_history_stage=stage)
    else:
        assert list(agent.model.generate_stream(messages, computer_use_history_stage=stage))
        assert [request['stream'] for request in service.requests] == [True, False]
    assert_tool_trace(service.requests[0]['messages'])
    assert session.computer_use_history_omission_stage is stage
    assert first.observations_images == [old]
    assert second.observations_images == [latest]
    assert not session.behavior_records.get('prefix_boundaries')


def test_native_stop_after_partial_thinking_does_not_accept_late_image_projection(tmp_path):
    runner, session, agent, service = native_context(tmp_path, mode='stop')
    upload, old, latest, first, second, stage = computer_history(agent, session)
    scope = session.begin_run()
    current_session_context.set(session.session_id, scope)
    try:
        stream = agent.model.generate_stream(agent.write_memory_to_messages(computer_use_history_stage=stage),
            request_id='native-stopped-observation', computer_use_history_stage=stage)
        assert next(stream).reasoning
        session.abort_active_runs()
        list(stream)
        assert session.computer_use_history_omission_stage is stage
        assert first.observations_images == [old]
        assert second.observations_images == [latest]
        assert_tool_trace(agent.write_memory_to_messages(computer_use_history_stage=None))
        assert not session.behavior_records.get('prefix_boundaries')
    finally:
        session.finish_run(scope)
        current_session_context.clear()


@pytest.mark.asyncio
async def test_native_answer_now_retains_real_screenshots_and_pending_permission(tmp_path):
    runner, session, agent, service = native_context(tmp_path)
    upload, old, latest, first, second, stage = computer_history(agent, session)
    result = await runner.generate_answer_now_reply(session.session_id)
    assert result['status'] == 'ok'
    assert result['answer'] == 'accepted answer'
    assert_tool_trace(service.requests[0]['messages'])
    assert image_dimensions(service.requests[0]['messages']) == [(4, 4), (12, 8), (12, 8)]
    assert 'historical computer-use images omitted' not in repr(service.requests[0]['messages'])
    assert session.computer_use_history_omission_stage is stage
    assert first.observations_images == [old] and second.observations_images == [latest]
    assert not session.behavior_records.get('prefix_boundaries')


def metadata(session_id=SESSION_ID, *, cleanup=None):
    return VisionAutomationTransportMetadata(history_image_policy='aggregate_prior_image_blocks',
        manual_image_count=0, source_count=1, retained_tick_count=1,
        binding=VisionAutomationBinding(page_generation=1, mode='agent', top_level_context_id=session_id,
            automation_task_id=TASK_ID, capture_cycle_id=CYCLE_ID, cleanup_token=cleanup))


def routing_history(runner, session, image, text='Inspect exactly this routing task.\n  Preserve task text.  '):
    call = session.agent.model._resolve_assistant_control(
        '<tool_call><function=switch_agent><parameter=target_agent>basic_agent</parameter></function></tool_call>',
        None, [], allowed_agent_names={'basic_agent'}).control_call
    user = Message(role='user', content=[{'type': 'image', 'image': image}, {'type': 'text', 'text': text}])
    runner._record_native_routing_call(session, user, ChatMessage(role='assistant', content='', tool_calls=[call]),
        {'control_call': call, 'target_agent': 'basic_agent', 'status': 'completed'})
    return call.id, text


def test_native_vision_accepted_omission_preserves_exact_routing_task_and_receipt(tmp_path):
    runner, session, agent, service = native_context(tmp_path)
    image = Image.new('RGB', (6, 6), 'yellow')
    call_id, text = routing_history(runner, session, image)
    before = agent.write_memory_to_messages()
    assert image_objects(before) == [image]
    stage = runner.stage_agent_request_admission(session.session_id, session.mode, metadata(), inbound_image_count=1)
    accepted, scope = runner._begin_staged_agent_request(stage, session.mode, lambda admission, scopes: None)
    try:
        messages = accepted.agent.write_memory_to_messages()
        tasks = [m for m in messages if m.role == 'user' and isinstance(m.content, list)]
        texts = [part['text'] for m in tasks for part in m.content if part.get('type') == 'text']
        assert text in texts
        assert any('[historical images omitted: count=1;' in value for value in texts)
        assert image_objects(messages) == []
        responses = [m for m in messages if m.role == 'tool']
        assert [(m.tool_call_id, m.name) for m in responses] == [(call_id, 'switch_agent')]
        accepted.agent.model.generate(messages)
        assert [(m.tool_call_id, m.name) for m in service.requests[0]['messages'] if m.role == 'tool'] == [(call_id, 'switch_agent')]
        assert session.behavior_records['vision_automation_historical_image_omission'][-1]['omitted_image_count'] == 1
    finally:
        accepted.finish_run(scope)


def test_native_vision_cleanup_callback_failure_restores_routing_image_and_unconsumed_token(tmp_path):
    runner, session, agent, service = native_context(tmp_path)
    routing_history(runner, session, Image.new('RGB', (6, 6), 'yellow'))
    stage = runner.stage_agent_request_admission(session.session_id, session.mode, metadata(), inbound_image_count=1)
    accepted, scope = runner._begin_staged_agent_request(stage, session.mode, lambda admission, scopes: None)
    accepted.finish_run(scope)
    token = runner.register_agent_vision_automation_cleanup(session.session_id, metadata().binding, CLEANUP)
    newer = Image.new('RGB', (6, 6), 'green')
    call_id, text = routing_history(runner, session, newer, text='Keep newer task exactly.  ')
    before = session.agent.write_memory_to_messages()
    cleanup_stage = runner.stage_agent_request_admission(session.session_id, session.mode, metadata(cleanup=CLEANUP), inbound_image_count=1)

    def reject_acceptance(admission, scopes):
        raise RuntimeError('acceptance registration failed')

    with pytest.raises(RuntimeError, match='acceptance registration failed'):
        runner._begin_staged_agent_request(cleanup_stage, session.mode, reject_acceptance)
    assert session.vision_automation_cleanup_token is token
    assert token.consumed is False
    assert session.agent.write_memory_to_messages() == before
    assert image_objects(before) == [newer]
    prepared, protected = session.agent.model._prepare_model_messages(before)
    assert image_dimensions(prepared) == [(6, 6)]
    assert any(m.tool_call_id == call_id and m.name == 'switch_agent' for m in prepared)
    assert newer.getpixel((0, 0)) == (0, 128, 0)


def test_native_child_cannot_consume_parent_cu_or_vision_permissions(tmp_path):
    runner, session, agent, service = native_context(tmp_path)
    upload, old, latest, first, second, parent_stage = computer_history(agent, session)
    child_id = '22222222-2222-4222-8222-222222222222'
    child_runner, child, child_agent, child_service = native_context(tmp_path, child=True, session_id=child_id)
    child_upload, child_old, child_latest, child_first, child_second, child_stage = computer_history(child_agent, child)
    assert child_stage is None
    child_agent.model.generate(child_agent.write_memory_to_messages(), computer_use_history_stage=parent_stage)
    assert child_first.observations_images == [child_old]
    assert child_second.observations_images == [child_latest]
    assert session.computer_use_history_omission_stage is parent_stage
    assert first.observations_images == [old]
    assert_tool_trace(child_service.requests[0]['messages'])
    with pytest.raises(SessionBusyError) as caught:
        child_runner.stage_agent_request_admission(child.session_id, child.mode, metadata(child_id), inbound_image_count=1)
    assert 'active run' in str(caught.value) or 'child' in str(caught.value)
