# Agent-Mode Reasoning Folding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add foldable reasoning UI for normal assistant chat bubbles in non-agent mode and `coordinator_2` agent-mode bubbles, while keeping `basic_agent` thought/observation cards and all existing stable routing/streaming/dedup behavior unchanged.

**Architecture:** Keep backend and SSE contracts unchanged. Introduce a small frontend-only incremental reasoning parser that can split streamed `<think>...</think>` content into `reasoning_content` plus answer text, then reuse a shared reasoning-fold UI in both text and vision pages. Wire the parser into both normal chat streaming and `coordinator_2` projected chat chunks so both paths share the same behavior and tests.

**Tech Stack:** Next.js 14, React 18, TypeScript, Node built-in test runner, existing frontend reducer helpers.

---

## File Map

### Create
- `frontend/src/lib/assistant-reasoning.ts`
  - Pure incremental parser + assistant-message updater helpers for explicit reasoning deltas and `<think>` tagged content deltas.
- `frontend/src/lib/assistant-reasoning.test.mts`
  - Focused Node tests for parser state transitions and streamed split behavior.
- `frontend/src/components/AssistantReasoningSection.tsx`
  - Shared fold/expand UI for normal assistant chat bubbles only.

### Modify
- `frontend/src/lib/dispatch-message.ts`
  - Delegate projected `coordinator_2` chat bubble chunk handling to the new assistant reasoning helper.
- `frontend/src/lib/dispatch-message.test.mts`
  - Lock reducer behavior for projected chat chunks with reasoning split and preserve existing message-id/dedup-safe behavior.
- `frontend/src/lib/answer-now-ui.ts`
  - Tighten assistant renderable-text fallback so “reasoning-only, no answer yet” does not leak reasoning into the main bubble body.
- `frontend/src/lib/answer-now-ui.test.mjs`
  - Cover new placeholder behavior for reasoning-only assistant bubbles.
- `frontend/src/app/page.tsx`
  - Use the shared reasoning-fold UI for normal assistant bubbles and route normal chat chunks through the shared assistant reasoning helper.
- `frontend/src/app/vision/page.tsx`
  - Mirror the same reasoning-fold UI and streaming helper usage as the text page.
- `frontend/src/lib/timeline-projection.test.mts`
  - Add source-level assertions that text/vision pages both use the shared reasoning component/helper and keep `basic_agent` card rendering intact.

### Verification targets
- `frontend/src/lib/assistant-reasoning.test.mts`
- `frontend/src/lib/dispatch-message.test.mts`
- `frontend/src/lib/answer-now-ui.test.mjs`
- `frontend/src/lib/timeline-projection.test.mts`
- `npm run type-check`
- `npm run build`
- Focused backend regression slices already guarding `coordinator_2` message-id and dedup behavior

---

### Task 1: Build the shared incremental reasoning parser

**Files:**
- Create: `frontend/src/lib/assistant-reasoning.ts`
- Create: `frontend/src/lib/assistant-reasoning.test.mts`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/lib/assistant-reasoning.test.mts` with parser coverage that matches the approved spec:

```ts
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  appendAssistantDelta,
  createAssistantReasoningState,
} from './assistant-reasoning.ts';

test('content delta without think tags stays in the visible answer body', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });
  const next = appendAssistantDelta(initial, {
    contentDelta: '直接答案',
    defaultReasoningCollapsed: false,
  });

  assert.equal(next.content, '直接答案');
  assert.equal(next.reasoning_content, '');
  assert.equal(next.reasoningDetected, false);
  assert.equal(next.reasoningParseState, 'outside');
});

test('single chunk think block is split into reasoning and final answer', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: true });
  const next = appendAssistantDelta(initial, {
    contentDelta: '<think>先分析问题</think>最终答案',
    defaultReasoningCollapsed: true,
  });

  assert.equal(next.reasoning_content, '先分析问题');
  assert.equal(next.content, '最终答案');
  assert.equal(next.reasoningDetected, true);
  assert.equal(next.reasoningCollapsed, true);
  assert.equal(next.reasoningParseState, 'outside');
});

