# Stable Single-ID Child Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sequential dispatches to one stable child identity safe, preserve prior child memory after completed turns, rebuild only the Agent/model context after explicit child abort, and prevent terminal metadata or same-child reuse before the original worker is quiescent.

**Architecture:** Keep one deterministic child `AgentSession` and one Session Hub for the lifetime of a manager/child pair. Represent each accepted dispatch with a new `ChildAgentRun` object but no new execution ID; use exact object identity plus the existing opaque Hub producer token to reject stale callbacks. Abnormal termination retains producer ownership until targeted abort and the captured run-scope barrier complete, after which terminal publication, optional context reset, and future admission occur in a fixed order.

**Tech Stack:** Python 3.12, asyncio, threading conditions/events, FastAPI, pytest/pytest-asyncio, TypeScript, Node test runner, Next.js 14.

**Spec:** `docs/superpowers/specs/2026-08-29-stable-single-id-child-lifecycle-design.md`

## Global Constraints

- One manager/child pair has one deterministic canonical child ID for its lifetime; never append a UUID suffix for a previously known child.
- `child_run_id` is a compatibility name for the canonical child/session ID, not an execution-attempt identity.
- Do not add a child execution ID, generation ID, recovery ID, or second child event store.
- Existing upstream `request_id`, run-scope tokens, and opaque Hub producer tokens remain transient infrastructure tokens only.
- Sequential completed dispatches reuse the existing child Agent memory.
- Explicit child abort rebuilds a fresh Agent/model context inside the same `AgentSession` shell and preserves the stable ID, Hub, journal, runtime registration, URL, and manager mapping.
- Timeout, cancellation, abort, and early streamed error cannot publish terminal state, release producer ownership, or return a blackboard outcome before the original worker barrier completes.
- Same-child admission checks quiescence before clearing abort or mutating any session state.
- Different child IDs remain concurrently executable; no lock may be held for the duration of child execution.
- All targeted abort calls use `fallback_to_abort_all=False`; manager and sibling children remain untouched.
- Manager/child Hub events, lifecycle metadata, producer tokens, and replay cursors never enter LLM prompts or manager blackboard observations.
- Bounded Hub retention is out of scope for this plan.
- Follow strict TDD: write each behavioral regression first, run it and capture the expected failure, then write the minimum production change and rerun to green.
- Do not run any Git command, create a branch/worktree, stage, or commit unless the user separately grants Git permission. Replace commit checkpoints with written task reports and independent review gates.

## File and Responsibility Map

- Modify `backend/agent/agent_runner.py`: canonical child identity, `ChildAgentRun` ownership, same-child admission, worker quiescence, targeted abnormal cleanup, stable-shell reset, admission rollback, terminal statuses.
- Modify `backend/api/agent.py`: resolve child Session Hub by canonical `child_run_id` after removal of redundant `child_session_id` storage.
- Modify `tests/test_manager_agent_dispatch.py`: deterministic red/green lifecycle regressions and updated stable-ID expectations.
- Modify `tests/test_session_store_cleanup.py`: canonical-ID construction and manager disposal/TTL assertions after removing redundant child-session storage.
- Modify `tests/test_agent_session_recovery_api.py`: canonical child ID fixtures.
- Modify `tests/test_agent_stop_api.py`: canonical child ID fixtures and timeout terminal filtering.
- Modify `frontend/src/lib/child-agent-transcript.ts`: expose `timeout` and prioritize explicit terminal status.
- Modify `frontend/src/lib/api.test.mts`: child transcript timeout and multi-turn stable-ID behavior.
- Modify `frontend/src/lib/timeline-projection.ts`: remove the unused `child_session_id` timeline identity alias.
- Modify `AGENTS.md`: align item 38 with stable shell + fresh Agent/model context semantics while retaining targeted abort, barrier, bounded replay audit, and TTL requirements.

---

### Task 1: Canonical Child Identity and Exact-Run Hub Ownership

**Files:**
- Modify: `backend/agent/agent_runner.py:2164-2198, 2917-3070, 3143-3160, 5998-6145, 6245-6263, 7566-7617`
- Modify: `backend/api/agent.py:1355-1374`
- Modify: `frontend/src/lib/timeline-projection.ts:5-22`
- Modify: `tests/test_manager_agent_dispatch.py`
- Modify: `tests/test_session_store_cleanup.py`
- Modify: `tests/test_agent_session_recovery_api.py`
- Modify: `tests/test_agent_stop_api.py`

