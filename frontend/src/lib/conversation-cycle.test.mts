import assert from 'node:assert/strict';
import test from 'node:test';
import { createConversationCycleController, type ConversationCycleEnvironment } from './conversation-cycle.ts';
import { writeCycleBookmark, selectCycleRecovery, writeOwnedCycleBookmark } from './cycle-page.ts';
import type { Message } from './message-types.ts';
import type { CycleStartRequest } from './cycle-types.ts';
import { CycleRecoveryError } from './api.ts';

function harness(pageMode: 'text' | 'vision' = 'text') {
  const values = new Map<string, string>();
  const effects: string[] = [];
  const storage = { values, getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { effects.push('storage'); values.set(key, value); },
    removeItem: (key: string) => { effects.push('storage'); values.delete(key); } };
  let messages: Message[] = [], nextId = 0;
  let callbacks: { event: (event: any) => void; complete: () => void; error: (error: Error) => void } | undefined;
  let before: ((signal: AbortSignal) => Promise<void>) | undefined;
  let stop: (() => Promise<void>) | undefined;
  const requests: CycleStartRequest[] = [];
  const env: ConversationCycleEnvironment = {
    pageMode, storage: () => storage, now: () => new Date(0), createId: () => { effects.push('id'); return `id-${++nextId}`; },
    setMessages: (update) => { messages = update(messages); },
    getOptions: () => ({ showThumbnails: true, getRequestSettings: () => ({ agent_mode: true, enable_thinking: false, enable_local_memory: false }),
      beforeCameraStart: async (signal) => { effects.push('camera'); await before?.(signal); } }),
    client: {
      async startCycle(request, event, complete, error) { effects.push('post'); requests.push(request); callbacks = { event, complete, error }; return new AbortController(); },
      async getCycleStatus(id) { effects.push('status'); return { cycle_run_id: id, status: 'running', event_sequence: 0, request_fingerprint: 'f' }; },
      resumeCycle(_id, _fingerprint, _cursor, event, complete, error) { effects.push('resume'); callbacks = { event, complete, error }; return new AbortController(); },
      async stopCycle() { effects.push('stop'); await stop?.(); },
    },
  };
  const controller = createConversationCycleController(env);
  const states: unknown[] = []; controller.subscribe(() => { effects.push('state'); states.push(controller.getSnapshot()); });
  return { controller, storage, effects, requests, env, getMessages: () => messages, callbacks: () => callbacks!, setBefore: (value: typeof before) => { before = value; }, setStop: (value: typeof stop) => { stop = value; } };
}
const ordinary = [{ type: 'message' as const, message: 'hello' }];
const photo = [{ type: 'message' as const, message: 'look', photo: [0] }];

test('Text rejects all camera start paths before IDs, ownership, state, storage, hardware or network', async () => {
  const h = harness();
  const original = JSON.stringify(photo);
  await h.controller.start(photo, [], true, false);
  assert.match(h.controller.getSnapshot().validationError ?? '', /steps\[0\].photo/);
  assert.deepEqual(h.effects, ['state']); // Local validation feedback is the only permitted state change.
  assert.equal(h.controller.getSnapshot().status, 'idle');
  assert.equal(h.controller.startGuard.active, false);
  assert.equal(JSON.stringify(photo), original);
});

test('Vision without runtime photo capability rejects saved/default/programmatic photos before effects', async () => {
  const programs = [
    {steps: photo, schedules: [], path: 'steps[0].photo'},
    {steps: [{type: 'cycle', cycle_count: 2, cycle_steps: photo}], schedules: [], path: 'steps[0].cycle_steps[0].photo'},
    {steps: [], schedules: [{id: 'a', time: '09:00:00', recurrence: 'once', day_count: 1, message: '', photo: [0]}], path: 'scheduled_messages[0].photo'},
  ];
  for (const origin of ['saved', 'default', 'programmatic']) {
    for (const program of programs) {
      const h = harness('vision'), runtime = h.env.getOptions;
      h.env.getOptions = () => ({...runtime(), allowPhotos: false});
      const source = origin === 'saved' ? JSON.parse(JSON.stringify(program)) : program;
      const original = JSON.stringify(source);
      await h.controller.start(source.steps, source.schedules, true, false);
      assert.equal(h.controller.getSnapshot().status, 'idle', `${origin}: ${program.path}`);
      assert.ok(h.controller.getSnapshot().validationError?.includes(program.path));
      assert.match(h.controller.getSnapshot().validationError ?? '', /整次未启动；请使用支持视觉的模型/);
      assert.doesNotMatch(h.controller.getSnapshot().validationError ?? '', /Text|请在 Vision 打开/);
      assert.deepEqual(h.effects, ['state'], 'only local validation feedback may change');
      assert.equal(h.controller.startGuard.active, false);
      assert.equal(selectCycleRecovery(h.storage, 'vision').kind, 'none');
      assert.equal(JSON.stringify(source), original);
    }
  }
});

