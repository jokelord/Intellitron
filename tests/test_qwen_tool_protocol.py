"""Native XML controls are lossless, schema-driven, and fail closed."""
import importlib.util
import pytest
from backend.agent.text_tool_protocol import NaturalText, TextToolCall, TextToolProtocolError, ToolCallSequence


def protocol():
    assert importlib.util.find_spec('backend.agent.qwen_tool_protocol') is not None, 'native protocol implementation missing'
    from backend.agent import qwen_tool_protocol
    return qwen_tool_protocol


SCHEMAS = {'run': {'type': 'object', 'properties': {
    'code': {'type': 'string'}, 'flag': {'type': 'boolean'},
    'count': {'type': 'integer'}, 'data': {'type': 'object'},
    'items': {'type': 'array'}, 'nil': {'type': 'null'}},
    'required': ['code'], 'additionalProperties': False}}


def xml(parameters, name='run'):
    return '<tool_call><function=' + name + '>' + ''.join(
        '<parameter=' + key + '>' + value + '</parameter>' for key, value in parameters
    ) + '</function></tool_call>'


def test_native_multiple_calls_roundtrip():
    p = protocol()
    raw = xml([('code', 'print(1)')]) + '\n' + xml([('code', 'print(2)')])
    result = p.parse_qwen_protocol(raw, tool_schemas=SCHEMAS)
    assert result == ToolCallSequence((TextToolCall('run', {'code': 'print(1)'}), TextToolCall('run', {'code': 'print(2)'})))
    assert p.parse_qwen_protocol(p.format_qwen_tool_calls(result.calls, tool_schemas=SCHEMAS), tool_schemas=SCHEMAS) == result


def test_native_schema_types_and_whitespace():
    p = protocol()
    code = ' \n' + '    print("true")\n' * 1000 + '\t '
    result = p.parse_qwen_protocol(xml([('code', code), ('flag', 'true'), ('count', '2'), ('data', '{"x":false}'), ('items', '[1,null]'), ('nil', 'null')]), tool_schemas=SCHEMAS)
    assert result.calls[0].arguments == {'code': code, 'flag': True, 'count': 2, 'data': {'x': False}, 'items': [1, None], 'nil': None}
    assert p.parse_qwen_protocol(xml([('code', ' true ')]), tool_schemas=SCHEMAS).calls[0].arguments['code'] == ' true '


@pytest.mark.parametrize('bad', [
    xml([('code', 'a')], 'unknown'), xml([('code', 'a'), ('code', 'b')]),
    xml([('bad', 'x')]), xml([]), xml([('code', 'x'), ('flag', 'True')]),
    xml([('code', 'x'), ('count', 'true')]), xml([('code', 'x'), ('data', '{"x":1,"x":2}')]),
    xml([('code', 'x'), ('items', '[NaN]')]), xml([('code', 'x'), ('items', '[1e999]')]),
    xml([('code', '<tool_call>')]), xml([('code', '</parameter>')]),
    '<tool_call>{"name":"run","arguments":{"code":"x"}}</tool_call>',
    xml([('code', 'x')]) + ' trailing prose', '<tool_call><function=run><parameter=code>x',
])
def test_native_rejects_invalid_batch(bad):
    p = protocol()
    with pytest.raises(TextToolProtocolError):
        p.parse_qwen_protocol(xml([('code', 'valid first')]) + '\n' + bad, tool_schemas=SCHEMAS)


@pytest.mark.parametrize('schema', [{'type': ['string', 'null']}, {'anyOf': [{'type': 'string'}, {'type': 'integer'}]}])
def test_native_rejects_ambiguous_union(schema):
    p = protocol()
    with pytest.raises(TextToolProtocolError):
        p.parse_qwen_protocol(xml([('code', 'null')]), tool_schemas={'run': {'type': 'object', 'properties': {'code': schema}}})


@pytest.mark.parametrize('text', ['```xml\n' + xml([('code', 'x')]) + '\n```', '~~~text\n' + xml([('code', 'x')]) + '\n~~~', '> ' + xml([('code', 'x')]), '`' + xml([('code', 'x')]) + '`', 'Mention <tool_call> as a token.', '<<TOOL_CALL>>\n{"name":"run","arguments":{}}\n<<END_TOOL_CALL>>'])
def test_native_fenced_examples_are_inert(text):
    p = protocol()
    assert p.parse_qwen_protocol(text, tool_schemas=SCHEMAS) == NaturalText(text)
    gate = p.QwenStreamControlGate()
    assert ''.join(gate.push(char) for char in text) + gate.finish() == text
    assert not gate.suppresses_visible_content


