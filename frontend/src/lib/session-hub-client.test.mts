import assert from 'node:assert/strict';
import test from 'node:test';

import {
  SessionAttachmentRegistry,
  readSessionHubStream,
  type SessionHubPayload,
} from './session-hub-client.ts';

function responseFor(payloads: Array<SessionHubPayload | string>): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const payload of payloads) {
        const data = typeof payload === 'string' ? payload : JSON.stringify(payload);
        controller.enqueue(encoder.encode(`data: ${data}\n\n`));
      }
      controller.close();
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

test('reader dispatches a positive contiguous Hub stream and returns its stable terminal', async () => {
  const sequences: number[] = [];
  const result = await readSessionHubStream(responseFor([
    { event_sequence: 1, type: 'request_accepted', request_id: 'req-1', session_id: 'session-1', status: 'accepted' },
    { event_sequence: 2, type: 'timeline_event', event: { kind: 'turn_started' } },
    { event_sequence: 3, type: 'done', request_id: 'req-1', session_id: 'session-1', status: 'completed' },
  ]), {
    afterSequence: 0,
    expectedRequestId: 'req-1',
    requestKind: 'Chat',
    handlers: { onPayload: (payload) => { sequences.push(payload.event_sequence); } },
  });

  assert.deepEqual(sequences, [1, 2, 3]);
  assert.deepEqual(result, { kind: 'terminal', lastSequence: 3 });
});

test('reader treats comment heartbeats as transport activity without dispatching payloads', async () => {
  const encoder = new TextEncoder();
  const response = new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(': ping\n\n'));
      controller.enqueue(encoder.encode(
        'data: {"event_sequence":1,"type":"done","request_id":"req-1","session_id":"session-1"}\n\n',
      ));
      controller.close();
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  const activity: string[] = [];
  const payloads: number[] = [];

  const result = await readSessionHubStream(response, {
    expectedRequestId: 'req-1',
    requestKind: 'Agent',
    handlers: {
      onPayload: (payload) => { payloads.push(payload.event_sequence); },
    },
    onActivity: () => { activity.push('chunk'); },
  });

  assert.deepEqual(activity, ['chunk', 'chunk']);
  assert.deepEqual(payloads, [1]);
  assert.deepEqual(result, { kind: 'terminal', lastSequence: 1 });
});

test('reader suppresses replay duplicates at or below the cursor', async () => {
  const sequences: number[] = [];
  const result = await readSessionHubStream(responseFor([
    { event_sequence: 2, type: 'timeline_event', event: { kind: 'message_delta', text: 'old' } },
    { event_sequence: 3, type: 'timeline_event', event: { kind: 'message_delta', text: 'new' } },
    { event_sequence: 3, type: 'timeline_event', event: { kind: 'message_delta', text: 'duplicate' } },
    { event_sequence: 4, type: 'done', session_id: 'session-1' },
  ]), {
    afterSequence: 2,
    requestKind: 'Agent',
    handlers: { onPayload: (payload) => { sequences.push(payload.event_sequence); } },
  });

  assert.deepEqual(sequences, [3, 4]);
  assert.deepEqual(result, { kind: 'terminal', lastSequence: 4 });
});

test('reader reports an exact sequence gap as subscriber detachment', async () => {
  const result = await readSessionHubStream(responseFor([
    { event_sequence: 1, type: 'timeline_event', event: { kind: 'turn_started' } },
    { event_sequence: 3, type: 'done', session_id: 'session-1' },
  ]), {
    afterSequence: 0,
    requestKind: 'Chat',
    handlers: { onPayload: () => undefined },
  });

  assert.equal(result.kind, 'detached');
  assert.equal(result.lastSequence, 1);
  assert.equal(result.error?.message, 'Session Hub event sequence gap: expected 2, received 3');
});

test('handler failure keeps the cursor at the last applied event so retry replays the failed sequence', async () => {
  const terminal = {
    event_sequence: 1,
    type: 'done',
    request_id: 'req-1',
    session_id: 'session-1',
    status: 'completed',
  } as const;
  const first = await readSessionHubStream(responseFor([terminal]), {
    afterSequence: 0,
    expectedRequestId: 'req-1',
    requestKind: 'Chat',
    handlers: {
      onPayload: () => { throw new Error('projection failed'); },
    },
  });

  assert.equal(first.kind, 'detached');
  assert.equal(first.lastSequence, 0);
  assert.equal(first.error?.message, 'projection failed');

  const applied: number[] = [];
  const retried = await readSessionHubStream(responseFor([terminal]), {
    afterSequence: first.lastSequence,
    expectedRequestId: 'req-1',
    requestKind: 'Chat',
    handlers: {
      onPayload: (payload) => { applied.push(payload.event_sequence); },
    },
  });

  assert.deepEqual(applied, [1]);
  assert.deepEqual(retried, { kind: 'terminal', lastSequence: 1 });
});