**Interfaces:**
- Produces: `ChildAgentRun` with one stored canonical identity field, `child_run_id: str`.
- Produces: `_record_child_agent_event(child_run: ChildAgentRun, payload: dict[str, Any]) -> None`.
- Produces: `_record_child_terminal_event(child_run: ChildAgentRun, payload: dict[str, Any]) -> None`.
- Produces: exact-object/current-producer validation used by Tasks 2–4.
- Consumes: existing `child_agent_runs: dict[str, ChildAgentRun]`, `ChildAgentRun.child_session_identity`, and Hub producer token.

- [ ] **Step 1: Write a failing canonical-ID construction test**

Add a helper in `tests/test_manager_agent_dispatch.py` that constructs a run without a second child session ID:

```python
def _child_run_for_session(
    session: AgentSession,
    *,
    manager_session_id: str = "manager-session",
    child_name: str = "reader",
    status: str = "pending",
) -> ChildAgentRun:
    return ChildAgentRun(
        child_run_id=session.session_id,
        manager_session_id=manager_session_id,
        dispatch_id="dispatch-1",
        child_name=child_name,
        profile_name="basic_agent",
        assigned_task="Inspect the independent source carefully.",
        status=status,
        child_session_identity=session,
    )
```

Use this helper in a test that registers the child Hub and calls `stream_child_agent_events(child_run.child_run_id)`. The test must fail before implementation because `ChildAgentRun.__init__` still requires `child_session_id`.

- [ ] **Step 2: Write a failing stale-run callback test**

Create two `ChildAgentRun` objects with the same `child_run_id` and exact same `AgentSession`, but different producer tokens across sequential Hub generations. Replace `runner.child_agent_runs[stable_id]` with the second object, then call:

```python
with pytest.raises(RuntimeError, match="stale"):
    runner._record_child_agent_event(
        old_run,
        {"type": "content", "content": "must-not-append"},
    )
```

Assert the stale payload is absent from the Hub journal and the replacement producer remains active. The production change that this test catches is a recorder that resolves the current run only by the reusable string ID.

- [ ] **Step 3: Run the two new tests and verify RED**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_manager_agent_dispatch.py::test_child_run_uses_one_canonical_session_identity \
  tests/test_manager_agent_dispatch.py::test_stale_child_run_object_cannot_append_to_replacement_producer \
  -q
```

Expected: FAIL because `child_session_id` is required and/or the recorder accepts only a string ID and dynamically resolves the replacement run.

- [ ] **Step 4: Remove redundant stored child-session identity**

Change the dataclass shape to:

```python
@dataclass
class ChildAgentRun:
    child_run_id: str
    manager_session_id: str
    dispatch_id: str
    child_name: str
    profile_name: str
    assigned_task: str
    # existing non-identity fields remain unchanged
```

Replace every `child_run.child_session_id` lookup with `child_run.child_run_id`. Update constructors in backend tests and API tests. Do not add a property alias that preserves a second identity concept.

In `backend/api/agent.py`, resolve the Hub with:

```python
entry = runtime.resolve(child_run.child_run_id) if isinstance(runtime, SessionHubRuntime) else None
```

Remove `TimelineEvent.child_session_id` from `frontend/src/lib/timeline-projection.ts`; it is unused and must not remain a parallel identity field.

- [ ] **Step 5: Bind event publication to the exact run object**

Implement the recorder contract in this order:

```python
def _record_child_agent_event(
    self,
    child_run: ChildAgentRun,
    payload: dict[str, Any],
) -> None:
    with self._child_agent_lock:
        if self.child_agent_runs.get(child_run.child_run_id) is not child_run:
            raise RuntimeError("child Agent run is stale")
        child_session = self.sessions.get(child_run.child_run_id)
        if child_session is not child_run.child_session_identity:
            raise RuntimeError("child Agent session generation is stale")
        producer_token = child_run.event_hub_producer_token
    if producer_token is None:
        raise RuntimeError("child Agent Hub producer is unavailable")
    if child_session.event_hub.capture_active_producer() is not producer_token:
        raise RuntimeError("child Agent producer token is not current")
    event = {**payload, "seq": child_session.event_hub.last_sequence}
    child_session.event_hub.append(event, producer_token=producer_token)
