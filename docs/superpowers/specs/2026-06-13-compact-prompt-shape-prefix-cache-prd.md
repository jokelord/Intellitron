# Compact Prompt Shape Prefix-Cache PRD

> Status: PRD only. This document defines a future architecture direction and does not authorize implementation by itself.
> Date: 2026-06-13
> Scope: compact prompt construction, prefix-cache observability, agent/chat compact integration, unified timeline compatibility.
> Non-scope for this PRD-writing task: no backend implementation, no frontend implementation, no SGLang launch changes.
>
> **2026-06-14 implementation note:** Parts of the future architecture are now implemented: `cache_first_tail_with_fallback`, stable compact-control prompt clause, strict validation, reliable `reliable_position1` fallback, compact thinking-off, summary-only prompt, and `source_token_budget`. Current hard rule: cache-first keeps the untrimmed source only when it is within `source_token_budget`; if the untrimmed source exceeds that budget, compact skips cache-first and runs budgeted reliable fallback with `cache_first_skipped_reason=source_exceeds_budget`. Treat older "current facts" below as historical PRD context, not an exact description of current code.

## 1. Problem Statement

Compact currently works by sending a long prior context to the local model and asking it to produce a summary. End-to-end testing showed that compact duration can be too long. The SGLang terminal can show `#cached-token: 0` during compact even after previous prefix-cache stability work.

The surprising part is not only SGLang configuration. The current compact request shape intentionally diverges from the previous normal chat/agent request shape. That divergence is why SGLang cannot reuse the previous request's longest prefix for the compact request.

This PRD defines a future architecture to recover prefix-cache hit rate where safe, while preserving compact reliability and the unified timeline/message model.

## 2. Current Facts From Code

### 2.1 Agent auto-compact order

`AgentRunner.run_stream()` checks compact before routing and before the current user message is passed to `agent.run()`.

- `backend/agent/agent_runner.py:4860-4887`: `run_stream()` calls `_should_auto_compact_session(session)` and then `_run_compact_then_continue(session, trigger="auto")` before coordinator routing.
- `backend/agent/agent_runner.py:4548-4578`: `_should_auto_compact_session()` counts messages from `session.agent.write_memory_to_messages()`.
- `backend/agent/agent_runner.py:3877-3894`: the current user `message` is appended later by `agent.run(message, stream=True, images=..., reset=...)`.

This means agent-mode compact already follows the desired order: compact old context first, then bind the current user turn to the new compacted context.

### 2.2 Agent compact body construction

Agent compact body construction currently starts from the active agent memory, but overrides the system prompt.

- `backend/agent/agent_runner.py:4629-4712`: `_build_compact_body_messages()` converts `session.agent.write_memory_to_messages()` into backend `Message` objects and replaces `body_messages[0]` with compact-specific system text.
- `backend/agent/agent_runner.py:4691-4697`: the compact system prompt is `no_tools_preamble + main_prompt + no_tools_trailer`.
- `backend/agent/agent_runner.py:4706-4712`: only `body_messages[0]` is replaced; `body_messages[1:]` is preserved.
- `backend/agent/agent_runner.py:4714-4761`: `_run_compact_then_continue()` builds `CompactRequest(mode="agent", body_messages=body_messages, trigger="auto")`.
- `backend/agent/agent_runner.py:4763-4843`: manual agent `/compact` uses the same body construction path.

The code comments explain the reason: using the active agent system prompt caused the model to emit routing XML or tool-call format instead of a summary. The current override is a reliability fix.

### 2.3 Compact prompt builder shape

The v2 compact builder inserts the compact instruction at position 1.

- `backend/compact/prompt_builder.py:98-144`: `build_compact_messages_v2()` requires `body_messages[0].role == "system"` and returns `[body_messages[0], instruction_msg] + body_messages[1:]`.
- `backend/compact/prompt_builder.py:115-127`: the docstring states this was changed from tail insertion to position-1 insertion because a tail instruction was ignored in 40k+ contexts.

Current final agent compact shape is therefore:

```text
[compact_system, compact_instruction_user, old_history...]
```

This diverges from the previous normal agent/chat request at token 0, so `#cached-token: 0` is expected.

### 2.4 Compact LLM call and memory replacement

