import assert from 'node:assert/strict';
import test from 'node:test';
import * as policy from './local-memory.ts';
import { buildChatWireMessages, type Message } from './message-types.ts';
import { createChatSessionHandlers } from './chat-session-handlers.ts';
import { LLMClient } from './api.ts';

const sid = 'window-session';
const ref = (turn: number, role = 'user') => `chat:${sid}:${turn}:${role}`;
function turn(turn: number, anchor?: string): Message[] {
  return [
    { id: ref(turn), chatTurnRef: ref(turn), ...(anchor ? { chatHistoryStartRef: anchor } : {}), role: 'user', content: `question ${turn}`, timestamp: new Date(0) },
    { id: ref(turn, 'assistant'), chatMessageRef: ref(turn, 'assistant'), role: 'assistant', content: `answer ${turn}`, timestamp: new Date(0) },
  ];
}
const history = (count = 15) => Array.from({ length: count }, (_, index) => turn(index+1)).flat();
function prepare(messages: Message[], enabled: boolean) {
  assert.equal(typeof policy.selectChatLocalMemoryHistoryWindow, 'function');
  const selected = policy.selectChatLocalMemoryHistoryWindow(messages, sid, enabled);
  return buildChatWireMessages({ messages: selected.messages, maxContextLength: 100000,
    currentUserContent: 'next', preserveFullHistory: selected.preserveFullHistory });
}

test('first enable uses the last accepted off window as a complete prefix, without resurrecting excluded UI messages', () => {
  const oldUi = history();
  const previousOff = buildChatWireMessages({ messages: oldUi, maxContextLength: 100000, currentUserContent: 'question 16' });
  const anchor = ref(6);
  const afterOffAccepted = [...oldUi, ...turn(16, anchor)];
  const enabled = prepare(afterOffAccepted, true);
  assert.deepEqual(enabled.slice(0, previousOff.length).map(({ role, content }) => ({ role, content })),
    previousOff.map(({ role, content }) => ({ role, content })), 'provider message prefix excludes opaque transport refs');
  assert.equal(enabled.some((message) => message.content === 'question 1'), false);
  assert.equal(enabled.some((message) => message.content === 'answer 16'), true);
});

test('unknown initial anchor retains legacy selection; a matched exact window remains pinned after toggle off', () => {
  const ui = history();
  const baseline = buildChatWireMessages({ messages: ui, maxContextLength: 100000, currentUserContent: 'next' });
  assert.deepEqual(prepare(ui, true), baseline);
  const accepted = turn(16, ref(6));
  accepted[0].localMemories = [{ text: 'display-only hit' }];
  const nextOff = prepare([...ui, ...accepted], false);
  assert.equal(nextOff[0].metadata?.chat_turn_ref, ref(6));
  assert.equal(nextOff.length, 23);
  assert.equal(JSON.stringify(nextOff).includes('display-only hit'), false);
});

test('actual first outgoing assistant ref is available without inferring neighboring text or turn', () => {
  const ui = history();
  const wire = buildChatWireMessages({ messages: ui.slice(1), maxContextLength: 100000, currentUserContent: 'next', preserveFullHistory: true });
  assert.equal(wire[0].metadata?.chat_message_ref, ref(1, 'assistant'));
  assert.equal(typeof policy.getChatRequestHistoryStartRef, 'function');
  assert.equal(policy.getChatRequestHistoryStartRef(wire, sid), ref(1, 'assistant'));
});

