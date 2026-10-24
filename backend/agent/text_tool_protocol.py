"""Strict framed text protocol for local-model tool calls and agent routing.

This module is deliberately side-effect free. It parses only a complete
assistant visible-content candidate and never executes tools or mutates Agent
state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import AbstractSet, Any


TOOL_CALL_OPENER = "<<TOOL_CALL>>"
TOOL_CALL_CLOSER = "<<END_TOOL_CALL>>"
SWITCH_AGENT_OPENER = "<<SWITCH_AGENT>>"
LEGACY_OPENERS = ("Calling tools:", "调用工具:")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOOL_OPENER_RE = re.compile(rf"^{re.escape(TOOL_CALL_OPENER)}$")
_SWITCH_COMMAND_RE = re.compile(
    rf"^{re.escape(SWITCH_AGENT_OPENER)}[ \t]+(?P<target>[A-Za-z_][A-Za-z0-9_]*)[ \t]*$"
)
_ALLOWED_FENCE_OPENER_RE = re.compile(r"^```(?:xml|json|text)?[ \t]*$")
_STANDALONE_FENCE_RE = re.compile(r"(?m)^```(?:\r?\n|$)")
_NESTED_FENCE_RE = re.compile(r"(?m)^[ \t]*```")
_TRAILING_REASONING_FENCE_RE = re.compile(
    r"(?:^|\r?\n)(?P<opener>```(?:xml|json|text)?[ \t]*)"
    r"(?:\r?\n)?[ \t]*$"
)
_NEW_CONTROL_OPENERS = (TOOL_CALL_OPENER, SWITCH_AGENT_OPENER)
_RESERVED_OPENERS = (*_NEW_CONTROL_OPENERS, *LEGACY_OPENERS)
_UNICODE_LINE_BOUNDARY_RE = re.compile(r"\r\n|[\n\r\x85\u2028\u2029]")
_CORRECTION_SUFFIX = (
    "Correction: output <<TOOL_CALL>> followed by one or more strict JSON "
    'objects shaped exactly as {"name":"tool_name","arguments":{}}, using '
    "double quotes and two-space indentation, then close with <<END_TOOL_CALL>>."
)


@dataclass(frozen=True)
class _MarkdownFence:
    marker: str
    run_length: int


def _markdown_fence_opener(line: str) -> _MarkdownFence | None:
    """Return a CommonMark-style fence opener for one physical line.

    We only need the fence properties that affect control-marker safety: up to
    three leading spaces, a backtick or tilde run of at least three, and a
    backtick info string that itself contains no backtick.
    """

    match = re.fullmatch(r" {0,3}(?P<run>`{3,}|~{3,})(?P<info>.*)", line)
    if match is None:
        return None
    run = match.group("run")
    if run[0] == "`" and "`" in match.group("info"):
        return None
    return _MarkdownFence(marker=run[0], run_length=len(run))


def _is_markdown_fence_closer(line: str, fence: _MarkdownFence) -> bool:
    match = re.fullmatch(
        rf" {{0,3}}(?P<run>{re.escape(fence.marker)}{{{fence.run_length},}})[ \t]*",
        line,
    )
    return match is not None


def _markdown_scanned_lines(
    content: str,
) -> list[tuple[str, int, int, bool]]:
    """Return physical lines with whether each line is fenced syntax/content."""

    scanned: list[tuple[str, int, int, bool]] = []
    fence: _MarkdownFence | None = None
    for line, raw_start, raw_end in _physical_lines(content):
        if fence is not None:
            scanned.append((line, raw_start, raw_end, True))
            if _is_markdown_fence_closer(line, fence):
                fence = None
            continue
        opener = _markdown_fence_opener(line)
        if opener is not None:
            scanned.append((line, raw_start, raw_end, True))
            fence = opener
            continue
        scanned.append((line, raw_start, raw_end, False))
    return scanned


class TextToolProtocolError(ValueError):
    """A fail-closed protocol error suitable for AgentParsingError feedback."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        block_index: int | None = None,
        line: int | None = None,
        column: int | None = None,
        duplicate_key: str | None = None,
    ) -> None:
        detail = message.rstrip()
        if _CORRECTION_SUFFIX not in detail:
            detail = f"{detail} {_CORRECTION_SUFFIX}"
        if "No tool was executed." not in detail:
            detail = f"{detail} No tool was executed."
        super().__init__(detail)
        self.code = code
        self.message = detail
        self.block_index = block_index
        self.line = line
        self.column = column
        self.duplicate_key = duplicate_key


