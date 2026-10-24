from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock
import json
import pytest
import yaml
from backend.config import get_config
from backend.agent.agent_runner import AgentRunner, AgentSession
ROOT = Path(__file__).resolve().parents[1]
PROFILES = ['coordinator_1', 'coordinator_2', 'manager_agent', 'basic_agent']

def runner_for(dialect='qwen35-native'):
    cfg = deepcopy(get_config())
    cfg.model.model_type = 'text'
    cfg.inference.tool_protocol = dialect
    cfg.inference.capture_raw_tool_calls = True
    cfg.agent.enabled_agent_profiles = PROFILES
    llm = MagicMock(); llm.config = cfg
    return AgentRunner(llm, cfg)

def test_all_prompt_yaml_are_native():
    for path in (ROOT / 'config').rglob('*.yaml'):
        if 'prompt' in path.name:
            text = path.read_text()
            assert '<<TOOL_CALL>>' not in text, path
            assert '<<END_TOOL_CALL>>' not in text, path
            assert '<<SWITCH_AGENT>>' not in text, path
            assert 'native_system_prompt:' not in text, path

@pytest.mark.parametrize('profile', PROFILES)
def test_native_profiles_use_rich_complete_prompt_and_one_protocol(profile):
    runner = runner_for()
    session = AgentSession(session_id='audit', agent=MagicMock(), mode='tool_calling', tool_protocol='qwen35-native')
    agent = runner._build_profile_agent(profile, session=session)
    prompt = agent.system_prompt
    assert '<<TOOL_CALL>>' not in prompt
    assert '<<SWITCH_AGENT>>' not in prompt
    assert prompt.count('Qwen 原生工具调用协议') == 1
    assert 'Takes inputs:' not in prompt
    important = {'coordinator_1': '数值精确计算', 'coordinator_2': 'high effort reasoning',
                 'manager_agent': 'Recommended child final report format', 'basic_agent': 'GUI launcher'}
    assert important[profile] in prompt

@pytest.mark.parametrize('profile', PROFILES)
def test_legacy_profiles_select_frozen_compatibility_templates(profile):
    compatibility = json.loads((ROOT / 'config/compatibility/legacy-agent-prompts.json').read_text())
    runner = runner_for()
    session = AgentSession(session_id='old', agent=MagicMock(), mode='tool_calling', tool_protocol='legacy_text')
    agent = runner._build_profile_agent(profile, session=session)
    assert agent.prompt_templates['system_prompt'] == compatibility['profiles'][profile]['system_prompt']
    assert session.tool_protocol == 'legacy_text'
    if profile == 'coordinator_1':
        turn = runner._build_coordinator_turn_message('inspect file', profile, session=session)
        assert '<<SWITCH_AGENT>>' in turn


def test_native_custom_profile_without_system_template_fails_closed():
    runner = runner_for()
    runner.config.agent.enabled_agent_profiles.append('future_agent')
    runner.config.agent_prompts.extra_profiles['future_agent'] = {'instructions': 'Future profile'}
    session = AgentSession(session_id='audit', agent=MagicMock(), mode='tool_calling', tool_protocol='qwen35-native')
    with pytest.raises(ValueError, match='system_prompt'):
        runner._build_profile_agent('future_agent', session=session)

@pytest.mark.parametrize('path', list((ROOT / 'config').glob('qwen*config-infer.yaml')))
def test_live_qwen_configs_enable_native_template(path):
    config = yaml.safe_load(path.read_text())
    assert config['inference']['tool_protocol'] == 'qwen35-native'
    for provider in ['sglang', 'vllm']:
        assert config[provider]['chat_template'] == 'config/chat-templates/qwen-native-cache-stable.jinja'
    assert config['inference']['capture_raw_tool_calls'] is True
    assert config['vllm']['tool_call_parser'] == 'qwen3_coder_raw_passthrough'
    assert config['vllm']['tool_parser_plugin'] == 'config/vllm-plugins/qwen_raw_passthrough.py'
    assert config['vllm']['enable_auto_tool_choice'] is True

