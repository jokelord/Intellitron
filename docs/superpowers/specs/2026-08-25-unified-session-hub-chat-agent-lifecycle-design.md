# Unified Session Hub for Chat and Agent Lifecycles

**Date:** 2026-08-25

**Status:** Approved for implementation

**Supersedes:** The ordinary non-Agent Chat exclusion in `docs/superpowers/specs/2026-08-22-universal-agent-session-hub-recovery-design.md`. All compatible requirements from that document remain binding, especially session-owned execution, subscriber-independent inference, recovery by `session_id`, deterministic release, frontend-only bubble truncation, and prompt/cache isolation.

## 1. Purpose

Use one backend Session Hub framework for every top-level LLM interaction, including ordinary Chat and Agent mode on both `/` and `/vision`. A browser connection is only a subscriber. Closing, refreshing, navigating away from, or reopening a page must not cancel accepted inference. The backend producer continues until natural completion or an explicit user lifecycle action targets it.

This design has two immediate correctness goals:

1. Fix ordinary Chat Vision Automation getting stuck at `思考中...` because the Hub-backed Chat path does not publish `request_accepted` to its subscribers.
2. Remove the duplicated Chat-versus-Agent lifecycle and recovery implementations so future acceptance, detachment, terminal, replay, Stop, and memory-management changes are made once.

It also closes the two still-valid Answer Now concurrency findings from the latest review:

- a delayed callback from an obsolete transport can still detach the retained session after a rejected replacement; and
- an original stable-terminal event can be lost while a provisional Answer Now replacement temporarily owns the transaction request identity.

## 2. Current-State Findings

### 2.1 Ordinary Chat Vision Automation acceptance is never observed

The current Chat Hub path is split across `backend/api/chat.py`, `backend/api/chat_session_store.py`, `frontend/src/lib/api.ts`, and page-local recovery code.

When `ChatCompletionRequest.chat_session_id` is present, `create_chat_completion()` creates a detached `produce_chat_session_events()` task and returns `stream_chat_session_live()`. The producer calls the request-lifecycle registry's `accept()` and `mark_running()`, but it does not append a canonical `request_accepted` record to the Chat Hub. `_generate_stream_response()` emits only OpenAI-compatible role/content/reasoning chunks and `[DONE]`. The live subscriber forwards only stored `wire` records.

Vision Automation stages its page mutation and historical-image omission until `onRequestAccepted`. Because that callback never fires on the Hub-backed Chat path:

- the staged user/assistant bubbles are not committed;
- the automation controller does not transition from pre-accept state;
- content callbacks target stale or absent UI state;
- the model may finish in the backend while the page remains visually busy.

This is a deterministic protocol gap, not an inference-engine hang.

### 2.2 The current Chat Hub is not the Agent Hub lifecycle

The current implementation reuses `AgentSessionHub` only as a journal container. It still maintains separate behavior:

- a separate `ChatSessionStore` and Chat-only retention policy;
- Chat-only `wire`, cumulative `content`, and cumulative `reasoning` records;
- Chat-only live and replay generators;
- `/v1/chat/session/{id}/events` versus `/v1/agent/session/{id}/events`;
- `chatSessionEvents()` versus `agentSessionEvents()` frontend parsers;
- local transcript recovery versus Agent bookmark recovery;
- page-local terminal and detach branches for Chat.

The cumulative Chat snapshots also retain repeated prefixes. For an output split into many deltas this can approach quadratic retained characters, while raw wire records duplicate the same text again. This conflicts with the single-copy memory design.

### 2.3 The reviewed Answer Now races still exist

`VisionAutomationController.beginAnswerNowReplacement()` currently dispatches `request_superseded` and immediately rewrites the authoritative `transaction.requestId`. If the original request reaches stable terminal before the replacement is accepted, the original terminal callback fails the current-transaction identity check and is discarded. A later known replacement rejection restores the old request ID but cannot restore the lost terminal transition.

The Vision page also aborts the original controller, installs the replacement controller, and restores the already-aborted original controller after a known replacement rejection. A delayed original `onDetached` can then appear current and detach local session ownership/bookmark state. Temporary global Stop/Answer Now flags are not permanent transport ownership and cannot safely reject late callbacks.

## 3. Identity Model

### 3.1 Public identities

`session_id` is the only public identity for a recoverable Chat or Agent conversation. It is used for:

