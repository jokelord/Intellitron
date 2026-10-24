import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildCompressionTiers,
  buildVisionAutomationManifest,
  estimateDataUrlBytes,
  prepareVisionPayload,
} from "./payload.ts";
import { FrozenBundleBatch } from "./bundle-sampler.ts";
import type { SourceDescriptor, TickBundle } from "./types.ts";

const display: SourceDescriptor = {
  sourceId: "display-device-id",
  kind: "display",
  stableAlias: "desktop",
  label: "User's private window title",
};
const camera1: SourceDescriptor = {
  sourceId: "camera-source-b",
  kind: "camera",
  stableAlias: "camera_1",
  label: "Secret hardware label",
  deviceId: "private-camera-device-id",
};

function resource(size: number, disposed: number[]) {
  return {
    uploadBlob: new Blob([new Uint8Array(size)], { type: "image/jpeg" }) as Blob | null,
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
      disposed.push(size);
    },
  };
}

function bundle(tickIndex: number, relativeTimeSeconds: number, size: number, disposed: number[]): TickBundle {
  return {
    tickIndex,
    timestampMs: 1000 + relativeTimeSeconds * 1000,
    relativeTimeSeconds,
    frames: [camera1, display].map((source) => ({ source, resource: resource(size, disposed) })),
  };
}

function frozenBatch(bundles: readonly TickBundle[]): FrozenBundleBatch {
  return new FrozenBundleBatch(bundles, [display, camera1]);
}

test("Data URL estimates include the exact UTF-8 header and Base64 padding", () => {
  assert.equal(estimateDataUrlBytes(0), 23);
  assert.equal(estimateDataUrlBytes(1), 27);
  assert.equal(estimateDataUrlBytes(2), 27);
  assert.equal(estimateDataUrlBytes(3), 27);
  assert.equal(
    estimateDataUrlBytes(3, "image/å"),
    new TextEncoder().encode("data:image/å;base64,").byteLength + 4,
  );
});

test("compression tiers use the fixed quality ladder at each deterministic dimension", () => {
  const tiers = buildCompressionTiers({
    initialQuality: 85,
    minimumQuality: 40,
    maxDimensionPx: 1000,
    minimumDimensionPx: 256,
  });
  assert.deepEqual(tiers.slice(0, 6), [
    { quality: 85, maxDimensionPx: 1000 },
    { quality: 75, maxDimensionPx: 1000 },
    { quality: 65, maxDimensionPx: 1000 },
    { quality: 55, maxDimensionPx: 1000 },
    { quality: 45, maxDimensionPx: 1000 },
    { quality: 40, maxDimensionPx: 1000 },
  ]);
  assert.deepEqual(
    [...new Set(tiers.map((tier) => tier.maxDimensionPx))],
    [1000, 800, 640, 512, 409, 327, 261, 256],
  );
  assert.deepEqual(buildCompressionTiers({
    initialQuality: 82,
    minimumQuality: 67,
    maxDimensionPx: 200,
    minimumDimensionPx: 256,
  }), [
    { quality: 82, maxDimensionPx: 200 },
    { quality: 72, maxDimensionPx: 200 },
    { quality: 67, maxDimensionPx: 200 },
  ]);
});

test("manifest is deterministic, source-safe, two-decimal, and has no Shared ticks", () => {
  const disposed: number[] = [];
  const bundles = [bundle(19, 0, 1, disposed), bundle(42, 4.026, 1, disposed)];
  const manifest = buildVisionAutomationManifest({
    manualImageCount: 2,
    sources: [camera1, display],
    bundles,
  });

  assert.equal(manifest, [
    "<vision_automation_manifest>",
    "Manual images: 2, image indexes 1-2",
    "Source 1: Desktop, image indexes 3-4",
    "Source 2: Camera 1, image indexes 5-6",
    "Relative times: 0.00s, 4.03s",
    "</vision_automation_manifest>",
  ].join("\n"));
  assert.doesNotMatch(manifest, /Shared ticks|device|private|task|session|request/i);
});