def test_native_fragmented_eof():
    p = protocol()
    gate = p.QwenStreamControlGate(tool_schemas=SCHEMAS)
    assert ''.join(gate.push(char) for char in xml([('code', 'x')])) + gate.finish() == ''
    assert gate.suppresses_visible_content
    gate = p.QwenStreamControlGate()
    assert gate.push('<tool_') == ''
    with pytest.raises(TextToolProtocolError):
        gate.finish()


def test_native_prose_prefix_stream_and_error_location():
    p = protocol()
    gate = p.QwenStreamControlGate(tool_schemas=SCHEMAS)
    text = 'I will read it.\n' + xml([('code', 'x')])
    assert ''.join(gate.push(char) for char in text) + gate.finish() == 'I will read it.\n'
    with pytest.raises(TextToolProtocolError) as caught:
        p.parse_qwen_protocol('\n' + xml([('code', 'x'), ('flag', 'FALSE')]), tool_schemas=SCHEMAS)
    assert caught.value.line == 2
    assert caught.value.column is not None
    assert 'flag' in str(caught.value)
    assert '<<TOOL_CALL>>' not in str(caught.value)


@pytest.mark.parametrize('raw', ['<tool_call extra>', '<tool_callx>', '<tool_call'])
def test_native_malformed_opener_never_leaks(raw):
    p = protocol()
    gate = p.QwenStreamControlGate()
    assert ''.join(gate.push(char) for char in raw) == ''
    with pytest.raises(TextToolProtocolError):
        gate.finish()


def test_native_stream_partial_noncontrol_line_does_not_hide_next_control():
    p = protocol()
    gate = p.QwenStreamControlGate(tool_schemas=SCHEMAS)
    text = '<\n' + xml([('code', 'x')])
    assert ''.join(gate.push(char) for char in text) + gate.finish() == '<\n'


def test_native_raw_entities_and_markup_are_lossless():
    p = protocol()
    value = ' &amp; &lt; <functional> <parameters> "null"\r\n '
    assert p.parse_qwen_protocol(xml([('code', value)]), tool_schemas=SCHEMAS).calls[0].arguments['code'] == value


def test_native_provider_validation_paths_and_constraints():
    p = protocol()
    schema = {'run': {'type': 'object', 'properties': {'count': {'type': 'integer', 'minimum': 1}}, 'required': ['count']}}
    with pytest.raises(TextToolProtocolError) as caught:
        p.validate_qwen_tool_calls([TextToolCall('run', {'count': 0})], tool_schemas=schema)
    assert caught.value.line is None and caught.value.column is None
    assert 'calls[0].function.arguments.count' in str(caught.value)
    with pytest.raises(TextToolProtocolError):
        p.validate_qwen_tool_calls([TextToolCall('run', {'count': 1.0})], tool_schemas=schema)


def test_native_full_openai_schema_and_codec_preserve_raw_string():
    p = protocol()
    schemas = {'run': {'type': 'function', 'function': {'name': 'run', 'parameters': SCHEMAS['run']}}}
    calls = [TextToolCall('run', {'code': ' true ', 'flag': False, 'nil': None})]
    assert p.format_qwen_tool_calls(calls, tool_schemas=schemas) == xml([('code', '\n true \n'), ('flag', '\nfalse\n'), ('nil', '\nnull\n')])


@pytest.mark.parametrize('value', ['/tmp/demo.txt', '  /tmp/demo.txt  ', '\n  code\n\n', '\r\n\tcode\r\n'])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_native_template_parameter_framing_preserves_value(value, newline):
    p = protocol()
    # Hand-written equivalent of the serving template's parameter envelope.
    rendered = '<tool_call>\n<function=run>\n<parameter=code>' + newline + value + newline + '</parameter>\n</function>\n</tool_call>'
    assert p.parse_qwen_protocol(rendered, tool_schemas=SCHEMAS).calls[0].arguments['code'] == value
    calls = [TextToolCall('run', {'code': value})]
    assert p.parse_qwen_protocol(p.format_qwen_tool_calls(calls, tool_schemas=SCHEMAS), tool_schemas=SCHEMAS).calls == tuple(calls)


