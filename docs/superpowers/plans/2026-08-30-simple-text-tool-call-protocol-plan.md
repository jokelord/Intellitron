# Simple Text Tool-Call Protocol Implementation Plan

> **2026-09-11 authoritative migration:** executable controls now use exact
> double-angle physical-line delimiters and strict `{"name": ..., "arguments": {...}}`
> call objects. The formatter emits one shared frame per model-turn batch; the
> parser also accepts repeated frames. Old triple-angle and XML/HTML-like output
> is inert NaturalText. Compact history pairing may recognize retired triple
> traces read-only, while compact summary validation rejects both generations.
> These rules supersede conflicting historical steps and examples below.
> The remaining unchecked task list is retained only as an implementation audit;
> it is not an instruction to restore the older grammar or XML rejection rules.

> **For agentic workers:** REQUIRED SUB-SKILLS: Use `superpowers:subagent-driven-development` and `superpowers:test-driven-development`. Reuse the existing context-bearing implementation subagent, execute this plan strictly task-by-task, and complete each red/green test cycle before moving to the next task.

**Goal:** Replace every legacy textual tool-call and routing parser with the fail-closed `<<TOOL_CALL>> ... <<END_TOOL_CALL>>` / `<<SWITCH_AGENT>>` protocol while preserving provider-native calls, Agent execution semantics, canonical memory traces, streaming safety, and prefix-cache behavior.

**Architecture:** A new side-effect-free `backend/agent/text_tool_protocol.py` module owns framing, strict JSON, formatting, reserved-prefix gating, and the permanent legacy-opener rejection firewall. `LocalLLMModel` deterministically selects exactly one source—provider-native structured calls or local text protocol—and adapts accepted calls into existing smolagents objects; `SmartToolCallingAgent` and routing consume those typed results without changing orchestrator, timeline, SSE, or frontend contracts.

**Tech Stack:** Python 3, dataclasses, standard-library `json`/`re`, smolagents `ChatMessageToolCall`, FastAPI backend tests with pytest/pytest-asyncio, YAML/Jinja agent prompts.

**Spec:** `docs/2026-08-30-simple-text-tool-call-protocol-design.md`

## Global Constraints

- Do not run version-control commands; repository `AGENTS.md` overrides the planning skill's usual repository workflow.
- Do not modify dependency package source under `./.sglang`.
- Use `./.sglang/bin/python -m pytest` for every Python test command.
- Text tool calls are one or more strict call objects in a shared frame (preferred), or repeated complete frames. A prose prefix is allowed before an exact physical-line tool opener; never accept an outer array, trailing cross-line prose, or mixed routing/tool control.
- Prompt examples and canonical formatting use UTF-8, `ensure_ascii=False`, `indent=2`, and strict JSON; parser acceptance must not depend on indentation width.
- The exact legacy openers `Calling tools:` and `调用工具:` at the first non-whitespace position are permanently rejected before adapter selection; their payload is never parsed, extracted, repaired, or executed.
- Provider-native structured calls are authoritative when present. Ordinary accompanying output text is not text-parsed; native calls plus visible text beginning with a new reserved opener are ambiguous dual encoding and must be rejected atomically.
- Parser failures are fail-closed and atomic: all blocks validate before any call is handed to `SmartToolCallingAgent.process_tool_calls()`.
- Parse only provider-classified visible content. Never parse `reasoning_content`.
- Preserve `ActionStep.tool_calls`, assistant tool-call → user observation ordering, call IDs, and canonical historical roles/content/order. Never derive model input from UI/timeline state.
- The prompt/schema migration is one intentional prefix boundary. Do not rewrite earlier canonical history; after the boundary, resume append-only stability immediately.
- Preserve compact cache-first prompt shape, Answer Now append-only correction semantics, SGLang/vLLM prompt equivalence, and all Stop/abort lifecycle behavior.
- Do not add stop sequences for either new opener.
- Do not change frontend files, SSE/timeline event names, event payload schemas, or orchestration stage/blackboard semantics.

---

## File and Responsibility Map

- **Create `backend/agent/text_tool_protocol.py`:** protocol constants, immutable result types, strict parser, formatter, prefix predicates, permanent legacy firewall, and incremental visible-content gate.
- **Create `tests/test_text_tool_protocol.py`:** exhaustive protocol grammar, strict JSON, collision, formatting, firewall, and stream-gate unit tests.
- **Create `tests/test_local_llm_tool_protocol.py`:** deterministic native/text adapter selection, dual-encoding rejection, batch schema validation, call-ID generation, and streaming adapter tests.
- **Modify `backend/agent/local_llm_model.py`:** use the new module; keep a separate strict provider-native adapter; delete all legacy textual extract/repair paths; gate visible streaming deltas; retain raw visible content in request-scoped stream state.
- **Modify `backend/agent/agent_runner.py`:** consume typed text results, remove old stripping/scanning logic and obsolete stop sequences, serialize memory with the new formatter, route switch results, and enforce Answer Now buffering/rejection.
- **Modify `config/config-agent-prompts.yaml`:** migrate coordinator, manager, basic/VLM instructions and examples to the new markers with two-space JSON indentation.
- **Modify `backend/compact/validation.py`:** reject new executable markers in compact summaries.
- **Modify `backend/compact/source_budget.py`:** recognize new canonical tool-call traces while retaining a narrowly documented historical-trace check for old already-sent memory; this check never parses or executes legacy output.
- **Modify tests:** `tests/test_multi_tool_parsing.py`, `tests/test_manager_agent_dispatch.py`, `tests/test_multi_agent_runner.py`, `tests/test_multi_agent_config.py`, `tests/test_agent_session_context.py`, `tests/test_local_llm_compactable_payload.py`, `tests/test_compact_validation.py`, `tests/test_compact_agent_cache_first.py`, `tests/test_compact_source_budget.py`, `tests/test_stream_tools_cycle.py`, and `tests/test_write_memory_tool_calls.py`.
- **Delete `tests/test_strip_tool_call_text.py`:** it exclusively tests legacy extraction/repair behavior that must no longer exist; equivalent new memory/stream safety is covered by the new focused tests.
- **No planned business-logic change:** `backend/agent/orchestrator.py`, frontend code, and SSE/timeline schemas.

