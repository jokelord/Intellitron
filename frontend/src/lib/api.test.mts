import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { CycleRecoveryError, dispatchAgentSessionPayload, getApiBaseUrl, LLMClient, llmClient, mergeServerDefaultsWithCache, parseCachedChatSettings } from './api.ts';
import {
  applyChildTranscriptEvent,
  childAgentStreamEventToStep,
  mergeChildTranscriptStep,
} from './child-agent-transcript.ts';
import { MediaSourceRegistry } from './vision-automation/media-source-registry.ts';
import type { AgentToolConfigurationCandidate } from './agent-tool-configuration.ts';
import {
  appendManualImages,
  getImageUploaderActionState,
} from './vision-automation/ui-contracts.ts';

const require = createRequire(import.meta.url);
const testDir = path.dirname(fileURLToPath(import.meta.url));

test('agentChatStream sends the captured tool configuration candidate explicitly', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const candidate: AgentToolConfigurationCandidate = {
    schema_version: 1,
    catalog_revision: 'catalog-a',
    manager_agent_tools: ['read_file'],
    basic_agent_tools: ['read_file', 'write_file'],
  };
  let body: Record<string, unknown> | null = null;
  globalThis.fetch = async (_input, init) => {
    body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    const encoder = new TextEncoder();
    const chunks = [
      `data: ${JSON.stringify({ event_sequence: 1, type: 'request_accepted', request_id: 'request-tool-1', session_id: 'session-tool-1', tool_configuration: { ...candidate, locked: true, fingerprint: 'fingerprint-a' } })}\n\n`,
      `data: ${JSON.stringify({ event_sequence: 2, type: 'done', session_id: 'session-tool-1' })}\n\n`,
    ];
    return new Response(new ReadableStream({ start(controller) { chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk))); controller.close(); } }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };
  await llmClient.agentChatStream({ message: 'hello', request_id: 'request-tool-1', tool_configuration: candidate }, () => {}, () => {});
  assert.deepEqual(body?.tool_configuration, candidate);
});

test('getApiBaseUrl preserves explicitly configured non-loopback backend URLs', () => {
  const baseUrl = getApiBaseUrl({
    configuredBaseUrl: 'http://192.168.2.54:8090',
    location: {
      protocol: 'http:',
      hostname: '192.168.2.54',
    },
  });

  assert.equal(baseUrl, 'http://192.168.2.54:8090');
});

test('getApiBaseUrl uses same-origin proxy for configured loopback backend on remote browsers', () => {
  const baseUrl = getApiBaseUrl({
    configuredBaseUrl: 'http://localhost:8090',
    location: {
      protocol: 'http:',
      hostname: '192.168.2.54',
    },
  });

  assert.equal(baseUrl, '/api/backend');
});

test('getApiBaseUrl defaults to the project backend port for remote browsers', () => {
  const baseUrl = getApiBaseUrl({
    configuredBaseUrl: undefined,
    location: {
      protocol: 'http:',
      hostname: '192.168.2.54',
    },
  });

  assert.equal(baseUrl, '/api/backend');
});

test('Next.js default API base URL matches the backend service port', (t) => {
  const original = process.env.NEXT_PUBLIC_API_BASE_URL;
  t.after(() => {
    if (original === undefined) {
      delete process.env.NEXT_PUBLIC_API_BASE_URL;
    } else {
      process.env.NEXT_PUBLIC_API_BASE_URL = original;
    }
    delete require.cache[require.resolve('../../next.config.js')];
  });

  delete process.env.NEXT_PUBLIC_API_BASE_URL;
  delete require.cache[require.resolve('../../next.config.js')];

  const nextConfig = require('../../next.config.js');

  assert.equal(nextConfig.env.NEXT_PUBLIC_API_BASE_URL, 'http://localhost:8090');
});

test('Next.js allowedDevOrigins is configured from environment for LAN development', (t) => {
  const original = process.env.NEXT_ALLOWED_DEV_ORIGINS;
  t.after(() => {
    if (original === undefined) {
      delete process.env.NEXT_ALLOWED_DEV_ORIGINS;
    } else {
      process.env.NEXT_ALLOWED_DEV_ORIGINS = original;
    }
    delete require.cache[require.resolve('../../next.config.js')];
  });

  process.env.NEXT_ALLOWED_DEV_ORIGINS = '192.168.2.54, thor.local';
  delete require.cache[require.resolve('../../next.config.js')];

  const nextConfig = require('../../next.config.js');

  assert.deepEqual(nextConfig.allowedDevOrigins, ['192.168.2.54', 'thor.local']);
});