test('split think tags across chunks keep parser state until the closing tag arrives', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });

  const first = appendAssistantDelta(initial, {
    contentDelta: '<thi',
    defaultReasoningCollapsed: false,
  });
  assert.equal(first.content, '');
  assert.equal(first.reasoning_content, '');

  const second = appendAssistantDelta(first, {
    contentDelta: 'nk>逐步推理',
    defaultReasoningCollapsed: false,
  });
  assert.equal(second.reasoning_content, '逐步推理');
  assert.equal(second.reasoningParseState, 'inside_think');

  const third = appendAssistantDelta(second, {
    contentDelta: '</th',
    defaultReasoningCollapsed: false,
  });
  assert.equal(third.reasoning_content, '逐步推理');
  assert.equal(third.reasoningParseState, 'inside_think');

  const fourth = appendAssistantDelta(third, {
    contentDelta: 'ink>答案',
    defaultReasoningCollapsed: false,
  });
  assert.equal(fourth.reasoning_content, '逐步推理');
  assert.equal(fourth.content, '答案');
  assert.equal(fourth.reasoningParseState, 'outside');
});

test('explicit reasoning delta and tagged reasoning both accumulate into reasoning_content', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });
  const withReasoning = appendAssistantDelta(initial, {
    reasoningDelta: '显式推理',
    defaultReasoningCollapsed: false,
  });
  const final = appendAssistantDelta(withReasoning, {
    contentDelta: '<think>补充推理</think>答案',
    defaultReasoningCollapsed: false,
  });

  assert.equal(final.reasoning_content, '显式推理补充推理');
  assert.equal(final.content, '答案');
  assert.equal(final.reasoningCollapsed, false);
});

test('reasoning-only streaming keeps the visible answer body empty', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: true });
  const next = appendAssistantDelta(initial, {
    contentDelta: '<think>还在推理中</think>',
    defaultReasoningCollapsed: true,
  });

  assert.equal(next.reasoning_content, '还在推理中');
  assert.equal(next.content, '');
  assert.equal(next.reasoningDetected, true);
});

test('literal think markup stays visible once answer text has already started', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });
  const next = appendAssistantDelta(initial, {
    contentDelta: '答案<think>字面量</think>尾巴',
    defaultReasoningCollapsed: false,
  });

  assert.equal(next.content, '答案<think>字面量</think>尾巴');
  assert.equal(next.reasoning_content, '');
  assert.equal(next.reasoningDetected, false);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/assistant-reasoning.test.mts"
```

Expected: FAIL because `frontend/src/lib/assistant-reasoning.ts` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/lib/assistant-reasoning.ts` with a pure parser that preserves stream state across chunks:

