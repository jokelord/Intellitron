# Custom Compact Focus Design

> Status: draft PRD/spec, code-inspected 2026-07-03
> Builds on: `2026-06-06-slash-commands-and-compact-design.md`
> Must preserve: AGENTS.md Compact Prefix-Cache Covenant, especially cache-first tail-only compact control
> Implementation plan: not written yet

## 1. Goals

Add task-oriented compaction without changing the core compact pipeline:

- Auto-compact should summarize prior context with awareness of the real user message that triggered the next turn.
- Manual `/compact <...>` should let the user provide an explicit focus instruction for the summary.
- The compaction prompt configuration in `config/config-agent-prompts.yaml` should be reviewed for dead or vestigial fields before implementation changes are made.

## 2. Non-Goals

- Do not change `compact.prompt_shape=cache_first_tail_with_fallback` or `compact.prompt_control_enabled=true` behavior.
- Do not inject compact-control text into normal chat or agent prompts.
- Do not trim or rewrite `cache_first_body_messages` before the cache-first attempt.
- Do not move custom compact instructions into historical `messages`; they must remain an explicit compact request field or tail compact-control addendum.
- Do not change latency knobs such as `performance_ratio`, `max_summary_tokens`, or thinking settings.
- Do not invent a new frontend protocol. Continue using `/v1/compact/run` and existing `compact_*` SSE events.

## 3. Current Code Findings

### 3.1 Existing support already present

`backend/compact/types.py` already defines `CompactRequest.custom_instructions`.

`backend/compact/prompt_plan.py` passes `custom_instructions` to both reliable and cache-first prompt plans:

- `build_reliable_position1_plan(..., custom_instructions=...)`
- `build_cache_first_tail_plan(..., custom_instructions=...)`

`backend/compact/prompt_builder.py` appends custom instructions to reliable compact prompts after `prompts.custom_instructions_header`.

`backend/compact/prompt_control.py` appends custom instructions to cache-first tail instructions after `prompts.custom_instructions_header`.

`frontend/src/lib/compact-client.ts` already accepts `customInstructions?: string | null` and serializes it as `custom_instructions`.

### 3.2 Gaps to close

Manual text and vision pages call `buildCompactRequestBody(..., customInstructions: null)`, so `/compact <...>` cannot affect compaction today.

`backend/api/compact.py` accepts `CompactRunRequest.custom_instructions`, but the `mode='agent'` branch calls `agent_runner.run_compact_for_session(request.session_id, mode="agent", trigger="manual")` without forwarding the custom instructions.

`AgentRunner.run_compact_for_session()` and `_run_compact_then_continue()` build `CompactRequest(...)` without a `custom_instructions` argument.

Chat auto-compact already excludes the current user turn from `compact_source_messages` and appends it after the compact envelope. This is the right place to pass a task-focus instruction built from that current user message.

Agent auto-compact runs after `_bootstrap_session_from_context_messages(..., current_message=message)`. That bootstrap function intentionally skips the current user message, leaving it for `agent.run()` to append. This means the real user message is also available as a safe focus hint for agent auto-compact.

### 3.3 Config prompt fields that look dead or vestigial

`compaction.compact_control_sentinel` is loaded into `CompactionPromptsConfig` but the runtime helpers now no-op any sentinel escaping. It is currently empty in YAML and should remain empty. This field is vestigial unless a future explicit sentinel scheme is reintroduced, which would violate the current tail-only covenant.

`compaction.compact_control_system_clause` is loaded but `append_compact_control_clause()` and `ensure_compact_control_system_message()` are deliberate no-ops. Normal prompts must not receive a compact-control system clause. This field is effectively dead in current running logic and should stay empty or be removed in a separate cleanup.

`compaction.compact_control_user_instruction` is still used by `build_compact_control_instruction()`, but that function also hardcodes a self-contained control instruction before appending the configured text. The YAML text duplicates much of the hardcoded safety wording. It is not dead, but it is redundant and should be simplified only with test coverage because cache-first tail control depends on it.

`compaction.no_tools_preamble`, `main_prompt`, and `no_tools_trailer` are still used by reliable fallback and by agent-mode compact-specific system prompt construction. They are not dead.

`compaction.compact_user_instruction` is still used by reliable `reliable_position1` prompt shape and by `_extract_prior_summary_rule()` for cache-first. It is not dead.

