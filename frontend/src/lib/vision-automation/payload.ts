import {
  assertBundleMatchesSourceCohort,
  type FrozenBundleBatch,
  MAX_AUTOMATION_SOURCE_COUNT,
  MAX_MANUAL_IMAGE_COUNT,
  MAX_REAL_IMAGES_PER_TURN,
} from "./bundle-sampler.ts";
import type {
  AutomationFrame,
  CompressionTier,
  SourceDescriptor,
  TickBundle,
} from "./types.ts";

export interface CompressionSettings {
  initialQuality: number;
  minimumQuality: number;
  maxDimensionPx: number;
  minimumDimensionPx: number;
}

export interface VisionManifestInput {
  manualImageCount: number;
  sources: readonly SourceDescriptor[];
  bundles: readonly TickBundle[];
}

export interface FrameEncodingInput {
  blob: Blob;
  source: SourceDescriptor;
  bundle: TickBundle;
  frame: AutomationFrame;
}

export type AutomationFrameEncoder = (
  input: FrameEncodingInput,
  tier: CompressionTier,
  signal: AbortSignal,
) => Promise<Blob>;

export type BlobToDataUrlEncoder = (blob: Blob, signal: AbortSignal) => Promise<string>;

export interface PrepareVisionPayloadInput {
  manualDataUrls: readonly string[];
  batch: FrozenBundleBatch;
  signal?: AbortSignal;
  userText: string;
  maxEncodedPayloadBytes: number;
  lockFirstBundle?: boolean;
  compression: CompressionSettings;
  encodeConcurrency: number;
  encodeFrame: AutomationFrameEncoder;
  blobToDataUrl?: BlobToDataUrlEncoder;
}

function chronologicalOrder(left: TickBundle, right: TickBundle): number {
  return left.timestampMs - right.timestampMs || left.tickIndex - right.tickIndex;
}

export interface PreparedVisionPayload {
  images: readonly string[];
  text: string;
  manifest: string;
  encodedImageBytes: number;
  compressionTier: CompressionTier;
  retainedBundles: readonly TickBundle[];
  droppedTickIndexes: readonly number[];
  sourceCount: number;
  retainedTickCount: number;
}

interface EncodedFrame {
  bundle: TickBundle;
  source: SourceDescriptor;
  blob: Blob;
}

interface EncodedDataFrame {
  bundle: TickBundle;
  source: SourceDescriptor;
  dataUrl: string;
}

export const AUTOMATION_JPEG_MIME = "image/jpeg";

class InvalidJpegOutputError extends Error {}

export function estimateDataUrlBytes(blobBytes: number, mime = "image/jpeg"): number {
  if (!Number.isInteger(blobBytes) || blobBytes < 0) {
    throw new RangeError("blobBytes must be a non-negative integer");
  }
  const header = `data:${mime};base64,`;
  return new TextEncoder().encode(header).byteLength + 4 * Math.ceil(blobBytes / 3);
}

export function buildCompressionTiers(settings: CompressionSettings): CompressionTier[] {
  const { initialQuality, minimumQuality, maxDimensionPx, minimumDimensionPx } = settings;
  if (!Number.isInteger(initialQuality) || !Number.isInteger(minimumQuality)
    || minimumQuality < 1 || initialQuality > 95 || minimumQuality > initialQuality) {
    throw new RangeError("Invalid JPEG quality range");
  }
  if (!Number.isInteger(maxDimensionPx) || !Number.isInteger(minimumDimensionPx)
    || maxDimensionPx < 1 || minimumDimensionPx < 1) {
    throw new RangeError("Image dimensions must be positive integers");
  }
  const qualities: number[] = [];
  for (let quality = initialQuality; quality >= minimumQuality; quality -= 10) {
    qualities.push(quality);
  }
  if (qualities.at(-1) !== minimumQuality) qualities.push(minimumQuality);

  const floor = Math.min(maxDimensionPx, minimumDimensionPx);
  const dimensions = [maxDimensionPx];
  while (dimensions.at(-1)! > floor) {
    const next = Math.max(floor, Math.floor(dimensions.at(-1)! * 0.8));
    if (next === dimensions.at(-1)) break;
    dimensions.push(next);
  }
  return dimensions.flatMap((maxDimension) => qualities.map((quality) => ({
    quality,
    maxDimensionPx: maxDimension,
  })));
}

