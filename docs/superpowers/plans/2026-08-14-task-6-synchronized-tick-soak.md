# Task 6 Synchronized Tick Soak Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a permanent, practical 10,000-attempt behavioral soak that proves bounded scheduling and exact image-resource ownership across coordinator, sampler, transaction, and Abort boundaries.

**Architecture:** A dedicated Node test drives five deterministic fake leased capture sources through the real `TickCoordinator`. Complete bundles transfer into a real `TickBundleSampler`; periodic freezes transfer the bounded pool into at most one `FrozenBundleBatch`, while tracked `OwnedImageResource` instances prove exact Blob/Object URL disposal.

**Tech Stack:** TypeScript, Node test runner, fake monotonic clock/timers, browser `Blob`, existing Vision Automation modules.

## Global Constraints

- Do not run Git.
- Add only the dedicated soak test, this plan, and Task 6 report evidence unless the soak exposes a real production defect.
- Execute exactly 10,000 coordinator tick attempts.
- Keep runtime practical with deterministic fake time and tiny resources.
- Preserve all existing production behavior unless a failing ownership invariant proves a defect.

---

### Task 1: Permanent Synchronized Ownership Soak

**Files:**
- Create: `frontend/src/lib/vision-automation/tick-coordinator-soak.test.mts`

**Interfaces:**
- Consumes: `TickCoordinator`, `TickBundleSampler`, `FrozenBundleBatch`, `OwnedImageResource`, `FullCaptureCallbackToken`, and `CaptureSource`.
- Produces: one permanent Node behavioral test named with `10,000 synchronized tick attempts` for focused reruns.

- [ ] **Step 1: Write the complete behavioral test**

Create deterministic test-only helpers:

```ts
class FakeClock {
  now = 0;
  readonly timers = new Map<object, { handler: () => void; cancelled: boolean }>();
  maxActive = 0;
  setTimeout(handler: () => void): ReturnType<typeof setTimeout>;
  clearTimeout(handle: ReturnType<typeof setTimeout>): void;
  fireOnlyActive(): void;
}
```

Use five `CaptureSource` implementations that create tracked real `OwnedImageResource` values on successful source attempts, reject on fixed modular schedules, and leave one fixed modular schedule pending until the fake timeout fires so its resource settles late. Track lease state, listener additions/removals, resource dispose calls, created URLs, and URL revocations without retaining `TickBundle` history.

Drive attempts sequentially:

```ts
for (let attempt = 0; attempt < 10_000; attempt += 1) {
  const tick = coordinator.captureOnce();
  if (isLateAttempt(attempt)) clock.fireOnlyActive();
  await tick;
  resolveLateAttemptWhenPresent();
  await coordinator.waitForBackgroundCleanup();
  assertBoundedState();
  if ((attempt + 1) % 1_000 === 0) rotateTransactionAndCycle();
}
```

Alternate manual image count between 0 and 9 so five-source capacity changes between 50 and 48. At each 1,000-attempt transaction boundary, dispose the previous sent batch, freeze the current sampler, instantiate one new sampler, update the full cycle token, and call `transitionCycle()`.

Assert throughout and at final cleanup:

```ts
assert.ok(currentSampler.bundles.length <= currentSampler.capacity);
assert.ok(currentSampler.bundles.length * sourceCount <= currentSampler.capacity * sourceCount);
assert.ok(clock.activeCount <= 1);
assert.ok(activeFrozenBatchCount <= 1);
assert.ok(activeCurrentPoolCount <= 1);
assert.equal(allCapturedBundles.length, 0);
```

After `coordinator.abort()`, dispose the current sampler and sent batch. Assert every source lease is released, each source is disposed once, listeners are removed once, every created resource has exactly one effective dispose, every upload/thumbnail Blob reference is null, every created Object URL appears exactly once in the revocation ledger, and no timers remain active.

- [ ] **Step 2: Run the focused test and classify RED**

Run:

```bash
node --test --experimental-strip-types src/lib/vision-automation/tick-coordinator-soak.test.mts
```

Expected first result: either PASS because existing production ownership is correct, or FAIL with a precise invariant identifying a test defect or real production ownership defect. Do not alter production for a harness/accounting failure.

- [ ] **Step 3: Make only the minimal correction required by RED**

Correct fake-clock microtask draining, deterministic attempt accounting, or test ownership bookkeeping in the test. Edit production only if an actual coordinator/sampler/resource contract violates an asserted ownership invariant.

- [ ] **Step 4: Re-run focused soak to GREEN**

Run the focused command until it exits 0 with one passing test and practical duration.

### Task 2: Verification and Evidence

**Files:**
- Modify: `.superpowers/sdd/task-6-report.md`

**Interfaces:**
- Consumes: permanent soak command and all requested existing suites.
- Produces: exact command, counts, durations, RED/GREEN result, ownership assertions, and concerns.

- [ ] **Step 1: Run all verification gates**

Run the Task 6 command including `tick-coordinator-soak.test.mts`, the Task 2 suite (`types`, `config`, `image-resources`, and `bundle-sampler`), controller suite, focused synchronized soak, full API suite, and `npm run type-check`.

- [ ] **Step 2: Append exact evidence**

Record exact commands, pass/fail/skip counts, durations, focused soak body duration, whether production changed, and residual real-browser risk in `.superpowers/sdd/task-6-report.md`.

- [ ] **Step 3: Final constraint check**

Confirm no Git command ran and no unrelated production file changed. Return only status, one-line test summary, and concerns.
