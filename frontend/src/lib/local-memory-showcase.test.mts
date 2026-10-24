import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { dispatchAgentSessionPayload, LLMClient } from './api.ts';
import { projectTimelineEvent } from './timeline-projection.ts';
import { createTextAgentSessionHandlers } from './text-agent-session-handlers.ts';
import { createCycleAgentProjection } from './cycle-agent-projection.ts';
import { buildAgentContextMessages, buildChatWireMessages, type Message, type AgentRunState } from './message-types.ts';

const memory = (session = 'session-a', turn = 1, text = '仅展示命中的原文') => ({
  kind: 'local_memory_retrieved', session_id: session, turn_index: turn, chunks: [{ text }],
});
const started = (session = 'session-a', turn = 1) => ({
  kind: 'turn_started', session_id: session, turn_index: turn, user_content: `raw question ${turn}`, images: [],
});
function harness(optimistic = false) {
  let messages: Message[] = optimistic ? [
    { id: 'optimistic-user', role: 'user', content: 'raw question 1', timestamp: new Date(0) },
    { id: 'optimistic-assistant', role: 'assistant', content: '', timestamp: new Date(0), isStreaming: true },
  ] : [];
  const runRef: { current: AgentRunState | null } = { current: optimistic ? {
    assistantMessageId: 'optimistic-assistant', projectedAssistantMessageIds: new Set(), stopWasClicked: false, answerNowWasClicked: false,
  } : null };
  const handlers = createTextAgentSessionHandlers({
    activeRunRef: runRef,
    setMessages: (update) => { messages = update(messages); },
    setAgentSessionId() {}, resetStreamingState() {}, clearStoppedState() {}, clearAnswerNowFlag() {}, clearStopFlag() {},
    getTerminalBehavior: () => ({ suppressErrorMessage: false, resetStreamingState: false, resetStoppedState: false, clearAnswerNowFlag: false }),
    newlyCompactMarkedRef: { current: new Set() }, defaultReasoningCollapsed: false,
    now: () => new Date(0), createId: (prefix) => `${prefix}-test`,
  }, runRef);
  return { handlers, getMessages: () => messages, event: (event: any) => dispatchAgentSessionPayload({ type: 'timeline_event', event }, handlers) };
}

test('memory timeline metadata validates identity and text while copying only display text', () => {
  const projection = projectTimelineEvent({ ...memory(), chunks: [{ text: 'text', source: '/private/source.md', lines: [1,2] }] } as any);
  assert.deepEqual(projection, { kind: 'local_memory_retrieved', sessionId: 'session-a', turnIndex: 1, chunks: [{ text: 'text' }] });
  for (const invalid of [
    { session_id: '' }, { session_id: null }, { turn_index: -1 }, { turn_index: 1.5 },
    { turn_index: '1' }, { chunks: null }, { chunks: [{ text: 5 }] }, { chunks: [null] },
  ]) assert.deepEqual(projectTimelineEvent({ ...memory(), ...invalid } as any), { kind: 'noop' });
});

test('live optimistic reconciliation and full Hub replay attach identical display-only chunks to the canonical user', async () => {
  const live = harness(true);
  const replay = harness();
  for (const target of [live, replay]) {
    await target.event(started());
    await target.event(memory());
    const beforeRepeat = target.getMessages();
    await target.event(memory());
    assert.equal(target.getMessages(), beforeRepeat, 'duplicate replaces idempotently');
  }
  assert.deepEqual(live.getMessages().filter((m) => m.role === 'user'), replay.getMessages().filter((m) => m.role === 'user'));
  const user = live.getMessages()[0];
  assert.equal(user.id, 'session-a-turn-1-user');
  assert.equal(user.content, 'raw question 1');
  assert.deepEqual(user.localMemories, [{ text: '仅展示命中的原文' }]);
  assert.equal(live.getMessages()[1].localMemories, undefined);
  assert.equal(user.metadata, undefined);
});

test('different turns, Stop and malformed or unrelated metadata cannot attach to the latest global user', async () => {
  const target = harness();
  await target.event(started());
  await target.event(memory());
  target.handlers.onDetached();
  await target.event(started('session-a', 2));
  await target.event(memory('session-a', 2, 'second memory'));
  const before = target.getMessages();
  await target.event(memory('another-session', 2, 'must not attach'));
  await target.event({ ...memory(), chunks: [{ text: false }] });
  await target.event(memory('session-a', 0, 'Cycle-only zero index'));
  assert.equal(target.getMessages(), before);
  assert.deepEqual(before.filter((m) => m.role === 'user').map((m) => m.localMemories), [[{ text: '仅展示命中的原文' }], [{ text: 'second memory' }]]);
});

test('showcase never becomes chat or Agent bootstrap wire content', async () => {
  const target = harness();
  await target.event(started());
  await target.event(memory());
  for (const preserveFullHistory of [false, true]) {
    for (const wire of [
      buildAgentContextMessages({ messages: target.getMessages(), maxContextLength: 10000, currentUserContent: 'next', preserveFullHistory }),
      buildChatWireMessages({ messages: target.getMessages(), maxContextLength: 10000, currentUserContent: 'next', preserveFullHistory, systemPrompt: '' }),
    ]) {
      assert.equal(JSON.stringify(wire).includes('localMemories'), false);
      assert.equal(JSON.stringify(wire).includes('仅展示命中的原文'), false);
      assert.equal(wire[0].content, 'raw question 1');
    }
  }
});