```ts
export type AssistantReasoningFields = {
  content?: string;
  reasoning_content?: string;
  reasoningCollapsed?: boolean;
  reasoningDetected?: boolean;
  reasoningParseState?: 'outside' | 'inside_think';
  reasoningBuffer?: string;
};

type CreateStateOptions = {
  defaultReasoningCollapsed: boolean;
};

type AppendAssistantDeltaOptions = {
  contentDelta?: string;
  reasoningDelta?: string;
  defaultReasoningCollapsed: boolean;
};

type FinalizeAssistantReasoningOptions = {
  defaultReasoningCollapsed: boolean;
};

const OPEN_TAG = '<think>';
const CLOSE_TAG = '</think>';
const OPEN_PREFIXES = Array.from({ length: OPEN_TAG.length - 1 }, (_, i) => OPEN_TAG.slice(0, i + 1));
const CLOSE_PREFIXES = Array.from({ length: CLOSE_TAG.length - 1 }, (_, i) => CLOSE_TAG.slice(0, i + 1));

export function createAssistantReasoningState(
  options: CreateStateOptions,
): Required<AssistantReasoningFields> {
  return {
    content: '',
    reasoning_content: '',
    reasoningCollapsed: options.defaultReasoningCollapsed,
    reasoningDetected: false,
    reasoningParseState: 'outside',
    reasoningBuffer: '',
  };
}

function longestTrailingPrefix(value: string, prefixes: string[]): string {
  for (let i = prefixes.length - 1; i >= 0; i -= 1) {
    if (value.endsWith(prefixes[i])) {
      return prefixes[i];
    }
  }
  return '';
}

function ensureReasoningDefaults<T extends AssistantReasoningFields>(
  message: T,
  defaultReasoningCollapsed: boolean,
): T & Required<AssistantReasoningFields> {
  return {
    ...message,
    content: typeof message.content === 'string' ? message.content : '',
    reasoning_content: typeof message.reasoning_content === 'string' ? message.reasoning_content : '',
    reasoningCollapsed: message.reasoningCollapsed ?? defaultReasoningCollapsed,
    reasoningDetected: message.reasoningDetected ?? false,
    reasoningParseState: message.reasoningParseState ?? 'outside',
    reasoningBuffer: message.reasoningBuffer ?? '',
  };
}

export function appendAssistantDelta<T extends AssistantReasoningFields>(
  message: T,
  options: AppendAssistantDeltaOptions,
): T & Required<AssistantReasoningFields> {
  const next = ensureReasoningDefaults(message, options.defaultReasoningCollapsed);

  if (options.reasoningDelta) {
    next.reasoning_content += options.reasoningDelta;
    next.reasoningDetected = true;
  }

  let pending = `${next.reasoningBuffer}${options.contentDelta ?? ''}`;
  next.reasoningBuffer = '';

  while (pending.length > 0) {
    if (next.reasoningParseState === 'outside') {
      if (next.content.length > 0) {
        next.content += pending;
        break;
      }

      const openIndex = pending.indexOf(OPEN_TAG);
      if (openIndex !== -1) {
        next.content += pending.slice(0, openIndex);

        if (next.content.length > 0) {
          next.content += pending.slice(openIndex);
          break;
        }

        next.reasoningDetected = true;
        pending = pending.slice(openIndex + OPEN_TAG.length);
        next.reasoningParseState = 'inside_think';
        continue;
      }

      const openPrefix = longestTrailingPrefix(pending, OPEN_PREFIXES);
      if (openPrefix) {
        next.content += pending.slice(0, pending.length - openPrefix.length);
        next.reasoningBuffer = openPrefix;
      } else {
        next.content += pending;
      }
      break;
    }

    const closeIndex = pending.indexOf(CLOSE_TAG);
    if (closeIndex !== -1) {
      next.reasoning_content += pending.slice(0, closeIndex);
      pending = pending.slice(closeIndex + CLOSE_TAG.length);
      next.reasoningParseState = 'outside';
      continue;
    }

    const closePrefix = longestTrailingPrefix(pending, CLOSE_PREFIXES);
    if (closePrefix) {
      next.reasoning_content += pending.slice(0, pending.length - closePrefix.length);
      next.reasoningBuffer = closePrefix;
    } else {
      next.reasoning_content += pending;
    }
    break;
  }

  return next;
}

export function finalizeAssistantReasoning<T extends AssistantReasoningFields>(
  message: T,
  options: FinalizeAssistantReasoningOptions,
): T & Required<AssistantReasoningFields> {
  const next = ensureReasoningDefaults(message, options.defaultReasoningCollapsed);

  if (next.reasoningBuffer) {
    if (next.reasoningParseState === 'inside_think') {
      next.reasoning_content += next.reasoningBuffer;
      next.reasoningDetected = true;
    } else {
      next.content += next.reasoningBuffer;
    }
    next.reasoningBuffer = '';
  }

  next.reasoningParseState = 'outside';
  return next;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/assistant-reasoning.test.mts"
```

