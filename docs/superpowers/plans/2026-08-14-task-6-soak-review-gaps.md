# Task 6 Soak Review Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the transition/start race and inspectable recursive-collection gaps in the permanent 10,000-attempt soak without changing production behavior.

**Architecture:** Every transitioning boundary will invoke `transitionCycle()` followed by `start()` while old timeout cleanup is pending, then execute a slow automatic immediate capture. The collection auditor will recursively inspect reflectively reachable own properties using string and symbol keys, normalized collection-entry paths, and ancestry-based cycle detection while explicitly excluding non-inspectable native/private/module/closure state.

**Tech Stack:** TypeScript, Node test runner, existing Vision Automation production classes, deterministic fake timer queue.

## Global Constraints

- Do not run Git.
- Change only the soak test, report, and this plan unless RED reproduces a production defect.
- Keep exactly 10,000 recurring production attempts and all ownership/disposal assertions.
- Use RED before harness implementation for both review findings.

---

### Task 1: Recursive Collection Audit

**Files:**
- Modify: `frontend/src/lib/vision-automation/tick-coordinator-soak.test.mts`

**Interfaces:**
- Consumes: `Reflect.ownKeys`, Array/Map/Set entries, plain own-property graphs.
- Produces: strict recursive `auditProductionCollections()` with symbol paths, normalized `[]` paths, and ancestry cycle detection.

- [ ] **Step 1: Add nested and symbol fixture expectations**

Create a cyclic nested fixture with `nested.cache` and a symbol-keyed fixture. Assert both over-bound and unknown-path failures using explicit normalized paths.

- [ ] **Step 2: Run the collection fixture to RED**

```bash
node --test --experimental-strip-types --test-name-pattern="collection audit" src/lib/vision-automation/tick-coordinator-soak.test.mts
```

Expected: nested and symbol fixtures do not throw because the current helper scans direct string keys only.

- [ ] **Step 3: Implement recursive reflection**

Visit each root with an ancestry `Set<object>`. Use `Reflect.ownKeys()` and stable symbol formatting. Audit Array/Map/Set sizes at the current path, recurse through collection object entries with `[]` path segments, recurse through plain object own values, stop cycles, and do not recurse into non-plain native/class children unless they are explicit roots.

- [ ] **Step 4: Replace promoted nested roots with recursive path bounds**

Use bounds for coordinator active-batch settlements, sampler retained bundle frames, and sent-batch bundle frames through normalized recursive paths. Continue supplying sources and resources as roots because they are non-plain production owners.

- [ ] **Step 5: Run fixture GREEN**

Expected: all direct, nested, symbol, over-bound, unknown, and cyclic cases pass.

### Task 2: Transition Plus Start Race

**Files:**
- Modify: `frontend/src/lib/vision-automation/tick-coordinator-soak.test.mts`

**Interfaces:**
- Consumes: `TickCoordinator.transitionCycle()` and `TickCoordinator.start()` while `settlingBarrier` remains pending.
- Produces: nine production-equivalent dual-scheduling boundaries and nine slow automatic immediate captures.

- [ ] **Step 1: Add race counters and final assertions to RED**

Require nine boundary `start()` calls returning `false`, nine slow automatic immediate starts, no interval before each slow immediate capture completes, and one deterministic zero-delay interval afterward.

- [ ] **Step 2: Run focused soak to RED**

Expected: boundary start/slow counters remain zero under the existing transition-only harness.

- [ ] **Step 3: Implement boundary sequence in the harness**

After updating the token, call `transitionCycle(token)` then immediately `await start()` before late old cleanup resolves. Mark attempts 1,001 through 9,001 as slow. After old late cleanup, assert the automatic immediate attempt owns one timeout plus one source completion and no interval; after slow completion, the one interval must be delay 0.

- [ ] **Step 4: Update exact scheduler expectations**

Keep 10,000 timeout schedules, 9,991 interval schedules, 9,990 interval firings, nine transitions, and ten transactions. Increase the exact legitimate zero-delay count by the nine newly slow post-transition attempts without allowing duplicates.

- [ ] **Step 5: Run full focused GREEN**

Expected: both fixture and 10,000-attempt soak pass with no production change.

### Task 3: Verification and Honest Evidence

**Files:**
- Modify: `.superpowers/sdd/task-6-report.md`

- [ ] **Step 1: Run focused soak, Task 6, Task 2, controller, API, and type-check**

- [ ] **Step 2: Append exact RED/GREEN evidence and root causes**

Document the transition/start sequence, slow-immediate zero-delay result, recursive/symbol paths, cycle behavior, commands, counts, and durations.

- [ ] **Step 3: State reflection limits precisely**

State that native JavaScript `#private`, module-level, closure-held, and engine-internal collections cannot be introspected by this helper. Their coverage is limited to public resource, timer, listener, lease, disposal, and revocation accounting; do not claim universal hidden-cache detection.