- `backend/compact/orchestrator.py:46-95`: `CompactOrchestrator.run_compact()` emits compact events and builds prompt messages.
- `backend/compact/orchestrator.py:195-202`: compact calls `llm_service.chat_stream_async(..., tools=None, request_id=summary_id)`.
- `backend/compact/orchestrator.py:230-243`: output is cleaned and applied through `apply_compaction`.
- `backend/agent/agent_runner.py:4580-4627`: `_replace_agent_memory()` archives old `memory.steps`, replaces memory with a single summary envelope `TaskStep`, keeps `has_run=True`, resets routing to the entry agent, and sets `first_post_compact_turn=True`.

The post-compact context window is implemented by replacing agent memory, not by creating a new frontend-visible session.

### 2.5 Chat compact path

- `backend/api/chat.py:416-488`: chat mode first runs `filter_messages_for_llm()` against frontend metadata.
- `backend/api/chat.py:495-521`: chat auto-compact currently builds `CompactRequest(mode="chat", body_messages=...)`, then rebuilds `final_messages` as summary envelope plus the current turn.
- `backend/compact/chat_message_filter.py:38-65`: compact metadata filtering replaces the latest compact summary bubble with an envelope and drops `pre_compact` messages.

After the 2026-06-13 ordering fix, chat auto-compact should compact prior history only and bind the last current user message afterward, matching the agent-mode sequence.

### 2.6 Frontend and unified timeline constraints

- `docs/superpowers/specs/2026-05-13-unified-multi-agent-message-model-design.md:12-14`: visible UI transcript and model context must be decoupled.
- `docs/superpowers/specs/2026-05-13-unified-multi-agent-message-model-design.md:145-180`: all future agent events should first enter canonical timeline event families.
- `docs/superpowers/specs/2026-06-06-slash-commands-and-compact-design.md:49-52`: compact events flow through existing `timeline_event`; no new top-level SSE protocol.
- `frontend/src/lib/timeline-projection.ts:109-148`: `projectTimelineEvent()` projects `compact_started`, `compact_delta`, `compact_completed`, and `compact_failed`.
- `frontend/src/lib/compact-projection.ts:82-209`: `applyCompactProjection()` mutates visible message state, marks pre-boundary messages, and inserts the compact summary bubble.

Prompt-cache work must not treat frontend bubbles, dispatch card text, or raw timeline events as model prompt material.

## 3. Why The Old v2 Prefix-Cache Ideal Is Not Current Reality

The older v2 compact prompt spec aimed for a shape where the compact request reused previous messages byte-for-byte and appended one compact instruction at the tail.

- `docs/superpowers/specs/outdated-2026-06-11-v2-compact-prompt-turn-shape-design.md:28-37`: original principle said compact input should match previous chat messages and add only one compact-instruction user row.
- `docs/superpowers/specs/outdated-2026-06-11-v2-compact-prompt-turn-shape-design.md:54-62`: original goals said only the trailing compact instruction should diverge.

Current implementation intentionally breaks that ideal in two places:

1. Agent compact replaces the active agent system prompt with compact-specific system prompt.
2. The builder inserts compact instruction at position 1 instead of at the tail.

This is not accidental drift. It was introduced because the cache-perfect shape failed summary compliance in practice. Any future cache-hit optimization must preserve reliability rather than blindly restoring the old shape.

## 4. Product Goals

- G1: Improve compact prompt-shape prefix-cache hit rate where possible.
- G2: Preserve the reliable summary behavior of current compact; no return to routing XML/tool-call summaries.
- G3: Preserve agent-mode order: compact previous context, then bind current user turn to the new context.
- G4: Preserve chat-mode order: compact previous history only, then bind current user turn.
- G5: Preserve unified timeline event flow and frontend projection behavior.
- G6: Produce measurable diagnostics before and after any prompt-shape experiment.
- G7: Keep compact event-triggered only: manual `/compact` or threshold-triggered auto-compact, not per-turn summarization.

## 5. Non-Goals

- N1: Do not implement this PRD in the current PRD-writing task.
- N2: Do not introduce per-turn LLM summarization.
- N3: Do not feed raw frontend transcript, raw timeline, routing deltas, dispatch card text, or UI bubble state into the LLM.
- N4: Do not revive `working_memory`, `unresolved_items`, `tool_ledger`, `recent_visible_history`, `agent_contract`, `user_goal`, or prompt-injected `context_pack`.
- N5: Do not change compact SSE event schema.
- N6: Do not introduce a separate compact SSE protocol.
- N7: Do not change `_replace_agent_memory()` envelope semantics.
- N8: Do not regenerate `runtime_context` during compact.
- N9: Do not require SGLang server restart or SGLang code changes for the initial architecture experiment.

