"""Native requests use the same schema on the real sync/stream/fallback adapters."""
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from smolagents.models import ChatMessage, MessageRole
from backend.agent.local_llm_model import LocalLLMModel
from backend.agent.text_tool_protocol import TextToolProtocolError
from backend.llm_service import GenerationResult

class Read:
    name = "read_file"
    description = "Read one file."
    inputs = {"file_path": {"type": "string", "description": "Path"}}
    output_type = "string"

XML = '<tool_call>\n<function=read_file>\n<parameter=file_path>\n/tmp/a\n</parameter>\n</function>\n</tool_call>'

def model(service=None):
    service = service or MagicMock()
    service.config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1280))
    session = SimpleNamespace(tool_protocol="qwen35-native", session_id="native", current_agent_name="basic_agent")
    return LocalLLMModel(llm_service=service, session=session)


def test_raw_native_calls_are_resolved():
    result = model()._resolve_assistant_control(XML, None, [Read()])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].function.arguments == {"file_path": "/tmp/a"}


def test_native_stream_keeps_tools_and_metadata():
    m = model()
    m.llm_service.chat.return_value = iter([{"content": "Done."}])
    call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": {"file_path": "/tmp/a"}}}
    messages = [SimpleNamespace(role="assistant", content="", tool_calls=[call]),
                SimpleNamespace(role="tool", content="  result\n", name="read_file", tool_call_id="c1")]
    list(m.generate_stream(messages, tools_to_call_from=[Read()], request_id="r1"))
    kwargs = m.llm_service.chat.call_args.kwargs
    assert kwargs["tools"][0]["function"]["name"] == "read_file"
    assert kwargs["messages"][0].tool_calls == [call]
    assert kwargs["messages"][1].role == "tool"
    assert kwargs["messages"][1].tool_call_id == "c1"
    assert kwargs["messages"][1].content == "  result\n"


def test_empty_native_stream_fallback_keeps_schema():
    m = model()
    m.llm_service.chat.side_effect = [iter([]), GenerationResult(text=XML)]
    deltas = list(m.generate_stream([ChatMessage(role=MessageRole.USER, content="read")], tools_to_call_from=[Read()], request_id="r2"))
    requests = [call.kwargs for call in m.llm_service.chat.call_args_list]
    assert requests[0]["tools"] == requests[1]["tools"]
    assert requests[0]["tools"] is not None
    assert all(not (getattr(d, "content", "") or "").startswith("<tool_call>") for d in deltas)


def test_provider_native_switch_is_strict():
    m = model()
    bad = [{"id": "s1", "type": "function", "function": {"name": "switch_agent", "arguments": {"agent_name": "basic_agent"}}}]
    with pytest.raises(TextToolProtocolError):
        m._resolve_assistant_control("", bad, [], allowed_agent_names={"basic_agent"})


def test_native_structured_stream_calls_are_not_lost():
    m = model()
    m.llm_service.chat.return_value = iter([
        {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"file_path":'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '"/tmp/a"}'}}]},
    ])
    deltas = list(m.generate_stream([ChatMessage(role=MessageRole.USER, content="read")], tools_to_call_from=[Read()], request_id="r3"))
    assert any(getattr(d, "tool_calls", None) for d in deltas)
    assert m.llm_service.chat.call_count == 1
    assert m.get_stream_state("r3")["provider_tool_calls"][0]["function"]["arguments"] == '{"file_path":"/tmp/a"}'

@pytest.mark.parametrize('flag', ['answering_now', 'answer_now'])
def test_answer_now_keeps_last_successful_normal_compactable_prefix(flag):
    m = model()
    original = [SimpleNamespace(role='assistant', content='accepted normal answer')]
    m._session.last_compactable_llm_messages = original
    m._session.last_compactable_llm_tools = [{'stable':'schema'}]
    setattr(m._session, flag, True)
    m.llm_service.chat.return_value = iter([{'content':XML}])
    list(m.generate_stream([ChatMessage(role=MessageRole.USER, content='Answer Now')], tools_to_call_from=[Read()]))
    assert m._session.last_compactable_llm_messages is original
    assert m._session.last_compactable_llm_tools == [{'stable':'schema'}]


def test_native_provider_duplicate_call_ids_rejected_before_execution():
    m = model()
    calls = [{'id':'duplicate', 'type':'function', 'function':{'name':'read_file', 'arguments':{'file_path':path}}} for path in ['a','b']]
    with pytest.raises(TextToolProtocolError, match='tool_calls\\[1\\].id'):
        m._resolve_assistant_control('', calls, [Read()])


def test_uninitialized_legacy_model_has_no_native_session():
    m = LocalLLMModel.__new__(LocalLLMModel)
    assert m.native_tool_protocol is False
