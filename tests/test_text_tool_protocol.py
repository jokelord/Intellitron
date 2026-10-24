"""Behavioral tests for the framed local text tool-call protocol."""

from __future__ import annotations

import json

import pytest

from backend.agent.text_tool_protocol import (
    NaturalText,
    StreamControlGate,
    SwitchAgentCommand,
    TextToolCall,
    TextToolProtocolError,
    ToolCallSequence,
    format_switch_agent,
    format_tool_call_sequence,
    has_malformed_control_attempt,
    parse_text_protocol,
    reject_legacy_opener,
    starts_new_control_opener,
    starts_textual_tool_call_attempt,
)


def test_multiple_blocks_preserve_order_and_complex_symbols():
    content = r"""<<TOOL_CALL>>
{
  "name": "dispatching_parallel_agent",
  "arguments": {
  "child_id": "reader_a",
  "task": "Analyze :' ; [ ] { } < > \\ | and \"quoted\" JSON."
}
}
<<END_TOOL_CALL>>

<<TOOL_CALL>>
{
  "name": "read_file",
  "arguments": {
  "file_path": "/tmp/example.json"
}
}
<<END_TOOL_CALL>>"""

    result = parse_text_protocol(
        content,
        allowed_tool_names={"dispatching_parallel_agent", "read_file"},
    )

    assert isinstance(result, ToolCallSequence)
    assert [call.name for call in result.calls] == [
        "dispatching_parallel_agent",
        "read_file",
    ]
    assert result.calls[0].arguments == {
        "child_id": "reader_a",
        "task": "Analyze :' ; [ ] { } < > \\ | and \"quoted\" JSON.",
    }
    assert result.calls[1].arguments == {"file_path": "/tmp/example.json"}


@pytest.mark.parametrize("literal_separator", [r"\n", r"\n\n"])
def test_literal_newline_suffix_consumes_same_physical_closer_line(literal_separator):
    first = "<<TOOL_CALL>>\n{\"name\":\"first_tool\",\"arguments\":{\n  \"value\": 1\n}}\n<<END_TOOL_CALL>>"
    second = "<<TOOL_CALL>>\n{\"name\":\"second_tool\",\"arguments\":{\n  \"value\": 2\n}}\n<<END_TOOL_CALL>>"

    with pytest.raises(TextToolProtocolError):
        parse_text_protocol(
            first + literal_separator + second,
            allowed_tool_names={"first_tool", "second_tool"},
        )


def test_literal_newline_separators_do_not_create_physical_tool_lines():
    blocks = [
        f'<<TOOL_CALL>> tool_{index}\n{{\n  "index": {index}\n}}\n<<END_TOOL_CALL>>'
        for index in range(4)
    ]
    content = blocks[0] + r"\n" + blocks[1] + r"\n\n" + blocks[2] + r"\n" + blocks[3]

    with pytest.raises(TextToolProtocolError):
        parse_text_protocol(
            content,
            allowed_tool_names={f"tool_{index}" for index in range(4)},
        )


def test_literal_separator_after_final_closer_is_ignored_on_same_physical_line():
    content = '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>' + r"\n"

    result = parse_text_protocol(content, allowed_tool_names={"read_file"})

    assert result == ToolCallSequence(
        (TextToolCall(name="read_file", arguments={}),)
    )


def test_literal_separator_does_not_make_a_nonstandalone_final_closer_valid():
    content = '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}\n  <<END_TOOL_CALL>>' + r"\n"

    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content, allowed_tool_names={"read_file"})

    assert exc_info.value.code == "missing_closer"


