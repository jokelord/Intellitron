# Stable Single-ID Child Lifecycle Safety Design

**Date:** 2026-08-29  
**Status:** Approved for implementation  
**Scope:** Manager-created child Agent identity, execution quiescence, reuse admission, abnormal termination, explicit child abort, Hub producer ownership, and terminal status consistency.

## 0. Precedence and Compatibility

This design refines the one-public-session-identity rule in `2026-08-22-universal-agent-session-hub-recovery-design.md` for manager-created children.

It supersedes two older child-specific details:

- the separation of browser-facing `child_run_id` from backend `child_session_id` in `2026-07-06-manager-agent-parallel-dispatch-design.md`;
- the requirement in AGENTS.md item 38 to remove the child `AgentSession` and manager-to-child mapping after explicit child abort.

For the second item, “fresh context” now means a fresh Agent/model context inside the stable child session shell. It does not mean a new child identity, a new Hub, or a removed mapping. The targeted-abort, worker-barrier, bounded-replay-audit, sibling-isolation, manager-isolation, and TTL cleanup requirements in that AGENTS.md item remain binding. Bounded Hub retention is intentionally deferred to its own design, as stated in Section 11.

## 1. Goal

Make repeated dispatches to the same manager-created child safe while preserving that child's prior context after ordinary completed turns. A child has one stable identity for its entire lifetime. The implementation must not add a child-dispatch execution identity merely to distinguish sequential dispatch attempts.

The design must preserve the unified Session Hub model:

- one stable child session identity;
- one append-only child Hub journal;
- at most one active producer;
- sequential dispatches represented by producer generations and assignment anchors, not new child IDs;
- subscriber-independent execution and replay;
- model memory kept separate from UI timeline and Hub records.

## 2. Confirmed Problems

### 2.1 Abnormal terminal metadata can precede worker quiescence

`_run_basic_stream()` creates a run scope and starts a daemon worker. The worker owns `finish_run()`. The timeout, cancellation, and streamed-error paths can nevertheless publish a child terminal event, release the Hub producer, set `ChildAgentRun.terminal_event`, and return to the manager before that worker exits.

Consequences include:

- a terminal child whose `AgentSession.run_in_progress` is still true;
- an upstream request continuing after the child has been reported terminal;
- old worker cleanup overlapping a later dispatch;
- concurrent mutation of one child's Agent memory, state, request registry, and abort flags.

### 2.2 Same-child admission does not require quiescence

The current reuse path resolves the stable child session, clears its abort state, claims a new Hub producer, and replaces `child_agent_runs[child_run_id]` without first proving that the prior run scope and worker have exited.

Clearing abort before the admission checks finish is particularly unsafe: even a dispatch that later receives `SessionBusyError` can remove the abort signal needed by the old worker.

### 2.3 Explicit child abort changes the identity on retry

The current abort disposal path removes the manager-to-child mapping but leaves the old child `AgentSession` in `SessionStore` for replay. A later dispatch finds the deterministic base ID occupied and creates a UUID-suffixed child session ID. That behavior violates the single-ID requirement.

Fresh model context after explicit child abort and stable child identity are separate requirements. Freshness must be implemented by resetting the model context inside the stable child session shell, not by generating a replacement identity.

### 2.4 Child admission has an incomplete rollback boundary

Producer claim and child-run registration happen before the first child assignment event and manager pending update. An exception after claim can leave an active producer, stale run metadata, and a terminal event that is never set.

### 2.5 Timeout has inconsistent terminal semantics

The manager records `timeout`, while the child Hub emits a generic error and the child transcript projects it as `failed`. The manager-wide `abort_session()` terminal filter also omits `timeout` and can target an already terminal child again.

## 3. Identity Model

### 3.1 One canonical child identity

For a manager and manager-supplied child label, `_make_child_identity_id(manager_session_id, child_id)` produces the canonical child identity. That value is stable until the manager session is explicitly deleted or expires.

The canonical value is used for:

