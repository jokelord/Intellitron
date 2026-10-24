/**
 * Regression tests for `applyCompactProjection` ordering.
 *
 * Bug history (2026-06-08, after the /compact auto-trigger E2E test):
 *   The agent-mode auto-compact pipeline emits compact_* SSE events
 *   BEFORE the routing / dispatch / content events. The frontend
 *   already created the streaming assistant placeholder when
 *   `handleSend` flushed, so the old projection:
 *
 *     1. marked EVERY message (including the streaming assistant) as
 *        `isPreCompact: true` → opacity-60 on a not-yet-rendered
 *        bubble,
 *     2. appended the compact_summary AT THE END of the messages
 *        array (i.e. AFTER the streaming assistant).
 *
 *   The visible UI order was therefore
 *
 *     [previous assistant, user, STREAMING_ASSISTANT(faded), compact_summary]
 *
 *   which is misleading: the compact happens BEFORE the assistant
 *   response, so the user expects
 *
 *     [previous assistant, user, compact_summary, STREAMING_ASSISTANT]
 *
 *   These tests pin the fix: callers (the agent-mode timeline
 *   handler) pass the streaming assistant's id via
 *   `options.insertBeforeMessageId`; the projection then inserts the
 *   compact_summary at that index and only marks the messages
 *   strictly before that index as `isPreCompact`. The manual
 *   /compact slash-command path keeps the legacy "mark all, append
 *   at end" behavior (no insert hint → fall through to default).
 */
import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import type { MutableRefObject } from "react";

import { applyCompactProjection } from "./compact-projection.ts";
import type { Message } from "./message-types.ts";

const testDir = path.dirname(fileURLToPath(import.meta.url));

function makeRef<T>(initial: T): MutableRefObject<T> {
  return { current: initial } as MutableRefObject<T>;
}

function asstMsg(id: string, extras: Partial<Message> = {}): Message {
  return {
    id,
    role: "assistant",
    content: "",
    timestamp: new Date("2026-06-08T00:00:00Z"),
    isStreaming: true,
    ...extras,
  } as Message;
}

function userMsg(id: string, content: string): Message {
  return {
    id,
    role: "user",
    content,
    timestamp: new Date("2026-06-08T00:00:00Z"),
  } as Message;
}

function makeSetMessages(
  initial: Message[],
): {
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>;
  getMessages: () => Message[];
} {
  let state = initial;
  const setMessages: React.Dispatch<React.SetStateAction<Message[]>> = (updater) => {
    state =
      typeof updater === "function"
        ? (updater as (prev: Message[]) => Message[])(state)
        : updater;
  };
  return { setMessages, getMessages: () => state };
}