def test_formatter_uses_two_space_utf8_json_and_round_trips():
    calls = (
        TextToolCall(
            name="dispatching_parallel_agent",
            arguments={
                "child_id": "reader_a",
                "task": "分析嵌套参数",
                "options": {"tags": ["数学", "shell"], "active": True},
            },
        ),
        TextToolCall(name="read_file", arguments={"file_path": "/tmp/a"}),
    )
    expected = """<<TOOL_CALL>>
{
  "name": "dispatching_parallel_agent",
  "arguments": {
    "child_id": "reader_a",
    "task": "分析嵌套参数",
    "options": {
      "tags": [
        "数学",
        "shell"
      ],
      "active": true
    }
  }
}

{
  "name": "read_file",
  "arguments": {
    "file_path": "/tmp/a"
  }
}
<<END_TOOL_CALL>>"""

    rendered = format_tool_call_sequence(calls)

    assert rendered == expected
    assert r"<<END_TOOL_CALL>>\n<<TOOL_CALL>>" not in rendered
    assert r"<<END_TOOL_CALL>>\n\n<<TOOL_CALL>>" not in rendered
    assert parse_text_protocol(
        rendered,
        allowed_tool_names={"dispatching_parallel_agent", "read_file"},
    ) == ToolCallSequence(calls)


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_parser_accepts_lf_and_crlf_with_outer_whitespace(line_ending):
    lines = [
        "<<TOOL_CALL>>",
        '{"name":"read_file","arguments":{"file_path":"/tmp/a"}}',
        "<<END_TOOL_CALL>>",
    ]
    content = f" \t{line_ending}" + line_ending.join(lines) + f"{line_ending}\t "

    result = parse_text_protocol(content, allowed_tool_names={"read_file"})

    assert result == ToolCallSequence(
        (TextToolCall(name="read_file", arguments={"file_path": "/tmp/a"}),)
    )


@pytest.mark.parametrize("separator", ["\r", "\x85", "\u2028", "\u2029"])
def test_forbidden_leading_unicode_separator_never_enters_control_mode(separator):
    control = "<<SWITCH_AGENT>> basic_agent"
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(
            separator + control,
            allowed_tool_names={"read_file"},
            allowed_agent_names={"basic_agent"},
        )

    assert exc_info.value.code == "invalid_protocol_whitespace"
    assert "LF or CRLF" in str(exc_info.value)
    assert "No tool was executed" in str(exc_info.value)


@pytest.mark.parametrize("separator", ["\r", "\x85", "\u2028", "\u2029"])
def test_forbidden_trailing_unicode_separator_never_completes_control_mode(separator):
    control = "<<SWITCH_AGENT>> basic_agent"
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(
            control + separator,
            allowed_tool_names={"read_file"},
            allowed_agent_names={"basic_agent"},
        )

    assert exc_info.value.code == "invalid_protocol_whitespace"
    assert "No tool was executed" in str(exc_info.value)


@pytest.mark.parametrize("separator", ["\r", "\x85", "\u2028", "\u2029"])
def test_forbidden_unicode_separator_at_fence_body_edge_is_inert_text(separator):
    content = (
        "```text\n"
        + separator
        + "<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>"
        + separator
        + "\n```"
    )

    assert parse_text_protocol(
        content, allowed_tool_names={"read_file"}
    ) == NaturalText(content)
@pytest.mark.parametrize("separator", ["\r", "\x85", "\u2028", "\u2029"])
def test_forbidden_unicode_separator_between_blocks_rejects_whole_batch(separator):
    first = "<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>"
    second = "<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>"

    with pytest.raises(TextToolProtocolError):
        parse_text_protocol(
            first + "\n" + separator + " \n" + second,
            allowed_tool_names={"read_file"},
        )


def test_switch_agent_is_the_only_control_line():
    content = " \n<<SWITCH_AGENT>> basic_agent\n\t"

    result = parse_text_protocol(
        content,
        allowed_agent_names={"coordinator_2", "basic_agent"},
    )

    assert result == SwitchAgentCommand(
        target_agent="basic_agent",
        raw_payload="<<SWITCH_AGENT>> basic_agent",
    )
    assert format_switch_agent("basic_agent") == "<<SWITCH_AGENT>> basic_agent"


def _fenced_test_block(name, arguments):
    return f'<<TOOL_CALL>>\n{{"name":"{name}","arguments":{arguments}}}\n<<END_TOOL_CALL>>'


@pytest.mark.parametrize("info", ["", "xml", "json", "text", "json  "])
def test_whole_response_outer_fence_keeps_strict_tool_block_inert(info):
    content = f"  \n```{info}\n" + _fenced_test_block(
        "read_file", '{"file_path":"/tmp/example.json"}'
    ) + "\n```\n  "

    assert parse_text_protocol(content, allowed_tool_names={"read_file"}) == NaturalText(
        content
    )