@pytest.mark.parametrize('profile', PROFILES)
def test_production_request_contains_native_prompt_and_exact_api_schema(profile):
    from backend.llm_service import GenerationResult
    runner = runner_for()
    session = AgentSession(session_id='request', agent=MagicMock(), mode='tool_calling', tool_protocol='qwen35-native')
    agent = runner._build_profile_agent(profile, session=session)
    session.agent = agent
    agent._agent_session = session
    runner.llm_service.chat.return_value = GenerationResult(text='Done.')
    agent.model.generate(agent.write_memory_to_messages(), tools_to_call_from=agent.tools_and_managed_agents)
    body = runner.llm_service.chat.call_args.kwargs
    text = str(body['messages'])
    assert '<<TOOL_CALL>>' not in text and '<<SWITCH_AGENT>>' not in text
    assert 'canonical framed tool call' not in text
    assert 'Qwen 原生工具调用协议' in text
    assert body['extra_body']['chat_template_kwargs']['raw_request_tools'] == body['tools']
    names = [s['function']['name'] for s in body['tools']]
    assert 'final_answer' not in names
    if profile == 'manager_agent':
        dispatch = next(t['function'] for t in body['tools'] if t['function']['name'] == 'dispatching_parallel_agent')
        assert set(dispatch['parameters']['properties']) == {'child_id', 'task'}
    elif profile == 'coordinator_2':
        proxy = next(t['function'] for t in body['tools'] if t['function']['name'] == 'basic_agent')
        assert set(proxy['parameters']['properties']) == {'task', 'additional_args'}


def test_selected_guidance_is_preserved_but_definition_is_only_in_request_tools():
    from backend.agent.tool_configuration import AgentToolConfigurationRequest
    runner = runner_for()
    req = AgentToolConfigurationRequest(schema_version=1, catalog_revision=runner.tool_catalog.catalog_revision,
                                       manager_agent_tools=['read_file'], basic_agent_tools=['edit_file'])
    snapshot = runner.tool_catalog.resolve_candidate(req)
    agent = runner._build_profile_agent('basic_agent', tool_snapshot=snapshot)
    assert 'XML 的 search_text 参数正文留空' in agent.system_prompt
    assert '使用 read_file 读取现有文件' not in agent.system_prompt
    assert 'Takes inputs:' not in agent.system_prompt
    assert 'canonical framed tool call' not in agent._tool_prompt_plan.business_tool_protocol


def test_legacy_shared_protocol_is_frozen_in_resolved_plan():
    runner = runner_for()
    session = AgentSession(session_id='old', agent=MagicMock(), mode='tool_calling', tool_protocol='legacy_text')
    agent = runner._build_profile_agent('manager_agent', session=session)
    old = runner.config.agent_prompts.legacy_prompt_compatibility['business_tool_protocol']
    assert agent._tool_prompt_plan.business_tool_protocol == old
    assert 'Qwen 原生工具调用协议' not in agent.system_prompt


def test_coordinator_routing_enum_has_all_and_only_allowed_targets():
    runner = runner_for()
    session = AgentSession(session_id='route', agent=MagicMock(), mode='tool_calling', tool_protocol='qwen35-native')
    session.agent = runner._build_profile_agent('coordinator_1', session=session)
    prompt = runner._build_coordinator_turn_message('dispatch', 'coordinator_1', session=session)
    for target in session.agent._allowed_switch_targets:
        assert '- ' + target in prompt
    session.agent._allowed_switch_targets = frozenset({'basic_agent'})
    prompt = runner._build_coordinator_turn_message('dispatch', 'coordinator_1', session=session)
    assert '- basic_agent' in prompt and '- manager_agent' not in prompt


def test_native_extra_profile_gets_shared_protocol_without_inheriting_basic_instructions():
    runner = runner_for()
    runner.config.agent.enabled_agent_profiles.append('future_agent')
    runner.config.agent_prompts.extra_profiles['future_agent'] = {
        'instructions': 'Future specialist', 'prompt_templates': {'system_prompt': '{{ custom_instructions }} Own complete profile rules.'}}
    agent = runner._build_profile_agent('future_agent')
    assert 'Own complete profile rules.' in agent.system_prompt
    assert agent.system_prompt.count('【Qwen 原生工具调用协议】') == 1
    assert 'GUI launcher' not in agent.system_prompt


def test_native_extra_profile_with_legacy_execution_syntax_is_rejected():
    runner = runner_for()
    runner.config.agent.enabled_agent_profiles.append('future_agent')
    runner.config.agent_prompts.extra_profiles['future_agent'] = {
        'prompt_templates': {'system_prompt': 'Use <<TOOL_CALL>> with JSON'}}
    with pytest.raises(ValueError, match='legacy'):
        runner._build_profile_agent('future_agent')


