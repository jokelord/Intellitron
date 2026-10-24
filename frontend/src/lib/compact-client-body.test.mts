/**
 * Contract: /compact wire body must include a SINGLE system message at
 * index 0 (never two consecutive system messages) whenever the runtime
 * context has one.
 *
 * Bug history (2026-06-11):
 *   The v2 compact prompt builder (`backend/compact/prompt_builder.py:98-99`)
 *   raises ValueError when `body_messages[0].role !== "system"`. The
 *   orchestrator translates this to a `compact_failed("build_error")`
 *   timeline event (1-second fail-fast). The frontend's
 *   `buildCompactRequestBody` was passing through the user's
 *   `messages: Message[]` list verbatim — which never has a system
 *   message at index 0, because `truncateMessagesForContext` strips
 *   it before passing it to the page. So every manual `/compact`
 *   invocation triggered the fail-fast path and the UI stalled 5+
 *   minutes waiting for a non-existent success.
 *
 *   The fix: `buildCompactRequestBody` now takes optional
 *   `systemPrompt` and `runtimeContext` arguments and MERGES them
 *   into a single system message at index 0, mirroring the pattern
 *   in `handleSend` (page.tsx:438-454, vision/page.tsx:431-447).
 *
 *   Both text and vision pages pass `settings.systemPrompt` and the
 *   runtime-time-context string (if enabled) through the helper, so
 *   the v2 builder gets the same system prompt the next chat call
 *   would.
 *
 *   We MUST merge into a single system message (not two), because
 *   SGLang rejects requests with multiple consecutive system messages
 *   ("System message must be at the beginning"). The backend's
 *   `_inject_runtime_context` (`backend/llm_service.py:80-101`) does
 *   the same merge for non-compact chat paths. The v2 compact builder
 *   does NOT inject runtime_context (per spec §6.1 I3), so the
 *   frontend must produce the merged form.
 *
 * These tests pin the contract so the regression cannot come back.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildCompactRequestBody } from "./compact-client.ts";

function msg(role, content, extras = {}) {
  return {
    id: `m-${role}-${Math.random()}`,
    timestamp: new Date(),
    role,
    content,
    ...extras,
  };
}

test("systemPrompt is prepended as the first message in the wire body", () => {
  const body = buildCompactRequestBody({
    agentMode: false,
    systemPrompt: "You are X",
    messages: [msg("user", "Q1")],
  });
  assert.equal(body.messages.length, 2, "system + 1 user message");
  assert.equal(body.messages[0].role, "system");
  assert.equal(body.messages[0].content, "You are X");
  assert.equal(body.messages[1].role, "user");
  assert.equal(body.messages[1].content, "Q1");
});

test("systemPrompt + runtimeContext are MERGED into a single system message (not 2 separate)", () => {
  const body = buildCompactRequestBody({
    agentMode: false,
    systemPrompt: "You are X",
    runtimeContext: "Current time: 2026-06-11",
    messages: [msg("user", "Q1")],
  });
  // CRITICAL: exactly ONE system message at index 0, then the user
  // message. SGLang rejects 2+ consecutive system messages.
  assert.equal(body.messages.length, 2);
  assert.equal(body.messages[0].role, "system");
  assert.equal(
    body.messages[0].content,
    "You are X\n\nCurrent time: 2026-06-11"
  );
  assert.equal(body.messages[1].role, "user");
  assert.equal(body.messages[1].content, "Q1");
  // Count system messages — must be exactly 1.
  const systemCount = body.messages.filter((m) => m.role === "system").length;
  assert.equal(
    systemCount,
    1,
    "must have exactly one system message (merged, not two separate)"
  );
});

test("empty/null systemPrompt is treated as absent — no prepending", () => {
  for (const empty of [null, undefined, ""]) {
    const body = buildCompactRequestBody({
      agentMode: false,
      systemPrompt: empty,
      messages: [msg("user", "Q1")],
    });
    assert.equal(
      body.messages.length,
      1,
      `systemPrompt=${JSON.stringify(empty)} should not prepend`
    );
    assert.equal(body.messages[0].role, "user");
    assert.equal(body.messages[0].content, "Q1");
  }
});

test("empty/null runtimeContext is treated as absent — falls through to systemPrompt only", () => {
  for (const empty of [null, undefined, ""]) {
    const body = buildCompactRequestBody({
      agentMode: false,
      systemPrompt: "You are X",
      runtimeContext: empty,
      messages: [msg("user", "Q1")],
    });
    assert.equal(
      body.messages.length,
      2,
      `runtimeContext=${JSON.stringify(empty)} should not add a 2nd system message`
    );
    assert.equal(body.messages[0].role, "system");
    assert.equal(body.messages[0].content, "You are X");
    assert.equal(body.messages[1].role, "user");
  }
});

test("systemPrompt + runtimeContext + customInstructions: system merged, custom_instructions stays out of messages", () => {
  const body = buildCompactRequestBody({
    agentMode: true,
    systemPrompt: "You are X",
    runtimeContext: "Current time: 2026-06-11",
    messages: [msg("user", "Q1")],
    customInstructions: "focus on code",
  });
  // Single merged system + 1 user = 2 total, not 3.
  assert.equal(body.messages.length, 2);
  assert.equal(body.messages[0].role, "system");
  assert.equal(
    body.messages[0].content,
    "You are X\n\nCurrent time: 2026-06-11"
  );
  assert.equal(body.messages[1].role, "user");
  assert.equal(body.messages[1].content, "Q1");
  // custom_instructions stays in the body field, not in the messages list.
  assert.equal(body.custom_instructions, "focus on code");
  for (const m of body.messages) {
    assert.notEqual(
      m.content,
      "focus on code",
      "custom_instructions must not leak into the messages list"
    );
  }
});

test("merges systemPrompt and runtimeContext in order: systemPrompt first, then runtimeContext, separated by \\n\\n", () => {
  // Pin the exact merge order — must mirror handleSend's push order.
  const body = buildCompactRequestBody({
    agentMode: false,
    systemPrompt: "FIRST",
    runtimeContext: "SECOND",
    messages: [msg("user", "Q")],
  });
  assert.equal(body.messages.length, 2);
  assert.equal(body.messages[0].role, "system");
  assert.equal(body.messages[0].content, "FIRST\n\nSECOND");
});

test("merges systemPrompt and runtimeContext in order: corner cases", () => {
  // (system, runtime) → content
  const cases: Array<{
    system: string | null;
    runtime: string | null;
    expected: string | null; // null → no system message at all
  }> = [
    { system: "X", runtime: "Y", expected: "X\n\nY" },
    { system: "", runtime: "Y", expected: "Y" },
    { system: "X", runtime: "", expected: "X" },
    { system: "X\n", runtime: "Y", expected: "X\n\nY" },
    { system: "X", runtime: "\nY", expected: "X\n\nY" },
    { system: "  X  ", runtime: "  Y  ", expected: "X\n\nY" },
    { system: "", runtime: "", expected: null },
    { system: "X", runtime: null, expected: "X" },
    { system: null, runtime: "Y", expected: "Y" },
    { system: null, runtime: null, expected: null },
  ];
  for (const c of cases) {
    const body = buildCompactRequestBody({
      agentMode: false,
      systemPrompt: c.system,
      runtimeContext: c.runtime,
      messages: [msg("user", "Q")],
    });
    if (c.expected === null) {
      assert.equal(
        body.messages[0].role,
        "user",
        `system=${JSON.stringify(c.system)}, runtime=${JSON.stringify(
          c.runtime
        )}: expected no system message, got ${JSON.stringify(
          body.messages[0]
        )}`
      );
      assert.equal(body.messages.length, 1);
    } else {
      assert.equal(
        body.messages[0].role,
        "system",
        `system=${JSON.stringify(c.system)}, runtime=${JSON.stringify(
          c.runtime
        )}: expected system, got ${JSON.stringify(body.messages[0])}`
      );
      assert.equal(
        body.messages[0].content,
        c.expected,
        `system=${JSON.stringify(c.system)}, runtime=${JSON.stringify(
          c.runtime
        )}: expected content=${JSON.stringify(c.expected)}, got ${JSON.stringify(
          body.messages[0].content
        )}`
      );
      assert.equal(body.messages.length, 2);
    }
  }
});

test("whitespace-only systemPrompt treated as absent", () => {
  // systemPrompt is whitespace-only → falls through to runtimeContext.
  const body = buildCompactRequestBody({
    agentMode: false,
    systemPrompt: "   ",
    runtimeContext: "Current time: 2026-06-11",
    messages: [msg("user", "Q1")],
  });
  assert.equal(body.messages.length, 2);
  assert.equal(body.messages[0].role, "system");
  assert.equal(body.messages[0].content, "Current time: 2026-06-11");
  assert.equal(body.messages[1].role, "user");
  assert.equal(body.messages[1].content, "Q1");
});

test("whitespace-only systemPrompt + absent runtimeContext → no system message", () => {
  const body = buildCompactRequestBody({
    agentMode: false,
    systemPrompt: "   ",
    messages: [msg("user", "Q1")],
  });
  assert.equal(body.messages.length, 1);
  assert.equal(body.messages[0].role, "user");
  assert.equal(body.messages[0].content, "Q1");
});

test("whitespace-only runtimeContext treated as absent", () => {
  // runtimeContext is whitespace-only → no merge separator, just the
  // systemPrompt alone.
  const body = buildCompactRequestBody({
    agentMode: false,
    systemPrompt: "You are X",
    runtimeContext: "   ",
    messages: [msg("user", "Q1")],
  });
  assert.equal(body.messages.length, 2);
  assert.equal(body.messages[0].role, "system");
  assert.equal(body.messages[0].content, "You are X");
  // No trailing \n\n — runtimeContext was treated as absent.
  assert.equal(
    (body.messages[0].content as string).endsWith("\n\n"),
    false,
    "whitespace-only runtimeContext must not add a trailing \\n\\n"
  );
  assert.equal(body.messages[1].role, "user");
});

test("agent mode: mode='agent' + session_id + systemPrompt all flow through", () => {
  const body = buildCompactRequestBody({
    agentMode: true,
    sessionId: "agent-xyz",
    systemPrompt: "You are X",
    messages: [msg("user", "Q1")],
  });
  assert.equal(body.mode, "agent");
  assert.equal(body.session_id, "agent-xyz");
  assert.equal(body.messages.length, 2);
  assert.equal(body.messages[0].role, "system");
  assert.equal(body.messages[0].content, "You are X");
  assert.equal(body.messages[1].role, "user");
  assert.equal(body.messages[1].content, "Q1");
});

test("metadata on input messages is preserved in the wire body", () => {
  const body = buildCompactRequestBody({
    agentMode: false,
    systemPrompt: "You are X",
    messages: [
      msg("user", "Q1", { metadata: { pre_compact: true } }),
      msg("assistant", "A1", { metadata: { trace_id: "t-1" } }),
    ],
  });
  // 1 system + 2 user/assistant
  assert.equal(body.messages.length, 3);
  // System message has no metadata.
  assert.equal(body.messages[0].metadata, undefined);
  // User/assistant metadata is forwarded unchanged.
  assert.deepEqual(body.messages[1].metadata, { pre_compact: true });
  assert.deepEqual(body.messages[2].metadata, { trace_id: "t-1" });
});

test("merged system content preserves internal whitespace (no full normalization)", () => {
  // Whitespace inside the systemPrompt is preserved; we only trim() to
  // detect "absent", we don't strip internal whitespace.
  const raw = "You are X\n  with leading/trailing whitespace  \nand\ttabs";
  const body = buildCompactRequestBody({
    agentMode: false,
    systemPrompt: raw,
    messages: [msg("user", "Q1")],
  });
  assert.equal(body.messages[0].content, raw);
});

test("multiple user messages are preserved in input order after the merged system", () => {
  const body = buildCompactRequestBody({
    agentMode: false,
    systemPrompt: "You are X",
    runtimeContext: "Current time: 2026-06-11",
    messages: [
      msg("user", "U1"),
      msg("assistant", "A1"),
      msg("user", "U2"),
    ],
  });
  // 1 merged system + 3 user/assistant = 4 total.
  assert.equal(body.messages.length, 4);
  assert.equal(body.messages[0].role, "system");
  assert.equal(
    body.messages[0].content,
    "You are X\n\nCurrent time: 2026-06-11"
  );
  assert.equal(body.messages[1].role, "user");
  assert.equal(body.messages[1].content, "U1");
  assert.equal(body.messages[2].role, "assistant");
  assert.equal(body.messages[2].content, "A1");
  assert.equal(body.messages[3].role, "user");
  assert.equal(body.messages[3].content, "U2");
});

test("only one system message ever appears in the wire body (no 2 consecutive systems)", () => {
  // Sweep the parameter space to guarantee the "no 2 consecutive
  // system messages" invariant — SGLang rejects this.
  const sweep: Array<{
    system: string | null;
    runtime: string | null;
  }> = [
    { system: "X", runtime: "Y" },
    { system: "X", runtime: null },
    { system: null, runtime: "Y" },
    { system: null, runtime: null },
    { system: "", runtime: "" },
    { system: "  ", runtime: "  " },
    { system: "X", runtime: "  " },
    { system: "  ", runtime: "Y" },
  ];
  for (const c of sweep) {
    const body = buildCompactRequestBody({
      agentMode: false,
      systemPrompt: c.system,
      runtimeContext: c.runtime,
      messages: [msg("user", "U1"), msg("assistant", "A1")],
    });
    const systemCount = body.messages.filter((m) => m.role === "system").length;
    assert.ok(
      systemCount <= 1,
      `system=${JSON.stringify(c.system)}, runtime=${JSON.stringify(
        c.runtime
      )}: produced ${systemCount} system messages (SGLang rejects >1)`
    );
  }
});