test("payload is manual-first, then desktop and camera groups, with bounded conversion concurrency", async () => {
  const disposed: number[] = [];
  const bundles = [bundle(0, 0, 3, disposed), bundle(1, 1.25, 3, disposed)];
  let active = 0;
  let maxActive = 0;
  const result = await prepareVisionPayload({
    manualDataUrls: ["data:image/png;base64,TQ=="],
    batch: frozenBatch(bundles),
    userText: "Inspect these images.",
    maxEncodedPayloadBytes: 10_000,
    compression: {
      initialQuality: 85,
      minimumQuality: 40,
      maxDimensionPx: 1024,
      minimumDimensionPx: 256,
    },
    encodeConcurrency: 2,
    encodeFrame: async ({ source, bundle: current }) => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise((resolve) => setTimeout(resolve, 1));
      active -= 1;
      return new Blob([`${source.stableAlias}-${current.tickIndex}`], { type: "image/jpeg" });
    },
    blobToDataUrl: async (blob) => `data:${blob.type};base64,${Buffer.from(await blob.arrayBuffer()).toString("base64")}`,
  });

  assert.ok(maxActive <= 2);
  assert.deepEqual(result.images.map((url) => Buffer.from(url.split(",")[1], "base64").toString()), [
    "M",
    "desktop-0",
    "desktop-1",
    "camera_1-0",
    "camera_1-1",
  ]);
  assert.match(result.text, /^<vision_automation_manifest>/);
  assert.match(result.text, /<\/vision_automation_manifest>\n\nInspect these images\.$/);
  assert.equal(
    result.encodedImageBytes,
    result.images.reduce((total, image) => total + new TextEncoder().encode(image).byteLength, 0),
  );
  assert.deepEqual(disposed, []);
});

test("byte pressure applies one tier to every frame before dropping whole oldest bundles", async () => {
  const disposed: number[] = [];
  const bundles = [
    bundle(0, 0, 20, disposed),
    bundle(1, 1, 20, disposed),
    bundle(2, 2, 20, disposed),
  ];
  const seenTiers: string[] = [];
  const result = await prepareVisionPayload({
    manualDataUrls: [],
    batch: frozenBatch(bundles),
    userText: "",
    maxEncodedPayloadBytes: 60,
    compression: {
      initialQuality: 85,
      minimumQuality: 85,
      maxDimensionPx: 256,
      minimumDimensionPx: 256,
    },
    encodeConcurrency: 3,
    encodeFrame: async ({ source, bundle: current }, tier) => {
      seenTiers.push(`${current.tickIndex}:${source.stableAlias}:${tier.quality}:${tier.maxDimensionPx}`);
      return new Blob([new Uint8Array(1)], { type: "image/jpeg" });
    },
    blobToDataUrl: async () => "data:image/jpeg;base64,AA==",
  });

  assert.deepEqual(result.retainedBundles.map((item) => item.tickIndex), [2]);
  assert.deepEqual(result.droppedTickIndexes, [0, 1]);
  assert.equal(disposed.length, 4);
  assert.equal(new Set(seenTiers.map((entry) => entry.split(":").slice(2).join(":"))).size, 1);
});

test("unlocked payload byte pressure can discard the first bundle but always keeps the latest", async () => {
  const disposed: number[] = [];
  const result = await prepareVisionPayload({
    manualDataUrls: [],
    batch: frozenBatch([bundle(0, 0, 20, disposed), bundle(1, 1, 20, disposed), bundle(2, 2, 20, disposed)]),
    userText: "",
    maxEncodedPayloadBytes: 108,
    lockFirstBundle: false,
    compression: { initialQuality: 85, minimumQuality: 85, maxDimensionPx: 256, minimumDimensionPx: 256 },
    encodeConcurrency: 2,
    encodeFrame: async () => new Blob([new Uint8Array(1)], { type: "image/jpeg" }),
    blobToDataUrl: async () => "data:image/jpeg;base64,AA==",
  } as never);

  assert.deepEqual(result.retainedBundles.map((item) => item.tickIndex), [1, 2]);
  assert.deepEqual(result.droppedTickIndexes, [0]);
});

test("locked payload byte pressure keeps the first and latest bundle", async () => {
  const disposed: number[] = [];
  const result = await prepareVisionPayload({
    manualDataUrls: [],
    batch: frozenBatch([bundle(0, 0, 20, disposed), bundle(1, 1, 20, disposed), bundle(2, 2, 20, disposed)]),
    userText: "",
    maxEncodedPayloadBytes: 108,
    lockFirstBundle: true,
    compression: { initialQuality: 85, minimumQuality: 85, maxDimensionPx: 256, minimumDimensionPx: 256 },
    encodeConcurrency: 2,
    encodeFrame: async () => new Blob([new Uint8Array(1)], { type: "image/jpeg" }),
    blobToDataUrl: async () => "data:image/jpeg;base64,AA==",
  });

  assert.deepEqual(result.retainedBundles.map((item) => item.tickIndex), [0, 2]);
  assert.deepEqual(result.droppedTickIndexes, [1]);
});