test("compact_started with insertBeforeMessageId inserts compact_summary BEFORE that id and only marks pre-boundary messages", () => {
  const previousAssistant = asstMsg("asst-n");
  const currentUser = userMsg("user-n+1", "新问题");
  const streamingAssistant = asstMsg("asst-n+1", { isStreaming: true });
  const messages: Message[] = [previousAssistant, currentUser, streamingAssistant];

  const { setMessages, getMessages } = makeSetMessages(messages);
  const newlyCompactMarkedRef = makeRef<Set<string>>(new Set());

  applyCompactProjection(
    {
      kind: "compact_started",
      summaryMessageId: "compact-1",
      trigger: "auto",
      mode: "agent",
    },
    setMessages,
    newlyCompactMarkedRef,
    { insertBeforeMessageId: streamingAssistant.id },
  );

  const result = getMessages();
  assert.equal(result.length, 4, "should produce one extra message (the compact_summary)");
  assert.equal(
    result[0].id,
    previousAssistant.id,
    "previous assistant should remain at the head",
  );
  assert.equal(
    result[1].id,
    currentUser.id,
    "current user should remain right after previous assistant",
  );
  assert.equal(
    result[2].bubbleKind,
    "compact_summary",
    "the new compact_summary must be inserted before the streaming assistant",
  );
  assert.equal(
    result[2].compactionBoundaryId,
    "compact-1",
    "compact_summary must carry the boundary id",
  );
  assert.equal(result[2].compactStatus, "streaming");
  assert.equal(
    result[2].compactMetadata?.trigger,
    "auto",
    "trigger metadata must be preserved",
  );
  assert.equal(
    result[2].compactMetadata?.mode,
    "agent",
    "mode metadata must be preserved",
  );
  assert.equal(
    result[3].id,
    streamingAssistant.id,
    "the streaming assistant must remain at the end",
  );

  // isPreCompact semantics: with the streaming assistant as the
  // anchor, the messages strictly before the anchor (previous
  // assistant + current user) are marked. The streaming assistant
  // is the post-boundary response and must NOT be marked (it was
  // incorrectly faded in the pre-fix version).
  //
  // NOTE: marking the current user as isPreCompact is the legacy
  // behavior (the original applyCompactProjection marked
  // everything except existing compact_summary bubbles). In the
  // agent mode auto-compact path this is purely a visual hint
  // (the LLM consumes session.agent.memory.steps, NOT the wire
  // metadata filter), so we keep the legacy "mark all up to
  // anchor" semantic to avoid breaking other readers of the
  // message state. The fix is about ARRAY POSITION, not about
  // fine-tuning the isPreCompact boundary.
  assert.equal(
    result[0].isPreCompact,
    true,
    "previous assistant (pre-boundary) must be marked isPreCompact",
  );
  assert.equal(
    result[1].isPreCompact,
    true,
    "current user (pre-anchor) is marked isPreCompact, matching legacy mark-all semantic",
  );
  assert.equal(
    result[3].isPreCompact,
    undefined,
    "streaming assistant is the post-boundary response and must NOT be marked isPreCompact",
  );

  // Rollback set should include only the pre-anchor ids (i.e.
  // everything strictly before the streaming assistant).
  assert.deepEqual(
    Array.from(newlyCompactMarkedRef.current).sort(),
    [previousAssistant.id, currentUser.id].sort(),
    "rollback set must contain exactly the pre-anchor ids",
  );
});

test("compact_started with insertBeforeMessageId keeps existing compact_summary bubbles intact", () => {
  const previousCompact = {
    id: "compact-prev",
    role: "system",
    content: "old summary",
    timestamp: new Date("2026-06-07T00:00:00Z"),
    bubbleKind: "compact_summary",
    compactionBoundaryId: "compact-prev",
    compactStatus: "completed",
  } as Message;
  const user = userMsg("user-current", "再来一个问题");
  const streamingAssistant = asstMsg("asst-current", { isStreaming: true });
  const messages: Message[] = [previousCompact, user, streamingAssistant];

  const { setMessages, getMessages } = makeSetMessages(messages);
  const newlyCompactMarkedRef = makeRef<Set<string>>(new Set());

  applyCompactProjection(
    {
      kind: "compact_started",
      summaryMessageId: "compact-new",
      trigger: "auto",
      mode: "agent",
    },
    setMessages,
    newlyCompactMarkedRef,
    { insertBeforeMessageId: streamingAssistant.id },
  );

  const result = getMessages();
  assert.equal(result.length, 4, "should keep the old compact + add a new one");
  assert.equal(result[0].id, previousCompact.id, "old compact_summary must stay in place");
  assert.equal(result[0].isPreCompact, undefined, "old compact_summary must not be marked isPreCompact");
  assert.equal(result[1].id, user.id, "current user stays between the two summaries");
  assert.equal(result[1].isPreCompact, true, "current user is pre-anchor, must be marked isPreCompact");
  assert.equal(result[2].bubbleKind, "compact_summary", "new compact_summary is inserted before streaming assistant");
  assert.equal(result[2].compactionBoundaryId, "compact-new");
  assert.equal(result[3].id, streamingAssistant.id);
  assert.equal(result[3].isPreCompact, undefined, "streaming assistant is post-boundary, must NOT be marked isPreCompact");
});

