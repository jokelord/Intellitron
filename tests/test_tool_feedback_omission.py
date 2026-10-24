"""Opt-in historical tool text omission at ordinary user admission only."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image
from smolagents.memory import ActionStep, TaskStep, ToolCall
from smolagents.monitoring import Timing
from smolagents.utils import AgentParsingError

from backend.agent.agent_runner import AgentRunner
from backend.agent.canonical_messages import CanonicalToolResult, canonicalize_model_messages
from backend.config import ConfigLoader, get_config
from backend.llm_service import GenerationResult


LONG = '工具反馈 abc\n' * 20


class Transport:
    active_backend = 'sglang'
    config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))

    def __init__(self):
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        return iter([{'content': 'Done.'}]) if kwargs['stream'] else GenerationResult(text='Done.')

    def get_stream_usage(self, *args, **kwargs):
        return None

    def discard_stream_usage(self, *args, **kwargs):
        return None


def context(tmp_path, protocol='legacy_text', enabled=True, threshold=10):
    config = deepcopy(get_config())
    config.inference.tool_protocol = protocol
    config.model.model_type = 'text'
    config.agent.entry_agent_name = 'basic_agent'
    config.agent.session_temp_base_dir = str(tmp_path)
    config.agent.tool_feedback_omission = SimpleNamespace(enabled=enabled, threshold_chars=threshold)
    config.compact.auto_trigger = False
    service = Transport()
    runner = AgentRunner(service, config)
    session = runner.get_or_create_session('31313131-3131-4131-8131-313131313131', 'tool_calling')
    session.has_run = True
    session.agent.memory.steps.append(TaskStep(task='Earlier user task'))
    return runner, session, service


def action(text=LONG, *, calls=None, error=None, results=None):
    step = ActionStep(step_number=1, timing=Timing(start_time=1., end_time=1.1),
        model_output='Original assistant words', observations=text, error=error,
        tool_calls=calls or [ToolCall(name='read_file', arguments={'file_path': '/tmp/a'}, id='call-a')])
    if results is not None:
        step.canonical_tool_results = results
    return step


def messages(session):
    return canonicalize_model_messages(session.agent.write_memory_to_messages(consume_tool_images=False))


def text_content(body):
    return '\n'.join(m.content if isinstance(m.content, str) else '\n'.join(
        str(part.get('text', '')) for part in (m.content or []) if isinstance(part,dict)) for m in body)


def load_config(tmp_path, monkeypatch, subtree):
    import backend.config as mod
    monkeypatch.setattr(mod, 'PROJECT_ROOT', tmp_path)
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    (config_dir / 'config-overall.yaml').write_text(yaml.safe_dump({'agent_operation':'config/op.yaml'}))
    agent = {'entry_agent_name':'basic_agent'}
    if subtree is not ...:
        agent['tool_feedback_omission'] = subtree
    (config_dir / 'op.yaml').write_text(yaml.safe_dump({'agent':agent}))
    return ConfigLoader(config_dir / 'config-overall.yaml').load()


def test_missing_yaml_switch_preserves_long_feedback_by_default(tmp_path, monkeypatch):
    config = load_config(tmp_path, monkeypatch, ...)
    assert config.agent.tool_feedback_omission.enabled is False
    assert config.agent.tool_feedback_omission.threshold_chars == 10000


@pytest.mark.parametrize('subtree', [None, [], {'enabled':'false'}, {'threshold_chars':True},
    {'threshold_chars':0}, {'threshold_chars':-1}, {'threshold_chars':1.5}, {'threshold_chars':'10'}])
def test_invalid_yaml_omission_settings_fail_with_precise_path(tmp_path, monkeypatch, subtree):
    with pytest.raises(ValueError, match='agent.tool_feedback_omission'):
        load_config(tmp_path, monkeypatch, subtree)


def test_yaml_omission_configuration_loads_as_typed_settings(tmp_path, monkeypatch):
    config = load_config(tmp_path, monkeypatch, {'enabled':True, 'threshold_chars':4321})
    assert config.agent.tool_feedback_omission.enabled is True
    assert config.agent.tool_feedback_omission.threshold_chars == 4321


@pytest.mark.parametrize('protocol', ['legacy_text', 'qwen35-native'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_next_admitted_user_omits_history_but_current_task_keeps_feedback(tmp_path, protocol, stream):
    runner, session, service = context(tmp_path, protocol)
    old = action()
    session.agent.memory.steps.append(old)
    assert LONG in text_content(messages(session))  # Serialization alone never consumes eligibility.
    stage = runner.stage_agent_request_admission(session.session_id, session.mode, None)
    if stream:
        _ = [event async for event in runner.run_stream(session.session_id, 'Next user task',
            admission_stage=stage, on_run_registered=lambda *_:None)]
    else:
        await runner.run_sync(session.session_id, 'Next user task',
            admission_stage=stage, on_run_registered=lambda *_:None)
    assert stage._accepted
    assert service.requests
    assert LONG not in text_content(service.requests[-1]['messages'])
    assert 'feedback omitted by configuration' in repr(service.requests[-1]['messages'])
    assert old.observations == LONG  # Original UI/tool feedback remains available.
    assert 'Original assistant words' in repr(service.requests[-1]['messages'])
    assert '/tmp/a' in repr(service.requests[-1]['messages'])
    fresh = action('Current task result ' * 20)
    session.agent.memory.steps.append(fresh)
    assert fresh.observations in text_content(messages(session))
    assert not session.run_in_progress
    assert session.behavior_records['prefix_boundaries'][-1]['reason'] == 'tool_feedback_historical_text_omission'


@pytest.mark.parametrize('text, omitted', [('中文字ab',False), ('中文字ab!',True), ('a b\nc',False), ('a b\nc!',True)])
def test_strict_threshold_counts_unicode_characters_including_whitespace(tmp_path, text, omitted):
    runner, session, _ = context(tmp_path, threshold=5)
    session.agent.memory.steps.append(action(text))
    runner._omit_historical_tool_feedback(session)
    body = repr(messages(session))
    assert ('feedback omitted by configuration' in body) is omitted
    if not omitted:
        assert text in text_content(messages(session))


def test_disabled_and_child_context_are_byte_preserving(tmp_path):
    runner, session, _ = context(tmp_path, enabled=False)
    session.agent.memory.steps.append(action())
    before = messages(session)
    runner._omit_historical_tool_feedback(session)
    assert messages(session) == before
    runner.config.agent.tool_feedback_omission.enabled = True
    session.is_child_context = True
    runner._omit_historical_tool_feedback(session)
    assert messages(session) == before
    assert not session.behavior_records.get('prefix_boundaries')


def test_native_per_result_preserves_metadata_images_and_failed_feedback(tmp_path):
    runner, session, _ = context(tmp_path, 'qwen35-native')
    image = {'type':'image_url', 'image_url':{'url':'data:image/png;base64,unchanged'}, 'provenance':'computer_use_observation'}
    calls = [ToolCall(name='read_file', arguments={'file_path':'/tmp/a'}, id=f'call-{i}') for i in range(4)]
    results = [CanonicalToolResult('call-0','read_file',LONG),
        CanonicalToolResult('call-1','read_file','short'),
        CanonicalToolResult('call-2','read_file',[{'type':'text','text':LONG}, image, {'type':'text','text':'end'}]),
        CanonicalToolResult('call-3','read_file',LONG,'failed')]
    step = action(calls=calls, results=results)
    session.agent.memory.steps.append(step)
    runner._omit_historical_tool_feedback(session)
    tool_messages = [m for m in messages(session) if m.role == 'tool']
    assert [(m.tool_call_id,m.name) for m in tool_messages] == [(f'call-{i}','read_file') for i in range(4)]
    assert 'feedback omitted by configuration' in tool_messages[0].content
    assert tool_messages[1].content == 'short'
    assert image in tool_messages[2].content
    assert sum(p.get('type')=='text' for p in tool_messages[2].content) == 1
    assert tool_messages[3].content == LONG
    assert step.canonical_tool_results == results
    before = messages(session)
    boundaries = deepcopy(session.behavior_records['prefix_boundaries'])
    runner._omit_historical_tool_feedback(session)
    assert messages(session) == before
    assert session.behavior_records['prefix_boundaries'] == boundaries


def test_legacy_combined_block_errors_task_and_computer_use_images_are_retained(tmp_path):
    from backend.agent.computer_use_history import mark_computer_use_observation
    runner, session, _ = context(tmp_path)
    screenshot = Image.new('RGB',(3,3),'red')
    mark_computer_use_observation(screenshot, context_id=session.session_id, observation_id='observation-a', geometry_version=1)
    combined = action('a'*6+'\n'+'b'*6, calls=[ToolCall(name='read_file',arguments={},id='a'),ToolCall(name='read_file',arguments={},id='b')])
    combined.observations_images = [screenshot]
    error = action(LONG, error=AgentParsingError('Exact parser position feedback', session.agent.logger))
    session.agent.memory.steps += [combined, error]
    runner._omit_historical_tool_feedback(session)
    body = messages(session)
    assert 'feedback omitted by configuration' in repr(body)
    assert LONG in text_content(body)
    assert 'Exact parser position feedback' in repr(body)
    assert combined.observations_images == [screenshot]
    assert any(isinstance(m.content,list) and any(p.get('type') == 'image' for p in m.content) for m in body)
    assert 'Earlier user task' in repr(body)


@pytest.mark.parametrize('protocol', ['legacy_text', 'qwen35-native'])
def test_omission_is_idempotent_after_config_disable_and_clears_stale_compact_source(tmp_path, protocol):
    runner, session, _ = context(tmp_path, protocol)
    session.agent.memory.steps.append(action())
    session.last_compactable_llm_messages = messages(session)
    session.last_compactable_llm_tools = [{'old':'schema'}]
    session.last_compactable_memory_steps = tuple(session.agent.memory.steps)
    session.last_compactable_agent_name = 'basic_agent'
    runner._omit_historical_tool_feedback(session)
    boundaries = deepcopy(session.behavior_records['prefix_boundaries'])
    body = messages(session)
    runner.config.agent.tool_feedback_omission.enabled = False
    runner._omit_historical_tool_feedback(session)
    assert messages(session) == body
    assert session.behavior_records['prefix_boundaries'] == boundaries
    assert session.last_compactable_llm_messages is None
    assert session.last_compactable_memory_steps is None
    assert session.last_compactable_llm_tools is None
    assert LONG not in text_content(runner._build_cache_first_compact_body_messages(session))
    assert LONG not in text_content(runner._build_compact_body_messages(session,runner.config.agent_prompts.compaction))
    assert LONG not in text_content(runner._session_memory_to_messages(session))


@pytest.mark.asyncio
async def test_rejected_admission_preserves_feedback_and_cached_source(tmp_path):
    runner, session, _ = context(tmp_path)
    session.agent.memory.steps.append(action())
    before = messages(session)
    session.last_compactable_llm_messages = before
    stage = runner.stage_agent_request_admission(session.session_id,session.mode,None)
    def reject(*_):
        raise RuntimeError('pre-accept failure')
    with pytest.raises(RuntimeError,match='pre-accept failure'):
        await runner.run_sync(session.session_id,'Rejected task',admission_stage=stage,on_run_registered=reject)
    assert messages(session) == before
    assert session.last_compactable_llm_messages == before
    assert not session.behavior_records.get('prefix_boundaries')
    assert not session.run_in_progress


@pytest.mark.parametrize('protocol', ['legacy_text', 'qwen35-native'])
def test_real_multi_tool_error_feedback_survives_omission(tmp_path, protocol):
    from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction
    from smolagents.tools import Tool
    from backend.agent.agent_runner import SmartToolCallingAgent
    from backend.agent.local_llm_model import LocalLLMModel

    class Success(Tool):
        name = 'feedback_ok'
        description = 'Return large successful feedback.'
        inputs = {}
        output_type = 'string'
        def forward(self):
            return LONG

    class Failure(Tool):
        name = 'feedback_error'
        description = 'Return an exact error.'
        inputs = {}
        output_type = 'string'
        def forward(self):
            raise ValueError('Exact execution failure, retry with corrected input')

    runner, session, service = context(tmp_path, protocol)
    agent = SmartToolCallingAgent(tools=[Success(),Failure()], model=LocalLLMModel(service,session=session),tool_protocol=protocol)
    agent._agent_session = session
    session.agent = session.active_agent = agent
    step = ActionStep(step_number=1,timing=Timing(start_time=1.))
    chat = ChatMessage(role='assistant',content='',tool_calls=[ChatMessageToolCall(id=f'call-{i}',type='function',
        function=ChatMessageToolCallFunction(name=name,arguments={})) for i,name in enumerate(['feedback_ok','feedback_error'])])
    runner._omit_historical_tool_feedback(session)  # First admission has no historical tools.
    list(agent.process_tool_calls(chat,step))
    agent.memory.steps.append(step)
    assert LONG in text_content(messages(session))  # Current tool turn stays complete.
    runner._omit_historical_tool_feedback(session)
    body = text_content(messages(session))
    assert 'Exact execution failure, retry with corrected input' in body
    if protocol == 'legacy_text':
        assert LONG in body  # An indivisible block with error feedback stays intact.
    else:
        assert LONG not in body
        assert 'feedback omitted by configuration' in body


@pytest.mark.parametrize('protocol', ['legacy_text', 'qwen35-native'])
@pytest.mark.asyncio
async def test_answer_now_and_profile_preparation_do_not_trigger_omission(tmp_path, protocol):
    runner, session, _ = context(tmp_path, protocol)
    step = action()
    session.agent.memory.steps.append(step)
    before = messages(session)
    response = await runner.generate_answer_now_reply(session.session_id)
    assert response['status'] == 'ok'
    assert LONG in text_content(messages(session))
    assert not session.behavior_records.get('prefix_boundaries')
    assert LONG in text_content(runner._build_cache_first_compact_body_messages(session))
    runner.config.agent.entry_agent_name = 'coordinator_1'
    runner.prepare_session_for_new_turn(session.session_id)
    assert LONG in text_content(messages(session))
    assert not session.behavior_records.get('prefix_boundaries')
    runner._omit_historical_tool_feedback(session)
    runner.config.agent.entry_agent_name = 'basic_agent'
    runner.prepare_session_for_new_turn(session.session_id)
    assert LONG not in text_content(messages(session))
    assert 'feedback omitted by configuration' in text_content(messages(session))
    assert step.observations == LONG


@pytest.mark.parametrize('protocol', ['legacy_text', 'qwen35-native'])
@pytest.mark.asyncio
async def test_accepted_vision_image_and_tool_text_omissions_coexist(tmp_path, protocol):
    from backend.vision.contracts import VisionAutomationBinding, VisionAutomationTransportMetadata
    from backend.vision.history_omission import HistoricalImagePlaceholderTaskStep

    runner, session, service = context(tmp_path, protocol)
    old_image = Image.new('RGB',(3,3),'blue')
    session.agent.memory.steps[0].task_images = [old_image]
    old_step = action()
    session.agent.memory.steps.append(old_step)
    session.last_compactable_llm_messages = messages(session)
    session.last_compactable_memory_steps = tuple(session.agent.memory.steps)
    metadata = VisionAutomationTransportMetadata(history_image_policy='aggregate_prior_image_blocks',
        manual_image_count=0,source_count=1,retained_tick_count=1,
        binding=VisionAutomationBinding(page_generation=1,mode='agent',top_level_context_id=session.session_id,
            automation_task_id='task_01J0123456789ABCDEFGH',capture_cycle_id='cycle_01J0123456789ABCDEFG'))
    stage = runner.stage_agent_request_admission(session.session_id,session.mode,metadata,inbound_image_count=1)
    await runner.run_sync(session.session_id,'New vision task',admission_stage=stage,on_run_registered=lambda *_:None)
    assert isinstance(session.agent.memory.steps[0],HistoricalImagePlaceholderTaskStep)
    assert session.behavior_records['vision_automation_historical_image_omission'][-1]['reason'] == 'vision_automation_historical_image_omission'
    assert session.behavior_records['prefix_boundaries'][-1]['reason'] == 'tool_feedback_historical_text_omission'
    assert LONG not in text_content(service.requests[-1]['messages'])
    assert LONG not in text_content(runner._build_cache_first_compact_body_messages(session))
    assert old_step.observations == LONG
    assert not session.run_in_progress


def test_native_successful_dispatch_blackboard_retains_failed_sibling_feedback(tmp_path):
    from smolagents.models import ChatMessage
    from smolagents.tools import Tool

    class Success(Tool):
        name = 'feedback_ok'
        description = 'Return large successful feedback.'
        inputs = {}
        output_type = 'string'
        def forward(self):
            return LONG

    runner, session, service = context(tmp_path, 'qwen35-native')
    agent = runner._build_profile_agent('manager_agent',session=session)
    agent.tools['feedback_ok'] = Success()
    session.agent = session.active_agent = agent
    session.current_agent_name = 'manager_agent'
    transcript = ('<tool_call><function=dispatching_parallel_agent><parameter=child_id>invalid/child</parameter>'
        '<parameter=task>Inspect source A carefully and report.</parameter></function></tool_call>'
        '<tool_call><function=feedback_ok></function></tool_call>'
        '<tool_call><function=dispatching_parallel_agent><parameter=child_id>good-child</parameter>'
        '<parameter=task>Inspect source B carefully and report.</parameter></function></tool_call>')
    resolution = agent.model._resolve_assistant_control(transcript,None,agent.tools_and_managed_agents)
    assert len(resolution.tool_calls) == 3
    step = ActionStep(step_number=1,timing=Timing(start_time=1.))
    list(agent.process_tool_calls(ChatMessage(role='assistant',content='',tool_calls=list(resolution.tool_calls)),step))
    session.agent.memory.steps.append(step)
    assert [r.status for r in step.canonical_tool_results] == ['failed','completed','completed']
    shared = step.canonical_tool_results[-1].content
    assert 'Status: failed' in shared
    assert 'Status: completed' in shared
    assert 'Done.' in shared  # The real child runner completed against fixture inference.
    runner._omit_historical_tool_feedback(session)
    body = messages(session)
    tools = [m for m in body if m.role == 'tool']
    assert tools[-1].content == shared
    assert LONG not in text_content(body)  # Independent successful ordinary tool still omitted.
    assert 'feedback omitted by configuration' in tools[1].content