function sortedSources(sources: readonly SourceDescriptor[]): SourceDescriptor[] {
  const unique = new Map(sources.map((source) => [source.stableAlias, source]));
  return Array.from(unique.values()).sort((left, right) => {
    if (left.kind !== right.kind) return left.kind === "display" ? -1 : 1;
    const leftIndex = left.stableAlias === "desktop" ? 0 : Number(left.stableAlias.slice(7));
    const rightIndex = right.stableAlias === "desktop" ? 0 : Number(right.stableAlias.slice(7));
    return leftIndex - rightIndex;
  });
}

function manifestSourceName(source: SourceDescriptor): string {
  if (source.stableAlias === "desktop") return "Desktop";
  return `Camera ${Number(source.stableAlias.slice(7))}`;
}

export function buildVisionAutomationManifest(input: VisionManifestInput): string {
  const bundles = [...input.bundles].sort(chronologicalOrder);
  const sources = sortedSources(input.sources);
  const lines = ["<vision_automation_manifest>"];
  let nextImageIndex = 1;
  if (input.manualImageCount > 0) {
    lines.push(
      `Manual images: ${input.manualImageCount}, image indexes ${nextImageIndex}-${nextImageIndex + input.manualImageCount - 1}`,
    );
    nextImageIndex += input.manualImageCount;
  } else {
    lines.push("Manual images: 0");
  }
  sources.forEach((source, index) => {
    const count = bundles.length;
    const range = count > 0
      ? `, image indexes ${nextImageIndex}-${nextImageIndex + count - 1}`
      : "";
    lines.push(`Source ${index + 1}: ${manifestSourceName(source)}${range}`);
    nextImageIndex += count;
  });
  lines.push(`Relative times: ${bundles.map((bundle) => `${bundle.relativeTimeSeconds.toFixed(2)}s`).join(", ")}`);
  lines.push("</vision_automation_manifest>");
  return lines.join("\n");
}

async function mapConcurrentSafely<T, R>(
  values: readonly T[],
  concurrency: number,
  worker: (value: T, signal: AbortSignal) => Promise<R>,
  parentSignal?: AbortSignal,
): Promise<R[]> {
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 8) {
    throw new RangeError("encodeConcurrency must be an integer between 1 and 8");
  }
  const results = new Array<R>(values.length);
  let nextIndex = 0;
  let failure: unknown;
  const controller = new AbortController();
  const abortFromParent = () => controller.abort(parentSignal?.reason);
  if (parentSignal?.aborted) abortFromParent();
  else parentSignal?.addEventListener("abort", abortFromParent, { once: true });
  async function run(): Promise<void> {
    while (!controller.signal.aborted && nextIndex < values.length) {
      const index = nextIndex;
      nextIndex += 1;
      try {
        results[index] = await worker(values[index], controller.signal);
      } catch (error) {
        if (failure === undefined) failure = error;
        controller.abort();
      }
    }
  }
  try {
    await Promise.all(Array.from({ length: Math.min(concurrency, values.length) }, () => run()));
  } finally {
    parentSignal?.removeEventListener("abort", abortFromParent);
  }
  if (parentSignal?.aborted) throwAbortReason(parentSignal);
  if (failure !== undefined) throw failure;
  return results;
}

function throwAbortReason(signal: AbortSignal): never {
  if (signal.reason instanceof Error) throw signal.reason;
  throw new DOMException("Operation aborted", "AbortError");
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throwAbortReason(signal);
}

