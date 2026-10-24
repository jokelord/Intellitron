"""Native switch schema and call receipts exercise coordinator production paths."""
from copy import deepcopy
from unittest.mock import MagicMock
import pytest
from smolagents.memory import AgentMemory
from backend.agent.agent_runner import AgentRunner, AgentSession
from backend.agent.local_llm_model import LocalLLMModel
from backend.config import get_config
from backend.llm_service import GenerationResult

XML = '<tool_call>\n<function=switch_agent>\n<parameter=target_agent>\nbasic_agent\n</parameter>\n</function>\n</tool_call>'


def make_runner():
    config = deepcopy(get_config())
    config.inference.tool_protocol = "qwen35-native"
    config.model.model_type = "text"
    config.agent.entry_agent_name = "coordinator_1"
    config.agent.enabled_agent_profiles = ["coordinator_1", "coordinator_2", "basic_agent"]
    service = MagicMock()
    service.config = config
    runner = AgentRunner(service, config)
    session = AgentSession(session_id="native-route", agent=MagicMock(), mode="tool_calling", tool_protocol="qwen35-native")
    agent = runner._build_profile_agent("coordinator_1", session=session)
    session.agent = agent
    agent._agent_session = session
    service.chat.return_value = GenerationResult(text=XML)
    return runner, session, service


def test_native_coordinator_request_has_switch_schema_and_receipt():
    runner, session, service = make_runner()
    route = runner._run_coordinator_routing_pass(session, "inspect files", None)
    assert route["kind"] == "switch"
    assert route["target_agent"] == "basic_agent"
    schema = service.chat.call_args.kwargs["tools"][0]["function"]
    assert schema["name"] == "switch_agent"
    assert schema["parameters"]["required"] == ["target_agent"]
    assert schema["parameters"]["additionalProperties"] is False
    assert "<<SWITCH_AGENT>>" not in str(service.chat.call_args.kwargs["messages"])
    history = session.agent.write_memory_to_messages()
    calls = [m for m in history if getattr(m, "tool_calls", None)]
    results = [m for m in history if getattr(m, "role", None) == "tool"]
    assert len(calls) == len(results) == 1
    assert calls[0].tool_calls[0]["id"] == results[0].tool_call_id


def test_native_coordinator2_switch_schema_is_single_target():
    runner, session, service = make_runner()
    agent = runner._build_profile_agent("coordinator_2", session=session)
    switch = [t for t in agent.tools_and_managed_agents if isinstance(t, dict)]
    assert switch[0]["function"]["parameters"]["properties"]["target_agent"]["enum"] == ["basic_agent"]
    assert "<<SWITCH_AGENT>>" not in agent.system_prompt


def test_native_rejects_alias_and_mixed_switch():
    runner, session, service = make_runner()
    model = session.agent.model
    bad = [{"id":"s1", "type":"function", "function":{"name":"switch_agent", "arguments":{"agent_name":"basic_agent"}}}]
    with pytest.raises(ValueError):
        model._resolve_assistant_control("", bad, session.agent.tools_and_managed_agents)


def test_native_mixed_switch_and_ordinary_tool_rejected():
    runner, session, service = make_runner()
    read = {"type":"function", "function":{"name":"read_file", "description":"Read", "parameters":{
        "type":"object", "properties":{"file_path":{"type":"string"}}, "required":["file_path"], "additionalProperties":False}}}
    calls = [
        {"id":"s1", "type":"function", "function":{"name":"switch_agent", "arguments":{"target_agent":"basic_agent"}}},
        {"id":"r1", "type":"function", "function":{"name":"read_file", "arguments":{"file_path":"a"}}},
    ]
    with pytest.raises(ValueError, match="cannot be mixed"):
        session.agent.model._resolve_assistant_control("", calls, [*session.agent.tools_and_managed_agents, read])