- Hub lookup;
- sequential turn ownership;
- live subscription and replay;
- browser recovery bookmarks;
- explicit deletion;
- mode-specific lifecycle actions that target a session.

No public `run_id` is introduced.

`request_id` remains the identity of one logical request attempt. It is required for:

- request admission and idempotency;
- request-status queries;
- targeted SGLang/vLLM cancellation;
- Answer Now replacement admission;
- Vision Automation acceptance transactions.

It is not a competing conversation identity and is not used as the browser recovery bookmark.

### 3.2 Internal generation and subscriber identities

Each accepted turn claims an opaque Hub producer generation token. Each attached frontend transport has an opaque subscriber ownership token. These are implementation-only stale-callback guards, not IDs exposed in API payloads, URLs, localStorage, or model context.

Sequential Chat and Agent turns reuse the same `session_id` and append new Hub generations to the same session journal.

## 4. Unified Backend Session Hub Framework

### 4.1 Core components

The backend must expose one generic framework with the following responsibilities:

1. **Session directory:** resolve a globally unique `session_id` to a registered Hub entry without routing through Chat- or Agent-specific replay code.
2. **Hub journal:** append immutable canonical UI events once, allocate `event_sequence`, enforce one active producer generation, and provide cursor-based replay-to-live subscription.
3. **Producer lifecycle:** perform admission, publish acceptance, publish a turn boundary, consume a domain source, publish terminal state, update request status, and release producer ownership independently from subscriber connections.
4. **SSE subscription:** encode canonical records, resolve optional session-owned display assets, and stream records from a cursor. Subscriber cancellation unregisters only that subscriber.
5. **Retention and release:** keep recoverable records while required, then deterministically release journal, assets, subscribers, and registry references.

The existing `AgentSessionHub` implementation is already transport-only and may be retained behind a generic `SessionHub` name or compatibility alias. The implementation must not duplicate the core class.

### 4.2 Domain adapters

Chat and Agent keep small adapters because their source protocols differ. They do not own lifecycle state.

The Agent adapter decodes the existing Agent SSE payloads and preserves all timeline, orchestration, tool, observation, child, content, error, and done events.

The Chat adapter converts OpenAI-compatible stream chunks into canonical Hub events. It must not store raw SSE wire strings or cumulative full-text snapshots. It publishes only the new delta and relies on the frontend reducer to accumulate it.

Both adapters use the same producer sequence:

```text
reserve request lifecycle
validate/create session and claim producer generation
mark request accepted/running
append request_accepted
append turn_started
consume domain source and append canonical events
append exactly one error when applicable
append exactly one done terminal
close Hub generation
mark request terminal
release producer generation
```

No model call may start before pre-accept validation, session ownership, request lifecycle reservation, and producer claim succeed.

### 4.3 Canonical event envelope

Every subscriber receives an SSE JSON object with:

```json
{
  "event_sequence": 17,
  "type": "timeline_event"
}
```

The complete canonical event vocabulary is:

- `request_accepted`: includes `request_id`, `session_id`, and `status: "accepted"`;
- `timeline_event` with `event.kind: "turn_started"`: identifies the session, turn index, mode, visible user content, and session-owned display asset references;
- `timeline_event` with `event.kind: "message_delta"`: carries one new content or reasoning delta, a stable per-turn assistant `message_id`, `display: "chat_bubble"`, and `partial: true`;
- existing Agent timeline events, orchestration payloads, and Agent steps;
- `content` only where the existing Agent final-content contract requires it;
- `error`: one visible producer failure payload;
- `done`: includes `session_id`, `request_id`, and terminal `status`.

Chat message deltas use `source: "chat"`. Existing Agent `coordinator_2` message deltas retain `source: "coordinator_2"`. The shared projection accepts both sources without broadening unrelated timeline events into bubbles.

### 4.4 Turn boundaries and Chat reconstruction

Ordinary Chat must use one stable `session_id` across sequential turns instead of creating a new Chat journal ID for every send. Each turn has a session-local monotonically increasing `turn_index` and stable UI IDs derived from `session_id + turn_index`.

The Chat `turn_started` event contains only the newly submitted user turn, not the full frontend-supplied history. The full history remains the request's canonical model input but must never be copied into the Hub just because it was resent.

Replay from sequence zero reconstructs the complete available Chat transcript from its turn boundaries and assistant events. That projection can then be translated back into the existing `ChatMessage[]` wire format for later Chat requests. Hub metadata itself never enters the model payload.

### 4.5 Session-owned Vision display assets