test('agentChatStream preserves dispatch metadata from agent_step SSE payloads', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    const chunks = [
      `data: ${JSON.stringify({
        event_sequence: 1,
        type: 'agent_step',
        step_id: 'dispatch-step-1',
        step_type: 'dispatch',
        dispatch_status: 'completed',
        dispatch_stage: 'direct_answer',
        dispatch_kind: 'routing',
        display_slot: 'before_body',
        dispatch_id: 'dispatch-run-1',
        dispatch_message: '已完成响应路径判定',
      })}\n\n`,
      `data: ${JSON.stringify({ event_sequence: 2, type: 'done', session_id: 'session-1' })}\n\n`,
    ];

    return new Response(new ReadableStream({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  const receivedSteps: any[] = [];
  await llmClient.agentChatStream(
    { message: 'hello' },
    (step) => receivedSteps.push(step),
    () => {},
  );

  assert.equal(receivedSteps.length, 1);
  assert.deepEqual(receivedSteps[0], {
    id: 'dispatch-step-1',
    step_type: 'dispatch',
    content: undefined,
    name: undefined,
    input: undefined,
    partial: undefined,
    raw_tool_call: undefined,
    dispatch_status: 'completed',
    dispatch_stage: 'direct_answer',
    dispatch_kind: 'routing',
    dispatch_target_agent: undefined,
    dispatch_message: '已完成响应路径判定',
    dispatch_id: 'dispatch-run-1',
    display_slot: 'before_body',
  });
});

test('agentChatStream reads orchestration_done total_tools summary count', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    const chunks = [
      `data: ${JSON.stringify({
        event_sequence: 1,
        type: 'orchestration_done',
        step_id: 'orchestration-1',
        total_tools: 2,
        completed: 1,
        failed: 1,
        skipped: 0,
        timeout: 0,
        duration_ms: 40,
      })}\n\n`,
      `data: ${JSON.stringify({ event_sequence: 2, type: 'done', session_id: 'session-1' })}\n\n`,
    ];

    return new Response(new ReadableStream({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  const receivedSteps: any[] = [];
  await llmClient.agentChatStream(
    { message: 'run two tools' },
    (step) => receivedSteps.push(step),
    () => {},
  );

  assert.equal(receivedSteps[0].orchestration_summary.total, 2);
  assert.equal(receivedSteps[0].orchestration_summary.failed, 1);
});

test('childAgentStream reads child replay and live SSE events', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  const seenUrls: string[] = [];

  globalThis.fetch = async (url) => {
    seenUrls.push(String(url));
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'manager_assigned_task', task: 'Read source A.' })}\n\n`));
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'content', content: 'done' })}\n\n`));
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'done', session_id: 'child-session' })}\n\n`));
        controller.close();
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  const events: any[] = [];
  await client.childAgentStream('child-1', (event) => events.push(event));

  assert.equal(seenUrls[0], '/api/backend/v1/agent/child/stream?child_run_id=child-1');
  assert.deepEqual(events.map((event) => event.type), ['manager_assigned_task', 'content', 'done']);
});

test('agentSessionEvents requests the Hub cursor and rejects sequence gaps', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const urls: string[] = [];
  globalThis.fetch = async (url) => {
    urls.push(String(url));
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ event_sequence: 4, type: 'content', content: 'one' })}\n\n`));
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ event_sequence: 5, type: 'done', session_id: 'session-1' })}\n\n`));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const received: any[] = [];
  await client.agentSessionEvents('session-1', 3, { onEvent: (event) => received.push(event) });
  assert.equal(urls[0], '/api/backend/v1/session/session-1/events?after_sequence=3');
  assert.deepEqual(received.map((event) => event.event_sequence), [4, 5]);
});

test('agentChatStream forwards every sequenced live Hub payload to the shared consumer', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        for (const payload of [
          { event_sequence: 7, type: 'request_accepted', request_id: 'request-1', session_id: 'session-1', status: 'accepted' },
          { event_sequence: 8, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'session-1', turn_index: 1, user_content: 'hello', images: [], mode: 'tool_calling' } },
          { event_sequence: 9, type: 'content', content: 'answer' },
          { event_sequence: 10, type: 'done', session_id: 'session-1' },
        ]) controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const received: any[] = [];
  await client.agentChatStream(
    { session_id: 'session-1', message: 'hello', request_id: 'request-1' },
    () => undefined,
    () => undefined,
    () => undefined,
    () => undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    (event) => received.push(event),
  );
  assert.deepEqual(received.map((event) => event.event_sequence), [7, 8, 9, 10]);
});

test('agentChatStream validates sequenced live Hub records without a raw observer', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        for (const payload of [
          { event_sequence: 7, type: 'session_start', session_id: 'session-1' },
          { event_sequence: 8, type: 'content', content: 'A' },
          { event_sequence: 8, type: 'content', content: 'duplicate A' },
          { event_sequence: 10, type: 'content', content: 'gap C' },
        ]) controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const content: string[] = [];
  const errors: string[] = [];
  await client.agentChatStream(
    { session_id: 'session-1', message: 'hello' },
    () => undefined,
    (chunk) => content.push(chunk),
    undefined,
    (error) => errors.push(error.message),
  );

  assert.deepEqual(content, ['A']);
  assert.deepEqual(errors, ['Session Hub event sequence gap: expected 9, received 10']);
});

test('agentChatStream reports a pre-accept 409 JSON detail as a known rejection without transport recovery', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let fetches = 0;
  globalThis.fetch = async () => {
    fetches += 1;
    return new Response(JSON.stringify({ detail: 'Session busy: active run is still executing' }), {
      status: 409,
      statusText: 'Conflict',
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const calls: string[] = [];
  await client.agentChatStream(
    { session_id: 'session-active', message: 'automation', request_id: 'request-409' },
    () => undefined,
    () => undefined,
    undefined,
    (error) => calls.push(`error:${error.message}`),
    undefined,
    undefined,
    undefined,
    {
      onKnownRejection: (rejection) => calls.push(
        `rejected:${rejection.status}:${rejection.detail}:${rejection.retryable}`,
      ),
      onTransportEof: () => calls.push('transport-eof'),
    },
  );

  assert.deepEqual(calls, [
    'rejected:409:Session busy: active run is still executing:true',
  ]);
  assert.equal(fetches, 1);
});

test('chatStream sends 409, 503, and 422 JSON rejections through the known pre-accept path', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  for (const fixture of [
    { status: 409, detail: 'Session busy', retryable: true },
    { status: 503, detail: 'Temporarily unavailable', retryable: true },
    { status: 422, detail: 'Invalid request', retryable: false },
  ]) {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: fixture.detail }), {
      status: fixture.status,
      headers: { 'Content-Type': 'application/json' },
    });
    const calls: string[] = [];
    await client.chatStream([], { request_id: `chat-${fixture.status}` }, () => undefined, () => undefined,
      (error) => calls.push(`error:${error.message}`), undefined, undefined, {
        onKnownRejection: (rejection) => calls.push(`known:${rejection.status}:${rejection.detail}:${rejection.retryable}`),
        onTransportEof: () => calls.push('transport-eof'),
        onDetached: () => calls.push('detached'),
      });
    assert.deepEqual(calls, [`known:${fixture.status}:${fixture.detail}:${fixture.retryable}`]);
  }
});

test('Hub Chat nonterminal EOF reports detach without invoking fatal content error', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) { controller.close(); },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  const calls: string[] = [];
  await new LLMClient('/api/backend').chatStream(
    [],
    { request_id: 'chat-detach-1', session_id: 'chat-session-1' },
    () => undefined,
    () => calls.push('complete'),
    (error) => calls.push(`fatal:${error.message}`),
    undefined,
    undefined,
    { onDetached: (error) => calls.push(`detached:${error?.message ?? 'eof'}`) },
  );
  assert.deepEqual(calls, ['detached:eof']);
});

test('Hub Chat producer error is delivered before done and done remains the stable terminal', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const encoder = new TextEncoder();
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) {
      for (const payload of [
        { event_sequence: 1, type: 'request_accepted', request_id: 'chat-error-1' },
        { event_sequence: 2, type: 'error', error: 'producer failed', request_id: 'chat-error-1' },
        { event_sequence: 3, type: 'done', status: 'error', request_id: 'chat-error-1' },
      ]) controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
      controller.close();
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  const calls: string[] = [];
  await new LLMClient('/api/backend').chatStream(
    [], { request_id: 'chat-error-1', session_id: 'chat-session-error' },
    () => undefined,
    () => calls.push('done:release-busy'),
    (error) => calls.push(`error:display-only:${error.message}`),
    undefined, undefined,
    { onDetached: () => calls.push('detached') },
  );
  assert.deepEqual(calls, ['error:display-only:producer failed', 'done:release-busy']);
});

test('deleteSessionHub calls the canonical generic DELETE endpoint', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const calls: Array<{ url: string; method: string | undefined }> = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), method: init?.method });
    return Response.json({ status: 'deleted' });
  };
  await new LLMClient('/api/backend').deleteSessionHub('chat-or-agent');
  assert.deepEqual(calls, [{
    url: '/api/backend/v1/session/chat-or-agent', method: 'DELETE',
  }]);
});

test('answerNowStream sends 409 and 503 JSON rejections through known rejection without detaching', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  for (const fixture of [
    { status: 409, detail: 'Session busy', retryable: true },
    { status: 503, detail: 'Temporarily unavailable', retryable: true },
  ]) {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: fixture.detail }), {
      status: fixture.status,
      headers: { 'Content-Type': 'application/json' },
    });
    const calls: string[] = [];
    await client.answerNowStream('session-1', `answer-now-${fixture.status}`, () => undefined,
      undefined, (error) => calls.push(`error:${error.message}`), undefined, undefined, {
        onKnownRejection: (rejection) => calls.push(`known:${rejection.status}:${rejection.detail}:${rejection.retryable}`),
        onTransportEof: () => calls.push('transport-eof'),
        onDetached: () => calls.push('detached'),
      });
    assert.deepEqual(calls, [`known:${fixture.status}:${fixture.detail}:${fixture.retryable}`]);
  }
});

test('known rejections preserve FastAPI array and object details deterministically', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const fixtures = [
    { kind: 'chat' as const, detail: [{ msg: 'field required', loc: ['body', 'message'], type: 'missing' }, { type: 'invalid', msg: 'bad' }], expected: '[{"loc":["body","message"],"msg":"field required","type":"missing"},{"msg":"bad","type":"invalid"}]' },
    { kind: 'agent' as const, detail: { code: 'invalid_request', message: 'bad input' }, expected: '{"code":"invalid_request","message":"bad input"}' },
    { kind: 'answer-now' as const, detail: { errors: [{ field: 'session_id', message: 'invalid' }] }, expected: '{"errors":[{"field":"session_id","message":"invalid"}]}' },
  ];
  for (const fixture of fixtures) {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: fixture.detail }), {
      status: 422,
      headers: { 'Content-Type': 'application/json' },
    });
    let observed = '';
    const lifecycle = { onKnownRejection: (rejection: { detail: string }) => { observed = rejection.detail; } };
    if (fixture.kind === 'chat') {
      await client.chatStream([], { request_id: 'structured-chat' }, () => undefined, () => undefined, () => undefined, undefined, undefined, lifecycle);
    } else if (fixture.kind === 'agent') {
      await client.agentChatStream({ message: 'test', request_id: 'structured-agent' }, () => undefined, () => undefined, undefined, () => undefined, undefined, undefined, undefined, lifecycle);
    } else {
      await client.answerNowStream('session-1', 'structured-answer-now', () => undefined, undefined, () => undefined, undefined, undefined, lifecycle);
    }
    assert.equal(observed, fixture.expected);
  }
});

test('agentSessionEvents reports nonterminal EOF as a detachable subscriber', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"event_sequence":1,"type":"content","content":"partial"}\n\n'));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const lifecycle: string[] = [];
  await client.agentSessionEvents('session-1', 0, {
    onContent: () => lifecycle.push('content'),
    onComplete: () => lifecycle.push('complete'),
    onDetached: () => lifecycle.push('detached'),
  });

  assert.deepEqual(lifecycle, ['content', 'detached']);
});

test('agentSessionEvents detaches when a later Hub generation follows an earlier terminal record', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        for (const payload of [
          { event_sequence: 1, type: 'done', session_id: 'session-1' },
          { event_sequence: 2, type: 'session_start', session_id: 'session-1' },
          { event_sequence: 3, type: 'content', content: 'replacement is still running' },
        ]) controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const lifecycle: string[] = [];
  await client.agentSessionEvents('session-1', 0, {
    onComplete: () => lifecycle.push('complete'),
    onDetached: () => lifecycle.push('detached'),
  });
  assert.deepEqual(lifecycle, ['detached']);
});

test('live and recovery adapters dispatch identical ordered payload handlers', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const payloads = [
    { event_sequence: 1, type: 'request_accepted', request_id: 'request-1', session_id: 'session-1', status: 'accepted' },
    { event_sequence: 2, type: 'session_start', session_id: 'session-1' },
    { event_sequence: 3, type: 'agent_step', step_type: 'thought', content: 'reasoning' },
    { event_sequence: 4, type: 'orchestration_plan', step_id: 'plan-1', stages: [] },
    { event_sequence: 5, type: 'orchestration_update', step_id: 'update-1', call_id: 'call-1', status: 'completed' },
    { event_sequence: 6, type: 'orchestration_done', step_id: 'done-1', total_tools: 1, completed: 1, failed: 0, skipped: 0, timeout: 0, duration_ms: 5 },
    { event_sequence: 7, type: 'timeline_event', event: { kind: 'routing_delta' } },
    { event_sequence: 8, type: 'content', content: 'answer' },
    { event_sequence: 9, type: 'done', session_id: 'session-1' },
  ];
  globalThis.fetch = async (_url, init) => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        for (const payload of payloads) controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const record = (log: string[]) => ({
    accepted: (sessionId: string | undefined) => log.push(`accepted:${sessionId}`),
    session: (sessionId: string) => log.push(`session:${sessionId}`),
    step: (step: any) => log.push(`step:${step.step_type}:${step.orchestration_type ?? step.id}`),
    timeline: (event: any) => log.push(`timeline:${event.kind}`),
    content: (content: string) => log.push(`content:${content}`),
    done: (sessionId: string | undefined) => log.push(`done:${sessionId}`),
    error: (error: Error) => log.push(`error:${error.message}`),
  });
  const live: string[] = [];
  const liveHandlers = record(live);
  await client.agentChatStream(
    { session_id: 'session-1', message: 'hello', request_id: 'request-1' },
    liveHandlers.step,
    liveHandlers.content,
    liveHandlers.done,
    liveHandlers.error,
    liveHandlers.session,
    undefined,
    liveHandlers.timeline,
    { onRequestAccepted: (event) => liveHandlers.accepted(event.session_id) },
  );

  const recovery: string[] = [];
  const recoveryHandlers = record(recovery);
  await client.agentSessionEvents('session-1', 0, {
    onRequestAccepted: (event) => recoveryHandlers.accepted(event.session_id),
    onSessionStart: recoveryHandlers.session,
    onStep: recoveryHandlers.step,
    onTimelineEvent: recoveryHandlers.timeline,
    onContent: recoveryHandlers.content,
    onDone: recoveryHandlers.done,
    onPayloadError: recoveryHandlers.error,
  });

  assert.deepEqual(recovery, live);
  assert.deepEqual(live, [
    'accepted:undefined',
    'session:session-1',
    'step:thought:agent_step-3-thought',
    'step:orchestration:plan',
    'step:orchestration:update',
    'step:orchestration:done',
    'timeline:routing_delta',
    'content:answer',
    'done:session-1',
  ]);
});

test('dispatchAgentSessionPayload preserves exact raw textual tool-call debug', async () => {
  const raw = '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{\n  "file_path": "/tmp/a"\n}}\n<<END_TOOL_CALL>>';
  let received: any;

  await dispatchAgentSessionPayload({
    event_sequence: 7,
    type: 'agent_step',
    step_type: 'thought',
    raw_tool_call: raw,
    partial: false,
  }, {
    onStep: (step) => { received = step; },
  });

  assert.equal(received.raw_tool_call, raw);
  assert.equal(received.content, undefined);
  assert.equal(received.step_type, 'thought');
});

test('child transcript preserves and merges raw textual tool-call debug', () => {
  const raw = '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{\n  "file_path": "/tmp/a"\n}}\n<<END_TOOL_CALL>>';
  const initial = [{ id: 'thought-1', step_type: 'thought' as const, content: 'reasoning', partial: true }];
  const rawStep = childAgentStreamEventToStep({
    type: 'agent_step',
    step_type: 'thought',
    raw_tool_call: raw,
    partial: false,
  });

  assert.ok(rawStep);
  const merged = mergeChildTranscriptStep(initial, rawStep);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].content, 'reasoning');
  assert.equal(merged[0].raw_tool_call, raw);
});

test('live and recovery adapters dispatch payload errors through the same handler', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ event_sequence: 1, type: 'error', error: 'boom' })}\n\n`));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };
  const live: string[] = [];
  await client.agentChatStream({ message: 'hello' }, () => undefined, () => undefined, undefined, (error) => live.push(error.message));
  const recovery: string[] = [];
  await client.agentSessionEvents('session-1', 0, {
    onPayloadError: (error) => recovery.push(error.message),
  });
  assert.deepEqual(recovery, live);
  assert.deepEqual(live, ['boom']);
});

