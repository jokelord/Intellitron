import assert from 'node:assert/strict';
import test from 'node:test';
import { classifyCycleEvent, createCycleEventGuard, readCycleBookmark, updateCycleVisibility, writeCycleBookmark } from './cycle-page.ts';
import { createCycleAgentProjection } from './cycle-agent-projection.ts';
import { getAssistantRenderableText } from './answer-now-ui.ts';
import type { Message } from './message-types.ts';

test('scheduled dispatch projects as a distinct user message and terminal status follows timeline', () => {
  assert.equal(classifyCycleEvent({ type: 'step_begin', step_type: 'message', track: 'scheduled', detail: { message: 'check' }, scheduled_for: '2026-09-18T09:00:00+08:00' }).userText,
    '⏰ 定时 message · 2026-09-18T09:00:00+08:00\ncheck');
  assert.equal(classifyCycleEvent({ type: 'step_begin', step_type: 'message', track: 'ordinary', detail: { message: 'check' } }).userText, 'check');
  assert.equal(classifyCycleEvent({ type: 'cycle_expired' }).terminal, 'expired');
});

test('Cycle bookmark validates stored identity and preserves cursor across refresh', () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } };
  writeCycleBookmark(storage, { cycleRunId: 'run-a', fingerprint: 'abc', cursor: 8 });
  assert.deepEqual(readCycleBookmark(storage), { cycleRunId: 'run-a', fingerprint: 'abc', cursor: 8 });
  storage.setItem('cycle-active-run', '{"cycleRunId":"","fingerprint":"abc","cursor":-1}');
  assert.equal(readCycleBookmark(storage), null);
});

test('Cycle visibility follows the immutable cycle_started flag during replay', () => {
  assert.equal(updateCycleVisibility(true, { type: 'cycle_started', show_agent_steps: false }), false);
  assert.equal(updateCycleVisibility(false, { type: 'cycle_started', show_agent_steps: true }), true);
  assert.equal(updateCycleVisibility(true, { type: 'agent_step', step_type: 'thought' }), true);
});

test('Cycle event guard rejects late events after stop and events from an older generation', () => {
  const guard = createCycleEventGuard();
  const firstGeneration = guard.begin('run-a');
  assert.equal(guard.accept(firstGeneration, { type: 'cycle_started', cycle_run_id: 'run-a' }), true);

  guard.invalidate();
  assert.equal(guard.accept(firstGeneration, { type: 'llm_done', cycle_run_id: 'run-a' }), false);
  assert.equal(guard.accept(firstGeneration, { type: 'cycle_stopped', cycle_run_id: 'run-a' }), false);

  const secondGeneration = guard.begin('run-b');
  assert.equal(guard.accept(firstGeneration, { type: 'llm_done', cycle_run_id: 'run-a' }), false);
  assert.equal(guard.accept(secondGeneration, { type: 'llm_done', cycle_run_id: 'run-a' }), false);
  assert.equal(guard.accept(secondGeneration, { type: 'llm_done', cycle_run_id: 'run-b' }), true);
  assert.equal(guard.accept(secondGeneration, { type: 'llm_done' }), true);
});

test('stopped Cycle stream cannot append a late empty assistant bubble', () => {
  const harness = createProjectionHarness();
  const guard = createCycleEventGuard();
  const generation = guard.begin('run-a');
  const turn = harness.projection.beginMessageTurn('scheduled turn', true);
  harness.projection.stop();
  guard.invalidate();

  const lateDone = { type: 'llm_done' as const, cycle_run_id: 'run-a', content: '', event_sequence: 3 };
  if (guard.accept(generation, lateDone)) {
    void harness.projection.dispatch(lateDone, true);
  }
  const assistants = harness.getMessages().filter((message) => message.role === 'assistant');
  assert.equal(assistants.length, 1);
  assert.equal(assistants[0].id, turn.assistantMessageId);
  assert.equal(assistants[0].metadata?.cycle_stopped, true);
});

function createProjectionHarness() {
  let messages: Message[] = [];
  const projection = createCycleAgentProjection({
    setMessages: (update) => { messages = update(messages); },
    now: () => new Date(0),
    createId: (prefix) => `${prefix}-test`,
  });
  return { projection, getMessages: () => messages, setMessages: (next: Message[]) => { messages = next; } };
}