def test_native_control_prose_preserves_inert_examples_and_exact_prefix():
    p = protocol()
    prefix = 'Example:\n```xml\n' + xml([('code', 'inert')]) + '\n```\nNext:\n  '
    assert hasattr(p, 'native_control_prose'), 'native prose projection missing'
    assert p.native_control_prose(prefix + xml([('code', 'active')])) == prefix
    inert = '`<tool_call>` is a literal.'
    assert p.native_control_prose(inert) == inert


@pytest.mark.parametrize('indent', ['    ', '\t', '   \t'])
def test_native_markdown_indented_code_is_inert(indent):
    p = protocol()
    literal = indent + xml([('code', 'x')])
    assert p.parse_qwen_protocol(literal, tool_schemas=SCHEMAS) == NaturalText(literal)
    gate = p.QwenStreamControlGate()
    assert ''.join(gate.push(char) for char in literal) + gate.finish() == literal
    assert not gate.suppresses_visible_content


@pytest.mark.parametrize('marker', ['`', '``', '````'])
def test_native_completed_multiline_code_span_is_inert(marker):
    p = protocol()
    text = 'Literal ' + marker + 'example\n' + xml([('code', 'x')]) + '\n' + marker + ' ends here.'
    assert p.parse_qwen_protocol(text, tool_schemas=SCHEMAS) == NaturalText(text)
    gate = p.QwenStreamControlGate(tool_schemas=SCHEMAS)
    assert ''.join(gate.push(char) for char in text) + gate.finish() == text
    assert not gate.suppresses_visible_content


def test_native_completed_code_span_before_actual_control_keeps_prefix():
    p = protocol()
    prefix = '`literal\n<tool_call>\n`\n'
    text = prefix + xml([('code', 'x')])
    assert p.parse_qwen_protocol(text, tool_schemas=SCHEMAS).calls == (TextToolCall('run', {'code': 'x'}),)
    gate = p.QwenStreamControlGate(tool_schemas=SCHEMAS)
    assert ''.join(gate.push(char) for char in text) + gate.finish() == prefix
    assert gate.suppresses_visible_content


def test_native_nonclosing_fence_run_cannot_reactivate_example():
    p = protocol()
    text = '```xml\n```not-a-closer\n' + xml([('code', 'x')]) + '\n```'
    assert p.parse_qwen_protocol(text, tool_schemas=SCHEMAS) == NaturalText(text)
    gate = p.QwenStreamControlGate(tool_schemas=SCHEMAS)
    assert ''.join(gate.push(char) for char in text) + gate.finish() == text


def test_native_completed_inline_span_releases_before_eof_and_resumes_control_gate():
    p = protocol()
    gate = p.QwenStreamControlGate(tool_schemas=SCHEMAS)
    assert gate.push('Use `read_') == 'Use '
    assert gate.push('file` then explain.\n') == '`read_file` then explain.\n'
    assert gate.push('<tool_') == ''
    assert gate.push('call><function=run><parameter=code>x</parameter></function></tool_call>') == ''
    assert gate.finish() == ''
    assert gate.suppresses_visible_content


def test_native_raw_error_feedback_string_includes_location():
    p = protocol()
    with pytest.raises(TextToolProtocolError) as caught:
        p.parse_qwen_protocol('\n' + xml([('code', 'x'), ('flag', 'FALSE')]), tool_schemas=SCHEMAS)
    assert 'block 1' in str(caught.value)
    assert 'line 2' in str(caught.value)
    assert 'column ' in str(caught.value)


def test_native_completed_inline_span_single_delta_releases_without_finish():
    p = protocol()
    gate = p.QwenStreamControlGate()
    assert gate.push('Use `read_file` then explain.') == 'Use `read_file` then explain.'
    assert gate.finish() == ''


def test_native_partial_control_after_completed_inline_span_does_not_leak():
    p = protocol()
    gate = p.QwenStreamControlGate(tool_schemas=SCHEMAS)
    assert gate.push('Use `read_file`\n<') == 'Use `read_file`\n'
    assert gate.push('tool_call><function=run><parameter=code>x</parameter></function></tool_call>') == ''
    assert gate.finish() == ''
