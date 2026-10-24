# Cycle Agent-Step Visibility and Recovery Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop Cycle recovery from retrying terminal 404/410 failures forever and add an opt-in, resource-safe Cycle agent-step display mode.

**Architecture:** Keep the existing ordinary and scheduled tracks and single Cycle journal. Persist `show_agent_steps` in the Cycle request so backend forwarding and refresh replay are deterministic; forward high-volume agent events only when enabled. Reuse the existing Agent payload dispatcher, session-step handler, and `AgentStepDisplay` in both pages rather than creating a new SSE protocol.

**Tech Stack:** FastAPI/Pydantic, Python asyncio, Cycle event journal, Next.js 14, React, TypeScript, Node test runner.

**Spec:** `docs/superpowers/specs/2026-09-18-cycle-agent-step-visibility-and-recovery-fix-design.md`

## Global Constraints

- The ordinary track remains serial and the scheduled track remains a separate top-level fixed-time track.
- `show_agent_steps` defaults to `false`; hidden mode must not retain/render agent-step events in the browser.
- Agent-step forwarding is conditional and must use the bounded Cycle journal; it must not create unbounded per-step backend tasks or buffers.
- Cycle run identity and Agent session identity remain distinct; do not assume one Agent session when `share_context` is false.
- Scheduler metadata, visibility state, journal events, and UI state must never enter LLM prompts, canonical Agent memory, or prefix-cache inputs.
- Both `/` and `/vision` must receive equivalent Cycle lifecycle and rendering behavior; retain vision camera-generation guards.
- Do not alter generic Chat/Agent timeout or provider abort behavior.
- Use TDD: write a failing regression test before each production behavior change and run it before implementing the fix.
- Do not run git commands, create commits, or launch code-review agents.

---

### Task 1: Fix typed Cycle recovery failures and add the backend event-forwarding contract

**Files:**
- Modify: `frontend/src/lib/api.ts` (only the `followCycle` recovery branch if needed for the frontend contract test; the main frontend integration work is Task 2).
- Modify: `backend/cycle/models.py`.
- Modify: `backend/cycle/executor.py`.
- Test: `frontend/src/lib/api.test.mts`.
- Test: `tests/test_cycle_executor.py` or a focused new backend Cycle test module.

**Interfaces:**
- Produces `CycleStartRequest.show_agent_steps: bool = False`.
- Produces Cycle `cycle_started.show_agent_steps` metadata for replay.
- Produces conditional Cycle SSE events preserving the incoming Agent event type and Cycle journal sequence.
- Consumes existing Agent `run_stream()` payloads: `agent_step`, `orchestration_plan`, `orchestration_update`, `orchestration_done`, `timeline_event`, `content`, and `done`.

- [ ] **Step 1: Write the failing recovery test.**

Add an `LLMClient.resumeCycle()` test that returns an events-endpoint `410` with `{ "detail": { "code": "cycle_run_expired" } }`, records fetch attempts and `onError`, waits for the promise to settle, and asserts one fetch, one non-retryable `CycleRecoveryError`, and no retry delay. Add the equivalent `404 cycle_run_unavailable` case if the shared test helper allows it without duplicating setup.

- [ ] **Step 2: Run the recovery test and verify it fails for the current reason.**

Run:

```bash
node --test --experimental-strip-types src/lib/api.test.mts
```

Expected failure: the events reconnect branch throws a plain `Error`, so the catch loop does not call `onError` and schedules another fetch.

- [ ] **Step 3: Implement the minimal recovery fix.**

In the non-2xx events branch, replace the plain error construction with:

```ts
const detail = await response.text().catch(() => '');
throw parseCycleRecoveryError(response.status, detail);
```

Do not change retry behavior for transport errors or retryable statuses.

- [ ] **Step 4: Add failing backend contract tests.**

Cover a `CycleStartRequest` defaulting `show_agent_steps` to `False`, a `cycle_started` record exposing the flag, and an Agent stream containing one `agent_step` plus one `content`/`done` sequence. Assert the agent step is absent from Cycle events when the flag is false and present, with the original step fields and ordinary/scheduled metadata, when true.

- [ ] **Step 5: Run the backend tests and implement the smallest contract change.**

Run the focused backend tests first. Add the boolean field to `CycleStartRequest`, pass it through `_execute_steps`/`_dispatch_message`/`_execute_message`, and conditionally translate only the approved high-volume Agent event types into Cycle SSE records. Preserve `llm_chunk`/`llm_done` behavior and keep `show_agent_steps` out of all LLM arguments and memory payloads.

- [ ] **Step 6: Run both focused test groups.**

Run:

```bash
node --test --experimental-strip-types src/lib/api.test.mts
./.sglang/bin/python -m pytest tests/test_cycle_executor.py tests/test_cycle_recovery.py tests/test_cycle_api.py -q
```

Expected: all tests pass, with no new dependency or timeout changes.

### Task 2: Add the opt-in editor state and synchronized Cycle step projection

**Files:**
- Modify: `frontend/src/lib/cycle-types.ts`.
- Modify: `frontend/src/lib/cycle-page.ts` or add a small shared Cycle step projection helper there.
- Modify: `frontend/src/components/CycleEditor.tsx`.
- Modify: `frontend/src/app/page.tsx`.
- Modify: `frontend/src/app/vision/page.tsx`.
- Modify: `frontend/src/lib/message-types.ts` only if an explicit Cycle bubble kind is needed.
- Modify: `frontend/src/lib/cycle-corrections.test.mts` and/or add a focused Cycle projection test.

