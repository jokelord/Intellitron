import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CHAT_INPUT_MAX_ROWS,
  CHAT_INPUT_MIN_HEIGHT_PX,
  CHAT_INPUT_MAX_HEIGHT_PX,
  resizeChatInputTextarea,
} from "./chat-input-layout.ts";

test("chat input grows up to six rows", () => {
  assert.equal(CHAT_INPUT_MAX_ROWS, 6);
  assert.equal(CHAT_INPUT_MAX_HEIGHT_PX, 168);
});

test("resizeChatInputTextarea enables internal scrolling after max height", () => {
  const element = {
    scrollHeight: CHAT_INPUT_MAX_HEIGHT_PX + 40,
    style: { height: "", overflowY: "hidden" },
  };

  resizeChatInputTextarea(element);

  assert.equal(element.style.height, `${CHAT_INPUT_MAX_HEIGHT_PX}px`);
  assert.equal(element.style.overflowY, "auto");
});

test("resizeChatInputTextarea keeps short input non-scrollable", () => {
  const element = {
    scrollHeight: CHAT_INPUT_MIN_HEIGHT_PX,
    style: { height: "", overflowY: "auto" },
  };

  resizeChatInputTextarea(element);

  assert.equal(element.style.height, `${CHAT_INPUT_MIN_HEIGHT_PX}px`);
  assert.equal(element.style.overflowY, "hidden");
});

test("resizeChatInputTextarea can use configured input bar heights", () => {
  const element = {
    scrollHeight: 56,
    style: { height: "", overflowY: "auto" },
  };

  resizeChatInputTextarea(element, { minHeightPx: 56, maxHeightPx: 200 });

  assert.equal(element.style.height, "56px");
  assert.equal(element.style.overflowY, "hidden");
});
