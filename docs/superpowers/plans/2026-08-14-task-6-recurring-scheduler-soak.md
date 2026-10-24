# Task 6 Recurring Scheduler Soak Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strengthen the permanent 10,000-attempt soak so production recurring scheduling and every production-owned collection are behaviorally bounded.

**Architecture:** Replace direct capture driving with a deterministic due-time timer queue. Start the coordinator once, then execute coordinator/source timers until exactly 10,000 attempts complete; a strict test-only reflection helper audits every Array/Map/Set path on production roots and documented nested state.

**Tech Stack:** TypeScript, Node test runner, existing `TickCoordinator`, `TickBundleSampler`, `FrozenBundleBatch`, and `OwnedImageResource`.

## Global Constraints

- Do not run Git.
- Do not edit production unless the strengthened soak proves a real production defect.
- Do not call `captureOnce()` as the primary loop.
- Preserve all existing ownership, disposal, capacity, eviction, transaction, and Abort assertions.
- Every discovered collection path requires an explicit bound; unknown collection paths fail.

---

### Task 1: Strict Collection Audit RED/GREEN

**Files:**
- Modify: `frontend/src/lib/vision-automation/tick-coordinator-soak.test.mts`

**Interfaces:**
- Consumes: ordinary JavaScript own properties and Array/Map/Set sizes.
- Produces: test-only `auditProductionCollections(roots, bounds, nestedPaths)` with normalized paths and strict unknown-path rejection.

- [ ] **Step 1: Add a failing fixture test**

Add a test that creates `{ cache: [] }`, grows `cache`, and requires the not-yet-implemented auditor to reject both a known path over its maximum and an unknown cache path.

- [ ] **Step 2: Run the focused fixture test to RED**

Run:

```bash
node --test --experimental-strip-types --test-name-pattern="collection audit" src/lib/vision-automation/tick-coordinator-soak.test.mts
```

Expected: fail because the strict audit helper does not yet exist.

- [ ] **Step 3: Implement the minimal strict auditor**

For each named root, inspect all own property values. Record Array length, Map size, and Set size. Traverse only documented nested object paths, normalize indexed bundle/source/resource paths, reject every collection path absent from the explicit bound table, reject values over the current bound, and reject configured paths not observed when they are required for the current state.

- [ ] **Step 4: Re-run fixture test to GREEN**

Expected: both deliberate cache failures are detected and the fixture test passes.

### Task 2: Recurring Scheduler Soak

**Files:**
- Modify: `frontend/src/lib/vision-automation/tick-coordinator-soak.test.mts`

**Interfaces:**
- Consumes: `TickCoordinator.start()`, injected timeout functions, and production ownership objects.
- Produces: exactly 10,000 scheduler-driven attempts with bounded timer and collection evidence.

- [ ] **Step 1: Replace the fake timer set with an executable queue**

Store timer ID, due time, insertion sequence, role, delay, and handler. Coordinator timeout timers use the fixed timeout delay; all other injected coordinator timers are recurring intervals. Source helpers schedule deterministic fast, slow, and late completions on the same clock under source-only roles.

- [ ] **Step 2: Replace the direct loop with recurring execution**

Call `coordinator.start()` once. Execute the next due fake timer and drain microtasks until source start counters reach exactly 10,000. Never call `captureOnce()` from the loop.

Use ordinary successes, modular rejections, slow completions beyond the interval but before timeout, and timeout completions after timeout. Verify legitimate delay-0 intervals occur, each starts one tick, no duplicate interval/timeout ownership exists, and no delay-0 polling occurs without a completed prior barrier.

- [ ] **Step 3: Exercise automatic post-transition capture**

At attempts 1,000 through 9,000, force timeout cleanup, freeze and transfer the sampler while cleanup remains pending, update the full token, and call `transitionCycle()`. Execute the late source timer and prove cleanup starts the next cycle automatically without `start()` or `captureOnce()`. At attempt 10,000, freeze the final pool without starting attempt 10,001.

- [ ] **Step 4: Audit production collections periodically**

Audit coordinator roots (`sources`, `failureCounts`, `removeEndedListeners`, nested active-batch settlements), sampler roots (`sources`, retained bundles and nested frames), frozen batch roots, each source listener set, and each live/pending resource Object URL set. Use dynamic capacity/pending maxima and external audit-only disposal ledgers.

- [ ] **Step 5: Preserve final exact ownership assertions**

Assert deterministic failure/timeout counts, sampler evictions, capacities 48/50, ten transaction boundaries, nine automatic transitions, exact source starts, no attempt 10,001, exact resource disposal, exact URL revocation, one listener per source, no lease after Abort, and zero active timers.

- [ ] **Step 6: Run focused soak to GREEN**

Run:

```bash
node --test --experimental-strip-types src/lib/vision-automation/tick-coordinator-soak.test.mts
```

Expected: fixture and 10,000-attempt soak both pass in practical time.

### Task 3: Verification and Evidence

**Files:**
- Modify: `.superpowers/sdd/task-6-report.md`

**Interfaces:**
- Consumes: all final command output.
- Produces: exact scheduler, collection-audit, RED/GREEN, suite-count, duration, and concern evidence.

- [ ] **Step 1: Run Task 6, Task 2, controller, API, focused soak, and type-check**

Use the established exact commands, with the permanent soak file included in Task 6.

- [ ] **Step 2: Append exact evidence to the report**

Record commands, counts, timings, zero-delay scheduler evidence, strict collection paths, deliberate fixture detection, production-file status, and residual browser risk.

- [ ] **Step 3: Complete without Git**

Return only status, one-line test summary, and concerns.
