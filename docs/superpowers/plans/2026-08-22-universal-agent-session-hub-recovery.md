# Universal Agent Session Hub Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:systematic-debugging` before changing the existing lifecycle, `superpowers:test-driven-development` for every behavior change, and `superpowers:verification-before-completion` before reporting completion. The controller will execute this plan with subagents and task-level review gates.

**Goal:** Decouple all Agent execution from browser subscribers, add session-ID-based replay/recovery across browser restarts, retain complete backend Agent text, and enforce only a configurable 20,000-character frontend bubble display limit.

**Architecture:** Every `AgentSession` owns a generic append-only `AgentSessionHub`. A background producer owns the runner stream and request lifecycle, while POST and recovery SSE responses are cursor-based subscribers that can disconnect without cancellation. Text and Vision pages keep a small session-ID recovery bookmark and rebuild their projection from the same journal used for live events.

**Tech Stack:** Python 3.12, FastAPI/Starlette SSE, asyncio, threading-backed Agent workers, pytest, Next.js 14, React, TypeScript, Node test runner, Tailwind.

**Spec:** `docs/superpowers/specs/2026-08-22-universal-agent-session-hub-recovery-design.md`

## Global Constraints

- Read the complete spec and the repository `AGENTS.md` before editing.
- Do not run any git command. Do not create a branch, worktree, stash, commit, or patch dependency source.
- Use the existing main workspace and preserve unrelated user changes.
- `session_id` is the only public recovery/execution identity. Do not add a public `run_id`.
- Existing `request_id` remains internal to idempotency, request status, and targeted provider cancellation; it must not enter the browser recovery bookmark.
- The Hub applies to all Agent profiles and manager-created child Agent sessions, not only `manager_agent`.
- Backend conversation, thought, final content, and observation payloads must not be character-truncated.
- `webUI.content.bubble_character_limit` is exactly `20000` by default and affects rendering only.
- Stop and Answer Now do not remove recovery state. Only Trash/explicit session deletion and proven TTL expiry remove it.
- Do not feed Hub journals, recovery metadata, UI events, event sequences, or display IDs into LLM prompts, canonical history, `memory.steps`, tool traces, or prefix-cache inputs.
- Preserve the Prefix-Cache Covenant, structured assistant tool calls, user-role observations, Vision historical-image rules, compact prompt shape, provider equivalence, and targeted-abort semantics.
- Text and Vision pages must remain synchronized where behavior is shared.
- No task may silently broaden manager dispatch, child dispatch, tool parser, dependency, or configuration semantics.

## File and Responsibility Map

| File | Responsibility |
|---|---|
| `backend/agent/session_hub.py` | Session journal, sequence allocation, producer claim, cursor subscription, terminal/close state. |
| `backend/agent/agent_runner.py` | Attach one Hub to each Agent session; publish every profile and child event; release Hub with session resources; remove response truncation. |
| `backend/api/agent.py` | Start producer-owned Agent execution, make POST response a subscriber, expose session recovery SSE, integrate Stop/Answer Now/delete. |
| `backend/config.py` | Validate and expose Web UI content configuration. |
| `config/config-webUI.yaml` | Source of truth for `webUI.content.bubble_character_limit: 20000`. |
| `frontend/src/lib/api.ts` | Parse sequenced Agent session events and expose recovery subscription. |
| `frontend/src/lib/agent-session-recovery.ts` | Versioned localStorage bookmark and recovery state machine shared by Text/Vision. |
| `frontend/src/lib/text-agent-session-handlers.ts` | Apply both live and replay Agent events through one shared Text/Vision handler operation, including Answer Now continuation. |
| `frontend/src/lib/web-ui-config.ts` | Typed Web UI content config and validated bubble-limit helper. |
| `frontend/src/components/CollapsibleBubbleContent.tsx` | Rendering-only Unicode character window and expand/collapse UI. |
| `frontend/src/app/page.tsx` | Text Agent recovery button, Trash boundary, shared projection wiring. |
| `frontend/src/app/vision/page.tsx` | Vision Agent recovery button, Trash boundary, removal of unload-driven session deletion. |
| `frontend/src/app/agent/child/[childRunId]/page.tsx` | Child session Hub subscription and shared long-content rendering. |

---