async function defaultBlobToDataUrl(blob: Blob, signal: AbortSignal): Promise<string> {
  if (signal.aborted) throw new DOMException("Encoding aborted", "AbortError");
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  const chunkSize = 32_768;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    const end = Math.min(bytes.length, offset + chunkSize);
    const chunk = new Array<number>(end - offset);
    for (let index = offset; index < end; index += 1) chunk[index - offset] = bytes[index];
    binary += String.fromCharCode(...chunk);
  }
  if (signal.aborted) throw new DOMException("Encoding aborted", "AbortError");
  return `data:${AUTOMATION_JPEG_MIME};base64,${btoa(binary)}`;
}

function dataUrlBytes(dataUrl: string): number {
  return new TextEncoder().encode(dataUrl).byteLength;
}

async function encodeBundle(
  bundle: TickBundle,
  tier: CompressionTier,
  concurrency: number,
  encoder: AutomationFrameEncoder,
  signal?: AbortSignal,
): Promise<EncodedFrame[]> {
  const inputs = bundle.frames.map((frame): FrameEncodingInput => {
    if (frame.resource.uploadBlob === null) throw new Error("Cannot encode a disposed image resource");
    return { blob: frame.resource.uploadBlob, source: frame.source, bundle, frame };
  });
  const blobs = await mapConcurrentSafely(inputs, concurrency, async (frame, workerSignal) => {
    const blob = await encoder(frame, tier, workerSignal);
    if (blob.type.toLowerCase() !== AUTOMATION_JPEG_MIME) {
      throw new InvalidJpegOutputError("Automation frame encoder must return a JPEG Blob");
    }
    return blob;
  }, signal);
  return blobs.map((blob, index) => ({
    bundle,
    source: inputs[index].source,
    blob,
  }));
}

