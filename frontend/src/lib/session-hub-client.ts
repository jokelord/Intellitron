export interface SessionHubPayload {
  event_sequence: number;
  type: string;
  [key: string]: unknown;
}

const MAX_SESSION_HUB_PAYLOADS_PER_TASK = 32;

function yieldSessionHubBrowserTask(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

export interface SessionHubKnownRejection {
  status: number;
  detail: string;
  message: string;
  retryable: boolean;
  code?: string;
}

export interface SessionHubPayloadDispatchContext {
  matchesExpectedRequest: boolean;
}

export interface SessionHubPayloadHandlers {
  onPayload?: (
    payload: SessionHubPayload,
    context: SessionHubPayloadDispatchContext,
  ) => void | Promise<void>;
  onRequestAccepted?: (payload: SessionHubPayload) => void | Promise<void>;
  onKnownRejection?: (rejection: SessionHubKnownRejection) => void | Promise<void>;
}

export interface SessionHubReadInput {
  /** Recovery cursors are explicit. Live POST streams establish their base from the first record. */
  afterSequence?: number;
  expectedRequestId?: string;
  requestKind: 'Agent' | 'Chat';
  handlers: SessionHubPayloadHandlers;
  onActivity?: () => void;
  /** Answer Now must consume superseded and replacement generations together. */
  keepReadingAfterTerminal?: boolean;
}

export type SessionHubReadResult =
  | { kind: 'terminal'; lastSequence: number }
  | { kind: 'detached'; lastSequence: number; error?: Error }
  | { kind: 'rejected'; lastSequence: number; rejection: SessionHubKnownRejection };

export interface SessionAttachmentToken {
  readonly sessionId: string;
  readonly token: symbol;
}

export class SessionAttachmentRegistry {
  private current: SessionAttachmentToken | null = null;

  attach(sessionId: string): SessionAttachmentToken {
    if (typeof sessionId !== 'string' || sessionId.length === 0) {
      throw new TypeError('sessionId is required');
    }
    const attachment = Object.freeze({ sessionId, token: Symbol(sessionId) });
    this.current = attachment;
    return attachment;
  }

  isCurrent(attachment: SessionAttachmentToken): boolean {
    return this.current === attachment;
  }

  detach(attachment: SessionAttachmentToken): boolean {
    if (!this.isCurrent(attachment)) return false;
    this.current = null;
    return true;
  }

  getCurrent(): SessionAttachmentToken | null {
    return this.current;
  }
}

function stableJsonDetail(value: unknown): string | null {
  if (value === null || typeof value === 'boolean' || typeof value === 'number' || typeof value === 'string') {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    const entries = value.map(stableJsonDetail);
    return entries.some((entry) => entry === null) ? null : `[${entries.join(',')}]`;
  }
  if (typeof value === 'object') {
    const entries = Object.keys(value as Record<string, unknown>).sort().map((key) => {
      const serialized = stableJsonDetail((value as Record<string, unknown>)[key]);
      return serialized === null ? null : `${JSON.stringify(key)}:${serialized}`;
    });
    return entries.some((entry) => entry === null) ? null : `{${entries.join(',')}}`;
  }
  return null;
}

async function readKnownRequestRejection(
  response: Response,
  requestKind: 'Agent' | 'Chat',
): Promise<SessionHubKnownRejection> {
  let detail = `${requestKind} request rejected`;
  let code: string | undefined;
  try {
    const payload: unknown = await response.clone().json();
    if (typeof payload === 'object' && payload !== null && 'detail' in payload) {
      const rawDetail = (payload as Record<string, unknown>).detail;
      if (typeof rawDetail === 'object' && rawDetail !== null && !Array.isArray(rawDetail)) {
        const rawCode = (rawDetail as Record<string, unknown>).code;
        if (typeof rawCode === 'string' && rawCode.length > 0) code = rawCode;
      }
      detail = typeof rawDetail === 'string'
        ? rawDetail
        : stableJsonDetail(rawDetail) ?? detail;
    }
  } catch {
    // HTTP status is sufficient; never surface an HTML response body.
  }
  const retryable = response.status === 503
    || (response.status === 409 && /\bbusy\b/i.test(detail));
  return {
    status: response.status,
    detail,
    message: `${requestKind} request rejected (${response.status}): ${detail}`,
    retryable,
    ...(code === undefined ? {} : { code }),
  };
}

function requestMatch(
  payload: SessionHubPayload,
  expectedRequestId: string | undefined,
): boolean {
  if (!Object.prototype.hasOwnProperty.call(payload, 'request_id')) return true;
  if (typeof payload.request_id !== 'string' || payload.request_id.length === 0) {
    throw new TypeError('Session Hub request_id must be a non-empty string');
  }
  return expectedRequestId === undefined || payload.request_id === expectedRequestId;
}

export async function dispatchSessionHubPayload(
  payload: SessionHubPayload,
  handlers: SessionHubPayloadHandlers,
  expectedRequestId?: string,
): Promise<SessionHubPayloadDispatchContext> {
  const context = { matchesExpectedRequest: requestMatch(payload, expectedRequestId) };
  if (payload.type === 'request_accepted' && context.matchesExpectedRequest) {
    await handlers.onRequestAccepted?.(payload);
  }
  await handlers.onPayload?.(payload, context);
  return context;
}

function parseSseData(frame: string): string | null {
  const dataLines: string[] = [];
  for (const rawLine of frame.split(/\r?\n/)) {
    if (rawLine.startsWith(':')) continue;
    if (rawLine === 'data') dataLines.push('');
    else if (rawLine.startsWith('data:')) dataLines.push(rawLine.slice(5).replace(/^ /, ''));
  }
  return dataLines.length > 0 ? dataLines.join('\n') : null;
}

export async function readSessionHubStream(
  response: Response,
  input: SessionHubReadInput,
): Promise<SessionHubReadResult> {
  if (input.afterSequence !== undefined
    && (!Number.isInteger(input.afterSequence) || input.afterSequence < 0)) {
    throw new RangeError('afterSequence must be a non-negative integer');
  }
  let lastSequence = input.afterSequence ?? 0;
  let cursorEstablished = input.afterSequence !== undefined;

  if (!response.ok) {
    const rejection = await readKnownRequestRejection(response, input.requestKind);
    await input.handlers.onKnownRejection?.(rejection);
    return { kind: 'rejected', lastSequence, rejection };
  }
  if (!response.body) {
    return { kind: 'detached', lastSequence, error: new Error('Response body is empty') };
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let observedTerminal = false;
  let payloadsDispatchedInTask = 0;

  const consumeFrame = async (frame: string): Promise<boolean> => {
    const data = parseSseData(frame);
    if (data === null || data === '[DONE]') return false;
    const rawPayload: unknown = JSON.parse(data);
    if (typeof rawPayload !== 'object' || rawPayload === null || Array.isArray(rawPayload)) {
      throw new TypeError('Session Hub SSE payload must be a JSON object');
    }
    const payload = rawPayload as SessionHubPayload;
    if (!Number.isInteger(payload.event_sequence) || payload.event_sequence <= 0) {
      throw new TypeError('Session Hub event_sequence must be a positive integer');
    }
    if (!cursorEstablished) {
      lastSequence = payload.event_sequence - 1;
      cursorEstablished = true;
    }
    if (payload.event_sequence <= lastSequence) return false;
    if (payload.event_sequence !== lastSequence + 1) {
      throw new Error(
        `Session Hub event sequence gap: expected ${lastSequence + 1}, received ${payload.event_sequence}`,
      );
    }
    const context = await dispatchSessionHubPayload(
      payload,
      input.handlers,
      input.expectedRequestId,
    );
    lastSequence = payload.event_sequence;
    if (input.expectedRequestId === undefined) {
      observedTerminal = payload.type === 'done';
    } else if (context.matchesExpectedRequest && payload.type === 'done') {
      observedTerminal = true;
    } else if (context.matchesExpectedRequest && payload.type === 'request_accepted') {
      observedTerminal = false;
    }
    return true;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value.byteLength > 0) input.onActivity?.();
      buffer += decoder.decode(value, { stream: true });
      while (true) {
        const boundary = buffer.search(/\r?\n\r?\n/);
        if (boundary < 0) break;
        const match = buffer.slice(boundary).match(/^(?:\r?\n){2}/)?.[0] ?? '\n\n';
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + match.length);
        const dispatched = await consumeFrame(frame);
        if (observedTerminal && input.afterSequence === undefined && !input.keepReadingAfterTerminal) {
          try {
            await reader.cancel();
          } catch {
            // Stable terminal classification does not depend on cancellation.
          }
          return { kind: 'terminal', lastSequence };
        }
        if (dispatched) {
          payloadsDispatchedInTask += 1;
          if (payloadsDispatchedInTask >= MAX_SESSION_HUB_PAYLOADS_PER_TASK) {
            payloadsDispatchedInTask = 0;
            await yieldSessionHubBrowserTask();
          }
        }
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) await consumeFrame(buffer);
    if (observedTerminal && input.afterSequence === undefined && !input.keepReadingAfterTerminal) {
      return { kind: 'terminal', lastSequence };
    }
  } catch (error) {
    return {
      kind: 'detached',
      lastSequence,
      error: error instanceof Error ? error : new Error('Unknown Session Hub stream error'),
    };
  } finally {
    reader.releaseLock();
  }

  return observedTerminal
    ? { kind: 'terminal', lastSequence }
    : { kind: 'detached', lastSequence };
}