@pytest.mark.parametrize("separator", ["\n", "\n\n", r"\n", r"\n\n"])
def test_whole_response_outer_fence_keeps_multiple_blocks_inert(
    separator,
):
    first = _fenced_test_block("first_tool", '{\n  "value": 1\n}')
    second = _fenced_test_block("second_tool", '{\n  "value": 2\n}')
    content = f"```json\n{first}{separator}{second}\n```"

    assert parse_text_protocol(
        content,
        allowed_tool_names={"first_tool", "second_tool"},
    ) == NaturalText(content)


@pytest.mark.parametrize(
    "content",
    [
        "```text\n<<SWITCH_AGENT>> basic_agent\n```",
        "```text\nCalling tools:\n[]\n```",
    ],
)
def test_fenced_control_examples_are_inert_natural_text(content):
    assert parse_text_protocol(
        content,
        allowed_tool_names={"read_file"},
        allowed_agent_names={"basic_agent"},
    ) == NaturalText(content)


def test_unmatched_fence_after_executable_frame_is_rejected():
    content = (
        "<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n"
        "<<END_TOOL_CALL>>\n```"
    )
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content, allowed_tool_names={"read_file"})
    assert exc_info.value.code == "unexpected_content"
    assert "No tool was executed" in str(exc_info.value)


@pytest.mark.parametrize(
    "content",
    [
        "Prose before.\n```text\n<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}\n"
        "<<END_TOOL_CALL>>\n```",
        "```text\nExample only:\n<<TOOL_CALL>> read_file\n{}\n"
        "<<END_TOOL_CALL>>\n```",
        "```text\n<<TOOL_CALL>> read_file\n{}}\n<<END_TOOL_CALL>>\n```\n"
        "Prose after.",
        "```python\n<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>\n```",
        "```text\n<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}\n```",
        "```text\n<<TOOL_CALL>> read_file\n{}}\n<<END_TOOL_CALL>>",
        "```text\n<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":```json\n{}\n```\n"
        "<<END_TOOL_CALL>>\n```",
        "```python\nprint('normal example')\n```",
    ],
)
def test_fenced_demo_or_non_whole_response_remains_exact_natural_text(content):
    assert parse_text_protocol(content) == NaturalText(content)


@pytest.mark.parametrize(
    "content",
    [
        "~~~text\n" + _fenced_test_block("read_file", "{}") + "\n~~~",
        "~~~python title=demo\n" + _fenced_test_block("read_file", "{}"),
        (
            "~~~~text\n```\n"
            + _fenced_test_block("read_file", "{}")
            + "\n~~~~"
        ),
        (
            "```text\n~~~ not a closing fence\n"
            + _fenced_test_block("read_file", "{}")
            + "\n```"
        ),
        (
            "````text\n```\n"
            + _fenced_test_block("read_file", "{}")
            + "\n````"
        ),
    ],
)
def test_markdown_fence_state_keeps_all_tool_examples_natural(content):
    assert parse_text_protocol(content, allowed_tool_names={"read_file"}) == NaturalText(
        content
    )


@pytest.mark.parametrize(
    "content",
    [
        "```text\n<<SWITCH_AGENT>> basic_agent\n```",
        "~~~text route-example\n<<SWITCH_AGENT>> basic_agent\n~~~",
    ],
)
def test_whole_response_fenced_switch_is_natural_text(content):
    assert parse_text_protocol(
        content, allowed_agent_names={"basic_agent"}
    ) == NaturalText(content)


@pytest.mark.parametrize("fence", ["```text", "~~~text"])
@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("separator", ["\r", "\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("position", ["before", "after"])
def test_fence_body_unicode_boundaries_never_reactivate_control(
    fence, closed, separator, position
):
    frame = _fenced_test_block("read_file", "{}")
    body = separator + frame if position == "before" else frame + separator
    content = fence + "\n" + body + ("\n" + fence[:3] if closed else "")

    assert parse_text_protocol(content, allowed_tool_names={"read_file"}) == NaturalText(
        content
    )


@pytest.mark.parametrize("fence", ["```text", "~~~text"])
@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        "Calling tools:\n[]",
        "<tool_call><function=read_file></function></tool_call>",
        '<<TOOL_CALL>>\n{"name":"read_file","arguments":{}}\n<<END_TOOL_CALL>>',
        "<<SWITCH_AGENT>> basic_agent",
    ],
)
def test_all_fenced_control_examples_are_absent_from_attempt_predicates(
    fence, closed, body
):
    content = fence + "\n" + body + ("\n" + fence[:3] if closed else "")

    assert starts_new_control_opener(content) is False
    assert starts_textual_tool_call_attempt(content) is False
    assert has_malformed_control_attempt(content) is False


