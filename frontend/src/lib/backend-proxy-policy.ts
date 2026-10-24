export const MAX_VISION_JSON_REQUEST_BODY_BYTES = 142_606_336;

export interface BackendProxyBodyPolicyInput {
  method: string;
  path: readonly string[];
  contentType: string | null;
}

export class BackendProxyBodyTooLargeError extends Error {
  readonly maxBytes: number;

  constructor(maxBytes: number) {
    super(`Request body exceeds ${maxBytes} bytes`);
    this.maxBytes = maxBytes;
  }
}

export class BackendProxyInvalidPathError extends Error {}

function fullyDecodePathSegment(segment: string): string {
  let decoded = segment;
  for (let attempt = 0; attempt < 16; attempt += 1) {
    let next: string;
    try {
      next = decodeURIComponent(decoded);
    } catch {
      throw new BackendProxyInvalidPathError("Backend proxy path contains invalid encoding");
    }
    if (next === decoded) return decoded;
    decoded = next;
  }
  throw new BackendProxyInvalidPathError("Backend proxy path encoding is nested too deeply");
}

export function canonicalizeBackendProxyPath(path: readonly string[]): string[] {
  return path.map((segment) => {
    const decoded = fullyDecodePathSegment(segment);
    if (!decoded || decoded === "." || decoded === ".." || /[\\/\0]/.test(decoded)) {
      throw new BackendProxyInvalidPathError("Backend proxy path contains an unsafe segment");
    }
    return decoded;
  });
}

const LARGE_VISION_JSON_PATHS = new Set([
  "v1/chat/completions",
  "v1/agent/chat",
  "v1/responses",
]);

export function getBackendProxyBodyLimit(
  input: BackendProxyBodyPolicyInput,
): number | null {
  if (input.method.toUpperCase() !== "POST") {
    return null;
  }
  return LARGE_VISION_JSON_PATHS.has(input.path.join("/"))
    ? MAX_VISION_JSON_REQUEST_BODY_BYTES
    : null;
}

function declaredContentLength(request: Request): bigint | null {
  const value = request.headers.get("content-length");
  if (value === null || !/^\d+$/.test(value.trim())) return null;
  return BigInt(value.trim());
}

export async function readRequestBodyWithLimit(
  request: Request,
  maxBytes: number,
): Promise<Uint8Array[]> {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 0) {
    throw new RangeError("maxBytes must be a non-negative safe integer");
  }
  const declared = declaredContentLength(request);
  if (declared !== null && declared > BigInt(maxBytes)) {
    throw new BackendProxyBodyTooLargeError(maxBytes);
  }
  if (request.body === null) return [];

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let completed = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        completed = true;
        break;
      }
      total += value.byteLength;
      if (total > maxBytes) throw new BackendProxyBodyTooLargeError(maxBytes);
      chunks.push(value);
    }
    return chunks;
  } finally {
    if (!completed) {
      try {
        await reader.cancel();
      } catch {
        // Preserve the original read/size error.
      }
    }
    reader.releaseLock();
  }
}
