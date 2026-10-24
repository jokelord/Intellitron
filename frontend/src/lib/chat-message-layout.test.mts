import { test } from "node:test";
import assert from "node:assert/strict";

import { getChatMessageBubbleWidthClass } from "./chat-message-layout.ts";

test("assistant bubbles extend to the user avatar left edge", () => {
  assert.match(getChatMessageBubbleWidthClass("assistant"), /\bmax-w-\[calc\(100%-3\.75rem\)\]/);
});

test("user bubbles keep their existing compact width", () => {
  assert.match(getChatMessageBubbleWidthClass("user"), /\bmax-w-\[80%\]/);
});

test("message bubbles can shrink around fixed-width content", () => {
  assert.match(getChatMessageBubbleWidthClass("assistant"), /\bmin-w-0\b/);
  assert.match(getChatMessageBubbleWidthClass("user"), /\bmin-w-0\b/);
});