Expected: PASS with the Task 1 parser tests.

- [ ] **Step 5: Commit**

Only if the user explicitly wants commits:

```bash
git add frontend/src/lib/assistant-reasoning.ts frontend/src/lib/assistant-reasoning.test.mts
git commit -m "feat: add incremental assistant reasoning parser"
```

### Task 2: Route normal chat and projected `coordinator_2` chunks through the shared parser

**Files:**
- Modify: `frontend/src/lib/dispatch-message.ts`
- Modify: `frontend/src/lib/dispatch-message.test.mts`
- Modify: `frontend/src/lib/answer-now-ui.ts`
- Modify: `frontend/src/lib/answer-now-ui.test.mjs`

- [ ] **Step 1: Write the failing tests**

Extend `frontend/src/lib/dispatch-message.test.mts` and `frontend/src/lib/answer-now-ui.test.mjs` with behavior that proves the parser is now in the reducer path and that the visible answer body stays empty until an answer arrives:

```ts
test('appendProjectedChatChunk splits coordinator_2 reasoning from the visible answer body', () => {
  const messages = [{
    id: 'assistant-projected',
    role: 'assistant',
    content: '',
    timestamp: new Date('2026-05-19T10:00:00Z'),
    isStreaming: true,
  }];

  const updated = appendProjectedChatChunk(messages, {
    messageId: 'assistant-projected',
    text: '<think>先推导</think>最终答案',
    partial: true,
  }, () => new Date('2026-05-19T10:00:01Z'), undefined, {
    defaultReasoningCollapsed: true,
  });

  assert.equal(updated[0].reasoning_content, '先推导');
  assert.equal(updated[0].content, '最终答案');
  assert.equal(updated[0].reasoningDetected, true);
  assert.equal(updated[0].reasoningCollapsed, true);
});

test('appendProjectedChatChunk keeps reasoning-only chunks out of the visible answer body', () => {
  const messages = [{
    id: 'assistant-projected',
    role: 'assistant',
    content: '',
    timestamp: new Date('2026-05-19T10:00:00Z'),
    isStreaming: true,
  }];

  const updated = appendProjectedChatChunk(messages, {
    messageId: 'assistant-projected',
    text: '<think>仍在推理</think>',
    partial: true,
  }, () => new Date('2026-05-19T10:00:01Z'), undefined, {
    defaultReasoningCollapsed: false,
  });

  assert.equal(updated[0].reasoning_content, '仍在推理');
  assert.equal(updated[0].content, '');
});
```

```js
test('getAssistantRenderableText keeps the main bubble body empty while only reasoning is streaming', () => {
  const message = {
    id: 'assistant-1',
    role: 'assistant',
    content: '',
    reasoning_content: '还在推理',
    reasoningDetected: true,
    isStreaming: true,
    timestamp: new Date('2024-01-01T00:00:00.000Z'),
  };

  assert.equal(getAssistantRenderableText(message, false), '');
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/dispatch-message.test.mts" "frontend/src/lib/answer-now-ui.test.mjs"
```

Expected: FAIL because `appendProjectedChatChunk()` does not accept reasoning options yet and currently appends tagged text directly into `content`.

- [ ] **Step 3: Write minimal implementation**

Update `frontend/src/lib/dispatch-message.ts` to delegate assistant chunk merging to the shared parser, and update `frontend/src/lib/answer-now-ui.ts` so a reasoning-only assistant bubble does not show placeholder text in the main Markdown body.