def test_closed_fenced_example_then_real_physical_line_tool_executes():
    example = "```text\n<<TOOL_CALL>>\n{}\n<<END_TOOL_CALL>>\n```"
    real_call = _fenced_test_block("read_file", '{"file_path":"/tmp/a"}')

    result = parse_text_protocol(
        example + "\n" + real_call,
        allowed_tool_names={"read_file"},
    )

    assert result.calls == (
        TextToolCall(name="read_file", arguments={"file_path": "/tmp/a"}),
    )


def test_marker_in_prose_or_json_demo_is_natural_text():
    prose = (
        "The literal marker <<TOOL_CALL>> is required in this explanation.\n"
        '{"demo": "<<SWITCH_AGENT>> basic_agent"}'
    )

    assert parse_text_protocol(prose) == NaturalText(prose)


@pytest.mark.parametrize(
    "content",
    [
        "**Routing to the basic agent**\n\n<<SWITCH_AGENT>> basic_agent",
        (
            "Explanation first.\n"
            "<<TOOL_CALL>> read_file {\"file_path\":\"/tmp/a\"} "
            "<<END_TOOL_CALL>>"
        ),
    ],
)
def test_non_tool_control_or_malformed_opener_after_prose_is_rejected(content):
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(
            content,
            allowed_tool_names={"read_file"},
            allowed_agent_names={"basic_agent"},
        )

    assert exc_info.value.code == "misplaced_control_opener"
    assert "entire response" in str(exc_info.value)
    assert "No tool was executed" in str(exc_info.value)


def test_chinese_prose_before_glob_tool_is_accepted():
    content = """好的，我来实际演示一次工具调用。下面我会调用 glob 工具：

<<TOOL_CALL>>
{
  "name": "glob",
  "arguments": {
  "pattern": "**/*.py"
}
}
<<END_TOOL_CALL>>"""

    result = parse_text_protocol(content, allowed_tool_names={"glob"})

    assert result == ToolCallSequence(
        (TextToolCall(name="glob", arguments={"pattern": "**/*.py"}),)
    )


def test_prose_before_multiple_tools_preserves_batch_order():
    content = """I need two independent reads.
<<TOOL_CALL>>
{
  "name": "read_file",
  "arguments": {
  "file_path": "/tmp/a"
}
}
<<END_TOOL_CALL>>
<<TOOL_CALL>>
{
  "name": "read_file",
  "arguments": {
  "file_path": "/tmp/b"
}
}
<<END_TOOL_CALL>>"""

    result = parse_text_protocol(content, allowed_tool_names={"read_file"})

    assert [call.arguments["file_path"] for call in result.calls] == [
        "/tmp/a",
        "/tmp/b",
    ]


@pytest.mark.parametrize(
    "closer_suffix",
    ["", "   ", r"\n", r"\n\n", " arbitrary 中文 🧪 \x85\u2028\u2029"],
)
def test_closer_consumes_arbitrary_same_physical_line_suffix(closer_suffix):
    content = _fenced_test_block(
        "read_file", '{\n  "file_path": "/tmp/a"\n}'
    ) + closer_suffix

    result = parse_text_protocol(content, allowed_tool_names={"read_file"})

    assert result == ToolCallSequence(
        (TextToolCall(name="read_file", arguments={"file_path": "/tmp/a"}),)
    )


def test_next_tool_must_begin_on_next_real_physical_line():
    first = "<<TOOL_CALL>> read_file\n{}}\n<<END_TOOL_CALL>>"
    hidden_same_line_opener = r"\n<<TOOL_CALL>> read_file"
    remainder = "\n{}\n<<END_TOOL_CALL>>"

    with pytest.raises(TextToolProtocolError):
        parse_text_protocol(
            first + hidden_same_line_opener + remainder,
            allowed_tool_names={"read_file"},
        )


def test_final_closer_does_not_swallow_prose_on_next_physical_line():
    content = _fenced_test_block("read_file", "{}").replace(
        "<<END_TOOL_CALL>>", "<<END_TOOL_CALL>> ignored same-line suffix"
    ) + "\nordinary prose"

    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content, allowed_tool_names={"read_file"})

    assert exc_info.value.code == "unexpected_content"