## 6. Hard Constraints

### 6.1 Prefix-cache v2 constraints

- `docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-design.md:18-28`: stable prefix plus append-only dynamic tail is the core invariant.
- `docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-design.md:45-83`: LLM input is stable system/runtime/tool prefix plus serialized `memory.steps`; Tier 2 audit fields must not enter prompt body.
- `docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-design.md:116-122`: future historical compression must freeze old history and have the LLM consume only the latest compacted view.

### 6.2 Compact envelope constraints

- `backend/agent/agent_runner.py:4587-4599`: summary envelope is `summary_envelope_preface + summary_text + summary_envelope_suffix`, then stored as one `TaskStep`.
- `backend/agent/agent_runner.py:4599-4612`: `has_run=True` is mandatory so smolagents `reset=True` does not wipe the envelope.
- `backend/agent/agent_runner.py:4613-4617`: routing resets to entry agent and marks first post-compact turn.

### 6.3 Timeline constraints

Compact remains a timeline-projected behavior:

```text
backend compact_* event
  -> SSE timeline_event
  -> projectTimelineEvent()
  -> applyCompactProjection()
  -> visible compact_summary bubble
```

No future prompt-cache optimization should create UI-specific prompt records or a parallel frontend protocol.

## 7. Design Options

### Option A: Restore original tail-only compact instruction

Shape:

```text
[active_agent_system, ...old_history, compact_instruction_user]
```

Pros:

- Best theoretical SGLang longest-prefix reuse.
- Aligns with the original v2 turn-shape cache ideal.
- Compact request can share the active system and full old history prefix with the immediately previous normal request.

Cons:

- Previously unsafe: active agent system prompts can dominate and produce routing/tool-call output.
- Tail instruction was previously observed to be ignored in very long contexts.
- Requires strong validation and fallback.

This option is not acceptable as a direct replacement.

### Option B: Keep current reliable shape

Shape:

```text
[compact_system, compact_instruction_user, ...old_history]
```

Pros:

- Known to produce summaries reliably.
- Position-1 instruction has strong compliance.
- Minimal implementation risk.

Cons:

- Cannot reuse previous active-agent prompt/history prefix.
- `#cached-token: 0` is expected when compact system differs at token 0.
- Does not solve the user's prefix-cache concern.

This remains the fallback baseline.

### Option C: Meta-control preamble plus tail compact instruction

Add a small stable meta-control clause to all agent/chat system prompts at session/profile construction time. The clause is session-pinned and says that if the latest user message starts with a reserved compact sentinel, compact summarization mode supersedes normal routing/tool behavior for that one call.

Future compact shape:

```text
[active_agent_system_with_stable_compact_control, ...old_history, compact_instruction_user_with_sentinel]
```

Pros:

- Preserves active system and history prefix for SGLang longest-prefix reuse.
- The compact instruction is still the latest user turn, but the stable system already defines how to interpret it.
- Avoids replacing `body_messages[0]` for cache-first attempts.
- The meta-control clause is stable, so it does not violate prefix-cache v2.

Cons:

- It changes every profile's system prompt shape once after deployment.
- It may subtly affect normal agent behavior unless the sentinel rule is tightly scoped.
- Requires tests proving normal routing/tool behavior is unchanged.
- Still needs fallback because local model compliance is not guaranteed.

This is the recommended long-term direction, but only after observability is added.

### Option D: Hybrid cache-first fast path with reliable fallback

Attempt cache-first compact first. If validation fails, retry once with current reliable compact-specific system prompt.

Fast path shape can be Option C if the stable meta-control preamble exists:

```text
[active_agent_system_with_stable_compact_control, ...old_history, compact_instruction_user_with_sentinel]
```

Fallback shape is current reliable shape:

```text
[compact_system, compact_instruction_user, ...old_history]
```

Pros:

- Recovers cache hits when fast path works.
- Preserves correctness when active agent prompt still dominates.
- Produces measurable fallback rate.

Cons:

- Worst case can run two compact LLM calls.
- Requires robust validation of summary output.
- More moving parts than current compact.

