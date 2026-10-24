import assert from 'node:assert/strict';
import test from 'node:test';

import { createChatSessionHandlers } from './chat-session-handlers.ts';
import { buildChatWireMessages } from './message-types.ts';

test('Chat projection reconstructs one turn and applies live/replay deltas idempotently', async () => {
  const handlers = createChatSessionHandlers({ now: () => new Date(123) });
  const payloads = [
    {
      event_sequence: 1,
      type: 'timeline_event',
      event: {
        kind: 'turn_started',
        source: 'chat',
        session_id: 'session-1',
        turn_index: 2,
        user_content: 'hello',
        images: [],
        message_id: 'chat:session-1:2:assistant',
      },
    },
    {
      event_sequence: 2,
      type: 'timeline_event',
      event: {
        kind: 'message_delta', source: 'chat', display: 'chat_bubble',
        message_id: 'chat:session-1:2:assistant', text: 'A', partial: true, reasoning: false,
      },
    },
    {
      event_sequence: 3,
      type: 'timeline_event',
      event: {
        kind: 'message_delta', source: 'chat', display: 'chat_bubble',
        message_id: 'chat:session-1:2:assistant', text: 'R', partial: true, reasoning: true,
      },
    },
    {
      event_sequence: 4,
      type: 'timeline_event',
      event: {
        kind: 'message_delta', source: 'chat', display: 'chat_bubble',
        message_id: 'chat:session-1:2:assistant', text: 'B', partial: true, reasoning: false,
      },
    },
    { event_sequence: 5, type: 'done', session_id: 'session-1', status: 'completed' },
  ] as const;

  for (const payload of payloads) await handlers.onPayload(payload);
  for (const payload of payloads) await handlers.onPayload(payload);

  const messages = handlers.getMessages();
  assert.equal(messages.length, 2);
  assert.deepEqual(messages.map((message) => message.id), [
    'chat:session-1:2:user',
    'chat:session-1:2:assistant',
  ]);
  assert.equal(messages[0].content, 'hello');
  assert.equal(messages[1].content, 'AB');
  assert.equal(messages[1].reasoning_content, 'R');
  assert.equal(messages[1].isStreaming, false);
});

test('Chat projection ignores non-chat and non-bubble message deltas', async () => {
  const handlers = createChatSessionHandlers();
  await handlers.onPayload({
    event_sequence: 1,
    type: 'timeline_event',
    event: {
      kind: 'turn_started', session_id: 'session-1', turn_index: 1,
      user_content: 'hello', images: [], message_id: 'chat:session-1:1:assistant',
    },
  });
  await handlers.onPayload({
    event_sequence: 2,
    type: 'timeline_event',
    event: {
      kind: 'message_delta', source: 'future_agent', display: 'chat_bubble',
      message_id: 'chat:session-1:1:assistant', text: 'must stay hidden', partial: true,
    },
  });

  assert.equal(handlers.getMessages()[1].content, '');
});

test('Chat turn reconstruction preserves user text and the historical-image placeholder idempotently', async () => {
  const handlers = createChatSessionHandlers();
  const payload = {
    event_sequence: 1,
    type: 'timeline_event',
    event: {
      kind: 'turn_started',
      session_id: 'session-1',
      turn_index: 1,
      user_content: 'describe the previous capture',
      images: [],
      historical_image_placeholder: '[Historical Vision Automation image block omitted]',
      message_id: 'chat:session-1:1:assistant',
    },
  } as const;

  await handlers.onPayload(payload);
  await handlers.onPayload(payload);

  assert.deepEqual(handlers.getMessages()[0].content, [
    { type: 'text', text: 'describe the previous capture' },
    { type: 'text', text: '[Historical Vision Automation image block omitted]' },
  ]);
  assert.equal(handlers.getMessages().length, 2);
});

test('Chat turn reconstruction retains remaining images before text and omission placeholder', async () => {
  const handlers = createChatSessionHandlers();
  await handlers.onPayload({
    event_sequence: 4,
    type: 'timeline_event',
    event: {
      kind: 'turn_started',
      session_id: 'session-1',
      turn_index: 2,
      user_content: 'latest capture',
      images: ['data:image/png;base64,LATEST'],
      historical_image_placeholder: '[Earlier automated image block omitted]',
      message_id: 'chat:session-1:2:assistant',
    },
  });

  const user = handlers.getMessages()[0];
  assert.deepEqual(user.content, [
    { type: 'image_url', image_url: { url: 'data:image/png;base64,LATEST' } },
    { type: 'text', text: 'latest capture' },
    { type: 'text', text: '[Earlier automated image block omitted]' },
  ]);
  assert.deepEqual(user.images, ['data:image/png;base64,LATEST']);
});

test('Chat recovery keeps two automated historical turns as placeholders and only the latest images real', async () => {
  const handlers = createChatSessionHandlers();
  for (const payload of [
    {
      event_sequence: 1, type: 'timeline_event', event: {
        kind: 'turn_started', session_id: 'automation-chat', turn_index: 1,
        user_content: 'first automated turn', images: [],
        historical_image_placeholder: '[historical images omitted: count=2; sources=desktop]',
      },
    },
    { event_sequence: 2, type: 'done', session_id: 'automation-chat' },
    {
      event_sequence: 3, type: 'timeline_event', event: {
        kind: 'turn_started', session_id: 'automation-chat', turn_index: 2,
        user_content: 'second automated turn', images: [],
        historical_image_placeholder: '[historical images omitted: count=3; sources=desktop]',
      },
    },
    { event_sequence: 4, type: 'done', session_id: 'automation-chat' },
    {
      event_sequence: 5, type: 'timeline_event', event: {
        kind: 'turn_started', session_id: 'automation-chat', turn_index: 3,
        user_content: 'latest automated turn', images: ['data:image/jpeg;base64,LATEST'],
      },
    },
    { event_sequence: 6, type: 'done', session_id: 'automation-chat' },
  ]) await handlers.onPayload(payload);

  const wire = buildChatWireMessages({
    messages: [...handlers.getMessages()], maxContextLength: 100_000,
    currentUserContent: 'next', preserveFullHistory: true,
  });
  const serialized = JSON.stringify(wire);
  assert.match(serialized, /count=2/);
  assert.match(serialized, /count=3/);
  assert.match(serialized, /base64,LATEST/);
});

test('non-automation Chat recovery retains every historical image', async () => {
  const handlers = createChatSessionHandlers();
  await handlers.onPayload({
    event_sequence: 1, type: 'timeline_event', event: {
      kind: 'turn_started', session_id: 'ordinary-chat', turn_index: 1,
      user_content: 'ordinary one', images: ['data:image/jpeg;base64,ONE'],
    },
  });
  await handlers.onPayload({ event_sequence: 2, type: 'done', session_id: 'ordinary-chat' });
  await handlers.onPayload({
    event_sequence: 3, type: 'timeline_event', event: {
      kind: 'turn_started', session_id: 'ordinary-chat', turn_index: 2,
      user_content: 'ordinary two', images: ['data:image/jpeg;base64,TWO'],
    },
  });
  const wire = JSON.stringify(buildChatWireMessages({
    messages: [...handlers.getMessages()], maxContextLength: 100_000,
    currentUserContent: 'next', preserveFullHistory: true,
  }));
  assert.match(wire, /base64,ONE/);
  assert.match(wire, /base64,TWO/);
});
