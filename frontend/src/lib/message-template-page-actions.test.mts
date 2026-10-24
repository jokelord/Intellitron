import assert from "node:assert/strict";
import path from "node:path";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createRequire } from 'node:module';

import {
  insertMessageTemplateIntoInput,
  sendMessageTemplateThroughExistingPipeline,
} from "./message-template-page-actions.ts";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const { createRenderer, installTask6TypeScriptHook } = require('./vision-automation/react-component-renderer.cjs');
installTask6TypeScriptHook(fileURLToPath(new URL('../../', import.meta.url)));
const React = require('react');
const { useConversationState } = require('./conversation/use-conversation-state.ts');
const { useConversationCommands } = require('./conversation/use-conversation-commands.ts');

test("template insertion replaces the input exactly and focuses it after the state update", () => {
  const events: string[] = [];
  let input = "existing draft";

  insertMessageTemplateIntoInput(
    " leading text\nsecond line ",
    (value) => {
      input = value;
      events.push(`input:${value}`);
    },
    () => events.push("focus"),
    (callback) => {
      events.push("scheduled");
      callback();
    },
  );

  assert.equal(input, " leading text\nsecond line ");
  assert.deepEqual(events, [
    "input: leading text\nsecond line ",
    "scheduled",
    "focus",
  ]);
});

test("immediate template selection invokes the existing send pipeline exactly once", async () => {
  const sent: string[] = [];
  const expectedResult = Promise.resolve("accepted");

  const result = sendMessageTemplateThroughExistingPipeline(
    "exact template\ntext",
    (text) => {
      sent.push(text);
      return expectedResult;
    },
  );

  assert.equal(result, expectedResult);
  assert.equal(await result, "accepted");
  assert.deepEqual(sent, ["exact template\ntext"]);
});

test("text and vision pages own and persist the controlled immediate-send state", async (t) => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window');
  const saved = new Map<string, string>();
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: { getItem: (key: string) => saved.get(key) ?? null, setItem: (key: string, value: string) => saved.set(key, value) } } });
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'window', previous); else delete (globalThis as any).window; });
  for (const [relative, pageMode] of [["../app/page.tsx", 'text'], ["../app/vision/page.tsx", 'vision']] as const) {
    const source = await readFile(path.join(testDir, relative), "utf8");
    assert.match(source, /useConversationState\(\)/);
    assert.match(source, /useConversationCommands\(\{\s*state:\s*conversationState/);
    assert.match(source, /immediateSend=\{messageTemplateImmediateSend\}/);
    assert.match(source, /onImmediateSendChange=\{handleMessageTemplateImmediateSendChange\}/);
    let current: any;
    const Probe = () => {
      const state = useConversationState();
      const actions = useConversationCommands({state, pageMode, busy: false, cycleStatus: 'idle', send() {}, voiceController: {isVoiceMode: false}, buildRuntimeTimeContext: () => ''});
      current = { state, actions }; return null;
    };
    const renderer = createRenderer(React.createElement(Probe));
    try {
      assert.equal(current.state.messageTemplateImmediateSend, false);
      renderer.act(() => current.actions.handleMessageTemplateImmediateSendChange(true));
      assert.equal(current.state.messageTemplateImmediateSend, true);
      assert.equal([...saved.values()].at(-1), 'true');
      renderer.act(() => current.actions.handleMessageTemplateImmediateSendChange(false));
      assert.equal([...saved.values()].at(-1), 'false');
    } finally { renderer.unmount(); }
  }
});

test("vision page owns immediate-send state outside the keyed ImageUploader subtree", async () => {
  const source = await readFile(path.join(testDir, "../app/vision/page.tsx"), "utf8");
  const stateIndex = source.indexOf("useConversationState()");
  const uploaderIndex = source.indexOf("<ImageUploader");
  assert.notEqual(stateIndex, -1);
  assert.notEqual(uploaderIndex, -1);
  assert.ok(stateIndex < uploaderIndex);
});