for (const visible of [false, true]) {
  test(`Cycle showcase survives replay with show_agent_steps=${visible} and respects explicit step identity`, async () => {
    let messages: Message[] = [{ id: 'unrelated-user', role: 'user', content: 'other session', timestamp: new Date(0) }];
    let nextId = 0;
    const cycle = createCycleAgentProjection({
      setMessages: (update) => { messages = update(messages); }, now: () => new Date(0), createId: (prefix) => `${prefix}-${++nextId}`,
    });
    const first = { type: 'step_begin', cycle_run_id: 'cycle-a', track: 'ordinary' as const, path: [0], step_type: 'message' };
    const turn = cycle.beginMessageTurn('Cycle raw question', visible, first);
    const event = { ...first, type: 'timeline_event', event: memory('agent-cycle-context', 0) };
    await cycle.dispatch(event, visible);
    await cycle.dispatch(event, visible);
    assert.deepEqual(messages.find((m) => m.id === turn.userMessageId)?.localMemories, [{ text: '仅展示命中的原文' }]);
    const second = { ...first, path: [1] };
    const next = cycle.beginMessageTurn('next Cycle question', visible, second);
    const before = messages;
    await cycle.dispatch(event, visible);
    await cycle.dispatch({ ...event, cycle_run_id: 'cycle-other', path: [1] }, visible);
    assert.equal(messages, before);
    assert.equal(messages.find((m) => m.id === next.userMessageId)?.localMemories, undefined);
    assert.equal(messages[0].localMemories, undefined);
    cycle.stop();
    const stopped = messages;
    await cycle.dispatch({ ...event, path: [1] }, visible);
    assert.equal(messages, stopped);
  });
}

test('both user bubble branches render the shared showcase after user text and voice replay', async () => {
  const list = await readFile(new URL('../components/conversation/ConversationMessageList.tsx', import.meta.url), 'utf8');
  assert.match(list, /message\.voiceReplay && <VoiceReplayPlayer[^\n]+\n\s*<RetrievedLocalMemories chunks=\{message\.localMemories\}/);
  for (const page of ['app/page.tsx', 'app/vision/page.tsx']) {
    const source = await readFile(new URL(`../${page}`, import.meta.url), 'utf8');
    assert.match(source, /<ConversationMessageList messages=\{messages\}/);
    assert.match(source, /useConversationCycle\(/);
  }
  // Exact-user Cycle identity and live/replay showcase behavior are exercised
  // above; the route must reuse that projection instead of owning another copy.
  const cycle = await readFile(new URL('./conversation-cycle.ts', import.meta.url), 'utf8');
  assert.match(cycle, /projection\.beginMessageTurn\(projected\.userText, showSteps, incoming\)/);
});


test('actual Agent POST and retained Hub SSE replay recover the same memory showcase', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const payloads = [
    { event_sequence: 1, type: 'timeline_event', event: started() },
    { event_sequence: 2, type: 'timeline_event', event: memory() },
    { event_sequence: 3, type: 'done', session_id: 'session-a' },
  ];
  globalThis.fetch = async () => new Response(payloads.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''), { status: 200 });
  const client = new LLMClient('/api/backend');
  const live = harness(true);
  await client.agentChatStream({ message: 'raw question 1' }, live.handlers.onStep, live.handlers.onContent,
    live.handlers.onDone, live.handlers.onPayloadError, live.handlers.onSessionStart, undefined, live.handlers.onTimelineEvent);
  const restored = harness();
  await client.agentSessionEvents('session-a', 0, restored.handlers);
  assert.deepEqual(restored.getMessages().filter((m) => m.role === 'user'), live.getMessages().filter((m) => m.role === 'user'));
  assert.deepEqual(restored.getMessages()[0].localMemories, [{ text: '仅展示命中的原文' }]);
});

test('repeated Cycle path iterations and scheduled occurrences cannot consume earlier memory events', async () => {
  let messages: Message[] = [];
  let nextId = 0;
  const cycle = createCycleAgentProjection({
    setMessages: (update) => { messages = update(messages); }, now: () => new Date(0), createId: (prefix) => `${prefix}-${++nextId}`,
  });
  const start = { type: 'step_begin', cycle_run_id: 'cycle-a', track: 'ordinary' as const, path: [0], event_sequence: 1 };
  const first = cycle.beginMessageTurn('first iteration', false, start);
  const oldMemory = { ...start, type: 'timeline_event', event_sequence: 2, event: memory('agent-a', 0, 'first') };
  await cycle.dispatch(oldMemory, false);
  const second = cycle.beginMessageTurn('second iteration', false, { ...start, event_sequence: 3 });
  const before = messages;
  await cycle.dispatch(oldMemory, false);
  assert.equal(messages, before);
  assert.deepEqual(messages.find((m) => m.id === first.userMessageId)?.localMemories, [{ text: 'first' }]);
  assert.equal(messages.find((m) => m.id === second.userMessageId)?.localMemories, undefined);
  const scheduled = { ...start, track: 'scheduled' as const, path: [], schedule_id: 'daily', occurrence_index: 2, event_sequence: 10 };
  const third = cycle.beginMessageTurn('second scheduled occurrence', false, scheduled);
  const scheduledBefore = messages;
  await cycle.dispatch({ ...scheduled, type: 'timeline_event', occurrence_index: 1, event_sequence: 11, event: memory('agent-a', 0, 'old occurrence') }, false);
  assert.equal(messages, scheduledBefore);
  await cycle.dispatch({ ...scheduled, type: 'timeline_event', event_sequence: 12, event: memory('agent-a', 0, 'current occurrence') }, false);
  assert.deepEqual(messages.find((m) => m.id === third.userMessageId)?.localMemories, [{ text: 'current occurrence' }]);
});
