import assert from "node:assert/strict";
import { test } from "node:test";

import { captureBrowserFrame, createLeasedCaptureSource, raceWithAbort } from "./capture-source.ts";
import type { MediaStreamLease } from "./media-source-registry.ts";
import type { ImageResource, SourceDescriptor } from "./types.ts";

const descriptor: SourceDescriptor = {
  sourceId: "camera-source",
  kind: "camera",
  stableAlias: "camera_1",
  label: "Camera 1",
  deviceId: "camera-device",
};

function resource(): ImageResource {
  return {
    uploadBlob: new Blob(["jpeg"], { type: "image/jpeg" }),
    thumbnailBlob: null,
    disposed: false,
    acquireObjectURL() { throw new Error("unused"); },
    releaseObjectURL() { return false; },
    dispose() {
      this.disposed = true;
      this.uploadBlob = null;
    },
  };
}

test("leased capture source emits ended, captures frames, and releases once", async () => {
  const endedListeners = new Set<() => void>();
  let releaseCount = 0;
  const stream = {
    getTracks: () => [{
      addEventListener: (_type: string, listener: () => void) => endedListeners.add(listener),
      removeEventListener: (_type: string, listener: () => void) => endedListeners.delete(listener),
    }],
  } as unknown as MediaStream;
  const lease: MediaStreamLease = {
    ownerId: "automation",
    sourceKey: "camera:camera-device",
    stream,
    release() { releaseCount += 1; },
  };
  const captured = resource();
  const source = createLeasedCaptureSource({
    descriptor,
    lease,
    captureFrame: async () => captured,
  });
  let ended = 0;
  source.onEnded(() => { ended += 1; });

  const frame = await source.capture(new AbortController().signal);
  assert.strictEqual(frame.resource, captured);
  assert.deepEqual(frame.source, descriptor);
  endedListeners.forEach((listener) => listener());
  assert.equal(ended, 1);
  source.dispose();
  source.dispose();
  assert.equal(releaseCount, 1);
  assert.equal(endedListeners.size, 0);
  await assert.rejects(() => source.capture(new AbortController().signal), /disposed/);
});

test("leased capture rejects promptly on Abort and disposes a late ignored result", async () => {
  let resolveFrame!: (value: ImageResource) => void;
  let releases = 0;
  const lease: MediaStreamLease = {
    ownerId: "automation",
    sourceKey: "camera:camera-device",
    stream: { getTracks: () => [] } as unknown as MediaStream,
    release() { releases += 1; },
  };
  const late = resource();
  const source = createLeasedCaptureSource({
    descriptor,
    lease,
    captureFrame: () => new Promise((resolve) => { resolveFrame = resolve; }),
  });
  const controller = new AbortController();
  const capture = source.capture(controller.signal);
  const observed = capture.then(
    () => "fulfilled",
    (error: unknown) => error instanceof DOMException ? error.name : "rejected",
  );

  controller.abort(new DOMException("stop now", "AbortError"));
  assert.equal(await observed, "AbortError");
  source.dispose();
  assert.equal(releases, 1);

  resolveFrame(late);
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(late.disposed, true);
});

test("raceWithAbort rejects a pending browser wait without waiting for its promise", async () => {
  let resolveWait!: () => void;
  const wait = new Promise<void>((resolve) => { resolveWait = resolve; });
  const controller = new AbortController();
  const raced = raceWithAbort(wait, controller.signal);
  controller.abort(new DOMException("video aborted", "AbortError"));
  await assert.rejects(raced, /video aborted/);
  resolveWait();
});

test("browser capture uses available video data without waiting for a frame callback", async () => {
  const originalDocument = globalThis.document;
  const controller = new AbortController();
  let frameCallbackRequests = 0;
  const video = {
    videoWidth: 640,
    videoHeight: 480,
    currentFrame: "available-frame",
    muted: false,
    playsInline: false,
    srcObject: null as MediaStream | null,
    play: async () => undefined,
    pause: () => undefined,
    requestVideoFrameCallback: (_callback: () => void) => {
      frameCallbackRequests += 1;
      return 9;
    },
    cancelVideoFrameCallback: (_id: number) => undefined,
  };
  globalThis.document = {
    createElement(tag: string) {
      if (tag === "video") return video;
      if (tag !== "canvas") throw new Error(`Unexpected element ${tag}`);
      let painted = "";
      return {
        width: 0,
        height: 0,
        get currentFrame() { return painted; },
        getContext: () => ({ drawImage: (source: { currentFrame: string }) => { painted = source.currentFrame; } }),
        toBlob: (callback: (blob: Blob) => void) => callback(new Blob([painted], { type: "image/jpeg" })),
      };
    },
  } as unknown as Document;
  const capture = captureBrowserFrame({
    stream: { getTracks: () => [] } as unknown as MediaStream,
    descriptor,
    signal: controller.signal,
  }, { maxDimensionPx: 640, thumbnailSizePx: 48, jpegQuality: 80 });
  try {
    const image = await Promise.race([
      capture,
      new Promise<never>((_, reject) => setImmediate(() => reject(new Error("frame callback stalled capture")))),
    ]);
    assert.equal(await image.uploadBlob?.text(), "available-frame");
    assert.equal(frameCallbackRequests, 0);
    image.dispose();
  } finally {
    controller.abort(new DOMException("test cleanup", "AbortError"));
    await capture.catch(() => undefined);
    globalThis.document = originalDocument;
  }
});

