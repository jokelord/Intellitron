/**
 * Contract: /compact wire body must be built from runtime context
 * (chat vs. agent), NOT hardcoded to mode='chat'.
 *
 * Bug history (2026-06-06):
 *   Vision-page /agent-mode user reported that /compact produced a
 *   summary in the UI but the next /v1/agent/chat request still
 *   saw the full ~40k context. Root cause: both page.tsx and
 *   vision/page.tsx hardcoded
 *
 *       mode: 'chat'
 *
 *   in the request body. The backend then routed the compact to
 *   the CHAT compact orchestrator, which generated a summary in
 *   the chat context only — `session.agent.memory.steps` (the
 *   source of truth for the next agent run) was never replaced.
 *
 *   Backend fix: /v1/compact/run now branches on mode='agent' to
 *   delegate to AgentRunner.run_compact_for_session, which actually
 *   replaces memory.steps with the envelope step.
 *
 *   Frontend fix: extracted `buildCompactRequestBody` as the single
 *   source of truth, used by BOTH pages. It picks `mode` from
 *   `agentMode`.
 *
 * These tests pin the contract so the regression cannot come back.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildCompactRequestBody } from "./compact-client.ts";

function msg(role, extras = {}) {
  return {
    id: `m-${role}-${Math.random()}`,
    timestamp: new Date(),
    role,
    content: `hello from ${role}`,
    ...extras,
  };
}

test("regression: agent mode MUST produce mode='agent' in /compact body", () => {
  const body = buildCompactRequestBody({
    agentMode: true,
    sessionId: "agent-xyz",
    messages: [msg("user"), msg("assistant")],
  });
  assert.equal(
    body.mode,
    "agent",
    "agent mode must route to AgentRunner.run_compact_for_session " +
      "so session.agent.memory.steps is actually replaced; mode='chat' " +
      "would silently leave the agent memory untouched and the next " +
      "/v1/agent/chat would still see the full 40k context."
  );
  assert.equal(body.session_id, "agent-xyz");
});

test("regression: chat mode MUST produce mode='chat' in /compact body", () => {
  const body = buildCompactRequestBody({
    agentMode: false,
    sessionId: null,
    messages: [msg("user")],
  });
  assert.equal(body.mode, "chat");
  assert.equal(body.session_id, null);
});

test("regression: missing session_id collapses to null in chat mode", () => {
  const body = buildCompactRequestBody({
    agentMode: false,
    sessionId: undefined,
    messages: [msg("user")],
  });
  assert.equal(body.session_id, null);
});

test("buildCompactRequestBody translates isPreCompact into wire metadata", () => {
  const body = buildCompactRequestBody({
    agentMode: false,
    messages: [
      msg("user", { isPreCompact: true }),
      msg("assistant", { isPreCompact: true }),
    ],
  });
  for (const m of body.messages) {
    assert.deepEqual(m.metadata, { pre_compact: true });
  }
});

test("buildCompactRequestBody translates compact summary bubbles into wire metadata", () => {
  const body = buildCompactRequestBody({
    agentMode: false,
    messages: [
      msg("assistant", {
        bubbleKind: "compact_summary",
        compactionBoundaryId: "compact-1",
      }),
    ],
  });
  assert.deepEqual(body.messages[0].metadata, {
    compact_summary: true,
    summary_id: "compact-1",
  });
});

test("buildCompactRequestBody forwards source metadata unchanged", () => {
  const body = buildCompactRequestBody({
    agentMode: true,
    sessionId: "agent-1",
    messages: [msg("user", { metadata: { trace_id: "abc" } })],
  });
  assert.deepEqual(body.messages[0].metadata, { trace_id: "abc" });
});

test("buildCompactRequestBody always sets custom_instructions to null (or arg)", () => {
  const a = buildCompactRequestBody({ agentMode: true, messages: [] });
  assert.equal(a.custom_instructions, null);
  const b = buildCompactRequestBody({
    agentMode: true,
    messages: [],
    customInstructions: "focus on code",
  });
  assert.equal(b.custom_instructions, "focus on code");
});
