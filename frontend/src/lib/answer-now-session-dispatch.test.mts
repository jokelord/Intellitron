import assert from 'node:assert/strict';
import test from 'node:test';

import { LLMClient } from './api.ts';

test('Answer Now sequenced Hub records use the shared session handler exactly once', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(stream) {
        for (const payload of [
          { event_sequence: 3, type: 'request_accepted', request_id: 'answer-now-request-0001' },
          { event_sequence: 4, type: 'session_start', session_id: 'session-1' },
          { event_sequence: 5, type: 'content', content: 'replacement' },
          { event_sequence: 5, type: 'content', content: 'duplicate' },
          { event_sequence: 6, type: 'done', session_id: 'session-1' },
        ]) stream.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
        stream.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const calls: string[] = [];
  await new LLMClient('/api/backend').answerNowStream(
    'session-1',
    'answer-now-request-0001',
    () => undefined,
    undefined,
    (error) => calls.push(`error:${error.message}`),
    undefined,
    undefined,
    undefined,
    {
      onSessionStart: (sessionId) => calls.push(`start:${sessionId}`),
      onContent: (content) => calls.push(`content:${content}`),
      onDone: (sessionId) => calls.push(`done:${sessionId}`),
    },
  );

  assert.deepEqual(calls, ['start:session-1', 'content:replacement', 'done:session-1']);
});

test('Answer Now keeps an errored old generation projected until the accepted replacement reaches its terminal done', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(stream) {
        for (const payload of [
          { event_sequence: 11, type: 'error', error: 'old generation failed' },
          { event_sequence: 12, type: 'done', session_id: 'session-1' },
          { event_sequence: 13, type: 'request_accepted', request_id: 'answer-now-request-0001' },
          { event_sequence: 14, type: 'session_start', session_id: 'session-1' },
          { event_sequence: 15, type: 'content', content: 'replacement answer' },
          { event_sequence: 16, type: 'done', session_id: 'session-1' },
        ]) stream.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
        stream.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const calls: string[] = [];
  await new LLMClient('/api/backend').answerNowStream(
    'session-1',
    'answer-now-request-0001',
    () => undefined,
    (sessionId) => calls.push(`legacy-done:${sessionId}`),
    (error) => calls.push(`legacy-error:${error.message}`),
    undefined,
    undefined,
    undefined,
    {
      onPayloadError: (error) => calls.push(`shared-error:${error.message}`),
      onDone: (sessionId) => calls.push(`shared-done:${sessionId}`),
      onRequestAccepted: (event) => calls.push(`accepted:${event.requestId}`),
      onSessionStart: (sessionId) => calls.push(`shared-start:${sessionId}`),
      onContent: (content) => calls.push(`shared-content:${content}`),
    },
  );

  assert.deepEqual(calls, [
    'legacy-error:old generation failed',
    'shared-error:old generation failed',
    'accepted:answer-now-request-0001',
    'shared-start:session-1',
    'shared-content:replacement answer',
    'shared-done:session-1',
    'legacy-done:session-1',
  ]);
});

test('Answer Now nonterminal EOF immediately detaches its local subscriber without deleting the retained run', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(stream) {
        stream.enqueue(encoder.encode('data: {"event_sequence":21,"type":"request_accepted","request_id":"answer-now-request-0001"}\n\n'));
        stream.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const calls: string[] = [];
  await new LLMClient('/api/backend').answerNowStream(
    'session-1',
    'answer-now-request-0001',
    () => undefined,
    () => calls.push('legacy-done'),
    () => calls.push('legacy-error'),
    undefined,
    undefined,
    {
      onTransportEof: () => calls.push('transport-eof'),
      onDetached: () => calls.push('detached'),
    },
  );

  assert.deepEqual(calls, ['transport-eof', 'detached']);
});
