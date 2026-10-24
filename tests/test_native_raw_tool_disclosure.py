"""Raw-call disclosure remains display-only across native transport paths."""
import json
from copy import deepcopy

import pytest

from backend.agent.agent_runner import RawToolCallDebug
from backend.llm_service import GenerationResult
from test_native_compact_committed_tail import CALL, context, ordinary_step


PROVIDER = [{'id': 'provider-exact-id', 'type': 'function',
             'function': {'name': 'literal_read', 'arguments': ' { "file_path" : "/tmp/a" } '}}]


@pytest.mark.parametrize('source', ['xml', 'provider'])
@pytest.mark.parametrize('transport', ['stream', 'fallback', 'sync'])
def test_native_raw_disclosure_preserves_actual_transport_and_stays_out_of_memory(tmp_path, source, transport):
    runner, session, agent, service = context(tmp_path)
    raw = CALL if source == 'xml' else ''
    provider = deepcopy(PROVIDER) if source == 'provider' else None

    def chat(**kwargs):
        if not kwargs['stream']:
            return GenerationResult(text=raw, tool_calls=provider)
        if transport == 'fallback':
            return iter([{'content': ''}])
        if source == 'provider':
            arguments = provider[0]['function']['arguments']
            return iter([{'reasoning': 'I will read the file.'},
                {'tool_calls': [{'index': 0, 'id': provider[0]['id'], 'type': 'function',
                    'function': {'name': 'literal_read', 'arguments': arguments[:12]}}]},
                {'tool_calls': [{'index': 0, 'function': {'arguments': arguments[12:]}}]}])
        return iter([{'reasoning': 'I will read the file.'}, {'content': raw}])

    service.chat = chat
    agent.stream_outputs = transport != 'sync'
    step, events = ordinary_step(agent)
    disclosures = [event.raw_tool_call for event in events if isinstance(event, RawToolCallDebug)]
    assert len(disclosures) == 1
    assert next(event for event in events if isinstance(event, RawToolCallDebug)).source == (
        "model_text" if source == "xml" else "provider_payload")
    if source == 'xml':
        assert disclosures == [CALL]
    else:
        assert json.loads(disclosures[0]) == PROVIDER
        assert '<tool_call>' not in disclosures[0]
    assert step.tool_calls[0].name == 'literal_read'
    messages = agent.write_memory_to_messages(computer_use_history_stage=None, consume_tool_images=False)
    assert CALL not in repr(messages)
    assert 'raw_tool_call' not in repr(session.last_compactable_llm_messages)

@pytest.mark.parametrize('transport', ['stream', 'fallback', 'sync'])
def test_rejected_native_xml_still_has_exact_disclosure_without_execution_or_raw_memory(tmp_path, monkeypatch, transport):
    from smolagents.memory import ActionStep
    from smolagents.monitoring import Timing
    from smolagents.utils import AgentParsingError
    from backend.agent.qwen_tool_protocol import parse_qwen_protocol, QwenToolProtocolError
    from test_native_compact_committed_tail import LiteralRead

    raw = '  I will read.\n<tool_call><function=literal_read><parameter=file_path> /tmp/a </parameter></function>\n'
    runner, session, agent, service = context(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('rejected call must never execute')
    monkeypatch.setattr(LiteralRead, 'forward', forbidden)
    def chat(**kwargs):
        if not kwargs['stream']:
            return GenerationResult(text=raw)
        return iter([{'content': '' if transport == 'fallback' else raw}])
    service.chat = chat
    agent.stream_outputs = transport != 'sync'
    schemas = {schema['function']['name']:schema for schema in agent.model._request_tools(agent.tools_and_managed_agents)}
    with pytest.raises(QwenToolProtocolError) as expected:
        parse_qwen_protocol(raw, tool_schemas=schemas)
    step = ActionStep(step_number=1, timing=Timing(start_time=1.))
    events = []
    with pytest.raises(AgentParsingError) as actual:
        for event in agent._step_stream(step):
            events.append(event)
    assert str(actual.value) == str(expected.value)
    assert not hasattr(actual.value.__cause__, "raw_call_debug")
    assert raw not in repr(actual.value.__cause__.__dict__)
    debug = [event for event in events if isinstance(event, RawToolCallDebug)]
    assert [(event.raw_tool_call, event.source) for event in debug] == [(raw, 'model_text')]
    assert not step.tool_calls
    step.error = actual.value
    agent.memory.steps.append(step)
    messages = agent.write_memory_to_messages(computer_use_history_stage=None, consume_tool_images=False)
    assert raw not in repr(messages)
    assert not getattr(session, 'last_compactable_llm_messages', None)

@pytest.mark.parametrize('transport', ['stream', 'fallback', 'sync'])
@pytest.mark.parametrize('rejection', ['dual_encoding', 'mixed_switch'])
def test_rejected_semantic_native_xml_keeps_exact_disclosure_and_feedback(tmp_path, monkeypatch, transport, rejection):
    from smolagents.memory import ActionStep
    from smolagents.monitoring import Timing
    from smolagents.utils import AgentParsingError
    from backend.agent.text_tool_protocol import TextToolProtocolError
    from test_native_compact_committed_tail import SWITCH, LiteralRead

    runner, session, agent, service = context(tmp_path, profile='coordinator_2')
    raw = '  ' + CALL + ('\n' + SWITCH if rejection == 'mixed_switch' else '') + '\n '
    provider = deepcopy(PROVIDER) if rejection == 'dual_encoding' else None
    monkeypatch.setattr(LiteralRead, 'forward', lambda *a, **k: pytest.fail('rejected batch executed'))
    def chat(**kwargs):
        if not kwargs['stream']:
            return GenerationResult(text=raw, tool_calls=provider)
        return iter([{'content': '' if transport == 'fallback' else raw,
                      'tool_calls': None if transport == 'fallback' else provider}])
    service.chat = chat
    agent.stream_outputs = transport != 'sync'
    with pytest.raises(TextToolProtocolError) as expected:
        agent.model._resolve_assistant_control(raw, provider, agent.tools_and_managed_agents)
    step = ActionStep(step_number=1, timing=Timing(start_time=1.))
    events = []
    with pytest.raises(AgentParsingError) as actual:
        for event in agent._step_stream(step):
            events.append(event)
    assert str(actual.value) == str(expected.value)
    assert [(e.raw_tool_call,e.source) for e in events if isinstance(e,RawToolCallDebug)] == [(raw,'model_text')]
    assert not hasattr(actual.value.__cause__,'raw_call_debug')
    assert not step.tool_calls
    step.error = actual.value
    agent.memory.steps.append(step)
    assert raw not in repr(agent.write_memory_to_messages(computer_use_history_stage=None,consume_tool_images=False))
    assert not getattr(session,'last_compactable_llm_messages',None)