test('real Hub replay restores the matched window anchor and a successful auto-compact boundary for the next POST', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const started = (turn: number, anchor: string) => ({ kind: 'turn_started', source: 'chat', session_id: sid, turn_index: turn,
    user_content: `question ${turn}`, images: [], message_id: ref(turn, 'assistant'), history_start_ref: anchor });
  const event = (kind: string, fields: any = {}) => ({ type: 'timeline_event', event: { kind, summary_message_id: 'compact-window', ...fields } });
  const payloads = [
    { type: 'timeline_event', event: started(1, ref(1)) },
    { type: 'timeline_event', event: { kind: 'local_memory_retrieved', session_id: sid, turn_index: 1, chunks: [{ text: 'first memory' }], history_start_ref: ref(1) } },
    { type: 'done', session_id: sid },
    { type: 'timeline_event', event: started(2, ref(1)) },
    event('compact_started', { trigger: 'auto', mode: 'chat' }),
    event('compact_delta', { text: 'partial' }),
    event('compact_completed', { text: '<summary>frozen compact text</summary>', metadata: { summary_tokens: 5 } }),
    { type: 'timeline_event', event: { kind: 'local_memory_retrieved', session_id: sid, turn_index: 2, chunks: [{ text: 'second memory' }], history_start_ref: ref(2) } },
    { type: 'done', session_id: sid },
  ].map((payload, index) => ({ ...payload, event_sequence: index+1 }));
  globalThis.fetch = async () => new Response(payloads.map((payload) => `data: ${JSON.stringify(payload)}\n\n`).join(''), { status: 200 });
  const handlers = createChatSessionHandlers({ now: () => new Date(0) });
  await new LLMClient('/api/backend').sessionHubEvents(sid, 0, { onPayload: handlers.onPayload }, undefined, 'Chat');
  const messages = [...handlers.getMessages()];
  assert.equal(messages.find((message) => message.id === ref(1))?.isPreCompact, true);
  assert.equal(messages.find((message) => message.id === ref(2))?.isPreCompact, undefined);
  const summary = messages.find((message) => message.compactionBoundaryId === 'compact-window');
  assert.equal(summary?.compactStatus, 'completed');
  assert.equal(summary?.content, '<summary>frozen compact text</summary>');
  const next = prepare(messages, false);
  assert.equal(next[0].metadata?.compact_summary, true);
  assert.equal(next[0].metadata?.summary_id, 'compact-window');
  assert.equal(next.some((message) => message.metadata?.chat_turn_ref === ref(1)), false);
  assert.equal(next.some((message) => message.metadata?.chat_turn_ref === ref(2)), true);
  assert.equal(JSON.stringify(next).includes('second memory'), false);
});


test('new Chat bootstrap anchors the exact legacy window using opaque UI identity, without fake accepted turn indices', () => {
  const imported: Message[] = Array.from({ length: 30 }, (_, index) => ({ id: `agent-ui-${index}`, role: index % 2 ? 'assistant' : 'user',
    content: `legacy ${index}`, timestamp: new Date(0) }));
  const selected = policy.selectChatLocalMemoryHistoryWindow(imported, sid, true);
  const wire = buildChatWireMessages({ messages: selected.messages, chatSessionId: sid, maxContextLength: 100000,
    currentUserContent: 'first Chat request', preserveFullHistory: selected.preserveFullHistory });
  const anchor = `chat:${sid}:bootstrap:agent-ui-10:user`;
  assert.equal(wire.length, 21);
  assert.equal(wire[0].content, 'legacy 10');
  assert.equal(wire[0].metadata?.chat_message_ref, anchor);
  assert.equal(wire[0].metadata?.chat_turn_ref, undefined);
  assert.equal(policy.getChatRequestHistoryStartRef(wire, sid), anchor);
  assert.equal(wire.at(-1)?.metadata, undefined);
  const accepted = turn(1, anchor);
  accepted[0].localMemories = [{ text: 'selected hit' }];
  const pinned = policy.selectChatLocalMemoryHistoryWindow([...imported, ...accepted], sid, false);
  const next = buildChatWireMessages({ messages: pinned.messages, chatSessionId: sid, maxContextLength: 100000,
    currentUserContent: 'next', preserveFullHistory: pinned.preserveFullHistory });
  assert.equal(next[0].content, 'legacy 10');
  assert.equal(next.some((message) => message.content === 'legacy 0'), false);
  assert.equal(policy.getChatRequestHistoryStartRef(next, sid), anchor);
});