test('runtime photo capability cannot enable cameras on Text', async () => {
  const h = harness('text'), runtime = h.env.getOptions;
  h.env.getOptions = () => ({...runtime(), allowPhotos: true});
  await h.controller.start(photo, [], true, false);
  assert.equal(h.controller.getSnapshot().status, 'idle');
  assert.deepEqual(h.effects, ['state']);
});

test('Vision without photo capability still starts no-photo and empty-photo programs in both inference modes', async () => {
  for (const agentMode of [false, true]) {
    for (const steps of [ordinary, [{type: 'message' as const, message: 'hello', photo: []}]]) {
      const h = harness('vision'), runtime = h.env.getOptions;
      h.env.getOptions = () => ({...runtime(), allowPhotos: false, getRequestSettings: () => ({agent_mode: agentMode})});
      await h.controller.start(steps, [], true, false);
      assert.equal(h.controller.getSnapshot().status, 'running');
      assert.equal(h.requests.length, 1);
      assert.equal(h.requests[0].agent_mode, agentMode);
      assert.equal(h.effects.includes('camera'), false);
      assert.equal('allowPhotos' in h.requests[0], false);
      assert.equal('pageMode' in h.requests[0], false);
      assert.equal((selectCycleRecovery(h.storage, 'vision') as any).bookmark.pageMode, 'vision');
    }
  }
});

test('photo capability revocation during camera preparation cancels pending start before POST', async () => {
  const h = harness('vision'), runtime = h.env.getOptions;
  let allowPhotos = true, release!: () => void;
  h.env.getOptions = () => ({...runtime(), allowPhotos});
  h.setBefore(() => new Promise<void>(resolve => {release = resolve;}));
  const original = JSON.stringify(photo);
  const pending = h.controller.start(photo, [], true, false);
  assert.equal(h.effects.includes('camera'), true);
  allowPhotos = false; release(); await pending;
  assert.equal(h.requests.length, 0);
  assert.equal(h.controller.getSnapshot().status, 'idle');
  assert.ok(h.controller.getSnapshot().validationError?.includes('steps[0].photo'));
  assert.equal(selectCycleRecovery(h.storage, 'vision').kind, 'none');
  assert.equal(JSON.stringify(photo), original);
});

test('explicit Vision photo capability retains hardware preparation and the unchanged photo request', async () => {
  const h = harness('vision'), runtime = h.env.getOptions;
  h.env.getOptions = () => ({...runtime(), allowPhotos: true});
  await h.controller.start(photo, [], true, false);
  assert.equal(h.effects.includes('camera'), true);
  assert.deepEqual(h.requests[0].steps, photo);
  assert.equal('allowPhotos' in h.requests[0], false);
  assert.equal('pageMode' in h.requests[0], false);
});

test('cancel-pending-start action preserves every submitted Cycle owner until explicit Stop', async () => {
  const h = harness(); let transport: AbortController | undefined;
  const originalStart = h.env.client.startCycle;
  h.env.client.startCycle = async (...args) => {transport = await originalStart(...args); return transport;};
  await h.controller.start(ordinary, [], true, true);
  h.callbacks().event({type: 'cycle_started', cycle_run_id: 'id-1', event_sequence: 1, request_fingerprint: 'f'});
  const snapshot = h.controller.getSnapshot(), bookmark = [...h.storage.values], guardActive = h.controller.startGuard.active;
  h.effects.length = 0;
  h.controller.cancelStart();
  assert.equal(h.controller.getSnapshot(), snapshot);
  assert.deepEqual([...h.storage.values], bookmark);
  assert.equal(transport!.signal.aborted, false);
  assert.equal(h.controller.startGuard.active, guardActive);
  assert.deepEqual(h.effects, []);
  h.callbacks().event({type: 'step_begin', cycle_run_id: 'id-1', step_type: 'message', detail: {message: 'still owned'}, event_sequence: 2});
  assert.equal(h.getMessages()[0].content, 'still owned');
  await h.controller.stop();
  assert.equal(h.effects.includes('stop'), true);
  assert.equal(h.controller.getSnapshot().status, 'stopped');
});