---

### Task 1: Build the Strict Protocol Core

**Files:**
- Create: `backend/agent/text_tool_protocol.py`
- Create: `tests/test_text_tool_protocol.py`

**Interfaces:**
- Produces constants `TOOL_CALL_OPENER`, `TOOL_CALL_CLOSER`, `SWITCH_AGENT_OPENER`, and `LEGACY_OPENERS`.
- Produces `TextToolCall(name: str, arguments: dict[str, Any])`.
- Produces `NaturalText(text: str)`, `ToolCallSequence(calls: tuple[TextToolCall, ...])`, and `SwitchAgentCommand(target_agent: str, raw_payload: str)`.
- Produces `TextProtocolResult = NaturalText | ToolCallSequence | SwitchAgentCommand`.
- Produces `TextToolProtocolError(code: str, message: str, *, block_index: int | None = None, line: int | None = None, column: int | None = None, duplicate_key: str | None = None)`.
- Produces `parse_text_protocol(content: str | None, *, allowed_tool_names: AbstractSet[str] | None = None, allowed_agent_names: AbstractSet[str] | None = None) -> TextProtocolResult`.
- Produces `format_tool_call_sequence(calls: Sequence[TextToolCall]) -> str` and `format_switch_agent(target_agent: str) -> str`.
- Produces `starts_new_control_opener(content: str | None) -> bool`, `reject_legacy_opener(content: str | None) -> None`, and `StreamControlGate.push(delta: str) -> str` / `StreamControlGate.finish() -> str`.

- [ ] **Step 1: Add red tests for valid tool blocks, routing, formatting, collisions, and strict JSON**

Use concrete tests shaped like:

```python
from backend.agent.text_tool_protocol import (
    NaturalText,
    StreamControlGate,
    SwitchAgentCommand,
    TextToolCall,
    TextToolProtocolError,
    ToolCallSequence,
    format_tool_call_sequence,
    parse_text_protocol,
)
import pytest


def test_multiple_blocks_preserve_order_and_complex_symbols():
    content = """<<TOOL_CALL>>
{
  "name": "dispatching_parallel_agent",
  "arguments": {
  \"child_id\": \"reader_a\",
  \"task\": \"Analyze :' ; [ ] { } < > \\\\ | and \\\"quoted\\\" JSON.\"
}
}
<<END_TOOL_CALL>>

<<TOOL_CALL>>
{
  "name": "read_file",
  "arguments": {
  \"file_path\": \"/tmp/example.json\"
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
    assert result.calls[0].arguments["task"].endswith('| and "quoted" JSON.')


def test_fenced_example_is_natural_text():
    fence = "`" * 3
    content = f"""{fence}text
<<TOOL_CALL>>
{
  "name": "read_file",
  "arguments": {
  \"file_path\": \"/tmp/example.json\"
}
}
<<END_TOOL_CALL>>
{fence}"""
    assert parse_text_protocol(content) == NaturalText(content)


def test_second_invalid_block_rejects_the_whole_sequence():
    content = """<<TOOL_CALL>>
{
  "name": "read_file",
  "arguments": {
  \"file_path\": \"/tmp/a\"
}
}
<<END_TOOL_CALL>>
<<TOOL_CALL>>
{
  "name": "read_file",
  "arguments": {
  \"file_path\": \"/tmp/b\",
}
}
<<END_TOOL_CALL>>"""
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(content, allowed_tool_names={"read_file"})
    assert exc_info.value.code == "invalid_json"
    assert exc_info.value.block_index == 2
    assert exc_info.value.line is not None
    assert "No tool was executed." in str(exc_info.value)
```

Also test LF/CRLF, leading/trailing whitespace, nested objects/arrays, Unicode, escaped newlines, closer text inside a JSON string, one-line strict JSON acceptance, and exact two-space formatter output.

- [ ] **Step 2: Add red tests for fail-closed malformed inputs**

Parameterize top-level arrays/scalars/null, duplicate keys at both top and nested levels, `NaN`, `Infinity`, `-Infinity`, trailing commas, comments, single quotes, raw control characters, missing closer, malformed opener line, extra prose, trailing garbage, mixed `SWITCH_AGENT`/tool blocks, unknown tool, and unsupported route target. Assert error code, one-based block index where applicable, decoder line/column where available, and absence of parsed calls.

- [ ] **Step 3: Add red tests for the permanent firewall and incremental gate**

```python
@pytest.mark.parametrize("opener", ["Calling tools:", "调用工具:"])
def test_exact_legacy_opener_is_permanently_rejected(opener):
    with pytest.raises(TextToolProtocolError) as exc_info:
        parse_text_protocol(f"  {opener}\n[{{}}]")
    assert exc_info.value.code == "unsupported_legacy_protocol"


