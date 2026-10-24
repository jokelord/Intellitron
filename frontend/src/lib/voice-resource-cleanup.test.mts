import assert from "node:assert/strict";
import { test } from "node:test";

import {
  acquireVoiceMediaAfterCleanup,
  completeVoiceActivationSetup,
  disposeVoiceResources,
  waitForVoiceReleaseBarrier,
} from "./voice-input.ts";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((finish) => { resolve = finish; });
  return { promise, resolve };
}

function resources(close: () => Promise<void>, timeoutSeconds = 1) {
  const calls: string[] = [];
  const track = {
    onended: () => undefined,
    stop: () => { calls.push("stop_track"); },
  };
  const resource: Parameters<typeof disposeVoiceResources>[0] = {
    generation: 1,
    stream: { getTracks: () => [track] } as unknown as MediaStream,
    context: { close: () => { calls.push("close_context"); return close(); } } as AudioContext,
    source: { disconnect: () => { calls.push("disconnect_source"); } } as MediaStreamAudioSourceNode,
    worklet: {
      port: {
        postMessage: (message: { type: string }) => { calls.push(message.type); },
        close: () => { calls.push("close_port"); },
      },
      disconnect: () => { calls.push("disconnect_worklet"); },
    } as unknown as AudioWorkletNode,
    sink: { disconnect: () => { calls.push("disconnect_sink"); } } as GainNode,
    socket: null,
    socketReady: false,
    closing: false,
    abortTimeoutSeconds: timeoutSeconds,
    cleanupPromise: null,
    expectedReplay: null,
    pendingReplay: null,
    revokeReplay: () => undefined,
  };
  return { resource, calls, track };
}

const nextTurn = () => new Promise<void>((resolve) => setImmediate(resolve));

test("replacement capture waits for the previous native AudioContext to close", async () => {
  const closing = deferred();
  const { resource, calls, track } = resources(() => closing.promise);
  const cleanup = waitForVoiceReleaseBarrier(
    Promise.resolve(), resource, null, 1,
    (captured, deadline) => disposeVoiceResources(captured, false, deadline),
  );
  let admitted = false;
  let requested = false;
  const replacement = { id: "replacement" };
  const acquisition = acquireVoiceMediaAfterCleanup(
    () => { requested = true; return Promise.resolve(replacement); },
    cleanup, () => true, () => undefined,
  ).then((media) => { admitted = true; return media; });
  assert.equal(requested, true, "permission must retain the synchronous user gesture");
  assert.equal(track.onended, null);
  assert.ok(calls.includes("stop_track"));
  assert.ok(calls.includes("disconnect_worklet"));
  await nextTurn();
  assert.equal(admitted, false, "replacement graph must wait for native teardown");
  closing.resolve();
  assert.strictEqual(await acquisition, replacement);
});

test("audio teardown and socket abort start concurrently and both govern cleanup", async () => {
  const closing = deferred();
  const { resource, calls } = resources(() => closing.promise);
  const listeners = new Map<string, Set<(event: { data?: unknown }) => void>>();
  const socket = {
    readyState: 1,
    send: (payload: string) => { calls.push(JSON.parse(payload).type); },
    close: () => { calls.push("close_socket"); socket.readyState = 2; },
    addEventListener: (type: string, listener: (event: { data?: unknown }) => void) => {
      const current = listeners.get(type) ?? new Set();
      current.add(listener);
      listeners.set(type, current);
    },
    removeEventListener: (type: string, listener: (event: { data?: unknown }) => void) => {
      listeners.get(type)?.delete(listener);
    },
  };
  resource.socket = socket as unknown as WebSocket;
  let finished = false;
  const cleanup = disposeVoiceResources(resource, true);
  assert.strictEqual(disposeVoiceResources(resource, true), cleanup);
  void cleanup.then(() => { finished = true; });
  assert.ok(calls.includes("abort"), "abort must not wait for the context close promise");
  for (const listener of listeners.get("message") ?? []) {
    listener({ data: JSON.stringify({ type: "aborted" }) });
  }
  await nextTurn();
  assert.equal(finished, false, "socket acknowledgment alone cannot release the audio graph");
  closing.resolve();
  await cleanup;
  assert.equal(finished, true);
  assert.equal(calls.filter((call) => call === "close_context").length, 1);
  assert.equal(calls.filter((call) => call === "abort").length, 1);
});

