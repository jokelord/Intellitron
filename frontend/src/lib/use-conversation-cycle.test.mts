import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
const require = createRequire(import.meta.url);
const { createRenderer, installTask6TypeScriptHook } = require('./vision-automation/react-component-renderer.cjs');
installTask6TypeScriptHook(fileURLToPath(new URL('../../', import.meta.url)));
const React = require('react');
const { useConversationCycle } = require('./use-conversation-cycle.ts');
const { llmClient } = require('./api.ts');
const { selectCycleRecovery } = require('./cycle-page.ts');

test('server rendering the Cycle hook does not read browser storage or transport', () => {
  const { renderToString } = require('react-dom/server');
  let reads = 0;
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, get() { reads++; throw new Error('browser effect during SSR'); } });
  try {
    const Probe = () => { const cycle = useConversationCycle({ pageMode: 'text', showThumbnails: true, setMessages() {}, getRequestSettings: () => ({ agent_mode: true }) }); return React.createElement('div', null, cycle.cycleStatus); };
    assert.match(renderToString(React.createElement(Probe)), /idle/);
    assert.equal(reads, 0);
  } finally { if (previous) Object.defineProperty(globalThis, 'window', previous); else delete (globalThis as any).window; }
});

test('mounted hook reads fresh settings and detaches on unmount without stopping producer', async () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } };
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: storage } });
  const old = { start: llmClient.startCycle, stop: llmClient.stopCycle, status: llmClient.getCycleStatus };
  const requests: any[] = []; let stops = 0, current: any;
  const transport = new AbortController();
  llmClient.startCycle = async (request: unknown) => { requests.push(request); return transport; };
  llmClient.stopCycle = async () => { stops++; };
  llmClient.getCycleStatus = async () => { throw new Error('unexpected status request'); };
  const Probe = ({ thinking }: { thinking: boolean }) => { current = useConversationCycle({ pageMode: 'text', showThumbnails: true, setMessages() {}, getRequestSettings: () => ({ agent_mode: true, enable_thinking: thinking }) }); return React.createElement('div', null, current.cycleStatus); };
  const renderer = createRenderer(React.createElement(Probe, { thinking: false }));
  try {
    renderer.update(React.createElement(Probe, { thinking: true }));
    await current.handleCycleStart([{ type: 'message', message: 'hello' }], [], true, false);
    renderer.flush();
    assert.equal(requests[0].enable_thinking, true);
    assert.equal(current.cycleStatus, 'running');
    renderer.unmount();
    assert.equal(transport.signal.aborted, true);
    assert.equal(stops, 0);
    assert.equal(selectCycleRecovery(storage, 'text').kind, 'owned');
  } finally {
    renderer.unmount(); llmClient.startCycle = old.start; llmClient.stopCycle = old.stop; llmClient.getCycleStatus = old.status;
    if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow); else delete (globalThis as any).window;
  }
});

test('Text hook reports legacy recovery without a status request', () => {
  const values = new Map<string, string>([['cycle-active-run', JSON.stringify({ cycleRunId: 'legacy', fingerprint: 'f', cursor: 3 })]]);
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem() { throw new Error('foreign pointer rewrite'); }, removeItem() { throw new Error('foreign pointer removal'); } };
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: storage } });
  const old = llmClient.getCycleStatus; let status = 0, current: any;
  llmClient.getCycleStatus = async () => { status++; throw new Error('foreign status'); };
  const Probe = () => { current = useConversationCycle({ pageMode: 'text', showThumbnails: true, setMessages() {}, getRequestSettings: () => ({ agent_mode: true }) }); return React.createElement('div', null, current.cycleRecoveryNotice); };
  const renderer = createRenderer(React.createElement(Probe));
  try { assert.match(current.cycleRecoveryNotice, /Vision/); assert.equal(status, 0); }
  finally { renderer.unmount(); llmClient.getCycleStatus = old; if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow); else delete (globalThis as any).window; }
});

test('disabled browser storage leaves Cycle initialization usable', async () => {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { get localStorage() { throw new Error('storage disabled'); } } });
  let current: any;
  const Probe = () => { current = useConversationCycle({ pageMode: 'text', showThumbnails: false, setMessages() {}, getRequestSettings: () => ({ agent_mode: true }) }); return React.createElement('div', null, current.cycleStatus); };
  const renderer = createRenderer(React.createElement(Probe));
  try { await Promise.resolve(); assert.equal(current.cycleStatus, 'idle'); }
  finally { renderer.unmount(); if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow); else delete (globalThis as any).window; }
});

test('Vision hook follows current runtime photo capability without blocking ordinary Cycle starts', async () => {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  const values = new Map<string, string>();
  const storage = {getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key)};
  Object.defineProperty(globalThis, 'window', {configurable: true, value: {localStorage: storage}});
  const previousStart = llmClient.startCycle, previousStop = llmClient.stopCycle;
  const requests: any[] = []; let camera = 0, current: any;
  llmClient.startCycle = async (request: unknown) => {requests.push(request); return new AbortController();};
  llmClient.stopCycle = async () => undefined;
  const Probe = ({allowPhotos}: {allowPhotos: boolean}) => {
    current = useConversationCycle({pageMode: 'vision', allowPhotos, showThumbnails: true, setMessages() {}, getRequestSettings: () => ({agent_mode: true}), beforeCameraStart: async () => {camera++;}});
    return null;
  };
  const renderer = createRenderer(React.createElement(Probe, {allowPhotos: false}));
  const photo = [{type: 'message', message: 'look', photo: [0]}];
  try {
    await current.handleCycleStart(photo, [], true, false); renderer.flush();
    assert.equal(current.cycleStatus, 'idle'); assert.match(current.cycleValidationError, /steps\[0\].photo/);
    assert.equal(requests.length, 0); assert.equal(camera, 0);
    await current.handleCycleStart([{type: 'message', message: 'ordinary', photo: []}], [], true, false); renderer.flush();
    assert.equal(current.cycleStatus, 'running'); assert.equal(requests.length, 1); assert.equal(camera, 0);
    const owned = current.cycleSessionId;
    current.cancelCycleStart(); renderer.flush();
    assert.equal(current.cycleSessionId, owned); assert.equal(current.cycleStatus, 'running');
    await current.handleCycleStop(); renderer.flush();
    renderer.update(React.createElement(Probe, {allowPhotos: true}));
    await current.handleCycleStart(photo, [], true, false); renderer.flush();
    assert.equal(requests.length, 2); assert.equal(camera, 1); assert.deepEqual(requests[1].steps, photo);
    assert.equal('allowPhotos' in requests[1], false);
  } finally {
    renderer.unmount(); llmClient.startCycle = previousStart; llmClient.stopCycle = previousStop;
    if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow); else delete (globalThis as any).window;
  }
});
