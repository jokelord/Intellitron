import assert from 'node:assert/strict';
import http from 'node:http';
import test from 'node:test';

import { MAX_VISION_JSON_REQUEST_BODY_BYTES } from './backend-proxy-policy.ts';

test('backend proxy forwards API requests to the local backend and preserves SSE response', async (t) => {
  const originalBackendUrl = process.env.BACKEND_API_BASE_URL;
  let observedMethod: string | undefined;
  let observedBody: string | undefined;
  let observedPath: string | undefined;

  const server = http.createServer((req, res) => {
    observedMethod = req.method;
    observedPath = req.url;
    const chunks: Buffer[] = [];
    req.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
    req.on('end', () => {
      observedBody = Buffer.concat(chunks).toString('utf8');
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.end('data: {"type":"done"}\n\n');
    });
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');

  t.after(() => {
    if (originalBackendUrl === undefined) {
      delete process.env.BACKEND_API_BASE_URL;
    } else {
      process.env.BACKEND_API_BASE_URL = originalBackendUrl;
    }
    server.close();
  });

  process.env.BACKEND_API_BASE_URL = `http://127.0.0.1:${address.port}`;

  const { POST } = await import('../app/api/backend/[...path]/route.ts');
  const response = await POST(
    new Request('http://192.168.2.54:3000/api/backend/v1/agent/chat?debug=1', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{"message":"hi"}',
    }),
    { params: { path: ['v1', 'agent', 'chat'] } },
  );

  assert.equal(observedMethod, 'POST');
  assert.equal(observedPath, '/v1/agent/chat?debug=1');
  assert.equal(observedBody, '{"message":"hi"}');
  assert.equal(response.headers.get('content-type'), 'text/event-stream');
  assert.equal(await response.text(), 'data: {"type":"done"}\n\n');
});

test('backend proxy returns a visible 502 when the local backend request fails', async (t) => {
  const originalBackendUrl = process.env.BACKEND_API_BASE_URL;
  const server = http.createServer();
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  await new Promise<void>((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
  });

  t.after(() => {
    if (originalBackendUrl === undefined) {
      delete process.env.BACKEND_API_BASE_URL;
    } else {
      process.env.BACKEND_API_BASE_URL = originalBackendUrl;
    }
  });

  process.env.BACKEND_API_BASE_URL = `http://127.0.0.1:${address.port}`;

  const { POST } = await import('../app/api/backend/[...path]/route.ts');
  const response = await POST(
    new Request('http://192.168.2.54:3000/api/backend/v1/agent/chat', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{"message":"hi"}',
    }),
    { params: { path: ['v1', 'agent', 'chat'] } },
  );

  assert.equal(response.status, 502);
  assert.equal(response.headers.get('content-type'), 'application/json');
  const body = await response.json();
  assert.equal(body.error, 'backend_proxy_failed');
  assert.match(body.message, /ECONNREFUSED|connect/);
});

test('backend proxy streams SSE without relying on fetch body timeout handling', async (t) => {
  const originalFetch = globalThis.fetch;
  const originalBackendUrl = process.env.BACKEND_API_BASE_URL;

  const server = http.createServer((req, res) => {
    assert.equal(req.method, 'POST');
    assert.equal(req.url, '/v1/cycle/start');
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
    });
    res.write('data: {"type":"cycle_started"}\n\n');
    setTimeout(() => {
      res.end('data: [DONE]\n\n');
    }, 10);
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');

  t.after(() => {
    globalThis.fetch = originalFetch;
    if (originalBackendUrl === undefined) {
      delete process.env.BACKEND_API_BASE_URL;
    } else {
      process.env.BACKEND_API_BASE_URL = originalBackendUrl;
    }
    server.close();
  });

  process.env.BACKEND_API_BASE_URL = `http://127.0.0.1:${address.port}`;
  globalThis.fetch = (async () => {
    const error = new TypeError('terminated');
    (error as Error & { cause?: unknown }).cause = { code: 'UND_ERR_BODY_TIMEOUT' };
    throw error;
  }) as typeof fetch;

  const { POST } = await import('../app/api/backend/[...path]/route.ts');
  const response = await POST(
    new Request('http://192.168.2.54:3000/api/backend/v1/cycle/start', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{"steps":[]}',
    }),
    { params: { path: ['v1', 'cycle', 'start'] } },
  );

  assert.equal(response.status, 200);
  assert.equal(response.headers.get('content-type'), 'text/event-stream');
  assert.equal(
    await response.text(),
    'data: {"type":"cycle_started"}\n\ndata: [DONE]\n\n',
  );
});

