"""Strict, side-effect-free Qwen3.5 function/parameter control codec.

Parameter strings are raw text, not XML entity-decoded or whitespace-trimmed.
A paired multiline envelope removes exactly one matching LF/CRLF at each end;
additional value whitespace remains unchanged. The codec always emits this
explicit multiline envelope, including for empty strings.
Control blocks are executable only at an unfenced physical-line start; the
entire remaining batch must validate before any calls are returned.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator, validators

from .text_tool_protocol import (
    NaturalText, TextToolCall, TextToolProtocolError, ToolCallSequence,
    _markdown_scanned_lines, _markdown_fence_opener, _is_markdown_fence_closer,
)

_OPENER = '<tool_call>'
_RESERVED = ('<tool_call>', '</tool_call>', '<function=', '</function>', '<parameter=', '</parameter>', '<tool_response>', '</tool_response>')
_NAME = r'[A-Za-z_][A-Za-z0-9_]*'
_FUNCTION = re.compile(r'<function=(' + _NAME + r')>')
_PARAMETER = re.compile(r'<parameter=(' + _NAME + r')>')
_SPACE = re.compile(r'(?:[ \t\n]|\r\n)*')
_TYPE_CHECKER = Draft202012Validator.TYPE_CHECKER.redefine(
    'integer', lambda checker, value: isinstance(value, int) and not isinstance(value, bool)
)
_Validator = validators.extend(Draft202012Validator, type_checker=_TYPE_CHECKER)


class QwenToolProtocolError(TextToolProtocolError):
    """Native correction with raw offsets only when actual raw text exists."""
    def __init__(self, code: str, message: str, *, block_index: int | None = None,
                 line: int | None = None, column: int | None = None,
                 duplicate_key: str | None = None) -> None:
        detail = (message + ' Correction: use <tool_call><function=tool_name>'
                  '<parameter=parameter_name>value</parameter></function></tool_call> '
                  'with the exposed schema. No tool was executed.')
        ValueError.__init__(self, detail)
        self.code, self.message = code, detail
        self.block_index, self.line, self.column = block_index, line, column
        self.duplicate_key = duplicate_key


def _error(code: str, message: str, *, raw: str | None = None,
           offset: int = 0, block: int | None = None) -> QwenToolProtocolError:
    line = column = None
    if raw is not None:
        line = raw.count('\n', 0, offset) + 1
        column = offset - raw.rfind('\n', 0, offset)
    location = ''
    if block is not None:
        location += f' block {block}'
    if line is not None:
        location += f', line {line}, column {column}, offset {offset}'
    if location:
        message = f'Native tool protocol{location}: {message}'
    return QwenToolProtocolError(code, message, block_index=block, line=line, column=column)


def _parameters(schema: dict) -> dict:
    function = schema.get('function', schema)
    return function.get('parameters', function)


def _types(schema: dict) -> set[str]:
    declared = schema.get('type')
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return set(declared)
    for keyword in ('anyOf', 'oneOf'):
        if keyword in schema:
            return set().union(*(_types(branch) for branch in schema[keyword]))
    if 'enum' in schema:
        def kind(value):
            if value is None: return 'null'
            if isinstance(value, bool): return 'boolean'
            if isinstance(value, str): return 'string'
            if isinstance(value, int): return 'integer'
            if isinstance(value, float): return 'number'
            if isinstance(value, list): return 'array'
            if isinstance(value, dict): return 'object'
            return ''
        return {kind(value) for value in schema['enum']}
    return set()


def _strict_value(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('non-finite number')
    if isinstance(value, str) and any(marker in value for marker in _RESERVED):
        raise ValueError('native delimiter literal is ambiguous')
    if isinstance(value, dict):
        for key, item in value.items():
            _strict_value(key)
            _strict_value(item)
    elif isinstance(value, list):
        for item in value:
            _strict_value(item)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key {key!r}')
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f'non-finite JSON constant {value}')


def validate_qwen_tool_calls(calls: Sequence[TextToolCall], *,
                             tool_schemas: Mapping[str, dict]) -> ToolCallSequence:
    """Validate a whole provider/native batch without manufacturing raw offsets."""
    accepted = tuple(calls)
    if not accepted:
        raise _error('empty_batch', 'Tool batch is empty.')
    for index, call in enumerate(accepted, 1):
        path = f'calls[{index - 1}]'
        if call.name not in tool_schemas:
            raise _error('unknown_function', f'{path}.function.name: unknown function {call.name!r}.', block=index)
        if not isinstance(call.arguments, dict):
            raise _error('invalid_arguments', f'{path}.function.arguments must be an object.', block=index)
        schema = _parameters(tool_schemas[call.name])
        properties = schema.get('properties', {})
        unknown = set(call.arguments) - set(properties)
        if unknown:
            raise _error('unknown_parameter', f'{path}.function.arguments: unknown parameters {sorted(unknown)!r}.', block=index)
        try:
            for key, value in call.arguments.items():
                types = _types(properties[key])
                if not types or ('string' in types and types != {'string'}):
                    raise ValueError(f'parameter {key!r} has ambiguous or unspecified text encoding')
                _strict_value(value)
            error = next(_Validator(schema).iter_errors(call.arguments), None)
            if error is not None:
                location = '.'.join(str(piece) for piece in error.absolute_path)
                raise ValueError(f'{path}.function.arguments' + ('.' + location if location else '') + ': ' + error.message)
        except (ValueError, TypeError) as exc:
            raise _error('schema_validation', str(exc), block=index) from exc
    return ToolCallSequence(accepted)


def _indented_code(line: str) -> bool:
    column = 0
    for char in line:
        if char == ' ':
            column += 1
        elif char == '\t':
            column += 4 - column % 4
        else:
            break
        if column >= 4:
            return True
    return False


def _code_scan(text: str) -> tuple[list[tuple[int, int]], list[int]]:
    """Completed exact-length code spans and unresolved eligible backtick runs.

    Only runs outside real fences/indented code may open a span. Once opened,
    its matching run may cross lines and takes priority over apparent fences
    in the literal body. Unmatched runs never hide controls in final parsing.
    """
    runs = list(re.finditer(r'`+', text))
    masked = list(text)
    ranges, pending = [], []
    cursor = 0
    while cursor < len(runs):
        run = runs[cursor]
        scanned = _markdown_scanned_lines(''.join(masked))
        blocked = any(start <= run.start() < end and (fenced or _indented_code(line))
                      for line, start, end, fenced in scanned)
        before = text[:run.start()]
        escaped = (len(before) - len(before.rstrip('\\'))) % 2
        if blocked or escaped:
            cursor += 1
            continue
        closer = next((index for index in range(cursor + 1, len(runs))
                       if len(runs[index][0]) == len(run[0])), None)
        if closer is None:
            pending.append(run.start())
            cursor += 1
            continue
        left, right = run.start(), runs[closer].end()
        ranges.append((left, right))
        for position in range(left, right):
            if masked[position] not in '\r\n':
                masked[position] = ' '
        cursor = closer + 1
    return ranges, pending


def _inline_code_ranges(text: str) -> list[tuple[int, int]]:
    return _code_scan(text)[0]


def _find_start(text: str) -> int | None:
    literal_ranges = _inline_code_ranges(text)
    masked = list(text)
    for left, right in literal_ranges:
        for position in range(left, right):
            if masked[position] not in '\r\n':
                masked[position] = ' '
    scanned = _markdown_scanned_lines(''.join(masked))
    for (line, start, _end, _), (_, _, _, fenced) in zip(_markdown_scanned_lines(text), scanned):
        if not fenced and not _indented_code(line):
            stripped = line.lstrip(' \t')
            if stripped.startswith('<tool_call') or (stripped and '<tool_call>'.startswith(stripped) and stripped.startswith('<tool_')):
                position = start + len(line) - len(stripped)
                if not any(left <= position < right for left, right in literal_ranges):
                    return position
    return None


def _parse(text: str, *, tool_schemas: Mapping[str, dict] | None) -> NaturalText | ToolCallSequence:
    start = _find_start(text)
    if start is None:
        return NaturalText(text)
    cursor = start
    calls = []
    locations = []
    def skip():
        nonlocal cursor
        cursor = _SPACE.match(text, cursor).end()
    def expect(token):
        nonlocal cursor
        if not text.startswith(token, cursor):
            raise _error('invalid_grammar', f'Expected {token!r} at offset {cursor}.', raw=text, offset=cursor, block=len(calls) + 1)
        cursor += len(token)
    while cursor < len(text):
        block_start = cursor
        expect(_OPENER)
        skip()
        match = _FUNCTION.match(text, cursor)
        if match is None:
            raise _error('invalid_function', f'Expected one <function=name> at offset {cursor}.', raw=text, offset=cursor, block=len(calls) + 1)
        name = match[1]
        cursor = match.end()
        if tool_schemas is not None and name not in tool_schemas:
            raise _error('unknown_function', f'Unknown function {name!r}.', raw=text, offset=match.start(), block=len(calls) + 1)
        properties = _parameters(tool_schemas[name]).get('properties', {}) if tool_schemas is not None else {}
        arguments = {}
        param_locations = {}
        skip()
        while text.startswith('<parameter', cursor):
            match = _PARAMETER.match(text, cursor)
            if match is None:
                raise _error('invalid_parameter', 'Expected <parameter=name>.', raw=text, offset=cursor, block=len(calls) + 1)
            key = match[1]
            parameter_start = cursor
            cursor = match.end()
            end = text.find('</parameter>', cursor)
            if end < 0:
                raise _error('unclosed_parameter', f'Unclosed parameter {key!r}.', raw=text, offset=cursor, block=len(calls) + 1)
            raw_value = text[cursor:end]
            # Paired grammar newlines are distinct from the value's own bytes.
            newline = '\r\n' if raw_value.startswith('\r\n') else '\n'
            if (raw_value.startswith(newline) and raw_value.endswith(newline)
                    and len(raw_value) >= 2 * len(newline)):
                raw_value = raw_value[len(newline):-len(newline)]
            try:
                if key in arguments:
                    raise ValueError(f'duplicate parameter {key!r}')
                if any(marker in raw_value for marker in _RESERVED):
                    raise ValueError(f'parameter {key!r}: native delimiter literal is ambiguous')
                if tool_schemas is None:
                    value = raw_value
                else:
                    if key not in properties:
                        raise ValueError(f'unknown parameter {key!r}')
                    types = _types(properties[key])
                    if not types or ('string' in types and types != {'string'}):
                        raise ValueError(f'parameter {key!r}: ambiguous or unspecified schema encoding')
                    value = raw_value if types == {'string'} else json.loads(raw_value, object_pairs_hook=_pairs, parse_constant=_constant)
                    _strict_value(value)
            except (ValueError, TypeError) as exc:
                raise _error('invalid_parameter_value', f'Parameter {key!r}: {exc}', raw=text, offset=cursor, block=len(calls) + 1) from exc
            arguments[key] = value
            param_locations[key] = parameter_start
            cursor = end + len('</parameter>')
            skip()
        expect('</function>')
        skip()
        expect('</tool_call>')
        calls.append(TextToolCall(name, arguments))
        locations.append((block_start, param_locations))
        skip()
    if tool_schemas is None:
        return ToolCallSequence(tuple(calls))
    try:
        return validate_qwen_tool_calls(calls, tool_schemas=tool_schemas)
    except QwenToolProtocolError as exc:
        index = (exc.block_index or 1) - 1
        offset = locations[index][0]
        # The actual validator path identifies the offending parameter when present.
        for key, position in locations[index][1].items():
            if '.arguments.' + key in str(exc):
                offset = position
                break
        raise _error(exc.code, str(exc).split(' Correction:')[0], raw=text, offset=offset, block=index + 1) from exc


def native_control_prose(text: str) -> str:
    """Project the untouched prefix of an accepted native control candidate.

    This never validates or authorizes calls. Use it only after whole-batch
    acceptance; inert Markdown examples remain part of the prose.
    """
    start = _find_start(text)
    return text if start is None else text[:start]


def parse_qwen_protocol(text: str, *, tool_schemas: Mapping[str, dict]) -> NaturalText | ToolCallSequence:
    return _parse(text, tool_schemas=tool_schemas)


def format_qwen_tool_calls(calls: Sequence[TextToolCall], *, tool_schemas: Mapping[str, dict]) -> str:
    accepted = validate_qwen_tool_calls(calls, tool_schemas=tool_schemas)
    blocks = []
    for call in accepted.calls:
        if not re.fullmatch(_NAME, call.name) or any(not re.fullmatch(_NAME, key) for key in call.arguments):
            raise _error('invalid_identifier', 'Function and parameter names must be canonical identifiers.')
        parts = ['<tool_call><function=' + call.name + '>']
        for key, value in call.arguments.items():
            encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            parts.append('<parameter=' + key + '>\n' + encoded + '\n</parameter>')
        parts.append('</function></tool_call>')
        blocks.append(''.join(parts))
    return '\n'.join(blocks)


class QwenStreamControlGate:
    """Incremental unfenced-line control suppression; EOF validates the batch.

    Backtick spans are held only while unresolved; completed literal spans and
    following prose are released immediately without exposing control tails.
    With no schemas this checks framing only. Callers must parse the full model
    candidate against the request's schemas before accepting or executing calls.
    """
    def __init__(self, *, tool_schemas: Mapping[str, dict] | None = None):
        self._schemas = tool_schemas
        self._raw = ''
        self._pending = ''
        self._line = ''
        self._fence = None
        self._suppressed = False
        self._finished = False
        self._markdown_buffer = False
        self._visible_count = 0

    @property
    def suppresses_visible_content(self) -> bool:
        return self._suppressed

    def push(self, delta: str) -> str:
        if self._finished:
            raise RuntimeError('QwenStreamControlGate is already finished')
        self._raw += delta
        if self._suppressed:
            return ''
        if self._markdown_buffer:
            return self._flush_markdown()
        visible = []
        for char in delta:
            if self._suppressed:
                break
            if char == '`':
                # A completed inline span may cross physical lines. Hold from
                # its first backtick until EOF so classification never leaks a
                # potential control inside an as-yet unclosed literal span.
                self._markdown_buffer = True
                break
            if self._fence is None and not self._line.strip(' \t') and not _indented_code(self._line):
                candidate = self._pending + char
                if candidate == '<tool_call':
                    self._suppressed = True
                    self._pending = ''
                    break
                if _OPENER.startswith(candidate):
                    self._pending = candidate
                    continue
                if self._pending:
                    visible.append(self._pending)
                    self._line += self._pending
                    self._pending = ''
            visible.append(char)
            if char == '\n':
                line = self._line.removesuffix('\r')
                if self._fence is None:
                    self._fence = _markdown_fence_opener(line)
                elif _is_markdown_fence_closer(line, self._fence):
                    self._fence = None
                self._line = ''
            else:
                self._line += char
        result = ''.join(visible)
        self._visible_count += len(result)
        if self._markdown_buffer:
            result += self._flush_markdown()
        return result

    def _flush_markdown(self) -> str:
        _, pending = _code_scan(self._raw)
        safe_end = min(pending) if pending else len(self._raw)
        start = _find_start(self._raw)
        last_line = self._raw.rsplit('\n', 1)[-1]
        stripped = last_line.lstrip(' \t')
        if (start is None and stripped.startswith('<') and _OPENER.startswith(stripped)
                and not _indented_code(last_line)
                and not _markdown_scanned_lines(self._raw)[-1][3]):
            safe_end = min(safe_end, len(self._raw) - len(stripped))
        if start is not None and start <= safe_end:
            self._suppressed = True
            safe_end = start
        result = self._raw[self._visible_count:safe_end]
        self._visible_count = safe_end
        if not pending and not self._suppressed and safe_end == len(self._raw):
            self._markdown_buffer = False
            self._pending = ''
            self._line = self._raw.rsplit('\n', 1)[-1]
            self._fence = None
            # Restore real fence state; completed inline spans cannot open one.
            masked = list(self._raw)
            for left, right in _inline_code_ranges(self._raw):
                for position in range(left, right):
                    if masked[position] not in '\r\n':
                        masked[position] = ' '
            for line in ''.join(masked).split('\n')[:-1]:
                line = line.removesuffix('\r')
                if self._fence is None:
                    self._fence = _markdown_fence_opener(line)
                elif _is_markdown_fence_closer(line, self._fence):
                    self._fence = None
        return result

    def finish(self) -> str:
        if self._finished:
            return ''
        self._finished = True
        if self._markdown_buffer:
            start = _find_start(self._raw)
            if start is None:
                return self._raw[self._visible_count:]
            self._suppressed = True
            _parse(self._raw, tool_schemas=self._schemas)
            return self._raw[self._visible_count:start]
        if self._suppressed or self._pending.startswith('<tool_'):
            self._suppressed = True
            _parse(self._raw, tool_schemas=self._schemas)
            return ''
        pending, self._pending = self._pending, ''
        return pending