Large Base64 images must not be copied into every journal event or subscriber queue. Both Chat and Agent session entries use one generic display-asset owner:

- `turn_started` stores immutable asset IDs;
- SSE encoding resolves asset IDs only for the subscriber response;
- subscriber state stores cursors, not resolved payload copies;
- Vision Automation historical omission evicts or replaces historical assets according to the existing sticky omission rules;
- explicit deletion and TTL release destroy all assets owned by the exact session generation.

These assets are UI/recovery data only. They must never be read to construct an LLM prompt.

## 5. Unified API Contract

### 5.1 Starting Chat and Agent turns

`POST /v1/chat/completions` and `POST /v1/agent/chat` keep their domain request and response compatibility, but both become subscribers to the same Session Hub framework.

The canonical Chat extension field is `session_id`. A temporary input-only compatibility alias for `chat_session_id` may be accepted during migration, but internal code and frontend requests use only `session_id`. Supplying both with different values is HTTP 422. This alias is not a second identity.

The POST subscriber starts at the journal cursor captured immediately before its turn, so it does not replay earlier turns into the already-mounted page. The first accepted Hub record observed by the subscriber is `request_accepted`.

If the POST transport disconnects after acceptance, the producer continues. If it disconnects before acceptance, normal reservation rollback applies and no staged Vision Automation omission is committed.

### 5.2 Unified recovery endpoint

Add one canonical endpoint:

```text
GET /v1/session/{session_id}/events?after_sequence=<non-negative integer>
```

It resolves Chat and Agent entries through the shared directory, replays canonical events, and follows the active producer generation when one exists.

Legacy Chat and Agent recovery endpoints may remain as thin compatibility wrappers for one migration cycle. They must call the same resolver/subscriber function and contain no independent event parsing, filtering, snapshot logic, or lifecycle decisions. New frontend code uses only the canonical endpoint.

Unknown, explicitly deleted, released, or TTL-expired sessions return HTTP 404 with a stable unavailable/expired detail. Invalid cursors return HTTP 422.

### 5.3 Explicit lifecycle actions

- **Subscriber detach:** unregister only the exact subscriber token. It never stops inference or deletes recovery state.
- **Stop:** targets the current request/session according to existing Chat or Agent API semantics, aborts only that inference, publishes a stable terminal, and retains recovery data.
- **Answer Now:** remains Agent-only, provider-independent, and uses the same Hub producer handoff. It does not delete recovery state.
- **Clear/Trash:** is the normal explicit close. It clears the browser bookmark synchronously, targets the exact session entry, aborts/waits when necessary, then releases journal and assets.
- **TTL:** releases abandoned terminal sessions using pinned object/generation checks so cleanup from an older same-string ID cannot destroy a replacement session.

Stop, Answer Now, and the recovery button remain independent controls.

## 6. Shared Frontend Hub Consumer

### 6.1 Transport reader

`LLMClient` must have one sequenced Session Hub reader used by:

- initiating Chat POST streams;
- initiating Agent POST streams;
- Answer Now replacement streams;
- Chat recovery;
- Agent recovery.

The reader is responsible only for:

- SSE framing and JSON decoding;
- strict positive contiguous `event_sequence` validation;
- duplicate suppression by cursor;
- known pre-accept HTTP rejection delivery;
- distinguishing an observed stable terminal from subscriber detachment;
- invoking the domain-neutral payload dispatcher.

Domain projection remains in adapters registered with that dispatcher. The transport reader must not contain page mutation or React state logic.

### 6.2 Live and recovery projection

The same payload reducer handles live and replay events. `turn_started` creates or reconciles the user and assistant bubbles. `message_delta` appends content/reasoning to the stable assistant message. Existing Agent events continue through `text-agent-session-handlers.ts` and timeline projection.

Text and Vision pages share the same session attachment and busy-state helper. Page-local glue may select Chat versus Agent projection, but it may not implement acceptance, terminal, detach, or sequence behavior independently.

### 6.3 Browser recovery descriptor

Both Chat and Agent modes use the same versioned bookmark shape:

```text
sessionId
pageMode
interactionMode  # chat | agent
createdAt
schemaVersion
```

It contains no image body, conversation text, observation, timeline payload, request ID, or model context. Re-entering the appropriate page shows `恢复运行中的任务`. Clicking it subscribes from sequence zero, reconstructs the available projection, and continues live.

