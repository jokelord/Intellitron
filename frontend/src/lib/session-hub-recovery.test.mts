import assert from 'node:assert/strict';
import test from 'node:test';

import {
  SessionHubBookmarkTransactionRegistry,
  SessionHubLiveAttachmentRegistry,
  SessionHubRecoveryCoordinator,
  clearSessionHubBookmark,
  isCurrentSessionHubAttachment,
  readSessionHubBookmark,
  resolveSessionHubClearTarget,
  selectSessionHubRecoveryCandidate,
  writeSessionHubBookmark,
  type SessionHubBookmarkStorage,
} from './session-hub-recovery.ts';
import type { SessionHubPayloadHandlers, SessionHubReadResult } from './session-hub-client.ts';
import { clearRetainedAgentSessionBeforeDelete } from './agent-session-recovery.ts';

class StorageFake implements SessionHubBookmarkStorage {
  readonly values = new Map<string, string>();
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}

test('unified bookmarks contain exactly the five versioned fields for Chat and Agent', () => {
  const storage = new StorageFake();
  for (const [pageMode, interactionMode, sessionId] of [
    ['text', 'chat', 'text-chat'],
    ['text', 'agent', 'text-agent'],
    ['vision', 'chat', 'vision-chat'],
    ['vision', 'agent', 'vision-agent'],
  ] as const) {
    const bookmark = writeSessionHubBookmark({
      sessionId, pageMode, interactionMode, createdAt: 123,
    }, storage);
    assert.deepEqual(Object.keys(bookmark).sort(), [
      'createdAt', 'interactionMode', 'pageMode', 'schemaVersion', 'sessionId',
    ]);
    assert.deepEqual(readSessionHubBookmark(pageMode, storage), bookmark);
    assert.doesNotMatch(JSON.stringify(bookmark), /request|cursor|content|image|timeline|controller/i);
  }
});

test('legacy migration candidates survive transient recovery and clear only after canonical success', async () => {
  const storage = new StorageFake();
  storage.setItem('llm-agent-session-bookmark:vision', JSON.stringify({
    schemaVersion: 1, sessionId: 'legacy-agent', pageMode: 'vision', createdAt: 7,
  }));
  const candidate = selectSessionHubRecoveryCandidate('vision', storage);
  assert.equal(candidate?.bookmark.interactionMode, 'agent');
  assert.equal(candidate?.legacyStorageKey, 'llm-agent-session-bookmark:vision');

  const coordinator = new SessionHubRecoveryCoordinator();
  await coordinator.attach(candidate!.bookmark, async () => ({
    kind: 'detached', lastSequence: 0, error: new Error('backend unavailable'),
  }), { onBusy: () => undefined });
  assert.notEqual(storage.getItem(candidate!.legacyStorageKey!), null);

  candidate!.commitMigration(storage);
  assert.equal(storage.getItem(candidate!.legacyStorageKey!), null);
  assert.deepEqual(readSessionHubBookmark('vision', storage), candidate!.bookmark);
});

test('agent bookmarks migrate only the historical prefixed canonical UUID', () => {
  const storage = new StorageFake();
  storage.setItem('llm-session-hub-bookmark:text', JSON.stringify({
    schemaVersion: 1,
    sessionId: 'agent-123e4567-e89b-42d3-a456-426614174000',
    pageMode: 'text',
    interactionMode: 'agent',
    createdAt: 10,
  }));

  assert.equal(
    readSessionHubBookmark('text', storage)?.sessionId,
    '123e4567-e89b-42d3-a456-426614174000',
  );

  storage.setItem('llm-agent-session-bookmark:vision', JSON.stringify({
    schemaVersion: 1,
    sessionId: 'agent-not-a-uuid',
    pageMode: 'vision',
    createdAt: 11,
  }));
  assert.equal(
    selectSessionHubRecoveryCandidate('vision', storage)?.bookmark.sessionId,
    'agent-not-a-uuid',
  );
});

test('legacy Chat transcript contributes only its lightweight resume descriptor until recovery succeeds', () => {
  const storage = new StorageFake();
  storage.setItem('llm-chat-transcript:text', JSON.stringify({
    schemaVersion: 1,
    savedAt: 99,
    messages: [
      { id: 'user', role: 'user', content: 'large historical text', timestamp: '2026-01-01' },
      {
        id: 'assistant', role: 'assistant', content: 'partial', timestamp: '2026-01-01',
        chatResume: { sessionId: 'legacy-chat' },
      },
    ],
  }));
  const candidate = selectSessionHubRecoveryCandidate('text', storage);
  assert.deepEqual(candidate?.bookmark, {
    schemaVersion: 1,
    sessionId: 'legacy-chat',
    pageMode: 'text',
    interactionMode: 'chat',
    createdAt: 99,
  });
  assert.doesNotMatch(JSON.stringify(candidate?.bookmark), /large historical text|partial/);
  assert.notEqual(storage.getItem('llm-chat-transcript:text'), null);
  candidate?.commitMigration(storage);
  assert.equal(storage.getItem('llm-chat-transcript:text'), null);
});

