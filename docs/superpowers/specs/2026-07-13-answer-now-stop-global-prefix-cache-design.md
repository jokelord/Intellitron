# Answer Now, Stop, and Global Prefix-Cache Stability Design

## Goal

Make Agent-mode Answer Now and Stop safe and provider-neutral for local SGLang and NVIDIA NGC vLLM deployments. Answer Now must first abort the active run, wait until every pre-existing worker has fully exited, preserve its backend-retained partial output, and only then generate a no-tool final answer.

Expand the existing Compact Prefix-Cache Covenant into a backend-wide Prefix-Cache Covenant. All local-LLM paths should preserve a stable canonical prefix and append new tails unless a documented semantic boundary makes a prefix change unavoidable.

## Scope

This design covers:

- Agent-mode Answer Now and Stop in the text and vision pages.
- Streaming and non-streaming Agent Answer Now APIs.
- SGLang and vLLM/OpenAI-compatible request cancellation.
- Agent run-scope ownership, rollback, request IDs, partial-output retention, and SSE finalization.
- Provider-neutral stop-all behavior.
- Backend-wide prompt construction and prefix-cache auditing.
- vLLM deployment and cancellation guidance in `AGENTS.md`.

Answer Now remains an Agent-only feature. This work does not add Answer Now to normal chat mode, modify third-party package source, tune compact latency parameters, or introduce a new session scheduler.

## Current Problems

The current Answer Now endpoint marks the session aborted and requests inference cancellation, but immediately starts replacement generation. The old worker may still be rolling back the same mutable `memory.steps` list while the replacement serializes or commits to it. This can omit the latest partial output, remove the replacement answer during late rollback, or overlap native/tool cleanup with a new inference request.

Frontend Answer Now visibility is not provider-aware. The button is controlled by Agent mode and loading state, while the active inference backend is not exposed by `/config`. A vLLM deployment can therefore appear to lack the feature because Agent mode, persisted frontend settings, loading state, or the not-yet-created Agent session controls visibility. The button can also appear enabled before a session ID exists and then do nothing.

The existing Answer Now prompt says a few tool calls may be allowed, while the streaming implementation supplies no tools. The intended behavior is no tool execution.

The existing compact-specific cache rules express useful invariants that should apply to every backend path using a local LLM.

## Chosen Architecture

Use a session run-scope barrier plus a canonical append-only Answer Now tail. Do not add a general command queue or clone and merge mutable Agent instances.

### Safe Answer Now Handoff

The streaming and non-streaming APIs share one handoff primitive with these phases:

1. Validate that the session exists and atomically acquire an Answer Now single-flight guard.
2. Capture the set of run scope IDs active at handoff start and the current inference request ID.
3. Mark only the captured old scopes for abort and request provider-neutral targeted inference cancellation.
4. Keep the Answer Now request alive, but do not call the replacement model while any captured old scope remains active.
5. Let each old worker retain ownership of rollback, request cleanup, abort cleanup, and `finish_run()`.
6. After all captured scopes exit, freeze canonical messages from the active runtime Agent's stable `memory.steps` under the appropriate session synchronization.
7. Clear only abort state belonging to the completed old run. Do not clear or overwrite a newer request ID or scope.
8. Build the replacement request from the same session-pinned system/runtime/tool prefix and canonical serialized history, then append one self-contained internal Answer Now user tail.
9. Start a new run scope and request ID, stream one no-tool final answer, and append one final `ActionStep` on success.
10. Release the single-flight guard on every terminal path.

There is deliberately no timeout while waiting for old workers. The replacement must never overlap an old tool or native worker. If the Answer Now client disconnects during the wait, old-run cleanup continues, but replacement inference must not start.

### Run-Scope Barrier

`AgentSession` should expose a condition/event-based wait for a captured set of scope IDs. It must not poll a generic busy boolean because a later scope could otherwise extend or incorrectly satisfy the wait. A scope is complete only after its worker has finished rollback and all scope-owned cleanup.

The SSE consumer never releases a run scope. Worker-owned cleanup remains mandatory for normal Agent runs and Answer Now replacement runs.

### Single Flight

Frontend refs continue to suppress rapid duplicate clicks, but the backend is authoritative. At most one Answer Now handoff or replacement may be active per session. A duplicate request receives HTTP `409 Conflict` and must not attach to the existing stream or start another run.

## Provider-Neutral Cancellation