- the child `AgentSession.session_id`;
- the child page URL and Session Hub lookup;
- the `child_agent_runs` current-state entry;
- manager child status updates;
- child abort targeting.

No collision fallback may append a random suffix for a previously known logical child. A genuinely unrelated live session occupying the deterministic value remains an admission error; it must not silently create a second identity for the same child.

### 3.2 Compatibility fields are aliases, not identities

The existing wire field `child_run_id` remains for API and frontend compatibility, but its value is the canonical child identity. It does not identify one execution attempt.

Inside the backend, `ChildAgentRun.child_session_id` is redundant because it must always equal `ChildAgentRun.child_run_id`. The implementation should remove that stored field and resolve the `AgentSession` by the canonical `child_run_id`.

The manager tool's `child_id` argument remains the manager-scoped human-readable label. `call_id` identifies one model tool call, and `dispatch_id` groups manager UI updates for one turn. Neither is a second child identity, and neither may be used to create a child context.

### 3.3 Per-dispatch state without a per-dispatch ID

Each accepted dispatch may create a new `ChildAgentRun` object containing the current task, status, timestamps, terminal event, and exact Hub producer token. Object identity and the opaque existing producer token protect callbacks from stale ownership. No new execution ID or generation ID is added to application data.

Hub generation remains an internal property of `AgentSessionHub` and must not be copied into child URLs, API identity fields, frontend state, manager observations, or model prompts.

The existing upstream `request_id` and `AgentSession.begin_run()` scope token remain necessary for targeted provider cancellation and worker barriers. They are transient infrastructure tokens that already exist outside the child identity model. They are not stored or exposed as another child/run identity, and this design adds no replacement for them.

## 4. Stable Session Shell and Model Context

The child `AgentSession` is a durable identity and Hub shell. Ordinary successful dispatches reuse the existing Agent instance and memory so the child retains prior knowledge.

The following remain stable across sequential dispatches:

- `AgentSession.session_id`;
- the `AgentSession` object;
- `AgentSessionHub` and its Session Hub runtime registration;
- the manager-to-child mapping;
- the child page URL;
- Hub sequence allocation and existing replay records.

An explicit child abort resets only the model-execution context after the captured worker barrier completes. The reset must:

1. release images and other resources owned by the old Agent context;
2. discard the old Agent memory, Agent state, compact snapshots, accumulated model output, and request-local control state;
3. build a fresh `basic_agent` and runtime context bound to the existing child `AgentSession`;
4. leave the Hub, Hub journal, Session Hub registration, canonical identity, and manager mapping intact;
5. leave no active request, run scope, producer, abort flag, or Answer Now state.

Timeout or manager-driven cancellation continues to use the existing aborted-run rollback semantics: the interrupted attempt is rolled back to its baseline, and the prior successfully completed child context remains reusable after quiescence. Natural failure retains current behavior unless its worker marks the session aborted.

Manager session disposal and TTL expiry still delete the complete child session shell, Hub journal, mapping, model context, and request metadata after the disposal barrier.

## 5. Lifecycle and Ordering

### 5.1 Admission invariant

Before a sequential dispatch can clear abort state or claim a producer, all of the following must be true for the stable child session:

- the current `ChildAgentRun`, if any, has completed its worker barrier;
- `ChildAgentRun.terminal_event` is set;
- `AgentSession.capture_active_run_scopes()` is empty;
- `AgentSession.run_in_progress` is false;
- `AgentSessionHub.has_active_producer` is false;
- no explicit child reset or manager disposal is mutating the session.

If any condition is false, admission returns `SessionBusyError`. A rejected admission must not clear abort state, reset Answer Now state, modify memory, claim a producer, replace the current run object, or append events.

The existing `_child_context_creation_lock` becomes the short child lifecycle admission lock. It protects session selection, same-child readiness checks, context reset, producer claim, and current-run installation. It is never held while the child executes or while awaiting a worker, so distinct children continue to run concurrently.

The lock ordering is fixed:

1. `_child_context_creation_lock`;
2. `_child_agent_lock`;
3. Hub internal locks, entered only through Hub methods.

No code may acquire these locks in the reverse order.

### 5.2 Accepted execution

After admission succeeds:

1. create the new `ChildAgentRun` object with the stable child ID;
2. claim the exact Hub producer token;
3. install that exact run object as the current map value;
4. append the child `manager_assigned_task` anchor;
5. append the manager `pending` update;
6. mark the child running and start `_run_child_agent_to_final_report()`.

`clear_abort()`, `reset_answer_now()`, and `abort_manager.clear()` occur only after readiness checks pass and immediately before producer claim.

### 5.3 Normal terminal path

The normal child stream reaches its queue sentinel only after the worker has called `finish_run()`. The dispatch then:

1. confirms the run object still owns the current map entry;
2. publishes the completed or failed child terminal payload with the run's exact producer token;
3. closes and releases that producer;
4. appends the manager terminal update;
5. sets `ChildAgentRun.terminal_event` last;
6. returns the blackboard outcome.

`terminal_event` therefore means both “terminal metadata is committed” and “the original worker is quiescent.”

### 5.4 Timeout, cancellation, and early stream error

Abnormal completion uses one shared cleanup routine. It must:

1. pin the exact `ChildAgentRun` and `AgentSession` objects;
2. capture active run scopes and the current registered upstream request ID before abort mutation;
3. set session, session-abort-manager, and run-scope abort state;
4. call targeted `abort_request_by_id(..., fallback_to_abort_all=False)` when a request ID exists;
5. wait for all captured scopes and the original worker to finish;
6. verify the current run map still points to the pinned run object;
7. publish exactly one terminal payload;
8. close and release the exact producer token;
9. publish the manager terminal status;
10. set `terminal_event` last.

The producer remains claimed during the worker wait. That ownership prevents another same-child dispatch from passing admission. Cleanup awaits must remain effective after `CancelledError`; cancellation may determine the final status, but it may not skip the barrier.

Natural streamed errors do not require an upstream abort if the request is already unregistered, but they still require the run-scope barrier before terminal publication.

### 5.5 Explicit child abort

`abort_child_agent_run()` targets only the current run object under the stable child ID. It follows the same request capture, targeted abort, run-scope wait, terminal publication, and producer release ordering.

After the worker barrier and terminal publication, it resets the model context in the stable session shell while holding the short lifecycle lock. It does not remove the manager-to-child mapping and does not generate a new child ID. A later dispatch uses the same ID with a fresh Agent memory.

Repeated abort calls after terminal are idempotent. They do not abort a later dispatch that has replaced the current `ChildAgentRun` object.

## 6. Stale Callback Protection

Child event and terminal helpers must receive the exact `ChildAgentRun` object, not only its stable string ID.

Before writing, a helper verifies:

- `child_agent_runs[child_run.child_run_id] is child_run`;
- `sessions.get(child_run.child_run_id) is child_run.child_session_identity`;
- the run still holds its exact producer token;
- the Hub reports that token as the active producer.

A stale non-terminal callback is rejected without mutating the current run or Hub. A stale terminal retry may reconcile only the terminal phases already committed by its own token; it must never close or release a replacement producer.

This uses existing object references and producer tokens. It does not introduce another identifier.

## 7. Admission Rollback

Producer claim, current-run installation, assignment anchor, and manager pending publication form one admission operation. The two Hubs cannot provide a distributed atomic transaction, so rollback guarantees resource safety and deterministic visible terminal state rather than erasing already appended journal records.

If failure occurs before child execution starts:

- no run scope or model request is started;
- if no child event was appended, close and release the claimed producer without inventing transcript content;
- if the assignment anchor was appended, append one admission-failure terminal payload, then close and release the producer;
- remove or terminalize the current run entry only if the map still points to the same `ChildAgentRun` object;
- preserve the stable child session shell and manager mapping;
- return a failed tool outcome so the manager blackboard records the failure;
- never leave an active producer or unset terminal event for a terminal run.

