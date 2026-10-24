# Task 6 Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct all Task 6 review findings around media-attempt ownership, cycle identity, prompt Abort cleanup, synchronized timeout accounting, atomic manual-image limits, and testable UI contracts.

**Architecture:** Keep browser media leases in `MediaSourceRegistry`, but isolate every display chooser attempt by owner generation. Keep one `TickCoordinator` and one source-listener set across explicit cycle transitions while each active tick owns a quarantineable settlement batch. Put DOM-free interaction decisions in a small Task 6 UI contract module consumed by components and behavioral tests.

**Tech Stack:** TypeScript, React 18/Next.js 14, browser MediaStream/Canvas APIs, Node test runner with `--experimental-strip-types`.

## Global Constraints

- Do not edit `page.tsx`, Task 7 files, backend files, or configuration.
- Do not run Git.
- Preserve manual processing through `llmClient.processImageBinary(file, 'auto')`.
- Manual images have a code-enforced maximum of 9.
- Display chooser calls must occur synchronously in direct Start/Retry handlers.
- Every async callback token validates page instance, page generation, mode, top-level context, task ID, cycle ID, and cycle generation.
- Abort must promptly stop scheduling and release source leases even when capture work never settles.

---

### Task 1: Display Attempt Ownership and Abort-Racing Capture

**Files:**
- Modify: `frontend/src/lib/vision-automation/media-source-registry.test.mts`
- Modify: `frontend/src/lib/vision-automation/capture-source.test.mts`
- Modify: `frontend/src/lib/vision-automation/media-source-registry.ts`
- Modify: `frontend/src/lib/vision-automation/capture-source.ts`

**Interfaces:**
- Produces: owner-scoped display attempt generations and `CaptureCallbackToken extends AutomationBinding`.
- Produces: abort-racing leased source capture that disposes late resources.

- [ ] **Step 1: Write failing tests** for Abort followed by synchronous display Retry, stale display resolution quarantine, stale enumeration refresh, capture Abort rejection before ignored frame completion, and late resource disposal.
- [ ] **Step 2: Run the two focused test files** and confirm the new assertions fail for shared display pending state and unresolved capture work.
- [ ] **Step 3: Implement owner-scoped display acquisition generations** so each direct call invokes its chooser immediately and only its current owner attempt may install a stream.
- [ ] **Step 4: Implement abort racing in leased captures and browser video playback** while attaching late-result disposal.
- [ ] **Step 5: Rerun focused tests** and confirm all media/capture behaviors pass.

### Task 2: Full-Identity Cycle Coordinator and Prompt Abort

**Files:**
- Modify: `frontend/src/lib/vision-automation/tick-coordinator.test.mts`
- Modify: `frontend/src/lib/vision-automation/tick-coordinator.ts`

**Interfaces:**
- Consumes: full `CaptureCallbackToken` from Task 1.
- Produces: `sameCaptureCallbackToken`, `transitionCycle(token)`, and idempotent prompt `abort()` completion.

- [ ] **Step 1: Write failing fake-source/fake-timer tests** for all seven identity fields, repeated cycle transitions, reset T=0/tick index, one listener per source, non-settling Abort, immediate lease release, prompt fulfilled-frame disposal, and timeout failure accounting.
- [ ] **Step 2: Run coordinator tests** and confirm failures expose the partial token, immutable cycle, await-on-Abort, and all-source timeout accounting.
- [ ] **Step 3: Add full token comparison and explicit cycle transition** that resets cycle timing/index/failures without adding listeners.
- [ ] **Step 4: Introduce per-tick settlement tracking** so fulfilled resources can be quarantined immediately and only rejected/unresolved timeout entries increment failure counts.
- [ ] **Step 5: Make Abort idempotent and prompt** by cancelling timers, aborting capture, quarantining active batches, removing listeners, and disposing sources before returning one cached completion promise.
- [ ] **Step 6: Rerun coordinator tests** and confirm lifecycle and timeout behavior passes.

### Task 3: Behavioral UI Contracts and Component Remediation

**Files:**
- Create: `frontend/src/lib/vision-automation/ui-contracts.ts`
- Rewrite: `frontend/src/lib/vision-automation/components.test.mts`
- Modify: `frontend/src/components/VisionAutomationPanel.tsx`
- Modify: `frontend/src/components/VisionAutomationStatusRing.tsx`
- Modify: `frontend/src/components/VisionAutomationThumbnailMatrix.tsx`
- Modify: `frontend/src/components/ImageUploader.tsx`
- Modify: `frontend/src/components/CameraCapture.tsx`

**Interfaces:**
- Produces: `appendManualImages`, `getImageUploaderActionState`, `shouldMountAutomationPresentation`, `getVisionAutomationStatus`, and `createImagePreviewURLLease`.
- Produces: `VisionAutomationSourceHealth` panel data plus camera refresh and upload-image preview controls.

- [ ] **Step 1: Replace component source regex tests** with DOM-free tests of action state, atomic concurrent image commits, folded presentation, status mapping, source health, and preview URL release.
- [ ] **Step 2: Run component tests** and confirm the helper imports fail before implementation.
- [ ] **Step 3: Implement pure UI contracts** including the hard limit `min(configuredMax, 9)` and idempotent preview URL release.
- [ ] **Step 4: Delegate component decisions to the contracts**, remove the automation ring from `ImageUploader`, unmount folded automation presentation, and keep the reusable status ring available for Task 8's persistent plus button.
- [ ] **Step 5: Add panel camera refresh, per-source health/error output, and working image preview** whose upload Object URL is scoped to preview mount/selection.
- [ ] **Step 6: Remove compatibility marker comments** and keep manual camera processing/owner leases intact.
- [ ] **Step 7: Rerun component tests and type-check** until both pass.

### Task 4: Replace Existing Camera/Uploader Regex Tests

**Files:**
- Modify: `frontend/src/lib/api.test.mts`
- Test: Task 6 media, UI, and camera behavior files.

**Interfaces:**
- Consumes: `MediaSourceRegistry` and UI contracts from Tasks 1 and 3.

- [ ] **Step 1: Replace the five camera/uploader source-reading assertions** with fake-media and pure interaction behavior tests.
- [ ] **Step 2: Run the filtered camera/uploader tests** and confirm all pass without reading component source.
- [ ] **Step 3: Run complete Task 6 tests, Task 2 frozen tests, filtered camera/uploader tests, full `api.test.mts`, and `npm run type-check`**; record exact counts, exits, and unrelated failures.
- [ ] **Step 4: Append exact remediation evidence and remaining concerns** to `.superpowers/sdd/task-6-report.md`.

## Self-Review

- All eight requested remediation areas map to Tasks 1-4.
- Token names match frozen `AutomationBinding`: `pageInstanceId`, `pageGeneration`, `mode`, `contextId`, `automationTaskId`, plus `captureCycleId` and `cycleGeneration`.
- The plan contains no deferred implementation placeholders.
- No Git operation is included.