test('reader delivers known pre-accept rejection without reading SSE or inferring EOF', async () => {
  const rejections: string[] = [];
  const payloads: SessionHubPayload[] = [];
  const result = await readSessionHubStream(new Response(
    JSON.stringify({ detail: 'Session busy: active producer' }),
    { status: 409, headers: { 'Content-Type': 'application/json' } },
  ), {
    afterSequence: 0,
    requestKind: 'Chat',
    handlers: {
      onPayload: (payload) => { payloads.push(payload); },
      onKnownRejection: (rejection) => {
        rejections.push(`${rejection.status}:${rejection.detail}:${rejection.retryable}`);
      },
    },
  });

  assert.deepEqual(payloads, []);
  assert.deepEqual(rejections, ['409:Session busy: active producer:true']);
  assert.equal(result.kind, 'rejected');
  if (result.kind === 'rejected') assert.equal(result.rejection.status, 409);
});

test('reader preserves structured Agent tool configuration rejection codes', async () => {
  const response = new Response(JSON.stringify({
    detail: { code: 'tool_catalog_changed', message: 'stale catalog', session_id: 'A' },
  }), { status: 409, headers: { 'Content-Type': 'application/json' } });
  let code: string | undefined;
  const result = await readSessionHubStream(response, {
    requestKind: 'Agent',
    handlers: { onKnownRejection: (rejection) => { code = rejection.code; } },
  });
  assert.equal(result.kind, 'rejected');
  assert.equal(code, 'tool_catalog_changed');
});

test('successful EOF without done is detached rather than terminal', async () => {
  const result = await readSessionHubStream(responseFor([
    { event_sequence: 1, type: 'timeline_event', event: { kind: 'message_delta', text: 'partial' } },
  ]), {
    afterSequence: 0,
    requestKind: 'Agent',
    handlers: { onPayload: () => undefined },
  });

  assert.deepEqual(result, { kind: 'detached', lastSequence: 1 });
});

test('reader replays prior request generations but accepts and terminalizes only the expected request', async () => {
  const accepted: string[] = [];
  const payloads: number[] = [];
  const result = await readSessionHubStream(responseFor([
    { event_sequence: 1, type: 'request_accepted', request_id: 'req-old', session_id: 'session-1' },
    { event_sequence: 2, type: 'timeline_event', event: { kind: 'turn_started', turn_index: 1 } },
    { event_sequence: 3, type: 'done', request_id: 'req-old', session_id: 'session-1' },
    { event_sequence: 4, type: 'request_accepted', request_id: 'req-new', session_id: 'session-1' },
    { event_sequence: 5, type: 'timeline_event', event: { kind: 'turn_started', turn_index: 2 } },
    { event_sequence: 6, type: 'done', request_id: 'req-new', session_id: 'session-1' },
  ]), {
    afterSequence: 0,
    expectedRequestId: 'req-new',
    requestKind: 'Agent',
    handlers: {
      onPayload: (payload) => { payloads.push(payload.event_sequence); },
      onRequestAccepted: (payload) => { accepted.push(String(payload.request_id)); },
    },
  });

  assert.deepEqual(payloads, [1, 2, 3, 4, 5, 6]);
  assert.deepEqual(accepted, ['req-new']);
  assert.deepEqual(result, { kind: 'terminal', lastSequence: 6 });
});

test('reader bounds one browser task to 32 dispatched payloads without losing stream order', async () => {
  const encoder = new TextEncoder();
  const payloads: SessionHubPayload[] = Array.from({ length: 69 }, (_, index) => ({
    event_sequence: index + 1,
    type: 'agent_step',
    step_type: 'thought',
    content: `chunk-${index + 1}`,
    partial: true,
    request_id: 'req-burst',
  }));
  payloads.push({
    event_sequence: 70,
    type: 'done',
    request_id: 'req-burst',
    session_id: 'session-burst',
    status: 'completed',
  });
  const response = new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(
        payloads.map((payload) => `data: ${JSON.stringify(payload)}\n\n`).join(''),
      ));
      controller.close();
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });

  let browserTask = 0;
  const observed: Array<{ sequence: number; browserTask: number }> = [];
  const result = await readSessionHubStream(response, {
    afterSequence: 0,
    expectedRequestId: 'req-burst',
    requestKind: 'Agent',
    handlers: {
      onPayload: (payload) => {
        if (payload.event_sequence === 1) setTimeout(() => { browserTask = 1; }, 0);
        if (payload.event_sequence === 33) setTimeout(() => { browserTask = 2; }, 0);
        observed.push({ sequence: payload.event_sequence, browserTask });
      },
    },
  });

  assert.deepEqual(observed.map(({ sequence }) => sequence), Array.from({ length: 70 }, (_, index) => index + 1));
  assert.deepEqual(observed.slice(0, 32).map(({ browserTask: task }) => task), Array(32).fill(0));
  assert.deepEqual(observed.slice(32, 64).map(({ browserTask: task }) => task), Array(32).fill(1));
  assert.deepEqual(observed.slice(64).map(({ browserTask: task }) => task), Array(6).fill(2));
  assert.deepEqual(result, { kind: 'terminal', lastSequence: 70 });
});

test('attachment registry permanently rejects delayed callbacks from superseded tokens', () => {
  const registry = new SessionAttachmentRegistry();
  const original = registry.attach('session-1');
  const replacement = registry.attach('session-1');

  assert.equal(registry.isCurrent(original), false);
  assert.equal(registry.detach(original), false);
  assert.equal(registry.isCurrent(replacement), true);
  assert.equal(registry.detach(replacement), true);
  assert.equal(registry.isCurrent(replacement), false);
});
