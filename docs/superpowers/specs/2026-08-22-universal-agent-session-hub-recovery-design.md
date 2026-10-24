# Universal Agent Session Hub, Recovery, and UI Bubble Limits

**Date:** 2026-08-22

**Status:** Approved for implementation

## 1. Purpose

Make every LLM Agent execution independent from the lifetime of any one browser SSE connection. A browser refresh, browser restart, network disconnect, or `Open child` navigation must detach only that subscriber. The Agent session must continue until it reaches a natural terminal state or the user explicitly invokes a lifecycle action that is defined to stop it.

The same design must serve the complete Agent system: coordinator profiles, manager agents, basic agents, future top-level profiles, and manager-created child agents. It is not a manager-only feature.

The browser must retain a temporary recovery bookmark across refreshes and browser restarts. Re-entering `/vision` must show a top-level recovery action. Activating it must rebuild the original Agent conversation bubbles from the complete backend event history, replay events missed while disconnected, and continue following live events when execution is still active.

Backend Agent conversation, thought, final content, and observation payloads must have no character limit. UI rendering alone is limited by `webUI.content.bubble_character_limit`, defaulting to `20000`, with explicit expand/collapse controls.

## 2. Historical Bug and Root Cause

The original manager/child implementation coupled several unrelated lifecycles:

1. `/vision` page lifecycle cleanup deleted the manager session.
2. Manager disposal aborted the manager and all active children.
3. Child run state and event buffers were removed, so `Open child` could show `The manager session is being disposed` or return HTTP 404.
4. A first remediation retained terminal child replay and used an auxiliary-navigation guard to suppress manager DELETE.
5. The guard still called local cleanup, which aborted the live request transport.
6. A second remediation made guarded page cleanup a zero-teardown path and transferred Vision Automation controller disposal ownership to the page lifecycle adapter.
7. The failure remained because the protection depended on a 750 ms `pagehide`/paired-unmount permit. Jetson/WebView navigation can complete outside that timing window. The unmount then falls back to ordinary DELETE/disposal.

The decisive runtime evidence was that SGLang received `/abort_request` in the same millisecond that the Agent session became aborted. A passive SSE disconnect does not send `/abort_request`; manager session disposal does. Therefore a timing guard around page teardown cannot be the ownership boundary.

The architectural correction is to make execution session-owned and subscriber-independent.

## 3. Identity Model

### 3.1 One public identity

`session_id` is the only public identity for:

- Agent execution ownership;
- live subscription;
- replay and browser recovery;
- Stop;
- Answer Now;
- explicit session deletion;
- top-level and child Agent contexts.

Do not introduce a public `run_id`. The existing single-flight invariant permits at most one ordinary active producer per Agent session. Sequential turns append to one session journal.

### 3.2 Existing transport identities remain internal

The existing `request_id` remains necessary for HTTP request idempotency, request-status tracking, registration-token safety, and targeted SGLang/vLLM cancellation. It is not a recovery identity, is not shown to the user, and is not stored in the browser recovery bookmark.

Each journal record has a monotonically increasing `event_sequence`. This is a replay cursor, not another business identity.

Existing generation/registration tokens remain internal safeguards against stale workers and ID reuse. They must not become public recovery identifiers.

## 4. Universal `AgentSessionHub`

Every live `AgentSession` owns one `AgentSessionHub`. The Hub is generic and profile-agnostic.

It owns:

- the session-scoped append-only UI event journal;
- the next `event_sequence` value;
- zero or one ordinary active producer;
- subscriber registrations;
- current execution state;
- terminal metadata needed for replay and cleanup.

The Hub does not own or rewrite Agent model memory. It observes the existing outbound event stream and stores frontend events only.

### 4.1 Producer ownership

The background producer, not an HTTP response generator, consumes `AgentRunner.run_stream()` and owns:

- staged Agent admission;
- request lifecycle acceptance/running/terminal transitions;
- the Agent run-scope cleanup barrier;
- final closure of the runner stream;
- publishing terminal events to the session journal.

The producer must continue when there are zero subscribers. Client disconnect and `StreamingResponse` cancellation must never call `source.aclose()` on the producer-owned Agent stream.

Pre-accept validation and admission failures retain their current rollback behavior. Once the producer has registered and published acceptance, loss of the initiating subscriber does not roll back or abort the request.

### 4.2 Subscriber ownership

The original POST response and the recovery endpoint are subscribers. Closing a subscriber:

- unregisters only that subscriber;
- does not mutate `AgentSession.aborted`;
- does not cancel an active run scope;
- does not close the producer stream;
- does not send an upstream abort request;
- does not remove journal records.