### Task 1: Build the Generic Session Journal and Subscription Primitive

**Owner:** backend subagent; retain this same subagent for Tasks 2 and 3.

**Files:**
- Create: `backend/agent/session_hub.py`
- Modify: `backend/agent/agent_runner.py`
- Create: `tests/test_agent_session_hub.py`
- Modify: `tests/test_session_store_cleanup.py`

**Interfaces:**
- Produces `SessionEventRecord(sequence: int, payload: dict[str, Any])`.
- Produces `AgentSessionHub.append(payload) -> SessionEventRecord` with one immutable journal entry per call.
- Produces `AgentSessionHub.subscribe(after_sequence: int = 0) -> AsyncIterator[SessionEventRecord]` with atomic replay-to-live behavior.
- Produces internal producer claim/release operations keyed by an opaque generation token, not a public ID.
- Produces `AgentSessionHub.close(reason)` and `AgentSessionHub.release()` for subscriber wakeup and deterministic memory release.
- Adds `AgentSession.event_hub` without adding Hub state to any model-visible Agent state.

- [ ] **Step 1: Write deterministic RED tests for journal ordering and replay/live atomicity**

Create tests that append literal payloads with sequences 1 and 2, pause a subscriber exactly after its replay snapshot, append sequence 3, and assert the subscriber receives `[1, 2, 3]` once each. Add a second subscriber starting at sequence 2 and assert it receives only sequence 3.

```python
assert [record.sequence for record in received] == [1, 2, 3]
assert [record.sequence for record in resumed] == [3]
```

- [ ] **Step 2: Run the new tests and record genuine RED output**

Run:

```bash
./.sglang/bin/python -m pytest -q tests/test_agent_session_hub.py
```

Expected: collection/import failure because `backend.agent.session_hub` does not exist.

- [ ] **Step 3: Implement a single-copy append-only journal**

Use a lock-protected list of `SessionEventRecord` objects. Subscriber registrations hold a cursor and a coalescing notification primitive; they must not enqueue payload strings. `append()` allocates exactly one new positive sequence and wakes subscribers. `subscribe()` repeatedly snapshots records after its cursor, yields them in order, then waits for journal advance or close.

- [ ] **Step 4: Add producer ownership and closure tests**

Prove that one producer token can be claimed, a competing claim is rejected, a stale token cannot release a newer producer, terminal state wakes followers, and `release()` clears journal/subscriber references only for the exact Hub generation.

- [ ] **Step 5: Attach the Hub to `AgentSession` and pinned cleanup**

Create the Hub when the session is created. Ensure `SessionStore.delete_if_same`, explicit disposal, TTL cleanup, child cleanup, and failed candidate-session release close/release only the exact session object's Hub. Do not make ordinary `finish_run()` release the journal.

- [ ] **Step 6: Run focused GREEN tests**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_agent_session_hub.py \
  tests/test_session_store_cleanup.py
./.sglang/bin/python -m py_compile \
  backend/agent/session_hub.py \
  backend/agent/agent_runner.py
```

- [ ] **Step 7: Self-review Task 1**

Mutation checks: payload copies per subscriber, a replay/live gap, sequence duplication, stale producer release, and old-generation cleanup must each fail at least one test. Write the RED/GREEN evidence and exact files to the task report.

---

### Task 2: Move Top-Level Agent Execution Ownership from SSE to the Session Hub

**Owner:** same backend subagent as Task 1.

**Files:**
- Modify: `backend/api/agent.py`
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/request_lifecycle.py` only if a producer-owned transition needs an existing public helper
- Modify: `tests/test_agent_stop_api.py`
- Create: `tests/test_agent_session_recovery_api.py`
- Modify: `tests/test_agent_session_context.py`

**Interfaces:**
- Consumes `AgentSession.event_hub` and cursor subscription from Task 1.
- Adds `GET /v1/agent/session/{session_id}/events?after_sequence=<non-negative integer>`.
- Every published SSE payload contains top-level `event_sequence` metadata.
- Adds a `timeline_event` whose inner event has `kind: "turn_started"`, a session-local `turn_index`, visible user content, Vision display data/reference, and mode. None of these fields enter runner prompt arguments.
- POST `/v1/agent/chat` subscribes from the cursor captured immediately before the new turn; recovery subscribes from zero.

