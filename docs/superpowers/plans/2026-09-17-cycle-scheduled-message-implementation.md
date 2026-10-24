# Cycle Scheduled Message, Recovery, and UI Optimization Implementation Plan

> **Implementation rule:** execute this plan task-by-task in the shared workspace. Do not run git commands in this implementation unless the user explicitly authorizes them. Do not create commits as part of this plan.

**Goal:** Extend Cycle mode with a durable-in-process top-level scheduled-message track, browser-timezone-aware `HH:mm:ss` delivery, queued arbitration with ordinary messages, reconnectable SSE recovery, Cycle timeout exemption, and a footer-independent readable canvas in both text and vision pages.

**Approved design:** [2026-09-17-cycle-scheduled-message-and-recovery-design.md](/home/zk/Project/LLM-deployment/docs/superpowers/specs/2026-09-17-cycle-scheduled-message-and-recovery-design.md)

**Architecture terms:**

- **Cycle run:** one user-started execution instance, identified by a `cycle_run_id` and retained in the backend process until it finishes or is stopped.
- **Ordinary track:** the existing sequential `wait`/`message`/nested `cycle` execution tree. A `message` in this tree remains a normal message and may be repeated by a `cycle` step.
- **Scheduled track:** a separate, top-level list of scheduled messages. It is parallel to the ordinary track, has no nested cycle syntax, and is never placed inside a `cycle` step.
- **Timeline:** the single serialized execution/observable event stream for a Cycle run. It is the ordering authority for scheduled dispatch, ordinary message boundaries, replay, and UI projection.
- **Due queue:** the bounded queue of scheduled occurrences whose wall-clock deadlines have passed while the Cycle run was busy. Enqueueing never performs capture or model work.
- **Dispatch arbiter:** the only component allowed to turn either a due scheduled occurrence or an ordinary message boundary into capture/admission/Agent execution.

## Global implementation constraints

- Preserve the prefix-cache covenant. Scheduler metadata, queue state, run fingerprints, reconnect cursors, and timeline/UI events must not be inserted into LLM system prompts or canonical message history.
- Keep the ordinary track strictly serial. A scheduled task that becomes due during an LLM/tool sequence waits in the due queue and dispatches at the next legal arbiter boundary.
- At a simultaneous boundary, drain due scheduled occurrences before dispatching the ordinary `message`.
- Use an injected/monotonic test clock for scheduler unit tests; do not make tests sleep for real wall-clock time.
- Browser timezone is explicit request data. Validate it as an IANA timezone and use it for local `HH:mm:ss` interpretation; never silently substitute the backend timezone.
- `once` and `daily` are per scheduled-message settings. `daily` requires a per-item finite `day_count`; no infinite recurrence is allowed.
- Backend restart does not resume unfinished Cycle runs. Browser refresh, tab navigation, and SSE disconnect do not stop the producer; a later attach/reconnect replays retained timeline events.
- Both `frontend/src/app/page.tsx` and `frontend/src/app/vision/page.tsx` must receive equivalent Cycle lifecycle/recovery changes.
- Keep all existing ordinary-cycle request fields and behavior backward compatible when `scheduled_messages` is omitted.
- The user explicitly requested no code-review/review-agent phase after implementation. Only run proportionate automated tests/type-check/build verification before reporting completion.

## Task 1: Add scheduler domain models and deterministic due-queue logic

**Complexity/model:** medium backend task; use a balanced coding sub-agent with medium reasoning. This is a pure, test-first foundation and should not touch executor lifecycle code.

**Files:**

- Create `backend/cycle/scheduler.py`.
- Modify `backend/cycle/models.py`.
- Create `tests/test_cycle_scheduler.py`.
- Extend the relevant request-validation assertions in `tests/test_cycle_api.py` only if the model tests need API-level coverage.

**Steps:**