test("compact_started falls back to legacy behavior when insertBeforeMessageId is omitted", () => {
  // Manual /compact slash-command path: the caller does NOT have a
  // streaming assistant placeholder to anchor against (it is a
  // standalone slash command that runs against the existing history).
  // We must preserve the legacy semantics: mark all pre-existing
  // non-compact messages isPreCompact and append the summary at the
  // end. This keeps the manual /compact UX unchanged.
  const a = asstMsg("a");
  const u = userMsg("u", "x");
  const messages: Message[] = [a, u];

  const { setMessages, getMessages } = makeSetMessages(messages);
  const newlyCompactMarkedRef = makeRef<Set<string>>(new Set());

  applyCompactProjection(
    {
      kind: "compact_started",
      summaryMessageId: "compact-manual",
      trigger: "manual",
      mode: "chat",
    },
    setMessages,
    newlyCompactMarkedRef,
  );

  const result = getMessages();
  assert.equal(result.length, 3);
  assert.equal(result[0].isPreCompact, true, "first message marked isPreCompact");
  assert.equal(result[1].isPreCompact, true, "second message marked isPreCompact");
  assert.equal(result[2].bubbleKind, "compact_summary", "compact_summary appended at end");
  assert.deepEqual(
    Array.from(newlyCompactMarkedRef.current).sort(),
    ["a", "u"].sort(),
  );
});

test("compact_started falls back to legacy behavior when insertBeforeMessageId does not match any message", () => {
  // The hint should only change placement when it actually anchors
  // to an existing message. If the id is stale / unknown (e.g. the
  // user already cleared the chat between the request and the
  // event), we must NOT silently corrupt the array; we should
  // append at end and mark everything, matching the legacy path.
  const a = asstMsg("a");
  const messages: Message[] = [a];

  const { setMessages, getMessages } = makeSetMessages(messages);
  const newlyCompactMarkedRef = makeRef<Set<string>>(new Set());

  applyCompactProjection(
    {
      kind: "compact_started",
      summaryMessageId: "compact-stale",
      trigger: "auto",
      mode: "agent",
    },
    setMessages,
    newlyCompactMarkedRef,
    { insertBeforeMessageId: "ghost-assistant" },
  );

  const result = getMessages();
  assert.equal(result.length, 2);
  assert.equal(result[0].isPreCompact, true);
  assert.equal(result[1].bubbleKind, "compact_summary", "stale hint must fall through to append-at-end");
});

test("compact_failed rollback after insertBeforeMessageId restores the pre-boundary state cleanly", () => {
  // After compact_started marks pre-boundary messages and inserts a
  // compact_summary BEFORE the streaming assistant, the
  // compact_failed projection must:
  //   1. remove the compact_summary that was just inserted
  //   2. clear isPreCompact on the pre-boundary messages
  //   3. leave the streaming assistant untouched (it was never marked)
  const previousAssistant = asstMsg("asst-n");
  const currentUser = userMsg("user-n+1", "q");
  const streamingAssistant = asstMsg("asst-n+1", { isStreaming: true });
  const messages: Message[] = [previousAssistant, currentUser, streamingAssistant];

  const { setMessages, getMessages } = makeSetMessages(messages);
  const newlyCompactMarkedRef = makeRef<Set<string>>(new Set());

  // 1) Start the compact
  applyCompactProjection(
    {
      kind: "compact_started",
      summaryMessageId: "compact-fail",
      trigger: "auto",
      mode: "agent",
    },
    setMessages,
    newlyCompactMarkedRef,
    { insertBeforeMessageId: streamingAssistant.id },
  );

  let result = getMessages();
  assert.equal(result.length, 4, "sanity: compact_summary was inserted");
  assert.equal(result[0].isPreCompact, true);

  // 2) Compact fails → must roll back to the pre-compact array
  applyCompactProjection(
    {
      kind: "compact_failed",
      summaryMessageId: "compact-fail",
      reason: "llm_unreachable",
    },
    setMessages,
    newlyCompactMarkedRef,
  );

  result = getMessages();
  assert.deepEqual(
    result.map((m) => m.id),
    [previousAssistant.id, currentUser.id, streamingAssistant.id],
    "compact_failed must remove the compact_summary and leave the rest in place",
  );
  // The CSS treats both `false` and `undefined` as "not preCompact"
  // (`message.isPreCompact ? 'opacity-60' : ''` → both falsy). The
  // rollback contract is "falsy", not strictly undefined.
  assert.ok(
    !result[0].isPreCompact,
    "pre-boundary isPreCompact must be falsy on rollback",
  );
  assert.ok(
    !result[1].isPreCompact,
    "current user isPreCompact must remain falsy",
  );
  assert.ok(
    !result[2].isPreCompact,
    "streaming assistant isPreCompact must remain falsy",
  );
});

