import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { freezeLocalMemoryPreference } from './local-memory.ts';
import { DEFAULT_SETTINGS } from './message-types.ts';
import { llmClient, mergeServerDefaultsWithCache } from './api.ts';

test('local memory defaults off and cached false survives server default merging', () => {
  assert.equal(DEFAULT_SETTINGS.enableLocalMemory, false);
  assert.equal(mergeServerDefaultsWithCache({ enableLocalMemory: true }, { enableLocalMemory: false }).enableLocalMemory, false);
  assert.equal(mergeServerDefaultsWithCache({ enableLocalMemory: false }, { enableLocalMemory: true }).enableLocalMemory, true);
});

for (const enabled of [undefined, false, true]) {
  test(`Agent POST explicitly sends local memory ${enabled ?? 'default false'}`, async (t) => {
    const original = globalThis.fetch;
    t.after(() => { globalThis.fetch = original; });
    let body: any;
    globalThis.fetch = async (_url, init) => {
      body = JSON.parse(String(init?.body));
      return new Response('data: {"event_sequence":1,"type":"done"}\n\n', { status: 200 });
    };
    await llmClient.agentChatStream({ message: 'raw message', enable_local_memory: enabled }, () => {}, () => {});
    assert.equal(body.enable_local_memory, enabled === true);
    assert.equal(body.message, 'raw message');
  });
}

test('both pages use the shared Chat/Agent memory toggle and sample each actual POST', async () => {
  const header = await readFile(new URL('../components/conversation/ConversationHeader.tsx', import.meta.url), 'utf8');
  const transport = await readFile(new URL('./conversation/conversation-transport.ts', import.meta.url), 'utf8');
  const defaults = await readFile(new URL('./conversation/conversation-bootstrap.ts', import.meta.url), 'utf8');
  assert.match(header, /<LocalMemoryToggle/);
  assert.doesNotMatch(header, /<LocalMemoryToggle[^>]*agentMode=/);
  assert.match(transport, /freezeLocalMemoryPreference\(/);
  assert.match(defaults, /enableLocalMemory: loaded \? previous\.enableLocalMemory : base\.enableLocalMemory/);
  for (const page of ['app/page.tsx', 'app/vision/page.tsx']) {
    const source = await readFile(new URL(`../${page}`, import.meta.url), 'utf8');
    assert.match(source, /<ConversationHeader[^\n]*enableLocalMemory=\{settings\.enableLocalMemory\}/);
    assert.match(source, /runAgentConversationTransport\(/);
    assert.match(source, /runChatConversationTransport\(/);
    assert.match(source, /localMemoryEnabled: settingsRef\.current\.enableLocalMemory/);
    assert.match(source, /enable_local_memory: settingsRef\.current\.enableLocalMemory/);
  }
});

test('Cycle start retries keep the initially frozen local memory flag', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const bodies: any[] = [];
  const request = { agent_mode: true, share_context: true, steps: [], enable_local_memory: false };
  globalThis.fetch = async (_url, init) => {
    bodies.push(JSON.parse(String(init?.body)));
    if (bodies.length === 1) {
      request.enable_local_memory = true;
      throw new Error('retry transport');
    }
    return new Response('data: {"type":"cycle_completed"}\n\n', { status: 200 });
  };
  await new Promise<void>((resolve, reject) => {
    void llmClient.startCycle(request, () => {}, resolve, reject);
  });
  assert.deepEqual(bodies.map((body) => body.enable_local_memory), [false, false]);
});


test('deferred sends read the current preference, while same-ID retries retain their first POST flag', () => {
  const frozen = new Map<string, boolean>();
  let currentPreference = false;
  const queuedSend = () => freezeLocalMemoryPreference(frozen, 'queue-item-1', currentPreference);
  currentPreference = true;
  assert.equal(queuedSend(), true);
  currentPreference = false;
  assert.equal(queuedSend(), true);
  assert.equal(freezeLocalMemoryPreference(frozen, 'next-request', currentPreference), false);
});


test('definitive terminals and context clear release retained preference snapshots', async () => {
  const actions = await readFile(new URL('./conversation/conversation-actions.ts', import.meta.url), 'utf8');
  assert.match(actions, /localMemoryRequestsRef\.current\.clear\(\)/);
  for (const page of ['app/page.tsx', 'app/vision/page.tsx']) {
    const source = await readFile(new URL(`../${page}`, import.meta.url), 'utf8');
    assert.equal(source.includes('localMemoryRequestsRef.current.delete('), true, page);
    assert.match(source, /clearConversation\(/);
  }
});

for (const [rawUserText, wireText] of [
  ['', '(图片已附加，请分析)'],
  ['   ', '(图片已附加，请分析)'],
  ['', '[Vision automation capture manifest: camera_1]'],
  ['请识别图片中的部署版本', '请识别图片中的部署版本'],
]) {
  test(`Vision request memory eligibility follows original user text ${JSON.stringify(rawUserText)} with wire ${JSON.stringify(wireText)}`, async (t) => {
    const original = globalThis.fetch;
    t.after(() => { globalThis.fetch = original; });
    const preference = true;
    const frozen = new Map<string, boolean>();
    const hasUserText = rawUserText.trim().length > 0;
    let body: any;
    globalThis.fetch = async (_url, init) => {
      body = JSON.parse(String(init?.body));
      return new Response('data: {"event_sequence":1,"type":"done"}\n\n', { status: 200 });
    };
    await llmClient.agentChatStream({
      message: wireText,
      images: ['data:image/jpeg;base64,AAAA'],
      enable_local_memory: freezeLocalMemoryPreference(frozen, 'vision-memory-request', preference, hasUserText),
    }, () => {}, () => {});
    assert.equal(body.enable_local_memory, hasUserText);
    assert.equal(body.message, wireText);
    assert.deepEqual(body.images, ['data:image/jpeg;base64,AAAA']);
    assert.equal(preference, true, 'image-only eligibility does not mutate the saved preference');
    assert.equal(freezeLocalMemoryPreference(frozen, 'vision-memory-request', true, true), hasUserText,
      'same-ID retry retains the original frozen eligibility');
  });
}