def test_native_snapshot_stale_shared_protocol_cannot_reintroduce_old_frames():
    from dataclasses import replace
    runner = runner_for()
    old = runner.default_tool_configuration_snapshot
    snapshot = replace(old, basic_agent=replace(old.basic_agent, business_tool_protocol='Use <<TOOL_CALL>> frames'))
    agent = runner._build_profile_agent('basic_agent', tool_snapshot=snapshot)
    assert '<<TOOL_CALL>>' not in agent.system_prompt
    assert agent._tool_prompt_plan.business_tool_protocol == runner.config.agent_prompts.business_tool_protocol

@pytest.mark.parametrize('syntax', ['Calling tools:', '调用工具:', '<tool_call>{"name":"read_file","arguments":{}}</tool_call>', '<tool_call>\n{"name":"read_file"}</tool_call>'])
def test_native_framework_execution_syntax_guard_includes_old_headers_and_wrong_json(syntax):
    runner = runner_for()
    runner.config.agent.enabled_agent_profiles.append('future_agent')
    runner.config.agent_prompts.extra_profiles['future_agent'] = {'prompt_templates': {'system_prompt': syntax}}
    with pytest.raises(ValueError, match='legacy'):
        runner._build_profile_agent('future_agent')


def test_native_user_task_may_quote_old_markers_without_rejection_or_rewriting():
    from backend.llm_service import GenerationResult
    runner = runner_for()
    session = AgentSession(session_id='quote', agent=MagicMock(), mode='tool_calling', tool_protocol='qwen35-native')
    session.agent = runner._build_profile_agent('coordinator_1', session=session)
    task = 'Explain why <<TOOL_CALL>> and Calling tools: are obsolete. Compare <tool_call>{"name":"x"}</tool_call>.'
    runner.llm_service.chat.return_value = GenerationResult(text='Answer.')
    runner._run_coordinator_routing_pass(session, task, None)
    body = runner.llm_service.chat.call_args.kwargs
    assert any(task in str(message.content) for message in body['messages'])


def test_native_selected_module_conflicting_execution_syntax_fails_before_transport():
    from dataclasses import replace
    from backend.agent.tool_configuration import AgentToolConfigurationError
    runner = runner_for()
    old = runner.default_tool_configuration_snapshot
    snapshot = replace(old, basic_agent=replace(old.basic_agent, tool_modules=('Calling tools: obsolete format',)))
    with pytest.raises(AgentToolConfigurationError, match='legacy'):
        runner._build_profile_agent('basic_agent', tool_snapshot=snapshot)
    runner.llm_service.chat.assert_not_called()


def test_native_canonical_api_json_illustration_without_control_wrapper_is_allowed():
    runner = runner_for()
    runner.config.agent.enabled_agent_profiles.append('future_agent')
    runner.config.agent_prompts.extra_profiles['future_agent'] = {'prompt_templates': {'system_prompt': 'API data example: {"role":"assistant","tool_calls":[]}.'}}
    agent = runner._build_profile_agent('future_agent')
    assert 'API data example' in agent.system_prompt


def test_session_wrapper_omitted_search_arguments_use_real_defaults_offline(monkeypatch):
    from backend.agent.agent_runner import _make_session_aware_wrapper
    from backend.tools import web_tools
    observed = []
    def offline_search(query, timeout, config, *, max_results):
        observed.append((query, timeout, max_results))
        return [{'title': str(i), 'url': 'https://example.test/' + str(i), 'description': ''}
                for i in range(max_results or 1)]
    monkeypatch.setattr(web_tools, '_get_config', lambda: {'searxng_timeout': 30})
    monkeypatch.setattr(web_tools, '_search_with_searxng', offline_search)
    wrapped = _make_session_aware_wrapper(web_tools.web_search, 'web_search')
    output = wrapped(query='offline omitted defaults')
    assert observed == [('offline omitted defaults', 30, 80)]
    assert output.count('https://example.test/') == 80
    assert wrapped.inputs['max_results']['default'] == 80
    assert wrapped.inputs['timeout']['default'] == 30
    wrapped(query='explicit values', max_results=2, timeout=11)
    assert observed[-1] == ('explicit values', 11, 2)