@dataclass(frozen=True)
class TextToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class NaturalText:
    text: str


@dataclass(frozen=True)
class ToolCallSequence:
    calls: tuple[TextToolCall, ...]


@dataclass(frozen=True)
class SwitchAgentCommand:
    target_agent: str
    raw_payload: str


TextProtocolResult = NaturalText | ToolCallSequence | SwitchAgentCommand


@dataclass(frozen=True)
class _OuterFenceEnvelope:
    opener_line: str
    body: str
    whole_response: bool
    has_closing_fence: bool


class _DuplicateJSONKey(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class _NonFiniteJSONConstant(ValueError):
    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(key)
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> Any:
    raise _NonFiniteJSONConstant(value)


def _protocol_lstrip(value: str) -> str:
    """Strip only protocol-approved leading whitespace.

    The control grammar accepts horizontal space/tab plus LF and CRLF. Bare CR
    and other Unicode line separators are intentionally left in place.
    """

    index = 0
    while index < len(value):
        if value[index] in " \t\n":
            index += 1
            continue
        if value.startswith("\r\n", index):
            index += 2
            continue
        break
    return value[index:]


def _protocol_rstrip(value: str) -> str:
    """Strip only protocol-approved trailing whitespace."""

    index = len(value)
    while index > 0:
        if value[index - 1] in " \t":
            index -= 1
            continue
        if value[index - 1] == "\n":
            index -= 2 if index > 1 and value[index - 2] == "\r" else 1
            continue
        break
    return value[:index]


def _protocol_strip(value: str) -> str:
    return _protocol_rstrip(_protocol_lstrip(value))


def _is_protocol_whitespace(value: str) -> bool:
    return not _protocol_strip(value)


def _physical_lines(content: str) -> list[tuple[str, int, int]]:
    """Split framing lines only on LF/CRLF while retaining raw offsets."""

    lines: list[tuple[str, int, int]] = []
    raw_start = 0
    while True:
        newline = content.find("\n", raw_start)
        if newline < 0:
            lines.append((content[raw_start:], raw_start, len(content)))
            return lines
        framing_end = (
            newline - 1
            if newline > raw_start and content[newline - 1] == "\r"
            else newline
        )
        lines.append((content[raw_start:framing_end], raw_start, newline + 1))
        raw_start = newline + 1
        if raw_start == len(content):
            lines.append(("", raw_start, raw_start))
            return lines


def _find_prefixed_tool_sequence_start(content: str) -> int | None:
    """Find the first physical-line tool opener outside Markdown fences.

    Bare CR, NEL, LS, and PS are not physical line boundaries. An earlier
    line-start control attempt also prevents a later tool block from hiding it.
    """

    for framing_line, raw_start, _raw_end, is_fenced in _markdown_scanned_lines(
        content
    ):
        if is_fenced:
            continue
        candidate = framing_line.lstrip(" \t")
        if _TOOL_OPENER_RE.fullmatch(framing_line):
            return raw_start
        if any(candidate.startswith(opener) for opener in _NEW_CONTROL_OPENERS):
            return None
    return None


def starts_new_control_opener(content: str | None) -> bool:
    """Return whether content contains an executable new control opener."""

    if not content:
        return False
    stripped = _protocol_lstrip(content)
    return any(stripped.startswith(opener) for opener in _NEW_CONTROL_OPENERS) or (
        _find_prefixed_tool_sequence_start(content) is not None
    )


def starts_textual_tool_call_attempt(content: str | None) -> bool:
    """Return whether content contains a current or legacy tool attempt."""

    if not content:
        return False
    stripped = _protocol_lstrip(content)
    return stripped.startswith(TOOL_CALL_OPENER) or any(
        stripped.startswith(opener) for opener in LEGACY_OPENERS
    ) or _find_prefixed_tool_sequence_start(content) is not None


def _find_misplaced_control_opener(content: str) -> tuple[str, int] | None:
    """Find a line-start control marker after prose, excluding Markdown fences.

    This deliberately does not search for inline marker mentions. It also keeps
    fenced documentation inert: only a marker that begins an ordinary response
    line outside a fence is classified as a malformed control attempt.
    """

    stripped = _protocol_lstrip(content)
    if stripped.startswith(SWITCH_AGENT_OPENER):
        # The ordinary strict parser owns a response that already entered
        # control mode, including its later blocks and any trailing content.
        return None
    if _find_prefixed_tool_sequence_start(content) is not None:
        # Prose before the first exact physical-line tool opener is accepted.
        # The strict sequence parser owns everything from that opener onward.
        return None

    saw_visible_prefix = False
    for line_index, (physical_line, _start, _end, is_fenced) in enumerate(
        _markdown_scanned_lines(content), start=1
    ):
        if is_fenced:
            saw_visible_prefix = True
            continue
        # Unicode separators are classification boundaries only. They remain
        # inside one executable physical line and are never normalized.
        for raw_line in _UNICODE_LINE_BOUNDARY_RE.split(physical_line):
            stripped_line = raw_line.lstrip(" \t")
            attempted_opener = next(
                (
                    opener
                    for opener in _NEW_CONTROL_OPENERS
                    if stripped_line.startswith(opener)
                ),
                None,
            )
            if attempted_opener is not None and (
                saw_visible_prefix
                or (
                    attempted_opener == TOOL_CALL_OPENER
                    and raw_line != stripped_line
                )
            ):
                return attempted_opener, line_index
            if raw_line.strip(" \t"):
                saw_visible_prefix = True
    return None


def _looks_like_control_with_unrestricted_edges(content: str) -> bool:
    stripped = content.lstrip()
    return any(stripped.startswith(opener) for opener in _RESERVED_OPENERS)


def _has_forbidden_protocol_boundary(content: str) -> bool:
    """Detect whitespace that old unrestricted stripping made executable."""

    if not content:
        return False

    if (
        _protocol_lstrip(content) != content.lstrip()
        and _looks_like_control_with_unrestricted_edges(content)
    ):
        return True
    if (
        _protocol_rstrip(content) != content.rstrip()
        and _looks_like_control_with_unrestricted_edges(content)
        and not _protocol_lstrip(content).startswith(TOOL_CALL_OPENER)
        and _find_prefixed_tool_sequence_start(content) is None
    ):
        return True

    # Markdown-fenced bytes are inert display content. In particular, Unicode
    # separators inside the fence body cannot reactivate protocol whitespace
    # checks through an unrestricted secondary fence parser.
    return False


def has_malformed_control_attempt(content: str | None) -> bool:
    """Return whether content is a recognizable but noncanonical control.

    The function classifies only enough to fail closed. It never extracts a
    tool name or arguments and therefore cannot turn malformed text into an
    executable call.
    """

    if not content:
        return False
    return (
        _has_forbidden_protocol_boundary(content)
        or _find_misplaced_control_opener(content) is not None
    )


def _reject_malformed_control_attempt(content: str) -> None:
    if _has_forbidden_protocol_boundary(content):
        raise TextToolProtocolError(
            "invalid_protocol_whitespace",
            (
                "Control protocol error: only spaces, tabs, LF or CRLF are "
                "allowed around a control response and its outer fence. Bare "
                "CR and other Unicode line separators are not executable "
                "protocol whitespace."
            ),
        )

    misplaced = _find_misplaced_control_opener(content)
    if misplaced is not None:
        opener, line = misplaced
        raise TextToolProtocolError(
            "misplaced_control_opener",
            (
                f"Control protocol error on line {line}: {opener} is not an "
                "exact standalone physical-line tool opener outside Markdown "
                "fences. Natural-language prose may precede only a canonical "
                "tool opener on a real LF/CRLF line. An agent switch must still "
                "occupy the entire response as exactly one "
                f"{SWITCH_AGENT_OPENER} target line."
            ),
            line=line,
        )

def _inspect_outer_fence(
    content: str,
    *,
    unrestricted_whitespace: bool = False,
) -> _OuterFenceEnvelope | None:
    """Inspect one possible whole-response fence without stripping its body."""

    stripped = content.lstrip() if unrestricted_whitespace else _protocol_lstrip(content)
    if not stripped.startswith("```"):
        return None
    newline = stripped.find("\n")
    if newline < 0:
        return None
    opener_line = stripped[:newline]
    if opener_line.endswith("\r"):
        opener_line = opener_line[:-1]
    rest = stripped[newline + 1 :]
    closing_matches = list(_STANDALONE_FENCE_RE.finditer(rest))
    for match in reversed(closing_matches):
        suffix = rest[match.end() :]
        if (
            not suffix.strip()
            if unrestricted_whitespace
            else _is_protocol_whitespace(suffix)
        ):
            return _OuterFenceEnvelope(
                opener_line=opener_line,
                body=rest[: match.start()],
                whole_response=True,
                has_closing_fence=True,
            )
    return _OuterFenceEnvelope(
        opener_line=opener_line,
        body=rest,
        whole_response=False,
        has_closing_fence=bool(closing_matches),
    )


def _fenced_control_kind(content: str | None) -> str | None:
    """Classify only a fence whose first interior token is a control opener."""

    if not content:
        return None
    envelope = _inspect_outer_fence(content)
    if envelope is None:
        return None
    # A closed fence followed by prose is an ordinary Markdown response.
    if not envelope.whole_response and envelope.has_closing_fence:
        return None
    interior = _protocol_lstrip(envelope.body)
    if interior.startswith(TOOL_CALL_OPENER):
        return "tool"
    if interior.startswith(SWITCH_AGENT_OPENER):
        return "switch"
    if any(interior.startswith(opener) for opener in LEGACY_OPENERS):
        return "legacy"
    return None


def reconstruct_split_fenced_tool_call(
    reasoning: str | None,
    content: str | None,
) -> str | None:
    """Rebuild only an allowed trailing-reasoning fence plus exact content.

    This is a narrow provider wrapper adapter. It never includes earlier
    reasoning and never makes markers found in reasoning executable.
    """

    if not reasoning or not content:
        return None
    match = _TRAILING_REASONING_FENCE_RE.search(reasoning)
    if match is None or not _protocol_lstrip(content).startswith(TOOL_CALL_OPENER):
        return None
    fragment = f"{match.group('opener')}\n{content}"
    envelope = _inspect_outer_fence(fragment)
    if (
        envelope is None
        or not envelope.whole_response
        or _fenced_control_kind(fragment) != "tool"
    ):
        return None
    return fragment


def reject_legacy_opener(content: str | None) -> None:
    """Permanently reject exact legacy openers without inspecting payloads."""

    if not content:
        return
    stripped = _protocol_lstrip(content)
    if any(stripped.startswith(opener) for opener in LEGACY_OPENERS):
        raise TextToolProtocolError(
            "unsupported_legacy_protocol",
            "Unsupported legacy tool-call protocol. Use the framed text protocol.",
        )


def _skip_json_separator_whitespace(body: str, index: int) -> int:
    """Skip only space/tab/LF/CRLF between top-level call objects."""

    while index < len(body):
        if body[index] in " \t\n":
            index += 1
            continue
        if body.startswith("\r\n", index):
            index += 2
            continue
        break
    return index


def _parse_call_objects(
    body: str,
    *,
    first_item_index: int,
    allowed_tool_names: AbstractSet[str] | None,
) -> list[TextToolCall]:
    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_finite_constant,
    )
    calls: list[TextToolCall] = []
    cursor = _skip_json_separator_whitespace(body, 0)
    while cursor < len(body):
        item_index = first_item_index + len(calls)
        try:
            parsed, end = decoder.raw_decode(body, cursor)
        except _DuplicateJSONKey as exc:
            raise TextToolProtocolError(
                "duplicate_json_key",
                f'Tool-call protocol error in item {item_index}: duplicate JSON key "{exc.key}".',
                block_index=item_index,
                duplicate_key=exc.key,
            ) from exc
        except _NonFiniteJSONConstant as exc:
            raise TextToolProtocolError(
                "non_finite_json",
                f"Tool-call protocol error in item {item_index}: non-finite JSON constant {exc.value} is not allowed.",
                block_index=item_index,
            ) from exc
        except json.JSONDecodeError as exc:
            raise TextToolProtocolError(
                "invalid_json",
                (
                    f"Tool-call protocol error in item {item_index}: invalid JSON "
                    f"at line {exc.lineno}, column {exc.colno}: {exc.msg}."
                ),
                block_index=item_index,
                line=exc.lineno,
                column=exc.colno,
            ) from exc

        if not isinstance(parsed, dict):
            raise TextToolProtocolError(
                "call_must_be_object",
                f"Tool-call protocol error in item {item_index}: each call must be one JSON object; arrays are not accepted.",
                block_index=item_index,
            )
        if set(parsed) != {"name", "arguments"}:
            raise TextToolProtocolError(
                "invalid_call_schema",
                (
                    f"Tool-call protocol error in item {item_index}: object keys "
                    "must be exactly 'name' and 'arguments'."
                ),
                block_index=item_index,
            )
        tool_name = parsed["name"]
        if not isinstance(tool_name, str) or not _IDENTIFIER_RE.fullmatch(tool_name):
            raise TextToolProtocolError(
                "invalid_tool_name",
                f"Tool-call protocol error in item {item_index}: name must be a valid identifier string.",
                block_index=item_index,
            )
        arguments = parsed["arguments"]
        if not isinstance(arguments, dict):
            raise TextToolProtocolError(
                "arguments_must_be_object",
                f"Tool-call protocol error in item {item_index}: arguments must be a JSON object.",
                block_index=item_index,
            )
        if allowed_tool_names is not None and tool_name not in allowed_tool_names:
            allowed = ", ".join(sorted(allowed_tool_names)) or "<none>"
            raise TextToolProtocolError(
                "unknown_tool",
                f"Tool-call protocol error in item {item_index}: unknown tool {tool_name!r}; allowed tools: {allowed}.",
                block_index=item_index,
            )
        calls.append(TextToolCall(name=tool_name, arguments=arguments))
        cursor = _skip_json_separator_whitespace(body, end)
        if cursor == end and cursor < len(body):
            next_item_index = first_item_index + len(calls)
            raise TextToolProtocolError(
                "missing_call_separator",
                (
                    f"Tool-call protocol error before item {next_item_index}: "
                    "top-level call objects must be separated by space, tab, "
                    "LF, or CRLF."
                ),
                block_index=next_item_index,
            )
    return calls


