# Cycle Thinking, Scheduled Projection, and Stop Cleanup Fix Plan

## Goal

Fix the three end-to-end Cycle regressions reported after the initial
agent-step visibility implementation:

1. A Cycle Agent turn must use the browser-selected `enable_thinking` value.
2. A visible scheduled-track turn must project the same Agent steps and
   reasoning UI as a visible ordinary-track turn.
3. Stopping a Cycle must finalize the current Cycle assistant bubble so an
   empty stopped turn never falls back to the generic `思考中...` placeholder.

The ordinary and scheduled tracks remain two top-level tracks executed by one
serial Cycle producer and one journal/timeline. This change does not add a
third execution track, nested schedule, or prompt/history field.

## Root-cause findings

- `CycleStartRequest` has no reasoning-toggle field, the two pages do not put
  the setting in the Cycle request, and `CycleExecutor._execute_message()`
  invokes `AgentRunner.run_stream()` without `enable_thinking`. The normal
  Agent API already carries this value through its request and contextvar
  path, so Cycle currently diverges at the request boundary.
- Backend scheduled dispatch already uses the same `_dispatch_message()` and
  conditional Agent-event relay as ordinary dispatch. The frontend must be
  tested against the exact scheduled `step_begin` → Agent payloads →
  `llm_done` sequence and must keep its active turn projection alive across
  the scheduled metadata boundary.
- `handleCycleStop()` clears/finalizes the Cycle projection but does not mark
  its empty assistant bubble as stopped. `getAssistantRenderableText()` then
  intentionally returns `思考中...` for an empty, non-stopped assistant
  message. Stop cleanup needs a Cycle-specific stopped state or equivalent
  explicit finalization.

## Implementation tasks

### Task 1 — Backend reasoning propagation

Modify the Cycle request model/types and executor call chain to carry an
explicit boolean defaulting to `false`. Pass it to every Cycle Agent turn's
`run_stream()` call. Add regression tests asserting both ordinary and
scheduled dispatch preserve the selected value and that it is not placed in
the prompt/history.

Preserve the coordinator routing override contract: the dedicated
`coordinator_1_routing_extra_body` remains the routing call's configured
control unless the existing Agent request path already defines an explicit
per-request override. If the implementation makes the user toggle apply to
routing too, it must do so by a narrow, tested merge that never mutates the
configured object in place and does not alter unrelated normal-agent
behavior.

### Task 2 — Scheduled visible-turn projection and stop finalization

Add frontend tests first for:

- a scheduled `step_begin` followed by realistic `agent_step`, orchestration,
  timeline, content, and `llm_done` events producing one completed assistant
  Cycle bubble with Agent steps;
- stopping an empty or step-only current Cycle turn producing no
  `思考中...` placeholder and a finalized stopped bubble;
- equivalent behavior in text and vision page wiring.

Implement the smallest shared Cycle projection/helper change that gives
ordinary and scheduled turns identical lifecycle semantics. Keep the
existing `dispatchAgentSessionPayload()` and
`createTextAgentSessionHandlers()` path; do not invent a Cycle-specific SSE
protocol. Ensure the current run reference, accumulated step state, and
temporary UI IDs are released on llm completion, failure, stop, terminal
completion, and unmount. Keep the text and vision pages synchronized.

### Task 3 — Focused verification

Run the focused backend Cycle/Agent tests, focused frontend Cycle/projection
tests, frontend type-check, and production build verification. Do not run a
code-review workflow or use git commands.

## Acceptance criteria

- `enable_thinking: false` and `enable_thinking: true` are both observable at
  the Cycle Agent runner boundary for ordinary and scheduled messages.
- Visible scheduled messages show their Agent steps in the existing
  `AgentStepDisplay`/reasoning components; hidden mode remains unchanged.
- Stop leaves no active Cycle assistant message rendering `思考中...`.
- Both `/` and `/vision` have equivalent Cycle behavior.
- Existing Cycle scheduling, recovery, and Agent tests remain green.