def test_stream_gate_hides_split_reserved_control_but_releases_false_alarm():
    gate = StreamControlGate()
    assert gate.push("  <<<TOOL_") == ""
    assert gate.push("CALL>>> read_file\n") == ""
    assert gate.push("{}\n<<END_TOOL_CALL>>") == ""
    assert gate.finish() == ""

    visible_gate = StreamControlGate()
    assert visible_gate.push("  <<<TOOL_") == ""
    assert visible_gate.push("GUIDE>>> is documentation") == (
        "  <<<TOOL_GUIDE>>> is documentation"
    )
```

Add cases proving legacy/new markers in prose or fenced blocks remain visible because they are not at the first non-whitespace position.

- [ ] **Step 4: Run the new test module and confirm red**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_text_tool_protocol.py -q
```

Expected: collection/import failure because `backend.agent.text_tool_protocol` does not exist.

- [ ] **Step 5: Implement immutable results, strict decoder, grammar, formatter, and gate**

Use this public shape and keep parsing side-effect-free:

```python
TOOL_CALL_OPENER = "<<TOOL_CALL>>"
TOOL_CALL_CLOSER = "<<END_TOOL_CALL>>"
SWITCH_AGENT_OPENER = "<<SWITCH_AGENT>>"
LEGACY_OPENERS = ("Calling tools:", "调用工具:")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class TextToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolCallSequence:
    calls: tuple[TextToolCall, ...]


@dataclass(frozen=True)
class SwitchAgentCommand:
    target_agent: str
    raw_payload: str
```

Implement strict JSON with an `object_pairs_hook` that raises on every duplicate key and a `parse_constant` callback that rejects non-finite values. Normalize CRLF only for framing; do not rewrite the JSON body. Locate closers only as exact standalone lines, validate every block before returning a sequence, and format with:

```python
json.dumps(arguments, ensure_ascii=False, indent=2, allow_nan=False)
```

The gate must remain undecided while its buffered first non-whitespace text is a prefix of any new or legacy opener, become permanently suppressed when an opener is complete, and release the exact buffered bytes once the prefix diverges.

- [ ] **Step 6: Run protocol tests to green**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_text_tool_protocol.py -q
```

Expected: all tests pass.

---

### Task 2: Replace `LocalLLMModel` Parsing with Deterministic Adapters

**Files:**
- Modify: `backend/agent/local_llm_model.py`
- Create: `tests/test_local_llm_tool_protocol.py`
- Modify: `tests/test_local_llm_compactable_payload.py`
- Modify: `tests/test_multi_tool_parsing.py`

**Interfaces:**
- Consumes Task 1 protocol types/functions and `StreamControlGate`.
- Produces `AssistantControlResolution(source: Literal["natural", "text", "native"], tool_calls: tuple[ChatMessageToolCall, ...] = (), switch_agent: SwitchAgentCommand | None = None)` in `local_llm_model.py`.
- Produces `LocalLLMModel._resolve_assistant_control(content: str, provider_tool_calls: Sequence[dict | ChatMessageToolCall] | None, tools_to_call_from: Sequence[Any] | None, *, allowed_agent_names: AbstractSet[str] | None = None) -> AssistantControlResolution`.
- Produces `LocalLLMModel._adapt_text_calls(calls: Sequence[TextToolCall]) -> tuple[ChatMessageToolCall, ...]`.
- Produces `LocalLLMModel._validate_tool_call_batch(calls: Sequence[ChatMessageToolCall], tools_to_call_from: Sequence[Any]) -> None`.
- Retains a separate `LocalLLMModel._parse_raw_tool_calls(raw_tool_calls: Sequence[dict | ChatMessageToolCall]) -> list[ChatMessageToolCall] | None` for provider-native data only.
- Provider-native switch normalization, if needed by routing, is named `_normalize_native_switch_target(arguments: Any) -> str | None` and never parses assistant text.

- [ ] **Step 1: Add red adapter-selection and atomic-validation tests**

```python
from unittest.mock import MagicMock

import pytest

from backend.agent.local_llm_model import LocalLLMModel


class _ReadFileTool:
    name = "read_file"
    inputs = {
        "file_path": {
            "type": "string",
            "description": "Absolute file path.",
        }
    }


@pytest.fixture
def model():
    return LocalLLMModel(llm_service=MagicMock())


@pytest.fixture
def read_file_tool():
    return _ReadFileTool()


def test_native_calls_are_authoritative_over_ordinary_text(model, read_file_tool):
    native = [{
        "id": "provider_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"file_path":"/tmp/a"}'},
    }]
    resolved = model._resolve_assistant_control(
        "I will inspect the file.", native, [read_file_tool]
    )
    assert resolved.source == "native"
    assert [call.id for call in resolved.tool_calls] == ["provider_1"]


