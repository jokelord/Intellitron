import assert from "node:assert/strict";
import { test } from "node:test";

import {
  FrozenBundleBatch,
  MAX_AUTOMATION_SOURCE_COUNT,
  TickBundleSampler,
  calculateBundleCapacity,
} from "./bundle-sampler.ts";
import type { SourceDescriptor, TickBundle } from "./types.ts";

const sources: SourceDescriptor[] = [
  { sourceId: "desktop", kind: "display", stableAlias: "desktop", label: "Screen" },
  { sourceId: "camera", kind: "camera", stableAlias: "camera_1", label: "Camera" },
];

function bundle(tickIndex: number, disposed: number[]): TickBundle {
  return {
    tickIndex,
    timestampMs: tickIndex * 1_000,
    relativeTimeSeconds: tickIndex,
    frames: sources.map((source) => ({
      source,
      resource: {
        uploadBlob: new Blob([`${tickIndex}-${source.sourceId}`], { type: "image/jpeg" }),
        thumbnailBlob: null,
        disposed: false,
        acquireObjectURL() {
          throw new Error("unused");
        },
        releaseObjectURL() {
          return false;
        },
        dispose() {
          if (this.disposed) return;
          this.disposed = true;
          this.uploadBlob = null;
          disposed.push(tickIndex);
        },
      },
    })),
  };
}

test("capacity follows frame, manual-image, image-count, and source ceilings", () => {
  assert.equal(calculateBundleCapacity({
    maxFramesPerSource: 16,
    manualImageCount: 9,
    sourceCount: 3,
  }), 16);
  assert.equal(calculateBundleCapacity({
    maxFramesPerSource: 250,
    manualImageCount: 9,
    sourceCount: 241,
  }), 1);
  assert.equal(calculateBundleCapacity({
    maxFramesPerSource: 250,
    manualImageCount: 0,
    sourceCount: 125,
  }), 2);
  assert.throws(() => calculateBundleCapacity({
    maxFramesPerSource: 1,
    manualImageCount: 0,
    sourceCount: MAX_AUTOMATION_SOURCE_COUNT + 1,
  }), /241/);
});

test("capacity one always retains only the latest complete bundle", () => {
  const disposed: number[] = [];
  const sampler = new TickBundleSampler({
    maxFramesPerSource: 1,
    manualImageCount: 0,
    sources,
    lockFirstBundle: true,
    rng: () => 0,
  });
  sampler.add(bundle(0, disposed));
  sampler.add(bundle(1, disposed));

  assert.deepEqual(sampler.bundles.map((item) => item.tickIndex), [1]);
  assert.deepEqual(disposed, [0, 0]);
});

test("capacity two permanently retains the first and latest bundles", () => {
  const disposed: number[] = [];
  const sampler = new TickBundleSampler({
    maxFramesPerSource: 2,
    manualImageCount: 0,
    sources,
    lockFirstBundle: true,
    rng: () => 0.5,
  });
  for (let tick = 0; tick < 5; tick += 1) sampler.add(bundle(tick, disposed));

  assert.deepEqual(sampler.bundles.map((item) => item.tickIndex), [0, 4]);
  assert.deepEqual(new Set(disposed), new Set([1, 2, 3]));
});

test("unlocked capacity two may evict the earliest bundle while preserving the latest bundle", () => {
  const disposed: number[] = [];
  const sampler = new TickBundleSampler({
    maxFramesPerSource: 2,
    manualImageCount: 0,
    sources,
    lockFirstBundle: false,
    rng: () => 0,
  } as never);
  for (let tick = 0; tick < 5; tick += 1) sampler.add(bundle(tick, disposed));

  assert.deepEqual(sampler.bundles.map((item) => item.tickIndex), [3, 4]);
  assert.deepEqual(new Set(disposed), new Set([0, 1, 2]));
});

test("locked byte pressure falls back to the latest bundle when the first and latest cannot both fit", () => {
  const disposed: number[] = [];
  const oneBundleBytes = 2 * (new TextEncoder().encode("data:image/jpeg;base64,").byteLength + 12);
  const sampler = new TickBundleSampler({
    maxFramesPerSource: 4,
    manualImageCount: 0,
    manualEncodedBytes: 0,
    maxEncodedPayloadBytes: oneBundleBytes * 2,
    sources,
    lockFirstBundle: true,
    rng: () => 0,
  });
  sampler.add(bundle(0, disposed));
  sampler.add(bundle(1, disposed));
  sampler.add(bundle(2, disposed));

  assert.deepEqual(sampler.bundles.map((item) => item.tickIndex), [0, 2]);
  assert.ok(sampler.estimatedEncodedBytes <= oneBundleBytes * 2);

  sampler.setManualImageCount(1, oneBundleBytes);

  assert.deepEqual(sampler.bundles.map((item) => item.tickIndex), [2]);
  assert.ok(sampler.estimatedEncodedBytes + oneBundleBytes <= oneBundleBytes * 2);
});

