import assert from 'node:assert/strict';
import test from 'node:test';
import { validateCycleProgram } from './cycle-capability.ts';
import { selectCycleRecovery, writeOwnedCycleBookmark, clearOwnedCycleBookmark, migrateConfirmedVisionCycleBookmark, writeCycleBookmark } from './cycle-page.ts';

export function memoryStorage() {
  const values = new Map<string, string>();
  return { values, getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); } };
}
const scheduled = { id: 'a', time: '09:00:00', recurrence: 'once', day_count: 1, message: '', photo: [0] };

test('Text rejects ordinary, nested and scheduled photos with exact paths without mutation', () => {
  for (const [steps, schedules, path] of [
    [[{ type: 'message', message: 'x', photo: [0] }], [], 'steps[0].photo'],
    [[{ type: 'cycle', cycle_count: 2, cycle_steps: [{ type: 'message', message: 'x', photo: [1] }] }], [], 'steps[0].cycle_steps[0].photo'],
    [[], [scheduled], 'scheduled_messages[0].photo'],
  ] as const) {
    const original = JSON.stringify([steps, schedules]);
    const result = validateCycleProgram('text', steps, schedules);
    assert.equal(result.ok, false);
    if (!result.ok) { assert.equal(result.error.path, path); assert.equal(result.error.code, 'text_camera_forbidden'); }
    assert.equal(JSON.stringify([steps, schedules]), original);
    assert.equal(validateCycleProgram('vision', steps, schedules).ok, true);
  }
});

test('empty photos are harmless and accepted programs are independently snapshotted', () => {
  const steps = [{ type: 'message', message: 'hello', photo: [] as number[] }];
  const result = validateCycleProgram('text', steps, []);
  assert.equal(result.ok, true);
  if (result.ok) { assert.equal(result.hasPhotos, false); steps[0].message = 'changed'; assert.equal(result.program.steps[0].type === 'message' && result.program.steps[0].message, 'hello'); }
});

test('malformed, cyclic and sparse programs fail closed with paths', () => {
  const cyclic: any = { type: 'cycle', cycle_count: 1, cycle_steps: [] }; cyclic.cycle_steps.push(cyclic);
  for (const [steps, schedules] of [
    [null, []], [[], {}], [[null], []], [[{ type: 'unknown' }], []],
    [[{ type: 'cycle', cycle_count: 1, cycle_steps: {} }], []],
    [[{ type: 'message', message: 'x', photo: '0' }], []],
    [[{ type: 'message', message: 'x', photo: [-1] }], []],
    [[{ type: 'wait', wait: Infinity }], []], [[cyclic], []],
    [new Array(1), []], [[], [{ ...scheduled, recurrence: 'never' }]],
  ]) {
    const result = validateCycleProgram('vision', steps, schedules);
    assert.equal(result.ok, false);
    if (!result.ok) { assert.equal(result.error.code, 'invalid_program'); assert.match(result.error.path, /^(steps|scheduled_messages)/); }
  }
});

test('owned bookmarks isolate pages and Text never adopts legacy recovery', () => {
  const storage = memoryStorage();
  writeCycleBookmark(storage, { cycleRunId: 'legacy', fingerprint: 'old', cursor: 4 });
  assert.equal(selectCycleRecovery(storage, 'text').kind, 'foreign-legacy');
  assert.equal(selectCycleRecovery(storage, 'vision').kind, 'legacy-vision');
  const vision = { version: 2 as const, pageMode: 'vision' as const, cycleRunId: 'vision', fingerprint: 'v', cursor: 8 };
  writeOwnedCycleBookmark(storage, vision);
  clearOwnedCycleBookmark(storage, 'text', 'vision');
  assert.deepEqual(selectCycleRecovery(storage, 'vision'), { kind: 'owned', bookmark: vision });
  assert.equal(storage.values.has('cycle-active-run'), true);
});

test('bookmark writes keep monotonic cursors and stale clears cannot erase replacement', () => {
  const storage = memoryStorage();
  const bookmark = { version: 2 as const, pageMode: 'text' as const, cycleRunId: 'a', fingerprint: 'f', cursor: 8 };
  writeOwnedCycleBookmark(storage, bookmark);
  writeOwnedCycleBookmark(storage, { ...bookmark, cursor: 2 });
  assert.equal((selectCycleRecovery(storage, 'text') as any).bookmark.cursor, 8);
  writeOwnedCycleBookmark(storage, { ...bookmark, cycleRunId: 'b', cursor: 0 });
  clearOwnedCycleBookmark(storage, 'text', 'a');
  assert.equal((selectCycleRecovery(storage, 'text') as any).bookmark.cycleRunId, 'b');
});

test('confirmed legacy Vision migration preserves identity and replacement legacy pointers', () => {
  const storage = memoryStorage();
  const old = { cycleRunId: 'legacy', fingerprint: '', cursor: 4 };
  writeCycleBookmark(storage, old);
  migrateConfirmedVisionCycleBookmark(storage, old, { ...old, fingerprint: 'confirmed' });
  assert.equal(storage.values.has('cycle-active-run'), false);
  assert.equal((selectCycleRecovery(storage, 'vision') as any).bookmark.fingerprint, 'confirmed');
  writeCycleBookmark(storage, { ...old, cycleRunId: 'replacement' });
  migrateConfirmedVisionCycleBookmark(storage, old, { ...old, fingerprint: 'confirmed' });
  assert.equal(JSON.parse(storage.getItem('cycle-active-run')!).cycleRunId, 'replacement');
});

test('malformed owned and whitespace legacy pointers cannot become recovery owners', () => {
  const storage = memoryStorage();
  for (const value of [
    { version: 2, pageMode: 'vision', cycleRunId: 'a', fingerprint: 'f', cursor: 0 },
    { version: 2, pageMode: 'text', cycleRunId: 'a', fingerprint: 'f', cursor: -1 },
    { version: 99, pageMode: 'text', cycleRunId: 'a', fingerprint: 'f', cursor: 0 },
  ]) {
    storage.values.set('cycle-active-run:v2:text', JSON.stringify(value));
    assert.equal(selectCycleRecovery(storage, 'text').kind, 'none');
  }
  writeCycleBookmark(storage, { cycleRunId: '   ', fingerprint: 'f', cursor: 0 });
  assert.equal(selectCycleRecovery(storage, 'vision').kind, 'none');
});

test('failed owned migration write does not delete a legacy recovery pointer', () => {
  const storage = memoryStorage(); const old = { cycleRunId: 'a', fingerprint: '', cursor: 8 };
  writeCycleBookmark(storage, old);
  storage.values.set('cycle-active-run:v2:vision', JSON.stringify({ ...old, version: 2, pageMode: 'vision' }));
  storage.setItem = () => { throw new Error('storage full'); };
  migrateConfirmedVisionCycleBookmark(storage, old, { ...old, fingerprint: 'confirmed' });
  assert.deepEqual(JSON.parse(storage.getItem('cycle-active-run')!), old);
});