def _parse_switch_command(
    content: str,
    *,
    allowed_agent_names: AbstractSet[str] | None,
) -> SwitchAgentCommand:
    candidate = _protocol_strip(content)
    match = _SWITCH_COMMAND_RE.fullmatch(candidate)
    allowed = (
        ", ".join(sorted(allowed_agent_names)) or "<none>"
        if allowed_agent_names is not None
        else "<any valid identifier>"
    )
    if match is None:
        attempted_target = candidate[len(SWITCH_AGENT_OPENER) :]
        attempted_target = attempted_target.splitlines()[0].strip() or "<missing>"
        raise TextToolProtocolError(
            "invalid_switch_command",
            (
                "Switch-agent protocol error: invalid command for attempted target "
                f"{attempted_target!r}; allowed targets: {allowed}. Output exactly "
                f"one {SWITCH_AGENT_OPENER} target line."
            ),
        )

    target = match.group("target")
    if allowed_agent_names is not None and target not in allowed_agent_names:
        raise TextToolProtocolError(
            "unsupported_agent",
            f"Switch-agent protocol error: unsupported target {target!r}; allowed targets: {allowed}.",
        )
    return SwitchAgentCommand(target_agent=target, raw_payload=candidate)


def _parse_tool_call_sequence(
    content: str,
    *,
    allowed_tool_names: AbstractSet[str] | None,
) -> ToolCallSequence:
    # Keep byte-for-byte JSON bodies. Only the framing view removes the CR
    # that belongs to a CRLF line ending.
    lines = _physical_lines(content)

    line_index = 0
    calls: list[TextToolCall] = []

    while line_index < len(lines):
        while (
            line_index < len(lines)
            and not lines[line_index][0].strip(" \t")
        ):
            line_index += 1
        if line_index >= len(lines):
            break

        opener_line = lines[line_index][0]
        if _TOOL_OPENER_RE.fullmatch(opener_line) is None:
            code = (
                "invalid_opener"
                if opener_line.startswith(TOOL_CALL_OPENER)
                else "unexpected_content"
            )
            raise TextToolProtocolError(
                code,
                (
                    f"Tool-call protocol error before item {len(calls) + 1}: "
                    f"expected an exact {TOOL_CALL_OPENER} opener line."
                ),
                block_index=len(calls) + 1,
            )

        first_item_index = len(calls) + 1
        body_start = lines[line_index][2]
        line_index += 1

        while (
            line_index < len(lines)
            and not lines[line_index][0].startswith(TOOL_CALL_CLOSER)
        ):
            line_index += 1
        if line_index >= len(lines):
            raise TextToolProtocolError(
                "missing_closer",
                (
                    f"Tool-call protocol error before item {first_item_index}: missing "
                    f"standalone {TOOL_CALL_CLOSER} line."
                ),
                block_index=first_item_index,
            )

        body = content[body_start:lines[line_index][1]]
        frame_calls = _parse_call_objects(
            body,
            first_item_index=first_item_index,
            allowed_tool_names=allowed_tool_names,
        )
        if not frame_calls:
            raise TextToolProtocolError(
                "empty_tool_frame",
                f"Tool-call protocol error before item {first_item_index}: frame must contain at least one call object.",
                block_index=first_item_index,
            )
        calls.extend(frame_calls)
        line_index += 1

    if not calls:
        raise TextToolProtocolError(
            "empty_tool_sequence",
            "Tool-call protocol error: at least one complete tool-call block is required.",
        )
    return ToolCallSequence(tuple(calls))


