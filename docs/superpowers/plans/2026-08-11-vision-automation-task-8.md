# Vision Automation Task 8 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the approved Vision automation contracts into `/vision` without changing Task 2/6 components, backend behavior, configuration, or the Text page.

**Architecture:** `vision/page.tsx` owns browser identity, source selection/acquisition, UI placement, and the adapter from `useVisionAutomation` submissions into the existing Chat/Agent streaming UI. Task 7 owns frozen payloads, staged omission, acceptance/status resolution, stable terminal state, and conditional post-Abort cleanup. Transport metadata carries one strict nested wire-only binding and never enters prompt messages or manifest text.

**Tech Stack:** Next.js 14, React 18, TypeScript, Node test runner, FastAPI/Pydantic wire-contract fixtures.

## Global Constraints

- Do not run Git or create commits.
- Production UI changes are limited to `frontend/src/app/vision/page.tsx`; use `frontend/src/app/globals.css` only if unavoidable.
- Narrow Task 7 transport edits are limited to `send-contract.ts`, `api.ts`, `use-vision-automation.ts`, and their tests.
- Do not edit Task 2/6 modules/components, backend source, config, or `frontend/src/app/page.tsx`.
- Armed busy Send rejects in place and preserves text, manual images, sampled frames, leases, and retained bundles.
- `images` remains manual-only.
- Browser/task/cycle/cleanup identity remains nested transport metadata only.

---

### Task 1: Strict Nested Transport Binding

**Files:**
- Modify: `frontend/src/lib/vision-automation/send-contract.ts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/vision-automation/use-vision-automation.ts`
- Test: `frontend/src/lib/vision-automation/send-contract.test.mts`
- Test: `frontend/src/lib/api.test.mts`
- Test: `frontend/src/lib/vision-automation/use-vision-automation.test.mts`

**Interfaces:**
- Produce `VisionAutomationTransportBinding` with `page_generation`, `mode`, `top_level_context_id`, `automation_task_id`, `capture_cycle_id`, and optional `cleanup_token`.
- Extend `VisionAutomationTransportMetadata` with required `binding`.
- Build normal automation binding from `AutomationBinding` plus submission cycle identity.
- Stage cleanup sends with zero-image metadata carrying the matching cleanup token.

- [ ] Add failing contract tests for nested snake-case binding, page generation bounds, exact modes, high-entropy lifecycle IDs, Chat/Agent body parity, and absence from messages/manifest text.
- [ ] Run focused tests and confirm RED because `binding` is absent.
- [ ] Add strict frontend validation matching `backend/vision/contracts.py` and serialize the nested binding.
- [ ] Add conditional cleanup-token state: mark accepted real image blocks, create a transport cleanup token only on Abort when needed, retain it across discard/pre-accept failure, and clear it only on matching accepted commit/cancellation.
- [ ] Run focused Task 7/API tests to GREEN.

### Task 2: Page Integration Contract

**Files:**
- Create: `frontend/src/lib/vision-automation/page-contract.test.mts`
- Modify: `frontend/src/app/vision/page.tsx`
- Narrowly update: `frontend/src/lib/api.test.mts` only if an existing page-source contract must recognize the approved integration.

**Interfaces:**
- Consume `useVisionAutomation(options)` and Task 6 acquisition helpers/components.
- Keep one page UUID, monotonic Chat generation, page-local Chat context UUID, and preallocated Agent session ID.
- Use the Agent session ID as `top_level_context_id`; use the page-local conversation ID for Chat.

- [ ] Add failing page contracts for identity allocation/invalidation, config/localStorage loading, source acquisition/retry, armed busy rejection, mode/Cycle exclusion, cleanup hooks, and normal unarmed queue preservation.
- [ ] Add failing render/source contracts for inline panel, plus-button status ring, one thumbnail matrix, historical summary cards, and manual-only `images`.
- [ ] Run the page contract and confirm RED.
- [ ] Integrate server config conversion and local preferences, source descriptors/selection, camera refresh, camera/display acquisition, direct display retry, frame JPEG encoding, and controller options.
- [ ] Wire Trigger Start, Retry Sources, Retry Failed Source, Abort, sampler/manual count, panel stats/health/error, persistent ring, summary cards, and preview through existing approved components.
- [ ] Run page contracts to GREEN.

### Task 3: Chat/Agent Lifecycle Adapter

**Files:**
- Modify: `frontend/src/app/vision/page.tsx`
- Test: `frontend/src/lib/vision-automation/page-contract.test.mts`
- Test: `frontend/src/lib/api.test.mts`

**Interfaces:**
- Automation submission consumes `VisionAutomationSubmitInput` and sends `payload.text`, `payload.images`, `stagedMessages`, request ID, metadata, signal, and lifecycle callbacks through existing stream methods.
- Ordinary post-Abort sends optionally consume `StagedVisionAutomationCleanupSend`.

- [ ] Add failing contracts proving Chat/Agent acceptance, EOF/status disambiguation, stable terminal forwarding, and cleanup commit/discard.
- [ ] Add failing contracts proving Stop, Answer Now, errors, and disconnects do not incorrectly restart or silently abort the sampling task.
- [ ] Refactor the page request runner only as far as necessary so normal and automation sends share existing UI/timeline behavior.
- [ ] Reject armed busy Send before queue/draft/image mutation; preserve the existing unarmed queue path.
- [ ] Make clear/pagehide/unmount/context and mode changes perform Abort-grade invalidation; disable mode switch while armed and preserve Cycle mutual exclusion.
- [ ] Run focused page/API tests to GREEN.

### Task 4: Full Verification And Report

**Files:**
- Create: `.superpowers/sdd/task-8-report.md`

- [ ] Run all Vision automation tests, rendered component contracts, API/message/proxy/timeline tests sequentially.
- [ ] Run backend Pydantic/FastAPI wire-contract tests for the nested binding.
- [ ] Run `npm run type-check`.
- [ ] Run `npm run build:verify`.
- [ ] Record RED/GREEN lifecycle evidence, exact commands/results, warnings, file scope, and remaining browser/E2E concerns in the Task 8 report.

## Self-Review

- Every requested identity field is produced in Task 1 and consumed in Task 2/3.
- Both Chat and Agent use the same nested metadata shape; Agent context is preallocated before acquisition.
- Cleanup creation, staging, commit/discard, and cancellation have explicit tests.
- Busy, Stop, Answer Now, error/disconnect, clear/pagehide/unmount, mode/Cycle, UI placement, and normal-send behavior each have an implementation/test step.
- No placeholder, Git, commit, backend edit, Task 2/6 edit, config edit, Text-page edit, or extra production file is planned.