This is the recommended future architecture after Phase 0 diagnostics.

### Option E: Profile-aware compact shape

Use cache-first shape only for safe profiles and current reliable shape for risky profiles.

Examples:

- Chat mode with ordinary system prompt: cache-first may be safe.
- `coordinator_1`: risky because system prompt is a routing contract.
- `basic_agent`: risky because system prompt/tool schemas can dominate.
- Future summarizer-safe profiles: cache-first may be safe.

Pros:

- Lower fallback frequency than a universal fast path.
- Avoids known bad profiles.

Cons:

- Requires profile safety classification.
- External or user-provided system prompts are hard to classify.
- Partial improvement only.

This can be combined with Option D.

## 8. Recommended Future Architecture

The recommended future architecture is a phased combination of Option C and Option D.

### Phase 0: Observability before prompt-shape changes

Add diagnostics that can answer these questions for every compact call:

- Which prompt shape was used?
- What is the rendered prefix divergence point compared with the last normal LLM request in the same session?
- How many prompt tokens were sent?
- How many cached tokens did SGLang report if available?
- How long did prefill take?
- How long did decode take?
- How many tokens did the summary generate?
- Did validation pass on the first attempt or fallback?

The diagnostics must be log-only or metadata-only. They must not enter LLM prompt text.

### Phase 1: Define a stable compact-control sentinel

Introduce a stable, profile-independent compact-control rule in system prompt construction. The rule must be short and tightly scoped.

Example conceptual rule:

```text
If and only if the latest user message begins with <compact_control>, ignore normal task execution, do not call tools, and produce the requested compact summary of prior messages. This control message is internal and only appears during explicit context compaction.
```

Requirements:

- The sentinel must not be user-typable in normal UI paths without escaping or internal wrapping.
- The sentinel must be stable across a session.
- The sentinel must not include timestamps, UUIDs, counters, session IDs, or runtime-changing data.
- The clause must not revive old sidecar/context-pack fields.

### Phase 2: Add cache-first compact prompt plan

Introduce an internal prompt plan concept. This is a design unit, not an exposed API.

```text
CompactPromptPlan
  shape: "cache_first_tail" | "reliable_position1"
  source_messages: List[Message]
  compact_instruction: Message
  validation_policy: CompactValidationPolicy
```

Cache-first plan:

```text
[active_system_with_compact_control, ...old_history, compact_control_instruction]
```

Reliable fallback plan:

```text
[compact_system, compact_instruction, ...old_history]
```

### Phase 3: Validate compact output before applying memory replacement

Validation must happen before `_replace_agent_memory()` or chat final-message rebuild.

Reject fast-path output if any of these are true:

- Empty output after `format_compact_summary()`.
- Output contains routing XML such as `<action>switch_agent</action>` or `<target>coordinator_2</target>`.
- Output contains tool-call markers such as `Calling tools:` or JSON tool-call arrays.
- Output is below a configurable minimum length relative to the prompt size.
- Output lacks the required summary skeleton if the active prompt still requires the 9-section format.

Rejected fast-path output should not be shown to users as `compact_delta` unless the UX explicitly supports retracting failed compact streams. The safer default is to buffer fast-path output until validation passes; only fallback or validated output should be streamed as visible `compact_delta`.

### Phase 4: Fallback once, then fail visibly

If cache-first validation fails:

1. Log validation failure with reason.
2. Retry once with current reliable position-1 compact system prompt.
3. If fallback passes, emit normal compact events and apply compaction.
4. If fallback fails, emit `compact_failed` with verbose reason.

Do not loop retries. Local single-model deployment does not benefit from a circuit breaker.

## 9. Chat Mode Specific Rules

Chat mode has no server-side session memory. Its source is the request messages after `filter_messages_for_llm()`.

Future chat auto-compact must preserve the ordering fixed on 2026-06-13:

```text
filtered = filter_messages_for_llm(original_chat_messages)
current_user = filtered[-1] if filtered[-1].role == "user" else None
history_for_compact = filtered[:-1] if current_user else filtered
compact(history_for_compact)
final_messages = [summary_envelope, current_user]
```

The current user message must not be summarized by the compact LLM call.

Manual `/compact` in chat mode is different: it is an explicit user action over the visible messages already in page state. It may compact all messages supplied to `/v1/compact/run`, because there is no separate current user turn waiting to be bound afterward.