def test_native_plus_new_text_control_is_ambiguous_and_atomic(model, read_file_tool):
    native = [{
        "id": "provider_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"file_path":"/tmp/a"}'},
    }]
    text = """<<TOOL_CALL>>
{
  "name": "read_file",
  "arguments": {
  \"file_path\": \"/tmp/b\"
}
}
<<END_TOOL_CALL>>"""
    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control(text, native, [read_file_tool])
    assert exc_info.value.code == "ambiguous_dual_encoding"


def test_legacy_firewall_runs_before_native_selection(model, read_file_tool):
    native = [{
        "id": "provider_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"file_path":"/tmp/a"}'},
    }]
    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control("Calling tools:\n[]", native, [read_file_tool])
    assert exc_info.value.code == "unsupported_legacy_protocol"
```

Add tests for: no native calls → text parser; native plus ordinary JSON demo → native only; text call IDs are backend-generated and unique; native IDs are retained; unknown tool and one invalid argument object reject the entire batch before any execution; malformed native argument JSON is an adapter error rather than `{"raw": ...}`; `final_answer` remains filtered.

- [ ] **Step 2: Add red streaming tests for raw-state preservation and UI suppression**

Use a fake `LLMService.chat()` stream whose content chunks split the opener. Assert yielded non-reasoning deltas contain no protocol bytes, while `model.get_stream_state(request_id)["content"]` contains the exact raw control response. Add a false-prefix test that yields all original characters in order, and a reasoning-marker test proving reasoning still streams but is never considered visible control content.

- [ ] **Step 3: Run the focused tests and confirm red**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_local_llm_tool_protocol.py tests/test_local_llm_compactable_payload.py -q
```

Expected: failures reference missing `_resolve_assistant_control` and ungated control deltas.

- [ ] **Step 4: Implement deterministic source selection and batch validation**

Implement this exact ordering:

```python
@dataclass(frozen=True)
class AssistantControlResolution:
    source: Literal["natural", "text", "native"]
    tool_calls: tuple[ChatMessageToolCall, ...] = ()
    switch_agent: SwitchAgentCommand | None = None


def _resolve_assistant_control(
    self,
    content,
    provider_tool_calls,
    tools_to_call_from,
    *,
    allowed_agent_names=None,
):
    reject_legacy_opener(content)
    if provider_tool_calls:
        if starts_new_control_opener(content):
            raise TextToolProtocolError(
                "ambiguous_dual_encoding",
                "Provider-native calls and local text control cannot coexist. No tool was executed.",
            )
        calls = tuple(self._parse_raw_tool_calls(provider_tool_calls) or ())
        source = "native"
        switch_agent = None
    else:
        allowed = {tool.name for tool in tools_to_call_from or []}
        parsed = parse_text_protocol(
            content,
            allowed_tool_names=allowed,
            allowed_agent_names=allowed_agent_names,
        )
        calls = self._adapt_text_calls(parsed.calls) if isinstance(parsed, ToolCallSequence) else ()
        switch_agent = parsed if isinstance(parsed, SwitchAgentCommand) else None
        source = "text" if calls or switch_agent is not None else "natural"
    self._validate_tool_call_batch(calls, tools_to_call_from or [])
    visible_calls = tuple(call for call in calls if call.function.name != "final_answer")
    return AssistantControlResolution(source, visible_calls, switch_agent)
```

When `allowed_agent_names` is supplied for a routing context, convert exactly one provider-native `switch_agent` call into `resolution.switch_agent` via `_normalize_native_switch_target()` and validate its target against that set. A provider-native mixed batch containing `switch_agent` plus another call is an atomic protocol error. Outside routing contexts, provider-native calls remain ordinary tool calls and follow the registered-tool validation path.

Batch validation must run before returning any call. Validate name membership, arguments-as-object, required/unexpected keys, and JSON-level values against each registered tool's `inputs` metadata (`string`, `integer` excluding `bool`, `number` excluding `bool`, `boolean`, `array`, `object`, `null`, and `any`; honor `nullable` and `default`). Business-specific validation such as child ID syntax remains in existing manager preflight.

Use an opaque backend ID factory with turn-local ordering, such as `call_text_<uuid hex>_<index>`. Tests may assert uniqueness/order, not a numeric base.

- [ ] **Step 5: Gate visible content in `generate_stream()` without changing raw stream state**

Instantiate one `StreamControlGate` per `generate_stream()` call. Continue appending provider `content` to `stream_content` and request-scoped diagnostics exactly as today, but yield only `gate.push(content)`; at normal completion yield `gate.finish()` if non-empty. Reasoning deltas bypass this gate and remain marked `delta.reasoning = True`. Apply the same gate to empty-stream non-streaming fallback text.

Update `generate()` and empty-stream fallback to call `_resolve_assistant_control()` with both provider-native calls and `tools_to_call_from`; place only `resolution.tool_calls` on the returned/delta `ChatMessage`. Do not compare or merge text-parsed and native calls.

- [ ] **Step 6: Delete all legacy textual parsing and repair code**

