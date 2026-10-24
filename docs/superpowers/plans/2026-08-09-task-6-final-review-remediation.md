# Task 6 Final Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make timeout cleanup non-blocking, registry attempt metadata bounded, active-source health complete, and Task 6 component behavior execute under a real React reconciler.

**Architecture:** Timed-out tick batches remain quarantined behind one observable background-cleanup promise while foreground freeze and cycle transitions proceed without overlapping captures. Media acquisition uses globally monotonic attempt records retained only while pending. Component tests compile Task 6 TSX and mount it through the installed `react-reconciler` into a minimal in-memory host tree with real state/effect cleanup.

**Tech Stack:** TypeScript, React 18, installed `react-reconciler` 0.27, Node test runner, Next.js type-check.

## Global Constraints

- Do not modify `use-vision-automation.ts`, `page.tsx`, Task 7 files, backend files, configuration, or dependencies.
- Do not run Git.
- `transitionCycle(fullToken)` remains the only supported multi-cycle transition API.
- Timed-out and stale image resources must never re-enter a committed bundle.
- No source capture may overlap unresolved work from a prior timed-out tick.

---

### Task 1: Non-Blocking Freeze and Background Cleanup

**Files:**
- Modify: `frontend/src/lib/vision-automation/tick-coordinator.test.mts`
- Modify: `frontend/src/lib/vision-automation/tick-coordinator.ts`

**Interfaces:**
- Produces: `waitForBackgroundCleanup(): Promise<void>` with stable identity while cleanup is pending.
- Preserves: `transitionCycle(token: FullCaptureCallbackToken): void`.

- [ ] Write a failing fake-timer test where one source times out and never settles; `freeze()` must beat a zero-delay deadline.
- [ ] Assert repeated `waitForBackgroundCleanup()` calls return the same pending promise, late completion is disposed, and the promise resolves after settlement.
- [ ] Assert `transitionCycle()` resets cycle identity while cleanup is pending, but `captureOnce()` remains blocked until cleanup resolves.
- [ ] Run the focused coordinator test and verify RED from `freeze()` awaiting `settlingBarrier`.
- [ ] Stop awaiting detached cleanup in `freeze()`, permit transition after foreground settlement, and retain the barrier as the idempotent no-overlap/background-cleanup gate.
- [ ] Rerun coordinator tests to GREEN.

### Task 2: Bounded Monotonic Registry Attempts

**Files:**
- Modify: `frontend/src/lib/vision-automation/media-source-registry.test.mts`
- Modify: `frontend/src/lib/vision-automation/media-source-registry.ts`

**Interfaces:**
- Produces: globally increasing `attemptToken` records and `diagnostics(): MediaSourceRegistryDiagnostics`.

- [ ] Add a failing test that performs 10,000 display Start/Abort attempts with unresolved choosers and asserts chooser count 10,000 plus zero pending owner-attempt keys/records and zero streams.
- [ ] Add focused retry/late-settlement assertions proving stale attempts remain invalid after their registry metadata is deleted.
- [ ] Run the registry test and verify RED because owner/display generation maps retain owner keys.
- [ ] Replace generation maps with pending attempt records, invalidate/unregister them on Abort/retry/release/settlement, and expose read-only count diagnostics.
- [ ] Run the registry suite and focused soak to GREEN.

### Task 3: Complete Active-Source Health Merge

**Files:**
- Modify: `frontend/src/lib/vision-automation/components.test.mts`
- Modify: `frontend/src/lib/vision-automation/ui-contracts.ts`
- Modify: `frontend/src/components/VisionAutomationPanel.tsx`

**Interfaces:**
- Produces: `mergeSourceHealthPresentation(activeSources, availableSources, updates)`.

- [ ] Add a failing pure test with two active sources and one update; assert the missing active source renders `waiting` and an explicit failed available source remains visible.
- [ ] Run component contracts and verify RED for the missing merge helper.
- [ ] Implement active-first health merging with waiting defaults and append only explicitly updated non-active sources.
- [ ] Delegate panel rendering to the helper and rerun contracts to GREEN.

### Task 4: Executed React Component Coverage

**Files:**
- Create: `frontend/src/lib/vision-automation/react-component-renderer.cjs`
- Create: `frontend/src/lib/vision-automation/components-rendered.test.mjs`
- Modify only if a behavioral defect is exposed: Task 6 panel/ring/matrix components.

**Interfaces:**
- Produces test-only `render(element)`, host-node queries, `click(node)`, `flush()`, `update(element)`, and `unmount()` using installed `react-reconciler`.

- [ ] Build a minimal mutation host renderer and TypeScript/TSX require hook without adding packages.
- [ ] Add rendered RED tests that execute Refresh click, open/close preview and observe upload URL acquire/release, collapse panel and observe thumbnail URL cleanup, and render the reusable status ring independently from `ImageUploader`.
- [ ] Run the rendered component test; fix harness defects first, then any real component behavior exposed.
- [ ] Run complete Task 6, Task 2, full API, type-check, and focused soak commands.
- [ ] Append exact RED/GREEN and final command evidence to `.superpowers/sdd/task-6-report.md`.

## Self-Review

- Every requested finding maps to one task.
- No Task 7/controller file is listed for modification.
- The test renderer uses already-installed packages only.
- The plan preserves no-overlap by blocking capture, not foreground freeze or cycle identity transition, on detached cleanup.