1. Add a top-level Pydantic scheduled-message request model with stable client identity and the approved fields:

   ```python
   class ScheduledMessageRequest(BaseModel):
       id: str
       time: str  # exactly HH:mm:ss
       recurrence: Literal["once", "daily"]
       day_count: int = 1
       message: str = ""
       photo: list[int] | None = None
   ```

   Validate the exact zero-padded `HH:mm:ss` shape and real clock ranges, require positive `day_count`, require `day_count == 1` for `once`, and reject empty scheduled messages with neither text nor photo. Keep scheduled messages out of `CycleStepRequest`; a nested ordinary `cycle` cannot contain a scheduled step.

2. Extend `CycleStartRequest` with `cycle_run_id: str | None`, `scheduled_messages: list[ScheduledMessageRequest]`, and `timezone: str`. Keep `session_id` as a compatibility/identity hint only; do not encode uncertain `share_context` session multiplicity into the API contract.

3. Implement pure schedule primitives with aware datetimes and `zoneinfo.ZoneInfo`:

   ```python
   def validate_timezone(name: str) -> ZoneInfo: ...
   def parse_hhmmss(value: str) -> time: ...
   def first_occurrence(started_at: datetime, item: ScheduledMessageRequest, zone: ZoneInfo) -> datetime: ...
   ```

   `first_occurrence` must schedule today when the local target has not passed at run start, otherwise the next eligible day. For a `once` item it returns one occurrence; for `daily` it supports exactly the configured finite day count.

4. Add a deterministic `ScheduledTrack`/queue abstraction that stores only finite occurrence state, exposes the next deadline, emits every occurrence due at a supplied `now`, and orders due items deterministically by `(scheduled_for, editor_order, occurrence_index, id)`. It must make occurrence delivery idempotent by a stable occurrence key.

5. Write failing tests first, then the minimal implementation. Cover valid/invalid times, invalid IANA zones, today-versus-next-day behavior, once, daily count limits, DST-aware local interpretation, simultaneous due ordering, multiple occurrences becoming due while busy, and duplicate due collection not duplicating an occurrence.

6. Run the focused test module and leave the scheduler API documented in docstrings so later agents can consume it without rediscovering semantics.

## Task 2: Wire Cycle configuration and server defaults

**Complexity/model:** low-to-medium configuration task; use the same backend sub-agent as Task 1 via a follow-up so it retains the model/config context. Do not change executor behavior in this task.

**Files:**

- Modify `backend/config.py`.
- Modify `config/config-misc.yaml`.
- Modify `config/config-server.yaml` only for Cycle-specific server-exposed defaults if needed.
- Add/update `tests/test_cycle_config.py`.

**Steps:**

1. Extend the Cycle config dataclass/parser with explicit, configurable values from the approved design:

   ```yaml
   cycle:
     timeout_policy: exempt_cycle_run
     scheduled_track_max_duration_seconds: 2592000
     max_scheduled_message_days: 30
     event_journal_retention_seconds: 86400
     event_journal_max_records: 20000
     default_scheduled_messages: []
   ```

   Preserve the existing `enabled`, `max_depth`, `max_duration`, `show_thumbnails`, and `default_sequence` fields. Use typed config objects for defaults rather than duplicating hardcoded request parsing.

2. Validate that the only supported Cycle timeout policy is the explicit exemption policy (or fail clearly for an unsupported value), scheduled horizon and retention are positive, journal max records is positive, and the configured maximum scheduled days is positive.

3. Ensure `/config` exposes the values needed by the frontend without exposing internal queue state or creating a second frontend default source. Preserve existing cache-over-server setting precedence for unrelated Web settings.

4. Add regression tests for omitted new keys, configured new keys, invalid values, and legacy cycle YAML parsing.

## Task 3: Introduce a process-local Cycle run registry and reconnectable timeline journal

**Complexity/model:** high backend architecture task; use the most capable coding sub-agent with high reasoning. This task establishes lifecycle ownership and must be completed before executor scheduling integration. It may modify the session-hub code, but must not alter unrelated chat/agent behavior.