```

Apply the same exact-object validation to `_record_child_terminal_event`. Preserve its existing append/close/release reconciliation fields and idempotency. Update every production and test caller to pass the pinned `ChildAgentRun` object.

Preserve the current child payload `seq` convention in this task. Correcting or redesigning payload-local sequencing is outside this approved lifecycle scope and must not be bundled into the identity refactor.

Do not change the Hub's internal generation implementation.

- [ ] **Step 6: Run focused canonical-ID and Hub ownership tests**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_manager_agent_dispatch.py \
  tests/test_agent_session_hub.py \
  tests/test_agent_session_recovery_api.py \
  tests/test_agent_stop_api.py \
  tests/test_session_store_cleanup.py \
  -q
```

Expected: all selected tests pass; no constructor retains independently supplied `child_session_id`.

- [ ] **Step 7: Self-review and write the task report**

Confirm that all session lookups use the canonical ID, all callbacks carry the exact run object, stale callbacks cannot touch the replacement token, and no new identity field was introduced. Record RED/GREEN commands and output in the assigned report file. Do not run Git.

---

### Task 2: Worker Barrier and Quiescent Same-Child Admission

**Files:**
- Modify: `backend/agent/agent_runner.py:2395-2459, 3162-3383, 4638-5297`
- Modify: `tests/test_manager_agent_dispatch.py`

**Interfaces:**
- Consumes: exact-object recorders from Task 1.
- Produces: `_wait_for_child_run_quiescence(...)` shared abnormal-cleanup helper.
- Produces: same-child readiness check performed before any abort reset.
- Preserves: different-child parallel execution and one-producer Hub semantics.

- [ ] **Step 1: Write a deterministic timeout barrier regression**

Add a controlled worker fixture with `threading.Event` gates. The fake child coroutine must create a real `AgentSession.begin_run()` scope and arrange for a background worker to own `finish_run(scope_id)`. Start dispatch with a short configured timeout, but do not release the worker gate.

After timeout abort state is observable, assert:

```python
assert dispatch_task.done() is False
assert child_session.run_in_progress is True
assert child_session.event_hub.has_active_producer is True
assert child_run.terminal_event.is_set() is False
assert not any(
    payload.get("type") in {"done", "error", "aborted"}
    for payload in _child_hub_payloads(runner, child_run)
)
```

Release the worker, await dispatch, then assert timeout status, inactive scope, released producer, and terminal event set. This test catches terminal publication before `finish_run()`.

- [ ] **Step 2: Add targeted-abort evidence to the timeout regression**

Register `session_request_manager.set(stable_child_id, "req-child-timeout")` before timeout. Patch only the external `backend.llm_service.abort_request_by_id` call and assert the real dispatch cleanup invokes:

```python
targeted_abort.assert_called_once_with(
    "req-child-timeout",
    f"http://{runner.config.sglang.host}:{runner.config.sglang.port}",
    fallback_to_abort_all=False,
)
```

The test's primary assertions remain real session state, Hub ownership, and barrier ordering; the mock is limited to the external provider call.

- [ ] **Step 3: Write a rejected-admission abort-preservation regression**

While an old same-child run owns an active scope and producer, set the child session aborted and call a second dispatch with the same child label. Expect `SessionBusyError`, then assert:

```python
assert child_session.check_aborted() is True
assert runner.child_agent_runs[stable_id] is old_run
assert child_session.event_hub.capture_active_producer() is old_token
```

This catches the current `clear_abort()`-before-claim ordering.

- [ ] **Step 4: Run the three lifecycle regressions and verify RED**

Run the exact new test names with `pytest -q`. Expected failures:

- timeout dispatch completes before the worker gate is released;
- targeted provider abort is not called by timeout;
- rejected same-child admission clears the old abort flag.

- [ ] **Step 5: Implement a single abnormal quiescence helper**

Add an async helper with an explicit contract equivalent to:

```python
async def _wait_for_child_run_quiescence(
    self,
    child_run: ChildAgentRun,
    child_session: AgentSession,
    *,
    request_abort: bool,
) -> None:
    captured_scope_ids = child_session.capture_active_run_scopes()
    captured_request_id = self.get_current_request_id(child_run.child_run_id)
    if request_abort:
        child_session.abort()
        abort_manager.abort(child_run.child_run_id)
        child_session.abort_active_runs()
        await self._targeted_abort_disposal_request(
            child_run.child_run_id,
            captured_request_id,
        )
    await asyncio.to_thread(
        child_session.wait_for_run_scopes,
        captured_scope_ids,
    )
```