@pytest.mark.parametrize('dialect', ['legacy_text', 'qwen35-native'])
@pytest.mark.parametrize('profile', ['coordinator_1', 'coordinator_2', 'manager_agent'])
def test_non_system_fallback_instructions_keep_rich_base_behavior(dialect, profile):
    runner = runner_for()
    session = AgentSession(session_id='fallback', agent=MagicMock(), mode='tool_calling', tool_protocol=dialect)
    agent = runner._build_profile_agent(profile, session=session)
    base = (runner.config.agent_prompts.legacy_prompt_compatibility['profiles']['basic_agent']
            if dialect == 'legacy_text' else runner.config.agent_prompts.basic_agent['prompt_templates'])
    assert agent.prompt_templates['final_answer'] == base['final_answer']


def test_external_minimal_config_loads_bundled_legacy_asset(monkeypatch, tmp_path):
    import backend.config as config_module
    overall = tmp_path / 'config-overall.yaml'
    overall.write_text('{}\n')
    monkeypatch.setattr(config_module, 'PROJECT_ROOT', tmp_path)
    config = config_module.ConfigLoader(config_path=overall).load()
    compatibility = config.agent_prompts.legacy_prompt_compatibility
    assert 'basic_agent' in compatibility['profiles']
    assert '<<TOOL_CALL>>' in compatibility['profiles']['basic_agent']['system_prompt']
    assert not (tmp_path / 'config/compatibility/legacy-agent-prompts.json').exists()


@pytest.mark.parametrize('profile', ['manager_agent', 'basic_agent', 'explorer_agent', 'auditor_agent', 'neutral_extra'])
def test_non_coordinator_profiles_do_not_receive_switch_agent_guidance_or_schema(profile):
    from backend.llm_service import GenerationResult
    runner = runner_for()
    runner.config.agent.enabled_agent_profiles.extend(['explorer_agent', 'auditor_agent', 'neutral_extra'])
    runner.config.agent_prompts.extra_profiles['neutral_extra'] = {
        'instructions': 'Neutral profile rules.',
        'prompt_templates': {'system_prompt': '{{ custom_instructions }} Own neutral rules.'}}
    session = AgentSession(session_id='neutral', agent=MagicMock(), mode='tool_calling', tool_protocol='qwen35-native')
    agent = runner._build_profile_agent(profile, session=session)
    session.agent = agent
    agent._agent_session = session
    runner.llm_service.chat.return_value = GenerationResult(text='Done.')
    agent.model.generate(agent.write_memory_to_messages(), tools_to_call_from=agent.tools_and_managed_agents)
    request = runner.llm_service.chat.call_args.kwargs
    owned_prompt = agent.system_prompt + '\n' + str(agent.instructions)
    assert 'switch_agent' not in owned_prompt
    assert 'managed handoff' not in owned_prompt
    assert 'target_agent' not in owned_prompt
    assert 'switch_agent' not in str(request['messages'])
    assert 'switch_agent' not in [tool['function']['name'] for tool in request['tools'] or []]
    forbidden_switch = '<tool_call>\n<function=switch_agent>\n<parameter=target_agent>\nbasic_agent\n</parameter>\n</function>\n</tool_call>'
    runner.llm_service.chat.return_value = GenerationResult(text=forbidden_switch)
    with pytest.raises(ValueError, match='switch_agent'):
        agent.model.generate(agent.write_memory_to_messages(), tools_to_call_from=agent.tools_and_managed_agents)
    assert agent.memory.steps == []


def test_secondary_coordinator_keeps_dedicated_handoff_instructions_and_schema():
    from backend.agent.qwen_tool_protocol import parse_qwen_protocol
    import re
    runner = runner_for()
    session = AgentSession(session_id='secondary', agent=MagicMock(), mode='tool_calling', tool_protocol='qwen35-native')
    agent = runner._build_profile_agent('coordinator_2', session=session)
    assert '<function=switch_agent>' in agent.system_prompt
    assert 'basic_agent managed schema' in agent.system_prompt
    assert '必要时只能切换到 `basic_agent`' in agent.instructions
    schemas = agent.model._request_tools(agent.tools_and_managed_agents)
    by_name = {tool['function']['name']: tool for tool in schemas}
    assert by_name['switch_agent']['function']['parameters']['properties']['target_agent']['enum'] == ['basic_agent']
    assert set(by_name['basic_agent']['function']['parameters']['properties']) == {'task', 'additional_args'}
    blocks = re.findall(r'(?ms)^<tool_call>\n.*?^</tool_call>$', agent.system_prompt)
    switch = next(block for block in blocks if '<function=switch_agent>' in block)
    parsed = parse_qwen_protocol(switch, tool_schemas=by_name)
    assert parsed.calls[0].arguments == {'target_agent': 'basic_agent'}