**Files:**

- Create `backend/cycle/run_registry.py` (or an equivalently focused module under `backend/cycle/`).
- Modify `backend/cycle/executor.py`.
- Modify `backend/session_hub_runtime.py`.
- Modify `backend/api/session_hub.py` only if the generic event endpoint is used for Cycle recovery.
- Add `tests/test_cycle_run_registry.py` and extend `tests/test_session_hub.py`/Cycle tests as appropriate.

**Steps:**

1. Add a process-local `CycleRunRecord` containing the opaque `cycle_run_id`, request fingerprint, start/status metadata, cancel signal, producer task reference, Cycle event hub/journal, and the scheduler/ordinary-track state required to continue after an HTTP subscriber disappears. Keep this record in memory only; no backend-restart recovery is required.

2. Make the producer independent of the `StreamingResponse` generator. Starting a run creates one producer task and appends canonical Cycle timeline events to an append-only per-run journal. The HTTP endpoint becomes a subscriber/serializer over that journal. A disconnected subscriber must not cancel the producer or its scheduler clock.

3. Add bounded retention configured by `event_journal_retention_seconds` and `event_journal_max_records`. Retention may prune old replay data, but it must never remove events newer than the configured bounds or corrupt sequence ordering. The run remains attachable while active; completed runs remain attachable only while retained according to the selected policy.

4. Extend the runtime registry with a Cycle entry kind or a dedicated Cycle registry path. Do not assume a single Agent session per Cycle when `share_context` is false; the Cycle run identity and Agent session identity must remain separate.

5. Define attach semantics:

   ```text
   attach(cycle_run_id, after_sequence, request_fingerprint) -> replay + live subscription
   ```

   A matching fingerprint attaches idempotently. A mismatch returns a conflict. A missing/expired run returns a precise not-found/expired error. Replay uses the canonical timeline event sequence, and the subscriber receives heartbeats without adding fake timeline records.

6. Preserve existing chat/agent session hub behavior. Add tests for subscriber disconnect while producer continues, replay from a cursor, bounded journal behavior, duplicate attach, fingerprint mismatch, stop, and unknown/expired run handling.

## Task 4: Integrate the two-track scheduler, arbiter, and Cycle timeout exemption

**Complexity/model:** highest-risk backend integration task; reuse the Task 3 agent via follow-up and set high reasoning. This task owns executor state-machine changes and should not be parallelized with other backend edits.

**Files:**

- Modify `backend/cycle/executor.py`.
- Modify `backend/api/cycle.py`.
- Modify `backend/main.py` if construction needs new configured dependencies.
- Modify `backend/config.py` only for integration wiring discovered by the same agent.
- Extend `tests/test_cycle_executor.py`.
- Create/extend `tests/test_cycle_recovery.py` and `tests/test_cycle_api.py`.

**Steps:**

1. Refactor `CycleExecutor.start` so it starts/attaches a run record and returns a subscriber stream; the producer executes independently. Preserve current SSE event names where possible and add explicit run/sequence metadata needed for reconnect.

2. Instantiate exactly one scheduler clock task per Cycle run. It waits until the next scheduled deadline (using aware timezone values), then enqueues the due occurrence and signals the arbiter. It must not capture a photo, call Agent APIs, or run an LLM itself. A finite daily schedule stops its clock when all occurrences are delivered or skipped by stop.

3. Add the dispatch arbiter as the sole path for message execution. It must enforce:

   ```text
   scheduler clock -> due queue -> arbiter
   ordinary boundary -> arbiter
   arbiter: due scheduled occurrence(s) first, then ordinary message
   ```

   The arbiter may dispatch scheduled work only at safe boundaries, after the current wait/Agent sequence completes. A scheduled occurrence that becomes due during reasoning queues without unbounded task creation. At a simultaneous ordinary-message boundary, scheduled work wins.

