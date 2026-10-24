import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DICTATION_MAGIC_SEND_STORAGE_KEY,
  clearAcceptedDictationMagicSendSource,
  clearDictationMagicSendSourceIfUnchanged,
  createDictationMagicSendPollScheduler,
  createDictationMagicSendPollController,
  findDictationMagicSendMatch,
  loadDictationMagicSendSettings,
  readDictationMagicSendSettings,
  writeDictationMagicSendSettings,
} from "./dictation-magic-send.ts";

test("cache precedence is per field and invalid values fall back to YAML", () => {
  const defaults = { commands: ["Over, over. "], pollIntervalMs: 500 };
  const rulesOnly = loadDictationMagicSendSettings(
    JSON.stringify({ version: 1, rules: [{ text: "Case Exact ", enabled: true }], pollIntervalMs: "bad" }),
    defaults,
  );
  assert.deepEqual(rulesOnly.rules, [{ text: "Case Exact ", enabled: true }]);
  assert.equal(rulesOnly.pollIntervalMs, 500);

  const intervalOnly = loadDictationMagicSendSettings(
    JSON.stringify({ version: 1, rules: "bad", pollIntervalMs: 20 }),
    defaults,
  );
  assert.deepEqual(intervalOnly.rules, [{ text: "Over, over. ", enabled: true }]);
  assert.equal(intervalOnly.pollIntervalMs, 500);
  assert.equal(DICTATION_MAGIC_SEND_STORAGE_KEY, "llm-asr-dictation-magic-send");
});

test("versioned cache rejects unsupported payloads, isolates invalid fields, and tolerates storage failures", () => {
  const defaults = { commands: ["YAML command"], pollIntervalMs: 500 };
  assert.deepEqual(
    loadDictationMagicSendSettings(JSON.stringify({ rules: [], pollIntervalMs: 100 }), defaults),
    { rules: [{ text: "YAML command", enabled: true }], pollIntervalMs: 500 },
  );
  assert.deepEqual(
    loadDictationMagicSendSettings(JSON.stringify({ version: 1, rules: [], pollIntervalMs: 250 }), defaults),
    { rules: [], pollIntervalMs: 250 },
  );

  const writes: string[] = [];
  const storage = {
    getItem: () => JSON.stringify({ version: 1, rules: [], pollIntervalMs: Number.NaN }),
    setItem: (_key: string, value: string) => { writes.push(value); },
  };
  assert.deepEqual(readDictationMagicSendSettings(storage, defaults), {
    rules: [], pollIntervalMs: 500,
  });
  writeDictationMagicSendSettings(storage, { rules: [], pollIntervalMs: 100 });
  assert.deepEqual(JSON.parse(writes[0]), { version: 1, rules: [], pollIntervalMs: 100 });
  assert.doesNotThrow(() => writeDictationMagicSendSettings({
    getItem: () => null,
    setItem: () => { throw new Error("blocked"); },
  }, { rules: [], pollIntervalMs: 100 }));
  assert.deepEqual(readDictationMagicSendSettings({ getItem: () => { throw new Error("blocked"); }, setItem: () => {} }, defaults), {
    rules: [{ text: "YAML command", enabled: true }], pollIntervalMs: 500,
  });
});

test("matching is exact, case-sensitive, enabled-only, and chooses the longest suffix", () => {
  const rules = [
    { text: "over.", enabled: true },
    { text: "Over, over.", enabled: true },
    { text: "Over, over. ", enabled: true },
    { text: "Disabled", enabled: false },
  ];
  assert.deepEqual(findDictationMagicSendMatch("hello Over, over. ", rules), {
    text: "Over, over. ", enabled: true,
  });
  assert.equal(findDictationMagicSendMatch("hello over, over", rules), undefined);
  assert.equal(findDictationMagicSendMatch("hello Disabled", rules), undefined);
});

test("polling strips the exact command, exposes its original source, and suppresses whitespace-only sends", async () => {
  const sent: Array<[string, string]> = [];
  const controller = createDictationMagicSendPollController();
  const result = await controller.poll({
    mode: "dictation",
    input: "  body Over, over. ",
    rules: [{ text: "Over, over. ", enabled: true }],
    busy: false,
    send: async (text, source) => { sent.push([text, source]); },
    clear: () => { sent.push(["clear", ""]); },
  });
  assert.equal(result, true);
  assert.deepEqual(sent, [["body", "  body Over, over. "]]);

  await controller.poll({
    mode: "dictation", input: "Over, over.", rules: [{ text: "Over, over.", enabled: true }], busy: false,
    send: async () => { sent.push(["unexpected", ""]); }, clear: () => { sent.push(["clear", ""]); },
  });
  assert.deepEqual(sent, [["body", "  body Over, over. "], ["clear", ""]]);
});

test("source-aware consumption prevents a completed request from re-sending and preserves later edits", async () => {
  const controller = createDictationMagicSendPollController();
  let input = "body Go";
  let sends = 0;
  const options = {
    mode: "dictation" as const,
    rules: [{ text: "Go", enabled: true }],
    busy: false,
    clear: () => { input = ""; },
    send: async (_body: string, source: string) => {
      sends += 1;
      input = clearDictationMagicSendSourceIfUnchanged(input, source);
    },
  };
  await controller.poll({ ...options, input });
  assert.equal(input, "");
  assert.equal(await controller.poll({ ...options, input }), false);
  input = "body Go plus typing";
  assert.equal(clearDictationMagicSendSourceIfUnchanged(input, "body Go"), "body Go plus typing");
  assert.equal(sends, 1);
});

test("automation consumption clears only an accepted unchanged trigger source", () => {
  assert.equal(clearAcceptedDictationMagicSendSource(true, "body Go", "body Go"), "");
  assert.equal(clearAcceptedDictationMagicSendSource(false, "body Go", "body Go"), "body Go");
  assert.equal(clearAcceptedDictationMagicSendSource(true, "body Go later edit", "body Go"), "body Go later edit");
});

test("polling only activates while idle dictation and prevents an in-flight duplicate", async () => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let sends = 0;
  const controller = createDictationMagicSendPollController();
  const options = {
    input: "body Go", rules: [{ text: "Go", enabled: true }], clear: () => {},
    send: async () => { sends += 1; await pending; },
  };
  assert.equal(await controller.poll({ ...options, mode: "keyboard", busy: false }), false);
  assert.equal(await controller.poll({ ...options, mode: "dictation", busy: true }), false);
  const first = controller.poll({ ...options, mode: "dictation", busy: false });
  assert.equal(await controller.poll({ ...options, mode: "dictation", busy: false }), false);
  release();
  assert.equal(await first, true);
  assert.equal(sends, 1);
});

test("poll scheduler waits one interval, rebuilds on interval change, and stops on mode exit", () => {
  const scheduled: Array<{ callback: () => void; delay: number; id: number }> = [];
  const cancelled: number[] = [];
  const polls: number[] = [];
  const scheduler = createDictationMagicSendPollScheduler({
    schedule: (callback, delay) => { const id = scheduled.length + 1; scheduled.push({ callback, delay, id }); return id; },
    cancel: (id) => { cancelled.push(id as number); },
    poll: () => { polls.push(Date.now()); },
  });
  scheduler.start(500);
  assert.deepEqual(polls, [], "mount must not poll synchronously");
  scheduled[0].callback();
  assert.equal(polls.length, 1);
  scheduler.start(250);
  assert.deepEqual(cancelled, [1]);
  assert.equal(scheduled[1].delay, 250);
  scheduler.stop();
  assert.deepEqual(cancelled, [1, 2]);
});