Remove `_parse_tool_calls_from_text`, `_parse_switch_agent_command`, both `_split_visible_text...` functions, `detect_malformed_tool_transcript`, all transcript normalization/repair/balanced-array helpers listed in the spec, and imports used only by them (`ast` or aliases). Replace `_remember_compactable_payload()` detection with a protocol classification that clears/skips compactable payload for new reserved control, provider calls, legacy rejection, or parsing error, while preserving normal text behavior.

Delete old-format success/repair cases from `tests/test_multi_tool_parsing.py`; retain and convert the valuable reasoning-vs-visible-content and `_step_stream` malformed-error tests to the new protocol.

- [ ] **Step 7: Run adapter and parser integration tests to green**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_text_tool_protocol.py tests/test_local_llm_tool_protocol.py tests/test_local_llm_compactable_payload.py tests/test_multi_tool_parsing.py -q
```

Expected: all tests pass and no test expects successful parsing of XML, action wrappers, bare JSON, outer arrays, Python literals, or repaired JSON.

---

### Task 3: Integrate Tool Execution, Canonical Memory, and Answer Now

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `tests/test_agent_session_context.py`
- Modify: `tests/test_write_memory_tool_calls.py`
- Modify: `tests/test_stream_tools_cycle.py`
- Delete: `tests/test_strip_tool_call_text.py`

**Interfaces:**
- Consumes `parse_text_protocol()`, `format_tool_call_sequence()`, `TextToolCall`, `ToolCallSequence`, `SwitchAgentCommand`, and `TextToolProtocolError`.
- `SmartToolCallingAgent._step_stream()` continues yielding existing smolagents deltas, `ActionOutput`, orchestration events, and `SecondaryHandoffRequest`; no new public event type is introduced.
- `write_memory_to_messages()` continues returning `list[ChatMessage]` with assistant tool-call content followed by user-role observations.

- [ ] **Step 1: Add red `_step_stream` tests for atomic parsing and no raw-protocol leakage**

Create streaming candidates where: one valid call executes; two valid calls reach `process_tool_calls()` in order; the second block is malformed and `process_tool_calls()` is never called; the opener is split across deltas; a marker appears only in reasoning; and a natural fenced example remains the final answer. Assert malformed errors become `AgentParsingError` observations with `No tool was executed.`.

Also assert `_step_stream()` passes no new marker as a stop sequence:

```python
assert "<<TOOL_CALL>>" not in captured_stop_sequences
assert "<<SWITCH_AGENT>>" not in captured_stop_sequences
assert "Calling tools:" not in captured_stop_sequences
```

- [ ] **Step 2: Add red memory serialization tests**

Use an `ActionStep` with two `MemoryToolCall` entries and an observation. Assert:

```python
assert messages[tool_index].role == MessageRole.TOOL_CALL
assert messages[tool_index].content[0]["text"].startswith(
    "<<TOOL_CALL>>\n{\n  \"name\": \"read_file\",\n  \"arguments\":"
)
assert '\n\n{\n  "name": "grep",\n  "arguments":' in messages[tool_index].content[0]["text"]
assert messages[tool_index + 1].role == MessageRole.TOOL_RESPONSE
```

Assert `ActionStep.tool_calls` remains populated, generated IDs match observation/event linkage, no `Calling tools:` transcript is emitted for new steps, and normal assistant final text is unchanged.

- [ ] **Step 3: Add red Answer Now tests for raw buffered control and dual encoding**

Update the existing Answer Now tests to supply new framed blocks. Add a case where the stream gate suppresses all visible control deltas but request-scoped raw content begins with `<<TOOL_CALL>>`; Answer Now must still reject it, execute nothing, append one corrective user tail, and never replay marker/JSON to SSE. Add native+new-opener dual encoding and second-violation cases.

- [ ] **Step 4: Run the focused tests and confirm red**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_agent_session_context.py tests/test_write_memory_tool_calls.py tests/test_stream_tools_cycle.py -q
```

Expected: failures show old parser/stripper calls, old memory transcript, and incomplete Answer Now detection.

- [ ] **Step 5: Replace `_step_stream` parsing and delete text stripping**

After stream completion, pass only `tool_parse_content` from request-scoped visible content plus any provider `chat_message.tool_calls` to `_resolve_assistant_control()`. Raise `AgentParsingError(str(exc), self.logger)` on `TextToolProtocolError`. Use `resolution.tool_calls` for normal execution; if `resolution.switch_agent` is set, clear raw `model_output` and yield `SecondaryHandoffRequest(target_agent=resolution.switch_agent.target_agent)` instead of treating marker text as a final answer.

Because valid control mode contains no prose, set `memory_step.model_output = None` and `memory_step.model_output_message.content = None` after accepted tool calls. Delete `_strip_tool_call_text`, `_RE_XML_TOOL_CALL`, `_extract_balanced_array`, `_extract_balanced_object`, and all callers; do not replace them with another body scanner.

- [ ] **Step 6: Serialize canonical memory with the new formatter**

Replace `_tool_call_transcript_text()` with a formatter bridge:

```python
def _tool_call_protocol_text(tool_calls) -> str | None:
    if not tool_calls:
        return None
    calls = tuple(
        TextToolCall(
            name=tool_call.name,
            arguments=_normalize_tool_call_arguments(tool_call.arguments),
        )
        for tool_call in tool_calls
    )
    return format_tool_call_sequence(calls)
```