- [ ] **Step 1: Write a RED test that closes the initiating subscriber after acceptance**

Use a deterministic fake Agent stream that publishes acceptance, blocks, then completes when released. Start the real FastAPI streaming wrapper, consume acceptance, close only the response body, release the producer, and assert:

```python
assert session.aborted is False
assert upstream_abort_calls == []
assert producer_reached_terminal.is_set()
assert registry.get_status(request_id).status == "terminal"
```

The current response-owned `_stream_with_agent_lifecycle()` must fail this contract by closing its source or cleanup owner.

- [ ] **Step 2: Write RED tests for recovery replay and the exact cursor race**

Start a POST subscriber, detach it after sequence N, append N+1 while the recovery GET crosses replay-to-live, and assert the recovery response receives each sequence from 1 through terminal exactly once. Test invalid negative/non-integer cursors and unknown session IDs.

- [ ] **Step 3: Run RED tests**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_agent_session_recovery_api.py \
  tests/test_agent_stop_api.py -k 'subscriber or recovery or lifecycle'
```

Expected: disconnect cancels/closes the source and the recovery route is absent.

- [ ] **Step 4: Extract producer-owned lifecycle orchestration**

The background producer must be the only code that advances staged admission, consumes `AgentRunner.run_stream()`, appends decoded payload dictionaries, closes the source, waits for captured run scopes, and marks the request lifecycle terminal. Do not let `StreamingResponse` cancellation await or close this producer-owned source.

Publish request acceptance and `turn_started` through the Hub before ordinary Agent deltas. Preserve pending-abort registration tokens and same-request retry semantics.

- [ ] **Step 5: Implement POST subscription and recovery GET**

The POST response captures `start_sequence`, starts the producer, and follows records after that cursor. The recovery GET follows records after the validated cursor. Encode one SSE `data:` record per Hub record while preserving existing payload fields and adding `event_sequence`.

When replay reaches a terminal Hub with no active producer, close cleanly after the terminal record. Subscriber cancellation unregisters only the subscriber.

- [ ] **Step 6: Verify all entry profiles use the same top-level boundary**

Parameterize tests for coordinator entry, manager entry, and basic-agent entry. The API must not branch producer ownership by profile. Add a regression placeholder profile using the standard runner stream to prove future profiles inherit the same transport boundary.

- [ ] **Step 7: Run Task 2 GREEN verification**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_agent_session_hub.py \
  tests/test_agent_session_recovery_api.py \
  tests/test_agent_stop_api.py \
  tests/test_agent_session_context.py
./.sglang/bin/python -m py_compile \
  backend/api/agent.py \
  backend/agent/agent_runner.py
```

- [ ] **Step 8: Self-review Task 2**

Trace every `aclose()` and `CancelledError` path. Prove that only producer cleanup closes the runner stream and that subscriber disconnect cannot mark lifecycle terminal as aborted. Confirm `turn_started` is outbound-only and absent from LLM messages/memory.

---

### Task 3: Generalize the Hub to Child Agents, Explicit Lifecycle Actions, Full Content, and TTL Release

**Owner:** same backend subagent as Tasks 1 and 2.

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/api/agent.py`
- Modify: `backend/config.py`
- Modify: `tests/test_manager_agent_dispatch.py`
- Modify: `tests/test_agent_stop_api.py`
- Modify: `tests/test_session_store_cleanup.py`
- Modify: `tests/test_multi_agent_config.py`
- Create: `tests/test_agent_full_content_stream.py`

**Interfaces:**
- Child public identity remains its child `session_id`; existing child URLs may retain parameter names for compatibility but resolve the child session Hub.
- Stop and Answer Now append/replay through the same session journal and never release it.
- DELETE and TTL close/release top-level and child Hubs only after their disposal barriers.
- Backend `WebUIConfig` exposes `content.bubble_character_limit` as a positive integer, default `20000`.

- [ ] **Step 1: Write RED child-subscriber independence tests**

Dispatch a real child through the canonical parser and `SmartToolCallingAgent.process_tool_calls()` path. Attach and close the child stream while its deterministic worker is blocked. Assert the child and manager remain un-aborted, then release the worker and recover the full child journal by child session ID.

- [ ] **Step 2: Write RED Stop/Answer Now retention tests**

After Stop and after Answer Now, assert the active producer terminates as required but `session.event_hub` still returns the original events plus terminal/replacement events. Assert recovery data is removed only by exact session DELETE or TTL cleanup.

- [ ] **Step 3: Write RED full-content tests with literal 200,001-character payloads**

Use hand-constructed content such as `"观" * 200_001`. Assert streaming thought, sync thought, observation, final content, child publication, and Hub replay equal the exact literal length and suffix. These tests must fail on every current `[:2000]` and `[:5000]` path.

- [ ] **Step 4: Run RED tests**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_agent_full_content_stream.py \
  tests/test_manager_agent_dispatch.py -k 'subscriber or replay or full_content' \
  tests/test_agent_stop_api.py -k 'answer_now or stop or recovery'
```

