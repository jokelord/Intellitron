"""Adapter and streaming tests for the local framed tool-call protocol."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from smolagents.models import ChatMessage, MessageRole

from backend.agent.local_llm_model import LocalLLMModel
from backend.agent.text_tool_protocol import TextToolProtocolError
from backend.llm_service import GenerationResult


class _ReadFileTool:
    name = "read_file"
    description = "Read one file."
    inputs = {
        "file_path": {
            "type": "string",
            "description": "Absolute file path.",
        }
    }
    output_type = "string"


class _SchemaTool:
    name = "schema_tool"
    description = "Exercise JSON-level schema validation."
    inputs = {
        "required_text": {"type": "string", "description": "Required."},
        "count": {"type": "integer", "description": "Optional.", "default": 1},
        "ratio": {"type": "number", "description": "Nullable.", "nullable": True},
        "enabled": {"type": "boolean", "description": "Optional.", "default": False},
        "items": {"type": "array", "description": "Optional.", "default": []},
        "metadata": {"type": "object", "description": "Optional.", "default": {}},
        "nothing": {"type": "null", "description": "Optional.", "default": None},
        "anything": {"type": "any", "description": "Optional.", "default": None},
    }
    output_type = "string"


class _ZeroArgumentTool:
    name = "zero_argument_tool"
    description = "Accept no arguments."
    inputs = {}
    output_type = "string"


@pytest.fixture
def model():
    return LocalLLMModel(llm_service=MagicMock())


@pytest.fixture
def read_file_tool():
    return _ReadFileTool()


def _native_call(name, arguments, *, call_id="provider_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _text_call(path="/tmp/a"):
    return (
        '<<TOOL_CALL>>\n'
        f'{{"name":"read_file","arguments":{{"file_path":"{path}"}}}}\n'
        '<<END_TOOL_CALL>>'
    )


def test_native_calls_are_authoritative_over_ordinary_text(model, read_file_tool):
    resolved = model._resolve_assistant_control(
        "I will inspect the file.",
        [_native_call("read_file", '{"file_path":"/tmp/a"}')],
        [read_file_tool],
    )

    assert resolved.source == "native"
    assert [call.id for call in resolved.tool_calls] == ["provider_1"]
    assert resolved.tool_calls[0].function.arguments == {"file_path": "/tmp/a"}


def test_native_plus_ordinary_json_demo_is_not_text_parsed(model, read_file_tool):
    resolved = model._resolve_assistant_control(
        'Example JSON: {"name":"other_tool","arguments":{}}',
        [_native_call("read_file", {"file_path": "/tmp/a"})],
        [read_file_tool],
    )

    assert resolved.source == "native"
    assert [call.function.name for call in resolved.tool_calls] == ["read_file"]


def test_native_plus_new_text_control_is_ambiguous_and_atomic(model, read_file_tool):
    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control(
            _text_call("/tmp/text"),
            [_native_call("read_file", {"file_path": "/tmp/native"})],
            [read_file_tool],
        )

    assert exc_info.value.code == "ambiguous_dual_encoding"
    assert "No tool was executed." in str(exc_info.value)


def test_native_plus_whole_fenced_tool_example_keeps_native_authoritative(
    model,
    read_file_tool,
):
    fenced = "```json\n" + _text_call("/tmp/text") + "\n```"

    resolved = model._resolve_assistant_control(
        fenced,
        [_native_call("read_file", {"file_path": "/tmp/native"})],
        [read_file_tool],
    )

    assert resolved.source == "native"
    assert [call.function.arguments for call in resolved.tool_calls] == [
        {"file_path": "/tmp/native"}
    ]


def test_legacy_firewall_runs_before_native_selection(model, read_file_tool):
    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control(
            "Calling tools:\n[]",
            [_native_call("read_file", {"file_path": "/tmp/native"})],
            [read_file_tool],
        )

    assert exc_info.value.code == "unsupported_legacy_protocol"


def test_no_native_calls_use_text_parser_and_generate_opaque_unique_ids(
    model,
    read_file_tool,
):
    text = _text_call("/tmp/a") + "\n\n" + _text_call("/tmp/b")

    first = model._resolve_assistant_control(text, None, [read_file_tool])
    second = model._resolve_assistant_control(text, [], [read_file_tool])

    assert first.source == "text"
    assert [call.function.arguments["file_path"] for call in first.tool_calls] == [
        "/tmp/a",
        "/tmp/b",
    ]
    assert all(
        re.fullmatch(r"call_text_[0-9a-f]{32}_[01]", call.id)
        for call in first.tool_calls
    )
    assert len({call.id for call in first.tool_calls}) == 2
    assert {call.id for call in first.tool_calls}.isdisjoint(
        {call.id for call in second.tool_calls}
    )


def test_unknown_tool_rejects_the_entire_text_batch(model, read_file_tool):
    text = _text_call() + '''

<<TOOL_CALL>>
{
  "name": "missing_tool",
  "arguments": {}
}
<<END_TOOL_CALL>>'''

    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control(text, None, [read_file_tool])

    assert exc_info.value.code == "unknown_tool"
    assert "No tool was executed." in str(exc_info.value)


def test_one_invalid_argument_object_rejects_the_entire_native_batch(
    model,
    read_file_tool,
):
    native = [
        _native_call("read_file", {"file_path": "/tmp/a"}, call_id="provider_1"),
        _native_call("read_file", ["/tmp/b"], call_id="provider_2"),
    ]

    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control("", native, [read_file_tool])

    assert exc_info.value.code == "arguments_must_be_object"
    assert "No tool was executed." in str(exc_info.value)


def test_native_call_requires_arguments_field_even_for_zero_argument_tool(
    model,
    read_file_tool,
):
    native = [
        _native_call("read_file", {"file_path": "/tmp/a"}, call_id="provider_1"),
        {
            "id": "provider_2",
            "type": "function",
            "function": {"name": "zero_argument_tool"},
        },
    ]

    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control(
            "",
            native,
            [read_file_tool, _ZeroArgumentTool()],
        )

    assert exc_info.value.code == "invalid_native_tool_call"
    assert exc_info.value.block_index == 2
    assert "arguments" in str(exc_info.value)
    assert "No tool was executed." in str(exc_info.value)


def test_malformed_native_argument_json_is_not_wrapped_as_raw(model, read_file_tool):
    native = [_native_call("read_file", '{"file_path":')]

    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control("", native, [read_file_tool])

    assert exc_info.value.code == "invalid_native_arguments_json"
    assert "raw" not in getattr(exc_info.value, "arguments", {})


@pytest.mark.parametrize(
    "arguments",
    [
        '{"file_path":"/tmp/a","file_path":"/tmp/b"}',
        '{"file_path":NaN}',
    ],
)
def test_native_argument_json_rejects_duplicates_and_non_finite_constants(
    model,
    read_file_tool,
    arguments,
):
    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control(
            "",
            [_native_call("read_file", arguments)],
            [read_file_tool],
        )

    assert exc_info.value.code == "invalid_native_arguments_json"


def test_hidden_native_final_answer_is_filtered_before_real_tool_validation(model):
    resolved = model._resolve_assistant_control(
        "",
        [_native_call("final_answer", {"answer": "done"})],
        [],
    )

    assert resolved.source == "native"
    assert resolved.tool_calls == ()


def test_schema_validation_honors_defaults_nullable_and_json_types(model):
    valid = {
        "required_text": "ok",
        "ratio": None,
        "count": 2,
        "enabled": True,
        "items": [1, "two"],
        "metadata": {"nested": True},
        "nothing": None,
        "anything": [None, {"x": 1}],
    }

    resolved = model._resolve_assistant_control(
        "",
        [_native_call("schema_tool", valid)],
        [_SchemaTool()],
    )

    assert resolved.tool_calls[0].function.arguments == valid


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"required_text": "ok", "unexpected": 1},
        {"required_text": "ok", "count": True},
        {"required_text": "ok", "ratio": False},
        {"required_text": "ok", "enabled": 1},
        {"required_text": "ok", "items": {}},
        {"required_text": "ok", "metadata": []},
        {"required_text": "ok", "nothing": "null"},
        {"required_text": "ok", "anything": float("nan")},
    ],
)
def test_schema_validation_rejects_missing_unexpected_or_wrong_json_types(
    model,
    arguments,
):
    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control(
            "",
            [_native_call("schema_tool", arguments)],
            [_SchemaTool()],
        )

    assert exc_info.value.code == "argument_schema"


def test_native_switch_is_separate_in_routing_context(model):
    resolved = model._resolve_assistant_control(
        "",
        [_native_call("switch_agent", '"basic_agent"')],
        [],
        allowed_agent_names={"basic_agent", "coordinator_2"},
    )

    assert resolved.source == "native"
    assert resolved.tool_calls == ()
    assert resolved.switch_agent is not None
    assert resolved.switch_agent.target_agent == "basic_agent"


def test_native_switch_mixed_with_tool_call_is_rejected_atomically(model):
    native = [
        _native_call("switch_agent", {"target_agent": "basic_agent"}),
        _native_call("read_file", {"file_path": "/tmp/a"}, call_id="provider_2"),
    ]

    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control(
            "",
            native,
            [_ReadFileTool()],
            allowed_agent_names={"basic_agent"},
        )

    assert exc_info.value.code == "mixed_native_switch"


def test_native_switch_target_must_be_allowed(model):
    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control(
            "",
            [_native_call("switch_agent", {"target_agent": "auditor_agent"})],
            [],
            allowed_agent_names={"basic_agent"},
        )

    assert exc_info.value.code == "unsupported_agent"
    assert "auditor_agent" in str(exc_info.value)
    assert "basic_agent" in str(exc_info.value)


class _ChunkService:
    def __init__(self, chunks):
        self.chunks = chunks

    def chat(self, **kwargs):
        self.kwargs = kwargs
        return iter(self.chunks)


def _visible_text(deltas):
    return "".join(
        delta.content or ""
        for delta in deltas
        if not getattr(delta, "reasoning", False)
    )


def test_stream_suppresses_split_control_but_keeps_exact_raw_state(read_file_tool):
    chunks = [
        {"content": " \n<<TOOL_"},
        {"content": "CALL>>\n{\"name\":\"read_file\",\"arguments\":{\"file_path\":\"/tmp/a\"}}\n"},
        {"content": "<<END_TOOL_CALL>>"},
    ]
    model = LocalLLMModel(llm_service=_ChunkService(chunks))

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="read")],
            tools_to_call_from=[read_file_tool],
            request_id="req-control",
        )
    )

    assert _visible_text(deltas) == ""
    assert model.get_stream_state("req-control") == {
        "content": "".join(chunk["content"] for chunk in chunks),
        "reasoning": "",
    }


def test_stream_releases_chunk_split_whole_fenced_example_and_keeps_raw_state(
    read_file_tool,
):
    chunks = [
        {"content": "```"},
        {"content": "xml\n<<<TOOL_"},
        {
            "content": (
                "CALL>>> read_file\n{\n  \"file_path\": \"/tmp/fenced\"\n}\n"
                "<<END_TOOL_CALL>>\n```"
            )
        },
    ]
    model = LocalLLMModel(llm_service=_ChunkService(chunks))

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="read")],
            tools_to_call_from=[read_file_tool],
            request_id="req-fenced-control",
        )
    )

    assert _visible_text(deltas) == "".join(chunk["content"] for chunk in chunks)
    assert model.get_stream_state("req-fenced-control") == {
        "content": "".join(chunk["content"] for chunk in chunks),
        "reasoning": "",
    }


@pytest.mark.parametrize(
    "chunks",
    [
        [
            {"content": "~~~text\n"},
            {"content": "<<TOOL_CALL>>\n"},
            {"content": '{"name":"read_file","arguments":{}}\n<<END_TOOL_CALL>>'},
        ],
        [
            {"content": "```text\n"},
            {"content": "<<SWITCH_AGENT>> basic_agent\n"},
            {"content": "```"},
        ],
    ],
)
def test_stream_adapter_keeps_fenced_controls_visible_and_natural(chunks, read_file_tool):
    model = LocalLLMModel(llm_service=_ChunkService(chunks))
    raw = "".join(chunk["content"] for chunk in chunks)

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="show example")],
            tools_to_call_from=[read_file_tool],
            request_id="req-fenced-natural",
        )
    )
    resolved = model._resolve_assistant_control(
        raw,
        None,
        [read_file_tool],
        allowed_agent_names={"basic_agent"},
    )

    assert _visible_text(deltas) == raw
    assert resolved.source == "natural"
    assert resolved.tool_calls == ()


@pytest.mark.parametrize("fence", ["```text", "~~~text"])
@pytest.mark.parametrize("separator", ["\r", "\x85", "\u2028", "\u2029"])
def test_adapter_keeps_unicode_inside_fenced_tool_example_natural(
    model, read_file_tool, fence, separator
):
    raw = fence + "\n" + separator + _text_call()

    resolved = model._resolve_assistant_control(raw, None, [read_file_tool])

    assert resolved.source == "natural"
    assert resolved.tool_calls == ()


def test_stream_adapter_hides_split_tool_after_visible_prose_but_keeps_raw_state(
    read_file_tool,
):
    chunks = [
        {"content": "先检查。\n"},
        {"content": "<<TOOL_"},
        {
            "content": (
                'CALL>>\n{"name":"read_file","arguments":{"file_path":"/tmp/a"}}\n'
                "<<END_TOOL_CALL>>"
            )
        },
    ]
    model = LocalLLMModel(llm_service=_ChunkService(chunks))

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="read")],
            tools_to_call_from=[read_file_tool],
            request_id="req-prose-control",
        )
    )

    assert _visible_text(deltas) == "先检查。\n"
    assert model.get_stream_state("req-prose-control")["content"] == "".join(
        chunk["content"] for chunk in chunks
    )


def test_stream_releases_false_prefix_without_changing_bytes():
    chunks = [
        {"content": "\t<<<TOOL_"},
        {"content": "GUIDE>>> is documentation"},
        {"content": "\nnext"},
    ]
    model = LocalLLMModel(llm_service=_ChunkService(chunks))

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="explain")],
            request_id="req-visible",
        )
    )

    expected = "".join(chunk["content"] for chunk in chunks)
    assert _visible_text(deltas) == expected
    assert model.get_stream_state("req-visible")["content"] == expected


def test_reasoning_marker_streams_but_never_controls_visible_content():
    reasoning = "<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>"
    chunks = [
        {"reasoning": reasoning},
        {"content": "Natural final answer."},
    ]
    model = LocalLLMModel(llm_service=_ChunkService(chunks))

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="answer")],
            request_id="req-reasoning",
        )
    )

    assert deltas[0].content == reasoning
    assert deltas[0].reasoning is True
    assert _visible_text(deltas) == "Natural final answer."
    assert model.get_stream_state("req-reasoning") == {
        "content": "Natural final answer.",
        "reasoning": reasoning,
    }


class _EmptyThenFallbackService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return iter([]) if kwargs["stream"] else self.result


def test_empty_stream_fallback_applies_the_same_control_gate(read_file_tool):
    text = _text_call()
    model = LocalLLMModel(
        llm_service=_EmptyThenFallbackService(GenerationResult(text=text))
    )

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="read")],
            tools_to_call_from=[read_file_tool],
            request_id="req-fallback",
        )
    )

    assert _visible_text(deltas) == ""
    assert deltas == []
    assert model.get_stream_state("req-fallback")["content"] == text


@pytest.mark.parametrize(
    "text",
    [
        '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{\"file_path\":\"/tmp/a\"}}',
        "Calling tools:\n[]",
    ],
)
def test_empty_stream_fallback_defers_text_protocol_errors_to_step_boundary(
    read_file_tool,
    text,
):
    model = LocalLLMModel(
        llm_service=_EmptyThenFallbackService(GenerationResult(text=text))
    )

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="read")],
            tools_to_call_from=[read_file_tool],
            request_id="req-invalid-text-fallback",
        )
    )

    assert deltas == []
    assert model.get_stream_state("req-invalid-text-fallback") == {
        "content": text,
        "reasoning": "",
    }


def test_empty_stream_fallback_keeps_provider_native_calls(read_file_tool):
    model = LocalLLMModel(
        llm_service=_EmptyThenFallbackService(
            GenerationResult(
                text="I will inspect it.",
                tool_calls=[
                    _native_call("read_file", '{"file_path":"/tmp/native"}')
                ],
            )
        )
    )

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="read")],
            tools_to_call_from=[read_file_tool],
            request_id="req-native-fallback",
        )
    )

    assert len(deltas) == 1
    assert deltas[0].tool_calls[0].function.name == "read_file"


def test_empty_stream_fallback_preserves_native_plus_text_for_step_arbitration(
    read_file_tool,
):
    native_calls = [
        _native_call("read_file", '{"file_path":"/tmp/native"}')
    ]
    text = _text_call("/tmp/text")
    model = LocalLLMModel(
        llm_service=_EmptyThenFallbackService(
            GenerationResult(
                text=text,
                tool_calls=native_calls,
            )
        )
    )

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="read")],
            tools_to_call_from=[read_file_tool],
            request_id="req-ambiguous-fallback",
        )
    )

    assert deltas == []
    assert model.get_stream_state("req-ambiguous-fallback") == {
        "content": text,
        "reasoning": "",
        "provider_tool_calls": native_calls,
    }


def test_generate_uses_text_adapter_for_new_protocol(read_file_tool):
    service = SimpleNamespace(
        chat=lambda **kwargs: GenerationResult(text=_text_call())
    )
    model = LocalLLMModel(llm_service=service)

    result = model.generate(
        [ChatMessage(role=MessageRole.USER, content="read")],
        tools_to_call_from=[read_file_tool],
    )

    assert result.tool_calls[0].function.name == "read_file"
    assert result.tool_calls[0].function.arguments == {"file_path": "/tmp/a"}