**Interfaces:**
- Consumes `CycleStartRequest.show_agent_steps` and Cycle events from Task 1.
- Reuses `dispatchAgentSessionPayload()` and `createTextAgentSessionHandlers()` with a Cycle-local `AgentRunState` reference.
- Produces `CycleEditorState.showAgentSteps`, a `Show agent steps` switch, and synchronized live/replay projection in both pages.

- [ ] **Step 1: Write failing state/projection tests.**

Add tests that assert:

```ts
resolveCycleEditorState(null, [], []).showAgentSteps === false;
resolveCycleEditorState('{"steps":[]}', [], []).showAgentSteps === false;
resolveCycleEditorState('{"steps":[],"showAgentSteps":true}', [], []).showAgentSteps === true;
```

Add a source or pure-helper test proving both pages pass `show_agent_steps`, handle Cycle `agent_step`/orchestration payloads, and clear the Cycle-local run reference after `llm_done`/terminal cleanup.

- [ ] **Step 2: Run the frontend tests and verify the new assertions fail.**

Run:

```bash
node --test --experimental-strip-types src/lib/cycle-corrections.test.mts src/lib/cycle-page.test.mts
```

Expected failure: the editor state has no visibility field and page Cycle handlers currently discard all Agent steps.

- [ ] **Step 3: Implement the shared state and switch.**

Extend `CycleEditorState` with `showAgentSteps`, migrate old saved state to `false`, persist the field, and add a disabled-while-running checkbox/switch labeled `Show agent steps`. Extend `CycleEditor`’s `onStart` callback with the boolean and pass it from both pages into the request. Set a Cycle-local ref before starting and recover its value from `cycle_started.show_agent_steps` before replayed agent events are projected.

- [ ] **Step 4: Implement the live/replay projection using existing Agent handlers.**

For each Cycle message turn, create one assistant `Message` with `isStreaming: true`, `agentSteps: []`, and a Cycle-specific display marker. When visibility is enabled, route Cycle `agent_step` and orchestration payloads through `dispatchAgentSessionPayload()` into the existing handler, route nested Agent timeline events into the same handler, and route `llm_chunk` content into that turn’s handler. On `llm_done`, finalize the same bubble; if no step/content bubble exists, preserve the current compact final-answer behavior. On failure, stop, terminal completion, and unmount, clear the current run/assistant IDs and any temporary step accumulator refs.

The page render must show all Cycle agent steps when the Cycle switch is enabled, without allowing the global normal-chat hidden-reasoning setting to suppress the explicit Cycle choice. The switch being disabled must keep agent-step events out of React state entirely.

- [ ] **Step 5: Keep text and vision glue synchronized.**

Apply the same callback signatures, event routing, cleanup, and Cycle bubble rendering to `page.tsx` and `vision/page.tsx`. Preserve the vision-only camera stream release and generation guards. Do not put Cycle timeline data into `buildAgentContextMessages()` or any request prompt body.

- [ ] **Step 6: Run the focused frontend tests, type-check, and build.**

Run:

```bash
node --test --experimental-strip-types src/lib/cycle-corrections.test.mts src/lib/cycle-page.test.mts src/lib/api.test.mts
npm run type-check
npm run build:verify
```

Expected: all commands exit successfully and both `/` and `/vision` compile.

### Task 3: Integrated automated regression verification

**Files:**
- Test only: existing Cycle/backend/frontend test files as needed for concrete regressions from Tasks 1–2.

**Interfaces:**
- Consumes the completed backend and frontend contracts from Tasks 1–2.
- Produces fresh verification evidence; no review report or code-review workflow.

- [ ] **Step 1: Run the complete focused backend Cycle suite.**

```bash
./.sglang/bin/python - <<'PY'
import glob
import uvloop
import pytest

uvloop.install()
paths = sorted(glob.glob('tests/test_cycle*.py')) + [
    'tests/test_stream_tools_cycle.py',
    'tests/test_session_hub_runtime.py',
    'tests/test_session_hub_deletion.py',
    'tests/test_session_store_cleanup.py',
    'tests/test_main_lifespan_wiring.py',
    'tests/test_compact_cycle_integration.py',
]
raise SystemExit(pytest.main(['-q', *paths]))
PY
```

- [ ] **Step 2: Run the complete focused frontend suite.**

```bash
node --test --experimental-strip-types src/lib/cycle-page.test.mts src/lib/cycle-corrections.test.mts src/lib/api.test.mts
npm run type-check
npm run build:verify
```

- [ ] **Step 3: If a command fails, fix only a concrete regression.**

Use the same responsible implementation agent for a targeted follow-up, preserve the resource-lifetime and prefix-cache constraints, and rerun the exact failing command plus the complete focused suite. Do not broaden into unrelated Agent fixture failures.

- [ ] **Step 4: Report exact evidence and known non-changes.**

Report changed files, exact pass counts/exit codes, the confirmed follow-cycle fix, the default-off resource rationale, and any intentionally preserved behavior. Do not claim completion without fresh command output.

## Execution order and delegation

Run Tasks 1–3 serially because the frontend projection consumes the backend event contract. Reuse the previous backend architecture agent for Task 1 and the previous frontend integration agent for Task 2; communicate with both in English. Do not run implementation agents concurrently on overlapping files. Do not launch task reviewers or a final review agent because the user explicitly requested implementation plus automated verification only.
