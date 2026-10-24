import { test } from "node:test";
import assert from "node:assert/strict";
import { runCompactSSE } from "./compact-client.ts";

function makeFakeStream(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(c);
      controller.close();
    },
  });
}

test("regression: runCompactSSE parses a single data: event and calls onEvent", async () => {
  const body = makeFakeStream([
    new TextEncoder().encode(
      'data: {"kind":"compact_started","summary_message_id":"a","text":null,"partial":false,"metadata":{"trigger":"manual","mode":"chat"}}\n\n'
    ),
    new TextEncoder().encode("data: [DONE]\n\n"),
  ]);
  const orig = globalThis.fetch;
  (globalThis as { fetch: typeof fetch }).fetch = (async () =>
    new Response(body, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    })) as typeof fetch;
  try {
    const events: unknown[] = [];
    await runCompactSSE({
      url: "/v1/compact/run",
      body: { mode: "chat" },
      onEvent: (ev) => events.push(ev),
    });
    assert.equal(events.length, 1);
    assert.equal((events[0] as { kind: string }).kind, "compact_started");
    assert.equal(
      (events[0] as { summary_message_id: string }).summary_message_id,
      "a"
    );
    assert.deepEqual(
      (events[0] as { metadata: unknown }).metadata,
      { trigger: "manual", mode: "chat" }
    );
  } finally {
    (globalThis as { fetch: typeof fetch }).fetch = orig;
  }
});

test("regression: runCompactSSE streams multiple data: events as they arrive", async () => {
  const body = makeFakeStream([
    new TextEncoder().encode('data: {"kind":"compact_started","summary_message_id":"a","metadata":{}}\n\n'),
    new TextEncoder().encode('data: {"kind":"compact_delta","summary_message_id":"a","text":"hello","partial":true}\n\n'),
    new TextEncoder().encode('data: {"kind":"compact_delta","summary_message_id":"a","text":" world","partial":true}\n\n'),
    new TextEncoder().encode('data: {"kind":"compact_completed","summary_message_id":"a","text":"hello world","partial":false,"metadata":{"trigger":"manual"}}\n\n'),
    new TextEncoder().encode("data: [DONE]\n\n"),
  ]);
  const orig = globalThis.fetch;
  (globalThis as { fetch: typeof fetch }).fetch = (async () =>
    new Response(body, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    })) as typeof fetch;
  try {
    const events: { kind: string; text?: string }[] = [];
    await runCompactSSE({
      url: "/v1/compact/run",
      body: { mode: "chat" },
      onEvent: (ev) => events.push(ev as { kind: string; text?: string }),
    });
    assert.equal(events.length, 4);
    assert.equal(events[0].kind, "compact_started");
    assert.equal(events[1].kind, "compact_delta");
    assert.equal(events[1].text, "hello");
    assert.equal(events[2].text, " world");
    assert.equal(events[3].kind, "compact_completed");
    assert.equal(events[3].text, "hello world");
  } finally {
    (globalThis as { fetch: typeof fetch }).fetch = orig;
  }
});

test("regression: runCompactSSE handles events split across chunk boundaries", async () => {
  const body = makeFakeStream([
    new TextEncoder().encode('data: {"kind":"compact_star'),
    new TextEncoder().encode('ted","summary_message_id":"x","metadata":{}}\n\n'),
    new TextEncoder().encode('data: [DONE]\n\n'),
  ]);
  const orig = globalThis.fetch;
  (globalThis as { fetch: typeof fetch }).fetch = (async () =>
    new Response(body, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    })) as typeof fetch;
  try {
    const events: unknown[] = [];
    await runCompactSSE({
      url: "/v1/compact/run",
      body: { mode: "chat" },
      onEvent: (ev) => events.push(ev),
    });
    assert.equal(events.length, 1);
    assert.equal((events[0] as { kind: string }).kind, "compact_started");
  } finally {
    (globalThis as { fetch: typeof fetch }).fetch = orig;
  }
});

test("regression: runCompactSSE throws on non-2xx status", async () => {
  const orig = globalThis.fetch;
  (globalThis as { fetch: typeof fetch }).fetch = (async () =>
    new Response("internal error", { status: 500 })) as typeof fetch;
  try {
    await assert.rejects(
      runCompactSSE({
        url: "/v1/compact/run",
        body: { mode: "chat" },
        onEvent: () => {},
      }),
      /status=500/
    );
  } finally {
    (globalThis as { fetch: typeof fetch }).fetch = orig;
  }
});
