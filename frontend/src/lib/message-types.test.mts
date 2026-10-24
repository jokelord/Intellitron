/**
 * Regression tests for the /compact wire-format translation.
 *
 * Bug history (2026-06-06):
 *   After /compact, the frontend marked old messages with
 *   `isPreCompact: true` and created a new `bubbleKind: 'compact_summary'`
 *   message. Both were LOCAL state. When `handleSend()` built the request
 *   body for `/v1/chat/completions`, it only copied `role` + `content` —
 *   the compact flags were silently dropped, so the backend
 *   `filter_messages_for_llm` had no metadata to act on. The LLM
 *   received the unfiltered full history. SGLang terminal went from
 *   ~3k tokens (during compact) back to ~40k tokens (next chat).
 *
 * Fix: `truncateMessagesForContext` now translates the local flags to
 * wire `metadata` so the backend can apply the filter.
 *
 * These tests pin the contract: the wire `ChatMessage[]` produced by
 * the helper MUST carry the compact metadata so the backend's filter
 * has the markers it needs.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildAgentContextMessages, buildChatWireMessages, decideChatSettingsPersist, truncateMessagesForContext, generateId } from "./message-types.ts";

function makeMsg(
  role: "system" | "user" | "assistant",
  content: string,
  extras: Record<string, unknown> = {}
) {
  return {
    id: generateId(),
    timestamp: new Date(),
    role,
    content,
    ...extras,
  } as any;
}

test("truncateMessagesForContext attaches pre_compact metadata for isPreCompact messages", () => {
  const messages = [
    makeMsg("user", "old message 1", { isPreCompact: true }),
    makeMsg("assistant", "old reply 1", { isPreCompact: true }),
    makeMsg("user", "current question"),
  ];
  const out = truncateMessagesForContext(messages, 100_000, undefined);
  // All three should be present (small enough to fit)
  assert.equal(out.length, 3, "all messages should be retained for small input");
  // First two should carry pre_compact metadata
  assert.deepEqual(out[0].metadata, { pre_compact: true }, "old user message must carry pre_compact");
  assert.deepEqual(out[1].metadata, { pre_compact: true }, "old assistant reply must carry pre_compact");
  // Current question should NOT carry pre_compact
  assert.equal(out[2].metadata, undefined, "current user message must NOT carry pre_compact");
});

test("truncateMessagesForContext attaches compact_summary metadata for compact_summary bubbles", () => {
  const summaryId = "compact-summary-123";
  const messages = [
    makeMsg("user", "old", { isPreCompact: true }),
    makeMsg("system", "summary text", {
      bubbleKind: "compact_summary",
      compactionBoundaryId: summaryId,
    }),
    makeMsg("user", "new question"),
  ];
  const out = truncateMessagesForContext(messages, 100_000, undefined);
  assert.equal(out.length, 3);
  // Summary message should carry compact_summary + summary_id
  assert.deepEqual(
    out[1].metadata,
    { compact_summary: true, summary_id: summaryId },
    "compact_summary message must carry wire metadata"
  );
});

test("truncateMessagesForContext does not attach metadata to ordinary messages", () => {
  const messages = [
    makeMsg("user", "hi"),
    makeMsg("assistant", "hello"),
  ];
  const out = truncateMessagesForContext(messages, 100_000, undefined);
  assert.equal(out.length, 2);
  assert.equal(out[0].metadata, undefined, "ordinary user message must NOT carry metadata");
  assert.equal(out[1].metadata, undefined, "ordinary assistant message must NOT carry metadata");
});

test("truncateMessagesForContext preserves non-compact metadata fields", () => {
  // If a message has a `metadata` object already, the helper must
  // forward it (e.g. external OpenAI clients sending metadata).
  const messages = [
    makeMsg("user", "hi", { metadata: { trace_id: "abc" } }),
  ];
  const out = truncateMessagesForContext(messages, 100_000, undefined);
  assert.equal(out.length, 1);
  assert.deepEqual(out[0].metadata, { trace_id: "abc" });
});

test("truncateMessagesForContext does not double-add pre_compact when metadata already has it", () => {
  // If a message has `isPreCompact: true` AND already has metadata
  // with pre_compact, the helper should not clobber the existing metadata.
  const messages = [
    makeMsg("user", "old", {
      isPreCompact: true,
      metadata: { pre_compact: true, source: "external" },
    }),
  ];
  const out = truncateMessagesForContext(messages, 100_000, undefined);
  assert.equal(out.length, 1);
  // Existing metadata should be preserved (not overwritten with just
  // {pre_compact: true}).
  assert.equal(out[0].metadata?.pre_compact, true);
  assert.equal(out[0].metadata?.source, "external");
});

test("truncateMessagesForContext output can be spread to wire format without losing metadata", () => {
  // Regression: the page-level handleSend used to copy messages
  // field-by-field as {role, content} which silently dropped the
  // metadata field. The fix is to spread the helper's output as-is.
  // This test pins the contract: spread(...) of the helper's output
  // is wire-format-ready and includes metadata.
  const messages = [
    makeMsg("user", "old", { isPreCompact: true }),
    makeMsg("system", "summary text", {
      bubbleKind: "compact_summary",
      compactionBoundaryId: "s-1",
    }),
    makeMsg("user", "new question"),
  ];
  // No systemPrompt arg → truncateMessagesForContext does NOT add a
  // leading system message, so the output preserves the user's
  // history as-is. The page-level filter rule is: skip system
  // messages UNLESS they carry compact_summary metadata.
  const out = truncateMessagesForContext(messages, 100_000, undefined);
  const wireFormat = out
    .filter((m) => {
      // Skip the leading system message added by truncateMessagesForContext
      // (no compact metadata → skip). Keep compact_summary bubbles.
      if (m.role === "system" && m.metadata?.["compact_summary"] !== true) {
        return false;
      }
      return true;
    })
    .map((m) => ({ ...m }));
  // Pre-compact message carries metadata
  assert.equal(wireFormat[0].metadata?.pre_compact, true);
  // Compact summary bubble (role=system but has compact metadata) IS kept
  assert.equal(wireFormat[1].metadata?.compact_summary, true);
  assert.equal(wireFormat[1].metadata?.summary_id, "s-1");
  // New question has no metadata
  assert.equal(wireFormat[2].metadata, undefined);
});

test("buildChatWireMessages centralizes normal chat wire history with compact metadata", () => {
  const messages = [
    makeMsg("user", "old", { isPreCompact: true }),
    makeMsg("system", "summary text", {
      bubbleKind: "compact_summary",
      compactionBoundaryId: "s-1",
    }),
    makeMsg("user", "recent question"),
  ];
  const out = buildChatWireMessages({
    messages,
    maxContextLength: 100_000,
    systemPrompt: "system now",
    runtimeContext: "runtime now",
    currentUserContent: "new question",
  });

  assert.deepEqual(out[0], { role: "system", content: "system now\n\nruntime now" });
  assert.equal(out[1].metadata?.pre_compact, true);
  assert.equal(out[2].metadata?.compact_summary, true);
  assert.equal(out[2].metadata?.summary_id, "s-1");
  assert.deepEqual(out.at(-1), { role: "user", content: "new question" });
});

test("buildChatWireMessages drops UI-only compact fields except explicit metadata", () => {
  const out = buildChatWireMessages({
    messages: [
      makeMsg("user", "old", {
        isPreCompact: true,
        bubbleKind: "default",
        compactionBoundaryId: "ui-only-boundary",
        compactStatus: "completed",
      }),
      makeMsg("assistant", "summary", {
        bubbleKind: "compact_summary",
        compactionBoundaryId: "summary-1",
      }),
    ],
    maxContextLength: 100_000,
    currentUserContent: "next",
  });

  const serialized = JSON.stringify(out);
  assert.match(serialized, /"pre_compact":true/);
  assert.match(serialized, /"compact_summary":true/);
  assert.match(serialized, /"summary_id":"summary-1"/);
  assert.doesNotMatch(serialized, /isPreCompact|bubbleKind|compactionBoundaryId|compactStatus|ui-only-boundary/);
});

test("buildChatWireMessages keeps omission text but drops UI historical image summaries", () => {
  const placeholder = "[historical images omitted: count=2; sources=manual; relative_range=unavailable]";
  const out = buildChatWireMessages({
    messages: [makeMsg("user", [{ type: "text", text: placeholder }] as any, {
      historicalImageSummaries: [{
        count: 2,
        sources: ["manual"],
        placeholder,
      }],
    })],
    maxContextLength: 100_000,
    currentUserContent: "next",
  });

  assert.match(JSON.stringify(out), /historical images omitted/);
  assert.doesNotMatch(JSON.stringify(out), /historicalImageSummaries/);
});

test("buildAgentContextMessages carries visible prior chat turns without chat system prompt", () => {
  const out = buildAgentContextMessages({
    messages: [
      makeMsg("user", "嘿，你是谁"),
      makeMsg("assistant", "你好！我是运行在 NVIDIA Jetson Thor 平台上的 AI 助手。"),
    ],
    maxContextLength: 100_000,
    currentUserContent: "搜索：黄仁勋 最近去了哪个大学演讲",
  });

  assert.deepEqual(out, [
    { role: "user", content: "嘿，你是谁" },
    { role: "assistant", content: "你好！我是运行在 NVIDIA Jetson Thor 平台上的 AI 助手。" },
    { role: "user", content: "搜索：黄仁勋 最近去了哪个大学演讲" },
  ]);
});

test("automation Chat wire history preserves every canonical prior message", () => {
  const messages = Array.from({ length: 24 }, (_, index) => (
    makeMsg(index % 2 === 0 ? "user" : "assistant", `turn-${index}`)
  ));
  const out = buildChatWireMessages({
    messages,
    maxContextLength: 10,
    systemPrompt: "system",
    currentUserContent: "current",
    preserveFullHistory: true,
  });

  assert.equal(out.length, 26);
  assert.equal(out[1].content, "turn-0");
  assert.equal(out[24].content, "turn-23");
  assert.equal(out[25].content, "current");
});

test("automation Agent context preserves every canonical prior message", () => {
  const messages = Array.from({ length: 24 }, (_, index) => (
    makeMsg(index % 2 === 0 ? "user" : "assistant", `turn-${index}`)
  ));
  const out = buildAgentContextMessages({
    messages,
    maxContextLength: 10,
    currentUserContent: "current",
    preserveFullHistory: true,
  });

  assert.equal(out.length, 25);
  assert.equal(out[0].content, "turn-0");
  assert.equal(out[23].content, "turn-23");
  assert.equal(out[24].content, "current");
});

test("normal wire builders retain the existing last-20 behavior", () => {
  const messages = Array.from({ length: 24 }, (_, index) => (
    makeMsg(index % 2 === 0 ? "user" : "assistant", `turn-${index}`)
  ));

  const chat = buildChatWireMessages({
    messages,
    maxContextLength: 100_000,
    currentUserContent: "current",
  });
  const agent = buildAgentContextMessages({
    messages,
    maxContextLength: 100_000,
    currentUserContent: "current",
  });

  assert.equal(chat[0].content, "turn-4");
  assert.equal(agent[0].content, "turn-4");
});

test("decideChatSettingsPersist skips the initial post-hydration default-settings save", () => {
  let skippedInitialPersist = false;

  let decision = decideChatSettingsPersist(false, skippedInitialPersist);
  assert.equal(decision.persist, false);
  assert.equal(decision.skippedInitialPersist, false);

  decision = decideChatSettingsPersist(true, skippedInitialPersist);
  assert.equal(
    decision.persist,
    false,
    "the first hydrated save effect still sees DEFAULT_SETTINGS and must not overwrite saved custom prompt"
  );
  assert.equal(decision.skippedInitialPersist, true);

  skippedInitialPersist = decision.skippedInitialPersist;
  decision = decideChatSettingsPersist(true, skippedInitialPersist);
  assert.equal(
    decision.persist,
    true,
    "the next settings render contains restored/user settings and should be persisted"
  );
  assert.equal(decision.skippedInitialPersist, true);
});