def parse_text_protocol(
    content: str | None,
    *,
    allowed_tool_names: AbstractSet[str] | None = None,
    allowed_agent_names: AbstractSet[str] | None = None,
) -> TextProtocolResult:
    """Parse one complete visible assistant response using whole-response mode."""

    text = content or ""
    _reject_malformed_control_attempt(text)
    reject_legacy_opener(text)
    stripped = _protocol_lstrip(text)

    envelope = _inspect_outer_fence(text)
    fenced_kind = _fenced_control_kind(text)
    if envelope is not None and fenced_kind is not None:
        # Markdown fences are display/documentation syntax. Tool, switch, and
        # retired examples inside them are all inert, including unclosed fences.
        return NaturalText(text)

    if stripped.startswith(SWITCH_AGENT_OPENER):
        return _parse_switch_command(
            stripped,
            allowed_agent_names=allowed_agent_names,
        )
    if stripped.startswith(TOOL_CALL_OPENER):
        return _parse_tool_call_sequence(
            stripped,
            allowed_tool_names=allowed_tool_names,
        )
    prefixed_tool_start = _find_prefixed_tool_sequence_start(text)
    if prefixed_tool_start is not None:
        return _parse_tool_call_sequence(
            text[prefixed_tool_start:],
            allowed_tool_names=allowed_tool_names,
        )
    return NaturalText(text)