All stop paths use the request ID registered for the actual active provider:

- vLLM/OpenAI-compatible provider: close the registered upstream HTTP response. Closing the client stream is the cancellation signal to the containerized engine.
- SGLang: close the registered upstream response and issue a targeted `/abort_request` for the same request ID.
- Never use abort-all as a fallback for a single Stop or Answer Now operation.
- Preserve pending-abort-before-registration behavior so an abort arriving during request setup is honored when the response is registered.
- Exact request-ID cleanup must never clear a newer replacement request.

Stop-all should first mark sessions/scopes aborted and close every registered active provider response. SGLang abort-all may remain as an SGLang-specific supplementary operation only when SGLang is actually active; vLLM must not depend on it.

Cancellation errors are diagnostic, not permission to start replacement inference early. Answer Now still waits for captured old scopes to exit through cooperative abort and worker cleanup.

## Partial Output and Memory Semantics

The source of Answer Now context is backend canonical Agent memory, never frontend bubbles or timeline state. Old-run rollback must preserve stream-visible partial `ActionStep.model_output`, completed observations, and task context according to existing rollback rules before the barrier is released.

Historical tool traces retain smolagents role conversion:

- `TOOL_CALL` serializes as `assistant`.
- `TOOL_RESPONSE`/Observation, including text returned by tools such as `read_file`, serializes as `user`.

`write_memory_to_messages()` may strip duplicate image payloads where already required, but must not strip `tool_calls`, summarize tool observations, change observation role, or break action/observation pairing.

## No-Tool Answer Now Contract

The final user-channel tail is self-contained and states that it is an internal immediate-answer instruction, not a new user task. It tells the model to:

- use only the preceding user turns, retained reasoning, and tool observations;
- answer immediately and concisely;
- not call tools in this Answer Now response only, without restricting later user turns;
- not route or hand off;
- not continue planning or investigation;
- return only the final response.

The normal Agent system/runtime and tool-schema prefix should remain unchanged for prefix reuse. Runtime enforcement must refuse to execute any tool call emitted during Answer Now. If the model violates the instruction, append one corrective tail and retry at most once without rewriting the prior prefix. A second violation produces an explicit error and never enters the tool chain.

## Frontend Behavior

Both `frontend/src/app/page.tsx` and `frontend/src/app/vision/page.tsx` must remain synchronized.

- Show Answer Now only while an Agent-mode run is loading.
- Do not branch button visibility on SGLang versus vLLM.
- Disable the button until `agentSessionId` exists and while Answer Now is already active.
- Finalize all old tracked bubbles exactly once before creating one replacement assistant bubble.
- Preserve interrupted content and reasoning-only output for review.
- Ignore late terminal callbacks from the old stream when they would reset replacement state.
- Stop during replacement silently finalizes retained partial replacement output without an Answer Now failure message.

Expose the actual active inference backend and relevant cancellation capability through `/config` for diagnostics and integration tests, not as a feature gate.

## Global Prefix-Cache Covenant

The covenant applies to normal text and vision chat, every Agent profile and tool loop, manager/future agents, routing and handoff, Answer Now, Stop-following turns, manual/automatic compact, retries, fallbacks, and both local providers.

The canonical request shape is:

```text
session-pinned system/runtime/tool prefix
+ canonical serialized prior memory/messages
+ append-only current-turn tails
```

The following rules are mandatory:

1. Previously sent historical message roles, contents, ordering, and tool action/observation pairs are not trimmed, merged, rewritten, or paraphrased merely to construct a later turn.
2. Frontend bubbles, raw timeline events, dispatch text, routing deltas, UI state, and session IDs never enter prompt bodies.
3. Dynamic controls are self-contained final tails. They are not inserted into system messages or the middle of history.
4. Stop changes request lifecycle state only. It does not rewrite canonical history. Retained partial model output is appended as a memory step.
5. Answer Now preserves the normal system/runtime/tool prefix and canonical history, then appends its no-tool tail. The no-tool restriction applies only to that Answer Now response and explicitly does not carry into later user turns.
6. Compact keeps its cache-first-tail behavior. Untrimmed source is attempted first, `source_token_budget` only caps an enabled reliable fallback, and failed candidate output is not exposed before validation.
7. Stream usage is diagnostics only and never becomes prompt or chat content.
8. Provider adapters may change transport fields, but equivalent SGLang and vLLM logical requests must receive equivalent messages.
9. Tests compare captured canonical messages/serialized request prefixes. Cache logs and token counts are supporting diagnostics, not correctness proof.
10. Once a new stable prefix is established after a legitimate semantic boundary, subsequent turns return to append-only behavior.

