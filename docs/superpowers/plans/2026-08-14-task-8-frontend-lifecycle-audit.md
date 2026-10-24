# Task 8 Frontend Lifecycle Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire exact Agent sessions at destructive page-context boundaries and keep automation requests model-busy until stable request terminal.

**Architecture:** `AgentSessionOwnership` assigns a unique local token to each bound session and detaches synchronously before asynchronous cleanup/deletion. `AutomationTerminalGate` separates candidate visual completion from stable lifecycle release. `LLMClient.deleteAgentSession()` owns DELETE serialization while the Vision page owns best-effort retirement ordering.

**Tech Stack:** Next.js 14, React 18, TypeScript, Node test runner.

## Global Constraints

- Do not run Git.
- Do not edit backend source.
- Preserve normal non-automation callback timing.
- Ordinary Vision Automation Abort must not delete the active Agent session.
- `pagehide` exclusively retires its detached identity with `keepalive: true`; subsequent unmount skips it.

---

### Task 1: Agent Session Delete Client

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Test: `frontend/src/lib/api.test.mts`

**Interfaces:**
- Produces: `LLMClient.deleteAgentSession(sessionId: string, options?: { keepalive?: boolean }): Promise<void>`

- [ ] Add a failing fetch-capture test for encoded DELETE URL, method, and optional keepalive.
- [ ] Run `node --test --experimental-strip-types src/lib/api.test.mts` and confirm the method is absent.
- [ ] Implement the method with non-2xx error propagation.
- [ ] Rerun the focused API test.

### Task 2: Exact Session Ownership And Retirement

**Files:**
- Modify: `frontend/src/lib/vision-automation/page-integration.ts`
- Modify: `frontend/src/app/vision/page.tsx`
- Test: `frontend/src/lib/vision-automation/page-integration.test.mts`

**Interfaces:**
- Produces: `AgentSessionOwnership.bind()`, `detach()`, and `canDelete()` exact-token semantics.
- Consumes: `LLMClient.deleteAgentSession()`.

- [ ] Add failing executable tests for detach-before-await, pagehide-to-unmount idempotence, same-string BFCache reuse protection, and a fresh restored context.
- [ ] Run the page integration test and confirm identity/lifecycle failures.
- [ ] Implement the ownership token state and wire allocation through one page helper.
- [ ] On Clear, pagehide, unmount, Agent-to-Chat apply, and other context replacement, detach synchronously, invalidate/Abort locally, then best-effort delete if the old ID was not rebound.
- [ ] Keep ordinary Automation Abort free of session deletion.
- [ ] Rerun focused page/API tests.

### Task 3: Candidate Completion Versus Stable Terminal

**Files:**
- Modify: `frontend/src/lib/vision-automation/page-integration.ts`
- Modify: `frontend/src/app/vision/page.tsx`
- Test: `frontend/src/lib/vision-automation/page-integration.test.mts`
- Test: `frontend/src/lib/vision-automation/use-vision-automation.test.mts`
- Test: `frontend/src/lib/api.test.mts`

**Interfaces:**
- Produces: `AutomationTerminalGate.markCandidateComplete()` and `markStableTerminal()`.

- [ ] Add failing executable tests showing candidate completion finalizes visual output but leaves model-busy, ASR, queue/send, and cycle eligibility blocked.
- [ ] Add Answer Now replacement terminal coverage and preserve pre-existing non-automation callback order.
- [ ] Run focused tests and confirm candidate completion releases state too early.
- [ ] Wire Chat, Agent, EOF/error recovery, and Answer Now automation callbacks through the gate; release state exactly once at bound terminal.
- [ ] Rerun focused tests.

### Task 4: Verification And Report

**Files:**
- Modify: `.superpowers/sdd/task-8-report.md`

- [ ] Run the complete Vision Automation/page/API/history matrix.
- [ ] Run the complete frontend matrix.
- [ ] Run backend wire/lifecycle contracts without editing backend.
- [ ] Run `npm run type-check` and `npm run build:verify`.
- [ ] Append root causes, RED/GREEN evidence, exact commands, results, and remaining browser E2E concerns to the Task 8 report.