test('visible Cycle agent events project into one assistant turn and cleanup releases the run ref', async () => {
  const harness = createProjectionHarness();
  harness.setMessages([
    { id: 'cycle-user', role: 'user', content: 'inspect', timestamp: new Date(0) },
    { id: 'cycle-assistant', role: 'assistant', content: '', timestamp: new Date(0), isStreaming: true, agentSteps: [], bubbleKind: 'cycle' },
  ]);
  harness.projection.beginTurn('cycle-assistant');
  await harness.projection.dispatch({ type: 'agent_step', step_type: 'thought', content: 'thinking', event_sequence: 2 }, true);
  await harness.projection.dispatch({ type: 'timeline_event', event: { kind: 'dispatch_delta', display: 'dispatch_card', text: 'route', partial: true }, event_sequence: 3 }, true);
  await harness.projection.dispatch({ type: 'llm_chunk', content: 'answer', event_sequence: 4 }, true);
  await harness.projection.dispatch({ type: 'llm_done', content: 'answer', event_sequence: 5 }, true);

  const assistant = harness.getMessages().find((message) => message.id === 'cycle-assistant');
  assert.equal(harness.getMessages().filter((message) => message.role === 'assistant').length, 1);
  assert.equal(assistant?.content, 'answer');
  assert.equal(assistant?.isStreaming, false);
  assert.equal(assistant?.agentSteps?.some((step) => step.step_type === 'thought'), true);
  assert.equal(harness.projection.runRef.current, null);
});

test('hidden Cycle agent events do not enter React messages and cleanup finalizes an active turn', async () => {
  const harness = createProjectionHarness();
  harness.setMessages([
    { id: 'cycle-assistant', role: 'assistant', content: '', timestamp: new Date(0), isStreaming: true, agentSteps: [], bubbleKind: 'cycle' },
  ]);
  harness.projection.beginTurn('cycle-assistant');
  await harness.projection.dispatch({ type: 'agent_step', step_type: 'observation', content: 'large', event_sequence: 2 }, false);
  assert.deepEqual(harness.getMessages()[0].agentSteps, []);
  harness.projection.clear();
  assert.equal(harness.projection.runRef.current, null);
  assert.equal(harness.getMessages()[0].isStreaming, false);
});

test('visible Cycle llm_done supplies final content when no chunk was received', async () => {
  const harness = createProjectionHarness();
  harness.setMessages([{
    id: 'cycle-assistant', role: 'assistant', content: '', timestamp: new Date(0),
    isStreaming: true, agentSteps: [], bubbleKind: 'cycle',
  }]);
  harness.projection.beginTurn('cycle-assistant');
  await harness.projection.dispatch({ type: 'llm_done', content: 'replayed final answer', event_sequence: 2 }, true);
  const assistant = harness.getMessages()[0];
  assert.equal(assistant.content, 'replayed final answer');
  assert.equal(assistant.isStreaming, false);
  assert.equal(harness.projection.runRef.current, null);
});

test('scheduled visible Cycle turn follows the real event order into one completed assistant bubble', async () => {
  const harness = createProjectionHarness();
  let visible = false;
  visible = updateCycleVisibility(visible, { type: 'cycle_started', show_agent_steps: true, event_sequence: 1 });
  const scheduledStart = classifyCycleEvent({
    type: 'step_begin', step_type: 'message', track: 'scheduled', schedule_id: 'wake-up',
    occurrence_index: 1, scheduled_for: '2026-09-18T09:00:00+08:00', detail: { message: 'scheduled inspect' },
    event_sequence: 2,
  });
  const turn = harness.projection.beginMessageTurn(scheduledStart.userText ?? '', visible);
  await harness.projection.dispatch({ type: 'agent_step', track: 'scheduled', schedule_id: 'wake-up', occurrence_index: 1, step_type: 'thought', content: 'scheduled thought', event_sequence: 3 }, visible);
  await harness.projection.dispatch({ type: 'orchestration_plan', track: 'scheduled', schedule_id: 'wake-up', occurrence_index: 1, stages: [], event_sequence: 4 }, visible);
  await harness.projection.dispatch({ type: 'timeline_event', track: 'scheduled', schedule_id: 'wake-up', occurrence_index: 1, event: { kind: 'message_delta', message_id: 'agent-message', text: 'timeline text', source: 'chat', display: 'chat_bubble', partial: true }, event_sequence: 5 }, visible);
  await harness.projection.dispatch({ type: 'llm_chunk', track: 'scheduled', schedule_id: 'wake-up', occurrence_index: 1, content: 'scheduled answer', event_sequence: 6 }, visible);
  await harness.projection.dispatch({ type: 'llm_done', track: 'scheduled', schedule_id: 'wake-up', occurrence_index: 1, content: 'scheduled answer', event_sequence: 7 }, visible);

  const assistants = harness.getMessages().filter((message) => message.role === 'assistant');
  assert.equal(assistants.length, 1);
  assert.equal(assistants[0].id, turn.assistantMessageId);
  assert.equal(assistants[0].content, 'timeline textscheduled answer');
  assert.equal(assistants[0].isStreaming, false);
  assert.equal(assistants[0].agentSteps?.some((step) => step.step_type === 'thought'), true);
  assert.equal(harness.projection.runRef.current, null);
});