Use `request_abort=True` for timeout and cancellation/abort. Use `False` for a streamed error whose request is already terminal/unregistered. Preserve cancellation cleanup with an independently owned cleanup task, for example:

```python
cleanup_task = asyncio.create_task(
    self._wait_for_child_run_quiescence(
        child_run,
        child_session,
        request_abort=True,
    )
)
try:
    await asyncio.shield(cleanup_task)
except asyncio.CancelledError:
    await cleanup_task
    raise
```

The exact implementation may differ, but the cleanup operation must remain owned and awaited after the outer dispatch task receives cancellation; creating a shielded anonymous coroutine and abandoning its Task is not sufficient.

- [ ] **Step 6: Move terminal publication after quiescence**

Refactor `_dispatching_parallel_agent_for_session()` so every path determines status/error first, awaits the required barrier, then performs terminal publication, manager update, `terminal_event.set()`, and blackboard return in that order.

Do not release the producer inside the timeout handler. Do not set `terminal_event` anywhere before the worker barrier.

- [ ] **Step 7: Make same-child admission read-only until readiness passes**

Under the short `_child_context_creation_lock`, resolve the stable session and current run, then reject when any of these is true:

```python
current_run is not None and not current_run.terminal_event.is_set()
child_session.run_in_progress
bool(child_session.capture_active_run_scopes())
child_session.event_hub.has_active_producer
```

Only after all checks pass may the code call `clear_abort()`, `reset_answer_now()`, `abort_manager.clear()`, claim the next producer, and replace the map entry.

Never hold `_child_context_creation_lock` while awaiting the child or worker barrier.

- [ ] **Step 8: Run lifecycle tests GREEN, then run adjacent regressions**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_manager_agent_dispatch.py -q
./.sglang/bin/python -m pytest \
  tests/test_agent_stop_api.py \
  tests/test_session_store_cleanup.py \
  tests/test_agent_session_hub.py \
  -q
```

Expected: all pass with no hanging `asyncio.to_thread` waiter. If a test times out, report the exact test and stack; do not call the task green.

- [ ] **Step 9: Self-review and write the task report**

Document RED/GREEN evidence, confirm producer ownership remains active through the barrier, and confirm distinct children still execute concurrently. Do not run Git.

---

### Task 3: Stable-Shell Fresh Context After Explicit Child Abort

**Files:**
- Modify: `backend/agent/agent_runner.py:3072-3243, 3407-3485, 6177-6235`
- Modify: `tests/test_manager_agent_dispatch.py:548-979`
- Modify: `tests/test_session_store_cleanup.py`

**Interfaces:**
- Consumes: quiescent terminal semantics and short admission lock from Task 2.
- Produces: `_reset_child_model_context(child_session: AgentSession) -> None`.
- Preserves: exact `AgentSession`, Hub, Hub runtime entry, journal, stable URL, and manager mapping.
- Resets: Agent/model context and all model-owned mutable state after explicit child abort.

- [ ] **Step 1: Replace the old fresh-ID retry test with a failing stable-shell test**

The test must run a child, add recognizable prior memory/state, explicitly abort it while a controlled scope is active, release the worker barrier, and dispatch the same child label again.

Assert after abort and before redispatch:

```python
assert runner.sessions.get(stable_id) is original_child_session
assert runner.child_agent_sessions_by_key[(manager.session_id, child_name)] == stable_id
assert original_child_session.event_hub is original_hub
assert original_child_session.agent.memory.steps == []
assert original_child_session.agent.state == {}
```

Assert after redispatch:

```python
assert new_run.child_run_id == stable_id
assert runner.sessions.get(stable_id) is original_child_session
assert runner.sessions.get(stable_id).event_hub is original_hub
assert latest_assignment["reused_context"] is False
assert latest_assignment["prior_step_count"] == 0
```

Also assert earlier Hub transcript records remain replayable. This catches the current mapping removal and UUID-suffix fallback.

- [ ] **Step 2: Write a completed-turn memory reuse control test**

Dispatch the same child twice without explicit abort. Seed or produce one canonical memory step in the first turn, and assert the second assignment reports `reused_context=True`, `prior_step_count > 0`, and uses the same Agent instance. This prevents the fresh-context fix from accidentally resetting normal prior-child memory.

- [ ] **Step 3: Write a deterministic collision test**

Place a genuinely unrelated live `AgentSession` at the deterministic child ID while no manager mapping establishes ownership. Dispatching that child must raise `SessionBusyError`. Assert no UUID-suffixed session was created and no mapping was installed. The old tests expecting `-{uuid}` fallback must be updated to the single-ID contract.

- [ ] **Step 4: Run the stable-shell tests and verify RED**

Expected: the explicit-abort retry currently receives a different suffixed ID/mapping behavior, and the unrelated collision currently allocates a suffix rather than rejecting admission.

- [ ] **Step 5: Implement focused model-context reset**

Add an instance helper that may run only after quiescence and with no active producer:

```python
def _reset_child_model_context(self, child_session: AgentSession) -> None:
    if child_session.run_in_progress or child_session.event_hub.has_active_producer:
        raise SessionBusyError(child_session.session_id)
    self._release_session_resources(child_session, set(), release_hub=False)
    runtime_context = self._build_runtime_context_for_profile(
        "basic_agent",
        session=child_session,
    )
    child_session.agent = self._build_profile_agent(
        "basic_agent",
        session=child_session,
        runtime_context=runtime_context,
    )
    child_session.active_agent = None
    child_session.current_agent_name = "basic_agent"
    child_session.runtime_context = runtime_context
    child_session.has_run = False
    child_session.has_shared_history = False
    child_session.clear_abort()
    child_session.reset_answer_now()
    abort_manager.clear(child_session.session_id)
    session_request_manager.cleanup(child_session.session_id)
