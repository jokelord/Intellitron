import * as http from 'node:http';
import * as https from 'node:https';
import { Readable } from 'node:stream';
import type { IncomingHttpHeaders } from 'node:http';
import {
  BackendProxyInvalidPathError,
  BackendProxyBodyTooLargeError,
  canonicalizeBackendProxyPath,
  getBackendProxyBodyLimit,
  readRequestBodyWithLimit,
} from '../../../../lib/backend-proxy-policy.ts';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type RouteContext = {
  params: {
    path?: string[];
  };
};

const DEFAULT_BACKEND_API_BASE_URL = 'http://127.0.0.1:8090';

function shouldLogProxySuccess(): boolean {
  return process.env.BACKEND_PROXY_LOG === '1';
}

function resolveBackendBaseUrl(): string {
  const candidates = [
    process.env.BACKEND_API_BASE_URL,
    process.env.NEXT_PUBLIC_API_BASE_URL,
    DEFAULT_BACKEND_API_BASE_URL,
  ];

  for (const candidate of candidates) {
    if (!candidate) continue;
    try {
      const url = new URL(candidate);
      if (url.protocol === 'http:' || url.protocol === 'https:') {
        return candidate.replace(/\/$/, '');
      }
    } catch {
      // Relative browser-only API bases such as /api/backend are not backend targets.
    }
  }

  return DEFAULT_BACKEND_API_BASE_URL;
}

function buildTargetUrl(request: Request, pathSegments: readonly string[]): string {
  const sourceUrl = new URL(request.url);
  const path = pathSegments.map(encodeURIComponent).join('/');
  const target = new URL(`${resolveBackendBaseUrl()}/${path}`);
  target.search = sourceUrl.search;
  return target.toString();
}

function buildForwardHeaders(request: Request): Headers {
  const headers = new Headers();
  const contentType = request.headers.get('content-type');
  const authorization = request.headers.get('authorization');

  if (contentType) headers.set('content-type', contentType);
  if (authorization) headers.set('authorization', authorization);

  return headers;
}

function buildResponseHeaders(response: Response): Headers {
  const headers = new Headers();
  for (const name of ['content-type', 'cache-control']) {
    const value = response.headers.get(name);
    if (value) headers.set(name, value);
  }
  return headers;
}

function buildResponseHeadersFromNode(headers: IncomingHttpHeaders): Headers {
  const responseHeaders = new Headers();
  for (const name of ['content-type', 'cache-control']) {
    const value = headers[name];
    if (Array.isArray(value)) {
      responseHeaders.set(name, value.join(', '));
    } else if (value) {
      responseHeaders.set(name, value);
    }
  }
  return responseHeaders;
}

function toNodeHeaders(headers: Headers): http.OutgoingHttpHeaders {
  const nodeHeaders: http.OutgoingHttpHeaders = {};
  headers.forEach((value, key) => {
    nodeHeaders[key] = value;
  });
  return nodeHeaders;
}

async function requestBackend(
  targetUrl: string,
  method: string,
  headers: Headers,
  body: ArrayBuffer | readonly Uint8Array[] | undefined,
  signal: AbortSignal,
): Promise<Response> {
  return new Promise((resolve, reject) => {
    const target = new URL(targetUrl);
    const transport = target.protocol === 'https:' ? https : http;
    const upstreamRequest = transport.request(
      target,
      {
        method,
        headers: toNodeHeaders(headers),
      },
      (upstreamResponse) => {
        const stream = Readable.toWeb(upstreamResponse) as ReadableStream<Uint8Array>;
        resolve(new Response(stream, {
          status: upstreamResponse.statusCode ?? 502,
          statusText: upstreamResponse.statusMessage,
          headers: buildResponseHeadersFromNode(upstreamResponse.headers),
        }));
      },
    );

    const abortUpstream = () => upstreamRequest.destroy(new Error('client aborted'));
    if (signal.aborted) {
      abortUpstream();
    } else {
      signal.addEventListener('abort', abortUpstream, { once: true });
      upstreamRequest.on('close', () => signal.removeEventListener('abort', abortUpstream));
    }

    upstreamRequest.setTimeout(0);
    upstreamRequest.on('error', reject);
    if (body === undefined) {
      upstreamRequest.end();
    } else if (body instanceof ArrayBuffer) {
      upstreamRequest.end(Buffer.from(body));
    } else {
      Readable.from(body).pipe(upstreamRequest);
    }
  });
}

async function proxy(request: Request, context: RouteContext): Promise<Response> {
  const method = request.method.toUpperCase();
  let path: string[];
  try {
    path = canonicalizeBackendProxyPath(context.params.path ?? []);
  } catch (error) {
    if (error instanceof BackendProxyInvalidPathError) {
      return Response.json({ error: 'invalid_backend_path' }, { status: 400 });
    }
    throw error;
  }
  const targetUrl = buildTargetUrl(request, path);
  const startedAt = Date.now();
  if (shouldLogProxySuccess()) {
    console.info(`[backend-proxy] -> ${method} ${targetUrl}`);
  }
  let body: ArrayBuffer | Uint8Array[] | undefined;
  if (method !== 'GET' && method !== 'HEAD') {
    const bodyLimit = getBackendProxyBodyLimit({
      method,
      path,
      contentType: request.headers.get('content-type'),
    });
    try {
      body = bodyLimit === null
        ? await request.arrayBuffer()
        : await readRequestBodyWithLimit(request, bodyLimit);
    } catch (error) {
      if (error instanceof BackendProxyBodyTooLargeError) {
        return Response.json(
          { error: 'payload_too_large', max_bytes: error.maxBytes },
          { status: 413 },
        );
      }
      throw error;
    }
  }
  let response: Response;

  try {
    response = await requestBackend(targetUrl, method, buildForwardHeaders(request), body, request.signal);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[backend-proxy] !! ${method} ${targetUrl} failed after ${Date.now() - startedAt}ms: ${message}`);
    return Response.json(
      { error: 'backend_proxy_failed', message },
      { status: 502 },
    );
  }

  if (shouldLogProxySuccess()) {
    console.info(`[backend-proxy] <- ${method} ${targetUrl} ${response.status} after ${Date.now() - startedAt}ms`);
  }

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: buildResponseHeaders(response),
  });
}

export async function GET(request: Request, context: RouteContext): Promise<Response> {
  return proxy(request, context);
}

export async function POST(request: Request, context: RouteContext): Promise<Response> {
  return proxy(request, context);
}

export async function PUT(request: Request, context: RouteContext): Promise<Response> {
  return proxy(request, context);
}

export async function PATCH(request: Request, context: RouteContext): Promise<Response> {
  return proxy(request, context);
}

export async function DELETE(request: Request, context: RouteContext): Promise<Response> {
  return proxy(request, context);
}
