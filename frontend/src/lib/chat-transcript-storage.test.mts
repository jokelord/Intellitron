import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CHAT_TRANSCRIPT_SCHEMA_VERSION,
  clearChatTranscript,
  readChatTranscript,
  writeChatTranscript,
  type ChatTranscriptStorage,
  type PersistedChatMessage,
} from "./chat-transcript-storage.ts";

class StorageFake implements ChatTranscriptStorage {
  readonly values = new Map<string, string>();
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}

class QuotaFake extends StorageFake {
  private readonly failFirstWrites: number;
  private writes = 0;
  constructor(failFirstWrites: number) {
    super();
    this.failFirstWrites = failFirstWrites;
  }
  setItem(key: string, value: string): void {
    this.writes += 1;
    if (this.writes <= this.failFirstWrites) {
      throw new DOMException("quota exceeded", "QuotaExceededError");
    }
    this.values.set(key, value);
  }
}

const sampleMessage = () => ({
  id: "msg-1",
  role: "user",
  content: "你好",
  timestamp: new Date("2026-08-25T10:00:00Z"),
  isStreaming: true,
  voiceReplay: { url: "blob:dead", transcript: "你好" },
});

test("chat transcript round-trips messages, revives dates, and drops runtime-only fields", () => {
  const storage = new StorageFake();
  writeChatTranscript("text", [sampleMessage()] as never[], storage);

  const restored = readChatTranscript("text", storage);
  assert.ok(restored);
  assert.equal(restored.schemaVersion, CHAT_TRANSCRIPT_SCHEMA_VERSION);
  assert.equal(restored.messages.length, 1);
  const first = restored.messages[0] as PersistedChatMessage & {
    timestamp: Date;
    voiceReplay?: unknown;
  };
  assert.equal(first.role, "user");
  assert.ok(first.timestamp instanceof Date);
  assert.equal(first.isStreaming, false);
  assert.equal(first.voiceReplay, undefined);
});

test("chat transcript read rejects foreign or mismatched payloads and clears them", () => {
  for (const raw of ["not-json", JSON.stringify({ schemaVersion: 99, savedAt: 1, messages: [] })]) {
    const storage = new StorageFake();
    storage.setItem("llm-chat-transcript:text", raw);
    assert.equal(readChatTranscript("text", storage), null);
    assert.equal(storage.getItem("llm-chat-transcript:text"), null);
  }
});

test("chat transcript write falls back to the newest half on quota errors", () => {
  const storage = new QuotaFake(1);
  const messages = Array.from({ length: 8 }, (_, index) => ({
    id: `msg-${index}`,
    role: "user",
    content: `m${index}`,
    timestamp: new Date(2026, 7, 25, 10, index),
  }));
  writeChatTranscript("text", messages as never[], storage);

  const restored = readChatTranscript("text", storage);
  assert.ok(restored);
  assert.deepEqual(
    restored.messages.map((message) => message.id),
    ["msg-4", "msg-5", "msg-6", "msg-7"],
  );
});

test("chat transcript clear removes the stored payload", () => {
  const storage = new StorageFake();
  writeChatTranscript("vision", [], storage);
  clearChatTranscript("vision", storage);
  assert.equal(readChatTranscript("vision", storage), null);
});

test('accepted Chat opaque references survive local transcript persistence and remain transport metadata', async () => {
  const { buildChatWireMessages } = await import('./message-types.ts');
  const { buildCompactRequestBody } = await import('./compact-client.ts');
  const storage = new StorageFake();
  const message = { ...sampleMessage(), id: 'chat:session-persisted:7:user', chatTurnRef: 'chat:session-persisted:7:user', localMemories: [{ text: 'display-only excerpt' }] };
  writeChatTranscript('text', [message] as never[], storage);
  const restored = readChatTranscript('text', storage)!;
  assert.equal(restored.messages[0].chatTurnRef, 'chat:session-persisted:7:user');
  const wire = buildChatWireMessages({ messages: restored.messages, maxContextLength: 100000, currentUserContent: 'next' });
  assert.equal(wire[0].metadata?.chat_turn_ref, 'chat:session-persisted:7:user');
  assert.equal(JSON.stringify(wire).includes('display-only excerpt'), false);
  const compact = buildCompactRequestBody({ agentMode: false, sessionId: 'session-persisted', messages: restored.messages });
  assert.equal(compact.messages[0].metadata?.chat_turn_ref, 'chat:session-persisted:7:user');
  assert.equal(JSON.stringify(compact).includes('display-only excerpt'), false);
});
