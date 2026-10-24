"""Canonical call metadata must survive the real transport boundaries."""
from copy import deepcopy
from types import SimpleNamespace

from backend.llm_service import Message, prepare_multimodal_messages
from backend.api.chat import ChatMessage as APIMessage
from backend.compact.orchestrator import CompactOrchestrator
from backend.compact.prompt_diagnostics import canonicalize_messages
from backend.config import InferenceConfig

CALL = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": {"file_path": "/tmp/a"}}}


def decorated_message():
    m = Message("assistant", "")
    m.tool_calls = [deepcopy(CALL)]
    m.reasoning_content = "inspect first"
    return m


def test_multimodal_copy_preserves_call_metadata():
    m = decorated_message()
    result = prepare_multimodal_messages([m], max_image_dimension=None)[0]
    assert getattr(result, "tool_calls", None) == [CALL]
    assert getattr(result, "reasoning_content", None) == "inspect first"
    assert result is not m


def test_api_accepts_tool_metadata():
    m = APIMessage(role="tool", content="  code\n", name="read_file", tool_call_id="c1")
    assert getattr(m, "name", None) == "read_file"
    assert getattr(m, "tool_call_id", None) == "c1"


def test_compact_normalization_keeps_assistant_call_and_result():
    result = CompactOrchestrator._normalize_prompt_messages([
        {"role": "assistant", "content": "", "tool_calls": [CALL]},
        {"role": "tool", "content": "  code\n", "name": "read_file", "tool_call_id": "c1"},
    ])
    assert getattr(result[0], "tool_calls", None) == [CALL]
    assert getattr(result[1], "tool_call_id", None) == "c1"
    assert result[1].content == "  code\n"


def test_prompt_diagnostics_compare_call_arguments():
    a, b = decorated_message(), decorated_message()
    b.tool_calls[0]["function"]["arguments"]["file_path"] = "/tmp/b"
    assert canonicalize_messages([a]) != canonicalize_messages([b])


def test_missing_protocol_defaults_legacy():
    assert getattr(InferenceConfig(), "tool_protocol", None) == "legacy_text"