4. Reuse the existing photo capture and Agent admission/session logic for both message kinds. Scheduled messages carry their own text/photo payload and must emit enough timeline data for the frontend to render a distinct scheduled user bubble while retaining existing ordinary `message` rendering.

5. Maintain independent scheduled item identity and occurrence idempotency across reconnects. Never use wall-clock equality; compare deadlines with `now >= due_at` and let the queue order resolve ties.

6. Implement the approved timeout policy. `timeout_policy: exempt_cycle_run` means no total Cycle wall-clock deadline kills a valid long-running schedule. Keep individual waits bounded by their requested duration and retain bounded upstream/operation timeouts. Cycle-owned Agent sessions must not be reclaimed by generic idle/TTL cleanup while the run is active. Localized operation errors should produce the existing error timeline event and follow the documented continuation/stop behavior rather than silently dropping the queue.

7. Ensure stop cancels the producer, scheduler clock, pending wait, and active Agent request through the existing targeted abort path; it must not abort unrelated sessions. Ensure all run-registry resources are released exactly once after terminal state.

8. Add tests first for queueing during Agent work, scheduler-before-ordinary tie ordering, no preemption, daily finite horizon, long horizon beyond old `max_duration`, disconnect/reconnect, stop while scheduled work is pending, targeted abort, and duplicate start/attach.

9. Run the focused backend Cycle/session-hub test set and fix only regressions caused by this feature.

## Task 5: Extend shared frontend Cycle types/editor/progress UI

**Complexity/model:** medium frontend task; use a balanced coding sub-agent with medium reasoning. Keep the write set limited to shared Cycle components/types and styles; do not edit page lifecycle glue here.

**Files:**

- Modify `frontend/src/lib/cycle-types.ts`.
- Modify `frontend/src/components/CycleStepEditor.tsx`.
- Modify `frontend/src/components/CycleEditor.tsx`.
- Modify `frontend/src/components/CycleProgress.tsx`.
- Modify `frontend/src/app/globals.css`.
- Add or update focused frontend tests under `frontend/src/` if the existing test setup covers these components.

**Steps:**

1. Add shared types for the top-level scheduled track and its recurrence settings:

   ```ts
   type ScheduledRecurrence = "once" | "daily";
   interface ScheduledMessage { id: string; time: string; recurrence: ScheduledRecurrence; day_count: number; message: string; photo?: number[]; }
   ```

   Extend `CycleStartRequest`, `CycleEditorState`, and the Cycle SSE event union with run identity, sequence/reconnect fields, scheduled-message lifecycle fields, and an explicit event kind/track discriminator. Keep ordinary step types unchanged and do not add `scheduled` as a nested `CycleStep` union member.

2. Build a top-level Scheduled messages editor with `HH:mm:ss` input, `每天`/`仅1次` selector, per-item `day_count` shown/enabled only for daily, text input, and the existing camera/photo selection. Add/remove/reorder scheduled items without placing them in the recursive ordinary step editor.

3. Validate client-side before start: exact time format, IANA timezone availability through `Intl.DateTimeFormat().resolvedOptions().timeZone`, positive finite daily count, and non-empty text/photo content. Keep server validation authoritative and display its error.

4. Add status/progress rendering that distinguishes ordinary-track steps from scheduled-track occurrences and shows queued/delivered state without changing the canonical SSE event protocol into a new ad hoc UI-only stream.

5. Replace hardcoded dark-only Cycle styling with `.cycle-canvas` styles in `globals.css`. The canvas must not consume `--webui-footer-bg-alpha` or inherit footer opacity. Use a low fixed background alpha (about `0.94`), readable light-mode foreground/border/input colors, and matching dark-mode colors. Do not apply opacity to the whole panel or text.

6. Preserve localStorage compatibility: old state with only ordinary `steps` and `shareContext` must load with an empty scheduled track; saving must retain both tracks.

## Task 6: Update Cycle API client and synchronize text/vision lifecycle + recovery