def format_tool_call_sequence(calls: Sequence[TextToolCall]) -> str:
    """Render tool calls in the canonical two-space-indented text format."""

    if not calls:
        raise TextToolProtocolError(
            "empty_tool_sequence",
            "Cannot format an empty tool-call sequence.",
        )

    objects: list[str] = []
    for block_index, call in enumerate(calls, start=1):
        if not _IDENTIFIER_RE.fullmatch(call.name):
            raise TextToolProtocolError(
                "invalid_tool_name",
                f"Cannot format invalid tool name {call.name!r}.",
                block_index=block_index,
            )
        if not isinstance(call.arguments, dict):
            raise TextToolProtocolError(
                "arguments_must_be_object",
                f"Cannot format block {block_index}: arguments must be an object.",
                block_index=block_index,
            )
        try:
            body = json.dumps(
                {"name": call.name, "arguments": call.arguments},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
        except ValueError as exc:
            raise TextToolProtocolError(
                "non_finite_json",
                f"Cannot format block {block_index}: non-finite JSON is not allowed.",
                block_index=block_index,
            ) from exc
        except TypeError as exc:
            raise TextToolProtocolError(
                "invalid_json_value",
                f"Cannot format block {block_index}: arguments are not JSON serializable.",
                block_index=block_index,
            ) from exc
        objects.append(body)
    return f"{TOOL_CALL_OPENER}\n" + "\n\n".join(objects) + f"\n{TOOL_CALL_CLOSER}"


def format_switch_agent(target_agent: str) -> str:
    """Render one canonical switch-agent control line."""

    if not _IDENTIFIER_RE.fullmatch(target_agent):
        raise TextToolProtocolError(
            "invalid_agent_name",
            f"Cannot format invalid agent name {target_agent!r}.",
        )
    return f"{SWITCH_AGENT_OPENER} {target_agent}"


class StreamControlGate:
    """Release prose/fenced examples while hiding executable control tails."""

    def __init__(self) -> None:
        self._mode = "pending"
        self._buffer = ""
        self._finished = False
        self._fence: _MarkdownFence | None = None
        self._line_buffer = ""
        self._control_candidate = ""

    @property
    def suppresses_visible_content(self) -> bool:
        return self._mode == "suppressed"

    def _finish_physical_line(self) -> None:
        line = self._line_buffer[:-1] if self._line_buffer.endswith("\r") else self._line_buffer
        if self._fence is not None:
            if _is_markdown_fence_closer(line, self._fence):
                self._fence = None
        else:
            self._fence = _markdown_fence_opener(line)
        self._line_buffer = ""
        self._control_candidate = ""

    def _scan_visible(self, value: str) -> str:
        emitted: list[str] = []
        visible_openers = _NEW_CONTROL_OPENERS
        for char in value:
            if self._mode == "suppressed":
                break

            if char == "\n":
                if self._control_candidate:
                    candidate = self._control_candidate
                    if candidate in visible_openers or candidate == TOOL_CALL_OPENER + "\r":
                        self._mode = "suppressed"
                        self._control_candidate = ""
                        break
                    emitted.append(candidate)
                    self._line_buffer += candidate
                    self._control_candidate = ""
                emitted.append(char)
                self._finish_physical_line()
                continue

            if self._fence is None and not self._line_buffer:
                candidate = self._control_candidate + char
                if candidate == SWITCH_AGENT_OPENER:
                    self._mode = "suppressed"
                    self._control_candidate = ""
                    break
                if any(opener.startswith(candidate) for opener in visible_openers):
                    self._control_candidate = candidate
                    continue
                if candidate == TOOL_CALL_OPENER + "\r":
                    self._control_candidate = candidate
                    continue
                if self._control_candidate:
                    emitted.append(candidate)
                    self._line_buffer += candidate
                    self._control_candidate = ""
                    continue

            emitted.append(char)
            self._line_buffer += char
        return "".join(emitted)

    def push(self, delta: str) -> str:
        if self._finished:
            raise RuntimeError("StreamControlGate is already finished")
        if not delta:
            return ""
        if self._mode == "visible":
            return self._scan_visible(delta)
        if self._mode == "suppressed":
            return ""

        self._buffer += delta
        stripped = _protocol_lstrip(self._buffer)
        if not stripped:
            return ""
        if any(
            stripped.startswith(opener)
            for opener in (*LEGACY_OPENERS, SWITCH_AGENT_OPENER)
        ):
            self._mode = "suppressed"
            self._buffer = ""
            return ""
        if stripped.startswith(TOOL_CALL_OPENER + "\n") or stripped.startswith(
            TOOL_CALL_OPENER + "\r\n"
        ):
            self._mode = "suppressed"
            self._buffer = ""
            return ""
        if any(opener.startswith(stripped) for opener in _RESERVED_OPENERS):
            return ""
        if stripped in {TOOL_CALL_OPENER, TOOL_CALL_OPENER + "\r"}:
            return ""

        self._mode = "visible"
        buffered = self._buffer
        self._buffer = ""
        return self._scan_visible(buffered)

    def finish(self) -> str:
        if self._finished:
            return ""
        self._finished = True
        if self._mode == "pending":
            self._mode = "visible"
            buffered = self._buffer
            self._buffer = ""
            emitted = self._scan_visible(buffered)
            if self._control_candidate == TOOL_CALL_OPENER:
                self._mode = "suppressed"
                self._control_candidate = ""
                return ""
            emitted += self._control_candidate
            self._control_candidate = ""
            return emitted
        if self._mode == "visible" and self._control_candidate:
            if self._control_candidate == TOOL_CALL_OPENER:
                self._mode = "suppressed"
                self._control_candidate = ""
                return ""
            buffered = self._control_candidate
            self._control_candidate = ""
            return buffered
        self._buffer = ""
        return ""


__all__ = [
    "LEGACY_OPENERS",
    "NaturalText",
    "SWITCH_AGENT_OPENER",
    "StreamControlGate",
    "SwitchAgentCommand",
    "TOOL_CALL_CLOSER",
    "TOOL_CALL_OPENER",
    "TextProtocolResult",
    "TextToolCall",
    "TextToolProtocolError",
    "ToolCallSequence",
    "format_switch_agent",
    "format_tool_call_sequence",
    "has_malformed_control_attempt",
    "parse_text_protocol",
    "reconstruct_split_fenced_tool_call",
    "reject_legacy_opener",
    "starts_new_control_opener",
    "starts_textual_tool_call_attempt",
]
