type RandomSource = {
  randomUUID?: () => string;
  getRandomValues?: (values: Uint8Array) => Uint8Array;
};

const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const LEGACY_AGENT_UUID = /^agent-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/;

function bytesToUuid(bytes: Uint8Array): string {
  const copy = new Uint8Array(bytes);
  copy[6] = (copy[6] & 0x0f) | 0x40;
  copy[8] = (copy[8] & 0x3f) | 0x80;

  const hex = Array.from(copy, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function createAgentSessionId(randomSource: RandomSource | null | undefined = globalThis.crypto): string {
  if (typeof randomSource?.randomUUID === 'function') {
    const generated = randomSource.randomUUID().toLowerCase();
    if (CANONICAL_UUID.test(generated)) return generated;
  }

  if (typeof randomSource?.getRandomValues === 'function') {
    const bytes = randomSource.getRandomValues(new Uint8Array(16));
    return bytesToUuid(bytes);
  }

  // Last-resort compatibility for browsers without Web Crypto.  The shape
  // remains canonical so a fresh agent session is accepted by the backend.
  const bytes = new Uint8Array(16);
  let seed = Date.now() >>> 0;
  for (let index = 0; index < bytes.length; index += 1) {
    seed = (seed * 1664525 + 1013904223 + Math.floor(Math.random() * 0x100000000)) >>> 0;
    bytes[index] = seed & 0xff;
  }
  return bytesToUuid(bytes);
}

/** Normalize only the historical, strictly UUID-shaped agent prefix form. */
export function normalizeAgentSessionId(sessionId: string): string {
  const match = LEGACY_AGENT_UUID.exec(sessionId);
  return match?.[1] ?? sessionId;
}