export async function prepareVisionPayload(
  input: PrepareVisionPayloadInput,
): Promise<PreparedVisionPayload> {
  throwIfAborted(input.signal);
  if (input.manualDataUrls.length > MAX_MANUAL_IMAGE_COUNT) {
    throw new RangeError(`Manual images cannot exceed ${MAX_MANUAL_IMAGE_COUNT}`);
  }
  if (!Number.isInteger(input.maxEncodedPayloadBytes) || input.maxEncodedPayloadBytes < 1) {
    throw new RangeError("maxEncodedPayloadBytes must be a positive integer");
  }
  const retained = [...input.batch.bundles].sort(chronologicalOrder);
  const sources = sortedSources(input.batch.sources);
  if (sources.length > MAX_AUTOMATION_SOURCE_COUNT) {
    throw new RangeError(`Automation sources cannot exceed ${MAX_AUTOMATION_SOURCE_COUNT}`);
  }
  if (input.manualDataUrls.length + sources.length * retained.length > MAX_REAL_IMAGES_PER_TURN) {
    throw new RangeError(`Real images cannot exceed ${MAX_REAL_IMAGES_PER_TURN}`);
  }
  retained.forEach((bundle) => assertBundleMatchesSourceCohort(bundle, input.batch.sources));
  const manualBytes = input.manualDataUrls.reduce((total, dataUrl) => total + dataUrlBytes(dataUrl), 0);
  if (manualBytes > input.maxEncodedPayloadBytes) {
    throw new Error("Manual images exceed the encoded payload budget");
  }

  const tiers = buildCompressionTiers(input.compression);
  const lockFirstBundle = input.lockFirstBundle ?? false;
  let encodedFrames: EncodedFrame[] = [];
  let selectedTier = tiers.at(-1)!;
  let estimatedBytes = Infinity;
  const droppedTickIndexes: number[] = [];
  for (const tier of tiers) {
    throwIfAborted(input.signal);
    const nextRetained: TickBundle[] = [];
    const nextEncodedFrames: EncodedFrame[] = [];
    for (const bundle of retained) {
      throwIfAborted(input.signal);
      try {
        nextEncodedFrames.push(...await encodeBundle(
          bundle,
          tier,
          input.encodeConcurrency,
          input.encodeFrame,
          input.signal,
        ));
        nextRetained.push(bundle);
      } catch (error) {
        throwIfAborted(input.signal);
        if (error instanceof InvalidJpegOutputError) throw error;
        if (!droppedTickIndexes.includes(bundle.tickIndex)) droppedTickIndexes.push(bundle.tickIndex);
      }
    }
    retained.splice(0, retained.length, ...nextRetained);
    encodedFrames = nextEncodedFrames;
    selectedTier = tier;
    estimatedBytes = manualBytes + encodedFrames.reduce(
      (total, frame) => total + estimateDataUrlBytes(frame.blob.size, AUTOMATION_JPEG_MIME),
      0,
    );
    if (estimatedBytes <= input.maxEncodedPayloadBytes) break;
  }

  while (estimatedBytes > input.maxEncodedPayloadBytes && retained.length > 1) {
    const removeIndex = lockFirstBundle && retained.length > 2 ? 1 : 0;
    const [dropped] = retained.splice(removeIndex, 1);
    droppedTickIndexes.push(dropped.tickIndex);
    encodedFrames = encodedFrames.filter((frame) => frame.bundle !== dropped);
    estimatedBytes = manualBytes + encodedFrames.reduce(
      (total, frame) => total + estimateDataUrlBytes(frame.blob.size, AUTOMATION_JPEG_MIME),
      0,
    );
  }
  if (estimatedBytes > input.maxEncodedPayloadBytes) {
    throw new Error("The latest bundle cannot fit within the encoded payload budget");
  }

  const encodedDataFrames: EncodedDataFrame[] = [];
  const conversionRetained: TickBundle[] = [];
  for (const bundle of retained) {
    throwIfAborted(input.signal);
    const bundleFrames = encodedFrames.filter((frame) => frame.bundle === bundle);
    try {
      const dataUrls = await mapConcurrentSafely(
        bundleFrames,
        input.encodeConcurrency,
        async (frame, workerSignal) => {
          const dataUrl = await (input.blobToDataUrl ?? defaultBlobToDataUrl)(frame.blob, workerSignal);
          if (!dataUrl.startsWith(`data:${AUTOMATION_JPEG_MIME};base64,`)) {
            throw new InvalidJpegOutputError("Automation serialization must return a JPEG Data URL");
          }
          return dataUrl;
        },
        input.signal,
      );
      conversionRetained.push(bundle);
      dataUrls.forEach((dataUrl, index) => encodedDataFrames.push({
        bundle,
        source: bundleFrames[index].source,
        dataUrl,
      }));
    } catch (error) {
      throwIfAborted(input.signal);
      if (error instanceof InvalidJpegOutputError) throw error;
      if (!droppedTickIndexes.includes(bundle.tickIndex)) droppedTickIndexes.push(bundle.tickIndex);
    }
  }
  retained.splice(0, retained.length, ...conversionRetained);
  const automationDataUrls = sources.flatMap((source) => retained.map((bundle) => {
    const encoded = encodedDataFrames.find(
      (frame) => frame.bundle === bundle && frame.source.stableAlias === source.stableAlias,
    );
    if (encoded === undefined) throw new Error("Encoded bundle is missing a source Data URL");
    return encoded.dataUrl;
  }));
  throwIfAborted(input.signal);
  const images = [...input.manualDataUrls, ...automationDataUrls];
  const encodedImageBytes = images.reduce((total, dataUrl) => total + dataUrlBytes(dataUrl), 0);
  if (encodedImageBytes > input.maxEncodedPayloadBytes) {
    throw new Error("Encoded image payload exceeds the byte budget");
  }
  const manifest = buildVisionAutomationManifest({
    manualImageCount: input.manualDataUrls.length,
    sources,
    bundles: retained,
  });
  const result: PreparedVisionPayload = {
    images,
    text: `${manifest}\n\n${input.userText}`,
    manifest,
    encodedImageBytes,
    compressionTier: selectedTier,
    retainedBundles: [...retained],
    droppedTickIndexes,
    sourceCount: sources.length,
    retainedTickCount: retained.length,
  };
  throwIfAborted(input.signal);
  input.batch.commitSelection(retained);
  return result;
}
