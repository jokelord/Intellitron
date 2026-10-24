import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import {
  normalizeCycleConfig,
  resolveCycleEditorState,
  summarizeCycleProgress,
  validateScheduledMessages,
  type ScheduledMessage,
} from './cycle-types.ts';
import { parseCycleRecoveryError } from './api.ts';

const scheduled: ScheduledMessage = {
  id: 'schedule-1',
  time: '09:00:00',
  recurrence: 'daily',
  day_count: 3,
  message: 'report',
};

test('scheduled day-count preflight accepts below and equal limits and rejects above', () => {
  assert.equal(validateScheduledMessages([{ ...scheduled, day_count: 2 }], 'Asia/Shanghai', 3), null);
  assert.equal(validateScheduledMessages([{ ...scheduled, day_count: 3 }], 'Asia/Shanghai', 3), null);
  assert.match(validateScheduledMessages([{ ...scheduled, day_count: 4 }], 'Asia/Shanghai', 3) ?? '', /最多为 3 天/);
  assert.equal(validateScheduledMessages([{ ...scheduled, day_count: 31 }], 'Asia/Shanghai'), null);
});

test('scheduled time preflight rejects every hour outside 00 through 23', () => {
  assert.notEqual(validateScheduledMessages([{ ...scheduled, time: '24:00:00' }], 'Asia/Shanghai', 3), null);
  assert.notEqual(validateScheduledMessages([{ ...scheduled, time: '29:59:59' }], 'Asia/Shanghai', 3), null);
  assert.equal(validateScheduledMessages([{ ...scheduled, time: '23:59:59' }], 'Asia/Shanghai', 3), null);
});

test('cycle config normalization preserves backend values and supports older responses', () => {
  const normalized = normalizeCycleConfig({
    enabled: true,
    max_depth: 7,
    max_duration: 3600,
    show_thumbnails: false,
    default_sequence: [{ type: 'wait', wait: 1 }],
    timeout_policy: 'exempt_cycle_run',
    scheduled_track_max_duration_seconds: 2592000,
    max_scheduled_message_days: 12,
    event_journal_retention_seconds: 86400,
    event_journal_max_records: 20000,
    default_scheduled_messages: [scheduled],
  });
  assert.equal(normalized.max_scheduled_message_days, 12);
  assert.deepEqual(normalized.default_scheduled_messages, [scheduled]);
  assert.equal(normalizeCycleConfig({}).max_scheduled_message_days, undefined);
});

test('cycle editor migration keeps old saved schedules empty and uses configured defaults for a new editor', () => {
  const defaults = { steps: [{ type: 'wait', wait: 5 }], scheduledMessages: [scheduled], shareContext: true };
  assert.deepEqual(resolveCycleEditorState('{"steps":[{"type":"message","message":"old"}],"shareContext":false}', defaults.steps, defaults.scheduledMessages), {
    steps: [{ type: 'message', message: 'old' }], scheduledMessages: [], shareContext: false, showAgentSteps: false,
  });
  assert.deepEqual(resolveCycleEditorState('{"steps":[],"showAgentSteps":true}', defaults.steps, defaults.scheduledMessages).showAgentSteps, true);
  assert.deepEqual(resolveCycleEditorState(null, defaults.steps, defaults.scheduledMessages), { ...defaults, showAgentSteps: false });
});

test('cycle request/state visibility defaults to false and persists an explicit true choice', () => {
  assert.equal(resolveCycleEditorState('{"steps":[]}', [], []).showAgentSteps, false);
  assert.equal(resolveCycleEditorState('{"steps":[],"showAgentSteps":true}', [], []).showAgentSteps, true);
});

test('cycle progress counts unique deliveries, excludes queued lateness, and shows known next time', () => {
  const events = [
    { type: 'scheduled_message_queued', track: 'scheduled', schedule_id: 'schedule-1', occurrence_index: 1, scheduled_for: '2026-09-17T08:00:00Z', lateness_seconds: 20 },
    { type: 'step_begin', track: 'scheduled', schedule_id: 'schedule-1', occurrence_index: 1, scheduled_for: '2026-09-17T08:00:00Z', lateness_seconds: 20 },
    { type: 'step_end', track: 'scheduled', schedule_id: 'schedule-1', occurrence_index: 1, scheduled_for: '2026-09-17T08:00:00Z', lateness_seconds: 20 },
    { type: 'step_end', track: 'scheduled', schedule_id: 'schedule-1', occurrence_index: 1, scheduled_for: '2026-09-17T08:00:00Z', lateness_seconds: 20 },
    { type: 'step_end', track: 'ordinary' },
    { type: 'scheduled_message_failed', track: 'scheduled', schedule_id: 'schedule-1', occurrence_index: 2, error: 'camera' },
    { type: 'scheduler_update', next_scheduled_for: '2026-09-18T08:00:00Z' },
  ];
  assert.deepEqual(summarizeCycleProgress(events, new Date('2026-09-17T12:00:00Z')), {
    ordinaryCompleted: 1,
    scheduledQueued: 1,
    scheduledDelivered: 1,
    failed: 1,
    cumulativeLatenessSeconds: 20,
    nextScheduledFor: '2026-09-18T08:00:00Z',
    terminalReason: undefined,
  });
});

test('cycle recovery maps expired and unavailable backend codes to distinct non-retryable errors', () => {
  const expired = parseCycleRecoveryError(410, { detail: { code: 'cycle_run_expired' } });
  const unavailable = parseCycleRecoveryError(404, { detail: { code: 'cycle_run_unavailable' } });
  assert.equal(expired.code, 'expired');
  assert.equal(unavailable.code, 'unavailable');
  assert.notEqual(expired.message, unavailable.message);
  assert.equal(expired.retryable, false);
  assert.equal(unavailable.retryable, false);
  assert.equal(parseCycleRecoveryError(404, 'cycle_run_unavailable_or_expired').code, 'unavailable');
});

