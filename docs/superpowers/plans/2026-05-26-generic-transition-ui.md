# Generic Transition UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the existing dispatch card into a generic transition UI that hides all `switch_agent` control text, preserves `coordinator_2` chat-bubble reasoning, and renders assistant messages in A-C-D-B order.

**Architecture:** Keep the unified timeline model and add a generic dispatch display contract layered on top of existing SSE transport. Backend emits structured transition updates and slot metadata; frontend projects them into the existing assistant message while rendering `before_body` and `after_body` sections separately around the reasoning/body lane.

**Tech Stack:** Python, FastAPI backend runner, smolagents, Next.js 14, React, TypeScript, Node test runner, pytest.

---

### Task 1: Add generic dispatch event and slot metadata in the backend

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write failing backend tests for generic transition events and slots**

Add tests in `tests/test_multi_agent_runner.py` that assert:

```python
def test_run_stream_coordinator_routing_emits_generic_dispatch_delta_before_body():
    ...
    assert timeline_event["kind"] == "dispatch_delta"
    assert timeline_event["display"] == "dispatch_card"
    assert timeline_event["display_slot"] == "before_body"


def test_run_basic_stream_coordinator_2_secondary_handoff_emits_after_body_dispatch_step():
    ...
    assert dispatch_step["step_type"] == "dispatch"
    assert dispatch_step["display_slot"] == "after_body"
    assert dispatch_step["dispatch_kind"] == "secondary_handoff"


def test_run_basic_stream_execution_steps_default_to_after_body_slot():
    ...
    assert tool_call_event["display_slot"] == "after_body"
    assert observation_event["display_slot"] == "after_body"
```

- [ ] **Step 2: Run focused backend tests and confirm failure**

Run:

```bash
PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_multi_agent_runner.py -q
```

Expected: new tests fail because `dispatch_delta`, `display_slot`, or `dispatch_kind` are missing.

- [ ] **Step 3: Implement minimal backend support for generic dispatch display events**

In `backend/agent/agent_runner.py`, add helpers and wire them into the existing routing and secondary handoff paths:

```python
def _queue_dispatch_timeline_event(
    self,
    loop,
    queue,
    session,
    *,
    message_id: str,
    dispatch_id: str,
    dispatch_kind: str,
    display_slot: str,
    text: str,
    partial: bool,
    source: str,
    target_agent: str | None = None,
) -> None:
    self._queue_timeline_message_event(
        loop,
        queue,
        session,
        kind="dispatch_delta",
        message_id=message_id,
        dispatch_id=dispatch_id,
        dispatch_kind=dispatch_kind,
        display="dispatch_card",
        display_slot=display_slot,
        source=source,
        target_agent=target_agent,
        text=text,
        partial=partial,
    )


def _make_dispatch_step(..., dispatch_kind: str, display_slot: str, ...):
    return {
        "type": "agent_step",
        "step_type": "dispatch",
        "dispatch_kind": dispatch_kind,
        "display_slot": display_slot,
        ...
    }
```

Apply the rules:

```python
# coordinator_1 routing
dispatch_kind="routing"
display_slot="before_body"

# coordinator_2 secondary handoff
dispatch_kind="secondary_handoff"
display_slot="after_body"

# tool / observation / thought from execution agents
payload["display_slot"] = "after_body"
```

- [ ] **Step 4: Keep routing compatibility while adding the new generic event**

Ensure `_run_coordinator_routing_stream()` still emits existing routing behavior records, but also emits the new display event:

```python
yield {
    "type": "timeline_event",
    "event": {
        "kind": "routing_delta",
        ...
    },
}

self._queue_dispatch_timeline_event(
    loop,
    queue,
    session,
    message_id=assistant_message_id,
    dispatch_id=f"dispatch-{run_scope_id}-routing",
    dispatch_kind="routing",
    display_slot="before_body",
    text=text,
    partial=True,
    source="coordinator_1",
    target_agent=target_agent,
)
```

- [ ] **Step 5: Run focused backend tests and confirm pass**

Run:

```bash
PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_multi_agent_runner.py -q
```

Expected: focused routing/handoff tests pass.