test('childAgentStream keeps reading reused child context after per-turn done', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      async start(controller) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'manager_assigned_task', task: 'Turn 1' })}\n\n`));
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'done', final_report: 'first final' })}\n\n`));
        await new Promise((resolve) => setTimeout(resolve, 0));
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'manager_assigned_task', task: 'Turn 2' })}\n\n`));
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'done', final_report: 'second final' })}\n\n`));
        controller.close();
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  const events: any[] = [];
  let completeCount = 0;
  await client.childAgentStream('child-1', (event) => events.push(event), undefined, () => {
    completeCount += 1;
  });

  assert.deepEqual(events.map((event) => event.task ?? event.final_report), [
    'Turn 1',
    'first final',
    'Turn 2',
    'second final',
  ]);
  assert.equal(completeCount, 1);
});

test('childAgentStream forwards abort controller signal to fetch', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  const controller = new AbortController();
  let seenSignal: AbortSignal | null | undefined;

  globalThis.fetch = async (_url, init) => {
    seenSignal = init?.signal;
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(streamController) {
        streamController.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'done' })}\n\n`));
        streamController.close();
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  await client.childAgentStream('child-1', () => {}, undefined, undefined, controller);

  assert.equal(seenSignal, controller.signal);
});

test('abortChildAgentRun posts only the child run ID', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let requestUrl = '';
  let requestInit: RequestInit | undefined;
  globalThis.fetch = async (url, init) => {
    requestUrl = String(url);
    requestInit = init;
    return new Response(JSON.stringify({ status: 'aborted', child_run_id: 'child-1' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const result = await client.abortChildAgentRun('child-1');

  assert.equal(requestUrl, '/api/backend/v1/agent/child/abort');
  assert.equal(requestInit?.method, 'POST');
  assert.deepEqual(JSON.parse(String(requestInit?.body)), { child_run_id: 'child-1' });
  assert.equal(result.status, 'aborted');
});

test('child agent route subscribes to replay-live child stream', async () => {
  const source = await readFile(path.join(testDir, '../app/agent/child/[childRunId]/page.tsx'), 'utf8');

  assert.match(source, /agentSessionEvents/);
  assert.match(source, /,\s*0,\s*\{/);
  assert.match(source, /params\.childRunId/);
  assert.match(source, /applyChildTranscriptEvent/);
  assert.match(source, /transcript\.turns\.map/);
  assert.match(source, /truncationNotice/);
  assert.match(source, /MarkdownRenderer/);
  assert.match(source, /AgentStepDisplay/);
  assert.match(source, /Abort child/);
  assert.match(source, /window\.confirm/);
  assert.match(source, /abortChildAgentRun\(params\.childRunId\)/);
  assert.match(source, /disabled=\{isAborting\}/);
});

test('child transcript keeps every reused child context turn visible', () => {
  const events = [
    { type: 'manager_assigned_task', child_name: 'reader', task: 'Read source A.' },
    { type: 'content', content: 'first ' },
    { type: 'content', content: 'answer' },
    { type: 'done', final_report: 'first final' },
    { type: 'manager_assigned_task', child_name: 'reader', task: 'Continue with source B.' },
    { type: 'content', content: 'second ' },
    { type: 'done', final_report: 'second final' },
  ];

  const state = events.reduce(applyChildTranscriptEvent, {
    childName: 'Child agent',
    turns: [],
    truncationNotice: null,
    status: 'connecting' as const,
    error: null,
  });

  assert.equal(state.childName, 'reader');
  assert.equal(state.turns.length, 2);
  assert.equal(state.turns[0].assignedTask, 'Read source A.');
  assert.equal(state.turns[0].content, 'first final');
  assert.equal(state.turns[0].status, 'completed');
  assert.equal(state.turns[1].assignedTask, 'Continue with source B.');
  assert.equal(state.turns[1].content, 'second final');
  assert.equal(state.turns[1].status, 'completed');
});

test('child transcript exposes fresh and reused runtime diagnostics', () => {
  const state = [
    { type: 'manager_assigned_task', child_name: 'reader', task: 'First', reused_context: false, prior_step_count: 0 },
    { type: 'done' },
    { type: 'manager_assigned_task', child_name: 'reader', task: 'Second', reused_context: true, prior_step_count: 7 },
  ].reduce(applyChildTranscriptEvent, {
    childName: 'Child agent', turns: [], truncationNotice: null,
    status: 'connecting' as const, error: null,
  });

  assert.equal(state.turns[0].reusedContext, false);
  assert.equal(state.turns[0].priorStepCount, 0);
  assert.equal(state.turns[1].reusedContext, true);
  assert.equal(state.turns[1].priorStepCount, 7);
});

test('child transcript timeout preserves timeout status and backwards-compatible errors', () => {
  const timeoutState = applyChildTranscriptEvent(
    applyChildTranscriptEvent({
      childName: 'Child agent', turns: [], truncationNotice: null,
      status: 'connecting' as const, error: null,
    }, {
      type: 'manager_assigned_task', child_name: 'reader', task: 'Timed task', turn_id: 'turn-timeout',
    }),
    { type: 'error', status: 'timeout', error: 'Child agent timed out after 1 seconds' },
  );

  assert.equal(timeoutState.status, 'timeout');
  assert.equal(timeoutState.turns.at(-1)?.status, 'timeout');
  assert.equal(timeoutState.error, 'Child agent timed out after 1 seconds');

  const failedState = applyChildTranscriptEvent(
    {
      childName: 'Child agent', turns: [], truncationNotice: null,
      status: 'connecting' as const, error: null,
    },
    { type: 'error', error: 'Legacy child failure' },
  );
  assert.equal(failedState.status, 'failed');
  assert.equal(failedState.turns.at(-1)?.status, 'failed');
  assert.equal(failedState.error, 'Legacy child failure');
});

test('child transcript preserves the task when truncation notice follows its anchor', () => {
  const state = [
    { type: 'manager_assigned_task', seq: 0, child_name: 'reader', task: 'Current turn task' },
    { type: 'truncated', seq: 0, dropped_events: 8, message: 'Earlier events were truncated.' },
  ].reduce(applyChildTranscriptEvent, {
    childName: 'Child agent', turns: [], truncationNotice: null,
    status: 'connecting' as const, error: null,
  });

  assert.equal(state.turns.length, 1);
  assert.equal(state.turns[0].assignedTask, 'Current turn task');
  assert.equal(state.truncationNotice, 'Earlier events were truncated.');
});

test('child agent route owns a vertical scroll container for standalone tabs', async () => {
  const source = await readFile(path.join(testDir, '../app/agent/child/[childRunId]/page.tsx'), 'utf8');

  assert.match(source, /h-screen/);
  assert.match(source, /overflow-y-auto/);
});

test('shared markdown renderer handles mermaid fenced code blocks', async () => {
  const source = await readFile(path.join(testDir, '../components/MarkdownRenderer.tsx'), 'utf8');

  assert.match(source, /language-mermaid/);
  assert.match(source, /mermaid\.render/);
  assert.match(source, /ReactMarkdown/);
});

test('shared markdown renderer exposes copy controls for fenced code blocks', async () => {
  const source = await readFile(path.join(testDir, '../components/MarkdownRenderer.tsx'), 'utf8');

  assert.match(source, /navigator\.clipboard\.writeText/);
  assert.match(source, /Copy/);
  assert.match(source, /Copied/);
  assert.match(source, /CodeBlock/);
});

test('shared markdown renderer exposes zoom controls for mermaid diagrams only', async () => {
  const source = await readFile(path.join(testDir, '../components/MarkdownRenderer.tsx'), 'utf8');

  assert.match(source, /zoom/i);
  assert.match(source, /Zoom in/);
  assert.match(source, /Zoom out/);
  assert.match(source, /Reset zoom/);
  assert.match(source, /setZoom/);
});

test('shared markdown renderer supports large mermaid zoom and drag movement', async () => {
  const source = await readFile(path.join(testDir, '../components/MarkdownRenderer.tsx'), 'utf8');
  const layout = await readFile(path.join(testDir, 'mermaid-layout.ts'), 'utf8');

  assert.match(source, /--webui-mermaid-min-height/);
  assert.match(source, /getNextMermaidZoom/);
  assert.match(source, /MERMAID_MAX_ZOOM/);
  assert.match(layout, /MERMAID_MAX_ZOOM = 6/);
  assert.match(layout, /Math\.min\(MERMAID_MAX_ZOOM/);
  assert.match(source, /Move/);
  assert.match(source, /setPan/);
  assert.match(source, /onPointerDown/);
  assert.match(source, /onPointerMove/);
  assert.match(source, /setPointerCapture/);
});

test('shared markdown renderer uses icon buttons and an 80 percent mermaid baseline', async () => {
  const source = await readFile(path.join(testDir, '../components/MarkdownRenderer.tsx'), 'utf8');
  const layout = await readFile(path.join(testDir, 'mermaid-layout.ts'), 'utf8');

  assert.match(source, /CopyIcon/);
  assert.match(source, /MoveIcon/);
  assert.match(source, /ZoomIn/);
  assert.match(source, /ZoomOut/);
  assert.match(source, /targetWidth = containerWidth \* 0\.8/);
  assert.match(source, /baseZoom/);
  assert.match(source, /ResizeObserver/);
  assert.match(source, /Move enabled/);
  assert.match(source, /getMermaidMoveButtonClass/);
  assert.match(layout, /MERMAID_MOVE_BUTTON_ON/);
  assert.match(layout, /bg-blue-100/);
});

test('shared markdown renderer normalizes fragile mermaid labels and svg widths', async () => {
  const source = await readFile(path.join(testDir, '../components/MarkdownRenderer.tsx'), 'utf8');

  assert.match(source, /normalizeMermaidChart/);
  assert.match(source, /normalizeMermaidGanttDependencies/);
  assert.match(source, /after b1 b2/);
  assert.match(source, /normalizeMermaidSvg/);
  assert.match(source, /replace\(\/\\swidth=\["'\]\[\^"'\]\*\["'\]\/i/);
  assert.match(source, /width="\$\{width\}px"/);
  assert.match(source, /const renderChart = normalizeMermaidChart\(chart\)/);
});

test('shared markdown renderer opens links in a new tab with opener protection', async () => {
  const source = await readFile(path.join(testDir, '../components/MarkdownRenderer.tsx'), 'utf8');

  assert.match(source, /target="_blank"/);
  assert.match(source, /rel="noopener noreferrer"/);
});

function fakeCameraMediaStream() {
  const track = {
    stopCount: 0,
    stop() { this.stopCount += 1; },
  };
  return {
    active: true,
    track,
    getTracks: () => [track],
  };
}

test('image uploader atomically caps concurrent upload and camera completions at nine', () => {
  const uploaded = appendManualImages([], Array.from({ length: 8 }, (_, index) => `upload-${index}`), 9);
  const firstCamera = appendManualImages(uploaded, ['camera-a'], 9);
  const secondCamera = appendManualImages(firstCamera, ['camera-b'], 9);
  assert.equal(firstCamera.length, 9);
  assert.deepEqual(secondCamera, firstCamera);
});

test('camera owner release stops tracks only after the final lease', async () => {
  const stream = fakeCameraMediaStream();
  const registry = new MediaSourceRegistry({
    getUserMedia: async () => stream as unknown as MediaStream,
  });
  const manual = await registry.acquireCamera({ ownerId: 'manual-photo', deviceId: 'camera-a' });
  const automation = await registry.acquireCamera({ ownerId: 'automation', deviceId: 'camera-a' });
  manual.release();
  assert.equal(stream.track.stopCount, 0);
  automation.release();
  assert.equal(stream.track.stopCount, 1);
});

test('camera action remains closable after a concurrent completion fills the image cap', () => {
  const state = getImageUploaderActionState({
    imageCount: 9,
    configuredMaxImages: 9,
    manualCameraVisible: true,
    automationEnabled: true,
    automationArmed: false,
    automationExpanded: false,
    cycleEnabled: true,
    cycleRunning: false,
  });
  const manual = state.actions.find((action) => action.id === 'manual_photo');
  assert.deepEqual(manual, { id: 'manual_photo', visible: true, disabled: false, mode: 'close' });
});

test('camera discovery deduplicates concurrent enumeration behaviorally', async () => {
  let enumerateCalls = 0;
  let resolveDevices!: (devices: MediaDeviceInfo[]) => void;
  const registry = new MediaSourceRegistry({
    enumerateDevices: () => {
      enumerateCalls += 1;
      return new Promise((resolve) => { resolveDevices = resolve; });
    },
  });
  const first = registry.enumerateCameraDevices(false);
  const second = registry.enumerateCameraDevices(false);
  assert.equal(enumerateCalls, 1);
  resolveDevices([{ kind: 'videoinput', deviceId: 'camera-a', label: 'Camera A' } as MediaDeviceInfo]);
  assert.deepEqual(await first, [{ deviceId: 'camera-a', label: 'Camera A' }]);
  assert.deepEqual(await second, [{ deviceId: 'camera-a', label: 'Camera A' }]);
});

test('camera startup deduplicates repeated requests for the same device behaviorally', async () => {
  const stream = fakeCameraMediaStream();
  let starts = 0;
  let resolveStart!: (stream: MediaStream) => void;
  const registry = new MediaSourceRegistry({
    getUserMedia: () => {
      starts += 1;
      return new Promise((resolve) => { resolveStart = resolve; });
    },
  });
  const first = registry.acquireCamera({ ownerId: 'manual-photo', deviceId: 'camera-a' });
  const second = registry.acquireCamera({ ownerId: 'automation', deviceId: 'camera-a' });
  assert.equal(starts, 1);
  resolveStart(stream as unknown as MediaStream);
  const leases = await Promise.all([first, second]);
  leases[0].release();
  leases[1].release();
  assert.equal(stream.track.stopCount, 1);
});

test('image lightbox escapes the app shell and fits the complete image inside the viewport', async () => {
  const source = await readFile(path.join(testDir, '../components/ImageLightbox.tsx'), 'utf8');

  assert.match(source, /createPortal\([\s\S]*document\.body/);
  assert.match(source, /max-w-\[calc\(100vw-2rem\)\]/);
  assert.match(source, /max-h-\[calc\(100dvh-2rem\)\]/);
  assert.match(source, /w-auto h-auto object-contain/);
});

test('vision pending image thumbnails open the shared lightbox', async () => {
  const source = await readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8');

  assert.match(
    source,
    /alt={`待发送图片 \$\{idx \+ 1\}`}[^>]*onClick=\{\(\) => setLightboxSrc\(img\)\}/,
  );
});

test('text and vision page headers use web UI title config', async () => {
  const textPage = await readFile(path.join(testDir, '../app/page.tsx'), 'utf8');
  const visionPage = await readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8');

  const header = await readFile(path.join(testDir, '../components/conversation/ConversationHeader.tsx'), 'utf8');
  assert.match(textPage, /<ConversationHeader pageMode="text" webUIConfig=\{webUIConfig\}/);
  assert.match(visionPage, /<ConversationHeader pageMode="vision" webUIConfig=\{webUIConfig\}/);
  assert.match(header, /getWebUIPageTitle\(webUIConfig, pageMode\)/);
  assert.match(header, /getWebUIHeaderSubtitle\(webUIConfig\)/);
  assert.doesNotMatch(textPage, /<Bot className="w-6 h-6 text-white"/);
  assert.doesNotMatch(visionPage, /<ImageIcon className="w-5 h-5 text-white"/);
});

test('text and vision pages expose collapsed plus action menus', async () => {
  const textPage = await readFile(path.join(testDir, '../app/page.tsx'), 'utf8');
  const visionPage = await readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8');
  const composer = await readFile(path.join(testDir, '../components/conversation/ConversationComposer.tsx'), 'utf8');
  const textActions = await readFile(path.join(testDir, '../components/conversation/ConversationTextInputActions.tsx'), 'utf8');
  const state = await readFile(path.join(testDir, './conversation/use-conversation-state.ts'), 'utf8');

  assert.match(composer, /<Plus/);
  assert.match(composer, /onClick=\{onToggleInputActions\}/);
  assert.match(composer, /aria-expanded=\{showInputActions\}/);
  assert.match(state, /const \[showInputActions, setShowInputActions\] = useState\(false\)/);
  for (const source of [textPage, visionPage]) {
    assert.match(source, /showInputActions=\{showInputActions\}/);
    assert.match(source, /<ConversationTextInputActions\s+visible=\{showInputActions\}/);
  }
  assert.match(textActions, /if \(!visible\) return null/);
  assert.match(textActions, /title="仅视觉模式可用"/);
  assert.match(visionPage, /showActionButtons=\{showInputActions\}/);
});

test('chat message avatars support configured center-cropped square images', async () => {
  const avatar = await readFile(path.join(testDir, '../components/ChatAvatar.tsx'), 'utf8');
  const textPage = await readFile(path.join(testDir, '../app/page.tsx'), 'utf8');
  const visionPage = await readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8');

  assert.match(avatar, /object-cover object-center/);
  assert.match(avatar, /rounded-lg/);
  assert.match(avatar, /getWebUIAvatarPath/);
  assert.match(avatar, /getWebUIAvatarSize/);
  assert.match(avatar, /style=\{\{ width: sizePx, height: sizePx \}\}/);
  const list = await readFile(path.join(testDir, '../components/conversation/ConversationMessageList.tsx'), 'utf8');
  assert.match(list, /<ChatAvatar role=\{message\.role === 'user' \? 'user' : 'assistant'\} config=\{webUIConfig\} \/>/);
  assert.match(textPage, /<ConversationMessageList[^\n]*webUIConfig=\{webUIConfig\}/);
  assert.match(visionPage, /<ConversationMessageList[^\n]*webUIConfig=\{webUIConfig\}/);
});

test('vision welcome screen uses configurable text and icon image', async () => {
  const visionPage = await readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8');

  assert.match(visionPage, /getWebUIVisionWelcomeConfig/);
  assert.match(visionPage, /welcomeConfig\.title/);
  assert.match(visionPage, /welcomeConfig\.description/);
  assert.match(visionPage, /welcomeConfig\.iconImagePath/);
  assert.match(visionPage, /object-contain/);
});

test('child transcript merges orchestration updates into the rendered plan step', () => {
  const plan = {
    id: 'orch-1',
    step_type: 'orchestration' as const,
    orchestration_type: 'plan' as const,
    stages: [
      {
        stage_id: 1,
        parallel: true,
        tools: [
          { call_id: 'call-1', name: 'read_file', args: {}, status: 'pending' as const },
        ],
      },
    ],
  };
  const update = {
    id: 'orch-update-1',
    step_type: 'orchestration' as const,
    orchestration_type: 'update' as const,
    orchestration_update: {
      call_id: 'call-1',
      status: 'completed' as const,
      result_preview: 'done',
      duration_ms: 12,
    },
  };
  const done = {
    id: 'orch-done-1',
    step_type: 'orchestration' as const,
    orchestration_type: 'done' as const,
    orchestration_summary: {
      total: 1,
      completed: 1,
      failed: 0,
      skipped: 0,
      timeout: 0,
      duration_ms: 12,
    },
  };

  const withPlan = mergeChildTranscriptStep([], plan);
  const withUpdate = mergeChildTranscriptStep(withPlan, update);
  const withDone = mergeChildTranscriptStep(withUpdate, done);

  assert.equal(withDone.length, 1);
  assert.equal(withDone[0].stages?.[0].tools[0].status, 'completed');
  assert.equal(withDone[0].stages?.[0].tools[0].result_preview, 'done');
  assert.equal(withDone[0].orchestration_summary?.completed, 1);
});

test('child transcript converts orchestration_done total_tools payload to summary total', () => {
  const step = childAgentStreamEventToStep({
    type: 'orchestration_done',
    total_tools: 2,
    completed: 1,
    failed: 1,
    skipped: 0,
    timeout: 0,
    duration_ms: 34,
  });

  assert.equal(step?.orchestration_summary?.total, 2);
  assert.equal(step?.orchestration_summary?.failed, 1);
});

test('child transcript merges partial thought chunks into one visible card', () => {
  const first = {
    id: 'thought-1',
    step_type: 'thought' as const,
    content: 'hello ',
    partial: true,
  };
  const second = {
    id: 'thought-1',
    step_type: 'thought' as const,
    content: 'world',
    partial: false,
  };

  const merged = mergeChildTranscriptStep(mergeChildTranscriptStep([], first), second);

  assert.equal(merged.length, 1);
  assert.equal(merged[0].content, 'hello world');
  assert.equal(merged[0].partial, false);
});

test('agentChatStream sends context_messages for mode-switch bootstrap', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  let capturedBody: any;
  globalThis.fetch = async (_url, init) => {
    capturedBody = JSON.parse(String(init?.body ?? '{}'));
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'done', session_id: 'session-1' })}\n\n`));
        controller.close();
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  await llmClient.agentChatStream(
    {
      session_id: 'session-1',
      message: '搜索：黄仁勋 最近去了哪个大学演讲',
      context_messages: [
        { role: 'user', content: '嘿，你是谁' },
        { role: 'assistant', content: '你好！我是运行在 NVIDIA Jetson Thor 平台上的 AI 助手。' },
        { role: 'user', content: '搜索：黄仁勋 最近去了哪个大学演讲' },
      ],
    },
    () => {},
    () => {},
  );

  assert.deepEqual(capturedBody.context_messages, [
    { role: 'user', content: '嘿，你是谁' },
    { role: 'assistant', content: '你好！我是运行在 NVIDIA Jetson Thor 平台上的 AI 助手。' },
    { role: 'user', content: '搜索：黄仁勋 最近去了哪个大学演讲' },
  ]);
});