test('cycle controls use isolated readable theme-aware styling and visible focus rings', async () => {
  const component = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../components/CycleStepEditor.tsx'), 'utf8');
  const styles = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../app/globals.css'), 'utf8');
  assert.doesNotMatch(component, /bg-gray-700|text-white|border-gray-600/);
  assert.match(component, /cycle-input/);
  assert.match(component, /cycle-button/);
  assert.match(styles, /isolation:\s*isolate/);
  assert.match(styles, /color-scheme:\s*light/);
  assert.match(styles, /focus-visible/);
});

test('text and vision pages consume the complete Cycle config and pass scheduled defaults to the editor', async () => {
  const textPage = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../app/page.tsx'), 'utf8');
  const visionPage = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../app/vision/page.tsx'), 'utf8');
  for (const source of [textPage, visionPage]) {
    assert.match(source, /useState<CycleConfig>\(normalizeCycleConfig\(\)\)/);
    assert.match(source, /useConversationBootstrap\(\{[^;]*setCycleConfig/s);
    assert.match(source, /<ConversationTextInputActions[\s\S]*?cycleConfig=\{cycleConfig\}/);
  }
  const textActions = await readFile(new URL('../components/conversation/ConversationTextInputActions.tsx', import.meta.url), 'utf8');
  assert.match(textActions, /maxScheduledMessageDays=\{cycleConfig\.max_scheduled_message_days\}/);
  assert.match(textActions, /defaultScheduledMessages=\{cycleConfig\.default_scheduled_messages\}/);
  assert.match(visionPage, /cycleMaxScheduledMessageDays=\{cycleConfig\.max_scheduled_message_days\}/);
  assert.match(visionPage, /cycleDefaultScheduledMessages=\{cycleConfig\.default_scheduled_messages\}/);
  const bootstrap = await readFile(new URL('./conversation/use-conversation-bootstrap.ts', import.meta.url), 'utf8');
  assert.match(bootstrap, /setCycleConfig\(normalizeCycleConfig\(config\.cycle\)\)/);
});

test('both routes delegate Cycle projection and lifecycle to the single shared hook', async () => {
  const textPage = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../app/page.tsx'), 'utf8');
  const visionPage = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../app/vision/page.tsx'), 'utf8');
  for (const source of [textPage, visionPage]) {
    assert.match(source, /useConversationCycle\(\{/);
    assert.match(source, /(?:onStart|onCycleStart)=\{handleCycleStart\}/);
    assert.match(source, /(?:onStop|onCycleStop)=\{handleCycleStop\}/);
    assert.doesNotMatch(source, /const onCycleTimelineEvent\s*=/);
    assert.doesNotMatch(source, /createCycleAgentProjection\(/);
  }
});

test('Text and Vision route capability is explicit and Clear cancels only unsubmitted Cycle starts', async () => {
  const textPage = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../app/page.tsx'), 'utf8');
  const visionPage = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../app/vision/page.tsx'), 'utf8');
  assert.match(textPage, /useConversationCycle\(\{\s*pageMode: 'text'/);
  assert.match(visionPage, /useConversationCycle\(\{\s*pageMode: 'vision'/);
  assert.match(visionPage, /allowPhotos:\s*visionCapabilityConfirmed/);
  assert.doesNotMatch(visionPage, /isStartBlocked:\s*\(\)\s*=>\s*!visionCapabilityConfirmed/);
  for (const source of [textPage, visionPage]) {
    assert.match(source, /cancelPendingCycleStart:\s*cancelCycleStart/);
    assert.doesNotMatch(source, /resetCycle\(\{\s*clearBookmark:\s*true\s*\}\)/);
    assert.doesNotMatch(source, /const handleCycleStop\s*=/);
    assert.doesNotMatch(source, /const handleCycleStart\s*=/);
  }
});

test('only Vision supplies Cycle camera preparation; shared controller owns its start guard', async () => {
  const textPage = await readFile(new URL('../app/page.tsx', import.meta.url), 'utf8');
  const visionPage = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../app/vision/page.tsx'), 'utf8');
  assert.doesNotMatch(textPage, /beforeCameraStart:/);
  assert.match(visionPage, /beforeCameraStart: async signal/);
  assert.match(visionPage, /startGuardActive: cycleStartGuardRef\.current\.active/);
  assert.match(visionPage, /cancelCycleStart\(\)/);
  assert.doesNotMatch(visionPage, /new CycleStartGuard\(/);
});

test('Cycle editor persists the opt-in visibility switch and disables it during a run', async () => {
  const source = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../components/CycleEditor.tsx'), 'utf8');
  assert.match(source, /Show agent steps/);
  assert.match(source, /checked=\{showAgentSteps\}/);
  assert.match(source, /disabled=\{isRunning \|\| invalidStoredProgram\}/);
  assert.match(source, /showAgentSteps \}/);
});

test('text and vision Cycle start requests propagate the current thinking setting explicitly', async () => {
  const textPage = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../app/page.tsx'), 'utf8');
  const visionPage = await readFile(path.join(path.dirname(new URL(import.meta.url).pathname), '../app/vision/page.tsx'), 'utf8');
  for (const source of [textPage, visionPage]) {
    assert.match(source, /getRequestSettings:\s*\(\)\s*=>\s*\(\{[^}]*enable_thinking: settingsRef\.current\.enableThinking/s);
  }
});
