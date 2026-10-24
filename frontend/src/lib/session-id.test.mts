import assert from 'node:assert/strict';
import test from 'node:test';

import { createAgentSessionId, normalizeAgentSessionId } from './session-id.ts';

test('createAgentSessionId emits an unprefixed canonical UUID from randomUUID', () => {
  const sessionId = createAgentSessionId({
    randomUUID: () => '123e4567-e89b-42d3-a456-426614174000',
  });

  assert.equal(sessionId, '123e4567-e89b-42d3-a456-426614174000');
});

test('createAgentSessionId works when crypto.randomUUID is unavailable', () => {
  const sessionId = createAgentSessionId({
    getRandomValues(values: Uint8Array) {
      for (let i = 0; i < values.length; i += 1) {
        values[i] = i;
      }
      return values;
    },
  });

  assert.match(sessionId, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

test('createAgentSessionId falls back when crypto is unavailable', () => {
  const sessionId = createAgentSessionId(null);

  assert.match(sessionId, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

test('normalizeAgentSessionId migrates only the historical prefixed UUID form', () => {
  assert.equal(
    normalizeAgentSessionId('agent-123e4567-e89b-42d3-a456-426614174000'),
    '123e4567-e89b-42d3-a456-426614174000',
  );
  assert.equal(normalizeAgentSessionId('agent-not-a-uuid'), 'agent-not-a-uuid');
  assert.equal(normalizeAgentSessionId('../outside'), '../outside');
});