@pytest.mark.asyncio
async def test_native_coordinator_stream_schema_and_stable_call_receipt():
    runner, session, service = make_runner()
    service.chat.side_effect = lambda **kwargs: iter([{"content":XML[:24]}, {"content":XML[24:]}])
    events = [e async for e in runner._run_coordinator_routing_stream(session, "inspect files", None)]
    route = [e["route"] for e in events if e.get("kind") == "result"][-1]
    assert route["kind"] == "switch"
    assert service.chat.call_args.kwargs["tools"][0]["function"]["name"] == "switch_agent"
    step = session.agent.memory.steps[-1]
    assert step.tool_calls[0].id == route["control_call"].id
    assert step.canonical_tool_results[0].call_id == step.tool_calls[0].id


def test_native_secondary_step_stream_keeps_call_and_receipt():
    from smolagents.memory import ActionStep
    from smolagents.monitoring import Timing
    from backend.agent.agent_runner import SecondaryHandoffRequest
    runner, session, service = make_runner()
    session.current_agent_name = "coordinator_2"
    agent = runner._build_profile_agent("coordinator_2", session=session)
    session.agent = agent
    agent._agent_session = session
    service.chat.side_effect = lambda **kwargs: iter([{"content": "Let the executor handle this.\n" + XML}])
    step = ActionStep(step_number=1, timing=Timing(start_time=0., end_time=.1))
    events = list(agent._step_stream(step))
    assert any(isinstance(e, SecondaryHandoffRequest) for e in events)
    assert step.tool_calls[0].name == "switch_agent"
    assert step.canonical_tool_results[0].call_id == step.tool_calls[0].id
    assert step.model_output == "Let the executor handle this.\n"
    assert "<tool_call>" not in step.model_output
    captured = session.last_compactable_llm_messages[-1].tool_calls[0]
    assert captured["id"] == step.tool_calls[0].id


def test_native_exhausted_coordinator_switch_has_failed_receipt():
    runner, session, service = make_runner()
    session.turn_switch_count = session.max_agent_switches_per_turn
    route = runner._run_coordinator_routing_pass(session, "inspect files", None)
    assert route["kind"] == "invalid"
    assert "limit" in route["error"]
    assert session.agent.memory.steps[-1].canonical_tool_results[0].status == "failed"


def test_native_exhausted_secondary_switch_does_not_handoff():
    from smolagents.memory import ActionStep
    from smolagents.monitoring import Timing
    from backend.agent.agent_runner import SecondaryHandoffRequest
    runner, session, service = make_runner()
    session.current_agent_name = "coordinator_2"
    session.turn_switch_count = session.max_agent_switches_per_turn
    agent = runner._build_profile_agent("coordinator_2", session=session)
    session.agent = agent
    agent._agent_session = session
    service.chat.side_effect = lambda **kwargs: iter([{"content":XML}])
    step = ActionStep(step_number=1, timing=Timing(start_time=0., end_time=.1))
    events = list(agent._step_stream(step))
    assert not any(isinstance(e, SecondaryHandoffRequest) for e in events)
    assert step.canonical_tool_results[0].status == "failed"

@pytest.mark.parametrize('profile', ['manager_agent', 'basic_agent'])
def test_native_selected_tool_guidance_is_preserved_without_other_tool_leaks(profile):
    from backend.agent.tool_configuration import AgentToolConfigurationRequest
    runner, session, service = make_runner()
    session.current_agent_name = profile
    runner.config.agent.enabled_agent_profiles.append("manager_agent")
    candidate = AgentToolConfigurationRequest(schema_version=1,
        catalog_revision=runner.tool_catalog.catalog_revision,
        manager_agent_tools=['read_file'] if profile == 'manager_agent' else [],
        basic_agent_tools=['read_file'] if profile == 'basic_agent' else [])
    snapshot = runner.tool_catalog.resolve_candidate(candidate)
    agent = runner._build_profile_agent(profile, session=session, tool_snapshot=snapshot)
    assert runner.config.agent_prompts.business_tool_modules['read_file']['prompt'].strip() in agent.system_prompt
    assert 'exec_command:' not in agent.system_prompt
    assert '<<TOOL_CALL>>' not in agent.system_prompt