test("browser capture uses one canvas snapshot for upload and thumbnail", async () => {
  const originalDocument = globalThis.document;
  const video = {
    videoWidth: 640,
    videoHeight: 480,
    currentFrame: "available-frame",
    muted: false,
    playsInline: false,
    srcObject: null as MediaStream | null,
    play: async () => undefined,
    pause: () => undefined,
  };
  globalThis.document = {
    createElement(tag: string) {
      if (tag === "video") return video;
      if (tag !== "canvas") throw new Error(`Unexpected element ${tag}`);
      let painted = "";
      return {
        width: 0,
        height: 0,
        get currentFrame() { return painted; },
        getContext: () => ({ drawImage: (source: { currentFrame: string }) => { painted = source.currentFrame; } }),
        toBlob: (callback: (blob: Blob) => void) => {
          const encoded = new Blob([painted], { type: "image/jpeg" });
          callback(encoded);
          // The stream advances while JPEG encoding of the upload settles.
          video.currentFrame = "later-frame";
        },
      };
    },
  } as unknown as Document;
  try {
    const capture = captureBrowserFrame({
      stream: { getTracks: () => [] } as unknown as MediaStream,
      descriptor,
      signal: new AbortController().signal,
    }, { maxDimensionPx: 640, thumbnailSizePx: 48, jpegQuality: 80 });
    const image = await capture;
    assert.equal(await image.uploadBlob?.text(), "available-frame");
    assert.equal(await image.thumbnailBlob?.text(), "available-frame");
    image.dispose();
  } finally {
    globalThis.document = originalDocument;
  }
});

test("aborting while video data loads removes the browser listener", async () => {
  const originalDocument = globalThis.document;
  const loadedDataListeners = new Set<() => void>();
  const video = {
    videoWidth: 0,
    videoHeight: 0,
    muted: false,
    playsInline: false,
    srcObject: null as MediaStream | null,
    paused: false,
    play: async () => undefined,
    pause() { this.paused = true; },
    addEventListener: (type: string, listener: () => void) => {
      if (type === "loadeddata") loadedDataListeners.add(listener);
    },
    removeEventListener: (type: string, listener: () => void) => {
      if (type === "loadeddata") loadedDataListeners.delete(listener);
    },
  };
  globalThis.document = {
    createElement(tag: string) {
      if (tag === "video") return video;
      throw new Error(`Unexpected element ${tag}`);
    },
  } as unknown as Document;
  try {
    const controller = new AbortController();
    const capture = captureBrowserFrame({
      stream: { getTracks: () => [] } as unknown as MediaStream,
      descriptor,
      signal: controller.signal,
    }, { maxDimensionPx: 640, thumbnailSizePx: 48, jpegQuality: 80 });
    await new Promise<void>((resolve) => setImmediate(resolve));
    controller.abort(new DOMException("capture stopped", "AbortError"));
    await assert.rejects(capture, /capture stopped/);
    assert.equal(loadedDataListeners.size, 0);
    assert.equal(video.paused, true);
    assert.equal(video.srcObject, null);
  } finally {
    globalThis.document = originalDocument;
  }
});

test("a static display capture uses the frame available after playback starts", async () => {
  const originalDocument = globalThis.document;
  let painted = "";
  const video = {
    videoWidth: 640,
    videoHeight: 480,
    currentFrame: "not-presented",
    muted: false,
    playsInline: false,
    srcObject: null as MediaStream | null,
    async play() {
      this.currentFrame = "first-presented-static-frame";
    },
    pause: () => undefined,
  };
  globalThis.document = {
    createElement(tag: string) {
      if (tag === "video") return video;
      if (tag !== "canvas") throw new Error(`Unexpected element ${tag}`);
      return {
        width: 0,
        height: 0,
        get currentFrame() { return painted; },
        getContext: () => ({ drawImage: (source: { currentFrame: string }) => { painted = source.currentFrame; } }),
        toBlob: (callback: (blob: Blob) => void) => callback(new Blob([painted], { type: "image/jpeg" })),
      };
    },
  } as unknown as Document;
  try {
    const capture = captureBrowserFrame({
      stream: { getTracks: () => [] } as unknown as MediaStream,
      descriptor: { ...descriptor, kind: "display", stableAlias: "desktop" },
      signal: new AbortController().signal,
    }, { maxDimensionPx: 640, thumbnailSizePx: 48, jpegQuality: 80 });
    const result = await Promise.race([
      capture,
      new Promise<never>((_, reject) => setImmediate(() => reject(new Error("static capture stalled")))),
    ]);
    assert.equal(await result.uploadBlob?.text(), "first-presented-static-frame");
    result.dispose();
  } finally {
    globalThis.document = originalDocument;
  }
});