### Legitimate Prefix Boundaries

Byte-identical reuse may be impossible at these explicit boundaries:

- first request initialization;
- successful user-requested or automatic compact memory replacement;
- an intentional switch to an Agent profile with a different system prompt or tool schema;
- multimodal protocol changes required to represent new image input;
- different provider tokenizer or chat-template implementations.

These are not blanket exemptions. Each occurrence must be reported with its reason and the stable prefix established after the boundary. Per-turn dynamic data must not be moved into system or historical middle positions.

## Error and Terminal Behavior

- A missing session produces a clear client error.
- Duplicate Answer Now is deterministic and does not start a second run.
- An Answer Now disconnect while waiting never starts replacement inference.
- An Answer Now disconnect while generating aborts only its replacement request and leaves worker-owned cleanup intact.
- A targeted provider cancellation failure is logged with provider, session, and request ID without exposing internal errors as model text.
- Intentional SGLang socket closure must not become a user-visible generation error.
- Old-stream `done`, `error`, or `aborted` events cannot reset a newer replacement run.
- Stop remains pure cancellation and never synthesizes an answer.

## Implementation Boundaries

Expected implementation areas include:

- `backend/agent/agent_runner.py`
- `backend/api/agent.py`
- `backend/llm_service.py`
- `backend/main.py` and the existing config response schema
- `frontend/src/lib/answer-now-ui.ts`
- `frontend/src/lib/api.ts`
- `frontend/src/app/page.tsx`
- `frontend/src/app/vision/page.tsx`
- existing backend and frontend test files
- `AGENTS.md`

Do not modify `.sglang`, vLLM container package source, or other dependency source. Do not change compact performance ratios, summary limits, or other latency knobs without user approval.

## Test Strategy

Implementation follows test-driven development.

### Handoff and Concurrency

- Replacement model invocation remains zero until every captured old scope exits.
- Late old rollback cannot delete replacement memory.
- The exact backend-retained partial model output appears in replacement messages.
- Tool/native worker cleanup cannot overlap replacement inference.
- Duplicate Answer Now requests do not create duplicate runs.
- Client disconnect during the barrier does not start replacement inference.

### Provider and Request Lifecycle

- vLLM cancellation closes the registered upstream response.
- Pending vLLM cancellation before response registration is honored.
- SGLang cancellation closes upstream and calls targeted abort exactly once.
- Cancellation follows the actual active provider after fallback.
- Old request cleanup cannot clear a replacement request ID.
- Stop-all closes provider-neutral active requests and uses SGLang abort-all only when applicable.

### Memory and Prefix Stability

- `read_file` and other tool observations remain user-role messages.
- `tool_calls` remain present and paired with observations.
- Consecutive normal local-LLM requests preserve the previous canonical request as the next request prefix.
- Answer Now differs only by append-only tails after the stable prefix.
- No UI, timeline, routing, dispatch, session, or usage data enters model messages.
- Compact cache-first behavior and agent last-compacted-message behavior remain intact.
- Every unavoidable prefix boundary is represented by an explicit test or audit finding.

### Frontend and SSE

- Text and vision pages expose identical Answer Now rules.
- Agent loading under vLLM displays Answer Now.
- Missing session ID disables the button.
- One click creates one replacement bubble.
- Late old-stream terminal events cannot reset replacement loading state.
- Stop during replacement retains partial output without failure text.

### Regression Verification

Run focused backend Agent/API/LLM tests, frontend Answer Now and projection tests, compact tests, tool-memory role tests, provider script/config tests, frontend build/type checks, and the broader available test suite appropriate to the environment.

## Work Allocation

- An implementation sub-agent performs the Answer Now, Stop, vLLM, UI, and associated test changes from a written plan.
- After implementation, an independent prefix-cache audit sub-agent examines every backend local-LLM call site, prompt builder, serializer, compact path, handoff, retry, and provider adapter. It reports and fixes confirmed covenant violations with tests.
- A final review sub-agent checks the implementation against this design, especially tool observation user roles, worker-owned cleanup, text/vision parity, and compact invariants.
- The main agent owns `AGENTS.md` updates, integration, review fixes, and final verification.

No Git command is used without explicit user permission.
