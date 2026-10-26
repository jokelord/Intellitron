# Coordinator 2 Tail Retraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a frontend tail-retraction path that removes leaked canonical `switch_agent` JSON from the `coordinator_2` bubble after secondary handoff is confirmed, while preserving the three-bubble lifecycle and cleaning obsolete backend parser glue where safe.

**Architecture:** Keep the existing backend structured `secondary_handoff` signal and execution-bubble tracking, but stop relying on backend parser timing as the sole guarantee. Add a shared frontend helper that retracts only a complete trailing canonical `switch_agent` control span after handoff confirmation, then let the existing transition dispatch card own that semantic slot.

**Tech Stack:** Next.js 14, React, TypeScript, Node test runner, Python pytest.

---

### Task 1: Write failing frontend regressions for post-handoff tail retraction

**Files:**
- Modify: `frontend/src/lib/dispatch-message.test.mts`
- Modify: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Add a failing helper test for trailing canonical switch JSON removal**

Add a test in `frontend/src/lib/dispatch-message.test.mts` that expects a helper to strip only a trailing complete canonical `switch_agent` JSON span:

```ts
test('stripTrailingCanonicalSwitchAgentControl removes a complete trailing switch control span', () => {
  const prose = '我需要切换给 basic_agent。\n\n';
  const payload = '[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}]';

  assert.deepEqual(
    stripTrailingCanonicalSwitchAgentControl(`${prose}${payload}`),
    {
      text: prose,
      retracted: true,
    },
  );
});
```

- [ ] **Step 2: Add a failing helper test that protects non-control trailing text**

Add a test that proves the helper does not retract ordinary bracket text or JSON examples without both `type":"agent"` and `switch_agent`:

```ts
test('stripTrailingCanonicalSwitchAgentControl leaves formulas and plain JSON examples intact', () => {
  assert.deepEqual(
    stripTrailingCanonicalSwitchAgentControl('矩阵是 [a, b, c]'),
    { text: '矩阵是 [a, b, c]', retracted: false },
  );

  assert.deepEqual(
    stripTrailingCanonicalSwitchAgentControl('{"demo":"switch_agent"}'),
    { text: '{"demo":"switch_agent"}', retracted: false },
  );
});
```

- [ ] **Step 3: Add a failing tracked-message mutation test**

Add a test in `frontend/src/lib/dispatch-message.test.mts` for a message-array helper that retracts the tracked assistant bubble by id without touching other bubbles:

```ts
test('retractTrackedAssistantTailText only updates the targeted assistant bubble', () => {
  const messages = [
    { id: 'assistant-1', role: 'assistant', content: '第一气泡' },
    { id: 'assistant-2', role: 'assistant', content: '我需要切换。\n\n[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}]' },
  ];

  const updated = retractTrackedAssistantTailText(messages, 'assistant-2', stripTrailingCanonicalSwitchAgentControl);

  assert.equal(updated[0].content, '第一气泡');
  assert.equal(updated[1].content, '我需要切换。\n\n');
});
```

- [ ] **Step 4: Add failing page-source regressions for handoff-triggered retraction in both pages**

Add assertions in `frontend/src/lib/timeline-projection.test.mts` that both page files import and call the shared retraction helper around the `coordinator_2` handoff boundary:

```ts
assert.match(textPageSource, /retractTrackedAssistantTailText/);
assert.match(visionPageSource, /retractTrackedAssistantTailText/);
assert.match(textPageSource, /timelineEvent\.kind === 'agent_switched'[\s\S]*source === 'coordinator_2'[\s\S]*retractTrackedAssistantTailText/);
assert.match(visionPageSource, /timelineEvent\.kind === 'agent_switched'[\s\S]*source === 'coordinator_2'[\s\S]*retractTrackedAssistantTailText/);
```