- [ ] **Step 5: Publish child events through the child session Hub**

Replace the child page's authoritative replay source with the child session Hub. The manager's internal wait for the child final report remains independent from child-page subscribers. Preserve ordered shared-blackboard behavior, max-parallel-child rules, duplicate child rejection, canonical parser feedback, terminal replay retention, and child abort endpoint semantics.

- [ ] **Step 6: Integrate Stop, Answer Now, DELETE, and TTL**

Stop performs targeted abort and publishes stable aborted/done events without releasing the Hub. Answer Now appends its replacement stream to the same journal. DELETE removes browser-recoverable state only after the existing top-level/child run barriers. TTL uses pinned session object/generation checks so an old cleanup cannot close a replacement session with the same string ID.

- [ ] **Step 7: Remove backend Agent text slicing**

Remove character slices from Agent thought/observation/final response serialization in streaming, synchronous, memory-step emission, and child publication. Keep short manager dispatch-card previews and unrelated tool-level resource limits unchanged.

- [ ] **Step 8: Add Web UI content config parsing**

Add a focused content config dataclass/field under `WebUIConfig`, validate non-boolean positive integers, default to `20000`, and expose it through the existing `/config` serialization. Do not read this value from AgentRunner or use it to truncate backend payloads.

- [ ] **Step 9: Run Task 3 GREEN verification**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_agent_session_hub.py \
  tests/test_agent_session_recovery_api.py \
  tests/test_agent_full_content_stream.py \
  tests/test_manager_agent_dispatch.py \
  tests/test_agent_stop_api.py \
  tests/test_session_store_cleanup.py \
  tests/test_multi_agent_config.py \
  tests/test_runtime_config_limits.py
./.sglang/bin/python -m py_compile \
  backend/agent/session_hub.py \
  backend/agent/agent_runner.py \
  backend/api/agent.py \
  backend/config.py
```

- [ ] **Step 10: Self-review Task 3**

Check child/manager lock ordering, producer/session generation isolation, disposal barriers, exact provider abort targeting, subscriber cleanup, full-content suffixes, and absence of Hub/UI data from prompt snapshots.

---

### Task 4: Add the Shared Configurable Bubble Display Window

**Owner:** frontend subagent; retain this same subagent for Task 5.

**Files:**
- Modify: `config/config-webUI.yaml`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/web-ui-config.ts`
- Create: `frontend/src/components/CollapsibleBubbleContent.tsx`
- Create: `frontend/src/lib/bubble-content.test.mts`
- Modify: `frontend/src/components/AgentStepDisplay.tsx`
- Modify: `frontend/src/components/CompactSummaryPanel.tsx`
- Modify: `frontend/src/app/agent/child/[childRunId]/page.tsx`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: existing rendered-component and page wiring tests as needed

**Interfaces:**
- `WebUIConfig.content?.bubble_character_limit?: number`.
- `getWebUIBubbleCharacterLimit(config) -> number`, with a validated fallback of `20000`.
- Shared component receives the full string, the configured positive limit, render mode/Markdown child contract, and optional localized labels. It never mutates the source string.

- [ ] **Step 1: Add the exact YAML value and write config/type RED tests**

Add:

```yaml
  content:
    bubble_character_limit: 20000
```

Write tests that parse a supplied value, reject/fallback from non-positive/non-finite values on the frontend boundary, and return exactly `20000` when absent.