test("frozen batches expose and commit retained bundles in chronological tick order", () => {
  const disposed: number[] = [];
  const tick2 = bundle(2, disposed);
  const tick1 = bundle(1, disposed);
  tick1.timestampMs = tick2.timestampMs;
  const batch = new FrozenBundleBatch([tick2, tick1], sources);

  assert.deepEqual(batch.bundles.map((item) => item.tickIndex), [1, 2]);
  batch.commitSelection([tick2, tick1]);
  assert.deepEqual(batch.bundles.map((item) => item.tickIndex), [1, 2]);
  batch.dispose();
});

test("full samplers evict an injected-random old middle and dispose its whole bundle", () => {
  const disposed: number[] = [];
  const sampler = new TickBundleSampler({
    maxFramesPerSource: 4,
    manualImageCount: 0,
    sources,
    lockFirstBundle: true,
    rng: () => 0.99,
  });
  for (let tick = 0; tick < 5; tick += 1) sampler.add(bundle(tick, disposed));

  assert.deepEqual(sampler.bundles.map((item) => item.tickIndex), [0, 1, 2, 4]);
  assert.deepEqual(disposed, [3, 3]);
});

test("manual-image shrink disposes complete bundles and increasing capacity never resurrects them", () => {
  const disposed: number[] = [];
  const sampler = new TickBundleSampler({
    maxFramesPerSource: 4,
    manualImageCount: 0,
    sources: Array.from({ length: 62 }, (_, index): SourceDescriptor => ({
      sourceId: `camera-${index + 1}`,
      kind: "camera",
      stableAlias: `camera_${index + 1}`,
      label: `Camera ${index + 1}`,
    })),
    lockFirstBundle: true,
    rng: () => 0,
  });
  const largeCohort = sampler.sources;
  const largeBundle = (tickIndex: number): TickBundle => ({
    tickIndex,
    timestampMs: tickIndex * 1_000,
    relativeTimeSeconds: tickIndex,
    frames: largeCohort.map((source) => ({
      source,
      resource: {
        uploadBlob: new Blob([String(tickIndex)], { type: "image/jpeg" }),
        thumbnailBlob: null,
        disposed: false,
        acquireObjectURL() { throw new Error("unused"); },
        releaseObjectURL() { return false; },
        dispose() {
          if (this.disposed) return;
          this.disposed = true;
          this.uploadBlob = null;
          disposed.push(tickIndex);
        },
      },
    })),
  });
  for (let tick = 0; tick < 4; tick += 1) sampler.add(largeBundle(tick));
  assert.equal(sampler.capacity, 4);

  sampler.setManualImageCount(9);
  assert.equal(sampler.capacity, 3);
  assert.deepEqual(sampler.bundles.map((item) => item.tickIndex), [0, 2, 3]);
  sampler.setManualImageCount(0);
  assert.equal(sampler.capacity, 4);
  assert.deepEqual(sampler.bundles.map((item) => item.tickIndex), [0, 2, 3]);
  sampler.add(largeBundle(4));
  assert.deepEqual(sampler.bundles.map((item) => item.tickIndex), [0, 2, 3, 4]);
  assert.ok(!sampler.bundles.some((item) => item.tickIndex === 1));
});

test("disposing a sampler clears only the bounded retained set and is idempotent", () => {
  const disposed: number[] = [];
  const sampler = new TickBundleSampler({
    maxFramesPerSource: 2,
    manualImageCount: 0,
    sources,
    rng: () => 0,
  });
  for (let tick = 0; tick < 100; tick += 1) sampler.add(bundle(tick, disposed));
  assert.equal(sampler.bundles.length, 2);
  sampler.dispose();
  sampler.dispose();
  assert.deepEqual(sampler.bundles, []);
  assert.equal(disposed.length, 200);
});

