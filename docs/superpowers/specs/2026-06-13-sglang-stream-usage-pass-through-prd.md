# SGLang Stream Usage Pass-Through PRD

> Status: PRD only. This document defines future implementation requirements and does not implement them.
> Date: 2026-06-13
> Scope: SGLang streaming usage propagation, compact diagnostics, backend logging, tests.
> Non-scope for this PRD-writing task: no backend implementation, no frontend implementation, no SGLang server launch changes.

## 1. Problem Statement

`CompactOrchestrator` already has diagnostic fields for SGLang prompt and completion token counts, but in real streaming compact calls these fields can remain `None`. This makes it hard to determine whether compact latency is dominated by prefill, decode, prompt length, or summary length.

The missing data has two causes:

1. Compact does not request OpenAI/SGLang streamed usage from `LLMService.chat_stream_async()`.
2. The SGLang streaming parser currently drops top-level `usage` chunks even if SGLang returns them.

This PRD defines a future design for passing SGLang stream usage through to compact diagnostics without changing user-visible compact text or frontend protocol behavior.

## 2. Current Facts From Code

### 2.1 Compact orchestrator already consumes usage if present

- `backend/compact/orchestrator.py:142-147`: initializes `_diag_prompt_tokens_sglang` and `_diag_completion_tokens_sglang` to `None`.
- `backend/compact/orchestrator.py:195-202`: calls `self.llm_service.chat_stream_async(...)` for compact.
- `backend/compact/orchestrator.py:203-209`: content chunks become `compact_delta` events.
- `backend/compact/orchestrator.py:210-214`: if a chunk contains `usage`, orchestrator records `prompt_tokens` and `completion_tokens`.
- `backend/compact/orchestrator.py:254-264`: `[compact-diag]` logs `prompt_tokens_sglang`, `completion_tokens_sglang`, `prefill_ms`, `decode_ms`, `total_ms`, and `max_summary_tokens`.

The consumer side is already prepared for usage chunks. The lower layers do not reliably deliver them.

### 2.2 Tests prove orchestrator-side behavior exists

- `tests/test_compact_orchestrator_instrumentation.py:151-188`: successful compact logs diagnostics with token fields as `None` when no usage chunk arrives.
- `tests/test_compact_orchestrator_instrumentation.py:222-248`: if a fake LLM service yields a usage chunk, `[compact-diag]` records real prompt and completion token values.
- `tests/test_compact_orchestrator_instrumentation.py:256-278`: failure path does not emit `[compact-diag]`.

This means future implementation should focus on request opt-in and stream parser pass-through, not on redesigning `CompactOrchestrator` diagnostics from scratch.

### 2.3 LLMService async streaming does not expose extra_body

- `backend/llm_service.py:1860-1870`: `LLMService.chat_stream_async()` signature accepts messages, generation params, tools, request ID, runtime context, and slim flag, but not `extra_body`.
- `backend/llm_service.py:1883-1893`: `chat_stream_async()` calls `self.chat(..., stream=True, ...)` without any `extra_body` path.
- `backend/llm_service.py:1898-1920`: it forwards provider chunks through an async queue.

The lower synchronous `LLMService.chat()` path already supports `extra_body`.

### 2.4 SGLang provider can merge extra_body into request payload

- `backend/llm_service.py:414-426`: `SGLangProvider.generate()` builds the OpenAI-compatible request body with model, messages, temperature, top_p, max_new_tokens, and stream.
- `backend/llm_service.py:428-429`: `extra_body` is merged into `request_data`.
- `backend/llm_service.py:436-442`: request ID and converted tools are added afterward.

Therefore `{"stream_options": {"include_usage": true}}` can reach SGLang if `chat_stream_async()` exposes and forwards `extra_body`.

### 2.5 SGLang stream parser drops usage

- `backend/llm_service.py:716-827`: `SGLangProvider._stream_generate()` parses SSE response lines.
- `backend/llm_service.py:797-804`: parser extracts `choices[0].delta.content`, `reasoning_content`, and `tool_calls`.
- `backend/llm_service.py:805-811`: parser yields only chunks containing content, reasoning, or tool calls.

OpenAI-style final usage chunks often have `choices: []` and top-level `usage`. Those chunks are currently ignored.

### 2.6 Existing frontend projection must remain unchanged

- `frontend/src/lib/timeline-projection.ts:109-148`: compact timeline events are projected by kind.
- `frontend/src/lib/compact-projection.ts:161-174`: `compact_delta` appends text to compact summary content.
- `frontend/src/lib/compact-projection.ts:176-193`: `compact_completed` sets the final summary text and optional summary metadata.

Usage-only chunks must not become `compact_delta` text.

## 3. Product Goals

- G1: Compact diagnostics should receive real SGLang `prompt_tokens` and `completion_tokens` for streaming compact calls when SGLang exposes them.
- G2: Usage pass-through must not change user-visible compact summary text.
- G3: Usage pass-through must not create a new frontend protocol.
- G4: Normal chat/agent streams should remain unchanged unless explicitly configured to request usage.
- G5: If usage is unavailable or rejected by a backend, compact should still work and log `None` token fields.
- G6: Implementation should be backend-local and provider-aware.