@pytest.mark.parametrize("separator", ["\r", "\x85", "\u2028", "\u2029"])
def test_unicode_line_boundary_before_standalone_control_is_fail_closed(separator):
    content = (
        "I will call the tool now."
        + separator
        + "<<TOOL_CALL>> read_file\n"
        + '{\n  "file_path": "/tmp/a"\n}\n'
        + "<<END_TOOL_CALL>>"
    )

    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content, allowed_tool_names={"read_file"})

    assert exc_info.value.code == "misplaced_control_opener"
    assert "No tool was executed" in str(exc_info.value)


@pytest.mark.parametrize(
    "content",
    [
        (
            "<tool_call>\n"
            "<function=read_file>\n"
            "<parameter=file_path>/tmp/a</parameter>\n"
            "</function>\n"
            "</tool_call>"
        ),
        "<function=read_file>\n<parameter=file_path>/tmp/a</parameter>\n</function>",
        "<parameter=file_path>/tmp/a</parameter>",
        (
            "```xml\n"
            "<tool_call><function=read_file></function></tool_call>\n"
            "```"
        ),
    ],
)
def test_xml_tool_like_text_is_natural_text(content):
    assert parse_text_protocol(content, allowed_tool_names={"read_file"}) == NaturalText(content)


@pytest.mark.parametrize(
    "content",
    [
        "The literal `<tool_call>` tag is an invalid format in this explanation.",
        (
            "```xml\n"
            "Example only; never execute this:\n"
            "<tool_call><function=read_file></function></tool_call>\n"
            "```"
        ),
        (
            "```xml\n<tool_call><function=read_file></function></tool_call>\n```\n"
            "The fenced text above is only documentation."
        ),
    ],
)
def test_inline_or_documented_xml_examples_remain_natural_text(content):
    assert parse_text_protocol(content) == NaturalText(content)


def test_closer_literal_inside_json_string_does_not_close_block():
    content = """<<TOOL_CALL>>
{
  "name": "write_file",
  "arguments": {
  "file_path": "/tmp/a.txt",
  "content": "literal <<END_TOOL_CALL>> remains data"
}
}
<<END_TOOL_CALL>>"""

    result = parse_text_protocol(content, allowed_tool_names={"write_file"})

    assert isinstance(result, ToolCallSequence)
    assert result.calls[0].arguments["content"] == (
        "literal <<END_TOOL_CALL>> remains data"
    )


def test_strict_json_accepts_an_escaped_newline_in_a_nested_value():
    content = r'''<<TOOL_CALL>>
{
  "name": "write_file",
  "arguments": {
  "file_path": "/tmp/a.txt",
  "metadata": {
    "summary": "line one\nline two"
  }
}
}
<<END_TOOL_CALL>>'''

    result = parse_text_protocol(content, allowed_tool_names={"write_file"})

    assert result.calls[0].arguments["metadata"] == {
        "summary": "line one\nline two"
    }


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ('["/tmp/a"]', "arguments_must_be_object"),
        ('"/tmp/a"', "arguments_must_be_object"),
        ("42", "arguments_must_be_object"),
        ("true", "arguments_must_be_object"),
        ("false", "arguments_must_be_object"),
        ("null", "arguments_must_be_object"),
        ('{"value": NaN}', "non_finite_json"),
        ('{"value": Infinity}', "non_finite_json"),
        ('{"value": -Infinity}', "non_finite_json"),
        ('{"file_path": "/tmp/a",}', "invalid_json"),
        ("{'file_path': '/tmp/a'}", "invalid_json"),
        ('{"file_path": "/tmp/a" // comment\n}', "invalid_json"),
        ('{"file_path": "/tmp/a"} {"extra": true}', "invalid_json"),
        ('{"content": "line one\nline two"}', "invalid_json"),
    ],
)
def test_strict_json_rejects_non_json_or_non_object_arguments(body, code):
    content = (
        '<<TOOL_CALL>>\n{"name":"read_file","arguments":'
        f"{body}" + "}\n"
        "<<END_TOOL_CALL>>"
    )

    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content, allowed_tool_names={"read_file"})

    assert exc_info.value.code == code
    assert exc_info.value.block_index == 1
    assert "No tool was executed." in str(exc_info.value)