test('bootstrap anchor survives actual Hub replay without copying initial UI rows or images back into the journal', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const anchor = `chat:${sid}:bootstrap:original-ui-row:user`;
  const payloads = [
    { type: 'timeline_event', event: { kind: 'turn_started', session_id: sid, turn_index: 1, user_content: 'first request', history_start_ref: anchor } },
    { type: 'timeline_event', event: { kind: 'local_memory_retrieved', session_id: sid, turn_index: 1, chunks: [{ text: 'selected hit' }], history_start_ref: anchor } },
    { type: 'done', session_id: sid },
  ].map((payload, index) => ({ ...payload, event_sequence: index+1 }));
  globalThis.fetch = async () => new Response(payloads.map((payload) => `data: ${JSON.stringify(payload)}\n\n`).join(''), { status: 200 });
  const handlers = createChatSessionHandlers({ now: () => new Date(0) });
  await new LLMClient('/api/backend').sessionHubEvents(sid, 0, { onPayload: handlers.onPayload }, undefined, 'Chat');
  const selected = policy.selectChatLocalMemoryHistoryWindow(handlers.getMessages(), sid, false);
  assert.equal(selected.historyStartRef, anchor);
  assert.equal(selected.preserveFullHistory, true);
  assert.equal(selected.messages.length, 2);
  assert.equal(selected.messages[0].content, 'first request');
  assert.equal(selected.messages.some((message) => message.id.includes('original-ui-row')), false);
});

test('request history anchor is frozen across same-ID retries including an initially absent anchor', () => {
  const anchors = new Map<string, string | undefined>();
  assert.equal(policy.freezeChatHistoryStartRef(anchors, 'request-a', ref(6)), ref(6));
  assert.equal(policy.freezeChatHistoryStartRef(anchors, 'request-a', ref(7)), ref(6));
  assert.equal(policy.freezeChatHistoryStartRef(anchors, 'request-b', undefined), undefined);
  assert.equal(policy.freezeChatHistoryStartRef(anchors, 'request-b', ref(7)), undefined);
});

test('a later failed compact preserves earlier retired rows and the last successful summary in replay', async () => {
  const handlers = createChatSessionHandlers({ now: () => new Date(0) });
  let sequence = 0;
  const send = (event: any) => handlers.onPayload({ event_sequence: ++sequence, type: 'timeline_event', event });
  await send({ kind: 'turn_started', session_id: sid, turn_index: 1, user_content: 'first', history_start_ref: ref(1) });
  await send({ kind: 'compact_started', summary_message_id: 'successful', trigger: 'manual', mode: 'chat' });
  await send({ kind: 'compact_completed', summary_message_id: 'successful', text: 'accepted summary' });
  await send({ kind: 'turn_started', session_id: sid, turn_index: 2, user_content: 'second', history_start_ref: ref(2) });
  await send({ kind: 'compact_started', summary_message_id: 'failed', trigger: 'auto', mode: 'chat' });
  await send({ kind: 'compact_failed', summary_message_id: 'failed', metadata: { reason: 'validation_failed' } });
  const restored = [...handlers.getMessages()];
  assert.equal(restored.find((message) => message.id === ref(1))?.isPreCompact, true);
  const wire = prepare(restored, false);
  assert.equal(wire[0].metadata?.summary_id, 'successful');
  assert.equal(wire.some((message) => message.metadata?.chat_turn_ref === ref(1)), false);
  assert.equal(wire.some((message) => message.metadata?.chat_turn_ref === ref(2)), true);
  assert.equal(wire.some((message) => message.metadata?.summary_id === 'failed'), false);
});