test('chatStream sends enable_thinking option in request body', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  let capturedBody: any;
  globalThis.fetch = async (_url, init) => {
    capturedBody = JSON.parse(String(init?.body ?? '{}'));
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
        controller.close();
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  await llmClient.chatStream(
    [{ role: 'user', content: 'hello' }],
    { enable_thinking: true },
    () => {},
    () => {},
    () => {},
  );

  assert.equal(capturedBody.enable_thinking, true);
});

test('chatStream sends automation metadata and parses request acceptance', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let capturedBody: any;
  globalThis.fetch = async (_url, init) => {
    capturedBody = JSON.parse(String(init?.body));
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"type":"request_accepted","request_id":"request-1"}\n\n'));
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };
  const accepted: string[] = [];
  const client = new LLMClient('/api/backend');
  await client.chatStream(
    [{ role: 'user', content: 'hello' }],
    {
      request_id: 'request-1',
      vision_automation: {
        history_image_policy: 'aggregate_prior_image_blocks',
        manual_image_count: 1,
        source_count: 2,
        retained_tick_count: 3,
        binding: {
          page_generation: 2,
          mode: 'chat',
          top_level_context_id: 'chat-context-0001',
          automation_task_id: 'task_01J0123456789ABCDEFGH',
          capture_cycle_id: 'cycle_01J0123456789ABCDEFG',
        },
      },
    },
    () => {},
    () => {},
    (error) => { throw error; },
    undefined,
    undefined,
    { onRequestAccepted: (event) => accepted.push(event.requestId) },
  );

  assert.equal(capturedBody.request_id, 'request-1');
  assert.deepEqual(capturedBody.vision_automation, {
    history_image_policy: 'aggregate_prior_image_blocks',
    manual_image_count: 1,
    source_count: 2,
    retained_tick_count: 3,
    binding: {
      page_generation: 2,
      mode: 'chat',
      top_level_context_id: 'chat-context-0001',
      automation_task_id: 'task_01J0123456789ABCDEFGH',
      capture_cycle_id: 'cycle_01J0123456789ABCDEFG',
    },
  });
  assert.equal(capturedBody.metadata, undefined);
  assert.deepEqual(accepted, ['request-1']);
});