@pytest.mark.parametrize(
    ("body", "duplicate_key"),
    [
        ('{"file_path": "/tmp/a", "file_path": "/tmp/b"}', "file_path"),
        ('{"outer": {"value": 1, "value": 2}}', "value"),
    ],
)
def test_strict_json_rejects_duplicate_keys_at_every_depth(body, duplicate_key):
    content = (
        '<<TOOL_CALL>>\n{"name":"read_file","arguments":'
        f"{body}" + "}\n"
        "<<END_TOOL_CALL>>"
    )

    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content, allowed_tool_names={"read_file"})

    assert exc_info.value.code == "duplicate_json_key"
    assert exc_info.value.block_index == 1
    assert exc_info.value.duplicate_key == duplicate_key
    assert exc_info.value.line is None
    assert exc_info.value.column is None


def test_invalid_json_reports_decoder_line_and_column():
    content = '<<TOOL_CALL>>\n{\n  "name": "read_file",\n  "arguments": {\n    "file_path": "/tmp/a",\n  }\n}\n<<END_TOOL_CALL>>'

    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content, allowed_tool_names={"read_file"})

    assert exc_info.value.code == "invalid_json"
    assert exc_info.value.block_index == 1
    assert exc_info.value.line == 5
    assert exc_info.value.column == 3


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (
            '<<TOOL_CALL>>\n{"name":"read_file","arguments":{"file_path":}}\n'
            "<<END_TOOL_CALL>>",
            "invalid_json",
        ),
        (
            '<<TOOL_CALL>>\n{"name":"read_file","arguments":{"value":1,"value":2}}\n'
            "<<END_TOOL_CALL>>",
            "duplicate_json_key",
        ),
        ("Calling tools:\n[]", "unsupported_legacy_protocol"),
        ('<<TOOL_CALL>>\n{"name":"read_file","arguments":{}}', "missing_closer"),
    ],
)
def test_representative_protocol_errors_include_minimal_new_format_correction(
    content,
    code,
):
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content, allowed_tool_names={"read_file"})

    message = str(exc_info.value)
    assert exc_info.value.code == code
    assert "<<TOOL_CALL>>" in message
    assert "<<END_TOOL_CALL>>" in message
    assert "strict JSON" in message
    assert "two-space indentation" in message
    assert "No tool was executed." in message


def test_invalid_route_identifier_reports_attempted_and_allowed_targets():
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(
            "<<SWITCH_AGENT>> basic-agnt",
            allowed_agent_names={"basic_agent", "coordinator_2"},
        )

    message = str(exc_info.value)
    assert exc_info.value.code == "invalid_switch_command"
    assert "basic-agnt" in message
    assert "basic_agent" in message
    assert "coordinator_2" in message


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (
            '<<TOOL_CALL>>\n{"name":"read_file","arguments":{}}',
            "missing_closer",
        ),
        (
            '<<TOOL_CALL>> read-file\n{"name":"read_file","arguments":{}}\n<<END_TOOL_CALL>>',
            "invalid_opener",
        ),
        (
            "<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>\nextra prose",
            "unexpected_content",
        ),
        (
            "<<SWITCH_AGENT>> basic_agent\nextra prose",
            "invalid_switch_command",
        ),
        (
            "<<SWITCH_AGENT>> basic_agent\n"
            "<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>",
            "invalid_switch_command",
        ),
    ],
)
def test_control_mode_rejects_incomplete_or_mixed_responses(content, code):
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(
            content,
            allowed_tool_names={"read_file"},
            allowed_agent_names={"basic_agent"},
        )

    assert exc_info.value.code == code
    assert "No tool was executed." in str(exc_info.value)


def test_second_invalid_block_rejects_the_whole_sequence():
    content = _fenced_test_block("read_file", '{"file_path":"/tmp/a"}') + (
        '\n<<TOOL_CALL>>\n{"name":"read_file","arguments":{"file_path":"/tmp/b",}}\n'
        '<<END_TOOL_CALL>>'
    )

    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content, allowed_tool_names={"read_file"})

    assert exc_info.value.code == "invalid_json"
    assert exc_info.value.block_index == 2
    assert exc_info.value.line is not None