test('stopping an empty Cycle turn marks the bubble stopped instead of rendering a thinking placeholder', () => {
  const harness = createProjectionHarness();
  harness.setMessages([{ id: 'cycle-assistant', role: 'assistant', content: '', timestamp: new Date(0), isStreaming: true, agentSteps: [], bubbleKind: 'cycle' }]);
  harness.projection.beginTurn('cycle-assistant');
  harness.projection.stop();
  const assistant = harness.getMessages()[0];
  assert.equal(assistant.isStreaming, false);
  assert.equal(assistant.metadata?.cycle_stopped, true);
  assert.equal(getAssistantRenderableText(assistant, assistant.metadata?.cycle_stopped === true), '已停止');
  assert.equal(harness.projection.runRef.current, null);
});

test('stopping a step-only Cycle turn preserves steps and marks the bubble stopped', async () => {
  const harness = createProjectionHarness();
  harness.setMessages([{ id: 'cycle-assistant', role: 'assistant', content: '', timestamp: new Date(0), isStreaming: true, agentSteps: [], bubbleKind: 'cycle' }]);
  harness.projection.beginTurn('cycle-assistant');
  await harness.projection.dispatch({ type: 'agent_step', step_type: 'observation', content: 'captured before stop', event_sequence: 1 }, true);
  harness.projection.stop();
  const assistant = harness.getMessages()[0];
  assert.equal(assistant.agentSteps?.[0]?.content, 'captured before stop');
  assert.equal(assistant.isStreaming, false);
  assert.equal(assistant.metadata?.cycle_stopped, true);
  assert.equal(getAssistantRenderableText(assistant, assistant.metadata?.cycle_stopped === true), '已停止');
  assert.equal(harness.projection.runRef.current, null);
});

test('late Cycle finish and failure continuations cannot clear a newer turn', async () => {
  const finishHarness = createProjectionHarness();
  finishHarness.setMessages([{ id: 'first', role: 'assistant', content: '', timestamp: new Date(0), isStreaming: true, agentSteps: [], bubbleKind: 'cycle' }]);
  finishHarness.projection.beginTurn('first');
  const firstFinish = finishHarness.projection.finish();
  finishHarness.projection.beginTurn('second');
  await firstFinish;
  assert.equal(finishHarness.projection.runRef.current?.assistantMessageId, 'second');

  const failureHarness = createProjectionHarness();
  failureHarness.setMessages([{ id: 'first', role: 'assistant', content: '', timestamp: new Date(0), isStreaming: true, agentSteps: [], bubbleKind: 'cycle' }]);
  failureHarness.projection.beginTurn('first');
  const firstFailure = failureHarness.projection.fail(new Error('first failed'));
  failureHarness.projection.beginTurn('second');
  await firstFailure;
  assert.equal(failureHarness.projection.runRef.current?.assistantMessageId, 'second');
});

test('ordinary llm_done cannot clear a scheduled turn started immediately afterward', async () => {
  const harness = createProjectionHarness();
  harness.setMessages([{ id: 'ordinary-assistant', role: 'assistant', content: 'ordinary', timestamp: new Date(0), isStreaming: true, agentSteps: [], bubbleKind: 'cycle' }]);
  harness.projection.beginTurn('ordinary-assistant');

  const ordinaryDone = harness.projection.finish();
  const scheduledTurn = harness.projection.beginMessageTurn('scheduled message', true);
  await harness.projection.dispatch({ type: 'agent_step', track: 'scheduled', schedule_id: 'schedule-a', occurrence_index: 1, step_type: 'thought', content: 'scheduled thought', event_sequence: 4 }, true);
  await ordinaryDone;

  const scheduledAssistant = harness.getMessages().find((message) => message.id === scheduledTurn.assistantMessageId);
  assert.equal(scheduledAssistant?.agentSteps?.some((step) => step.content === 'scheduled thought'), true);
  assert.equal(harness.projection.runRef.current?.assistantMessageId, scheduledTurn.assistantMessageId);
});