## 4. Non-Goals

- N1: Do not implement this PRD in the current PRD-writing task.
- N2: Do not switch compact to non-streaming generation.
- N3: Do not globally enable SGLang usage for every stream via server launch flags in the first implementation.
- N4: Do not expose usage chunks as chat text, reasoning text, or compact summary text.
- N5: Do not require frontend changes for the minimal version.
- N6: Do not require SGLang source modifications.
- N7: Do not make usage mandatory for successful compact.

## 5. Recommended Future Design

### 5.1 Compact-only request opt-in

Compact should request usage only for compact LLM calls by default.

Conceptual request addition:

```python
extra_body={"stream_options": {"include_usage": True}}
```

The request should be passed only when the active backend is known to support OpenAI/SGLang-compatible stream usage. If backend detection is unavailable, the implementation may attempt the request and fall back without `extra_body` only on known unsupported-extra-body errors.

### 5.2 Add `extra_body` to `LLMService.chat_stream_async()`

Future signature:

```python
async def chat_stream_async(
    self,
    messages: List[Message],
    max_new_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    tools: Optional[List[dict]] = None,
    request_id: Optional[str] = None,
    runtime_context: Optional[str] = None,
    slim: bool = False,
    extra_body: Optional[dict] = None,
) -> AsyncGenerator[dict, None]:
    ...
```

`chat_stream_async()` should pass `extra_body` to `self.chat(..., stream=True, extra_body=extra_body)`.

### 5.3 Pass top-level usage through SGLangProvider stream parser

`SGLangProvider._stream_generate()` should inspect parsed SSE JSON for top-level `usage` independently of content deltas.

Expected yielded shapes:

Content chunk without usage:

```python
{"content": "text", "reasoning": None, "tool_calls": None}
```

Usage-only final chunk:

```python
{"content": None, "reasoning": None, "tool_calls": None, "usage": {"prompt_tokens": 123, "completion_tokens": 45}}
```

Content chunk with continuous usage:

```python
{"content": "text", "reasoning": None, "tool_calls": None, "usage": {"prompt_tokens": 123, "completion_tokens": 1}}
```

The parser must yield a chunk if either text-like data exists or `usage` exists.

### 5.4 Keep `CompactOrchestrator` event behavior unchanged

`CompactOrchestrator.run_compact()` should continue to:

- append content deltas to `accumulated`
- emit `compact_delta` only for real text deltas
- update diagnostic token fields when `chunk.get("usage")` exists
- emit `compact_completed` with the cleaned summary text

Usage-only chunks should not generate `compact_delta`.

### 5.5 Logging fields

Minimum log fields:

- `prompt_tokens_sglang`
- `completion_tokens_sglang`
- `prefill_ms`
- `decode_ms`
- `total_ms`
- `max_summary_tokens`

Optional future fields if SGLang exposes them:

- `total_tokens_sglang`
- `cached_tokens_sglang`
- `reasoning_tokens_sglang`
- `prompt_cache_hit_tokens`

Optional fields must be absent or `None` when not available. Do not fabricate token counts from local heuristics under SGLang field names.

## 6. Configuration

Add a compact-scoped config field in a future implementation:

```yaml
compact:
  request_stream_usage: true
```

Rules:

- Default should be `true` for local SGLang if tested and stable.
- If backend rejects `stream_options`, compact should retry once without usage request and log a warning.
- The retry must be scoped to compact usage opt-in; do not create a generic retry that hides unrelated request errors.
- Do not hardcode backend-specific behavior outside the LLM service/provider boundary unless there is an explicit config gate.

## 7. Alternatives Considered

### Option A: Compact-only usage opt-in plus generic pass-through

Pros:

- Minimal behavior change.
- Does not alter normal chat streams.
- Aligns with existing orchestrator diagnostics.
- No SGLang server restart needed.

Cons:

- Requires `chat_stream_async()` interface change.
- Fake LLM services in tests need `**kwargs` or an `extra_body` parameter.
- Unsupported backends need graceful fallback.

This is the recommended option.

### Option B: Enable SGLang server default include usage globally

Pros:

- Every stream can expose usage without per-request opt-in.

Cons:

- Requires SGLang launch/config change.
- Changes global stream behavior.
- Backend parser still must be fixed.
- Could leak unexpected usage chunks into normal chat paths unless carefully filtered.

Not recommended as the first implementation.

### Option C: Make compact non-streaming

Pros:

- Non-streaming `GenerationResult.usage` already exists for SGLang responses.

Cons:

- Removes compact streaming UX.
- Users see longer apparent stalls.
- Violates current compact event model.

Not recommended.

## 8. Error Handling

Future implementation must handle these cases:

- SGLang returns usage chunk: pass through and log real token fields.
- SGLang does not return usage chunk: compact succeeds and logs `None` token fields.
- Backend rejects `stream_options`: retry compact once without usage opt-in if and only if the error matches unsupported-extra-body/unsupported-stream-options patterns.
- Stream includes malformed usage object: ignore token values that are not integers, continue compact.
- Stream errors before content: existing `compact_failed` behavior remains.

## 9. Event And UI Contract

Compact SSE sequence remains:

```text
compact_started
compact_delta * N
compact_completed | compact_failed
```

Usage is not a new compact event kind. Usage-only chunks are backend diagnostics only.

If a future UI wants to display token statistics, it must use optional metadata on `compact_completed`, not `compact_delta` text. That future UI change requires its own spec or a clearly scoped extension to this PRD.

## 10. Acceptance Criteria For Future Implementation

### 10.1 Functional

- `CompactOrchestrator` logs real `prompt_tokens_sglang` and `completion_tokens_sglang` when SGLang emits usage.
- `CompactOrchestrator` continues to log `None` fields when usage is unavailable.
- Usage-only stream chunks do not produce `compact_delta`.
- Normal chat stream output remains unchanged when usage is not requested.
- Compact succeeds even if usage opt-in is unavailable and fallback succeeds.

### 10.2 Diagnostics

- Logs identify whether stream usage was requested.
- Logs identify whether usage request was accepted, unavailable, or retried without usage.
- Logs keep timing fields independent from token fields.

### 10.3 Compatibility

- Existing compact instrumentation tests continue to pass.
- Existing chat/agent SSE clients do not need changes.
- Existing frontend compact projection does not receive usage as content.

## 11. Required Tests For Future Implementation

### 11.1 Provider parser tests

Simulate SGLang SSE chunks:

```text
data: {"choices":[{"delta":{"content":"hello"}}]}
data: {"choices":[],"usage":{"prompt_tokens":123,"completion_tokens":4,"total_tokens":127}}
data: [DONE]
```

Assert `_stream_generate()` yields one content chunk and one usage chunk.

### 11.2 Continuous usage tests

Simulate content chunk with top-level usage:

```text
data: {"choices":[{"delta":{"content":"h"}}],"usage":{"prompt_tokens":123,"completion_tokens":1}}
```

Assert yielded chunk includes both `content` and `usage`.

### 11.3 LLMService pass-through tests

Patch `LLMService.chat()` or provider call to capture kwargs. Assert `chat_stream_async(..., extra_body={...})` passes `extra_body` unchanged to `chat()`.

### 11.4 Compact request opt-in tests

Fake LLM service captures `extra_body`. Assert compact passes `{"stream_options": {"include_usage": True}}` when `compact.request_stream_usage` is true.

### 11.5 Compact no-delta-for-usage tests

Fake LLM service yields:

```python
{"content": "<summary>OK</summary>"}
{"usage": {"prompt_tokens": 10, "completion_tokens": 2}}
```

Assert only the content chunk becomes `CompactEventKind.DELTA`, and usage affects only the diagnostic log.

### 11.6 Fallback tests

Fake provider raises an unsupported `stream_options` error on the first call and succeeds without `extra_body` on the second call. Assert compact succeeds, warning is logged, and no generic retry is introduced for unrelated errors.

### 11.7 Regression tests

Run existing compact instrumentation tests:

```bash
PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_compact_orchestrator_instrumentation.py -q
```

Run chat compact regression tests to ensure no stream protocol leakage:

```bash
PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_compact_chat_v2_path.py tests/test_compact_chat_rebuild_contract.py tests/test_compact_chat_auto_trigger.py -q
```

## 12. Rollout Strategy For Future Work

1. Add provider parser usage pass-through behind tests.
2. Add `extra_body` to `LLMService.chat_stream_async()` and pass it through.
3. Add compact config `request_stream_usage`.
4. Make compact request usage only when config is enabled.
5. Add unsupported-stream-options fallback scoped to compact usage opt-in.
6. Run compact E2E and verify `[compact-diag]` contains real SGLang token counts.
7. Use those diagnostics to evaluate prompt-shape prefix-cache work.

## 13. Explicit Anti-Patterns

- Do not parse usage by inspecting text deltas.
- Do not send usage chunks as `compact_delta`.
- Do not globally alter normal chat stream payloads without an explicit API design.
- Do not make compact fail just because usage is absent.
- Do not label local heuristic estimates as `*_sglang` token counts.
- Do not add a broad retry around all compact LLM calls; retry only the usage opt-in rejection case.

## 14. Summary

The compact orchestrator already knows how to record usage if it receives a usage chunk. The missing pieces are request opt-in and parser pass-through. Future implementation should add compact-scoped usage request support, propagate `extra_body` through `chat_stream_async()`, preserve top-level SGLang `usage` chunks in `_stream_generate()`, and keep usage out of user-visible compact text. This gives reliable prefill/decode/token evidence for future compact performance work.
