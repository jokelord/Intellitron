import assert from "node:assert/strict";
import { test } from "node:test";

import { MediaSourceRegistry } from "./media-source-registry.ts";

function fakeStream(id: string) {
  const listeners = new Map<string, Set<() => void>>();
  const track = {
    stopCount: 0,
    stop() {
      this.stopCount += 1;
    },
    addEventListener(type: string, listener: () => void) {
      const current = listeners.get(type) ?? new Set();
      current.add(listener);
      listeners.set(type, current);
    },
    removeEventListener(type: string, listener: () => void) {
      listeners.get(type)?.delete(listener);
    },
    emit(type: string) {
      listeners.get(type)?.forEach((listener) => listener());
    },
  };
  return {
    id,
    active: true,
    track,
    getTracks: () => [track],
  };
}

test("two owners share one camera and only the final release stops its tracks", async () => {
  const stream = fakeStream("camera-1");
  let starts = 0;
  const registry = new MediaSourceRegistry({
    getUserMedia: async () => {
      starts += 1;
      return stream as unknown as MediaStream;
    },
  });

  const manual = await registry.acquireCamera({ ownerId: "manual", deviceId: "camera-1" });
  const automation = await registry.acquireCamera({ ownerId: "automation", deviceId: "camera-1" });
  assert.equal(starts, 1);
  assert.strictEqual(manual.stream, automation.stream);

  manual.release();
  manual.release();
  assert.equal(stream.track.stopCount, 0);
  assert.equal(registry.getLeasedStream("camera:camera-1", "automation"), automation.stream);
  automation.release();
  assert.equal(stream.track.stopCount, 1);
  registry.destroy();
  assert.equal(stream.track.stopCount, 1);
});

test("concurrent camera starts are deduplicated while preserving both owners", async () => {
  const stream = fakeStream("camera-1");
  let resolveStart!: (stream: MediaStream) => void;
  let starts = 0;
  const registry = new MediaSourceRegistry({
    getUserMedia: () => {
      starts += 1;
      return new Promise<MediaStream>((resolve) => {
        resolveStart = resolve;
      });
    },
  });
  const first = registry.acquireCamera({ ownerId: "manual", deviceId: "camera-1" });
  const second = registry.acquireCamera({ ownerId: "automation", deviceId: "camera-1" });
  assert.equal(starts, 1);
  resolveStart(stream as unknown as MediaStream);
  const [manual, automation] = await Promise.all([first, second]);
  manual.release();
  assert.equal(stream.track.stopCount, 0);
  automation.release();
  assert.equal(stream.track.stopCount, 1);
});

test("releasing one owner across devices never releases another owner", async () => {
  const streams = new Map([
    ["a", fakeStream("a")],
    ["b", fakeStream("b")],
  ]);
  const registry = new MediaSourceRegistry({
    getUserMedia: async (constraints) => {
      const deviceId = (constraints.video as MediaTrackConstraints).deviceId as ConstrainDOMStringParameters;
      return streams.get(String(deviceId.exact)) as unknown as MediaStream;
    },
  });
  await registry.acquireCamera({ ownerId: "manual", deviceId: "a" });
  await registry.acquireCamera({ ownerId: "automation", deviceId: "a" });
  await registry.acquireCamera({ ownerId: "manual", deviceId: "b" });

  registry.releaseOwner("manual");
  assert.equal(streams.get("a")!.track.stopCount, 0);
  assert.equal(streams.get("b")!.track.stopCount, 1);
  registry.releaseOwner("automation");
  assert.equal(streams.get("a")!.track.stopCount, 1);
});

test("display acquisition invokes the chooser callback synchronously in the direct call", async () => {
  const stream = fakeStream("display");
  let insideAcquireCall = true;
  let chooserCalledInside = false;
  const registry = new MediaSourceRegistry({
    getUserMedia: async () => {
      throw new Error("unused");
    },
  });
  const acquisition = registry.acquireDisplay({
    ownerId: "automation",
    requestDisplay: () => {
      chooserCalledInside = insideAcquireCall;
      return Promise.resolve(stream as unknown as MediaStream);
    },
  });
  insideAcquireCall = false;
  const lease = await acquisition;
  assert.equal(chooserCalledInside, true);
  assert.equal(lease.sourceKey, "display");
  lease.release();
  assert.equal(stream.track.stopCount, 1);
});

test("aborted acquisition cannot leak an unowned stream", async () => {
  const stream = fakeStream("camera-1");
  let resolveStart!: (stream: MediaStream) => void;
  const controller = new AbortController();
  const registry = new MediaSourceRegistry({
    getUserMedia: () => new Promise<MediaStream>((resolve) => {
      resolveStart = resolve;
    }),
  });
  const acquisition = registry.acquireCamera({
    ownerId: "automation",
    deviceId: "camera-1",
    signal: controller.signal,
  });
  controller.abort(new DOMException("cancelled", "AbortError"));
  resolveStart(stream as unknown as MediaStream);
  await assert.rejects(acquisition, /cancelled/);
  await Promise.resolve();
  assert.equal(stream.track.stopCount, 1);
});

