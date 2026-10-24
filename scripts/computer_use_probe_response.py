"""Strict response extraction for the two direct SGLang computer-use probes."""
from __future__ import annotations

import ast
import json
import re

from backend.computer_use.contracts import thaw


_TOOL_CODE = re.compile(r'\A\s*<tool_code>\s*(.*?)\s*</tool_code>\s*\Z', re.DOTALL)


def _literal_tool_code(content: str, allowed: set[str]) -> dict:
    if not isinstance(content, str) or len(content) > 4096:
        raise ValueError('final content is not one bounded tool-code call')
    match = _TOOL_CODE.fullmatch(content)
    if match is None:
        raise ValueError('final content is not one complete tool-code call')
    try:
        expression = ast.parse(match.group(1), mode='eval').body
    except SyntaxError as exc:
        raise ValueError('tool-code call is malformed') from exc
    if (not isinstance(expression, ast.Call)
            or not isinstance(expression.func, ast.Name)
            or expression.func.id not in allowed
            or expression.args):
        raise ValueError('tool-code call is not an allowlisted stage function')
    arguments = {}
    for keyword in expression.keywords:
        if keyword.arg is None or keyword.arg in arguments:
            raise ValueError('tool-code arguments must be distinct named literals')
        try:
            value = ast.literal_eval(keyword.value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
            raise ValueError('tool-code arguments must be literals') from exc
        if type(value) not in (int, float, str):
            raise ValueError('tool-code arguments must be numeric or string literals')
        arguments[keyword.arg] = value
    return {'name': expression.func.id, 'arguments': arguments}


def extract_probe_call(choice: dict, encoded) -> tuple[dict, dict]:
    """Prefer native calls; accept one complete final-content tool-code call otherwise.

    The caller must still use decode_stage on the returned envelope. This
    function does not inspect reasoning_content or extract numbers from prose.
    """
    allowed = {tool['name'] for tool in thaw(encoded.tools)}
    message = choice['message']
    calls = message.get('tool_calls') or []
    if len(calls) == 1:
        call = calls[0]['function']
        raw = {'name': call['name'], 'arguments': json.loads(call['arguments'])}
        source = 'structured_tool_calls'
    elif len(calls) == 0 and choice.get('finish_reason') == 'stop':
        content = message.get('content')
        try:
            raw = _literal_tool_code(content, allowed)
        except ValueError as exc:
            has_tool_code = isinstance(content, str) and _TOOL_CODE.fullmatch(content) is not None
            raise ValueError(f'expected one tool call; got 0; finish_reason="stop"; '
                             f'final_content_tool_code_present={has_tool_code}; {exc}') from exc
        source = 'final_content_tool_code'
    else:
        content = message.get('content')
        has_tool_code = isinstance(content, str) and _TOOL_CODE.fullmatch(content) is not None
        raise ValueError(f'expected one tool call; got {len(calls)}; '
                         f'finish_reason={choice.get("finish_reason")!r}; '
                         f'final_content_tool_code_present={has_tool_code}')
    if raw['name'] not in allowed or not isinstance(raw['arguments'], dict):
        raise ValueError('response tool call is not in the encoded stage schema')
    return raw, {'finish_reason': choice.get('finish_reason'), 'tool_call': raw,
                 'response_source': source}
