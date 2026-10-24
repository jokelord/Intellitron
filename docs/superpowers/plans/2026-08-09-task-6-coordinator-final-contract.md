# Task 6 Coordinator Final Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent in-flight timer polling and expose same-cycle recovery without resetting canonical cycle state.

**Architecture:** Scheduling refuses to create or service interval work while a capture is active; the active capture completion is the sole next-interval owner. `resumeCycle(fullToken)` validates exact current identity and restarts only pause/failure scheduling, preserving all cycle counters/timestamps/listeners.

**Tech Stack:** TypeScript and Node test runner.

## Global Constraints

- Modify only Task 6 coordinator/tests/report.
- Do not edit Task 7, pages, backend, configuration, or dependencies.
- Do not run Git.
- Preserve complete callback-token validation and no-overlap.

---

### Task 1: In-Flight Timer Ownership

**Files:**
- Modify: `frontend/src/lib/vision-automation/tick-coordinator.test.mts`
- Modify: `frontend/src/lib/vision-automation/tick-coordinator.ts`

- [ ] Add a fake-timer RED test that captures a scheduled interval handler, begins a slow post-cleanup immediate capture, executes the expired handler while unresolved, and asserts no extra capture and no zero-delay timer.
- [ ] Resolve the slow capture and assert its completion alone schedules the next interval.
- [ ] Add `inFlight` guards to `scheduleNext()` and the timer callback; an expired timer clears itself and exits during active capture.
- [ ] Run the coordinator suite to GREEN.

### Task 2: Same-Cycle Recovery API

**Files:**
- Modify: `frontend/src/lib/vision-automation/tick-coordinator.test.mts`
- Modify: `frontend/src/lib/vision-automation/tick-coordinator.ts`

**Produces:**

```ts
resumeCycle(token: FullCaptureCallbackToken): Promise<boolean>
```

- [ ] Add RED tests for recovery after a failed first tick and after a retained successful bundle.
- [ ] Assert exact token validation, unchanged cycle ID/generation, original T=0, monotonically increasing tick index, one listener set, cleared failure count, and immediate resumed capture.
- [ ] Implement `resumeCycle` without calling `transitionCycle`; reject mismatched/stale/aborted tokens, clear timer/pause/failure state, preserve cycle state, and start immediately or queue same-cycle start behind detached cleanup.
- [ ] Run Task 6, controller, full API, soak, and type-check; append exact results and signature to `.superpowers/sdd/task-6-report.md`.

## Self-Review

- No cycle timestamp/index/listener reset occurs in `resumeCycle`.
- No timer can poll an active capture.
- Task 7 remains untouched.