test("invalid RNG output leaves the retained pool unchanged and the incoming bundle caller-owned", () => {
  const disposed: number[] = [];
  const sampler = new TickBundleSampler({
    maxFramesPerSource: 4,
    manualImageCount: 0,
    sources,
    rng: () => 1,
  } as never);
  for (let tick = 0; tick < 4; tick += 1) sampler.add(bundle(tick, disposed));
  const incoming = bundle(4, disposed);

  assert.throws(() => sampler.add(incoming), /rng/);
  assert.deepEqual(sampler.bundles.map((item) => item.tickIndex), [0, 1, 2, 3]);
  assert.equal(incoming.frames.every((frame) => !frame.resource.disposed), true);
  assert.deepEqual(disposed, []);
});

test("invalid RNG during manual-image shrink rolls back capacity and retained ownership", () => {
  const cohort = Array.from({ length: 62 }, (_, index): SourceDescriptor => ({
    sourceId: `source-${index + 1}`,
    kind: "camera",
    stableAlias: `camera_${index + 1}`,
    label: `Camera ${index + 1}`,
  }));
  const disposed: number[] = [];
  const sampler = new TickBundleSampler({
    maxFramesPerSource: 4,
    manualImageCount: 0,
    sources: cohort,
    rng: () => 1,
  });
  const makeBundle = (tickIndex: number): TickBundle => ({
    tickIndex,
    timestampMs: tickIndex,
    relativeTimeSeconds: tickIndex,
    frames: cohort.map((source) => ({
      source,
      resource: {
        uploadBlob: new Blob([String(tickIndex)], { type: "image/jpeg" }),
        thumbnailBlob: null,
        disposed: false,
        acquireObjectURL() { throw new Error("unused"); },
        releaseObjectURL() { return false; },
        dispose() {
          if (this.disposed) return;
          this.disposed = true;
          this.uploadBlob = null;
          disposed.push(tickIndex);
        },
      },
    })),
  });
  for (let tick = 0; tick < 4; tick += 1) sampler.add(makeBundle(tick));

  assert.throws(() => sampler.setManualImageCount(9), /rng/);
  assert.equal(sampler.capacity, 4);
  assert.deepEqual(sampler.bundles.map((item) => item.tickIndex), [0, 1, 2, 3]);
  assert.deepEqual(disposed, []);
});

test("sampler validates the immutable source cohort count directly", () => {
  const tooManySources = Array.from(
    { length: MAX_AUTOMATION_SOURCE_COUNT + 1 },
    (_, index): SourceDescriptor => ({
      sourceId: `source-${index + 1}`,
      kind: "camera",
      stableAlias: `camera_${index + 1}`,
      label: `Camera ${index + 1}`,
    }),
  );
  assert.throws(() => new TickBundleSampler({
    maxFramesPerSource: 1,
    manualImageCount: 0,
    sources: tooManySources,
  }), /241/);
});

test("sampler rejects bundles that differ from its immutable source cohort", () => {
  const disposed: number[] = [];
  const sampler = new TickBundleSampler({
    maxFramesPerSource: 4,
    manualImageCount: 0,
    sources,
    rng: () => 0,
  } as never);
  const mismatched = bundle(0, disposed);
  mismatched.frames = [mismatched.frames[0], {
    ...mismatched.frames[1],
    source: {
      sourceId: "other-camera",
      kind: "camera",
      stableAlias: "camera_2",
      label: "Other",
    },
  }];

  assert.throws(() => sampler.add(mismatched), /source cohort/i);
  assert.deepEqual(sampler.bundles, []);
  assert.equal(mismatched.frames.every((frame) => !frame.resource.disposed), true);
});

test("freeze explicitly transfers retained ownership out of the sampler", () => {
  const disposed: number[] = [];
  const sampler = new TickBundleSampler({
    maxFramesPerSource: 2,
    manualImageCount: 0,
    sources,
    rng: () => 0,
  } as never);
  sampler.add(bundle(0, disposed));
  const freeze = (sampler as unknown as { freeze(): { bundles: readonly TickBundle[]; dispose(): void } }).freeze;
  assert.equal(typeof freeze, "function");

  const frozen = freeze.call(sampler);
  assert.deepEqual(sampler.bundles, []);
  assert.throws(() => sampler.setManualImageCount(1), /active pool/);
  sampler.dispose();
  assert.deepEqual(disposed, []);
  frozen.dispose();
  assert.deepEqual(disposed, [0, 0]);
});
