# Answer Now Disposal Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serialize Answer Now acquisition with pinned Agent session disposal so disposal-first cannot start a new replacement request and Answer-Now-first remains part of the disposal barrier.

**Architecture:** `AgentRunner.try_acquire_answer_now()` is the single admission operation under `_agent_admission_lock`. Streaming keeps lifecycle reservation and deferred-body ownership in `_AnswerNowStreamingResponse`, but delegates its exact-session single-flight acquisition to the runner.

**Tech Stack:** Python 3.12, FastAPI/Starlette ASGI, asyncio/threading, pytest.

## Global Constraints

- Do not run Git or edit frontend files.
- Preserve provider-neutral targeted abort, request lifecycle idempotency, and existing Answer Now outer cleanup barriers.
- Disposal/stale-object rejection returns generic busy output and never mutates canonical history.

### Task 1: Atomic Runner Admission

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_session_store_cleanup.py`

- [ ] Add RED disposal-first, Answer-Now-first, and same-string replacement identity races.
- [ ] Implement `try_acquire_answer_now(session_id, expected_session=None)` with one admission-lock critical section.
- [ ] Run focused disposal/session tests green.

### Task 2: Both Answer Now Endpoints

**Files:**
- Modify: `backend/api/agent.py`
- Test: `tests/test_agent_stop_api.py`

- [ ] Add RED non-stream and deferred streaming tests proving runner delegation, generic busy handling, exact lifecycle abandonment, and no worker/provider start.
- [ ] Replace both direct `session.try_begin_answer_now()` calls with runner admission.
- [ ] Run focused Answer Now/Stop tests green.

### Task 3: Verification And Evidence

**Files:**
- Modify: `.superpowers/sdd/task-5-report.md`

- [ ] Run disposal, Answer Now, Stop, Task 5, and Agent session suites.
- [ ] Compile affected backend modules.
- [ ] Append exact RED/GREEN/final evidence and residual concerns.

No commit step is permitted.