test("compact_delta and compact_completed target the inserted compact_summary by boundary id", () => {
  const streamingAssistant = asstMsg("asst-n+1", { isStreaming: true });
  const { setMessages, getMessages } = makeSetMessages([userMsg("u", "x"), streamingAssistant]);
  const newlyCompactMarkedRef = makeRef<Set<string>>(new Set());

  // 1) Insert compact_summary BEFORE streaming assistant
  applyCompactProjection(
    {
      kind: "compact_started",
      summaryMessageId: "compact-x",
      trigger: "auto",
      mode: "agent",
    },
    setMessages,
    newlyCompactMarkedRef,
    { insertBeforeMessageId: streamingAssistant.id },
  );

  // 2) Append a delta chunk
  applyCompactProjection(
    {
      kind: "compact_delta",
      summaryMessageId: "compact-x",
      text: "partial ",
      partial: true,
    },
    setMessages,
    newlyCompactMarkedRef,
  );

  // 3) Mark completed with the cleaned text
  applyCompactProjection(
    {
      kind: "compact_completed",
      summaryMessageId: "compact-x",
      text: "partial done",
      metadata: { summary_tokens: 99 },
    },
    setMessages,
    newlyCompactMarkedRef,
  );

  const result = getMessages();
  const summary = result[1];
  assert.equal(summary.bubbleKind, "compact_summary", "summary must sit between user and assistant");
  assert.equal(summary.compactionBoundaryId, "compact-x");
  assert.equal(summary.content, "partial done", "compact_completed must overwrite the streamed text");
  assert.equal(summary.compactStatus, "completed");
  assert.equal(
    summary.compactMetadata?.summaryTokens,
    99,
    "compact_completed metadata must surface summary_tokens",
  );
  assert.equal(result[2].id, streamingAssistant.id, "streaming assistant must remain at the tail");
});

test("applyCompactProjection accepts the legacy 3-arg signature for back-compat with manual /compact calls", async () => {
  const { setMessages, getMessages } = makeSetMessages([asstMsg('history', { isStreaming: false }), asstMsg('tail')]);
  applyCompactProjection({ kind: 'compact_started', summaryMessageId: 'manual-summary' }, setMessages, makeRef(new Set<string>()));
  assert.deepEqual(getMessages().map(message => message.id), ['history', 'tail', 'manual-summary']);
  assert.equal(getMessages()[0].isPreCompact, true);
  assert.equal(getMessages()[1].isPreCompact, true);
});

test("vision page also wires insertBeforeMessageId into the auto-compact timeline handler", async () => {
  for (const relative of ['../app/page.tsx', '../app/vision/page.tsx']) {
    const source = await readFile(path.join(testDir, relative), 'utf8');
    assert.match(source, /runAgentConversationTransport\(/);
    assert.match(source, /runChatConversationTransport\(/);
    assert.match(source, /useConversationCommands\(/);
  }
  // Agent and Chat intentionally have different anchors: assistant and current
  // user respectively. Their real event-projection behavior is tested above and
  // by conversation-transport.test.mts; neither route owns a shadow handler.
  const agent = await readFile(new URL('./text-agent-session-handlers.ts', import.meta.url), 'utf8');
  const chat = await readFile(new URL('./conversation/conversation-transport.ts', import.meta.url), 'utf8');
  assert.match(agent, /insertBeforeMessageId: run\.assistantMessageId/);
  assert.match(chat, /insertBeforeMessageId:\s*projectMemory\.getUserMessageId\(\)/);
});

test('compact_completed metadata does not get appended to summary content', () => {
  const messages: Message[] = [{
    id: 'compact-1',
    role: 'system',
    content: '',
    timestamp: new Date('2026-06-14T00:00:00Z'),
    bubbleKind: 'compact_summary',
    compactionBoundaryId: 'compact-1',
    compactStatus: 'streaming',
  } as Message];
  const { setMessages, getMessages } = makeSetMessages(messages);
  const ref = makeRef<Set<string>>(new Set());

  applyCompactProjection({
    kind: 'compact_completed',
    summaryMessageId: 'compact-1',
    text: 'Clean summary',
    metadata: { prompt_shape: 'cache_first_tail', cached_tokens: 900 },
  }, setMessages, ref);

  assert.equal(getMessages()[0].content, 'Clean summary');
});