- [ ] **Step 5: Run the focused frontend tests and verify they fail first**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/dispatch-message.test.mts" "frontend/src/lib/timeline-projection.test.mts"
```

Expected: FAIL because the new helpers and page integration do not exist yet.

### Task 2: Implement the shared frontend tail-retraction helpers

**Files:**
- Modify: `frontend/src/lib/dispatch-message.ts`
- Test: `frontend/src/lib/dispatch-message.test.mts`

- [ ] **Step 1: Add minimal balanced-tail parsing for canonical switch control spans**

Implement a helper that checks only the trailing tail of a string and retracts it only when the final balanced span is a canonical switch control payload.

Add code in `frontend/src/lib/dispatch-message.ts` shaped like:

```ts
export function stripTrailingCanonicalSwitchAgentControl(text: string): { text: string; retracted: boolean } {
  const trimmedEnd = text.trimEnd();
  const trailingWhitespace = text.slice(trimmedEnd.length);
  const span = findTrailingBalancedControlSpan(trimmedEnd);
  if (!span) {
    return { text, retracted: false };
  }

  const candidate = trimmedEnd.slice(span.start);
  if (!candidate.includes('"type":"agent"') || !candidate.includes('switch_agent')) {
    return { text, retracted: false };
  }

  return {
    text: `${trimmedEnd.slice(0, span.start)}${trailingWhitespace}`,
    retracted: true,
  };
}
```

- [ ] **Step 2: Add the tracked-message mutation helper**

Add a minimal helper that updates only one tracked assistant message by id:

```ts
export function retractTrackedAssistantTailText<T extends AssistantLikeMessage>(
  messages: T[],
  messageId: string | null | undefined,
  retractor: (text: string) => { text: string; retracted: boolean },
): T[] {
  return updateTrackedAssistantMessage(messages, messageId, (message) => {
    if (typeof message.content !== 'string') {
      return message;
    }

    const result = retractor(message.content);
    if (!result.retracted) {
      return message;
    }

    return {
      ...message,
      content: result.text,
    };
  });
}
```

- [ ] **Step 3: Run helper tests and make them pass**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/dispatch-message.test.mts"
```

Expected: PASS for the new helper tests.

### Task 3: Integrate post-handoff retraction into text and vision pages

**Files:**
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Test: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Import the new shared helper into both pages**

Extend the shared helper import lists so both pages import:

```ts
retractTrackedAssistantTailText,
stripTrailingCanonicalSwitchAgentControl,
```

- [ ] **Step 2: Retract bubble-2 tail text when `coordinator_2` handoff is confirmed**

In both timeline callbacks, before creating the execution bubble placeholder, retract the active `coordinator_2` bubble tail:

```ts
if (
  timelineEvent.kind === 'agent_switched'
  && timelineEvent.source === 'coordinator_2'
  && !agentRun.executionAssistantMessageId
) {
  setMessages((prev: Message[]) =>
    retractTrackedAssistantTailText(
      prev,
      getActiveTrackedAssistantMessageId(
        agentRun.assistantMessageId,
        agentRun.projectedAssistantMessageIds,
        agentRun.executionAssistantMessageId,
      ),
      stripTrailingCanonicalSwitchAgentControl,
    ),
  );

  const executionAssistantMessageId = generateId();
  agentRun.executionAssistantMessageId = executionAssistantMessageId;
  setMessages((prev: Message[]) => [
    ...prev,
    {
      id: executionAssistantMessageId,
      role: 'assistant',
      content: '',
      timestamp: new Date(),
      isStreaming: true,
      agentSteps: [],
      bubbleKind: 'execution',
    },
  ]);
}
```

- [ ] **Step 3: Keep retraction narrow and handoff-confirmed only**

Do not add any retraction call in `projection.kind === 'chat_chunk'` or on generic content updates. The only call site should be the confirmed `coordinator_2` handoff boundary.

- [ ] **Step 4: Run page-source regression tests**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts"
```

Expected: PASS, with both pages showing shared retraction helper usage.

### Task 4: Remove backend parser glue that is now redundant and unproven

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/agent/local_llm_model.py`
- Modify: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Re-read the current parser-only additions and keep only the durable hidden-control baseline**

Review the recent partial-prefix parser additions. If a branch exists only to speculate on early incomplete `[`/`{"id"` prefixes and is no longer part of the primary fix strategy, remove it.

- [ ] **Step 2: Delete the redundant partial-prefix helper if it no longer carries product behavior**

If `backend/agent/local_llm_model.py` contains a helper equivalent to `_split_visible_text_and_partial_switch_agent_command_prefix(...)` that was added only for speculative early-frame buffering, remove it and simplify the caller accordingly.

- [ ] **Step 3: Remove or rewrite parser tests that only prove the deleted speculative path**

Keep tests for:

```text
- complete canonical switch payload hiding
- split canonical payload completion across deltas
- no visible switch JSON in final bubble text when backend catches it in time
- secondary_handoff ordering and lifecycle
```

Remove tests that only prove speculative early-prefix buffering if that path is deleted.

- [ ] **Step 4: Run focused backend regressions**

Run:

```bash
PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py -q
```

Expected: PASS, with backend retaining structured handoff correctness even after dead-code cleanup.

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