test('ordinary POST explicitly transports the frozen window anchor and preserves raw request messages', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  let body: any;
  globalThis.fetch = async (_url, init) => {
    body = JSON.parse(String(init?.body));
    return new Response('data: [DONE]\n\n', { status: 200 });
  };
  const messages = [{ role: 'user' as const, content: 'raw request' }];
  await new LLMClient('/api/backend').chatStream(messages, { enable_local_memory: true, chat_history_start_ref: ref(6) },
    () => {}, () => {}, (error) => { throw error; });
  assert.equal(body.chat_history_start_ref, ref(6));
  assert.deepEqual(body.messages, messages);
});

test('manual compact after bootstrap replay keeps the explicit stored-prefix anchor and current system prompt', async () => {
  const { buildCompactRequestBody } = await import('./compact-client.ts');
  const anchor = `chat:${sid}:bootstrap:imported-first:user`;
  const messages = turn(1, anchor);
  messages[0].localMemories = [{ text: 'display-only selected text' }];
  const window = policy.selectChatLocalMemoryHistoryWindow(messages, sid, true);
  const body = buildCompactRequestBody({ agentMode: false, sessionId: sid,
    chatHistoryStartRef: window.historyStartRef, messages: window.messages, systemPrompt: 'user configured system prompt' });
  assert.equal(body.session_id, sid);
  assert.equal(body.chat_history_start_ref, anchor);
  assert.deepEqual(body.messages[0], { role: 'system', content: 'user configured system prompt' });
  assert.equal(JSON.stringify(body).includes('display-only selected text'), false);
});

test('live auto compact anchors before the current user and matches replay model-wire projection', async () => {
  const { createChatLocalMemoryDisplayHandler } = await import('./chat-session-handlers.ts');
  const { applyCompactProjection } = await import('./compact-projection.ts');
  const { projectTimelineEvent } = await import('./timeline-projection.ts');
  let live = [...turn(1), { id: 'optimistic-user', role: 'user' as const, content: 'question 2', timestamp: new Date(0) },
    { id: 'optimistic-assistant', role: 'assistant' as const, content: '', timestamp: new Date(0) }];
  const handler = createChatLocalMemoryDisplayHandler({ sessionId: sid, userMessageId: 'optimistic-user', assistantMessageId: 'optimistic-assistant',
    setMessages: (update) => { live = update(live); } });
  const started = { kind: 'turn_started', session_id: sid, turn_index: 2, user_content: 'question 2', history_start_ref: ref(1) };
  const compactEvents = [
    { kind: 'compact_started', summary_message_id: 'live-summary', trigger: 'auto' as const, mode: 'chat' as const },
    { kind: 'compact_completed', summary_message_id: 'live-summary', text: 'frozen compact result' },
  ];
  handler(started);
  assert.equal(handler.getUserMessageId(), ref(2));
  assert.equal(live.find((message) => message.id === 'optimistic-assistant')?.chatMessageRef, ref(2, 'assistant'));
  const marked = { current: new Set<string>() };
  for (const event of compactEvents) {
    handler(event);
    const projection = projectTimelineEvent(event);
    if (projection.kind === 'compact_started' || projection.kind === 'compact_completed') {
      applyCompactProjection(projection, (update) => { live = typeof update === 'function' ? update(live) : update; }, marked,
        { insertBeforeMessageId: handler.getUserMessageId() });
    }
  }
  assert.equal(live.find((message) => message.id === ref(1))?.isPreCompact, true);
  assert.equal(live.find((message) => message.id === ref(2))?.isPreCompact, undefined);
  assert.equal(live.find((message) => message.id === ref(2))?.chatHistoryStartRef, ref(2));
  const replay = createChatSessionHandlers({ initialMessages: turn(1), now: () => new Date(0) });
  for (const [index, event] of [started, ...compactEvents].entries()) {
    await replay.onPayload({ type: 'timeline_event', event_sequence: index+1, event });
  }
  assert.deepEqual(prepare(live, true), prepare([...replay.getMessages()], true));
});