### Task 2: Extend frontend types, projection, and dispatch semantics

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/timeline-projection.ts`
- Modify: `frontend/src/lib/dispatch-message.ts`
- Test: `frontend/src/lib/timeline-projection.test.mts`
- Test: `frontend/src/lib/dispatch-message.test.mts`

- [ ] **Step 1: Write failing frontend tests for dispatch delta and slot metadata**

Add tests asserting:

```ts
test('dispatch_delta becomes dispatch update projection', () => {
  assert.deepEqual(projectTimelineEvent({
    kind: 'dispatch_delta',
    display: 'dispatch_card',
    message_id: 'assistant-msg-42',
    dispatch_id: 'dispatch-run-42-secondary',
    dispatch_kind: 'secondary_handoff',
    display_slot: 'after_body',
    text: '正在转交执行代理',
    partial: true,
  }), {
    kind: 'dispatch_update',
    messageId: 'assistant-msg-42',
    dispatchId: 'dispatch-run-42-secondary',
    dispatchKind: 'secondary_handoff',
    displaySlot: 'after_body',
    text: '正在转交执行代理',
    partial: true,
  });
});

test('getDispatchCardCopy uses secondary handoff humanized copy', () => {
  assert.deepEqual(getDispatchCardCopy({
    step_type: 'dispatch',
    dispatch_kind: 'secondary_handoff',
    dispatch_status: 'completed',
    dispatch_target_agent: 'basic_agent',
  }), {
    statusText: '已转交执行代理',
    subText: '系统已将任务转交给 basic_agent 继续处理',
  });
});
```

- [ ] **Step 2: Run focused frontend unit tests and confirm failure**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/dispatch-message.test.mts" "frontend/src/lib/timeline-projection.test.mts"
```

Expected: tests fail because the new fields and copy rules do not exist yet.

- [ ] **Step 3: Extend shared frontend types with generic dispatch metadata**

Update `frontend/src/lib/api.ts`:

```ts
export interface AgentStep {
  ...
  dispatch_kind?: 'routing' | 'secondary_handoff';
  display_slot?: 'before_body' | 'after_body';
  dispatch_id?: string;
}

export interface TimelineEvent {
  ...
  dispatch_id?: string;
  dispatch_kind?: 'routing' | 'secondary_handoff';
  display_slot?: 'before_body' | 'after_body';
}
```

- [ ] **Step 4: Project generic dispatch events with routing fallback**

Update `frontend/src/lib/timeline-projection.ts`:

```ts
if (event.kind === 'dispatch_delta' && event.display === 'dispatch_card') {
  return {
    kind: 'dispatch_update',
    messageId: event.message_id,
    dispatchId: typeof event.dispatch_id === 'string' ? event.dispatch_id : undefined,
    dispatchKind: event.dispatch_kind === 'secondary_handoff' ? 'secondary_handoff' : 'routing',
    displaySlot: event.display_slot === 'before_body' ? 'before_body' : 'after_body',
    text: event.text || '',
    partial: event.partial ?? false,
  };
}

if (event.kind === 'routing_delta') {
  return {
    kind: 'dispatch_update',
    messageId: event.message_id,
    dispatchKind: 'routing',
    displaySlot: 'before_body',
    text: event.text || '',
    partial: event.partial ?? false,
  };
}
```

- [ ] **Step 5: Teach dispatch-message helpers to preserve generic dispatch identity and copy**

Update `frontend/src/lib/dispatch-message.ts` so merge and copy logic respects `dispatch_id`, `dispatch_kind`, and `display_slot`:

```ts
export function mergeDispatchStep(previousStep: AgentStep, nextStep: AgentStep): AgentStep {
  return {
    ...previousStep,
    ...nextStep,
    id: previousStep.id ?? nextStep.id,
    dispatch_id: previousStep.dispatch_id ?? nextStep.dispatch_id,
    dispatch_message: nextStep.dispatch_message ?? previousStep.dispatch_message,
    dispatch_kind: nextStep.dispatch_kind ?? previousStep.dispatch_kind,
    display_slot: nextStep.display_slot ?? previousStep.display_slot,
  };
}
```

And add copy rules:

```ts
if (step.dispatch_kind === 'secondary_handoff' && isCompleted) {
  return {
    statusText: '已转交执行代理',
    subText: `系统已将任务转交给 ${step.dispatch_target_agent || '执行代理'} 继续处理`,
  };
}
```

- [ ] **Step 6: Run focused frontend tests and confirm pass**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/dispatch-message.test.mts" "frontend/src/lib/timeline-projection.test.mts"
```

Expected: dispatch projection and copy tests pass.

### Task 3: Render assistant messages in before/body/after sections and compact the transition card

**Files:**
- Modify: `frontend/src/components/AgentStepDisplay.tsx`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Test: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Write failing source-level tests for A-C-D-B rendering and compact card changes**

Add tests asserting source contains:

```ts
assert.match(textPageSource, /const beforeBodySteps = .*display_slot === 'before_body'/);
assert.match(textPageSource, /const afterBodySteps = .*display_slot !== 'before_body'/);
assert.match(textPageSource, /<AgentStepDisplay steps=\{beforeBodySteps\} \/>/);
assert.match(textPageSource, /<AssistantReasoningSection/);
assert.match(textPageSource, /<ReactMarkdown/);
assert.match(textPageSource, /<AgentStepDisplay steps=\{afterBodySteps\} \/>/);