The existing Chat transcript localStorage may be read once for migration/fallback, but new accepted Hub sessions do not depend on it for in-progress recovery. It must not remain a second authoritative live-recovery state machine.

## 7. Vision Automation Semantics

### 7.1 Acceptance transaction

Chat and Agent Vision Automation use the exact same acceptance contract:

1. capture and omission are staged;
2. frontend visible mutation is staged;
3. backend validates and reserves;
4. Hub publishes canonical `request_accepted`;
5. frontend commits the exact matching staged mutation and omission;
6. later transport loss uses request status only when acceptance truly remains ambiguous.

A known HTTP 409/422/503 received before acceptance does not query status. Retryable 409/503 preserves the frozen transaction and request ID. Non-retryable 4xx discards the staged transaction and resumes capture. A genuine 200 SSE EOF without acceptance remains ambiguous.

### 7.2 Historical-image omission

All existing Prefix-Cache Covenant item 9 rules remain unchanged:

- omission is active only for a session that has sent Vision Automation images, plus the exact one cleanup send after Abort;
- only historical image blocks are replaced;
- current-turn images remain complete;
- pre-accept failure does not mutate history;
- Chat and Agent semantics are equivalent;
- an Agent omission is confined to its bound top-level session;
- recovery replays placeholders for historical turns and full images for the latest retained turn;
- no other mode reuses this exception.

The unified Hub carries projection evidence of the already-committed omission but does not perform or invent model-history rewriting.

## 8. Answer Now Ownership Model

### 8.1 Permanent subscriber ownership token

Every initiating/recovery/Answer Now transport captures a permanent ownership token. A callback may mutate attachment, session bookmark, busy state, current request, or terminal gate only if its token still owns that exact responsibility.

Global flags such as `answerNowWasClickedRef` and `stopWasClickedRef` remain UI behavior flags only. They are not stale-callback authority.

A superseded transport stays permanently superseded. Its delayed `onDetached`, `onTransportEof`, `onError`, or terminal callback cannot detach or clear the promoted transport's session state.

### 8.2 Provisional replacement transaction

Beginning Answer Now creates a provisional replacement object without changing the authoritative original request identity. The object records:

- original request ID and terminal state;
- replacement request ID;
- replacement transport ownership token;
- pending original stable terminal, if one arrives;
- whether replacement acceptance has been observed.

Transitions are:

```text
replacement accepted:
  promote replacement request and transport atomically
  discard buffered original terminal
  permanently supersede original subscriber callbacks

replacement known-rejected before acceptance:
  discard replacement provisional state
  retain original session ownership
  apply buffered original terminal if present
  never restore an already-aborted controller as current

replacement ambiguous transport:
  keep provisional state
  resolve the replacement request_id through request status

replacement stable terminal after acceptance:
  settle the promoted transaction exactly once
```

The original terminal may be buffered only while replacement admission is unresolved. It must be reconciled immediately on acceptance/rejection and cannot be silently lost.

## 9. Memory Management

The unified design must reduce duplication while retaining complete backend content:

- one immutable journal payload per canonical event;
- delta events rather than cumulative content/reasoning snapshots;
- no raw Chat wire copy in the journal;
- cursor/notification subscriber registrations, never per-subscriber payload queues;
- one session-owned copy of each retained display image;
- deterministic historical image eviction under Vision Automation rules;
- immediate subscriber-token cleanup on detach;
- producer/source/request registrations released at stable terminal;
- journal/assets retained only for recovery policy;
- explicit Clear/Delete and TTL use the same pinned release routine;
- active sessions are never evicted merely to satisfy a finished-session capacity limit;
- old-generation cleanup cannot release a newer same-string session.

Important retention values must come from existing configuration or a clearly modeled Web UI/server configuration path; new hard-coded memory policy values are forbidden. This work must not change `webUI.content.bubble_character_limit: 20000`, which remains a rendering-only limit.

## 10. Prefix-Cache, Model Context, and Provider Invariants

This feature is transport/UI infrastructure and must preserve the project covenants:

- never feed Hub events, session directory metadata, browser bookmarks, event sequences, subscriber tokens, display IDs, or replay state into the LLM;
- never rebuild model input from raw Hub payloads directly; recovered UI messages must pass through the existing explicit Chat/Agent wire translators;
- preserve canonical historical roles, ordering, tool calls, and user-role observations;
- preserve compact prompt shapes and compact metadata translation;
- preserve SGLang/vLLM equivalent message and tool-schema shapes;
- preserve targeted Stop/Answer Now abort behavior, pending abort, and registration-token safety;
- do not change final-answer filtering or tool parser behavior;
- do not modify dependency source.