`compaction.status_message` appears to be loaded and covered by config tests, but no current Python or frontend runtime usage was found in the inspected paths. It is likely UI-era vestigial config.

## 4. Approaches Considered

### Option A: Put task focus in `CompactRequest.custom_instructions` everywhere

This reuses the existing backend and frontend wire field. For cache-first, the instruction is appended only to the final internal compact-control user message, preserving the prior normal prefix. For reliable fallback, it is appended to the dedicated compact instruction.

Trade-off: The custom instruction field becomes a shared carrier for both manual user focus and auto-generated next-turn focus. This is acceptable if the generated text is clearly marked as an internal compact focus instruction.

### Option B: Add a separate `next_turn_user_message` field

This would make auto-compact intent explicit in the backend type model and avoid overloading `custom_instructions`.

Trade-off: It requires more schema, tests, and branching while the final prompt effect is still just an additional focus instruction. It also risks future callers treating `next_turn_user_message` as normal prompt history, which the covenant forbids.

### Option C: Append the next user message into compact source history

This would let the compactor see the next request as part of the conversation.

Trade-off: Reject. It changes what is being summarized, may duplicate the current user turn, and can make the summary list the next task under historical user messages. It also weakens prefix-cache stability by altering the source body instead of only appending tail control.

## 5. Recommended Design

Use Option A.

Introduce one small backend helper that converts a focus request into compact custom instructions. Manual compact uses the raw user-provided instruction. Auto-compact uses a generated English instruction derived from the real next-turn user message.

Suggested generated instruction:

```text
After compaction, the user's next task will be the instruction below. When writing the summary, prioritize preserving prior details that are relevant to that upcoming task. Do not answer or execute the task, and do not treat this focus note as part of the conversation being summarized. Use it only to decide which historical information needs extra detail.

Upcoming user instruction:
{{next_turn_user_message}}
```

This is clearer than the original wording because it distinguishes three things:

- The next user message is real, but the focus note is an internal compact instruction.
- The compactor must not answer the next task.
- The summary should include more detail only for prior information relevant to that next task.

## 6. Manual `/compact <...>` Requirements

### 6.1 Syntax

Support these forms:

- `/compact`
- `/compact <focus on API route changes>`
- `/compact <summarize tool-call failures and exact file edits>`

Only angle-bracket envelope syntax is accepted for custom manual instructions in this version. Text after `/compact` that is not exactly enclosed by one outer `<...>` pair should be rejected with a frontend-visible compact failure rather than silently sent as normal chat.

### 6.2 Parsing rules

- Trim leading and trailing whitespace after `/compact`.
- Empty args means `customInstructions=null`.
- A valid custom instruction starts with `<`, ends with `>`, and has non-empty trimmed content inside.
- The parser should strip only the outer envelope and preserve inner text exactly except for outer whitespace trim.
- Nested angle brackets are allowed as literal content if they appear inside the outer envelope.
- Missing closing `>` or empty `<>` should produce a local `compact_failed` projection with reason `invalid_custom_instruction`.

### 6.3 Frontend integration

`isCompleteSlashCommand()` already recognizes `/compact with args`. The page-level handlers must pass the exact argument string to `handleSelectSlashCommand()` instead of only passing the matched command object.

Both pages must stay in sync:

- `frontend/src/app/page.tsx`
- `frontend/src/app/vision/page.tsx`

The shared parsing helper should live in `frontend/src/lib/slash-commands.ts` or a compact-specific lib file. Do not duplicate parsing logic in both pages.

### 6.4 Backend integration

`backend/api/compact.py` must forward `request.custom_instructions` to agent mode.

`AgentRunner.run_compact_for_session()` must accept `custom_instructions: str | None` and pass it into `CompactRequest(...)`.

Chat mode already passes `request.custom_instructions` into `CompactRequest(...)` and should require only tests.

## 7. Auto-Compact Next-Turn Focus Requirements

### 7.1 Chat mode

In `backend/api/chat.py::_generate_stream_response`, when auto-compact triggers and `current_user_message` is present, build generated focus instructions from `current_user_message.content` and pass them as `CompactRequest.custom_instructions`.

The compact source must remain `filtered[:-1]`. Do not include the current user message in `body_messages` or `cache_first_body_messages`.

