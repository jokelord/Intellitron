import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
const require = createRequire(import.meta.url);
const { createRenderer, installTask6TypeScriptHook } = require('./vision-automation/react-component-renderer.cjs');
installTask6TypeScriptHook(fileURLToPath(new URL('../../', import.meta.url)));
const React = require('react');
const CycleEditor = require('../components/CycleEditor.tsx').default;

function renderEditor(options: { vision?: boolean; saved?: unknown; steps?: unknown[]; schedules?: unknown[]; storageDisabled?: boolean }) {
  const values = new Map<string, string>();
  if (options.saved) values.set('cycle-editor-state', JSON.stringify(options.saved));
  let writes = 0;
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { writes++; values.set(key, value); },
  };
  Object.defineProperty(globalThis, 'localStorage', options.storageDisabled
    ? { configurable: true, get() { throw new Error('storage disabled'); } }
    : { configurable: true, value: storage });
  const calls: unknown[][] = [];
  const renderer = createRenderer(React.createElement(CycleEditor, {
    isVisionMode: options.vision ?? false, maxDepth: 5, defaultSteps: options.steps ?? [], defaultScheduledMessages: options.schedules ?? [],
    executionStatus: 'idle', onStart: (...args: unknown[]) => { calls.push(args); }, onStop() {},
  }));
  return { renderer, values, calls, writes: () => writes,
    start() { renderer.click(renderer.findOne((node: any) => node.type === 'button' && renderer.text(node) === 'Start')); },
    text: () => renderer.container.children.map(renderer.text).join(''),
    close() { renderer.unmount(); if (previous) Object.defineProperty(globalThis, 'localStorage', previous); else delete (globalThis as any).localStorage; },
  };
}

for (const source of ['default', 'saved'] as const) {
  test(`Text editor rejects ${source} nested camera program before start/timer and preserves storage`, () => {
    const steps = [{ type: 'cycle', cycle_count: 2, cycle_steps: [{ type: 'message', message: 'inspect', photo: [0] }] }];
    const h = renderEditor(source === 'saved' ? { saved: { steps } } : { steps });
    const before = [...h.values], writes = h.writes();
    let timers = 0;
    const oldTimer = globalThis.setTimeout;
    globalThis.setTimeout = (() => { timers++; return 0; }) as any;
    try {
      h.start();
      assert.equal(h.calls.length, 0);
      assert.equal(timers, 0);
      assert.match(h.text(), /steps\[0\].cycle_steps\[0\].photo/);
      assert.doesNotMatch(h.text(), /will be skipped/);
      assert.deepEqual([...h.values], before);
      assert.equal(h.writes(), writes);
    } finally { globalThis.setTimeout = oldTimer; h.close(); }
  });
}

test('Text editor rejects photo-only scheduled default; Vision retains photo start', async () => {
  const schedules = [{ id: 'a', time: '09:00:00', recurrence: 'once', day_count: 1, message: '', photo: [1] }];
  const text = renderEditor({ schedules });
  try { text.start(); assert.equal(text.calls.length, 0); assert.match(text.text(), /scheduled_messages\[0\].photo/); }
  finally { text.close(); }
  const vision = renderEditor({ vision: true, schedules });
  try { vision.start(); assert.equal(vision.calls.length, 1); assert.deepEqual(vision.calls[0][1], schedules); await Promise.resolve(); }
  finally { vision.close(); }
});

test('malformed saved containers remain intact and report an error without render crash', () => {
  const saved = { steps: [{ type: 'cycle', cycle_count: 1, cycle_steps: 'invalid' }] };
  const h = renderEditor({ saved });
  try { assert.match(h.text(), /cycle_steps/); assert.deepEqual(JSON.parse(h.values.get('cycle-editor-state')!), saved); assert.equal(h.writes(), 0); }
  finally { h.close(); }
});

test('opening a saved draft does not rewrite it and explicit edits persist the changed program', () => {
  const saved = { steps: [{ type: 'message', message: 'draft', photo: [0] }], scheduledMessages: [{ id: 'a', time: '09:00:00', recurrence: 'once', day_count: 1, message: '' }] };
  const h = renderEditor({ vision: true, saved });
  try {
    assert.equal(h.writes(), 0);
    assert.deepEqual(JSON.parse(h.values.get('cycle-editor-state')!), saved);
    h.renderer.act(() => h.renderer.findOne((node: any) => node.type === 'textarea' && node.props.value === 'draft').props.onChange({ target: { value: 'changed' } }));
    assert.equal(h.writes(), 1);
    const persisted = JSON.parse(h.values.get('cycle-editor-state')!);
    assert.equal(persisted.steps[0].message, 'changed');
    assert.deepEqual(persisted.steps[0].photo, [0]);
  } finally { h.close(); }
});

test('unavailable optional storage falls back to configured editable defaults', async () => {
  const h = renderEditor({ storageDisabled: true, steps: [{ type: 'message', message: 'default' }] });
  try { h.start(); assert.equal(h.calls.length, 1); await Promise.resolve(); }
  finally { h.close(); }
});