```ts
import { appendAssistantDelta, createAssistantReasoningState } from './assistant-reasoning.ts';

type AssistantReasoningOptions = {
  defaultReasoningCollapsed: boolean;
};

export function appendProjectedChatChunk<T extends AssistantLikeMessage & AssistantReasoningFields>(
  messages: T[],
  chunk: ProjectedChatChunk,
  createTimestamp: () => Date,
  updateExisting?: (message: T) => T,
  reasoningOptions: AssistantReasoningOptions = { defaultReasoningCollapsed: false },
): T[] {
  const appendToMessage = (message: T): T => {
    const currentMessage = updateExisting ? updateExisting({ ...message }) : { ...message };
    return {
      ...appendAssistantDelta(
        currentMessage.content === undefined && currentMessage.reasoning_content === undefined
          ? ({ ...currentMessage, ...createAssistantReasoningState(reasoningOptions) } as T)
          : currentMessage,
        {
          contentDelta: chunk.text,
          defaultReasoningCollapsed: reasoningOptions.defaultReasoningCollapsed,
        },
      ),
      isStreaming: chunk.partial,
    } as T;
  };

  if (chunk.messageId) {
    const targetIndex = messages.findIndex((message) => message.id === chunk.messageId && message.role === 'assistant');
    if (targetIndex !== -1) {
      const updated = [...messages];
      updated[targetIndex] = appendToMessage(updated[targetIndex] as T);
      return updated;
    }

    return [
      ...messages,
      {
        id: chunk.messageId,
        role: 'assistant',
        timestamp: createTimestamp(),
        isStreaming: chunk.partial,
        ...appendAssistantDelta(createAssistantReasoningState(reasoningOptions), {
          contentDelta: chunk.text,
          defaultReasoningCollapsed: reasoningOptions.defaultReasoningCollapsed,
        }),
      } as T,
    ];
  }

  if (messages.length === 0) {
    return messages;
  }

  const updated = [...messages];
  const lastMessage = updated[updated.length - 1];
  if (!lastMessage || lastMessage.role !== 'assistant') {
    return updated;
  }

  updated[updated.length - 1] = appendToMessage(lastMessage as T);
  return updated;
}
```

```ts
export function getAssistantRenderableText(
  message: Pick<AnswerNowUiMessage, 'content' | 'isStreaming' | 'agentSteps' | 'reasoning_content' | 'reasoningDetected'>,
  isStopped: boolean,
): string {
  if (typeof message.content === 'string' && message.content) {
    return isStopped ? `${message.content} [已停止]` : message.content;
  }

  if (message.reasoningDetected || (typeof message.reasoning_content === 'string' && message.reasoning_content.length > 0)) {
    return isStopped ? '已停止' : '';
  }

  if ((message.agentSteps?.length ?? 0) > 0) {
    if (message.isStreaming) {
      return '';
    }
    return isStopped ? '已停止' : '';
  }

  return isStopped ? '已停止' : '思考中...';
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/assistant-reasoning.test.mts" "frontend/src/lib/dispatch-message.test.mts" "frontend/src/lib/answer-now-ui.test.mjs"
```

Expected: PASS with all existing tests plus the new reasoning-path tests.

- [ ] **Step 5: Commit**

Only if the user explicitly wants commits:

```bash
git add frontend/src/lib/assistant-reasoning.ts frontend/src/lib/assistant-reasoning.test.mts frontend/src/lib/dispatch-message.ts frontend/src/lib/dispatch-message.test.mts frontend/src/lib/answer-now-ui.ts frontend/src/lib/answer-now-ui.test.mjs
git commit -m "feat: split streamed reasoning from assistant bubble text"
```

### Task 3: Add the shared fold/expand UI and wire both pages to it

**Files:**
- Create: `frontend/src/components/AssistantReasoningSection.tsx`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Write the failing test**

Extend `frontend/src/lib/timeline-projection.test.mts` with source assertions that lock the shared UI usage in both pages and protect `basic_agent` card rendering:

```ts
test('text and vision pages use the shared assistant reasoning fold UI without changing agent step cards', async () => {
  const [textPageSource, visionPageSource, componentSource] = await Promise.all([
    readFile(path.join(testDir, '../app/page.tsx'), 'utf8'),
    readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8'),
    readFile(path.join(testDir, '../components/AssistantReasoningSection.tsx'), 'utf8'),
  ]);

  assert.match(textPageSource, /<AssistantReasoningSection/);
  assert.match(visionPageSource, /<AssistantReasoningSection/);
  assert.match(textPageSource, /defaultExpanded=\{settings\.showReasoning\}/);
  assert.match(visionPageSource, /defaultExpanded=\{settings\.showReasoning\}/);
  assert.match(textPageSource, /appendAssistantDelta\(/);
  assert.match(visionPageSource, /appendAssistantDelta\(/);
  assert.match(textPageSource, /<AgentStepDisplay steps=\{message\.agentSteps\} \/>/);
  assert.match(visionPageSource, /<AgentStepDisplay steps=\{message\.agentSteps\} \/>/);
  assert.match(componentSource, /aria-expanded=\{!collapsed\}/);
  assert.match(componentSource, /type="button"/);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts"
```

Expected: FAIL because the shared component does not exist yet and the pages still render raw italic reasoning text.

- [ ] **Step 3: Write minimal implementation**

Create a small shared component and wire both pages to it. The UI must only render for normal assistant bubbles; `AgentStepDisplay` stays untouched.

```tsx
type AssistantReasoningSectionProps = {
  messageId: string;
  reasoning: string;
  collapsed: boolean;
  isStreaming: boolean;
  onToggle: () => void;
};

export default function AssistantReasoningSection({
  messageId,
  reasoning,
  collapsed,
  isStreaming,
  onToggle,
}: AssistantReasoningSectionProps) {
  if (!reasoning) {
    return null;
  }

  const panelId = `${messageId}-reasoning`;

  return (
    <div className="mb-3 rounded-xl border border-gray-200 bg-gray-50/80 dark:border-gray-700 dark:bg-gray-900/40">
      <button
        type="button"
        aria-expanded={!collapsed}
        aria-controls={panelId}
        onClick={onToggle}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-sm text-gray-600 dark:text-gray-300"
      >
        <span>{collapsed ? '展开推理过程' : '收起推理过程'}</span>
        <span className="text-xs text-gray-400">{isStreaming ? '流式更新中' : '已完成'}</span>
      </button>
      {!collapsed && (
        <div id={panelId} className="border-t border-gray-200 px-3 py-2 text-sm italic text-gray-500 dark:border-gray-700 dark:text-gray-400 whitespace-pre-wrap">
          {reasoning}
        </div>
      )}
    </div>
  );
}
```

In both `frontend/src/app/page.tsx` and `frontend/src/app/vision/page.tsx`:

```tsx
import AssistantReasoningSection from '@/components/AssistantReasoningSection';
import { appendAssistantDelta } from '@/lib/assistant-reasoning';
```

Update normal chat `onChunk` to use the shared helper instead of appending `content` / `reasoning_content` manually:

```tsx
(content: string, reasoning?: string) => {
  setMessages((prev: Message[]) => {
    const updated = [...prev];
    const lastMessage = updated[updated.length - 1];
    if (lastMessage && lastMessage.role === 'assistant') {
      updated[updated.length - 1] = {
        ...lastMessage,
        ...appendAssistantDelta(lastMessage, {
          contentDelta: content,
          reasoningDelta: reasoning,
          defaultReasoningCollapsed: !settings.showReasoning,
        }),
      };
    }
    return updated;
  });
}
```

Update projected `coordinator_2` chunk calls to pass reasoning defaults into `appendProjectedChatChunk()`:

```tsx
return appendProjectedChatChunk(prev, projection, () => new Date(), (message) => {
  if (message.id !== agentRun.assistantMessageId) {
    return message;
  }

  const messageSteps = message.agentSteps || [];
  const messageLastStep = messageSteps.length > 0 ? messageSteps[messageSteps.length - 1] : null;
  const completedDispatchStep = completeRunningDispatchStep(messageLastStep);
  return {
    ...message,
    agentSteps: completedDispatchStep !== messageLastStep && completedDispatchStep
      ? [...messageSteps.slice(0, -1), completedDispatchStep]
      : message.agentSteps,
  };
}, {
  defaultReasoningCollapsed: !settings.showReasoning,
});
```

