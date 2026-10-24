"""Native cache-first source retains full messages and tool schema."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
from backend.config import get_config
from backend.compact.types import CompactRequest
from backend.compact.orchestrator import CompactOrchestrator
from backend.agent.agent_runner import AgentRunner
from backend.llm_service import Message
from backend.agent.canonical_messages import build_switch_agent_schema


def test_native_cache_first_plan_keeps_tool_schema():
    config = deepcopy(get_config())
    config.compact.prompt_shape = "cache_first_tail_with_fallback"
    config.compact.prompt_control_enabled = True
    config.compact.reliable_fallback_enabled = False
    source = [Message("system", "stable"), Message("user", "task")]
    req = CompactRequest(mode="agent", body_messages=source, cache_first_body_messages=source)
    req.tools = [build_switch_agent_schema({"basic_agent"})]
    orchestrator = CompactOrchestrator(llm_service=MagicMock(), config=config.compact, prompts=config.agent_prompts.compaction)
    plan = orchestrator._build_prompt_plans(req)[0]
    assert getattr(plan, "tools", None) == req.tools
    assert plan.messages[:2] == source


def test_native_cached_compact_body_preserves_call_metadata():
    config = deepcopy(get_config())
    runner = AgentRunner(MagicMock(), config)
    call = {"id":"c1", "type":"function", "function":{"name":"read_file", "arguments":{"file_path":"x"}}}
    m = Message("assistant", "")
    m.tool_calls = [call]
    session = SimpleNamespace(last_compactable_llm_messages=[Message("system","stable"),m], last_compactable_agent_name="basic_agent")
    body = runner._build_cache_first_compact_body_messages(session)
    assert body[1].tool_calls == [call]
    assert body[1] is not m


async def collect_answer_now(runner, session):
    import json
    return [json.loads(event.removeprefix('data: ').strip())
            async for event in runner.run_answer_now_stream(session.session_id)]


def answer_now_context():
    from backend.agent.agent_runner import AgentSession
    from backend.security.capabilities import build_session_capability
    config = deepcopy(get_config())
    config.inference.tool_protocol = 'qwen35-native'
    config.model.model_type = 'text'
    config.agent.enabled_agent_profiles = ['coordinator_1', 'coordinator_2', 'basic_agent']
    service = MagicMock()
    service.config = config
    runner = AgentRunner(service, config)
    sid = '21212121-2121-4121-8121-212121212121'
    session = AgentSession(session_id=sid, agent=None, mode='tool_calling',
        tool_protocol='qwen35-native', current_agent_name='coordinator_2',
        tool_configuration_snapshot=runner.default_tool_configuration_snapshot,
        capability=build_session_capability(config, session_id=sid, mode='tool_calling'))
    agent = runner._build_profile_agent('coordinator_2', session=session)
    session.agent = session.active_agent = agent
    runner.sessions.put(session)
    return runner, session, service


import pytest

@pytest.mark.asyncio
async def test_native_answer_now_buffers_switch_and_retries_same_prefix_schema():
    from unittest.mock import patch
    runner, session, service = answer_now_context()
    forbidden = '<tool_call>\n<function=switch_agent>\n<parameter=target_agent>\nbasic_agent\n</parameter>\n</function>\n</tool_call>'
    original = [Message('system','last normal context'), Message('assistant','normal answer')]
    session.last_compactable_llm_messages = original
    session.last_compactable_llm_tools = [{'normal':'schema'}]
    service.chat.side_effect = [iter([{'content':'hidden candidate\n'+forbidden}]), iter([{'content':'Immediate answer.'}])]
    with patch.object(runner, '_activate_secondary_handoff', side_effect=AssertionError('must not hand off')):
        events = await collect_answer_now(runner, session)
    assert [e['content'] for e in events if e['type'] == 'content'] == ['Immediate answer.']
    assert not [e for e in events if e['type'] == 'error']
    requests = [c.kwargs for c in service.chat.call_args_list]
    assert len(requests) == 2
    assert requests[0]['tools'] == requests[1]['tools']
    assert requests[1]['messages'][:-1] == requests[0]['messages']
    assert 'Do not call or execute any tool in this Answer Now response' in str(requests[0]['messages'][-1].content)
    assert not any(getattr(step,'tool_calls',None) for step in session.agent.memory.steps)
    assert session.agent.memory.steps[-1].model_output == 'Immediate answer.'
    assert session.last_compactable_llm_messages is original
    assert session.last_compactable_llm_tools == [{'normal':'schema'}]


@pytest.mark.asyncio
async def test_native_answer_now_repeated_calls_fail_without_memory_or_ui_leak():
    runner, session, service = answer_now_context()
    forbidden = '<tool_call>\n<function=switch_agent>\n<parameter=target_agent>\nbasic_agent\n</parameter>\n</function>\n</tool_call>'
    service.chat.side_effect = [iter([{'content':forbidden}]), iter([{'content':forbidden}])]
    before = list(session.agent.memory.steps)
    events = await collect_answer_now(runner, session)
    assert not [e for e in events if e['type'] == 'content']
    assert 'emitted tool calls twice' in [e['error'] for e in events if e['type'] == 'error'][0]
    assert len(service.chat.call_args_list) == 2
    assert session.agent.memory.steps == before


@pytest.mark.asyncio
async def test_native_compact_actual_request_and_diagnostics_share_schema_and_metadata():
    from unittest.mock import patch
    from backend.compact.prompt_diagnostics import make_prompt_snapshot
    from backend.compact.types import CompactEventKind
    from test_compact_orchestrator_cache_first import VALID, cfg, prompts
    captures = []
    class Inference:
        async def chat_stream_async(self, **kwargs):
            captures.append(kwargs)
            yield {'content':VALID}
    call = {'id':'kept-1','type':'function','function':{'name':'read_file','arguments':{'file_path':'a'}}}
    source = [Message('system','stable'), Message('assistant','',tool_calls=[call],reasoning_content=''),
        Message('tool','  exact result\n  ',name='read_file',tool_call_id='kept-1')]
    tools = [{'type':'function','function':{'name':'read_file','parameters':{'type':'object','properties':{'file_path':{'type':'string'}},'required':['file_path']}}}]
    req = CompactRequest(mode='agent',body_messages=source,cache_first_body_messages=source,reference_messages=source,tools=tools)
    conf = cfg()
    conf.reliable_fallback_enabled = False
    applied = []
    orchestrator = CompactOrchestrator(Inference(),conf,prompts(),apply_compaction=lambda *args:applied.append(args))
    with patch('backend.compact.orchestrator.make_prompt_snapshot', wraps=make_prompt_snapshot) as snapshots:
        events = [event async for event in orchestrator.run_compact(req)]
    assert events[-1].kind == CompactEventKind.COMPLETED
    assert len(captures) == len(applied) == 1
    actual = captures[0]
    assert actual['tools'] == tools
    assert actual['messages'][:-1] == source
    assert actual['extra_body']['chat_template_kwargs']['enable_thinking'] is False
    assert all(c.kwargs['tools'] == tools for c in snapshots.call_args_list)
    candidate = make_prompt_snapshot('cache_first_tail',actual['messages'],tools=tools)
    assert snapshots.call_args_list[-1].args[1] == actual['messages']
    assert candidate.content_hash != make_prompt_snapshot('cache_first_tail',actual['messages']).content_hash


@pytest.mark.parametrize('pinned', ['legacy_text','qwen35-native'])
def test_new_turn_profile_reset_keeps_session_dialect_despite_config_change(pinned):
    runner, session, service = answer_now_context()
    runner.config.agent.entry_agent_name = 'coordinator_2'
    session.tool_protocol = pinned
    runner.config.inference.tool_protocol = 'qwen35-native' if pinned == 'legacy_text' else 'legacy_text'
    refreshed = runner.prepare_session_for_new_turn(session.session_id)
    assert refreshed is session
    assert session.tool_protocol == pinned
    assert session.agent.model.native_tool_protocol == (pinned == 'qwen35-native')
    prompt = session.agent.system_prompt
    assert ('<<SWITCH_AGENT>>' in prompt) == (pinned == 'legacy_text')


@pytest.mark.parametrize('protocol', ['legacy_text','qwen35-native'])
def test_real_new_context_initialization_pins_selected_protocol(tmp_path, protocol):
    runner, existing, service = answer_now_context()
    runner.config.inference.tool_protocol = protocol
    runner.config.agent.entry_agent_name = 'basic_agent'
    runner.config.agent.session_temp_base_dir = str(tmp_path)
    stage = runner.stage_agent_request_admission(None,'tool_calling',None,inbound_image_count=0)
    session, scope = runner._begin_staged_agent_request(stage,'tool_calling',lambda admission,scopes:None)
    try:
        assert session is not existing
        assert session.tool_protocol == protocol
        assert session.agent.model.native_tool_protocol == (protocol == 'qwen35-native')
        assert ('<<TOOL_CALL>>' in session.agent.system_prompt) == (protocol == 'legacy_text')
    finally:
        session.finish_run(scope)