test('Hub-backed chatStream dispatches canonical acceptance, Chat deltas, and terminal through one sequenced path', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let capturedBody: Record<string, unknown> = {};
  globalThis.fetch = async (_url, init) => {
    capturedBody = JSON.parse(String(init?.body));
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        for (const payload of [
          { event_sequence: 7, type: 'request_accepted', request_id: 'req-chat', session_id: 'session-chat', status: 'accepted' },
          { event_sequence: 8, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'session-chat', turn_index: 2, user_content: 'hello', images: [], message_id: 'chat:session-chat:2:assistant' } },
          { event_sequence: 9, type: 'timeline_event', event: { kind: 'message_delta', source: 'chat', display: 'chat_bubble', message_id: 'chat:session-chat:2:assistant', text: 'R', reasoning: true, partial: true } },
          { event_sequence: 10, type: 'timeline_event', event: { kind: 'message_delta', source: 'chat', display: 'chat_bubble', message_id: 'chat:session-chat:2:assistant', text: 'A', reasoning: false, partial: true } },
          { event_sequence: 11, type: 'done', request_id: 'req-chat', session_id: 'session-chat', status: 'completed' },
        ]) controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const events: string[] = [];
  await new LLMClient('/api/backend').chatStream(
    [{ role: 'user', content: 'hello' }],
    { request_id: 'req-chat', session_id: 'session-chat' } as any,
    (content, reasoning) => events.push(`delta:${content}:${reasoning ?? ''}`),
    () => { events.push('complete'); },
    (error) => { throw error; },
    undefined,
    (event) => events.push(`timeline:${event.kind}`),
    { onRequestAccepted: (event) => { events.push(`accepted:${event.requestId}`); } },
  );

  assert.equal(capturedBody.session_id, 'session-chat');
  assert.equal(Object.hasOwn(capturedBody, 'chat_session_id'), false);
  assert.deepEqual(events, [
    'accepted:req-chat',
    'timeline:turn_started',
    'delta::R',
    'delta:A:',
    'complete',
  ]);
});