test('cancel-pending-start preserves submitted request ownership even before the transport attaches', async () => {
  const h = harness(); let attach!: (controller: AbortController) => void;
  const transport = new AbortController();
  h.env.client.startCycle = async () => new Promise<AbortController>(resolve => {attach = resolve;});
  const starting = h.controller.start(ordinary, [], true, false);
  const snapshot = h.controller.getSnapshot(), bookmark = [...h.storage.values];
  h.controller.cancelStart();
  assert.equal(h.controller.getSnapshot(), snapshot);
  assert.deepEqual([...h.storage.values], bookmark);
  assert.equal(h.controller.startGuard.active, true);
  attach(transport); await starting;
  assert.equal(transport.signal.aborted, false);
  await h.controller.stop(); assert.equal(transport.signal.aborted, true);
});

test('invalid saved/default/programmatic schedules and cyclic structures have only local error effects', async () => {
  const cyclic: any = { type: 'cycle', cycle_count: 1, cycle_steps: [] }; cyclic.cycle_steps.push(cyclic);
  for (const [steps, schedules] of [
    [[{ type: 'cycle', cycle_count: 1, cycle_steps: photo }], []],
    [[], [{ id: 'a', time: '09:00:00', recurrence: 'once', day_count: 1, message: '', photo: [0] }]],
    [[cyclic], []], [[{ type: 'message', message: 'x', photo: ['0'] }], []],
    [null, []], [ordinary, null],
  ]) {
    const h = harness(); await h.controller.start(steps, schedules, true, false);
    assert.deepEqual(h.effects, ['state']); assert.equal(h.controller.getSnapshot().status, 'idle');
  }
});

test('ordinary start sends existing wire shape and scheduled events retain order and metadata', async () => {
  const h = harness(); await h.controller.start(ordinary, [], true, true);
  assert.equal(h.effects.includes('camera'), false);
  assert.equal(h.requests.length, 1);
  assert.equal('pageMode' in h.requests[0], false);
  assert.equal('version' in h.requests[0], false);
  h.callbacks().event({ type: 'cycle_started', cycle_run_id: 'id-1', show_agent_steps: true, request_fingerprint: 'f', event_sequence: 1 });
  h.callbacks().event({ type: 'step_begin', cycle_run_id: 'id-1', step_type: 'message', track: 'scheduled', schedule_id: 'a', occurrence_index: 1, scheduled_for: '09:00', detail: { message: 'check' }, event_sequence: 2 });
  h.callbacks().event({ type: 'llm_done', cycle_run_id: 'id-1', content: 'answer', event_sequence: 3 });
  assert.match(String(h.getMessages()[0].content), /定时 message/);
  assert.equal(h.getMessages()[1].content, 'answer');
  h.callbacks().event({ type: 'cycle_completed', cycle_run_id: 'id-1', event_sequence: 4 });
  assert.equal(h.controller.getSnapshot().status, 'completed');
  assert.equal(h.controller.startGuard.active, false);
  assert.equal(selectCycleRecovery(h.storage, 'text').kind, 'none');
});

test('Vision hardware prestart is photo-only and cancellation prevents delayed POST', async () => {
  const h = harness('vision'); let release!: () => void;
  h.setBefore(() => new Promise<void>((resolve) => { release = resolve; }));
  const starting = h.controller.start(photo, [], true, false);
  assert.equal(h.effects.includes('camera'), true);
  h.controller.cancelStart(); release(); await starting;
  assert.equal(h.requests.length, 0);
  assert.equal(h.controller.getSnapshot().status, 'idle');
  assert.equal(selectCycleRecovery(h.storage, 'vision').kind, 'none');
});