test("locked payload falls back to the latest bundle when two locked bundles exceed the byte budget", async () => {
  const disposed: number[] = [];
  const result = await prepareVisionPayload({
    manualDataUrls: [],
    batch: frozenBatch([bundle(0, 0, 20, disposed), bundle(1, 1, 20, disposed)]),
    userText: "",
    maxEncodedPayloadBytes: 54,
    lockFirstBundle: true,
    compression: { initialQuality: 85, minimumQuality: 85, maxDimensionPx: 256, minimumDimensionPx: 256 },
    encodeConcurrency: 2,
    encodeFrame: async () => new Blob([new Uint8Array(1)], { type: "image/jpeg" }),
    blobToDataUrl: async () => "data:image/jpeg;base64,AA==",
  });

  assert.deepEqual(result.retainedBundles.map((item) => item.tickIndex), [1]);
  assert.deepEqual(result.droppedTickIndexes, [0]);
});

test("a latest bundle that cannot fit fails without partial-frame deletion", async () => {
  const disposed: number[] = [];
  await assert.rejects(() => prepareVisionPayload({
    manualDataUrls: [],
    batch: frozenBatch([bundle(0, 0, 2, disposed)]),
    userText: "x",
    maxEncodedPayloadBytes: 1,
    compression: {
      initialQuality: 85,
      minimumQuality: 85,
      maxDimensionPx: 256,
      minimumDimensionPx: 256,
    },
    encodeConcurrency: 1,
    encodeFrame: async () => new Blob([new Uint8Array(1)], { type: "image/jpeg" }),
  }), /latest bundle/i);
  assert.deepEqual(disposed, []);
});

test("one Data URL failure drops its complete bundle transactionally after sibling work settles", async () => {
  const disposed: number[] = [];
  const bundles = [bundle(0, 0, 3, disposed), bundle(1, 1, 3, disposed)];
  const batch = frozenBatch(bundles);
  let siblingAborted = false;
  let siblingSettled = false;
  let callerOwnershipIntactDuringConversion = false;

  const result = await prepareVisionPayload({
    manualDataUrls: [],
    batch,
    userText: "",
    maxEncodedPayloadBytes: 10_000,
    compression: {
      initialQuality: 85,
      minimumQuality: 85,
      maxDimensionPx: 256,
      minimumDimensionPx: 256,
    },
    encodeConcurrency: 2,
    encodeFrame: async ({ source, bundle: current }) => new Blob(
      [`${current.tickIndex}:${source.stableAlias}`],
      { type: "image/jpeg" },
    ),
    blobToDataUrl: async (blob, signal) => {
      const value = await blob.text();
      if (value === "0:desktop") {
        await Promise.resolve();
        throw new Error("desktop conversion failed");
      }
      if (value === "0:camera_1") {
        siblingAborted = signal.aborted;
        if (!signal.aborted) {
          await new Promise<void>((resolve) => {
            signal.addEventListener("abort", () => {
              siblingAborted = true;
              setTimeout(resolve, 5);
            }, { once: true });
          });
        }
        siblingSettled = true;
      }
      callerOwnershipIntactDuringConversion ||= disposed.length === 0;
      return `data:image/jpeg;base64,${Buffer.from(value).toString("base64")}`;
    },
  });

  assert.equal(siblingAborted, true);
  assert.equal(siblingSettled, true);
  assert.equal(callerOwnershipIntactDuringConversion, true);
  assert.deepEqual(result.retainedBundles.map((item) => item.tickIndex), [1]);
  assert.deepEqual(result.droppedTickIndexes, [0]);
  assert.deepEqual(batch.bundles.map((item) => item.tickIndex), [1]);
  assert.equal(disposed.length, 2);
});

test("one frame failure drops its complete bundle only after outstanding bundle work settles", async () => {
  const disposed: number[] = [];
  const bundles = [bundle(0, 0, 3, disposed), bundle(1, 1, 3, disposed)];
  const batch = frozenBatch(bundles);
  let siblingAborted = false;
  let siblingSettled = false;

  const result = await prepareVisionPayload({
    manualDataUrls: [],
    batch,
    userText: "",
    maxEncodedPayloadBytes: 10_000,
    compression: {
      initialQuality: 85,
      minimumQuality: 85,
      maxDimensionPx: 256,
      minimumDimensionPx: 256,
    },
    encodeConcurrency: 2,
    encodeFrame: async ({ source, bundle: current }, _tier, signal) => {
      if (current.tickIndex === 0 && source.stableAlias === "desktop") {
        await Promise.resolve();
        throw new Error("display frame failed");
      }
      if (current.tickIndex === 0) {
        await new Promise<void>((resolve) => {
          signal.addEventListener("abort", () => {
            siblingAborted = true;
            setTimeout(() => {
              siblingSettled = true;
              resolve();
            }, 5);
          }, { once: true });
        });
      }
      return new Blob([`${current.tickIndex}:${source.stableAlias}`], { type: "image/jpeg" });
    },
  });

  assert.equal(siblingAborted, true);
  assert.equal(siblingSettled, true);
  assert.deepEqual(result.retainedBundles.map((item) => item.tickIndex), [1]);
  assert.deepEqual(result.droppedTickIndexes, [0]);
  assert.deepEqual(batch.bundles.map((item) => item.tickIndex), [1]);
  assert.equal(disposed.length, 2);
});