Replace the inline italic reasoning block with the shared reasoning section in both pages:

```tsx
<AssistantReasoningSection
  messageId={message.id}
  reasoning={message.reasoning_content || ''}
  collapsed={message.reasoningCollapsed ?? !settings.showReasoning}
  isStreaming={Boolean(message.isStreaming)}
  onToggle={() => setMessages((prev: Message[]) => updateTrackedAssistantMessage(prev, message.id, (target) => ({
    ...target,
    reasoningCollapsed: !(target.reasoningCollapsed ?? !settings.showReasoning),
  })))}
/>
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/assistant-reasoning.test.mts" "frontend/src/lib/dispatch-message.test.mts" "frontend/src/lib/answer-now-ui.test.mjs" "frontend/src/lib/timeline-projection.test.mts"
```

Expected: PASS, including the new page/component source assertions.

- [ ] **Step 5: Commit**

Only if the user explicitly wants commits:

```bash
git add frontend/src/components/AssistantReasoningSection.tsx frontend/src/app/page.tsx frontend/src/app/vision/page.tsx frontend/src/lib/timeline-projection.test.mts
git commit -m "feat: add foldable reasoning UI for assistant chat bubbles"
```

### Task 4: Run protection regressions and final verification

**Files:**
- Verify only: `frontend/src/lib/assistant-reasoning.test.mts`
- Verify only: `frontend/src/lib/dispatch-message.test.mts`
- Verify only: `frontend/src/lib/answer-now-ui.test.mjs`
- Verify only: `frontend/src/lib/timeline-projection.test.mts`
- Verify only: `tests/test_multi_agent_runner.py`
- Verify only: `tests/test_agent_session_context.py`

- [ ] **Step 1: Run the focused frontend test suite**

Run:

```bash
node --test --experimental-strip-types "frontend/src/lib/assistant-reasoning.test.mts" "frontend/src/lib/dispatch-message.test.mts" "frontend/src/lib/answer-now-ui.test.mjs" "frontend/src/lib/timeline-projection.test.mts"
```

Expected: PASS.

- [ ] **Step 2: Run type checking**

Run from `frontend/`:

```bash
npm run type-check
```

Expected: PASS.

- [ ] **Step 3: Run production build**

Run from `frontend/`:

```bash
npm run build
```

Expected: PASS.

- [ ] **Step 4: Run backend protection slices for `coordinator_2`**

Run from the worktree root:

```bash
PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_multi_agent_runner.py::test_run_basic_stream_uses_distinct_coordinator_2_message_id_per_run tests/test_multi_agent_runner.py::test_run_basic_stream_does_not_duplicate_coordinator_2_message_delta_on_action_step_flush tests/test_multi_agent_runner.py::test_run_basic_stream_deduplicates_coordinator_2_final_answer_when_stream_already_includes_reasoning_and_answer tests/test_agent_session_context.py -q
```

Expected: PASS. This protects the message-id uniqueness, dedup fix, and session/finalize behavior that must not regress.

- [ ] **Step 5: Commit**

Only if the user explicitly wants commits:

```bash
git add frontend/src/lib/assistant-reasoning.ts frontend/src/lib/assistant-reasoning.test.mts frontend/src/lib/dispatch-message.ts frontend/src/lib/dispatch-message.test.mts frontend/src/lib/answer-now-ui.ts frontend/src/lib/answer-now-ui.test.mjs frontend/src/components/AssistantReasoningSection.tsx frontend/src/app/page.tsx frontend/src/app/vision/page.tsx frontend/src/lib/timeline-projection.test.mts
git commit -m "feat: fold assistant reasoning in normal and coordinator chat bubbles"
```
