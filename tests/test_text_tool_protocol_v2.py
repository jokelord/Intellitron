"""Migration contract for the double-angle text tool protocol."""

import pytest

from backend.agent.text_tool_protocol import (
    NaturalText,
    SwitchAgentCommand,
    TextToolCall,
    TextToolProtocolError,
    ToolCallSequence,
    format_switch_agent,
    format_tool_call_sequence,
    parse_text_protocol,
    starts_new_control_opener,
)


def _call(name="glob", arguments='{"pattern":"**/*"}'):
    return f"<<TOOL_CALL>>\n{{\"name\":\"{name}\",\"arguments\":{arguments}}}\n<<END_TOOL_CALL>>"


def test_double_angle_frame_parses_call_object_and_formatter_round_trips():
    expected = ToolCallSequence(
        (TextToolCall(name="glob", arguments={"pattern": "**/*"}),)
    )

    assert parse_text_protocol(_call(), allowed_tool_names={"glob"}) == expected
    rendered = format_tool_call_sequence(expected.calls)
    assert rendered == '''<<TOOL_CALL>>
{
  "name": "glob",
  "arguments": {
    "pattern": "**/*"
  }
}
<<END_TOOL_CALL>>'''
    assert parse_text_protocol(rendered, allowed_tool_names={"glob"}) == expected


def test_repeated_frames_and_shared_frame_objects_form_one_ordered_batch():
    repeated = _call("first", '{"value":1}') + "\n" + _call(
        "second", '{"value":2}'
    )
    shared = '''<<TOOL_CALL>>
{"name":"first","arguments":{"value":1}}
{"name":"second","arguments":{"value":2}}
<<END_TOOL_CALL>>'''

    for content in (repeated, shared):
        result = parse_text_protocol(
            content, allowed_tool_names={"first", "second"}
        )
        assert [(call.name, call.arguments["value"]) for call in result.calls] == [
            ("first", 1),
            ("second", 2),
        ]


@pytest.mark.parametrize("separator", ["", "\r", "\x85", "\u2028", "\u2029"])
def test_shared_frame_requires_protocol_whitespace_between_call_objects(separator):
    body = (
        '{"name":"first","arguments":{}}'
        + separator
        + '{"name":"second","arguments":{}}'
    )

    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(
            f"<<TOOL_CALL>>\n{body}\n<<END_TOOL_CALL>>",
            allowed_tool_names={"first", "second"},
        )

    assert exc_info.value.code == "missing_call_separator"
    assert exc_info.value.block_index == 2


@pytest.mark.parametrize(
    ("body", "code", "item_index"),
    [
        ('[{"name":"glob","arguments":{}}]', "call_must_be_object", 1),
        ('{"name":"glob"}', "invalid_call_schema", 1),
        ('{"arguments":{}}', "invalid_call_schema", 1),
        ('{"name":"bad-name","arguments":{}}', "invalid_tool_name", 1),
        ('{"name":"glob","arguments":[],"extra":1}', "invalid_call_schema", 1),
        ('{"name":"glob","name":"read_file","arguments":{}}', "duplicate_json_key", 1),
        (
            '{"name":"glob","arguments":{}}\n'
            '{"name":"read_file","arguments":[]}',
            "arguments_must_be_object",
            2,
        ),
    ],
)
def test_call_object_schema_fails_closed_with_item_location(body, code, item_index):
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(
            f"<<TOOL_CALL>>\n{body}\n<<END_TOOL_CALL>>",
            allowed_tool_names={"glob", "read_file"},
        )

    assert exc_info.value.code == code
    assert exc_info.value.block_index == item_index
    assert "No tool was executed" in str(exc_info.value)


@pytest.mark.parametrize(
    "old_or_xml",
    [
        "<<<TOOL_CALL>>> glob\n{}\n<<<END_TOOL_CALL>>>",
        "<<<SWITCH_AGENT>>> basic_agent",
        "<think>inspect</think><tool_call><function=glob></function></tool_call>",
        "prose\n<parameter=file_path>/tmp/a</parameter>",
    ],
)
def test_old_triple_and_xml_are_plain_natural_text(old_or_xml):
    assert parse_text_protocol(old_or_xml) == NaturalText(old_or_xml)
    assert starts_new_control_opener(old_or_xml) is False


def test_xml_prefix_does_not_block_later_canonical_physical_line_tool():
    content = "<think>I will inspect.</think>\n" + _call()
    result = parse_text_protocol(content, allowed_tool_names={"glob"})
    assert result.calls == (TextToolCall("glob", {"pattern": "**/*"}),)


def test_prose_before_new_tool_executes_but_prose_before_switch_is_rejected():
    content = "好的，我来实际调用 glob。\n" + _call()
    assert isinstance(
        parse_text_protocol(content, allowed_tool_names={"glob"}), ToolCallSequence
    )

    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(
            "Routing now.\n<<SWITCH_AGENT>> basic_agent",
            allowed_agent_names={"basic_agent"},
        )
    assert exc_info.value.code == "misplaced_control_opener"


def test_switch_remains_exclusive_and_uses_double_angle_marker():
    result = parse_text_protocol(
        " \r\n<<SWITCH_AGENT>> basic_agent\n",
        allowed_agent_names={"basic_agent"},
    )
    assert result == SwitchAgentCommand(
        target_agent="basic_agent",
        raw_payload="<<SWITCH_AGENT>> basic_agent",
    )
    assert format_switch_agent("basic_agent") == "<<SWITCH_AGENT>> basic_agent"


@pytest.mark.parametrize("suffix", ["", "   ", r"\n", r"\n\n", " 任意中文\u2028"])
def test_closer_consumes_arbitrary_same_physical_line_suffix(suffix):
    result = parse_text_protocol(
        _call() + suffix,
        allowed_tool_names={"glob"},
    )
    assert len(result.calls) == 1


def test_fenced_and_inline_new_markers_are_natural_text():
    fenced = "```json\n" + _call() + "\n```"
    inline = "Example: <<TOOL_CALL>> is the opener."
    assert parse_text_protocol(fenced) == NaturalText(fenced)
    assert parse_text_protocol(inline) == NaturalText(inline)


@pytest.mark.parametrize("separator", ["\r", "\x85", "\u2028", "\u2029"])
def test_forbidden_unicode_separator_does_not_create_executable_tool_line(separator):
    content = "prose" + separator + _call()
    with pytest.raises(TextToolProtocolError):
        parse_text_protocol(content, allowed_tool_names={"glob"})


@pytest.mark.parametrize("separator", ["\r", "\x85", "\u2028", "\u2029"])
def test_forbidden_unicode_before_outside_fence_canonical_tool_fails_closed(
    separator,
):
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(separator + _call(), allowed_tool_names={"glob"})

    assert exc_info.value.code == "invalid_protocol_whitespace"


def test_new_frame_requires_next_real_line_for_a_following_frame():
    hidden = _call() + r"\n" + _call("read_file", '{}')
    with pytest.raises(TextToolProtocolError):
        parse_text_protocol(hidden, allowed_tool_names={"glob", "read_file"})


def test_tool_opener_must_begin_at_physical_column_zero():
    content = "  " + _call()
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content, allowed_tool_names={"glob"})
    assert exc_info.value.code == "misplaced_control_opener"