### 7.2 Agent mode

In `AgentRunner.run_stream()`, when `_should_auto_compact_session(session)` is true, pass the real `message` argument into `_run_compact_then_continue()` as the next-turn focus source.

`_run_compact_then_continue()` should accept `next_turn_user_message: str | None`, build generated focus instructions, and pass them as `CompactRequest.custom_instructions`.

Do not write the generated focus instruction into `session.agent.memory.steps`, `runtime_context`, `context_pack`, timeline events, or frontend messages.

### 7.3 Content conversion

If the user message content is not a simple string, convert it to a concise text representation suitable for an instruction block. Do not serialize images or binary payloads into the focus instruction. For vision mode, include the textual user instruction and ignore image data in the custom focus text.

## 8. Prompt and Cache Constraints

Custom compact focus must be appended after `prompts.custom_instructions_header`, exactly as current code already does.

For cache-first compact, the custom focus appears only in the final tail internal compact-control message. The prior `source_messages` prefix remains byte-stable.

For reliable fallback, the custom focus appears in the compact instruction inserted by the reliable prompt builder. This remains the correctness path when fallback is enabled.

The custom focus instruction must also tell the compactor not to list the focus note itself in section 6 "All user messages". The real next user message will be present in the actual next LLM request after compact; the summary should not duplicate it as historical conversation.

## 9. Testing Requirements

### 9.1 Frontend tests

Add pure parser tests for:

- `/compact` returns `null` custom instructions.
- `/compact <focus on code>` returns `focus on code`.
- `/compact <>` fails with `invalid_custom_instruction`.
- `/compact focus on code` fails with `invalid_custom_instruction`.
- `/compact <focus with <literal> brackets>` returns `focus with <literal> brackets`.

Update text and vision page integration tests to assert the parsed custom instruction is passed to `buildCompactRequestBody()`.

### 9.2 Backend tests

Add compact API tests proving:

- Chat manual compact forwards `custom_instructions` into `CompactRequest`.
- Agent manual compact forwards `custom_instructions` from `/v1/compact/run` to `AgentRunner.run_compact_for_session()` and then into `CompactRequest`.
- Cache-first prompt plan appends custom instructions only in the final tail instruction and does not mutate source messages.

Add auto-compact tests proving:

- Chat auto-compact passes generated next-turn focus instructions while keeping the current user message out of `body_messages`.
- Agent auto-compact passes generated next-turn focus instructions from the `message` argument while preserving the normal `agent.run()` flow.

### 9.3 Config/dead-code tests

Keep tests that assert `compact_control_sentinel == ""` and `compact_control_system_clause == ""` in YAML.

Add or update tests documenting that `append_compact_control_clause()`, `ensure_compact_control_system_message()`, and `escape_user_compact_control_sentinel()` are no-ops by design under the tail-only compact-control covenant.

If `status_message`, `compact_control_sentinel`, or `compact_control_system_clause` are removed from config in a later cleanup, remove their config loader fields and tests in the same PR. Do not mix that removal into the first custom compact feature unless the implementation remains small.

## 10. Acceptance Criteria

- `/compact` without args behaves exactly as before.
- `/compact <custom instruction>` produces a compact summary that emphasizes the requested aspect.
- Invalid manual custom syntax never falls through as a normal user message.
- Auto-compact summaries preserve more information relevant to the next real user request.
- Cache-first compact still receives the first attempt whenever eligible.
- `source_token_budget` is still applied only to reliable fallback.
- Compact calls keep thinking disabled through `compact.extra_body.chat_template_kwargs.enable_thinking=false`.
- Normal chat and agent prompts contain no compact-control system clause.
- Text and vision pages remain behaviorally synchronized.

## 11. Open Implementation Notes

`backend/api/chat.py::_should_auto_compact_chat()` should be inspected during implementation. The code around this function appears structurally suspicious in the current file: the token-counting body is located after an early `return` in `_build_reasoning_extra_body()`, which may make chat auto-compact unreachable. This is outside the custom-focus feature itself but directly affects validating auto-compact behavior.

The existing 2026-06-06 spec already said `/compact` could accept optional trailing text as `custom_instructions`. Current code did not finish that behavior. Treat the manual `/compact <...>` work as completing that earlier design, with stricter envelope syntax requested here.