test('agentChatStream sends the same request ID and automation metadata shape', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let capturedBody: any;
  globalThis.fetch = async (_url, init) => {
    capturedBody = JSON.parse(String(init?.body));
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"event_sequence":1,"type":"request_accepted","request_id":"request-1"}\n\n'));
        controller.enqueue(encoder.encode('data: {"event_sequence":2,"type":"done","session_id":"session-1"}\n\n'));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };
  const accepted: string[] = [];
  const client = new LLMClient('/api/backend');
  await client.agentChatStream(
    {
      session_id: 'session-1',
      message: 'hello',
      request_id: 'request-1',
      vision_automation: {
        history_image_policy: 'aggregate_prior_image_blocks',
        manual_image_count: 1,
        source_count: 2,
        retained_tick_count: 3,
        binding: {
          page_generation: 2,
          mode: 'agent',
          top_level_context_id: 'session-1',
          automation_task_id: 'task_01J0123456789ABCDEFGH',
          capture_cycle_id: 'cycle_01J0123456789ABCDEFG',
        },
      },
    },
    () => {},
    () => {},
    () => {},
    (error) => { throw error; },
    undefined,
    undefined,
    undefined,
    { onRequestAccepted: (event) => accepted.push(event.requestId) },
  );

  assert.equal(capturedBody.request_id, 'request-1');
  assert.deepEqual(capturedBody.vision_automation, {
    history_image_policy: 'aggregate_prior_image_blocks',
    manual_image_count: 1,
    source_count: 2,
    retained_tick_count: 3,
    binding: {
      page_generation: 2,
      mode: 'agent',
      top_level_context_id: 'session-1',
      automation_task_id: 'task_01J0123456789ABCDEFGH',
      capture_cycle_id: 'cycle_01J0123456789ABCDEFG',
    },
  });
  assert.equal(capturedBody.metadata, undefined);
  assert.deepEqual(accepted, ['request-1']);
});

test('raw stream EOF reports transport EOF without inferring completion', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) { controller.close(); },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });

  let completed = false;
  let eof = false;
  const client = new LLMClient('/api/backend');
  await client.chatStream(
    [{ role: 'user', content: 'hello' }],
    { request_id: 'request-1' },
    () => {},
    () => { completed = true; },
    (error) => { throw error; },
    undefined,
    undefined,
    { onTransportEof: () => { eof = true; } },
  );

  assert.equal(completed, false);
  assert.equal(eof, true);
});

test('raw Agent stream EOF does not infer done or a stable terminal state', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) { controller.close(); },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });

  let completed = false;
  let eof = false;
  const client = new LLMClient('/api/backend');
  await client.agentChatStream(
    { message: 'hello', request_id: 'request-1' },
    () => {},
    () => {},
    () => { completed = true; },
    (error) => { throw error; },
    undefined,
    undefined,
    undefined,
    { onTransportEof: () => { eof = true; } },
  );

  assert.equal(completed, false);
  assert.equal(eof, true);
});

test('getRequestStatus queries the provider-neutral endpoint and validates response identity', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let observedUrl = '';
  globalThis.fetch = async (url) => {
    observedUrl = String(url);
    return Response.json({ request_id: 'request:a', status: 'running' });
  };

  const client = new LLMClient('/api/backend');
  const status = await client.getRequestStatus('request:a');

  assert.equal(observedUrl, '/api/backend/v1/request-status/request%3Aa');
  assert.deepEqual(status, { requestId: 'request:a', status: 'running' });
});

test('getRequestStatus rejects IDs outside the exact backend grammar before fetch', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let fetched = false;
  globalThis.fetch = async () => {
    fetched = true;
    return Response.json({ request_id: 'request/a', status: 'unknown' });
  };

  const client = new LLMClient('/api/backend');
  await assert.rejects(client.getRequestStatus('request/a'), /requestId/);
  assert.equal(fetched, false);
});

test('registerAgentVisionAutomationAbort sends the exact contract and validates response identity', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let observedUrl = '';
  let observedBody: unknown;
  globalThis.fetch = async (url, init) => {
    observedUrl = String(url);
    observedBody = JSON.parse(String(init?.body));
    return Response.json({ status: 'cleanup_pending', session_id: 'agent-session-0001' });
  };
  const client = new LLMClient('/api/backend');
  const request = {
    session_id: 'agent-session-0001',
    binding: {
      page_generation: 2,
      mode: 'agent' as const,
      top_level_context_id: 'agent-session-0001',
      automation_task_id: 'task_01J0123456789ABCDEFGH',
      capture_cycle_id: 'cycle_01J0123456789ABCDEFG',
      cleanup_token: null,
    },
    cleanup_token: 'cleanup_01J0123456789ABCDEFG',
  };

  const result = await client.registerAgentVisionAutomationAbort(request);

  assert.equal(observedUrl, '/api/backend/v1/agent/vision-automation/abort');
  assert.deepEqual(observedBody, request);
  assert.deepEqual(result, { status: 'cleanup_pending', session_id: 'agent-session-0001' });

  globalThis.fetch = async () => Response.json({
    status: 'cleanup_pending',
    session_id: 'agent-session-OTHER',
  });
  await assert.rejects(client.registerAgentVisionAutomationAbort(request), /identity/i);
});

test('cancelAgentVisionAutomationAbort sends the exact registration contract with the caller signal', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const controller = new AbortController();
  let observedUrl = '';
  let observedBody: unknown;
  let observedSignal: AbortSignal | null | undefined;
  const request = {
    session_id: 'agent-session-0001',
    binding: {
      page_generation: 2,
      mode: 'agent' as const,
      top_level_context_id: 'agent-session-0001',
      automation_task_id: 'task_01J0123456789ABCDEFGH',
      capture_cycle_id: 'cycle_01J0123456789ABCDEFG',
      cleanup_token: null,
    },
    cleanup_token: 'cleanup_01J0123456789ABCDEFG',
  };
  globalThis.fetch = async (url, init) => {
    observedUrl = String(url);
    observedBody = JSON.parse(String(init?.body));
    observedSignal = init?.signal;
    return Response.json({ status: 'cleanup_cancelled', session_id: request.session_id });
  };

  const result = await new LLMClient('/api/backend').cancelAgentVisionAutomationAbort(
    request,
    controller.signal,
  );

  assert.equal(observedUrl, '/api/backend/v1/agent/vision-automation/abort/cancel');
  assert.deepEqual(observedBody, request);
  assert.equal(observedSignal, controller.signal);
  assert.deepEqual(result, { status: 'cleanup_cancelled', session_id: request.session_id });
});

test('deleteAgentSession sends the exact encoded DELETE request and optional keepalive', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const requests: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), init });
    return Response.json({ status: 'ok' });
  };
  const client = new LLMClient('/api/backend');
  const deleteAgentSession = (
    client as LLMClient & {
      deleteAgentSession?: (sessionId: string, options?: { keepalive?: boolean }) => Promise<void>;
    }
  ).deleteAgentSession;

  assert.equal(typeof deleteAgentSession, 'function');
  await deleteAgentSession?.call(client, 'agent/session old', { keepalive: true });
  await deleteAgentSession?.call(client, 'agent-session-current');

  assert.equal(requests[0].url, '/api/backend/v1/agent/session/agent%2Fsession%20old');
  assert.equal(requests[0].init?.method, 'DELETE');
  assert.equal(requests[0].init?.keepalive, true);
  assert.equal(requests[1].url, '/api/backend/v1/agent/session/agent-session-current');
  assert.equal(requests[1].init?.keepalive, undefined);

  globalThis.fetch = async () => new Response('failed', { status: 503, statusText: 'Unavailable' });
  await assert.rejects(
    deleteAgentSession?.call(client, 'agent-session-current'),
    /delete.*failed|Unavailable/i,
  );
});