Subscribers track a cursor and read journal records after that cursor. Notification objects wake subscribers when the journal advances; subscriber queues must not contain copies of large text payloads. A slow subscriber therefore consumes no per-event payload copy and can catch up directly from the canonical journal.

Replay-to-live handoff must be atomic with respect to the journal cursor: an event is delivered exactly once, in sequence order, with no gap between the final replayed record and the first live record.

### 4.3 Journal contents

The journal stores complete UI-facing payloads, including:

- session and turn lifecycle events;
- coordinator routing and handoff timeline events;
- manager dispatch and child status events;
- thought/reasoning deltas;
- tool calls;
- complete tool observations;
- final content;
- compact events;
- Stop, Answer Now, failure, aborted, and done events.

Each sequential turn appends a frontend-neutral `turn_started` timeline event before its Agent events. It contains enough request projection data to reconstruct the user turn and the initial assistant bubble without relying on browser memory. It may reference session-owned Vision display assets, but UI recovery metadata and image references must never enter an LLM prompt.

Existing agent-specific protocols must not multiply. Hub storage wraps the same unified outbound payloads and existing timeline events. Future profiles must publish through this same mechanism.

### 4.4 Child sessions

Manager-created child contexts already have unique child `session_id` values. Each child uses the same Hub abstraction. The existing child page subscribes by the child session ID; no separate public child run identity is added.

The manager may still await a child's terminal report internally. A child-page subscriber disconnect must not cancel the child or its manager. Child events must no longer depend on a character-truncated or event-truncated replay buffer when the Hub journal is available.

## 5. API Contract

### 5.1 Starting a turn

`POST /v1/agent/chat` retains its request schema and SSE media type. Internally it starts or joins the session producer, captures the journal cursor immediately before the new turn, and subscribes from that cursor. It must not replay earlier session turns to the already-mounted initiating page.

All streamed payloads include `event_sequence` as transport metadata. Existing consumers that ignore the extra field remain compatible.

### 5.2 Recovery

Add a session-based endpoint equivalent to:

```text
GET /v1/agent/session/{session_id}/events?after_sequence=0
```

For browser recovery, the frontend requests from sequence zero and rebuilds the complete available session projection. If the producer is active, the same response follows live events after replay. If the session is terminal, the response ends after the terminal journal is replayed.

Unknown, deleted, or TTL-expired sessions return a stable not-found/expired response. The frontend presents an expired recovery state and may remove the stale bookmark after user acknowledgement.

### 5.3 Explicit lifecycle actions

- **Stop:** aborts the active producer and upstream inference as today, appends terminal events, and retains the Hub journal and browser bookmark.
- **Answer Now:** aborts the old active execution as today; the replacement answer events append to the same session journal. It does not delete the browser bookmark.
- **Normal completion or failure:** retains journal and bookmark.
- **Trash / Clear conversation:** is the only normal-close action. The frontend removes its bookmark before issuing session DELETE. The backend captures run scopes, performs targeted aborts where needed, waits for the disposal barrier, disposes manager and child resources, closes subscribers, and deletes journals and session-owned display assets.
- **Session TTL:** is the fallback cleanup boundary for abandoned sessions and releases the same resources as explicit deletion.

## 6. Browser Recovery

### 6.1 Recovery bookmark

Text and Vision pages use a shared versioned localStorage record containing only:

```text
sessionId
pageMode
createdAt
schemaVersion
```

It contains no conversation text, tool output, observation, image, model prompt, request ID, or UI event list.

The record is created when an Agent session is bound for a turn. It survives normal completion, failure, Stop, Answer Now, refresh, browser close, and browser restart. It is removed by the Trash action or after the backend proves that the session is no longer recoverable.

### 6.2 Recovery UI

When a bookmark exists and that session is not already attached to the current page, the page header displays `恢复运行中的任务`. The button is intentionally independent of Stop and Answer Now state; it may restore a terminal stopped or Answer Now conversation.

On activation the frontend:

1. validates and opens the session event stream from sequence zero;
2. clears only the failed/stale projection for that recoverable session;
3. feeds replay records through the same reducer used by live Agent events;
4. reconstructs user and assistant bubbles, reasoning, Agent steps, routing, manager dispatch cards, child states, and final content;
5. continues consuming live events when the producer is active;
6. hides the recovery button while attached;
7. shows the button again if transport detaches without an explicit Trash action.

Text and Vision pages must share the storage and recovery logic. Vision remains the primary reported reproduction path.

## 7. Backend Content Integrity

Backend Agent UI output has no character limit. Remove hard-coded slicing of conversation, model thought, and observation payloads in streaming and synchronous Agent response paths, including the current 2,000-character thought and 5,000-character observation slices.