def test_unknown_tool_and_unsupported_agent_are_rejected_without_guessing():
    tool_content = """<<TOOL_CALL>>
{
  "name": "read_flie",
  "arguments": {}
}
<<END_TOOL_CALL>>"""
    with pytest.raises(TextToolProtocolError) as tool_error:
        parse_text_protocol(tool_content, allowed_tool_names={"read_file"})
    assert tool_error.value.code == "unknown_tool"
    assert tool_error.value.block_index == 1

    with pytest.raises(TextToolProtocolError) as route_error:
        parse_text_protocol(
            "<<SWITCH_AGENT>> basic-agnt",
            allowed_agent_names={"basic_agent"},
        )
    assert route_error.value.code == "invalid_switch_command"

    with pytest.raises(TextToolProtocolError) as target_error:
        parse_text_protocol(
            "<<SWITCH_AGENT>> auditor_agent",
            allowed_agent_names={"basic_agent"},
        )
    assert target_error.value.code == "unsupported_agent"
    assert "auditor_agent" in str(target_error.value)
    assert "basic_agent" in str(target_error.value)


@pytest.mark.parametrize("opener", ["Calling tools:", "调用工具:"])
def test_exact_legacy_opener_is_permanently_rejected(opener):
    content = f"  \n{opener}\n[{{}}]"

    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content)

    assert exc_info.value.code == "unsupported_legacy_protocol"
    assert "No tool was executed." in str(exc_info.value)

    with pytest.raises(TextToolProtocolError) as direct_error:
        reject_legacy_opener(content)
    assert direct_error.value.code == "unsupported_legacy_protocol"


def test_legacy_openers_in_prose_or_fence_are_not_rejected():
    prose = "Example only: Calling tools:\n[]"
    fence = "`" * 3
    fenced = f"{fence}text\nExample only:\n调用工具:\n[]\n{fence}"

    reject_legacy_opener(prose)
    reject_legacy_opener(fenced)
    assert parse_text_protocol(prose) == NaturalText(prose)
    assert parse_text_protocol(fenced) == NaturalText(fenced)


@pytest.mark.parametrize(
    "content",
    [
        "<<TOOL_CALL>>",
        " \n<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>",
        "<<SWITCH_AGENT>> basic_agent",
    ],
)
def test_starts_new_control_opener_checks_first_non_whitespace_position(content):
    assert starts_new_control_opener(content) is True


def test_starts_new_control_opener_does_not_scan_prose():
    assert starts_new_control_opener("Example: <<TOOL_CALL>> read_file") is False


@pytest.mark.parametrize(
    "chunks",
    [
        (" \n<<TOOL_", "CALL>>\n", '{"name":"read_file","arguments":{}}\n', "<<END_TOOL_CALL>>") ,
        ("<<SWITCH_", "AGENT>> basic_agent"),
        (" \nCalling ", "tools:\n", "[]"),
        ("调用", "工具:\n", "[]"),
    ],
)
def test_stream_gate_suppresses_exact_new_and_legacy_prefixes(chunks):
    gate = StreamControlGate()

    emitted = "".join(gate.push(chunk) for chunk in chunks) + gate.finish()

    assert emitted == ""
    assert gate.suppresses_visible_content is True


def test_stream_gate_preserves_exact_bytes_when_possible_prefix_diverges():
    gate = StreamControlGate()

    emitted = [
        gate.push(" \t<<<TOOL_"),
        gate.push("GUIDE>>> is documentation"),
        gate.push("\nnext line"),
        gate.finish(),
    ]

    assert "".join(emitted) == (
        " \t<<<TOOL_GUIDE>>> is documentation\nnext line"
    )
    assert gate.suppresses_visible_content is False


def test_stream_gate_preserves_exact_bytes_for_a_legacy_false_prefix():
    gate = StreamControlGate()

    emitted = [
        gate.push(" \nCalling tool"),
        gate.push("set documentation"),
        gate.finish(),
    ]

    assert "".join(emitted) == " \nCalling toolset documentation"
    assert gate.suppresses_visible_content is False


def test_stream_gate_releases_incomplete_prefix_exactly_on_finish():
    gate = StreamControlGate()
    assert gate.push("\n<<SWITCH_") == ""

    assert gate.finish() == "\n<<SWITCH_"
    assert gate.suppresses_visible_content is False


def test_stream_gate_suppresses_split_standalone_tool_after_visible_prose():
    gate = StreamControlGate()
    chunks = (
        "Explanation before.\n",
        "<<TOOL_",
        "CALL>>\n{\"name\":\"read_file\",\"arguments\":",
        "{}}\n<<END_TOOL_CALL>>",
    )

    emitted = "".join(gate.push(chunk) for chunk in chunks) + gate.finish()

    assert emitted == chunks[0]
    assert gate.suppresses_visible_content is True