test("rejected native close does not reject the release barrier", async () => {
  const { resource } = resources(() => Promise.reject(new Error("already closing")));
  await assert.doesNotReject(disposeVoiceResources(resource, false));
});

test("a synchronous native close error does not skip socket teardown", async () => {
  const { resource, calls } = resources(() => { throw new Error("native close failed"); });
  resource.socket = { close: () => { calls.push("close_socket"); } } as WebSocket;
  await assert.doesNotReject(async () => disposeVoiceResources(resource, false));
  assert.ok(calls.includes("close_socket"));
});

test("the socket acknowledgment still gates cleanup after native audio closes", async () => {
  const { resource } = resources(() => Promise.resolve());
  const listeners = new Map<string, Set<(event: { data?: unknown }) => void>>();
  resource.socket = {
    readyState: 1,
    send: () => undefined,
    close: () => undefined,
    addEventListener: (type: string, listener: (event: { data?: unknown }) => void) => {
      const current = listeners.get(type) ?? new Set();
      current.add(listener);
      listeners.set(type, current);
    },
    removeEventListener: (type: string, listener: (event: { data?: unknown }) => void) => {
      listeners.get(type)?.delete(listener);
    },
  } as unknown as WebSocket;
  let finished = false;
  const cleanup = disposeVoiceResources(resource, true);
  void cleanup.then(() => { finished = true; });
  await nextTurn();
  assert.equal(finished, false);
  for (const listener of listeners.get("message") ?? []) {
    listener({ data: JSON.stringify({ type: "aborted" }) });
  }
  await cleanup;
  assert.equal(finished, true);
});

test("hung native close releases at the existing shared cleanup deadline", async () => {
  const closing = deferred();
  const { resource } = resources(() => closing.promise, 10);
  let finished = false;
  const started = Date.now();
  const cleanup = disposeVoiceResources(resource, false, started + 30);
  void cleanup.then(() => { finished = true; });
  await nextTurn();
  assert.equal(finished, false);
  await cleanup;
  assert.ok(Date.now() - started >= 20, "must wait for native close or its deadline");
  assert.ok(Date.now() - started < 1000, "must not extend the shared deadline");
  closing.resolve();
});

test("stale activation setup awaits only its captured native context cleanup", async () => {
  const setup = deferred();
  const closing = deferred();
  const { resource: old, calls } = resources(() => closing.promise);
  const { resource: current } = resources(() => Promise.resolve());
  let active = old;
  let finished = false;
  const completed = completeVoiceActivationSetup(
    old, () => setup.promise, () => active === old,
    (captured) => disposeVoiceResources(captured, false),
  ).then((result) => { finished = true; return result; });
  active = current;
  setup.resolve();
  await nextTurn();
  assert.equal(finished, false);
  assert.ok(calls.includes("close_context"));
  assert.equal(current.closing, false);
  closing.resolve();
  assert.equal(await completed, false);
  assert.strictEqual(active, current);
});

test("stale setup cleanup without a supplied deadline bounds a hung native close", async () => {
  const closing = deferred();
  const { resource } = resources(() => closing.promise, 0.03);
  let finished = false;
  const started = Date.now();
  const completed = completeVoiceActivationSetup(
    resource, () => Promise.resolve(), () => false,
    (captured) => disposeVoiceResources(captured, false),
  ).then((result) => { finished = true; return result; });
  await nextTurn();
  assert.equal(finished, false);
  assert.equal(await completed, false);
  assert.ok(Date.now() - started >= 20);
  assert.ok(Date.now() - started < 1000);
  closing.resolve();
});

test("full teardown waits concurrently for a previously retired transport and native context", async () => {
  const transport = deferred();
  const audio = deferred();
  const { resource } = resources(() => audio.promise);
  resource.transportCleanup = transport.promise;
  let finished = false;
  const cleanup = disposeVoiceResources(resource, true);
  void cleanup.then(() => { finished = true; });
  audio.resolve();
  await nextTurn();
  assert.equal(finished, false, "native close alone must not release pending old transport");
  transport.resolve();
  await cleanup;
  assert.equal(finished, true);
});

test("a rejected previously retired transport cannot strand full teardown", async () => {
  const { resource } = resources(() => Promise.resolve());
  resource.transportCleanup = Promise.reject(new Error("old transport failed"));
  await assert.doesNotReject(disposeVoiceResources(resource, true));
});