assert.doesNotMatch(componentSource, /Coordinator Transition/);
assert.match(componentSource, /className="rounded-xl border border-slate-700 bg-slate-900 text-slate-100 overflow-hidden shadow-sm"/);
```

- [ ] **Step 2: Run focused source-level tests and confirm failure**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts"
```

Expected: tests fail because assistant messages still render `AgentStepDisplay` before reasoning/body and the card still contains the old header text.

- [ ] **Step 3: Compact the transition card and remove the old top label**

Update `frontend/src/components/AgentStepDisplay.tsx` dispatch card block to remove the `Coordinator Transition` line and reduce padding:

```tsx
<div className="rounded-xl border border-slate-700 bg-slate-900 text-slate-100 overflow-hidden shadow-sm">
  <div className="flex items-center justify-between gap-3 px-4 py-2.5 border-b border-slate-800">
    <div className="text-base font-semibold text-slate-50">任务调度</div>
    <div className="text-xs text-slate-400">#{index + 1}</div>
  </div>

  <div className="px-4 py-3">
    ...
  </div>
</div>
```

- [ ] **Step 4: Render `before_body` and `after_body` step sections around reasoning/body**

In both page files, replace the single render path with:

```tsx
const agentSteps = message.agentSteps || [];
const beforeBodySteps = agentSteps.filter((step) => step.display_slot === 'before_body');
const afterBodySteps = agentSteps.filter((step) => step.display_slot !== 'before_body');

{beforeBodySteps.length > 0 && <AgentStepDisplay steps={beforeBodySteps} />}
<AssistantReasoningSection ... />
<ReactMarkdown ...>{...}</ReactMarkdown>
{afterBodySteps.length > 0 && <AgentStepDisplay steps={afterBodySteps} />}
```

- [ ] **Step 5: Preserve correct dispatch placement when timeline and step updates arrive**

In both page files, create dispatch steps from projection with explicit metadata:

```ts
const dispatchStep: AgentStep = {
  id: projection.dispatchId || projection.messageId || createClientStepId('dispatch'),
  step_type: 'dispatch',
  dispatch_id: projection.dispatchId,
  dispatch_kind: projection.dispatchKind,
  display_slot: projection.displaySlot,
  dispatch_status: 'running',
  dispatch_stage: projection.dispatchKind === 'routing' ? 'routing' : 'handoff',
  dispatch_message: projection.text,
  partial: projection.partial,
};
```

When merging, match the last dispatch step with the same `display_slot` and `dispatch_kind` instead of blindly replacing the last dispatch step.

- [ ] **Step 6: Run focused source-level tests and confirm pass**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts"
```

Expected: source-level order and card-style tests pass.

### Task 4: Close the loop with regressions and full verification

**Files:**
- Modify: `tests/test_multi_agent_runner.py`
- Modify: `frontend/src/lib/dispatch-message.test.mts`
- Modify: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Add regression for hidden switch text in final visible content**

Add a backend test that asserts:

```python
def test_run_basic_stream_coordinator_2_never_emits_switch_agent_json_into_visible_chat_bubble():
    ...
    visible_text = "".join(event["event"]["text"] for event in timeline_events if event["event"].get("display") == "chat_bubble")
    assert '"switch_agent"' not in visible_text
    assert '[{"id":"call_0"' not in visible_text
```

- [ ] **Step 2: Add regression for dispatch slot ordering in the frontend helpers**

Add tests in `dispatch-message.test.mts` or `timeline-projection.test.mts` asserting that a message with mixed step slots is intended to render in A-C-D-B order by source layout contracts.

```ts
assert.match(textPageSource, /beforeBodySteps/);
assert.match(textPageSource, /afterBodySteps/);
```

- [ ] **Step 3: Run backend baseline verification**

Run:

```bash
PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_multi_agent_runner.py tests/test_agent_session_context.py tests/test_multi_tool_parsing.py tests/test_timeline_projection.py -q
```

Expected: all backend regression suites pass.

- [ ] **Step 4: Run frontend verification**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/dispatch-message.test.mts" "frontend/src/lib/timeline-projection.test.mts"
npm run build
npm run type-check
```

Expected: node tests pass, build succeeds, type-check succeeds after build.

- [ ] **Step 5: Record any follow-up fixes uncovered by verification**

If a verification step fails, make the smallest fix in the touched file and rerun only the failing command first, then rerun the full command that failed.