test("one aborted pending owner cannot stop the stream before another pending owner leases it", async () => {
  const stream = fakeStream("camera-1");
  let resolveStart!: (stream: MediaStream) => void;
  const controller = new AbortController();
  const registry = new MediaSourceRegistry({
    getUserMedia: () => new Promise<MediaStream>((resolve) => {
      resolveStart = resolve;
    }),
  });
  const aborted = registry.acquireCamera({
    ownerId: "manual",
    deviceId: "camera-1",
    signal: controller.signal,
  });
  const active = registry.acquireCamera({
    ownerId: "automation",
    deviceId: "camera-1",
  });
  controller.abort(new DOMException("manual closed", "AbortError"));
  resolveStart(stream as unknown as MediaStream);

  await assert.rejects(aborted, /manual closed/);
  const lease = await active;
  assert.strictEqual(lease.stream, stream);
  assert.equal(stream.track.stopCount, 0);
  lease.release();
  assert.equal(stream.track.stopCount, 1);
});

test("aborting a pending display attempt lets Retry synchronously open a new chooser", async () => {
  const staleStream = fakeStream("stale-display");
  const retryStream = fakeStream("retry-display");
  const attempts: Array<(stream: MediaStream) => void> = [];
  const controller = new AbortController();
  const registry = new MediaSourceRegistry();
  let chooserCalls = 0;

  const requestDisplay = () => {
    chooserCalls += 1;
    return new Promise<MediaStream>((resolve) => attempts.push(resolve));
  };
  const stale = registry.acquireDisplay({
    ownerId: "automation",
    requestDisplay,
    signal: controller.signal,
  });
  assert.equal(chooserCalls, 1);

  controller.abort(new DOMException("task aborted", "AbortError"));
  const retry = registry.acquireDisplay({ ownerId: "automation", requestDisplay });
  assert.equal(chooserCalls, 2, "Retry must invoke getDisplayMedia in its direct call");

  attempts[1](retryStream as unknown as MediaStream);
  const retryLease = await retry;
  attempts[0](staleStream as unknown as MediaStream);
  await assert.rejects(stale, /aborted|generation/i);
  await Promise.resolve();

  assert.equal(staleStream.track.stopCount, 1);
  assert.equal(retryStream.track.stopCount, 0);
  assert.strictEqual(retryLease.stream, retryStream);
  retryLease.release();
  assert.equal(retryStream.track.stopCount, 1);
});

test("a camera refresh quarantines stale enumeration results", async () => {
  const enumerationResolvers: Array<(devices: MediaDeviceInfo[]) => void> = [];
  const registry = new MediaSourceRegistry({
    enumerateDevices: () => new Promise((resolve) => enumerationResolvers.push(resolve)),
  });
  const oldRequest = registry.enumerateCameraDevices(false);
  registry.clearDeviceCache();
  const refreshedRequest = registry.enumerateCameraDevices(false);
  assert.equal(enumerationResolvers.length, 2);

  enumerationResolvers[1]([{
    kind: "videoinput",
    deviceId: "fresh",
    label: "Fresh Camera",
  } as MediaDeviceInfo]);
  assert.deepEqual(await refreshedRequest, [{ deviceId: "fresh", label: "Fresh Camera" }]);

  enumerationResolvers[0]([{
    kind: "videoinput",
    deviceId: "stale",
    label: "Stale Camera",
  } as MediaDeviceInfo]);
  assert.deepEqual(await oldRequest, []);
  assert.deepEqual(await registry.enumerateCameraDevices(false), [{
    deviceId: "fresh",
    label: "Fresh Camera",
  }]);
});

test("10,000 display Start and Abort attempts retain no owner attempt metadata", () => {
  const registry = new MediaSourceRegistry();
  let chooserCalls = 0;
  for (let index = 0; index < 10_000; index += 1) {
    const ownerId = `automation-${index}`;
    const controller = new AbortController();
    void registry.acquireDisplay({
      ownerId,
      signal: controller.signal,
      requestDisplay: () => {
        chooserCalls += 1;
        return new Promise<MediaStream>(() => undefined);
      },
    });
    controller.abort(new DOMException("soak abort", "AbortError"));
    registry.releaseOwner(ownerId);
  }

  assert.equal(chooserCalls, 10_000);
  assert.deepEqual(registry.diagnostics(), {
    streams: 0,
    pendingStarts: 0,
    pendingConsumers: 0,
    pendingAttemptOwners: 0,
    pendingAttempts: 0,
    latestAttemptToken: 10_000,
  });
});
