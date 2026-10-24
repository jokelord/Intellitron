import test from 'node:test';
import assert from 'node:assert/strict';

const originalFetch = globalThis.fetch;
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;

function createSSEStream(events) {
  let index = 0;
  const encoder = new TextEncoder();

  return new ReadableStream({
    pull(controller) {
      if (index >= events.length) {
        controller.close();
        return;
      }

      const event = events[index++];
      if (typeof event === 'string') {
        controller.enqueue(encoder.encode(event));
        return;
      }

      if (event && typeof event === 'object' && event.pending) {
        return;
      }

      controller.close();
    },
  });
}

function sseResponse(events) {
  return new Response(createSSEStream(events), {
    status: 200, headers: { 'Content-Type': 'text/event-stream' },
  });
}

function installFakeTimers() {
  let nextId = 1;
  const timers = new Map();

  globalThis.setTimeout = (fn, delay) => {
    const id = nextId++;
    timers.set(id, { fn, delay });
    return id;
  };

  globalThis.clearTimeout = (id) => {
    timers.delete(id);
  };

  return {
    get delays() {
      return Array.from(timers.values()).map((timer) => timer.delay);
    },
    fireLatest() {
      const id = Math.max(...timers.keys());
      const timer = timers.get(id);
      if (!timer) {
        throw new Error('No timer scheduled');
      }
      timers.delete(id);
      timer.fn();
    },
    restore() {
      globalThis.setTimeout = originalSetTimeout;
      globalThis.clearTimeout = originalClearTimeout;
    },
  };
}

test.afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.setTimeout = originalSetTimeout;
  globalThis.clearTimeout = originalClearTimeout;
});

test('agentChatStream stops after logical done event', async () => {
  globalThis.fetch = async () => sseResponse([
      'data: {"event_sequence":1,"type":"session_start","session_id":"sid-1"}\n\n',
      'data: {"event_sequence":2,"type":"content","content":"partial"}\n\n',
      'data: {"event_sequence":3,"type":"done","session_id":"sid-1"}\n\n',
      { pending: true },
    ]);

  const { llmClient } = await import('../src/lib/api.ts');

  const contentChunks = [];
  const completions = [];

  await llmClient.agentChatStream(
    { message: 'hello' },
    () => {},
    (content) => contentChunks.push(content),
    (sessionId) => completions.push(sessionId),
    (error) => {
      throw error;
    },
  );

  assert.deepEqual(contentChunks, ['partial']);
  assert.deepEqual(completions, ['sid-1']);
});

test('agentChatStream surfaces backend error content payload', async () => {
  globalThis.fetch = async () => sseResponse([
      'data: {"event_sequence":1,"type":"error","content":"backend exploded"}\n\n',
      'data: {"event_sequence":2,"type":"done","session_id":"sid-2"}\n\n',
    ]);

  const { llmClient } = await import('../src/lib/api.ts');

  const seenErrors = [];

  await llmClient.agentChatStream(
    { message: 'hello' },
    () => {},
    () => {},
    () => {},
    (error) => seenErrors.push(error.message),
  );

  assert.deepEqual(seenErrors, ['backend exploded']);
});

test('agentChatStream resets idle timeout on progress chunks', async () => {
  const fakeTimers = installFakeTimers();

  globalThis.fetch = async (_url, options) => {
    assert.equal(options.signal.aborted, false);

    return sseResponse([
        'data: {"event_sequence":1,"type":"session_start","session_id":"sid-3"}\n\n',
        'data: {"event_sequence":2,"type":"content","content":"a"}\n\n',
        'data: {"event_sequence":3,"type":"content","content":"b"}\n\n',
        'data: {"event_sequence":4,"type":"done","session_id":"sid-3"}\n\n',
      ]);
  };

  const { llmClient } = await import('../src/lib/api.ts');
  llmClient.setStreamIdleTimeouts({ agent: 42 });

  await llmClient.agentChatStream(
    { message: 'hello' },
    () => {},
    () => {
      const delays = fakeTimers.delays;
      assert.equal(delays.at(-1), 42000);
    },
    () => {},
    (error) => {
      throw error;
    },
  );

  fakeTimers.restore();
});

test('chatStream stops after [DONE] and ignores trailing chunks', async () => {
  globalThis.fetch = async () => sseResponse([
      'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
      'data: [DONE]\n\n',
      'data: {"choices":[{"delta":{"content":"ignored"}}]}\n\n',
    ]);

  const { llmClient } = await import('../src/lib/api.ts');

  const contentChunks = [];

  await llmClient.chatStream(
    [{ role: 'user', content: 'hello' }],
    {},
    (content) => contentChunks.push(content),
    () => {},
    (error) => {
      throw error;
    },
  );

  assert.deepEqual(contentChunks, ['hello']);
});

test('startCycle stops after [DONE] and ignores trailing events', async () => {
  globalThis.fetch = async () => sseResponse([
      'data: {"type":"cycle_started","cycle_run_id":"cycle-1","session_id":"cycle-1","event_sequence":1}\n\n',
      'data: [DONE]\n\n',
      'data: {"type":"cycle_error","error":"ignored","event_sequence":2}\n\n',
    ]);

  const { llmClient } = await import('../src/lib/api.ts');

  const events = [];

  await new Promise((resolve, reject) => {
    llmClient.startCycle(
      { cycle_run_id: 'cycle-1', steps: [], agent_mode: false, share_context: true },
      (event) => events.push(event.type),
      resolve,
      reject,
    ).catch(reject);
  });

  assert.deepEqual(events, ['cycle_started']);
});