```

Preserve the exact Hub and Session Hub registration. Do not call `SessionStore.delete*()`, `event_hub.release()`, or remove the child mapping.

- [ ] **Step 6: Invoke reset only for explicit child abort**

After `abort_child_agent_run()` has awaited the captured run scopes and the original dispatch terminal barrier, acquire `_child_context_creation_lock`, verify the current run and session objects are still exact, then reset the model context. Return idempotently if a later run already replaced the current object.

Timeout and manager cancellation must not call this fresh-context reset; they retain prior completed memory after the interrupted turn rollback.

- [ ] **Step 7: Remove UUID collision fallback**

For an allocated deterministic child ID:

- reuse the mapped stable shell when ownership is established and readiness passes;
- create it once when unoccupied;
- raise `SessionBusyError` when an unrelated live session or retained incompatible Hub occupies the deterministic value;
- never append a random suffix.

- [ ] **Step 8: Run focused and cleanup regressions GREEN**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_manager_agent_dispatch.py \
  tests/test_session_store_cleanup.py \
  tests/test_agent_session_recovery_api.py \
  -q
```

Expected: stable-shell tests pass, completed memory reuse remains intact, manager disposal still deletes the exact child shell, and unrelated sessions remain untouched.

- [ ] **Step 9: Self-review and write the task report**

Confirm reset code does not release or replace the Hub, normal completion does not reset memory, and no suffix-generation branch remains. Include RED/GREEN evidence. Do not run Git.

---

### Task 4: Child Admission Rollback and Stale Cleanup Isolation

**Files:**
- Modify: `backend/agent/agent_runner.py:2920-3040, 3123-3349`
- Modify: `tests/test_manager_agent_dispatch.py:1027-1181` and new admission-fault tests

**Interfaces:**
- Consumes: exact run-object ownership from Task 1 and short lifecycle lock from Tasks 2–3.
- Produces: `_rollback_child_admission(...)` or an equivalent single cleanup block.
- Guarantees: no worker starts after failed admission publication; no active producer or dangling nonterminal run remains.

- [ ] **Step 1: Write a failing fault-before-assignment test**

Patch the child Hub append call to raise before mutation immediately after producer claim. Invoke dispatch and assert:

```python
assert child_session.event_hub.has_active_producer is False
assert not child_session.run_in_progress
assert runner.child_agent_runs.get(stable_id) is None or runner.child_agent_runs[stable_id].terminal_event.is_set()
assert child_runner_call_count == 0
```

The failure must be returned as a failed dispatch/tool outcome or raised through the established tool failure boundary; it must not leave an accepted invisible worker.