test('backend proxy rejects declared oversized Vision JSON before reading or forwarding', async (t) => {
  const originalBackendUrl = process.env.BACKEND_API_BASE_URL;
  let upstreamRequests = 0;
  const server = http.createServer((_req, res) => {
    upstreamRequests += 1;
    res.end();
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  t.after(() => {
    if (originalBackendUrl === undefined) delete process.env.BACKEND_API_BASE_URL;
    else process.env.BACKEND_API_BASE_URL = originalBackendUrl;
    server.close();
  });
  process.env.BACKEND_API_BASE_URL = `http://127.0.0.1:${address.port}`;

  const request = new Request('http://localhost/api/backend/v1/agent/chat', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'content-length': String(MAX_VISION_JSON_REQUEST_BODY_BYTES + 1),
    },
    body: new ReadableStream<Uint8Array>({
      pull(controller) {
        controller.enqueue(new Uint8Array([1]));
        controller.close();
      },
    }),
    duplex: 'half',
  } as RequestInit & { duplex: 'half' });
  let readerRequested = false;
  const getReader = request.body!.getReader.bind(request.body);
  request.body!.getReader = (...args) => {
    readerRequested = true;
    return getReader(...args);
  };
  const { POST } = await import('../app/api/backend/[...path]/route.ts');
  const response = await POST(request, { params: { path: ['v1', 'agent', 'chat'] } });

  assert.equal(response.status, 413);
  assert.equal(readerRequested, false);
  assert.equal(upstreamRequests, 0);
  assert.deepEqual(await response.json(), {
    error: 'payload_too_large',
    max_bytes: MAX_VISION_JSON_REQUEST_BODY_BYTES,
  });
});

test('backend proxy rejects encoded dot segments before policy lookup or forwarding', async (t) => {
  const originalBackendUrl = process.env.BACKEND_API_BASE_URL;
  let upstreamRequests = 0;
  const server = http.createServer((_req, res) => {
    upstreamRequests += 1;
    res.end();
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  t.after(() => {
    if (originalBackendUrl === undefined) delete process.env.BACKEND_API_BASE_URL;
    else process.env.BACKEND_API_BASE_URL = originalBackendUrl;
    server.close();
  });
  process.env.BACKEND_API_BASE_URL = `http://127.0.0.1:${address.port}`;

  const { POST } = await import('../app/api/backend/[...path]/route.ts');
  const response = await POST(
    new Request('http://localhost/api/backend/v1/%252e%252e/health', {
      method: 'POST',
      body: '{}',
    }),
    { params: { path: ['v1', '%252e%252e', 'health'] } },
  );

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: 'invalid_backend_path' });
  assert.equal(upstreamRequests, 0);
});

test('backend proxy keeps client abort wired through the upstream SSE response lifetime', async (t) => {
  const originalBackendUrl = process.env.BACKEND_API_BASE_URL;
  let resolveUpstreamClosed: (() => void) | undefined;
  const upstreamClosed = new Promise<void>((resolve) => { resolveUpstreamClosed = resolve; });
  const server = http.createServer((_req, res) => {
    res.on('close', () => resolveUpstreamClosed?.());
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    res.write('data: {"type":"content","content":"started"}\n\n');
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  t.after(() => {
    if (originalBackendUrl === undefined) delete process.env.BACKEND_API_BASE_URL;
    else process.env.BACKEND_API_BASE_URL = originalBackendUrl;
    server.close();
  });
  process.env.BACKEND_API_BASE_URL = `http://127.0.0.1:${address.port}`;

  const controller = new AbortController();
  const { POST } = await import('../app/api/backend/[...path]/route.ts');
  await POST(
    new Request('http://localhost/api/backend/v1/agent/chat', {
      method: 'POST',
      body: '{}',
      signal: controller.signal,
    }),
    { params: { path: ['v1', 'agent', 'chat'] } },
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  controller.abort();

  const closed = await Promise.race([
    upstreamClosed.then(() => true),
    new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 50)),
  ]);
  assert.equal(closed, true);
});
