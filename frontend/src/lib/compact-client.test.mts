import { test } from "node:test";
import assert from "node:assert/strict";
import { parseCompactSSEEvents } from "./compact-client.ts";

test("parseCompactSSEEvents extracts compact_started/delta/completed", () => {
  const raw = [
    'data: {"kind":"compact_started","summary_message_id":"c1","metadata":{}}\n\n',
    'data: {"kind":"compact_delta","summary_message_id":"c1","text":"Hello","partial":true}\n\n',
    'data: {"kind":"compact_completed","summary_message_id":"c1","text":"Summary: Hello","partial":false,"metadata":{}}\n\n',
    'data: [DONE]\n\n',
  ].join("");
  const events = parseCompactSSEEvents(raw);
  assert.equal(events.length, 3);
  assert.equal(events[0].kind, "compact_started");
  assert.equal(events[1].text, "Hello");
  assert.equal(events[2].text, "Summary: Hello");
});

test("parseCompactSSEEvents skips malformed lines", () => {
  const raw =
    'data: {not valid json\n\ndata: {"kind":"compact_failed","summary_message_id":"x","metadata":{"reason":"x"}}\n\n';
  const events = parseCompactSSEEvents(raw);
  assert.equal(events.length, 1);
  assert.equal(events[0].kind, "compact_failed");
});

test("parseCompactSSEEvents handles empty input", () => {
  assert.deepEqual(parseCompactSSEEvents(""), []);
});