## 10. Agent Mode Specific Rules

Agent mode compact source remains `session.agent.write_memory_to_messages()`.

The current user turn passed to `/v1/agent/chat` must not be appended before compact. This is already true in the current code.

Post-compact state must remain:

```text
session.agent.memory.steps = [TaskStep(summary_envelope)]
session.has_run = True
session.current_agent_name = config.agent.entry_agent_name
session.active_agent = None
session.first_post_compact_turn = True
```

The first post-compact turn's slim path must continue to be respected unless a future spec explicitly replaces it.

## 11. Unified Timeline Requirements

The prompt-shape optimization is backend-internal. The external timeline contract remains:

- `compact_started`
- `compact_delta`
- `compact_completed`
- `compact_failed`

No new top-level SSE event is required.

If future diagnostics are exposed to frontend, they must be added as optional `metadata` fields on compact events. Frontend projection must treat unknown metadata as optional and non-rendering by default.

## 12. Acceptance Criteria For Future Implementation

### 12.1 Correctness

- Agent compact does not include the current user turn that triggered `/v1/agent/chat`.
- Chat auto-compact does not include the current user turn that triggered `/v1/chat/completions`.
- Compact summary never applies if validation detects routing XML/tool-call output.
- `_replace_agent_memory()` invariants remain unchanged.
- Manual and auto compact still emit the same compact event families.
- Multi-compact chain preservation remains intact.

### 12.2 Performance and observability

- Compact logs prompt shape ID.
- Compact logs SGLang prompt/completion token usage when available.
- Compact logs first divergence index or stable hash comparison when implemented.
- Cache-first path shows higher cached-token count than reliable fallback in an E2E measurement where SGLang exposes cached tokens.
- Fallback rate is logged.

### 12.3 UI and protocol

- No frontend protocol fork.
- Existing `projectTimelineEvent()` compact projections continue to work.
- Existing `applyCompactProjection()` ordering behavior remains valid.
- Usage/prefix diagnostics do not render as chat text or compact summary content.

## 13. Required Tests For Future Implementation

- Builder shape test: cache-first plan produces `[active_system, ...history, compact_control_instruction]`.
- Builder shape test: reliable fallback plan preserves current `[compact_system, compact_instruction, ...history]` behavior.
- Rendered template test: cache-first plan shares a longer prefix with the previous normal request than fallback plan.
- Agent regression: cache-first invalid output containing `<action>switch_agent</action>` is rejected and fallback is attempted.
- Agent regression: fallback output is applied exactly once.
- Chat regression: auto-compact body excludes final current user, final LLM call includes summary envelope plus current user.
- Timeline regression: compact event shapes remain projected by `frontend/src/lib/timeline-projection.ts`.
- Multi-compact regression: prior summary remains part of second compact input and completed work migrates to Problem Solving.
- Slim-path regression: `first_post_compact_turn` remains set after compact and is consumed only for the intended post-routing call.

## 14. Rollout Strategy For Future Work

1. Implement observability only.
2. Run E2E with current reliable shape and record baseline:
   - prompt tokens
   - cached tokens when available
   - prefill ms
   - decode ms
   - total ms
   - summary length
3. Add compact-control sentinel behind config, default off.
4. Enable cache-first path behind config for local testing only.
5. Compare fast-path validation pass rate and latency.
6. Keep reliable fallback always available.
7. Only consider default-on after E2E shows improved latency without summary-quality regressions.

## 15. Explicit Anti-Patterns

- Do not simply revert to the old v2 tail-instruction design without validation.
- Do not move current user turn into compact input for auto-compact.
- Do not inject timeline/dispatch UI text into prompt.
- Do not add per-turn summarization.
- Do not make compact prompt depend on session ID, timestamp, random UUID, or run counter.
- Do not expose failed fast-path deltas to UI unless retraction semantics are explicitly designed.
- Do not delete the reliable fallback until a separate acceptance review proves it is unnecessary.

## 16. Summary

The current compact shape is reliable but cache-unfriendly. `#cached-token: 0` is expected because the compact request starts with a compact-specific system prompt and inserts instruction at position 1. A future cache-hit architecture should not blindly restore the old shape. It should add observability, introduce a stable compact-control sentinel, attempt cache-first prompt shape only where measurable and safe, validate output before applying compaction, and fall back to the current reliable prompt shape on any compliance failure.