This rule applies to Hub journals and child transcript replay. It does not remove deliberately short index-card previews such as manager dispatch summaries, and it does not change independent safety/resource limits in tools such as search result counts, file readers, OCR, or command output contracts.

No UI length setting may trim, rewrite, summarize, or otherwise alter canonical Agent memory.

## 8. Web UI Bubble Rendering Limit

Add this configuration to `config/config-webUI.yaml`:

```yaml
webUI:
  content:
    bubble_character_limit: 20000
```

The backend config model validates a positive integer and exposes the value through `/config`. Frontend types and helpers consume this value. The default is exactly `20000`.

The limit applies only when rendering bubble bodies:

- user and assistant chat content;
- reasoning/thought content;
- tool observations;
- compact-summary bodies;
- child transcript full-content sections.

The complete string remains in React state. A shared component counts Unicode code points, renders at most the first configured number by default, shows a localized `Content collapsed` notice, and provides `Expand all` / `Collapse` controls. Expanded mode renders the full string. Manager dispatch preview limits remain unchanged because those cards are indexes, not full-content bubbles.

Both `/` and `/vision` must use the shared behavior.

## 9. Memory Management

The design intentionally retains complete text in RAM on Jetson Thor. Memory correctness comes from ownership and deterministic release, not character truncation.

- Store each journal event payload once per session.
- Subscriber state stores cursors/notifications, not payload copies.
- Detach dead subscribers immediately.
- Reuse immutable session-owned Vision display assets instead of duplicating Base64 strings per event or subscriber.
- Release a top-level session Hub, child Hubs, journal payloads, display assets, subscriber references, tool resources, and request-manager registrations after the existing disposal barrier.
- TTL cleanup must remove abandoned terminal and active-but-orphaned session resources through the same pinned-generation checks used by explicit disposal.
- A replacement session using the same public ID must never be affected by cleanup from an older generation.

The feature does not promise continued inference across a backend process restart. After a backend restart, the browser bookmark may remain, but recovery must report that the session is unavailable rather than reconstruct invented state.

## 10. Prefix-Cache and Prompt Boundary

The Hub and recovery projection are transport/UI infrastructure only.

- Do not write event journals, subscriber state, recovery bookmarks, event sequences, message display IDs, browser page state, or replay metadata into system prompts, user prompts, Agent state consumed by the model, canonical message history, or `memory.steps`.
- Preserve historical tool calls and user-role observations exactly.
- Do not change Answer Now prompt tails, compact prompt shapes, provider tool schemas, multimodal history omission rules, or SGLang/vLLM prompt equivalence.
- Stop and subscriber detach remain lifecycle actions only and do not rewrite canonical history.

## 11. Failure Handling

- A subscriber disconnect is informational and must not become an Agent error.
- A producer failure publishes one error event and one stable terminal event, then releases its active-producer ownership while retaining replay.
- A Hub subscription failure affects only that subscriber.
- Slow or stale subscribers resume from their last sequence without producer cancellation.
- Recovery of an unknown/expired session produces an explicit UI state, never a generic `Request timeout or aborted` bubble.
- Duplicate restore clicks are single-flight per page and must not create duplicate subscribers or duplicate projected deltas.

## 12. Verification Requirements

Automated coverage must prove:

1. Closing the initiating subscriber after acceptance does not set `session.aborted`, close the producer, cancel a child, or call targeted upstream abort.
2. The producer reaches terminal with zero subscribers.
3. Replay-to-live delivery is gap-free, duplicate-free, and sequence ordered under a deterministic append/subscribe race.
4. Sequential turns share one `session_id` and append to one journal without a public run identity.
5. coordinator, manager, basic, future-profile plumbing, and child sessions use the common Hub boundary.
6. Stop and Answer Now retain recovery data; Trash and TTL release it.
7. Same-string session replacement is generation isolated.
8. A 200,001-character observation and final response are preserved exactly by backend streaming, journal replay, and child replay.
9. The UI defaults to 20,000 displayed Unicode characters, can expand to the exact full 200,001-character string, and can collapse again without mutating state.
10. Recovery bookmarks survive refresh semantics and are removed only by Trash or confirmed expiry.
11. Text and Vision wiring remain synchronized.
12. Existing manager dispatch, session disposal, Stop, Answer Now, compact, Vision Automation, and prefix-cache regression suites remain green.

## 13. Non-Goals

- No public `run_id`.
- No survival of active inference across backend process restart.
- No new LLM prompt fields or model-visible recovery context.
- No change to ordinary non-Agent chat execution ownership, although its rendered bubbles use the shared UI display limit.
- No removal of short manager dispatch-card previews.
- No dependency-source modification.
