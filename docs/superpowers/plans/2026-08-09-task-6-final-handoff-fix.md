# Task 6 Final Handoff Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically start the transitioned cycle at cleanup settlement and execute outer `ImageUploader` folding/concurrent-cap behavior under the existing React renderer.

**Architecture:** A detached-cleanup transition stores only the latest full callback token. Cleanup settlement validates that token against both coordinator state and current identity, then starts the first capture directly in the settlement microtask before normal interval scheduling. The test renderer exposes composite-fiber lookup and batched prop invocation so `ImageUploader` can be exercised without browser media or source inspection.

**Tech Stack:** TypeScript, React 18, installed `react-reconciler` 0.27, Node test runner.

## Global Constraints

- Do not edit Task 7 files, `page.tsx`, backend files, configuration, or dependencies.
- Do not run Git.
- Preserve no-overlap and all seven callback-token dimensions.
- `transitionCycle(fullToken)` stays synchronous and remains the only supported multi-cycle API.

---

### Task 1: Immediate Detached-Cleanup Handoff

**Files:**
- Modify: `frontend/src/lib/vision-automation/tick-coordinator.test.mts`
- Modify: `frontend/src/lib/vision-automation/tick-coordinator.ts`

**Interfaces:**
- Preserves: `transitionCycle(token: FullCaptureCallbackToken): void`.

- [ ] Rewrite the detached-cleanup regression to transition without calling `captureOnce()`, resolve old work, and require the next source capture/commit before any interval callback is invoked.
- [ ] Add latest-token/cancellation assertions: a superseded token cannot start, and freeze/Abort cancel pending automatic handoff.
- [ ] Run the focused coordinator test and verify RED because settlement only calls `scheduleNext()` while `started=false`.
- [ ] Store a pending immediate token only when transition occurs with detached cleanup; on settlement compare it with coordinator/current tokens, set started, invoke `captureOnce()` immediately, and schedule later ticks only after completion.
- [ ] Clear pending handoff on freeze and Abort, then rerun the full coordinator suite.

### Task 2: Outer ImageUploader Rendered Behavior

**Files:**
- Modify: `frontend/src/lib/vision-automation/react-component-renderer.cjs`
- Modify: `frontend/src/lib/vision-automation/components-rendered.test.mjs`

**Interfaces:**
- Produces renderer methods `findComponent(predicate)` and `act(callback)`.

- [ ] Add rendered tests that mount `ImageUploader`, click the automation action twice, and observe a child effect mount then cleanup through the outer fold path.
- [ ] Add a rendered controlled-uploader test that opens manual capture, obtains the real `CameraCapture` fiber props, invokes `onCapture` twice in one renderer batch, applies both emitted functional updaters sequentially, and asserts exactly nine images.
- [ ] Run rendered tests and verify RED because the renderer cannot query/invoke composite fibers.
- [ ] Add composite-fiber traversal and batched `act` to the test renderer; do not change production behavior unless the executed regression exposes a defect.
- [ ] Run Task 6, controller, full API, focused soak, and type-check; append exact evidence to `.superpowers/sdd/task-6-report.md`.

## Self-Review

- First-capture handoff is direct, not timer-based.
- Old/superseded tokens cannot start a new cycle.
- No Task 7 production file is modified.
- Outer tests execute React state/effects and functional updater callbacks without source regex.