**Complexity/model:** high frontend integration task; reuse the Task 5 agent via follow-up with high reasoning. This task owns both pages and must keep their glue code behaviorally synchronized, while preserving the vision-only camera release handling.

**Files:**

- Modify `frontend/src/lib/api.ts`.
- Modify `frontend/src/lib/cycle-types.ts` if wire translation requires a same-agent adjustment.
- Modify `frontend/src/app/page.tsx`.
- Modify `frontend/src/app/vision/page.tsx`.
- Modify `frontend/src/components/ImageUploader.tsx` if its Cycle callback shape changes.
- Add/update `frontend/src/lib/api.test.mts` and any page-level Cycle tests available in the repo.

**Steps:**

1. Translate frontend state to wire format explicitly. `startCycle` must send `scheduled_messages`, `timezone`, and a client-generated or server-returned `cycle_run_id` as specified by the backend contract; do not send local React-only fields or timeline projection state into the LLM message body.

2. Extend the API client with start/attach/reconnect support. A Cycle SSE disconnect must retain the run ID and reconnect cursor, call the attach endpoint, replay missed events in sequence order, and deduplicate already-rendered events. Browser refresh may recover only while the backend process still has the run; backend restart surfaces a clear expired/not-found state.

3. Keep the Cycle stream exempt from generic inactivity timeout behavior. Heartbeats must reset transport liveness, and the client must not abort a long-lived active Cycle merely because no content delta arrived during a wait or Agent operation.

4. Update both pages’ `handleCycleStart`, stop, event reducer, run bookkeeping, user-bubble projection, image projection, and completion/error cleanup with the same ordinary/scheduled event semantics. Scheduled dispatch must render as a distinct top-level scheduled message while ordinary `message` bubbles remain unchanged.

5. Preserve the vision page’s camera stream generation guards and delayed release behavior. Text page and vision page must agree on session/run identity, cursor handling, and terminal-state cleanup; only camera-specific work may differ.

6. Add client tests for timezone inclusion, scheduled payload shape, reconnect cursor, replay dedupe, heartbeat handling, stream inactivity exemption, expired run response, and ordinary/scheduled projection ordering.

## Task 7: End-to-end automated verification and targeted compatibility fixes

**Complexity/model:** medium verification/fix task; use the same frontend integration agent for frontend failures and the same backend integration agent for backend failures through follow-ups. This is automated verification only, not a code review.

**Steps:**

1. Run the focused backend tests:

   ```bash
   ./.sglang/bin/python -m pytest tests/test_cycle_scheduler.py tests/test_cycle_config.py tests/test_cycle_executor.py tests/test_cycle_recovery.py tests/test_cycle_api.py
   ```

   If the environment’s configured Python executable differs, use the repo’s documented uv environment without changing dependencies.

2. Run the existing backend regression subset for session hubs, agent lifecycle, and timeout cleanup that is directly affected by Cycle ownership.

3. Run frontend unit tests/type-check/build using the scripts declared in `frontend/package.json` (inspect the scripts first; do not invent package-manager commands or install dependencies). Verify both `/` and `/vision` compile.

4. Fix only concrete test/type/build failures caused by the implementation. Re-run each previously failing command and record the exact passing command/output in the final report.

5. Perform a read-only file/status inspection without git commands if needed, then report changed files, verification evidence, and any known limitation. Do not launch a review agent, request a code review, or claim unverified success.

## Execution order and delegation

Execute serially because backend lifecycle and frontend wire contracts are shared state:

```text
Task 1 -> Task 2 -> Task 3 -> Task 4 -> Task 5 -> Task 6 -> Task 7
```

Reuse the same backend agent for Tasks 1–2, the same architecture agent for Tasks 3–4, and the same frontend agent for Tasks 5–7. Use a lower reasoning level for pure validation/config work, medium for deterministic UI/domain work, and high for run registry, arbiter, and reconnect integration. No implementation subagents should edit overlapping files concurrently. No review phase is included by explicit user instruction.
