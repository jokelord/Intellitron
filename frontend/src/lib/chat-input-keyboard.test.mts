import assert from "node:assert/strict";
import { test } from "node:test";

import {
  getChatInputPlaceholder,
  shouldSubmitChatInput,
} from "./chat-input-keyboard.ts";

test("legacy send mode keeps Enter for newlines and Ctrl/Cmd+Enter for submit", () => {
  assert.equal(shouldSubmitChatInput({ key: "Enter" }, false), false);
  assert.equal(shouldSubmitChatInput({ key: "Enter", shiftKey: true }, false), false);
  assert.equal(shouldSubmitChatInput({ key: "Enter", ctrlKey: true }, false), true);
  assert.equal(shouldSubmitChatInput({ key: "Enter", metaKey: true }, false), true);
});

test("Enter-to-send mode submits plain Enter while Shift+Enter keeps a newline", () => {
  assert.equal(shouldSubmitChatInput({ key: "Enter" }, true), true);
  assert.equal(shouldSubmitChatInput({ key: "Enter", shiftKey: true }, true), false);
  assert.equal(shouldSubmitChatInput({ key: "Enter", ctrlKey: true }, true), true);
});

test("keyboard submit never fires for IME composition or non-Enter keys", () => {
  assert.equal(shouldSubmitChatInput({ key: "Enter", isComposing: true, ctrlKey: true }, false), false);
  assert.equal(shouldSubmitChatInput({ key: "Enter", isComposing: true }, true), false);
  assert.equal(shouldSubmitChatInput({ key: "a", ctrlKey: true }, false), false);
});

test("input placeholder describes the active send mode", () => {
  assert.equal(getChatInputPlaceholder(false), "输入您的问题... (Enter 换行, Ctrl+Enter 发送)");
  assert.equal(getChatInputPlaceholder(true), "输入您的问题... (Enter 发送, Shift+Enter 换行)");
});