- [ ] **Step 2: Write a failing fault-after-assignment test**

Allow the `manager_assigned_task` append, then force `_append_manager_child_update(..., "pending")` to raise. Assert:

- the child worker never starts;
- the visible assignment anchor is followed by exactly one failed terminal payload;
- producer ownership is closed and released;
- the run's terminal event is set;
- the stable session shell and mapping remain available for a later safe retry.

- [ ] **Step 3: Write a stale rollback isolation test**

Arrange for an old admission rollback callback to run after the map holds a replacement `ChildAgentRun` object with the same stable ID. Assert the rollback does not remove the replacement map entry, close its producer, clear its Agent memory, or remove the stable mapping.

- [ ] **Step 4: Run the admission-fault tests and verify RED**

Expected: current code leaks the claimed producer and/or leaves current metadata without terminal cleanup.

- [ ] **Step 5: Implement rollback with exact-object guards**

Wrap producer claim, map installation, child anchor publication, and manager pending publication in a `try` block. Track whether the child assignment anchor was appended.

On failure before worker start:

```python
if assignment_appended:
    # publish one failed terminal with the exact run/token
else:
    # close/release the exact token without transcript invention

with self._child_agent_lock:
    if self.child_agent_runs.get(stable_id) is child_run:
        # retain a terminal run for replay or remove only this exact object
```

Always set `terminal_event` for a retained terminal run. Never call `_release_session_resources()` during admission rollback; the stable shell remains reusable.

If manager pending publication fails, return or propagate a failed tool outcome through the existing `ToolResult` error path. Do not start `_run_child_agent_to_final_report()`.

- [ ] **Step 6: Preserve terminal retry reconciliation**

Run the existing mutated-then-raised append/close/release tests. Ensure `_record_child_terminal_event` still appends exactly one terminal record and never releases a replacement producer.

- [ ] **Step 7: Run Task 4 GREEN and adjacent Hub suites**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_manager_agent_dispatch.py \
  tests/test_agent_session_hub.py \
  tests/test_session_store_cleanup.py \
  -q
```

Expected: admission-fault tests and existing terminal reconciliation tests pass.

- [ ] **Step 8: Self-review and write the task report**

Confirm every producer claim has a deterministic release path, rollback is exact-object guarded, and no rollback branch touches a replacement run. Include RED/GREEN evidence. Do not run Git.

---

### Task 5: Timeout Status Contract, Frontend Projection, and Documentation Alignment

**Files:**
- Modify: `backend/agent/agent_runner.py:3041-3069, 3319-3334, 6052-6067, 6342-6348, 7566-7617`
- Modify: `frontend/src/lib/child-agent-transcript.ts:1-23, 165-193`
- Modify: `frontend/src/lib/api.test.mts:755-820`
- Modify: `tests/test_manager_agent_dispatch.py:456-547`
- Modify: `tests/test_agent_stop_api.py`
- Modify: `AGENTS.md:309`

**Interfaces:**
- Produces: canonical terminal status set containing `completed`, `failed`, `aborted`, and `timeout`.
- Produces: child terminal payloads with explicit `status` while retaining compatible top-level event types.
- Produces: frontend `ChildTranscriptStatus` including `timeout`.
- Consumes: stable child ID and terminal ordering from Tasks 1–4.

- [ ] **Step 1: Write a failing backend timeout-status test**

Extend the deterministic timeout test to assert the final child payload is:

```python
{
    "type": "error",
    "status": "timeout",
    "child_run_id": stable_id,
    "call_id": "call-timeout",
    "error": expected_timeout_message,
}
```

Ignore unrelated payload keys such as the Hub sequence wrapper. The current code fails because it omits explicit status.

- [ ] **Step 2: Write a failing manager abort filter test**

Create a manager with one timeout child and one running child, register request IDs for both, and call `abort_session(manager_id)`. Assert only the running child's request is targeted and the timeout child's abort flag is unchanged.

- [ ] **Step 3: Write a failing frontend transcript timeout test**

In `frontend/src/lib/api.test.mts`, apply:

```typescript
const next = applyChildTranscriptEvent(initialChildTranscriptState(), {
  type: 'error',
  status: 'timeout',
  error: 'Child agent timed out after 30 seconds',
});