test("non-JPEG encoder output is rejected without mutating frozen ownership", async () => {
  const disposed: number[] = [];
  const batch = frozenBatch([bundle(0, 0, 3, disposed)]);

  await assert.rejects(() => prepareVisionPayload({
    manualDataUrls: [],
    batch,
    userText: "",
    maxEncodedPayloadBytes: 10_000,
    compression: {
      initialQuality: 85,
      minimumQuality: 85,
      maxDimensionPx: 256,
      minimumDimensionPx: 256,
    },
    encodeConcurrency: 2,
    encodeFrame: async () => new Blob(["png"], { type: "image/png" }),
  }), /JPEG/);

  assert.deepEqual(batch.bundles.map((item) => item.tickIndex), [0]);
  assert.deepEqual(disposed, []);
});

test("serialization must use the same JPEG MIME used by byte estimation", async () => {
  const disposed: number[] = [];
  const batch = frozenBatch([bundle(0, 0, 3, disposed)]);

  await assert.rejects(() => prepareVisionPayload({
    manualDataUrls: [],
    batch,
    userText: "",
    maxEncodedPayloadBytes: 10_000,
    compression: {
      initialQuality: 85,
      minimumQuality: 85,
      maxDimensionPx: 256,
      minimumDimensionPx: 256,
    },
    encodeConcurrency: 2,
    encodeFrame: async () => new Blob(["jpeg"], { type: "image/jpeg" }),
    blobToDataUrl: async () => "data:image/png;base64,cG5n",
  }), /JPEG Data URL/);

  assert.deepEqual(batch.bundles.map((item) => item.tickIndex), [0]);
  assert.deepEqual(disposed, []);
});

test("caller AbortSignal cancels and settles compression without committing batch ownership", async () => {
  const disposed: number[] = [];
  const batch = frozenBatch([bundle(0, 0, 3, disposed)]);
  const controller = new AbortController();
  const reason = new DOMException("caller stopped compression", "AbortError");
  const observed: boolean[] = [];

  await assert.rejects(() => prepareVisionPayload({
    manualDataUrls: [],
    batch,
    signal: controller.signal,
    userText: "",
    maxEncodedPayloadBytes: 10_000,
    compression: {
      initialQuality: 85,
      minimumQuality: 85,
      maxDimensionPx: 256,
      minimumDimensionPx: 256,
    },
    encodeConcurrency: 2,
    encodeFrame: async ({ source }, _tier, signal) => {
      if (source.stableAlias === "camera_1") controller.abort(reason);
      await Promise.resolve();
      observed.push(signal.aborted);
      if (signal.aborted) throw signal.reason;
      return new Blob(["jpeg"], { type: "image/jpeg" });
    },
  }), /caller stopped compression/);

  assert.ok(observed.length >= 1);
  assert.equal(observed.every(Boolean), true);
  assert.deepEqual(batch.bundles.map((item) => item.tickIndex), [0]);
  assert.deepEqual(disposed, []);
});

test("caller AbortSignal cancels and settles Data URL work without committing batch ownership", async () => {
  const disposed: number[] = [];
  const batch = frozenBatch([bundle(0, 0, 3, disposed)]);
  const controller = new AbortController();
  const reason = new DOMException("caller stopped conversion", "AbortError");
  const observed: boolean[] = [];

  await assert.rejects(() => prepareVisionPayload({
    manualDataUrls: [],
    batch,
    signal: controller.signal,
    userText: "",
    maxEncodedPayloadBytes: 10_000,
    compression: {
      initialQuality: 85,
      minimumQuality: 85,
      maxDimensionPx: 256,
      minimumDimensionPx: 256,
    },
    encodeConcurrency: 2,
    encodeFrame: async ({ source, bundle: current }) => new Blob(
      [`${current.tickIndex}:${source.stableAlias}`],
      { type: "image/jpeg" },
    ),
    blobToDataUrl: async (blob, signal) => {
      if ((await blob.text()).includes("camera_1")) controller.abort(reason);
      await Promise.resolve();
      observed.push(signal.aborted);
      if (signal.aborted) throw signal.reason;
      return "data:image/jpeg;base64,anBlZw==";
    },
  }), /caller stopped conversion/);

  assert.ok(observed.length >= 1);
  assert.equal(observed.every(Boolean), true);
  assert.deepEqual(batch.bundles.map((item) => item.tickIndex), [0]);
  assert.deepEqual(disposed, []);
});
