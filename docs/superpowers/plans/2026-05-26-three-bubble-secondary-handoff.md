# Three-Bubble Secondary Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade `coordinator_2` secondary handoff from a same-bubble A-C-D-B model to a true three-bubble lifecycle where the handoff card terminates bubble 2 and execution steps continue in bubble 3.

**Architecture:** Keep the existing unified timeline and generic dispatch contract, but change the message lifecycle boundary. Backend must emit secondary handoff events in an order that lets bubble 2 show its handoff card before closing; frontend must track an explicit active assistant bubble so execution steps move into a new third bubble instead of reusing the `coordinator_2` bubble.

**Tech Stack:** Python, FastAPI backend runner, smolagents, Next.js 14, React, TypeScript, Node test runner, pytest.

---

### Task 1: Write failing regressions for three-bubble handoff boundaries

**Files:**
- Modify: `tests/test_multi_agent_runner.py`
- Modify: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Add a backend regression for bubble-2 completion ordering**

Add a test in `tests/test_multi_agent_runner.py` asserting that, in a `coordinator_2 -> basic_agent` handoff stream, the secondary handoff dispatch appears before `message_completed`, and `message_completed` appears before `agent_switched(source="coordinator_2")`.

- [ ] **Step 2: Add a backend regression for bubble-3 execution separation**

Add a test in `tests/test_multi_agent_runner.py` asserting that after the handoff boundary, the stream semantics imply a third execution phase rather than appending execution steps before bubble 2 completion.

- [ ] **Step 3: Add source-level frontend regressions for explicit execution bubble tracking**

Add assertions in `frontend/src/lib/timeline-projection.test.mts` that both page files contain:

```ts
assert.match(textPageSource, /executionAssistantMessageId/);
assert.match(visionPageSource, /executionAssistantMessageId/);
assert.match(textPageSource, /agent_switched/);
assert.match(visionPageSource, /agent_switched/);
```

- [ ] **Step 4: Run the focused failing tests**

Run:

```bash
PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py -q
node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts"
```

Expected: the new tests fail because the current implementation still closes bubble 2 too early and still routes execution steps back into the tracked `coordinator_2` bubble.

### Task 2: Fix backend secondary handoff event ordering

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Buffer trailing bubble-2 completion until after the handoff card**

Update `_coordinator_stream_secondary_handoff()` so that `message_completed` from the `coordinator_2` stream is buffered and emitted after the secondary handoff dispatch step, not before it.

- [ ] **Step 2: Keep abort/error behavior intact**

Ensure trailing `aborted` or `error` terminal events still short-circuit execution bubble startup.

- [ ] **Step 3: Re-run focused backend tests**

Run:

```bash
PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py -q
```

Expected: the new ordering regressions pass.

### Task 3: Add explicit third-bubble tracking in both page implementations

**Files:**
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/lib/dispatch-message.ts`
- Test: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Extend active run state with an explicit execution bubble id**

In both page files, extend the local `agentRun` tracking object with a field such as `executionAssistantMessageId?: string` and use it to decide which assistant bubble receives `onStep` / `onContent` updates.

- [ ] **Step 2: Create bubble 3 when `agent_switched(source="coordinator_2")` arrives**

In the timeline callback, inspect raw `timelineEvent` before or alongside projection. When it is `agent_switched` from `coordinator_2`, create a new assistant placeholder message with `isStreaming: true`, append it to `messages`, and store its id in `agentRun.executionAssistantMessageId`.

- [ ] **Step 3: Route execution steps to bubble 3, not bubble 2**

Update the tracked-assistant selection logic so that once `executionAssistantMessageId` exists, `onStep` and `onContent` update that message instead of the last projected `coordinator_2` bubble.

- [ ] **Step 4: Keep stop / answer-now / completion finalization aligned with all tracked bubbles**

Update stop and finalize paths so they collect and finalize:

```ts
[agentRun.assistantMessageId, ...agentRun.projectedAssistantMessageIds, agentRun.executionAssistantMessageId]
```

without duplicating ids.

- [ ] **Step 5: Re-run focused frontend tests**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts"
```

Expected: source-level tests now show explicit execution bubble tracking in both page files.

### Task 4: Keep shared helper behavior aligned with the new tracking model

**Files:**
- Modify: `frontend/src/lib/dispatch-message.ts`
- Modify: `frontend/src/lib/dispatch-message.test.mts`

- [ ] **Step 1: Add a helper that builds the complete tracked assistant id list without duplicates**

Update `dispatch-message.ts` helpers so bubble finalization can include the execution bubble id in a stable order.

- [ ] **Step 2: Add a unit test for execution bubble id collection**

Add a test in `frontend/src/lib/dispatch-message.test.mts` asserting that tracked assistant ids preserve order and avoid duplicates when an execution bubble id is present.

- [ ] **Step 3: Run focused dispatch helper tests**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/dispatch-message.test.mts"
```

Expected: helper tests pass.

### Task 5: Run full verification and fix the smallest failures first

**Files:**
- Modify only the files that fail verification

- [ ] **Step 1: Run backend baseline**

Run:

```bash
PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py tests/test_agent_session_context.py tests/test_multi_tool_parsing.py tests/test_timeline_projection.py -q
```

- [ ] **Step 2: Run frontend unit tests**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/dispatch-message.test.mts" "frontend/src/lib/timeline-projection.test.mts"
```

- [ ] **Step 3: Run frontend build**

Run:

```bash
npm run build
```

from `frontend/`.

- [ ] **Step 4: Run frontend type-check**

Run:

```bash
npm run type-check
```

from `frontend/`.

- [ ] **Step 5: If any command fails, fix the smallest root cause and rerun the failing command first**

Only after the failing command passes, rerun the full command that originally failed.