test('answerNowStream sends replacement request identity and stabilizes terminal lifecycle', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const order: string[] = [];
  let observedBody: unknown;
  globalThis.fetch = async (url, init) => {
    if (String(url).includes('/request-status/')) {
      order.push('status');
      return Response.json({ request_id: 'answer-now-request-0001', status: 'terminal' });
    }
    observedBody = JSON.parse(String(init?.body));
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(stream) {
        stream.enqueue(encoder.encode(
          'data: {"event_sequence":1,"type":"request_accepted","request_id":"answer-now-request-0001"}\n\n'
          + 'data: {"event_sequence":2,"type":"session_start","session_id":"agent-session-0001"}\n\n'
          + 'data: {"event_sequence":3,"type":"content","content":"final"}\n\n'
          + 'data: {"event_sequence":4,"type":"done","request_id":"answer-now-request-0001","session_id":"agent-session-0001"}\n\n',
        ));
        stream.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  await new LLMClient('/api/backend').answerNowStream(
    'agent-session-0001',
    'answer-now-request-0001',
    (content) => order.push(`content:${content}`),
    () => order.push('complete'),
    (error) => { throw error; },
    undefined,
    undefined,
    {
      pollIntervalMs: 0,
      onRequestAccepted: () => { order.push('accepted'); },
      onStableTerminal: () => { order.push('terminal'); },
    },
  );

  assert.deepEqual(observedBody, {
    session_id: 'agent-session-0001',
    request_id: 'answer-now-request-0001',
  });
  assert.deepEqual(order, ['accepted', 'content:final', 'complete', 'status', 'terminal']);
});

test('answerNowStream validates sequenced Hub payloads through the shared handler bundle', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(stream) {
        for (const payload of [
          { event_sequence: 3, type: 'request_accepted', request_id: 'answer-now-request-0001', session_id: 'session-1' },
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

test('answerNowStream reports ambiguous EOF instead of inferring replacement completion', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(stream) { stream.close(); },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  const events: string[] = [];

  await new LLMClient('/api/backend').answerNowStream(
    'agent-session-0001',
    'answer-now-request-0001',
    () => {},
    () => { events.push('complete'); },
    (error) => { throw error; },
    undefined,
    undefined,
    { onTransportEof: () => { events.push('eof'); } },
  );

  assert.deepEqual(events, ['eof']);
});

test('accepted Agent error event reports an interrupted transport for status stabilization', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const encoder = new TextEncoder();
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode('data: {"event_sequence":1,"type":"request_accepted","request_id":"request-1"}\n\n'));
      controller.enqueue(encoder.encode('data: {"event_sequence":2,"type":"error","error":"worker stopped"}\n\n'));
      controller.close();
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  const events: string[] = [];

  await new LLMClient('/api/backend').agentChatStream(
    { session_id: 'agent-session-0001', message: 'hello', request_id: 'request-1' },
    () => {},
    () => {},
    undefined,
    (error) => { events.push(`error:${error.message}`); },
    undefined,
    undefined,
    undefined,
    {
      onRequestAccepted: () => { events.push('accepted'); },
      onTransportEof: () => { events.push('interrupted'); },
    },
  );

  assert.deepEqual(events, ['accepted', 'error:worker stopped', 'interrupted']);
});

test('accepted Agent Stop or Answer Now abort reports interruption for stable-terminal recovery', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const encoder = new TextEncoder();
  globalThis.fetch = async (_url, init) => new Response(new ReadableStream({
    start(stream) {
      stream.enqueue(encoder.encode('data: {"event_sequence":1,"type":"request_accepted","request_id":"request-1"}\n\n'));
      init?.signal?.addEventListener('abort', () => {
        stream.error(init.signal?.reason ?? new DOMException('stopped', 'AbortError'));
      }, { once: true });
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  const controller = new AbortController();
  const events: string[] = [];

  await new LLMClient('/api/backend').agentChatStream(
    { session_id: 'agent-session-0001', message: 'hello', request_id: 'request-1' },
    () => {},
    () => {},
    undefined,
    (error) => { events.push(`error:${error.message}`); },
    undefined,
    controller,
    undefined,
    {
      onRequestAccepted: () => {
        events.push('accepted');
        controller.abort(new DOMException('Stop or Answer Now', 'AbortError'));
      },
      onTransportEof: () => { events.push('interrupted'); },
    },
  );

  assert.deepEqual(events, ['accepted', 'error:Request timeout or aborted', 'interrupted']);
});

test('Chat candidate completion polls until terminal before stable-terminal callback', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const order: string[] = [];
  const statuses = ['running', 'terminal'];
  globalThis.fetch = async (url) => {
    if (String(url).includes('/request-status/')) {
      const status = statuses.shift();
      order.push(`status:${status}`);
      return Response.json({ request_id: 'request-1', status });
    }
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const client = new LLMClient('/api/backend');
  await client.chatStream(
    [{ role: 'user', content: 'hello' }],
    { request_id: 'request-1' },
    () => {},
    () => { order.push('complete'); },
    (error) => { throw error; },
    undefined,
    undefined,
    {
      pollIntervalMs: 0,
      onStableTerminal: () => { order.push('terminal'); },
    },
  );

  assert.deepEqual(order, ['complete', 'status:running', 'status:terminal', 'terminal']);
});

test('Chat candidate completion survives reader cancellation rejection and still polls terminal', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const order: string[] = [];
  globalThis.fetch = async (url) => {
    if (String(url).includes('/request-status/')) {
      order.push('status');
      return Response.json({ request_id: 'request-1', status: 'terminal' });
    }
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      },
      cancel() {
        return Promise.reject(new Error('cancel failed'));
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const client = new LLMClient('/api/backend');
  await client.chatStream(
    [{ role: 'user', content: 'hello' }],
    { request_id: 'request-1' },
    () => {},
    () => { order.push('complete'); },
    (error) => { order.push(`error:${error.message}`); },
    undefined,
    undefined,
    { onStableTerminal: () => { order.push('terminal'); } },
  );

  assert.deepEqual(order, ['complete', 'status', 'terminal']);
});

test('captured frontend Chat and Agent bodies parse with the real FastAPI models', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const bodies: Record<string, unknown> = {};
  globalThis.fetch = async (url, init) => {
    const kind = String(url).includes('/agent/') ? 'agent' : 'chat';
    bodies[kind] = JSON.parse(String(init?.body));
    const requestId = (bodies[kind] as { request_id: string }).request_id;
    const terminal = kind === 'agent'
      ? `data: {"event_sequence":2,"type":"done","session_id":"session-1"}\n\n`
      : 'data: [DONE]\n\n';
    const accepted = kind === 'agent'
      ? `data: {"event_sequence":1,"type":"request_accepted","request_id":"${requestId}"}\n\n`
      : `data: {"type":"request_accepted","request_id":"${requestId}"}\n\n`;
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(accepted + terminal));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };
  const metadata = {
    history_image_policy: 'aggregate_prior_image_blocks' as const,
    manual_image_count: 0,
    source_count: 0,
    retained_tick_count: 0,
    binding: {
      page_generation: 2,
      mode: 'chat' as const,
      top_level_context_id: 'chat-context-0001',
      automation_task_id: 'task_01J0123456789ABCDEFGH',
      capture_cycle_id: 'cycle_01J0123456789ABCDEFG',
    },
  };
  const client = new LLMClient('/api/backend');
  await client.chatStream(
    [{ role: 'user', content: 'hello' }],
    { request_id: 'request-chat-1', vision_automation: metadata },
    () => {},
    () => {},
    (error) => { throw error; },
  );
  await client.agentChatStream(
    {
      session_id: 'agent-session-0001',
      message: 'hello',
      request_id: 'request-agent-1',
      vision_automation: {
        ...metadata,
        binding: {
          ...metadata.binding,
          mode: 'agent',
          top_level_context_id: 'agent-session-0001',
        },
      },
    },
    () => {},
    () => {},
    () => {},
    (error) => { throw error; },
  );

  const workspaceRoot = path.resolve(testDir, '../../..');
  const script = [
    'import json, sys',
    'from backend.api.chat import ChatCompletionRequest',
    'from backend.api.agent import AgentChatRequest',
    'payload = json.loads(sys.argv[1])',
    'chat = ChatCompletionRequest.model_validate(payload["chat"])',
    'agent = AgentChatRequest.model_validate(payload["agent"])',
    'assert chat.vision_automation is not None',
    'assert agent.vision_automation is not None',
    'assert "metadata" not in payload["chat"] and "metadata" not in payload["agent"]',
    'assert payload["chat"]["vision_automation"]["binding"]["mode"] == "chat"',
    'assert payload["agent"]["vision_automation"]["binding"]["mode"] == "agent"',
    'assert payload["agent"]["vision_automation"]["binding"]["top_level_context_id"] == payload["agent"]["session_id"]',
  ].join('; ');
  const result = spawnSync(
    path.join(workspaceRoot, '.sglang/bin/python'),
    ['-c', script, JSON.stringify(bodies)],
    { cwd: workspaceRoot, encoding: 'utf8', timeout: 10000 },
  );
  assert.equal(result.status, 0, result.stderr || result.stdout);
});

test('Agent done polls until terminal and propagates the caller AbortSignal', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const controller = new AbortController();
  const observedSignals: AbortSignal[] = [];
  globalThis.fetch = async (url, init) => {
    if (String(url).includes('/request-status/')) {
      observedSignals.push(init!.signal as AbortSignal);
      return Response.json({ request_id: 'request-1', status: 'terminal' });
    }
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(stream) {
        stream.enqueue(encoder.encode('data: {"event_sequence":1,"type":"done","request_id":"request-1","session_id":"session-1"}\n\n'));
        stream.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };
  let terminal = false;

  const client = new LLMClient('/api/backend');
  await client.agentChatStream(
    { message: 'hello', request_id: 'request-1' },
    () => {},
    () => {},
    () => {},
    (error) => { throw error; },
    undefined,
    controller,
    undefined,
    {
      pollIntervalMs: 0,
      onStableTerminal: () => { terminal = true; },
    },
  );

  assert.equal(terminal, true);
  assert.deepEqual(observedSignals, [controller.signal]);
});

test('request acceptance awaits an async omission commit before later stream events', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const order: string[] = [];
  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(stream) {
        stream.enqueue(encoder.encode(
          'data: {"type":"request_accepted","request_id":"request-1"}\n\n'
          + 'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'
          + 'data: [DONE]\n\n',
        ));
        stream.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const client = new LLMClient('/api/backend');
  await client.chatStream(
    [{ role: 'user', content: 'hello' }],
    { request_id: 'request-1' },
    () => { order.push('content'); },
    () => { order.push('complete'); },
    (error) => { throw error; },
    undefined,
    undefined,
    {
      onRequestAccepted: async () => {
        await Promise.resolve();
        order.push('accepted');
      },
    },
  );

  assert.deepEqual(order, ['accepted', 'content', 'complete']);
});

test('agentChatStream sends enable_thinking in request body', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  let capturedBody: any;
  globalThis.fetch = async (_url, init) => {
    capturedBody = JSON.parse(String(init?.body ?? '{}'));
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ event_sequence: 1, type: 'done', session_id: 'session-1' })}\n\n`));
        controller.close();
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  await llmClient.agentChatStream(
    { message: 'hello', enable_thinking: true },
    () => {},
    () => {},
  );

  assert.equal(capturedBody.enable_thinking, true);
});

test('chatStream forwards chat auto-compact SSE events to timeline callback', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    const chunks = [
      `data: ${JSON.stringify({
        kind: 'compact_started',
        summary_message_id: 'compact-1',
        metadata: { trigger: 'auto', mode: 'chat' },
        trigger: 'auto',
        mode: 'chat',
      })}\n\n`,
      `data: ${JSON.stringify({
        kind: 'compact_completed',
        summary_message_id: 'compact-1',
        text: 'Summary:\nDone',
        metadata: { trigger: 'auto', mode: 'chat' },
      })}\n\n`,
      `data: ${JSON.stringify({
        choices: [{ delta: { content: 'answer' } }],
      })}\n\n`,
      'data: [DONE]\n\n',
    ];

    return new Response(new ReadableStream({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  const client = new LLMClient('/api/backend');
  const events: any[] = [];
  const chunks: string[] = [];
  let completed = false;

  await client.chatStream(
    [{ role: 'user', content: 'hello' }],
    { temperature: 0, top_p: 1, max_tokens: 64 },
    (content) => chunks.push(content),
    () => {
      completed = true;
    },
    (error) => {
      throw error;
    },
    undefined,
    (event) => events.push(event),
  );

  assert.deepEqual(events.map((event) => event.kind), ['compact_started', 'compact_completed']);
  assert.deepEqual(chunks, ['answer']);
  assert.equal(completed, true);
});

test('agentChatStream completes when done event is received even if stream cancel fails', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ event_sequence: 1, type: 'done', session_id: 'session-1' })}\n\n`));
      },
      cancel() {
        throw new TypeError('Error in input stream');
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  const client = new LLMClient('/api/backend');
  let completedSessionId: string | undefined;
  let error: Error | undefined;

  await client.agentChatStream(
    { message: 'use search_and_fetch twice' },
    () => {},
    () => {},
    (sessionId) => {
      completedSessionId = sessionId;
    },
    (err) => {
      error = err;
    },
  );

  assert.equal(completedSessionId, 'session-1');
  assert.equal(error, undefined);
});

