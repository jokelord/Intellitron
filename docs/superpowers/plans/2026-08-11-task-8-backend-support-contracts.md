# Task 8 Backend Support Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add exact cleanup-token cancellation and lifecycle-managed streaming Answer Now replacement requests without changing frontend or prompt behavior.

**Architecture:** Cleanup cancellation remains in the existing AgentSession hashed-token state and is serialized by the Agent admission lock. Streaming Answer Now uses a dedicated API request model and ASGI response owner that reserves lifecycle identity before mutation, primes the unchanged runner stream through replacement run registration, emits acceptance, and waits for source plus run-scope cleanup before terminal.

**Tech Stack:** FastAPI, Starlette StreamingResponse/ASGI, Pydantic v2, asyncio, existing RequestLifecycleRegistry, pytest.

## Global Constraints

- Do not run Git or edit frontend files.
- Preserve Answer Now's targeted abort barrier, canonical prompt, full buffering, no-tool contract, and one corrective retry.
- Cleanup cancellation never mutates canonical Agent memory or prompt snapshots.
- Lifecycle duplicate request IDs never start or abort a second replacement worker.
- Terminal status is written only after worker, run-scope, stream ownership, and single-flight cleanup.

---

### Task 1: Exact Cleanup Cancellation

**Files:**
- Modify: `backend/vision/history_omission.py`
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/api/agent.py`
- Test: `tests/test_agent_vision_history_omission.py`
- Test: `tests/test_request_lifecycle_integration.py`

**Interfaces:**
- Consumes: `AgentVisionAutomationAbortRequest`, `VisionAutomationCleanupTokenState`.
- Produces: `cancel_agent_vision_automation_cleanup(session, binding, cleanup_token)` and `AgentRunner.cancel_agent_vision_automation_cleanup(...)`.

- [x] **Step 1: Write failing helper and route tests**

Cover exact pending cancellation, exact repeated cancellation, mismatched hash/binding rejection, consumed rejection, child rejection, admission/cancellation race serialization, unchanged memory, and exact JSON response.

- [x] **Step 2: Run RED tests**

Run: `./.sglang/bin/python -m pytest tests/test_agent_vision_history_omission.py tests/test_request_lifecycle_integration.py -q`

Expected: failures for missing cancel helper/route and cancellation state.

- [x] **Step 3: Implement the bounded cancelled tombstone and endpoint**

Use the same registration body. Under `_agent_admission_lock`, reject an active staged admission, validate the exact origin binding and SHA-256 digest, reject consumed/mismatched state, mark only the exact pending state cancelled, and make exact repeats return success.

- [x] **Step 4: Run GREEN tests**

Run the Step 2 command and require zero failures.

---

### Task 2: Lifecycle-Managed Streaming Answer Now

**Files:**
- Modify: `backend/api/agent.py`
- Test: `tests/test_agent_stop_api.py`
- Test: `tests/test_request_lifecycle_integration.py`
- Test: `tests/test_agent_session_context.py`

**Interfaces:**
- Consumes: `RequestLifecycleRegistry.reserve/accept/mark_running/mark_terminal/abandon`, `AgentRunner.run_answer_now_stream`, `AgentSession.wait_for_run_scopes`.
- Produces: required `AgentAnswerNowStreamRequest(session_id, request_id)` and lifecycle-owning Answer Now ASGI response.

- [x] **Step 1: Write failing lifecycle tests**

Cover missing/invalid ID generic 422, `agent_answer_now` reservation, duplicate suppression, old-run barrier before replacement, replacement run registration before acceptance, acceptance SSE with request/session IDs, accepted/running/status lookup, terminal after worker scope, disconnect cleanup, and generic conflict/capacity mappings.

- [x] **Step 2: Run RED tests**

Run: `./.sglang/bin/python -m pytest tests/test_agent_stop_api.py tests/test_request_lifecycle_integration.py tests/test_request_status_api.py -q`

Expected: failures for missing required ID, absent lifecycle reservation/acceptance, duplicate worker start, and premature terminal.

- [x] **Step 3: Implement the dedicated model and ASGI lifecycle owner**

Validate the required high-entropy ID before mutation. Reserve a fingerprint of the dedicated body with `request_kind="agent_answer_now"`; return duplicate status without acquiring single-flight. In ASGI ownership, capture/abort the old run, prime `run_answer_now_stream(..., single_flight_acquired=True)` through its first `session_start`, capture replacement scopes, accept/mark running, emit `request_accepted`, then forward the primed and remaining events. On all exits close the source, wait captured replacement scopes, release ownership, and only then mark terminal; abandon if acceptance never occurred.

- [x] **Step 4: Run GREEN tests**

Run the Step 2 command and require zero failures.

---

### Task 3: Regression Verification And Report

**Files:**
- Modify: `.superpowers/sdd/task-5-report.md`

- [x] **Step 1: Run requested regressions**

Run focused lifecycle/Answer Now, Task 5, Stop/provider, and compact suites, then compile all modified Python modules.

- [x] **Step 2: Append exact contracts and outputs**

Document both endpoint bodies/responses, duplicate/status behavior, acceptance/terminal ordering, RED/GREEN evidence, exact final counts, warnings, and remaining frontend/hardware concerns.