test('camera preparation rechecks fresh busy state and snapshots settings before awaiting', async () => {
  const h = harness('vision'); let release!: () => void, busy = false, enabled = false;
  h.env.getOptions = () => ({ showThumbnails: true, isStartBlocked: () => busy,
    getRequestSettings: () => ({ agent_mode: false, enable_thinking: enabled }),
    beforeCameraStart: () => new Promise<void>((resolve) => { release = resolve; }),
  });
  const pending = h.controller.start(photo, [], false, false);
  busy = true; enabled = true; release(); await pending;
  assert.equal(h.requests.length, 0); assert.equal(h.controller.getSnapshot().status, 'idle');
  busy = false;
  const next = h.controller.start(photo, [], false, false);
  enabled = false; release(); await next;
  assert.equal(h.requests[0].enable_thinking, true);
  assert.equal(h.requests[0].agent_mode, false); // Preserve the existing backend placeholder path.
  assert.equal(h.requests[0].share_context, false);
});

test('duplicate start and stale prestart failure cannot release a replacement generation', async () => {
  const h = harness('vision'); let reject!: (error: Error) => void;
  h.setBefore(() => new Promise<void>((_resolve, fail) => { reject = fail; }));
  const old = h.controller.start(photo, [], true, false);
  await h.controller.start(photo, [], true, false);
  assert.equal(h.effects.filter((effect) => effect === 'id').length, 1);
  h.controller.cancelStart(); await h.controller.start(ordinary, [], true, false);
  const replacement = h.controller.getSnapshot().sessionId;
  reject(new Error('late preparation failure')); await old;
  assert.equal(h.controller.getSnapshot().sessionId, replacement);
  assert.equal(h.controller.getSnapshot().status, 'running');
});

test('Text legacy/foreign recovery never polls, resumes or stops and leaves storage intact', async () => {
  const h = harness(); writeCycleBookmark(h.storage, { cycleRunId: 'legacy', fingerprint: 'f', cursor: 2 });
  const before = [...h.storage.values]; h.effects.length = 0;
  await h.controller.recover(); await h.controller.stop(); h.controller.reset({ clearBookmark: true });
  assert.equal(h.effects.some((effect) => ['status', 'resume', 'stop', 'storage'].includes(effect)), false);
  assert.deepEqual([...h.storage.values], before);
  assert.match(h.controller.getSnapshot().recoveryNotice ?? '', /Vision/);
});

test('Vision retains legacy pointer until matching replay confirms attachment, then migrates identity', async () => {
  const h = harness('vision'); writeCycleBookmark(h.storage, { cycleRunId: 'legacy', fingerprint: '', cursor: 9 });
  await h.controller.recover();
  assert.equal(h.effects.includes('resume'), true);
  assert.equal(h.storage.values.has('cycle-active-run'), true);
  h.callbacks().event({ type: 'cycle_started', cycle_run_id: 'legacy', request_fingerprint: 'f', event_sequence: 1 });
  assert.equal(h.storage.values.has('cycle-active-run'), false);
  const selection = selectCycleRecovery(h.storage, 'vision');
  assert.equal(selection.kind, 'owned');
  if (selection.kind === 'owned') assert.deepEqual(selection.bookmark, { version: 2, pageMode: 'vision', cycleRunId: 'legacy', fingerprint: 'f', cursor: 9 });
});

test('status success followed by attach rejection preserves the unconfirmed legacy pointer', async () => {
  const h = harness('vision'); const old = { cycleRunId: 'legacy', fingerprint: '', cursor: 9 };
  writeCycleBookmark(h.storage, old);
  await h.controller.recover(); h.callbacks().error(new Error('attach failed'));
  assert.deepEqual(JSON.parse(h.storage.getItem('cycle-active-run')!), old);
  assert.equal(selectCycleRecovery(h.storage, 'vision').kind, 'legacy-vision');
});

test('stale Stop completion cannot clear newer run or bookmark; late events cannot cross owner', async () => {
  const h = harness(); await h.controller.start(ordinary, [], true, false);
  const old = h.callbacks(); let release!: () => void;
  h.setStop(() => new Promise<void>((resolve) => { release = resolve; }));
  const stopping = h.controller.stop();
  await h.controller.start(ordinary, [], true, false);
  const replacement = h.controller.getSnapshot().sessionId;
  release(); await stopping;
  old.event({ type: 'cycle_completed', cycle_run_id: 'id-1', event_sequence: 5 });
  assert.equal(h.controller.getSnapshot().sessionId, replacement);
  assert.equal(h.controller.getSnapshot().status, 'running');
  assert.equal((selectCycleRecovery(h.storage, 'text') as any).bookmark.cycleRunId, replacement);
});