assert.equal(next.status, 'timeout');
assert.equal(next.turns.at(-1)?.status, 'timeout');
assert.equal(next.error, 'Child agent timed out after 30 seconds');
```

The production change caught is the reducer's unconditional `error -> failed` mapping.

- [ ] **Step 4: Run backend and frontend tests and verify RED**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_manager_agent_dispatch.py::test_child_timeout_waits_for_worker_barrier_and_targets_request \
  tests/test_manager_agent_dispatch.py::test_abort_manager_ignores_terminal_timeout_child \
  -q

cd frontend && node --test --experimental-strip-types \
  --test-name-pattern="child transcript.*timeout" \
  src/lib/api.test.mts
```

Expected: missing backend status, repeated timeout abort, and frontend `failed` mapping failures.

- [ ] **Step 5: Centralize backend terminal status semantics**

Define one module-level constant:

```python
CHILD_TERMINAL_STATUSES = frozenset({"completed", "failed", "aborted", "timeout"})
```

Use it in child abort idempotency, disposal, TTL protection, manager disposal, and `abort_session()`. Do not leave ad hoc terminal sets that can diverge again.

Generate terminal payloads with explicit status:

```python
terminal_payload = {
    "type": terminal_type,
    "status": child_run.status,
    "child_run_id": child_run.child_run_id,
    "call_id": child_run.call_id,
}
```

- [ ] **Step 6: Update the child transcript reducer**

Extend the status union:

```typescript
export type ChildTranscriptStatus =
  | 'connecting'
  | 'running'
  | 'completed'
  | 'failed'
  | 'aborted'
  | 'timeout';
```

For `event.type === 'error'`, use `event.status === 'timeout' ? 'timeout' : 'failed'`. Do not invent a new event type.

- [ ] **Step 7: Align AGENTS.md item 38**

Replace only the conflicting post-abort identity clause. It must state that after the targeted abort and original worker barrier, the old Agent/model context is reset inside the stable child `AgentSession`; the deterministic child ID, Hub, bounded replay audit, and `(manager_session_id, child_id)` mapping remain stable. Preserve sibling isolation, no abort-all, bounded replay requirements, truncation anchor/terminal rules, and TTL cleanup.

- [ ] **Step 8: Run focused GREEN verification**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_manager_agent_dispatch.py \
  tests/test_agent_stop_api.py \
  tests/test_session_store_cleanup.py \
  tests/test_agent_session_recovery_api.py \
  tests/test_agent_session_hub.py \
  -q

cd frontend && node --test --experimental-strip-types \
  src/lib/api.test.mts \
  src/lib/timeline-projection.test.mts

cd frontend && npm run type-check
```

Record exact pass/fail counts and exit codes. If a command leaves an open handle or exceeds the execution window, identify the exact test and report it separately; never label a timed-out command as passing.

- [ ] **Step 9: Run final cross-cutting verification**

Run syntax/import validation:

```bash
./.sglang/bin/python -m compileall -q backend/agent backend/api
```

Then rerun the original deterministic timeout/reuse regressions individually to confirm they remain green outside the aggregate suite.

- [ ] **Step 10: Self-review and write the task report**

Confirm timeout is consistent across manager metadata, Hub payload, child transcript, and manager abort filtering; verify AGENTS wording matches the implemented stable-shell behavior. Include RED/GREEN evidence. Do not run Git.

---

## Execution and Review Assignment

To satisfy AGENTS.md context-reuse and child-count constraints:

1. Reuse `dispatch_lifecycle_audit_v2` serially for Tasks 1–4 because all four modify the same backend lifecycle and test surfaces.
2. Reuse `timeline_contract_audit_v2` for Task 5 because it already owns the frontend/timeline/status context.
3. Reuse `hub_recovery_audit_v2` as the independent task reviewer for Tasks 1–5; it performs read-only spec-compliance and code-quality reviews and never edits.
4. After all tasks pass their gates, reuse a non-implementing prior child for the whole-change review. Do not create additional children unless a five-round review breaker or genuine context-capacity failure requires it.
5. Never execute two implementation tasks concurrently because Tasks 1–5 share `agent_runner.py`, its tests, or dependent interfaces.

Because Git permission has not been granted, review packages consist of the task brief, implementer report, exact changed-file list, current source files, and focused test evidence. Reviewers must not run Git commands. The controller records decisions and task completion in the plan-specific SDD ledger.