Manager timeline publication failure must not start an invisible child execution.

## 8. Terminal Status Contract

The canonical statuses remain:

```text
pending | running | completed | failed | aborted | timeout
```

Child terminal payloads retain the existing top-level event types for compatibility, but include canonical status explicitly:

- completed: `type="done", status="completed"`;
- failed: `type="error", status="failed"`;
- aborted: `type="aborted", status="aborted"`;
- timeout: `type="error", status="timeout"`.

The child transcript reducer prioritizes the explicit status, so timeout is displayed as timeout instead of failed. All backend terminal-state sets, including `abort_session()`, include `timeout`.

No timeout-specific child identity or event protocol is added.

## 9. Unified Timeline and Prompt Isolation

Manager child status continues through `manager_child_agent_update` timeline events. Child transcript events continue through the child Session Hub. No new SSE protocol is introduced.

The manager blackboard continues to contain child status and final reports only. It must not receive:

- Hub events or producer tokens;
- child session metadata;
- lifecycle locks or barrier diagnostics;
- frontend projection state;
- replay cursors or event sequences.

None of those fields may enter manager or child LLM prompts. Prior child Agent memory is reused only through canonical `memory.steps` on safe completed-turn reuse.

## 10. Testing Requirements

Backend regression tests must cover:

1. a real worker-owned run scope remains active after timeout is requested, and no child terminal event or producer release occurs until `finish_run()`;
2. timeout captures the registered request ID and calls targeted abort with `fallback_to_abort_all=False`;
3. immediate same-child redispatch before the old barrier returns `SessionBusyError` without clearing abort;
4. same-child redispatch after the barrier reuses the exact session ID and exact session shell;
5. ordinary completed redispatch preserves prior Agent memory;
6. explicit child abort resets Agent memory but preserves the session ID, Session Hub, replay journal, child URL, and manager mapping;
7. stale callbacks holding the old `ChildAgentRun` object cannot append to or close a replacement producer;
8. an exception after producer claim leaves no active producer, no nonterminal dangling run, and no started worker;
9. timeout is projected consistently by manager metadata, child Hub payload, child transcript reducer, and `abort_session()` terminal filtering;
10. different child IDs still enter execution concurrently;
11. same-turn duplicate child labels are still rejected before launch;
12. existing replay, terminal retry, producer handoff, manager disposal, TTL cleanup, and exact-object replacement protections remain green.

Frontend tests must verify that a child page using the stable ID replays multiple sequential assignments, displays timeout distinctly, and continues to merge manager cards by the existing manager dispatch coordinates.

Tests must assert lifecycle conditions and events rather than relying on arbitrary sleeps. A controlled worker gate may hold and release `finish_run()` deterministically.

## 11. Out of Scope

This design does not implement bounded Hub retention. The existing `manager_agent_child_event_buffer_limit` mismatch and Hub journal retention policy will be handled by a separate design and implementation plan.

Also out of scope:

- persistent recovery across backend restart;
- parallel executions of the same child;
- changing the manager dispatch tool schema;
- changing child tool permissions;
- compressing child final reports in the manager blackboard;
- changing normal Session Hub subscriber generation semantics;
- unrelated orchestration-card presentation changes;
- a new public run, execution, generation, or recovery ID.

## 12. Acceptance Criteria

The design is complete when all of the following are true:

1. The same manager/child pair always resolves to one stable child ID without UUID suffixes.
2. Sequential completed dispatches reuse prior child Agent memory.
3. Explicit child abort produces a fresh Agent context under that same ID.
4. No terminal event, producer release, manager terminal update, or blackboard outcome precedes the original worker barrier.
5. No rejected same-child admission clears or changes the old run's abort state.
6. No stale callback or cleanup operation can mutate a later dispatch under the same ID.
7. Admission failure leaves no active producer, orphan execution metadata, or invisible child worker.
8. Timeout has one consistent status across backend and frontend.
9. Different children remain genuinely parallel.
10. Unified Hub replay and model-prompt isolation remain intact.