test('owned Text photo events report capability anomaly without projecting images or stopping producer', async () => {
  const h = harness(); await h.controller.start(ordinary, [], true, false);
  h.callbacks().event({ type: 'step_begin', cycle_run_id: 'id-1', step_type: 'message', detail: { message: 'hello' }, event_sequence: 1 });
  h.callbacks().event({ type: 'photo_captured', cycle_run_id: 'id-1', image_base64: 'secret-image', event_sequence: 2 });
  assert.deepEqual(h.getMessages()[0].images, []);
  assert.match(h.controller.getSnapshot().validationError ?? '', /photo/);
  assert.equal(h.effects.includes('stop'), false);
});

test('detach only aborts subscriber and preserves owned bookmark for recovery', async () => {
  const h = harness(); await h.controller.start(ordinary, [], true, false);
  h.controller.detach();
  assert.equal(h.effects.includes('stop'), false);
  assert.equal(selectCycleRecovery(h.storage, 'text').kind, 'owned');
});

test('unknown EOF retains running ownership and recoverability until canonical terminal', async () => {
  const h = harness(); await h.controller.start(ordinary, [], true, false);
  h.callbacks().complete();
  assert.equal(h.controller.getSnapshot().status, 'running');
  assert.equal(selectCycleRecovery(h.storage, 'text').kind, 'owned');
  await h.controller.start(ordinary, [], true, false); assert.equal(h.requests.length, 1);
});

test('nonretryable expired recovery clears only owned pointer while retryable errors retain it', async () => {
  for (const retryable of [false, true]) {
    const h = harness();
    writeOwnedCycleBookmark(h.storage, { version: 2, pageMode: 'text', cycleRunId: 'old', fingerprint: 'f', cursor: 5 });
    writeOwnedCycleBookmark(h.storage, { version: 2, pageMode: 'vision', cycleRunId: 'foreign', fingerprint: 'v', cursor: 2 });
    h.env.client.getCycleStatus = async () => { throw new CycleRecoveryError('expired', 'expired', 410, retryable); };
    await h.controller.recover();
    assert.equal(h.controller.getSnapshot().status, 'expired');
    const text = selectCycleRecovery(h.storage, 'text');
    assert.equal(text.kind, retryable ? 'owned' : 'foreign-legacy');
    assert.equal((selectCycleRecovery(h.storage, 'vision') as any).bookmark.cycleRunId, 'foreign');
  }
});

test('recovery enforces fingerprint identity and ignores foreign/duplicate replay events', async () => {
  const h = harness(); await h.controller.start(ordinary, [], true, false);
  h.callbacks().event({ type: 'step_begin', cycle_run_id: 'foreign', step_type: 'message', detail: { message: 'bad' }, event_sequence: 1 });
  assert.equal(h.getMessages().length, 0);
  const valid = { type: 'step_begin', cycle_run_id: 'id-1', step_type: 'message', detail: { message: 'good' }, event_sequence: 2 };
  h.callbacks().event(valid); h.callbacks().event(valid);
  assert.equal(h.getMessages().length, 1);
  h.controller.detach();
  h.env.client.getCycleStatus = async (id) => ({ cycle_run_id: id, status: 'running', event_sequence: 3, request_fingerprint: 'different' });
  // Persist an actual known server fingerprint before exercising conflict handling.
  writeOwnedCycleBookmark(h.storage, { version: 2, pageMode: 'text', cycleRunId: 'id-1', fingerprint: 'known', cursor: 2 });
  await h.controller.recover();
  assert.equal(h.effects.includes('resume'), false);
  assert.equal(h.controller.getSnapshot().status, 'error');
});

test('fresh recovery ownership supersedes status promises from detached generations', async () => {
  const h = harness(); writeOwnedCycleBookmark(h.storage, { version: 2, pageMode: 'text', cycleRunId: 'old', fingerprint: 'f', cursor: 0 });
  let resolve!: (value: any) => void;
  h.env.client.getCycleStatus = () => new Promise((done) => { resolve = done; });
  const recovery = h.controller.recover(); h.controller.detach();
  await h.controller.start(ordinary, [], true, false);
  resolve({ cycle_run_id: 'old', request_fingerprint: 'f', status: 'running', event_sequence: 0 }); await recovery;
  assert.equal(h.effects.includes('resume'), false);
  assert.equal(h.controller.getSnapshot().sessionId, 'id-1');
});