test('startCycle completes when DONE is received even if stream cancel fails', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  globalThis.fetch = async () => {
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      },
      cancel() {
        throw new TypeError('Error in input stream');
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  const client = new LLMClient('/api/backend');
  let completed = false;
  let error: Error | undefined;

  await new Promise<void>((resolve) => {
    void client.startCycle(
      { steps: [], agent_mode: true, share_context: true },
      () => {},
      () => {
        completed = true;
        resolve();
      },
      (err) => {
        error = err;
        resolve();
      },
    );
  });

  assert.equal(completed, true);
  assert.equal(error, undefined);
});

test('startCycle sends an explicit run identity, browser timezone, and scheduled track', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  let requestUrl = '';
  let requestBody: Record<string, unknown> | undefined;
  globalThis.fetch = async (input, init) => {
    requestUrl = String(input);
    requestBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const client = new LLMClient('/api/backend');
  await new Promise<void>((resolve, reject) => {
    void client.startCycle(
      {
        cycle_run_id: 'cycle-run-1',
        steps: [],
        scheduled_messages: [{
          id: 'wake-up',
          time: '09:30:00',
          recurrence: 'daily',
          day_count: 3,
          message: 'scheduled hello',
        }],
        timezone: 'Asia/Shanghai',
        agent_mode: true,
        show_agent_steps: true,
        enable_thinking: true,
        share_context: true,
      } as Parameters<LLMClient['startCycle']>[0],
      () => {},
      resolve,
      reject,
    );
  });

  assert.equal(requestUrl, '/api/backend/v1/cycle/start');
  assert.deepEqual(requestBody, {
    cycle_run_id: 'cycle-run-1',
    steps: [],
    scheduled_messages: [{
      id: 'wake-up',
      time: '09:30:00',
      recurrence: 'daily',
      day_count: 3,
      message: 'scheduled hello',
    }],
    timezone: 'Asia/Shanghai',
    agent_mode: true,
    show_agent_steps: true,
    enable_thinking: true,
    enable_local_memory: false,
    share_context: true,
  });
});

test('resumeCycle surfaces expired event journals without retrying', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let fetches = 0;
  globalThis.fetch = async () => {
    fetches += 1;
    return new Response(JSON.stringify({ detail: { code: 'cycle_run_expired' } }), {
      status: 410,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const error = await new Promise<Error>((resolve) => {
    new LLMClient('/api/backend').resumeCycle('cycle-expired', 'fingerprint', 0, () => {}, () => {
      throw new Error('unexpected completion');
    }, resolve);
  });

  assert.equal(fetches, 1);
  assert.ok(error instanceof CycleRecoveryError);
  assert.equal((error as CycleRecoveryError).code, 'expired');
  assert.equal((error as CycleRecoveryError).retryable, false);
});

test('resumeCycle surfaces unavailable runs without retrying', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let fetches = 0;
  globalThis.fetch = async () => {
    fetches += 1;
    return new Response(JSON.stringify({ detail: { code: 'cycle_run_unavailable' } }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const error = await new Promise<Error>((resolve) => {
    new LLMClient('/api/backend').resumeCycle('cycle-unavailable', 'fingerprint', 0, () => {}, () => {
      throw new Error('unexpected completion');
    }, resolve);
  });

  assert.equal(fetches, 1);
  assert.ok(error instanceof CycleRecoveryError);
  assert.equal((error as CycleRecoveryError).code, 'unavailable');
  assert.equal((error as CycleRecoveryError).retryable, false);
});

test('attachCycle replays only events newer than the supplied cursor and ignores heartbeats', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  let requestUrl = '';
  globalThis.fetch = async (input) => {
    requestUrl = String(input);
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(': ping\n\n'));
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'step_begin', event_sequence: 8, track: 'scheduled', step_type: 'message', path: [] })}\n\n`));
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const events: Array<Record<string, unknown>> = [];
  const client = new LLMClient('/api/backend');
  await client.attachCycle('cycle-run-1', 'fingerprint-1', 7, (event) => events.push(event), () => {});

  assert.equal(requestUrl, '/api/backend/v1/cycle/cycle-run-1/events?after_sequence=7&request_fingerprint=fingerprint-1');
  assert.deepEqual(events, [{ type: 'step_begin', event_sequence: 8, track: 'scheduled', step_type: 'message', path: [] }]);
});

test('mergeServerDefaultsWithCache gives cached keys priority over server defaults by key presence', () => {
  const serverDefaults = {
    temperature: 0.7,
    topP: 0.9,
    maxTokens: 8192,
    showReasoning: true,
    systemPrompt: 'yaml default',
  };
  const cached = {
    temperature: 1.5,
    showReasoning: false,
    staleUnknownKey: 'ignored',
  };

  assert.deepEqual(mergeServerDefaultsWithCache(serverDefaults, cached), {
    temperature: 1.5,
    topP: 0.9,
    maxTokens: 8192,
    showReasoning: false,
    systemPrompt: 'yaml default',
  });
});

test('mergeServerDefaultsWithCache falls back to server defaults without a usable cache', () => {
  const serverDefaults = { temperature: 0.7, showReasoning: true };

  assert.deepEqual(mergeServerDefaultsWithCache(serverDefaults, null), { ...serverDefaults });
  assert.deepEqual(mergeServerDefaultsWithCache(serverDefaults, undefined), { ...serverDefaults });
  assert.deepEqual(mergeServerDefaultsWithCache(serverDefaults, {}), { ...serverDefaults });
});

test('parseCachedChatSettings returns null outside the browser', () => {
  assert.equal(parseCachedChatSettings(), null);
});

for (const source of ['model_text', 'provider_payload'] as const) {
  test(`raw disclosure source ${source} survives API dispatch and child thought merging`, async () => {
    const raw = source === 'model_text'
      ? '<tool_call>\n<function=read_file>\n<parameter=file_path>\n /tmp/a \n</parameter>\n</function>\n</tool_call>'
      : '{"id":"call-1","function":{"name":"read_file","arguments":"{\\"file_path\\":\\"/tmp/a\\"}"}}';
    const payload = { type: 'agent_step', step_type: 'thought', raw_tool_call: raw, raw_tool_call_source: source, partial: true };
    let received: any;
    await dispatchAgentSessionPayload(payload, { onStep: (step) => { received = step; } });
    assert.equal(received.raw_tool_call_source, source);
    assert.equal(received.raw_tool_call, raw);
    const step = childAgentStreamEventToStep(payload);
    assert.ok(step);
    const merged = mergeChildTranscriptStep([{ id:'before', step_type:'thought', content:'reasoning', partial:true }], step);
    assert.equal(merged[0].raw_tool_call_source, source);
    const continued = mergeChildTranscriptStep(merged, { id:'tail', step_type:'thought', content:' tail', partial:false });
    assert.equal(continued[0].raw_tool_call_source, source);
    assert.equal(continued[0].raw_tool_call, raw);
  });
}