- [ ] **Step 2: Write a real rendered-component RED test**

Render a 20,001-code-point string containing CJK plus an emoji beyond the boundary. Assert the collapsed DOM shows exactly the first 20,000 Unicode code points and not the suffix; click `Expand all` and assert the exact full string appears; click `Collapse` and assert the suffix is hidden again. Retain the full input object/string throughout.

- [ ] **Step 3: Run RED tests**

```bash
cd frontend
node --experimental-strip-types src/lib/bubble-content.test.mts
```

Expected: missing helper/component or full content rendered without collapse.

- [ ] **Step 4: Implement the shared rendering-only component**

Count/slice with Unicode code points rather than UTF-16 code units. The collapsed notice and buttons must be keyboard-accessible. The component may pass the selected display string to Markdown rendering, but the parent state and props retain the full string.

- [ ] **Step 5: Wire every full-content bubble surface**

Apply the component to user/assistant message bodies, reasoning/thought bodies, tool observations, compact summaries, and child transcript content in Text and Vision. Keep manager dispatch-card 160/180-character previews unchanged. Do not slice strings while receiving SSE or updating React state.

- [ ] **Step 6: Run Task 4 GREEN verification**

```bash
cd frontend
node --experimental-strip-types src/lib/bubble-content.test.mts
node src/lib/vision-automation/components-rendered.test.mjs
node --experimental-strip-types src/lib/timeline-projection.test.mts
npm run type-check
npm run build:verify
```

- [ ] **Step 7: Self-review Task 4**

Search frontend receive/reducer paths for new slicing. Confirm only render variables are bounded, Unicode boundaries are correct, expanded output equals the original literal, and Text/Vision/child use the shared component.

---

### Task 5: Implement Browser-Persistent Session Recovery and Shared Live/Replay Projection