def test_native_managed_proxy_step_preserves_task_and_additional_args():
    from smolagents.memory import ActionStep
    from smolagents.monitoring import Timing
    from backend.agent.agent_runner import SecondaryHandoffRequest
    from backend.llm_service import GenerationResult
    runner, session, service = make_runner()
    session.current_agent_name = 'coordinator_2'
    agent = runner._build_profile_agent('coordinator_2', session=session)
    session.agent = agent
    agent._agent_session = session
    agent.stream_outputs = False
    task = '  Perform this exact task.\n  '
    args = {'task':task, 'additional_args':{'note':'value', 'flag':True}}
    service.chat.return_value = GenerationResult(text='', tool_calls=[{'id':'proxy-1','type':'function','function':{'name':'basic_agent','arguments':args}}])
    step = ActionStep(step_number=1, timing=Timing(start_time=0.))
    events = list(agent._step_stream(step))
    handoff = next(e for e in events if isinstance(e, SecondaryHandoffRequest))
    assert handoff.task == task
    assert handoff.additional_args == args['additional_args']
    assert step.tool_calls[0].arguments == args
    assert step.canonical_tool_results[0].call_id == 'proxy-1'
    assert step.canonical_tool_results[0].status == 'completed'

@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_native_direct_coordinator_answer_preserves_actual_sent_prefix(stream):
    from backend.llm_service import GenerationResult
    runner, session, service = make_runner()
    session.active_agent = session.agent
    answer = '  Direct answer.\n  '
    reasoning = '  retained thought\n'
    if stream:
        service.chat.side_effect = lambda **kwargs: iter([{'reasoning':reasoning},{'content':answer}])
        events = [e async for e in runner._run_coordinator_routing_stream(session,'hello',None)]
        route = next(e['route'] for e in reversed(events) if e.get('kind') == 'result')
    else:
        service.chat.return_value = GenerationResult(text=answer,reasoning_content=reasoning)
        route = runner._run_coordinator_routing_pass(session,'hello',None)
    sent = service.chat.call_args.kwargs['messages']
    assert route['kind'] == 'final'
    runner._commit_direct_answer_to_shared_memory(session,'hello',route['answer'],None,native_turn=route.get('native_turn'))
    after,_ = session.agent.model._prepare_model_messages(session.agent.write_memory_to_messages())
    assert after[:-1] == sent
    assert after[-1].content == answer
    assert after[-1].reasoning_content == reasoning
    assert len(session.agent.memory.steps) == 2


def test_native_stop_rollback_preserves_completed_empty_prose_control_receipts():
    runner, session, service = make_runner()
    route = runner._run_coordinator_routing_pass(session,'inspect',None)
    before = session.agent.write_memory_to_messages()
    assert any(getattr(m,'tool_call_id',None) for m in before)
    session.rollback_run_state(0,False,None)
    after = session.agent.write_memory_to_messages()
    assert after == before
    assert session.agent.memory.steps[-1].canonical_tool_results[0].call_id == route['control_call'].id


def test_native_stop_keeps_failed_secondary_receipt_seen_by_next_request():
    from smolagents.memory import ActionStep
    from smolagents.monitoring import Timing
    runner, session, service = make_runner()
    session.current_agent_name = 'coordinator_2'
    session.turn_switch_count = session.max_agent_switches_per_turn
    agent = runner._build_profile_agent('coordinator_2',session=session)
    session.agent = session.active_agent = agent
    agent._agent_session = session
    agent.stream_outputs = False
    step = ActionStep(step_number=1,timing=Timing(start_time=0.))
    list(agent._step_stream(step))
    agent.memory.steps.append(step)
    before,_ = agent.model._prepare_model_messages(agent.write_memory_to_messages())
    assert before[-1].role == 'tool'
    assert step.canonical_tool_results[0].status == 'failed'
    session.rollback_run_state(0,False,None)
    after,_ = agent.model._prepare_model_messages(agent.write_memory_to_messages())
    assert after == before
