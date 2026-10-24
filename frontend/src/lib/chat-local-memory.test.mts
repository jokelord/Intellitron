import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { LLMClient } from './api.ts';
import * as chatHandlersModule from './chat-session-handlers.ts';
import { buildChatWireMessages, DEFAULT_SETTINGS, type Message } from './message-types.ts';
import { runChatConversationTransport } from './conversation/conversation-transport.ts';
import { createConversationTurnMessages } from './conversation/use-conversation-send.ts';
import { freezeLocalMemoryPreference } from './local-memory.ts';
import * as memoryPolicy from './local-memory.ts';
import { buildCompactRequestBody } from './compact-client.ts';

for (const enabled of [undefined, false, true]) {
  test(`ordinary Chat POST explicitly serializes memory ${enabled ?? 'default false'}`, async (t) => {
    const original = globalThis.fetch;
    t.after(() => { globalThis.fetch = original; });
    let body: any;
    globalThis.fetch = async (_url, init) => {
      body = JSON.parse(String(init?.body));
      return new Response('data: [DONE]\n\n', { status: 200 });
    };
    await new LLMClient('/api/backend').chatStream([{ role: 'user', content: 'raw user question' }],
      { enable_local_memory: enabled }, () => {}, () => {}, (error) => { throw error; });
    assert.equal(body.enable_local_memory, enabled === true);
    assert.deepEqual(body.messages, [{ role: 'user', content: 'raw user question' }]);
    assert.equal(body.tools, undefined);
  });
}

const sourceImage = 'data:image/jpeg;base64,AAAA';
const rawContent = [{ type: 'image_url', image_url: { url: sourceImage } }, { type: 'text', text: 'raw question' }];
const started = { kind: 'turn_started', source: 'chat', session_id: 'chat-session', turn_index: 1,
  user_content: 'raw question', images: [sourceImage], message_id: 'chat:chat-session:1:assistant' };
const memory = { kind: 'local_memory_retrieved', session_id: 'chat-session', turn_index: 1,
  chunks: [{ text: 'frozen excerpt' }] };
const stream = () => new Response([
  { event_sequence: 1, type: 'timeline_event', event: started },
  { event_sequence: 2, type: 'timeline_event', event: memory },
  { event_sequence: 3, type: 'done', session_id: 'chat-session' },
].map((payload) => `data: ${JSON.stringify(payload)}\n\n`).join(''), { status: 200 });

function displayHandler(options: any) {
  assert.equal(typeof chatHandlersModule.createChatLocalMemoryDisplayHandler, 'function');
  return chatHandlersModule.createChatLocalMemoryDisplayHandler(options);
}

test('real ordinary Chat POST and Hub replay bind retrieved memory to the same exact raw user turn', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async () => stream();
  let live: Message[] = [
    { id: 'optimistic-user', role: 'user', content: rawContent, images: [sourceImage], timestamp: new Date(0) },
    { id: 'optimistic-assistant', role: 'assistant', content: '', timestamp: new Date(0) },
  ];
  const onTimelineEvent = displayHandler({ sessionId: 'chat-session', userMessageId: 'optimistic-user',
    setMessages: (update: (previous: Message[]) => Message[]) => { live = update(live); } });
  const client = new LLMClient('/api/backend');
  await client.chatStream([{ role: 'user', content: rawContent }], { session_id: 'chat-session', enable_local_memory: true },
    () => {}, () => {}, (error) => { throw error; }, undefined, onTimelineEvent);
  const replay = chatHandlersModule.createChatSessionHandlers({ now: () => new Date(0) });
  await client.sessionHubEvents('chat-session', 0, { onPayload: replay.onPayload }, undefined, 'Chat');
  assert.deepEqual(live[0], replay.getMessages()[0]);
  assert.equal(live[0].id, 'chat:chat-session:1:user');
  assert.deepEqual(live[0].content, rawContent);
  assert.deepEqual(live[0].localMemories, [{ text: 'frozen excerpt' }]);
  assert.equal(live[0].metadata, undefined);
  assert.equal(live[1].id, 'optimistic-assistant', 'assistant ownership remains with the existing live callbacks');
});

test('live Chat display binding rejects pre-start, other sessions, other turns and malformed metadata without global-user fallback', () => {
  let messages: Message[] = [
    { id: 'optimistic-user', role: 'user', content: 'raw question', timestamp: new Date(0) },
    { id: 'latest-other-user', role: 'user', content: 'unrelated', timestamp: new Date(0) },
  ];
  const handler = displayHandler({ sessionId: 'chat-session', userMessageId: 'optimistic-user',
    setMessages: (update: (previous: Message[]) => Message[]) => { messages = update(messages); } });
  const before = messages;
  handler(memory);
  handler({ ...started, session_id: 'other' });
  assert.equal(messages, before);
  handler(started);
  handler(memory);
  const accepted = messages;
  handler(memory);
  handler({ ...memory, session_id: 'other' });
  handler({ ...memory, turn_index: 2 });
  handler({ ...memory, chunks: [{ text: false }] });
  handler({ ...started, turn_index: 2 });
  assert.equal(messages, accepted);
  assert.deepEqual(messages[0].localMemories, [{ text: 'frozen excerpt' }]);
  assert.equal(messages[1].localMemories, undefined);
});