test('duplicate restore is single-flight and stale callbacks cannot mutate the replacement attachment', async () => {
  const coordinator = new SessionHubRecoveryCoordinator();
  const bookmark = {
    schemaVersion: 1 as const,
    sessionId: 'session-a',
    pageMode: 'text' as const,
    interactionMode: 'chat' as const,
    createdAt: 1,
  };
  let opens = 0;
  let release!: (value: SessionHubReadResult) => void;
  let captured!: SessionHubPayloadHandlers;
  const connect = async (_controller: AbortController, handlers: SessionHubPayloadHandlers) => {
    opens += 1;
    captured = handlers;
    return new Promise<SessionHubReadResult>((resolve) => { release = resolve; });
  };
  const applied: number[] = [];
  const options = { onBusy: () => undefined, handlers: {
    onPayload: (payload: { event_sequence: number }) => { applied.push(payload.event_sequence); },
  } };
  const first = coordinator.attach(bookmark, connect, options);
  const duplicate = coordinator.attach(bookmark, connect, options);
  assert.equal(first, duplicate);
  assert.equal(opens, 1);

  coordinator.detach();
  const replacement = coordinator.attach({ ...bookmark, sessionId: 'session-b' }, async () => (
    { kind: 'terminal', lastSequence: 1 }
  ), options);
  await captured.onPayload?.({ event_sequence: 1, type: 'content' }, { matchesExpectedRequest: true });
  assert.deepEqual(applied, []);
  release({ kind: 'detached', lastSequence: 0 });
  await first;
  await replacement;
});

test('Trash clears only the unified bookmark for its page', () => {
  const storage = new StorageFake();
  writeSessionHubBookmark({ sessionId: 'a', pageMode: 'text', interactionMode: 'chat', createdAt: 1 }, storage);
  writeSessionHubBookmark({ sessionId: 'b', pageMode: 'vision', interactionMode: 'agent', createdAt: 2 }, storage);
  clearSessionHubBookmark('text', storage);
  assert.equal(readSessionHubBookmark('text', storage), null);
  assert.equal(readSessionHubBookmark('vision', storage)?.sessionId, 'b');
});

test('provisional bookmark survives ambiguous detach and acceptance-before-browser-close', () => {
  const storage = new StorageFake();
  const transactions = new SessionHubBookmarkTransactionRegistry();
  const transaction = transactions.begin({
    sessionId: 'accepted-before-close', pageMode: 'vision', interactionMode: 'chat', createdAt: 10,
  }, storage);
  assert.deepEqual(readSessionHubBookmark('vision', storage), transaction.bookmark);
  assert.equal(transactions.retain(transaction), true);
  assert.deepEqual(readSessionHubBookmark('vision', storage), transaction.bookmark);
  assert.equal(transactions.rollback(transaction, storage), false, 'stale detach owner cannot erase recovery handle');
});

test('exact preaccept rejection rolls back only its current provisional owner', () => {
  const storage = new StorageFake();
  const prior = writeSessionHubBookmark({
    sessionId: 'accepted-existing', pageMode: 'text', interactionMode: 'chat', createdAt: 1,
  }, storage);
  const transactions = new SessionHubBookmarkTransactionRegistry();
  const rejected = transactions.begin({
    sessionId: 'accepted-existing', pageMode: 'text', interactionMode: 'chat', createdAt: 2,
  }, storage);
  assert.equal(transactions.rollback(rejected, storage), true);
  assert.deepEqual(readSessionHubBookmark('text', storage), prior, 'same-session 409 must preserve accepted bookmark');

  const stale = transactions.begin({
    sessionId: 'stale', pageMode: 'text', interactionMode: 'agent', createdAt: 3,
  }, storage);
  const current = transactions.begin({
    sessionId: 'current', pageMode: 'text', interactionMode: 'agent', createdAt: 4,
  }, storage);
  assert.equal(transactions.rollback(stale, storage), false);
  assert.equal(readSessionHubBookmark('text', storage)?.sessionId, 'current');
  assert.equal(transactions.accept(current), true);
  assert.equal(transactions.rollback(current, storage), false);
});