@pytest.mark.parametrize("separator", ["", "\r", "\x85", "\u2028", "\u2029"])
def test_stream_gate_does_not_activate_inline_or_forbidden_line_boundary(separator):
    gate = StreamControlGate()
    content = "Explanation before." + separator + _fenced_test_block(
        "read_file", "{}"
    )

    emitted = gate.push(content) + gate.finish()

    assert emitted == content
    assert gate.suppresses_visible_content is False


@pytest.mark.parametrize(
    "chunks",
    [
        ("```text\n", "<<SWITCH_AGENT>> basic_agent\n", "```"),
        ("~~~text route-example\n", "<<SWITCH_AGENT>> basic_agent\n", "~~~"),
    ],
)
def test_stream_gate_releases_fenced_switch_examples(chunks):
    gate = StreamControlGate()

    per_chunk = [gate.push(chunk) for chunk in chunks]
    emitted = "".join(per_chunk) + gate.finish()

    assert per_chunk == list(chunks)
    assert emitted == "".join(chunks)
    assert gate.suppresses_visible_content is False


@pytest.mark.parametrize(
    "chunks",
    [
        (
            "```",
            "xml\n<<<TOOL_",
            "CALL>>> read_file\n{}\n",
            "<<END_TOOL_CALL>>\n```",
        ),
        (
            " \n`",
            "``json\n<<TOOL_CALL>>\n{\"name\":\"first\",\"arguments\":{}}\n<<END_TOOL_CALL>>",
            r"\n",
            "<<TOOL_CALL>>\n{\"name\":\"second\",\"arguments\":{}}\n<<END_TOOL_CALL>>\n```\n",
        ),
    ],
)
def test_stream_gate_releases_whole_fenced_tool_example_across_chunks(chunks):
    gate = StreamControlGate()

    emitted = "".join(gate.push(chunk) for chunk in chunks) + gate.finish()

    assert emitted == "".join(chunks)
    assert gate.suppresses_visible_content is False


@pytest.mark.parametrize(
    "chunks",
    [
        ("```json\n", '{"demo": ', '"<<TOOL_CALL>>"}\n```'),
        (
            "```text\nExample only:\n",
            "<<TOOL_CALL>> read_file\n{}\n",
            "<<END_TOOL_CALL>>\n```",
        ),
        (
            "```text\n<<TOOL_CALL>> read_file\n{}\n",
            "<<END_TOOL_CALL>>\n```\ntrailing prose",
        ),
        ("```py", "thon\nprint('example')\n```"),
        ("``", " incomplete"),
    ],
)
def test_stream_gate_preserves_exact_fenced_example_or_divergent_bytes(chunks):
    gate = StreamControlGate()

    emitted = "".join(gate.push(chunk) for chunk in chunks) + gate.finish()

    assert emitted == "".join(chunks)
    assert gate.suppresses_visible_content is False


def test_formatter_rejects_non_finite_values_and_invalid_identifiers():
    with pytest.raises(TextToolProtocolError) as non_finite_error:
        format_tool_call_sequence(
            (TextToolCall(name="tool_name", arguments={"value": float("nan")}),)
        )
    assert non_finite_error.value.code == "non_finite_json"

    with pytest.raises(TextToolProtocolError) as tool_name_error:
        format_tool_call_sequence((TextToolCall(name="bad-name", arguments={}),))
    assert tool_name_error.value.code == "invalid_tool_name"

    with pytest.raises(TextToolProtocolError) as agent_name_error:
        format_switch_agent("bad-name")
    assert agent_name_error.value.code == "invalid_agent_name"


def test_formatter_output_contains_strict_json_objects_only():
    rendered = format_tool_call_sequence(
        (TextToolCall(name="read_file", arguments={"file_path": "/tmp/a"}),)
    )
    body = rendered.split("\n", 1)[1].rsplit("\n", 1)[0]

    assert json.loads(body) == {
        "name": "read_file",
        "arguments": {"file_path": "/tmp/a"},
    }


def test_complete_json_at_eof_is_missing_closer_without_implicit_repair():
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(
            '<<TOOL_CALL>>\n{"name":"read_file","arguments":{"file_path":"/tmp/a"}}'
        )

    assert exc_info.value.code == "missing_closer"
