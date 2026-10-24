import assert from "node:assert/strict";
import test from "node:test";

import {
  BackendProxyInvalidPathError,
  BackendProxyBodyTooLargeError,
  MAX_VISION_JSON_REQUEST_BODY_BYTES,
  canonicalizeBackendProxyPath,
  getBackendProxyBodyLimit,
  readRequestBodyWithLimit,
} from "./backend-proxy-policy.ts";

test("proxy body policy applies the exact ceiling only to Vision JSON request paths", () => {
  assert.equal(MAX_VISION_JSON_REQUEST_BODY_BYTES, 142_606_336);
  for (const path of [
    ["v1", "chat", "completions"],
    ["v1", "agent", "chat"],
    ["v1", "responses"],
  ]) {
    assert.equal(getBackendProxyBodyLimit({
      method: "POST",
      path,
      contentType: "application/json; charset=utf-8",
    }), MAX_VISION_JSON_REQUEST_BODY_BYTES);
  }
  assert.equal(getBackendProxyBodyLimit({
    method: "POST",
    path: ["v1", "agent", "stop"],
    contentType: "application/json",
  }), null);
  for (const contentType of [null, "multipart/form-data", "text/plain"]) {
    assert.equal(getBackendProxyBodyLimit({
      method: "POST",
      path: ["v1", "agent", "chat"],
      contentType,
    }), MAX_VISION_JSON_REQUEST_BODY_BYTES, "headers cannot bypass an endpoint limit");
  }
  assert.equal(getBackendProxyBodyLimit({
    method: "GET",
    path: ["v1", "agent", "chat"],
    contentType: "application/json",
  }), null);
});

test("limited body reader rejects oversized Content-Length before reading", async () => {
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      controller.enqueue(new Uint8Array([1]));
      controller.close();
    },
  });
  const request = new Request("http://localhost/v1/agent/chat", {
    method: "POST",
    headers: { "content-length": "5" },
    body,
    duplex: "half",
  } as RequestInit & { duplex: "half" });
  let readerRequested = false;
  const getReader = request.body!.getReader.bind(request.body);
  request.body!.getReader = (...args) => {
    readerRequested = true;
    return getReader(...args);
  };

  await assert.rejects(
    readRequestBodyWithLimit(request, 4),
    BackendProxyBodyTooLargeError,
  );
  assert.equal(readerRequested, false);
});

test("limited body reader counts actual chunks when Content-Length is absent or forged", async () => {
  const request = new Request("http://localhost/v1/chat/completions", {
    method: "POST",
    headers: { "content-length": "1" },
    body: new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([1, 2, 3]));
        controller.enqueue(new Uint8Array([4, 5]));
        controller.close();
      },
    }),
    duplex: "half",
  } as RequestInit & { duplex: "half" });

  await assert.rejects(
    readRequestBodyWithLimit(request, 4),
    BackendProxyBodyTooLargeError,
  );
});

test("limited body reader accepts an exact declared and observed byte boundary", async () => {
  const request = new Request("http://localhost/v1/responses", {
    method: "POST",
    headers: { "content-length": "4" },
    body: new Uint8Array([1, 2, 3, 4]),
  });

  assert.deepEqual(
    new Uint8Array((await readRequestBodyWithLimit(request, 4)).flatMap((chunk) => Array.from(chunk))),
    new Uint8Array([1, 2, 3, 4]),
  );
});

test("proxy path canonicalization rejects literal and encoded dot segments", () => {
  assert.deepEqual(canonicalizeBackendProxyPath(["v1", "agent", "chat"]), ["v1", "agent", "chat"]);
  for (const path of [
    ["v1", "..", "health"],
    ["v1", "%2e%2e", "health"],
    ["v1", "%252e%252e", "health"],
    ["v1", "%252525252e%252525252e", "health"],
    ["v1", "%2fhealth"],
  ]) {
    assert.throws(() => canonicalizeBackendProxyPath(path), BackendProxyInvalidPathError);
  }
});

test("limited body reader cancels and releases its reader after a read error", async () => {
  let cancelled = false;
  let released = false;
  const request = {
    headers: new Headers(),
    body: {
      getReader() {
        return {
          read: async () => { throw new Error("read failed"); },
          cancel: async () => {
            cancelled = true;
          },
          releaseLock() {
            released = true;
          },
        };
      },
    },
  } as unknown as Request;

  await assert.rejects(readRequestBodyWithLimit(request, 4), /read failed/);
  assert.equal(cancelled, true);
  assert.equal(released, true);
});

test("limited body reader releases a native stream lock after success", async () => {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new Uint8Array([1]));
      controller.close();
    },
  });
  const request = new Request("http://localhost/v1/chat/completions", {
    method: "POST",
    body,
    duplex: "half",
  } as RequestInit & { duplex: "half" });

  await readRequestBodyWithLimit(request, 4);
  assert.equal(request.body!.locked, false);
});