test('only the current live attachment may finalize a detached Chat subscriber', () => {
  const attachments = new SessionHubLiveAttachmentRegistry();
  const old = attachments.attach('chat-old');
  const current = attachments.attach('chat-current');
  const finalized: string[] = [];
  assert.equal(attachments.detach(old, () => finalized.push('old')), false);
  assert.equal(attachments.detach(current, () => finalized.push('current')), true);
  assert.deepEqual(finalized, ['current']);
});

test('detached lifecycle ownership is rechecked after an async callback replaces the attachment', () => {
  const attachments = new SessionHubLiveAttachmentRegistry();
  const old = attachments.attach('old');
  assert.equal(isCurrentSessionHubAttachment(old, attachments), true);
  const replacement = attachments.attach('replacement');
  assert.equal(isCurrentSessionHubAttachment(old, attachments), false);
  assert.equal(isCurrentSessionHubAttachment(replacement, attachments), true);
  attachments.detach(replacement, () => undefined);
  assert.equal(isCurrentSessionHubAttachment(replacement, attachments), false);
});

test('Agent to Chat Clear deletes exactly the current canonical Chat Hub, not stale Agent ownership', async () => {
  const target = resolveSessionHubClearTarget({
    bookmarkSessionId: 'agent-old',
    attachedSessionId: 'chat-current',
    staleBoundSessionId: 'agent-old',
  });
  assert.equal(target, 'chat-current');
  const deleted: string[] = [];
  clearRetainedAgentSessionBeforeDelete({
    boundSessionId: target,
    bookmarkSessionId: undefined,
    clearBookmark: () => undefined,
    detachLocal: () => undefined,
    deleteSession: async (sessionId) => { deleted.push(sessionId); },
    onError: (error) => { throw error; },
  });
  await Promise.resolve();
  assert.deepEqual(deleted, ['chat-current']);
  assert.equal(resolveSessionHubClearTarget({
    bookmarkSessionId: undefined,
    attachedSessionId: undefined,
    staleBoundSessionId: 'agent-fallback',
  }), 'agent-fallback');
});

test('producer error displays once but does not release live ownership before done', () => {
  const attachments = new SessionHubLiveAttachmentRegistry();
  const attachment = attachments.attach('chat-error');
  const events: string[] = [];
  assert.equal(attachments.applyError(attachment, () => events.push('error')), true);
  assert.equal(attachments.applyError(attachment, () => events.push('duplicate')), false);
  assert.equal(attachments.isCurrent(attachment), true, 'error is not terminal');
  assert.equal(attachments.detach(attachment, () => events.push('done')), true);
  assert.deepEqual(events, ['error', 'done']);
});

test('stale recovery completion cannot mutate the replacement attachment', async () => {
  const coordinator = new SessionHubRecoveryCoordinator();
  const firstBookmark = {
    schemaVersion: 1 as const, sessionId: 'old', pageMode: 'text' as const,
    interactionMode: 'chat' as const, createdAt: 1,
  };
  let resolveOld!: (result: SessionHubReadResult) => void;
  const old = coordinator.attach(firstBookmark, async () => new Promise((resolve) => {
    resolveOld = resolve;
  }), { handlers: {}, onBusy: () => undefined });
  const busy: boolean[] = [];
  const replacement = coordinator.attach({ ...firstBookmark, sessionId: 'new' }, async () => ({
    kind: 'terminal', lastSequence: 1,
  }), { handlers: {}, onBusy: (value) => busy.push(value) });
  resolveOld({ kind: 'detached', lastSequence: 1, error: new Error('stale') });
  const oldResult = await old;
  await replacement;
  assert.equal(oldResult.kind, 'detached');
  assert.deepEqual(busy, [true, false]);
});

test('recovery settlement publishes failure while its attachment is still owned',async()=>{
  const coordinator=new SessionHubRecoveryCoordinator();
  let token:ReturnType<typeof coordinator.currentAttachmentToken>=null;
  const settlements:string[]=[];
  const pending=coordinator.attach({schemaVersion:1,sessionId:'failed-recovery',pageMode:'text',interactionMode:'chat',createdAt:1},async()=>({kind:'detached',lastSequence:0,error:new Error('offline')}),{
    handlers:{},onBusy:()=>{},onSettled:result=>{
      assert.ok(token);assert.equal(coordinator.isCurrentAttachment(token),true);
      settlements.push(result.kind);
    },
  });
  token=coordinator.currentAttachmentToken();await pending;
  assert.deepEqual(settlements,['detached']);assert.equal(coordinator.isCurrentAttachment(token!),false);
});