**Owner:** same frontend subagent as Task 4. Start only after Task 2 fixes the recovery wire contract.

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Create: `frontend/src/lib/agent-session-recovery.ts`
- Create: `frontend/src/lib/agent-session-recovery.test.mts`
- Create: `frontend/src/lib/text-agent-session-handlers.ts`
- Create: `frontend/src/lib/text-agent-session-handlers.test.mts`
- Create: `frontend/src/lib/answer-now-session-dispatch.test.mts`
- Modify: `frontend/src/lib/timeline-projection.ts`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/app/agent/child/[childRunId]/page.tsx`
- Modify: `frontend/src/lib/api.test.mts`
- Modify: `frontend/src/lib/timeline-projection.test.mts`
- Modify: `frontend/src/lib/vision-automation/page-integration.ts`
- Modify: `frontend/src/lib/vision-automation/page-integration.test.mts`

**Interfaces:**
- Versioned bookmark fields are exactly `schemaVersion`, `sessionId`, `pageMode`, and `createdAt`.
- No bookmark field stores request ID, message text, events, observations, or images.
- `LLMClient.agentSessionEvents(sessionId, afterSequence, callbacks, controller)` consumes the recovery SSE endpoint.
- Both live POST events and recovery GET events feed the same projection operation.

- [ ] **Step 1: Write bookmark lifecycle RED tests**

Use a real storage-compatible fake and literal records. Assert refresh/reinstantiation retains the descriptor. Assert normal completion, failure, Stop, and Answer Now do not remove it. Assert Trash clears it before the delete promise runs. Assert malformed version/fields are rejected without executing a request.

- [ ] **Step 2: Write shared live/replay handler RED tests**

Feed a literal sequence containing `turn_started`, routing, thought delta, tool call, a 200,001-character observation, manager child update, content, and terminal events through the real live POST and recovery GET adapters. Assert both invoke the same handler operation and produce identical state. Feed a duplicate sequence and assert it is ignored; feed a gap and assert recovery fails explicitly rather than silently corrupting the bubble. Include old `error/done` followed by matching Answer Now acceptance and replacement events, and assert the replacement lands in the original run-owned bubble.

- [ ] **Step 3: Run RED tests**

```bash
cd frontend
node --experimental-strip-types src/lib/agent-session-recovery.test.mts
node --experimental-strip-types src/lib/text-agent-session-handlers.test.mts
```

- [ ] **Step 4: Implement strict localStorage bookmark helpers**

Use separate Text/Vision page-mode slots or an equivalent keyed record so one page cannot claim the other page's context. Create/update the bookmark when the Agent session ID is bound. Never clear it from Stop, Answer Now, onComplete, or generic onError. Clear it synchronously at the start of Trash handling.

- [ ] **Step 5: Implement sequenced event parsing and the shared projection**

Parse `event_sequence` as a positive monotonic integer. Apply `turn_started` and all existing Agent/timeline payloads through one handler operation used by live and recovery flows; do not retain a second shadow reducer. Do not invent profile-specific SSE protocols. Preserve current coordinator message-ID uniqueness, manager dispatch joins, secondary-handoff bubble behavior, compact projection, and Answer Now's original-bubble continuation across producer generations.

- [ ] **Step 6: Add the header recovery action to Text and Vision**

Show `恢复运行中的任务` when a valid bookmark exists and the current page is not attached to that session. On click, single-flight a recovery subscription from zero, replace only stale projection state for that session, replay to terminal/live, and hide the button while attached. On transport detach without Trash, show it again.

If the backend proves the session unknown/expired, show a specific expired state and allow the stale bookmark to be dismissed. Do not render the generic `Request timeout or aborted` message as the recovered session result.

- [ ] **Step 7: Remove unload-driven backend session deletion**

Page refresh, browser close, pagehide, React unmount, and `Open child` may abort local fetch/media resources, but must not call session DELETE or Stop. Keep explicit local Vision Automation cleanup separate from backend Agent execution. The Trash handler remains the only normal frontend path that clears the bookmark and deletes the session.

Delete or retire the brittle 750 ms auxiliary-navigation ownership decision if it no longer owns any behavior; do not leave two competing session lifecycle systems.

- [ ] **Step 8: Migrate the child page to the generic session subscription**

Treat its route parameter as the child session ID. Replay and follow through the common sequenced event client while preserving the explicit `Abort child` action. Child page unmount only detaches its subscriber.

- [ ] **Step 9: Run Task 5 GREEN verification**

```bash
cd frontend
node --experimental-strip-types src/lib/agent-session-recovery.test.mts
node --experimental-strip-types src/lib/text-agent-session-handlers.test.mts
node --experimental-strip-types src/lib/vision-agent-session-recovery.test.mts
node --experimental-strip-types src/lib/answer-now-session-dispatch.test.mts
node --experimental-strip-types src/lib/bubble-content.test.mts
node --experimental-strip-types src/lib/api.test.mts
node --experimental-strip-types src/lib/timeline-projection.test.mts
node --experimental-strip-types src/lib/vision-automation/page-integration.test.mts
node src/lib/vision-automation/components-rendered.test.mjs
npm run type-check
npm run build:verify
```

- [ ] **Step 10: Self-review Task 5**

Trace every call to `deleteAgentSession`, `stopAgent`, and every request `AbortController`. Prove page lifecycle reaches only subscriber/local cleanup while Trash reaches bookmark deletion plus backend DELETE. Confirm Stop/Answer Now never touch the bookmark and no localStorage value contains content/images/request IDs.

---

### Task 6: Cross-Layer Integration, Prefix-Cache Audit, and Final Review

**Owner:** controller coordinates; original backend/frontend subagents fix findings in their own files; an independent reviewer performs the audit.

**Files:**
- Test-only additions may touch the focused files listed below.
- Production findings return to the owning backend or frontend subagent; do not let reviewers edit production code.

**Interfaces:**
- Consumes all Task 1-5 contracts.
- Produces a final evidence report, not a new runtime interface.

- [ ] **Step 1: Add or extend cross-layer disconnect/recover coverage**

Use the real FastAPI Agent route with a deterministic runner source. Accept a turn, detach the initiating response, let the producer publish manager/child/full-observation events with zero subscribers, attach recovery by `session_id`, and assert exact ordered replay through terminal. Assert no abort endpoint or session DELETE was called.

- [ ] **Step 2: Add or extend disposal and replacement-generation integration coverage**

Prove Trash/delete releases the exact old Hub and child Hubs after barriers, while a new same-string session and its producer/journal remain untouched by stale cleanup. Prove TTL produces the same release without subscriber leaks.

- [ ] **Step 3: Audit prompt and prefix-cache inputs**

Capture the actual `llm_messages` for equivalent requests before/after Hub integration and assert no event sequence, bookmark, recovery state, turn display metadata, or journal content was inserted. Preserve tool-call/observation roles and Answer Now/compact tails.

- [ ] **Step 4: Run the combined backend verification**

```bash
PYTHONPATH=/tmp/task2-pytest-uvloop${PYTHONPATH:+:$PYTHONPATH} \
./.sglang/bin/python -m pytest -q \
  tests/test_agent_session_hub.py \
  tests/test_agent_session_recovery_api.py \
  tests/test_agent_full_content_stream.py \
  tests/test_manager_agent_dispatch.py \
  tests/test_agent_stop_api.py \
  tests/test_session_store_cleanup.py \
  tests/test_agent_session_context.py \
  tests/test_write_memory_tool_calls.py \
  tests/test_agent_vision_history_omission.py \
  tests/test_compact_agent_routes_to_runner.py \
  tests/test_multi_agent_config.py \
  tests/test_runtime_config_limits.py