test('manual Chat compact keeps its Chat session and excludes the display-only memory snapshot', () => {
  const message: Message = { id: 'chat:chat-session:1:user', role: 'user', content: 'raw', timestamp: new Date(0), localMemories: [{ text: 'display snapshot' }] };
  const body = buildCompactRequestBody({ agentMode: false, sessionId: 'chat-session', messages: [message] });
  assert.equal(body.session_id, 'chat-session');
  assert.equal(JSON.stringify(body).includes('display snapshot'), false);
});

test('both pages wire actual Chat POST snapshots, shared exact-user projection, and the current Chat compact session', async () => {
  for (const pageMode of ['text', 'vision'] as const) {
    const turnMessages = createConversationTurnMessages({userContent: pageMode === 'vision' ? rawContent : 'raw question', initialRouting: false, createId: (() => {let id = 0; return () => `optimistic-${++id}`;})()});
    let messages: Message[] = [turnMessages.userMessage, turnMessages.assistantMessage];
    let wire: any, preference: any;
    await runChatConversationTransport({
      turn: { ...turnMessages, pageMode, requestId: 'memory-test', controller: new AbortController(), interactionMode: 'chat', sessionId: 'chat-session', historyMessages: [], userContent: turnMessages.userMessage.content, agentText: 'raw question', preserveFullHistory: false, hasUserText: true },
      settings: DEFAULT_SETTINGS, maxContextLength: 100000, runtimeContext: null, setMessages: update => {messages = update(messages);}, newlyCompactMarkedRef: {current: new Set()},
      localMemoryRequestsRef: {current: new Map()}, chatHistoryRequestsRef: {current: new Map()}, localMemoryEnabled: true, isCurrent: () => true, onDone() {}, onError(error) {throw error;},
      client: { chatStream: async (body, options, _chunk, _done, _error, _controller, timeline) => {
        wire = body; preference = options.enable_local_memory;
        timeline?.(started); timeline?.(memory);
        timeline?.({kind: 'compact_started', summary_message_id: 'memory-compact', mode: 'chat'});
      } },
    });
    assert.equal(preference, true);
    assert.equal(JSON.stringify(wire).includes('frozen excerpt'), false);
    assert.deepEqual(messages.find(message => message.role === 'user')?.localMemories, [{text: 'frozen excerpt'}]);
    const summary = messages.findIndex(message => message.bubbleKind === 'compact_summary');
    assert.equal(summary, 0, 'Chat auto compact anchors before the current canonical user');
    assert.equal(messages[1].id, 'chat:chat-session:1:user');
  }
  for (const page of ['app/page.tsx', 'app/vision/page.tsx']) {
    const source = await readFile(new URL(`../${page}`, import.meta.url), 'utf8');
    assert.match(source, /runChatConversationTransport\(/);
    assert.match(source, /useConversationCommands\(/);
    assert.doesNotMatch(source, /<LocalMemoryToggle[\s\S]*?agentMode=\{settings\.agentMode\}/);
  }
  const commands = await readFile(new URL('./conversation/use-conversation-commands.ts', import.meta.url), 'utf8');
  assert.match(commands, /sessionId: settings\.agentMode \? agentToolState\.pairedSessionId : chatSessionId/);
});


test('live and replayed historical Chat wire and compact carry the same session-qualified opaque reference', async () => {
  let messages: Message[] = [{ id: 'optimistic-user', role: 'user', content: rawContent, images: [sourceImage], timestamp: new Date(0) }];
  const handler = displayHandler({ sessionId: 'chat-session', userMessageId: 'optimistic-user',
    setMessages: (update: (previous: Message[]) => Message[]) => { messages = update(messages); } });
  handler(started);
  handler(memory);
  const replay = chatHandlersModule.createChatSessionHandlers({ now: () => new Date(0) });
  await replay.onPayload({ event_sequence: 1, type: 'timeline_event', event: started });
  await replay.onPayload({ event_sequence: 2, type: 'timeline_event', event: memory });
  for (const history of [messages, [...replay.getMessages()]]) {
    for (const preserveFullHistory of [false, true]) {
      const wire = buildChatWireMessages({ messages: history, maxContextLength: 100000, currentUserContent: 'next', preserveFullHistory });
      assert.equal(wire[0].metadata?.chat_turn_ref, 'chat:chat-session:1:user');
      assert.deepEqual(wire[0].content, rawContent);
      assert.equal(wire[wire.length-1].metadata?.chat_turn_ref, undefined, 'current user is not referenced before acceptance');
      assert.equal(JSON.stringify(wire).includes('frozen excerpt'), false);
    }
    const compact = buildCompactRequestBody({ agentMode: false, sessionId: 'chat-session', messages: history });
    assert.equal(compact.messages[0].metadata?.chat_turn_ref, 'chat:chat-session:1:user');
    assert.equal(JSON.stringify(compact).includes('frozen excerpt'), false);
  }
});

test('ordinary Chat deferred sends snapshot current preference and same-ID retries preserve it', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const bodies: any[] = [];
  globalThis.fetch = async (_url, init) => {
    bodies.push(JSON.parse(String(init?.body)));
    return new Response('data: [DONE]\n\n', { status: 200 });
  };
  const frozen = new Map<string, boolean>();
  let enabled = false;
  const client = new LLMClient('/api/backend');
  const sendQueued = () => client.chatStream([{ role: 'user', content: 'raw' }],
    { enable_local_memory: freezeLocalMemoryPreference(frozen, 'queued-request', enabled) }, () => {}, () => {}, (error) => { throw error; });
  enabled = true;
  await sendQueued();
  enabled = false;
  await sendQueued();
  await client.chatStream([{ role: 'user', content: 'next' }],
    { enable_local_memory: freezeLocalMemoryPreference(frozen, 'next-request', enabled) }, () => {}, () => {}, (error) => { throw error; });
  assert.deepEqual(bodies.map((body) => body.enable_local_memory), [true, true, false]);
});

for (const rawText of ['', '   ', 'user voice transcript']) {
  test(`Vision ordinary Chat preserves images and uses original text eligibility ${JSON.stringify(rawText)}`, async (t) => {
    const original = globalThis.fetch;
    t.after(() => { globalThis.fetch = original; });
    let body: any;
    globalThis.fetch = async (_url, init) => {
      body = JSON.parse(String(init?.body));
      return new Response('data: [DONE]\n\n', { status: 200 });
    };
    const content = [{ type: 'image_url', image_url: { url: sourceImage } }, { type: 'text', text: rawText || '[generated sampling manifest]' }];
    await new LLMClient('/api/backend').chatStream([{ role: 'user', content }],
      { enable_local_memory: freezeLocalMemoryPreference(new Map(), 'image-request', true, rawText.trim().length > 0) },
      () => {}, () => {}, (error) => { throw error; });
    assert.deepEqual(body.messages, [{ role: 'user', content }]);
    assert.equal(body.enable_local_memory, rawText.trim().length > 0);
  });
}

test('Hub replay stamps a session-qualified ref on an existing canonical raw user without replacing its content', async () => {
  const existing: Message = { id: 'chat:chat-session:1:user', role: 'user', content: rawContent, images: [sourceImage], timestamp: new Date(0) };
  const handlers = chatHandlersModule.createChatSessionHandlers({ initialMessages: [existing], now: () => new Date(0) });
  await handlers.onPayload({ event_sequence: 1, type: 'timeline_event', event: started });
  const user = handlers.getMessages()[0];
  assert.equal(user.chatTurnRef, 'chat:chat-session:1:user');
  assert.equal(user.content, existing.content);
  const body = buildCompactRequestBody({ agentMode: false, sessionId: 'chat-session', messages: [...handlers.getMessages()] });
  assert.equal(body.messages[0].metadata?.chat_turn_ref, 'chat:chat-session:1:user');
});


test('successful same-session retrieval pins more than twenty raw history messages after toggle off', () => {
  assert.equal(typeof memoryPolicy.shouldPreserveChatLocalMemoryHistory, 'function');
  const messages: Message[] = Array.from({ length: 30 }, (_, index) => ({
    id: `chat:chat-session:${Math.floor(index/2)+1}:${index % 2 ? 'assistant' : 'user'}`,
    role: index % 2 ? 'assistant' : 'user', content: `raw ${index}`, timestamp: new Date(0),
  }));
  messages[0] = { ...messages[0], chatTurnRef: 'chat:chat-session:1:user', chatHistoryStartRef: 'chat:chat-session:1:user', localMemories: [{ text: 'frozen excerpt' }] };
  const pinned = memoryPolicy.shouldPreserveChatLocalMemoryHistory(messages, 'chat-session', false);
  assert.equal(pinned, true);
  const wire = buildChatWireMessages({ messages, maxContextLength: 100000, currentUserContent: 'next', preserveFullHistory: pinned });
  assert.equal(wire.length, 31);
  assert.equal(wire[0].content, 'raw 0');
  assert.equal(wire[0].metadata?.chat_turn_ref, 'chat:chat-session:1:user');
  assert.equal(JSON.stringify(wire).includes('frozen excerpt'), false);
  assert.equal(memoryPolicy.shouldPreserveChatLocalMemoryHistory(messages, 'another-session', false), false);
  assert.equal(memoryPolicy.shouldPreserveChatLocalMemoryHistory([], 'chat-session', false), false);
  assert.equal(memoryPolicy.shouldPreserveChatLocalMemoryHistory(messages.map((m) => ({ ...m, localMemories: undefined })), 'chat-session', false), false);
  assert.equal(memoryPolicy.shouldPreserveChatLocalMemoryHistory([], 'chat-session', true), false, 'unknown first window must use existing legacy selection');
});