Keep the surrounding `MessageRole.TOOL_CALL` and `MessageRole.TOOL_RESPONSE` structure unchanged. Never remove `ActionStep.tool_calls` and never rewrite observations.

- [ ] **Step 7: Make Answer Now validate the complete raw visible candidate**

At the end of each buffered attempt, read `runtime_agent.model.get_stream_state(request_id)["content"]` for protocol validation; do not infer safety from yielded deltas because the stream gate intentionally hides control responses. A provider `tool_calls` delta, permanent legacy-firewall error, new tool/switch result, malformed reserved control, or dual encoding is a violation. Preserve the existing maximum of one append-only corrective user tail and never execute `process_tool_calls()`.

- [ ] **Step 8: Run Agent/memory/Answer Now tests to green**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_agent_session_context.py tests/test_write_memory_tool_calls.py tests/test_stream_tools_cycle.py -q
```

Expected: all tests pass; raw protocol bytes never appear in content events; action/observation memory pairing remains intact.

---

### Task 4: Migrate Routing and Agent Prompts

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `config/config-agent-prompts.yaml`
- Modify: `tests/test_multi_agent_runner.py`
- Modify: `tests/test_multi_agent_config.py`

**Interfaces:**
- Consumes `parse_text_protocol(content, allowed_agent_names=...)` and native-only `_normalize_native_switch_target()`.
- Keeps `_classify_coordinator_output(chat_message, *, parse_tool_text: bool = True) -> dict` as the routing boundary so callers and SSE projections do not change.
- Keeps `SecondaryHandoffRequest` and existing routing/dispatch timeline events unchanged.

- [ ] **Step 1: Add red coordinator classification tests**

Replace shorthand/canonical-array route fixtures with:

```python
content = "<<SWITCH_AGENT>> coordinator_2"
route = runner._classify_coordinator_output(
    ChatMessage(role=MessageRole.ASSISTANT, content=content)
)
assert route == {"kind": "switch", "target_agent": "coordinator_2"}
```

Add cases for unsupported targets, extra prose, multiple lines, tool blocks in a coordinator route, permanent legacy opener rejection, native structured switch plus ordinary text (native authoritative), and native structured switch plus a new opener (ambiguous, invalid, zero route execution).

- [ ] **Step 2: Add red streaming routing and secondary-handoff tests**

Convert existing split-array/suffix tests to split `<<SWITCH_AGENT>> basic_agent` chunks. Assert the marker never appears in `routing_delta`, chat bubbles, legacy content events, or final answers, while existing `routing_decision`, `secondary_handoff`, dispatch ordering, and `agent_switched` behavior remain unchanged. Keep LaTeX/braces/prose tests and assert they remain visible natural text.

- [ ] **Step 3: Add red prompt contract tests**

In `tests/test_multi_agent_config.py`, require:

```python
assert "<<SWITCH_AGENT>> coordinator_2" in coordinator_1_routing_prompt
assert "<<SWITCH_AGENT>> basic_agent" in coordinator_1_routing_prompt
assert "<<TOOL_CALL>>" in manager_system_prompt
assert "<<END_TOOL_CALL>>" in manager_system_prompt
assert "Calling tools:" not in active_prompt_text
assert "<tool_call>" not in active_prompt_text
```

Parse every fenced/example JSON body with `json.loads`, then assert `json.dumps(value, ensure_ascii=False, indent=2)` matches the documented two-space structure. Require explicit prompt rules for whole-response control mode, no prose/fences around real calls, multiple consecutive blocks, standard JSON escaping, and fenced display examples.

- [ ] **Step 4: Run routing/prompt tests and confirm red**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_multi_agent_runner.py tests/test_multi_agent_config.py -q
```

Expected: old JSON route fixtures and old prompt contracts fail.

- [ ] **Step 5: Replace route parsing and remove streaming suffix scanners**

In `_classify_coordinator_output()`, call `_resolve_assistant_control(content, chat_message.tool_calls, (), allowed_agent_names=set(self.config.agent.enabled_agent_profiles))`. Accept `resolution.switch_agent`, reject any `resolution.tool_calls`, and return direct answer only when `resolution.source == "natural"` and visible text is non-empty. The resolver runs the permanent legacy firewall first, makes native calls authoritative, and rejects dual encoding before classification.

Always read coordinator request-scoped raw `content` even when the visible-delta list is empty, because a valid switch command is suppressed by `StreamControlGate`. Delete `pending_control_text`, bracket balancing, JSON suffix splitting, and reasoning-only route parsing. Reasoning-only switch text must no longer route.

For coordinator_2, consume the `SecondaryHandoffRequest` emitted by `_step_stream()` and delete `_consume_trailing_switch_control`, `pending_switch_control_suffix`, and old JSON partial-buffer heuristics. Preserve current timeline/projection ordering.

- [ ] **Step 6: Rewrite all active prompt instructions and examples**

Use exact route lines:

```text
<<SWITCH_AGENT>> coordinator_2
<<SWITCH_AGENT>> basic_agent
```

Use tool examples with consecutive blocks and two-space indentation:

```text
<<TOOL_CALL>>
{
  "name": "dispatching_parallel_agent",
  "arguments": {
  "child_id": "reader_a",
  "task": "Read source A and return an evidence-based report."
}
}
<<END_TOOL_CALL>>

<<TOOL_CALL>>
{
  "name": "dispatching_parallel_agent",
  "arguments": {
  "child_id": "reader_b",
  "task": "Read source B and return an evidence-based report."
}
}
<<END_TOOL_CALL>>
```

Remove all statements that the system still accepts legacy formats. Keep tool descriptions, independent-same-turn constraints, child briefing rules, VLM identity instructions, and final-answer-as-natural-text rules unchanged.

- [ ] **Step 7: Run routing/prompt tests to green**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_multi_agent_runner.py tests/test_multi_agent_config.py -q
```

Expected: all tests pass with unchanged event payload assertions.

---

### Task 5: Preserve Compact and Prefix-Cache Contracts

**Files:**
- Modify: `backend/compact/validation.py`
- Modify: `backend/compact/source_budget.py`
- Modify: `tests/test_compact_validation.py`
- Modify: `tests/test_compact_agent_cache_first.py`
- Modify: `tests/test_compact_source_budget.py`
- Modify: compact/Answer Now fixtures in other tests only where they embed legacy tool transcripts

**Interfaces:**
- `validate_compact_summary(cleaned_summary: str, prompt_chars: int, config: CompactConfig) -> CompactValidationResult` keeps its signature.
- `_is_tool_call_message(message: Message) -> bool` remains a trace-pairing predicate only; it never parses or executes content.

- [ ] **Step 1: Add red compact validation and cache-first preservation tests**

```python
@pytest.mark.parametrize(
    "summary",
    [
        '<<TOOL_CALL>>\n{"name":"read_file","arguments":{}}\n<<END_TOOL_CALL>>',
        "<<SWITCH_AGENT>> basic_agent",
    ],
)
def test_validate_rejects_new_control_markers(summary):
    result = validate_compact_summary(summary, prompt_chars=10000, config=_cfg())
    assert result.ok is False
    assert result.reason == "routing_or_tool_output"
```

Update cache-first fixtures to new formatted assistant tool traces and assert the system/runtime prefix, tool-call content, observation role/content, and message order remain byte-for-byte unchanged. Add a source-budget test showing an already-historical `Calling tools:` trace remains paired with its observation without becoming executable.

- [ ] **Step 2: Run compact tests and confirm red**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_compact_validation.py tests/test_compact_agent_cache_first.py tests/test_compact_source_budget.py -q
```

Expected: new marker validation and new trace recognition fail before implementation.

- [ ] **Step 3: Add new anti-leak markers and update trace pairing**

Add both new openers and the tool closer to `ROUTING_OR_TOOL_MARKERS`. Update `_is_tool_call_message()` to recognize assistant content beginning with `<<TOOL_CALL>>`; retain recognition of `Calling tools:` solely for immutable historical trace pairing, with a comment stating that this branch does not parse, repair, or execute legacy output.

Do not change compact prompt shape, source-budget eligibility, fallback gating, validation thresholds, thinking settings, or summary token/latency configuration.

- [ ] **Step 4: Run compact tests to green**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_compact_validation.py tests/test_compact_agent_cache_first.py tests/test_compact_source_budget.py tests/test_compact_agent_system_override.py tests/test_compact_2nd_compact_migrate_problem_solving.py -q
```

Expected: all tests pass and cache-first source preserves canonical history exactly.

---

### Task 6: Prove the Full Manager Dispatch Path and Remove Legacy Test Debt

**Files:**
- Modify: `tests/test_manager_agent_dispatch.py`
- Modify: `tests/test_multi_tool_parsing.py`
- Modify: `tests/test_multi_agent_runner.py`
- Modify: `tests/test_agent_session_context.py`
- Modify: `tests/test_local_llm_compactable_payload.py`
- Modify: `tests/test_stream_tools_cycle.py`
- Modify: `tests/test_write_memory_tool_calls.py`
- Delete: `tests/test_strip_tool_call_text.py`
- Review only unless a failing contract requires scoped correction: `backend/agent/orchestrator.py`

**Interfaces:**
- Consumes LocalLLM text adapter output as existing `ChatMessageToolCall` objects.
- Preserves `SmartToolCallingAgent.process_tool_calls()` and `ToolOrchestrator` one-turn/one-stage behavior.

- [ ] **Step 1: Rewrite the production-path manager dispatch regression**

Replace `test_parser_to_process_tool_calls_produces_one_ordered_shared_blackboard` input with consecutive blocks:

```python
content = """<<TOOL_CALL>>
{
  "name": "dispatching_parallel_agent",
  "arguments": {
  \"child_id\": \"poet_a\",
  \"task\": \"Compose poem A with a self-contained detailed briefing.\"
}
}
<<END_TOOL_CALL>>

<<TOOL_CALL>>
{
  "name": "dispatching_parallel_agent",
  "arguments": {
  \"child_id\": \"poet_b\",
  \"task\": \"Compose poem B with a self-contained detailed briefing.\"
}
}
<<END_TOOL_CALL>>"""
resolution = runner.model._resolve_assistant_control(
    content,
    None,
    agent.tools_and_managed_agents,
)
calls = list(resolution.tool_calls)
```

Continue through the real `SmartToolCallingAgent.process_tool_calls()` path. Assert one parallel stage, one dispatch ID, ordered call/child blackboard entries, two retained `MemoryToolCall`s, and unchanged orchestration event payloads. Do not reduce this to a mocked private child coroutine parser test.

- [ ] **Step 2: Add zero-execution atomicity regressions at the execution boundary**

Add cases where block 2 has invalid JSON, an unknown tool, wrong top-level arguments, duplicate `child_id`, or extra dispatch arguments. Parser/schema errors must execute nothing. Existing manager business preflight behavior remains as specified: duplicate child IDs prevent any child launch, while independent ordinary tools follow the existing orchestrator contract only after the whole parser/schema batch is accepted.

- [ ] **Step 3: Remove or convert every legacy-success test and fixture**

Delete tests whose asserted behavior is XML/action/bare/inline/array/Python-literal parsing or repair. Convert tests that protect reasoning separation, UI hiding, memory pairing, dispatch order, timeout, abort, session reuse, or compact behavior to the new markers. Delete `tests/test_strip_tool_call_text.py` after its replacement contracts in Tasks 1–3 are green.

Use narrowly allowlisted static scans. Runtime occurrences of legacy openers are permitted only in:

- `text_tool_protocol.py` permanent rejection constants/error path;
- `compact/source_budget.py` immutable historical trace pairing;
- negative tests proving rejection or historical preservation.

No prompt, success fixture, parser helper, repair helper, or stop sequence may remain.

- [ ] **Step 4: Run manager and converted regression suites**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_manager_agent_dispatch.py tests/test_multi_tool_parsing.py tests/test_multi_agent_runner.py tests/test_agent_session_context.py tests/test_local_llm_compactable_payload.py tests/test_stream_tools_cycle.py tests/test_write_memory_tool_calls.py -q
```