```

If the environment does not need the existing sandbox `sitecustomize` path, run the same command without the `PYTHONPATH` prefix. Do not treat an environment loop-policy error as a product pass.

- [ ] **Step 5: Run the combined frontend verification**

```bash
cd frontend
node --experimental-strip-types src/lib/agent-session-recovery.test.mts
node --experimental-strip-types src/lib/text-agent-session-handlers.test.mts
node --experimental-strip-types src/lib/vision-agent-session-recovery.test.mts
node --experimental-strip-types src/lib/answer-now-session-dispatch.test.mts
node --experimental-strip-types src/lib/bubble-content.test.mts
node --experimental-strip-types src/lib/api.test.mts
node --experimental-strip-types src/lib/timeline-projection.test.mts
node --experimental-strip-types src/lib/vision-automation/page-integration.test.mts
node src/lib/vision-automation/components-rendered.test.mjs
npm run type-check
npm run build:verify
```

- [ ] **Step 6: Independent final review**

The reviewer must explicitly decide:

- whether any subscriber cancellation can still reach producer/source cancellation;
- whether all Agent profiles and children share the Hub boundary;
- whether session-ID reuse is generation-safe;
- whether Stop/Answer Now/Trash/TTL semantics exactly match the spec;
- whether complete backend content survives every stream/replay path;
- whether frontend limits rendering only;
- whether UI/recovery metadata is absent from model inputs;
- whether Text and Vision remain synchronized.

Critical and Important findings return to the original owning subagent for a TDD fix and scoped re-review. Final completion requires fresh controller-run verification after the last production edit.

## Execution Order and Agent Allocation

1. Dispatch the original backend lifecycle subagent on Task 1.
2. In parallel, dispatch the original frontend lifecycle subagent on Task 4; these tasks have no overlapping files.
3. Review each completed task independently.
4. Reuse the same backend subagent serially for Tasks 2 and 3 to preserve context and prefix reuse.
5. After Task 2 fixes the wire contract, reuse the same frontend subagent for Task 5.
6. Do not run backend Tasks 2 and 3 concurrently because they overlap `agent_runner.py` and `api/agent.py`.
7. Do not run frontend Tasks 4 and 5 concurrently because they overlap both pages and shared API/config files.
8. Run Task 6 only after both implementation streams are integrated.

## Plan Self-Review

- **Spec coverage:** Tasks 1-3 cover universal Hub ownership, all Agent/child paths, lifecycle actions, full backend content, memory release, and recovery API. Tasks 4-5 cover config, render-only limits, browser bookmarks, recovery projection, and page lifecycle. Task 6 covers cross-layer behavior and prompt isolation.
- **Identity consistency:** No task adds a public `run_id`; recovery uses `session_id`; `event_sequence` is only a cursor; `request_id` remains transport-internal.
- **Ownership consistency:** producer closes runner/lifecycle; subscribers only detach; Trash/TTL release journals; Stop/Answer Now retain them.
- **Overlap control:** one backend implementer is reused serially for overlapping backend tasks; one frontend implementer is reused serially for overlapping frontend tasks.
- **No placeholders:** every behavior has a named file, interface, failing test shape, implementation boundary, and verification command.