The only permitted multimodal prefix boundary remains the existing Vision Automation historical-image exception.

## 11. Failure Semantics

- A known non-2xx pre-accept response produces one exact visible rejection and never falls into ambiguous EOF recovery.
- A subscriber disconnect after acceptance does not publish an error and does not cancel the producer.
- A producer failure publishes at most one `error`, then one `done(status="error")`.
- A malformed or gapped Hub stream detaches that subscriber and exposes a recovery error without mutating backend execution.
- A duplicate restore click is single-flight per page attachment token.
- A terminal replay clears busy state and preserves the recovered final bubble.
- An unavailable/expired session produces a stable expired recovery state, not `Request timeout or aborted`.
- A delayed callback from any obsolete transport is ignored by ownership token.

## 12. Compatibility and Migration

The migration must be staged without running two authoritative frameworks:

1. Introduce the shared directory, producer lifecycle, canonical encoder/subscriber, and frontend reader behind tests.
2. Migrate Chat to canonical Hub events and stable `session_id`.
3. Route Agent and Answer Now through the same shared subscriber/ownership interfaces without changing Agent domain payloads.
4. Switch Text and Vision pages to the shared frontend consumer and bookmark.
5. Keep old endpoint/field aliases only as thin adapters.
6. Delete cumulative Chat snapshots, raw-wire journaling, and independent Chat recovery state after parity tests pass.

Compatibility adapters must not contain copied lifecycle decisions. A test must prove that their output is produced by the canonical subscription function.

## 13. Verification Requirements

Automated tests must prove all of the following:

1. Hub-backed ordinary Chat publishes `request_accepted` before any Chat delta.
2. Non-Agent `/vision` Automation commits its staged bubble on acceptance and leaves `思考中...` at stable terminal.
3. Chat and Agent initiating POST subscribers can disconnect after acceptance while producers reach terminal with zero subscribers.
4. Chat and Agent recovery use the same canonical endpoint, sequence validation, replay-to-live handoff, and terminal classification.
5. Sequential Chat turns reuse one `session_id`, append distinct `turn_started` events, and reconstruct the full transcript without a public run ID.
6. Chat content/reasoning journals retain each delta once and contain no raw SSE or cumulative snapshots.
7. Text and Vision pages use the shared attachment/busy/recovery helper in both Chat and Agent modes.
8. A known 409/503/422 before acceptance follows the established retry/discard rules without status lookup.
9. A raw successful-response EOF without acceptance remains ambiguous and uses request status.
10. Original accepted request -> provisional Answer Now -> original stable terminal -> replacement known rejection ends in the original stable terminal state with no lost event.
11. Replacement acceptance promotes ownership and ignores a later original terminal/detach callback.
12. Replacement rejection never restores an aborted controller as current.
13. Delayed obsolete `onDetached` cannot delete or detach the session/bookmark on Text or Vision pages.
14. Stop, Answer Now, and recovery controls remain logically independent.
15. Vision Automation recovery retains full latest-turn images and placeholders for historical automated images only.
16. Clear/Delete and TTL release the exact Hub journal/assets; Stop and normal terminal retain them.
17. Existing compact, Agent session recovery, child Agent, manager dispatch, targeted abort, request lifecycle, Chat, Vision, and prefix-cache suites remain green.
18. Frontend TypeScript type-check and production build verification pass.

## 14. Non-Goals

- No public `run_id`.
- No persistence of active inference across backend process restart.
- No database or cross-process event broker.
- No change to model prompts, canonical memory, tool traces, final-answer policy, or compact semantics.
- No conversion of ordinary Chat into a tool-calling Agent.
- No provider-specific frontend lifecycle.
- No association between recovery-button visibility and Stop or Answer Now state.
- No backend character limit for conversation, reasoning, final content, or observations.

## 15. Acceptance Criteria

The change is complete when an ordinary Chat or Agent turn can be started from `/` or `/vision`, the initiating page can disappear after acceptance, the backend producer continues, reopening the page presents the same recovery affordance, replay reconstructs the correct bubbles/events, and live following reaches the final answer. Non-Agent Vision Automation must no longer remain at `思考中...`. All lifecycle paths must use the same backend Hub producer/subscriber framework and the same frontend sequenced consumer, with only domain projection adapters remaining mode-specific.
