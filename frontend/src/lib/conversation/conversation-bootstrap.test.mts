import test from 'node:test';
import assert from 'node:assert/strict';
import { loadConversationBootstrap, resolveConversationDefaults } from './conversation-bootstrap.ts';
import { DEFAULT_SETTINGS } from '../message-types.ts';

test('config initializes the application even when optional model discovery fails', async () => {
  const order: string[] = [];
  const config = { model: { model_name: 'configured' } };
  await loadConversationBootstrap({
    getConfig: async () => { order.push('config'); return config; },
    applyConfig: (value) => { assert.equal(value, config); order.push('applied'); },
    listModels: async () => { order.push('models'); throw new Error('provider does not list models'); },
    applyModels: () => { throw new Error('failed discovery must not apply'); },
    isCurrent: () => true,
  });
  assert.deepEqual(order, ['config', 'applied', 'models']);
});

test('stale config completion cannot update an unmounted owner', async () => {
  let resolve!: (value: {}) => void;
  let current = true;
  let writes = 0;
  const pending = loadConversationBootstrap({ getConfig: () => new Promise((r) => { resolve = r; }),
    applyConfig: () => { writes++; }, isCurrent: () => current });
  current = false; resolve({}); await pending;
  assert.equal(writes, 0);
});

test('cached key presence wins and later refresh retains live mode controls', () => {
  const defaults = { ...DEFAULT_SETTINGS, maxTokens: 1024 };
  const previous = { ...defaults, agentMode: true, enableThinking: true, enableLocalMemory: true };
  const cached = { temperature: 0, showReasoning: false, systemPrompt: '', maxTokens: 4096, agentMode: false, enableThinking: false, enableLocalMemory: false };
  const first = resolveConversationDefaults(defaults, previous, false, cached, null);
  assert.equal(first.temperature, 0); assert.equal(first.showReasoning, false); assert.equal(first.systemPrompt, '');
  assert.equal(first.maxTokens, 1024); assert.equal(first.agentMode, false);
  const refreshed = resolveConversationDefaults(defaults, previous, true, cached, null);
  assert.equal(refreshed.agentMode, true); assert.equal(refreshed.enableThinking, true); assert.equal(refreshed.enableLocalMemory, true);
});

test('unresolved optional model enrichment never holds the bootstrap poll open', async () => {
  let resolveModels!: (models: string[]) => void;
  let current = true;
  let applied = 0;
  const modelPromise = new Promise<string[]>(resolve => { resolveModels = resolve; });
  const bootstrap = loadConversationBootstrap({ getConfig: async () => ({}), applyConfig: () => {},
    listModels: () => modelPromise, applyModels: () => { applied++; }, isCurrent: () => current });
  const winner = await Promise.race([bootstrap.then(() => 'ready'), new Promise(resolve => setTimeout(() => resolve('blocked'), 20))]);
  assert.equal(winner, 'ready');
  current = false; resolveModels(['late']); await Promise.resolve(); await Promise.resolve();
  assert.equal(applied, 0);
});