Expected: all tests pass; manager concurrency, lifecycle, blackboard, memory, and event contracts are unchanged.

- [ ] **Step 5: Run static legacy/dead-code scans and inspect every allowed hit**

Run:

```bash
rg -n "_parse_tool_calls_from_text|detect_malformed_tool_transcript|_repair_tool_transcript_text|_try_parse_malformed_write_file_transcript|_split_visible_text_around_switch_agent_command|_split_visible_text_and_trailing_switch_agent_command|ast\.literal_eval" backend tests config
rg -n "Calling tools:|调用工具:|<tool_call>|动作:" backend/agent backend/compact config/config-agent-prompts.yaml tests
```

Expected: first command returns no hits. Every second-command hit is one of the three explicit allowlist categories above; active prompt text, successful parsing fixtures, stop sequences, and old repair comments produce no hits.

---

### Task 7: Final Verification and Prefix-Boundary Audit

**Files:**
- Verify: all files listed above
- Verify unchanged: `frontend/`, `backend/agent/orchestrator.py` business semantics, SSE/timeline schemas

**Interfaces:**
- No new interface; this task verifies the integrated deliverable against the approved spec.

- [ ] **Step 1: Run syntax/import verification**

Run:

```bash
./.sglang/bin/python -m compileall -q backend/agent backend/compact
```

Expected: exit code 0 with no syntax/import errors.

- [ ] **Step 2: Run the complete targeted protocol suite**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_text_tool_protocol.py tests/test_local_llm_tool_protocol.py tests/test_multi_tool_parsing.py tests/test_manager_agent_dispatch.py tests/test_multi_agent_runner.py tests/test_multi_agent_config.py tests/test_agent_session_context.py tests/test_write_memory_tool_calls.py tests/test_stream_tools_cycle.py tests/test_local_llm_compactable_payload.py tests/test_compact_validation.py tests/test_compact_agent_cache_first.py tests/test_compact_source_budget.py tests/test_compact_agent_system_override.py tests/test_compact_2nd_compact_migrate_problem_solving.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run broader backend Agent/Compact regressions**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_agent_*.py tests/test_multi_*.py tests/test_manager_*.py tests/test_compact_*.py tests/test_stream_tools_cycle.py tests/test_write_memory_tool_calls.py -q
```

Expected: all selected backend tests pass. Any unrelated pre-existing failure must be reported with the exact test name and output; do not weaken a protocol assertion to make it pass.

- [ ] **Step 4: Audit prompt shape and migration boundary**

Confirm by focused tests/inspection that:

- all active agent profiles use only the new format and two-space JSON examples;
- no normal/Answer Now/compact request inserts dynamic protocol state into system or historical messages;
- the deployment prompt/schema change is treated as one intentional agent-profile/tool-schema boundary;
- previously sent role/content/order/tool observations are not rewritten;
- cache-first uses the same post-boundary stable prefix as the normal Agent call;
- SGLang and vLLM receive equivalent logical messages/tool schema;
- new openers are absent from stop sequences;
- frontend and SSE/timeline contracts are unchanged.

- [ ] **Step 5: Perform the final spec checklist**

Check each design acceptance criterion explicitly: no outer array; consecutive atomic blocks; strict JSON/no repair; permanent legacy rejection; deterministic native/text source selection; dual-encoding rejection; backend call IDs; routing line; no reasoning parsing; stream buffering/no leakage; canonical memory pairing; Compact and Answer Now safety; production-path manager dispatch; no frontend/SSE change. Record only concise pass/fail evidence in the task report, not per-section local record files.
