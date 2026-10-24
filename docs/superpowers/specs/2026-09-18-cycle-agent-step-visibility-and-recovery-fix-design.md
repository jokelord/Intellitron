# Cycle Agent-Step Visibility and Recovery Fix

> Status: approved for implementation by the user on 2026-09-18.
>
> This addendum extends the previously implemented Cycle scheduled-message design. It does not change the two-track scheduling model.

## 1. Confirmed recovery bug

`LLMClient.followCycle()` must treat non-2xx responses from the Cycle events endpoint as typed recovery failures. The events reconnect branch currently creates a plain `Error` for 404/410, while its catch branch only terminates on a non-retryable `CycleRecoveryError`. The result is an endless exponential-backoff reconnect loop after journal expiry or backend restart.

The branch must parse the response with `parseCycleRecoveryError(status, detail)`. Non-retryable `cycle_run_expired` and `cycle_run_unavailable` errors must reach `onError` exactly once and must not enter another reconnect delay. Existing bookmark/error cleanup in the page lifecycle remains authoritative.

## 2. Agent-step visibility behavior

Cycle retains two execution tracks:

- The ordinary track is the existing serial `wait`/`message`/nested `cycle` tree.
- The scheduled track is the independent top-level fixed-time message list.
- The single Cycle timeline/journal remains the ordering and recovery source. Agent-step visibility is a projection policy, not a third execution track and not an LLM context feature.

The Cycle editor adds a `Show agent steps` switch. It is disabled while a run is active and defaults to `false` for new and migrated editor state. The default is a resource-protection policy: unattended long-running Cycles can produce many thought/tool/observation/orchestration events, and retaining/rendering them all in a browser can consume substantial memory, DOM, and React update resources.

When enabled before Start:

1. The boolean is included in `CycleStartRequest.show_agent_steps` and therefore belongs to the immutable request fingerprint and retained run record.
2. The backend forwards `agent_step`, `orchestration_plan`, `orchestration_update`, `orchestration_done`, and agent `timeline_event` payloads through the Cycle journal with the existing Cycle sequence envelope. It does not create per-event tasks or call the LLM a second time.
3. The backend does not forward those high-volume agent events when the flag is false. Ordinary Cycle progress, final `llm_done`, queue, photo, failure, and terminal events remain unchanged.
4. The frontend consumes the forwarded events using the existing `dispatchAgentSessionPayload()` and `createTextAgentSessionHandlers()` behavior, then renders the existing `AgentStepDisplay`. No new Agent SSE protocol is introduced.
5. Each ordinary or scheduled message turn gets its own assistant execution bubble. Thought deltas, tool calls, observations, orchestration state, and dispatch operations are associated with that turn; the final response remains in the same bubble.
6. Browser refresh/replay uses the persisted run flag from `cycle_started` (and the event sequence journal), not the current editor toggle. A reconnect must rebuild the same visible/hidden behavior as the original run.

The Cycle visibility switch is independent of the global normal-chat reasoning switch: when enabled, Cycle steps are eligible for display. Existing per-component folding may still reduce the rendered DOM after completion, but it must not destroy canonical Cycle events or mutate LLM history.

## 3. Resource lifetime rules

- Hidden mode must not accumulate agent-step payloads in the browser.
- Visible mode must release the current Cycle agent-run reference, assistant-step accumulation references, and transport/controller references on `llm_done`, failure, stop, terminal completion, and component unmount.
- A new Cycle message turn must replace the previous turn’s temporary references before it begins.
- The backend must not retain separate unbounded buffers for forwarded agent steps; the existing bounded Cycle journal is the only retained UI/recovery store.
- No scheduler metadata, visibility flag, raw Cycle timeline, or display state may enter Agent prompts, canonical memory, or prefix-cache inputs.

## 4. Compatibility and non-goals

- `CycleStep` remains limited to `wait`, `message`, and `cycle`; scheduled messages remain top-level.
- `share_context` semantics and the distinction between Cycle run identity and Agent session identity remain unchanged.
- Backend restart still does not resume a Cycle.
- Existing text and vision page camera-generation guards remain unchanged except for synchronized Cycle event/display glue.
- No generic Chat/Agent timeout policy is changed.

## 5. Verification requirements

Tests must cover:

- follow-cycle 404/410 responses stopping retry and surfacing typed non-retryable errors;
- request/state round-tripping of `show_agent_steps` with a false default;
- backend forwarding of agent steps only when enabled, including scheduled and ordinary message metadata;
- frontend projection of streamed and replayed agent steps into the correct Cycle assistant bubble;
- cleanup of the current Cycle agent-step references at terminal boundaries;
- identical behavior in `/` and `/vision`;
- existing Cycle scheduling, recovery, type-check, and production build regressions.
