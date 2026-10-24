import assert from "node:assert/strict";
import { test } from "node:test";

import {
  clearAgentSessionBookmark,
  clearRetainedAgentSessionBeforeDelete,
  classifyAgentSessionRecoveryError,
  detachRetainedAgentSessionSubscriber,
  readAgentSessionBookmark,
  writeAgentSessionBookmark,
  type SessionBookmarkStorage,
} from "./agent-session-recovery.ts";

class StorageFake implements SessionBookmarkStorage {
  readonly values = new Map<string, string>();
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}

test("agent session bookmarks persist only the versioned Text/Vision descriptor", () => {
  const storage = new StorageFake();
  writeAgentSessionBookmark({ sessionId: "session-text", pageMode: "text", createdAt: 100 }, storage);
  writeAgentSessionBookmark({ sessionId: "session-vision", pageMode: "vision", createdAt: 200 }, storage);

  assert.deepEqual(readAgentSessionBookmark("text", storage), {
    schemaVersion: 1,
    sessionId: "session-text",
    pageMode: "text",
    createdAt: 100,
  });
  assert.deepEqual(readAgentSessionBookmark("vision", storage), {
    schemaVersion: 1,
    sessionId: "session-vision",
    pageMode: "vision",
    createdAt: 200,
  });
  assert.equal([...storage.values.values()].some((value) => /request|content|image|event/i.test(value)), false);

  clearAgentSessionBookmark("text", storage);
  assert.equal(readAgentSessionBookmark("text", storage), null);
  assert.equal(readAgentSessionBookmark("vision", storage)?.sessionId, "session-vision");
});

test("malformed bookmarks are rejected without retaining arbitrary fields", () => {
  const storage = new StorageFake();
  storage.setItem("llm-agent-session-bookmark:text", JSON.stringify({
    schemaVersion: 1,
    sessionId: "session-text",
    pageMode: "vision",
    createdAt: 1,
    requestId: "must-not-survive",
  }));

  assert.equal(readAgentSessionBookmark("text", storage), null);
  assert.equal(storage.getItem("llm-agent-session-bookmark:text"), null);
});

test("completion, failure, Stop, and Answer Now leave the bookmark until Trash clears it", () => {
  const storage = new StorageFake();
  writeAgentSessionBookmark({ sessionId: "session-1", pageMode: "vision", createdAt: 1 }, storage);

  for (const terminalAction of ["complete", "failure", "stop", "answer-now"]) {
    assert.equal(readAgentSessionBookmark("vision", storage)?.sessionId, "session-1", terminalAction);
  }

  let deleteObservedBookmark: string | null | undefined;
  clearAgentSessionBookmark("vision", storage);
  deleteObservedBookmark = readAgentSessionBookmark("vision", storage)?.sessionId;
  assert.equal(deleteObservedBookmark, undefined);
});

test("Trash clears the retained bookmark before deleting its exact unattached session even when local cleanup rejects", async () => {
  const order: string[] = [];
  const errors: string[] = [];

  clearRetainedAgentSessionBeforeDelete({
    boundSessionId: undefined,
    bookmarkSessionId: "bookmark-session",
    clearBookmark: () => order.push("clear"),
    detachLocal: async () => { order.push("detach"); throw new Error("camera already closed"); },
    deleteSession: async (sessionId) => { order.push(`delete:${sessionId}`); throw new Error("network unavailable"); },
    onError: (error) => errors.push(error instanceof Error ? error.message : String(error)),
  });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.deepEqual(order, ["clear", "detach", "delete:bookmark-session"]);
  assert.deepEqual(errors.sort(), ["camera already closed", "network unavailable"]);
});

test("recovery failures retain transient bookmarks and expose stale dismissal only for proven expiry", () => {
  assert.deepEqual(classifyAgentSessionRecoveryError(new Error("Agent session is unavailable or expired")), {
    kind: "expired",
    message: "Saved Agent session has expired.",
  });
  assert.deepEqual(classifyAgentSessionRecoveryError(new Error("Agent session event sequence gap: expected 3, received 5")), {
    kind: "transient",
    message: "Agent session event sequence gap: expected 3, received 5",
  });
});

test("a nonterminal Answer Now EOF detaches only its current local subscriber and preserves the bookmark", () => {
  const storage = new StorageFake();
  writeAgentSessionBookmark({ sessionId: "session-1", pageMode: "vision", createdAt: 1 }, storage);
  const calls: string[] = [];

  assert.equal(detachRetainedAgentSessionSubscriber({
    isCurrentTransport: true,
    boundSessionId: "session-1",
    detachedSessionId: "session-1",
    detachLocal: () => calls.push("detach"),
  }), true);
  assert.deepEqual(calls, ["detach"]);
  assert.equal(readAgentSessionBookmark("vision", storage)?.sessionId, "session-1");

  assert.equal(detachRetainedAgentSessionSubscriber({
    isCurrentTransport: false,
    boundSessionId: "session-2",
    detachedSessionId: "session-1",
    detachLocal: () => calls.push("stale-detach"),
  }), false);
  assert.deepEqual(calls, ["detach"]);
});
