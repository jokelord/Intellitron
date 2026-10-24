import { disposeBundle } from "./image-resources.ts";
import type { SourceDescriptor, TickBundle } from "./types.ts";

export const MAX_REAL_IMAGES_PER_TURN = 250;
export const MAX_MANUAL_IMAGE_COUNT = 9;
export const MAX_AUTOMATION_SOURCE_COUNT = 241;

export interface BundleCapacityInput {
  maxFramesPerSource: number;
  manualImageCount: number;
  sourceCount: number;
}

export interface TickBundleSamplerOptions extends Omit<BundleCapacityInput, "sourceCount"> {
  sources: readonly SourceDescriptor[];
  lockFirstBundle?: boolean;
  maxEncodedPayloadBytes?: number;
  manualEncodedBytes?: number;
  rng?: () => number;
}

function chronologicalOrder(left: TickBundle, right: TickBundle): number {
  return left.timestampMs - right.timestampMs || left.tickIndex - right.tickIndex;
}

function assertInteger(name: string, value: number, minimum: number, maximum: number): void {
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new RangeError(`${name} must be an integer between ${minimum} and ${maximum}`);
  }
}

export function calculateBundleCapacity(input: BundleCapacityInput): number {
  assertInteger("maxFramesPerSource", input.maxFramesPerSource, 1, MAX_REAL_IMAGES_PER_TURN);
  assertInteger("manualImageCount", input.manualImageCount, 0, MAX_MANUAL_IMAGE_COUNT);
  assertInteger("sourceCount", input.sourceCount, 1, MAX_AUTOMATION_SOURCE_COUNT);
  return Math.min(
    input.maxFramesPerSource,
    Math.floor((MAX_REAL_IMAGES_PER_TURN - input.manualImageCount) / input.sourceCount),
  );
}

function freezeSourceCohort(sources: readonly SourceDescriptor[]): readonly SourceDescriptor[] {
  assertInteger("sourceCount", sources.length, 1, MAX_AUTOMATION_SOURCE_COUNT);
  const aliases = new Set<string>();
  const sourceIds = new Set<string>();
  const frozen = sources.map((source) => {
    if (aliases.has(source.stableAlias) || sourceIds.has(source.sourceId)) {
      throw new Error("Source cohort contains duplicate aliases or source IDs");
    }
    aliases.add(source.stableAlias);
    sourceIds.add(source.sourceId);
    return Object.freeze({ ...source });
  });
  return Object.freeze(frozen);
}

export function assertBundleMatchesSourceCohort(
  bundle: TickBundle,
  sources: readonly SourceDescriptor[],
): void {
  if (bundle.frames.length !== sources.length) {
    throw new Error(`Tick bundle ${bundle.tickIndex} does not match the immutable source cohort`);
  }
  const framesByAlias = new Map(bundle.frames.map((frame) => [frame.source.stableAlias, frame]));
  if (framesByAlias.size !== sources.length || sources.some((source) => {
    const frame = framesByAlias.get(source.stableAlias);
    return frame === undefined
      || frame.source.sourceId !== source.sourceId
      || frame.source.kind !== source.kind;
  })) {
    throw new Error(`Tick bundle ${bundle.tickIndex} does not match the immutable source cohort`);
  }
}

export class FrozenBundleBatch {
  private ownedBundles: TickBundle[];
  private disposed = false;

  readonly sources: readonly SourceDescriptor[];

  constructor(bundles: readonly TickBundle[], sources: readonly SourceDescriptor[]) {
    this.sources = freezeSourceCohort(sources);
    bundles.forEach((bundle) => assertBundleMatchesSourceCohort(bundle, this.sources));
    this.ownedBundles = [...bundles].sort(chronologicalOrder);
  }

  get bundles(): readonly TickBundle[] {
    return [...this.ownedBundles];
  }

  commitSelection(retained: readonly TickBundle[]): void {
    if (this.disposed) throw new Error("FrozenBundleBatch is disposed");
    const retainedSet = new Set(retained);
    if (retainedSet.size !== retained.length
      || retained.some((bundle) => !this.ownedBundles.includes(bundle))) {
      throw new Error("Frozen batch selection must contain owned bundles only");
    }
    const dropped = this.ownedBundles.filter((bundle) => !retainedSet.has(bundle));
    this.ownedBundles = [...retained].sort(chronologicalOrder);
    dropped.forEach(disposeBundle);
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.ownedBundles.forEach(disposeBundle);
    this.ownedBundles = [];
  }
}

export class TickBundleSampler {
  private retained: TickBundle[] = [];
  private manualImageCount: number;
  private readonly maxFramesPerSource: number;
  private readonly maxEncodedPayloadBytes: number;
  private manualEncodedBytes: number;
  private readonly lockFirstBundle: boolean;
  private readonly rng: () => number;
  private frozen = false;
  private disposed = false;

  readonly sources: readonly SourceDescriptor[];

  constructor(options: TickBundleSamplerOptions) {
    this.sources = freezeSourceCohort(options.sources);
    calculateBundleCapacity({ ...options, sourceCount: this.sources.length });
    this.maxFramesPerSource = options.maxFramesPerSource;
    this.manualImageCount = options.manualImageCount;
    this.maxEncodedPayloadBytes = options.maxEncodedPayloadBytes ?? Number.POSITIVE_INFINITY;
    this.manualEncodedBytes = options.manualEncodedBytes ?? 0;
    this.lockFirstBundle = options.lockFirstBundle ?? false;
    if (typeof this.lockFirstBundle !== "boolean") {
      throw new TypeError("lockFirstBundle must be boolean");
    }
    if ((!Number.isInteger(this.maxEncodedPayloadBytes) && this.maxEncodedPayloadBytes !== Number.POSITIVE_INFINITY)
      || this.maxEncodedPayloadBytes <= 0) {
      throw new RangeError("maxEncodedPayloadBytes must be a positive integer");
    }
    if (!Number.isInteger(this.manualEncodedBytes) || this.manualEncodedBytes < 0
      || this.manualEncodedBytes > this.maxEncodedPayloadBytes) {
      throw new RangeError("manualEncodedBytes must fit the encoded payload budget");
    }
    this.rng = options.rng ?? Math.random;
  }

  get bundles(): readonly TickBundle[] {
    return [...this.retained];
  }

  get capacity(): number {
    return calculateBundleCapacity({
      maxFramesPerSource: this.maxFramesPerSource,
      manualImageCount: this.manualImageCount,
      sourceCount: this.sources.length,
    });
  }

  get estimatedEncodedBytes(): number {
    return this.retained.reduce(
      (total, bundle) => total + this.bundleEncodedBytes(bundle),
      0,
    );
  }

  add(bundle: TickBundle): void {
    if (this.disposed || this.frozen) throw new Error("TickBundleSampler does not own an active pool");
    assertBundleMatchesSourceCohort(bundle, this.sources);
    if (this.retained.some((current) => current.tickIndex === bundle.tickIndex)) {
      throw new Error(`Duplicate tick index ${bundle.tickIndex}`);
    }
    if (this.capacity === 1) {
      for (const current of this.retained) disposeBundle(current);
      this.retained = [bundle];
      return;
    }
    if (this.retained.length < this.capacity) {
      const nextRetained = [...this.retained, bundle].sort(chronologicalOrder);
      const removed = this.trimForByteBudget(nextRetained);
      this.retained = nextRetained;
      removed.forEach(disposeBundle);
      return;
    }
    const nextRetained = [...this.retained, bundle].sort(chronologicalOrder);
    const removeIndex = this.lockFirstBundle
      ? this.randomMiddleIndex(nextRetained.length)
      : this.randomEvictExceptLatestIndex(nextRetained.length);
    const removed = nextRetained.splice(removeIndex, 1);
    removed.push(...this.trimForByteBudget(nextRetained));
    this.retained = nextRetained;
    removed.forEach(disposeBundle);
  }

  setManualImageCount(manualImageCount: number, manualEncodedBytes = 0): void {
    if (this.disposed || this.frozen) throw new Error("TickBundleSampler does not own an active pool");
    assertInteger("manualImageCount", manualImageCount, 0, MAX_MANUAL_IMAGE_COUNT);
    if (!Number.isInteger(manualEncodedBytes) || manualEncodedBytes < 0
      || manualEncodedBytes > this.maxEncodedPayloadBytes) {
      throw new RangeError("manualEncodedBytes must fit the encoded payload budget");
    }
    const nextCapacity = calculateBundleCapacity({
      maxFramesPerSource: this.maxFramesPerSource,
      manualImageCount,
      sourceCount: this.sources.length,
    });
    const nextRetained = [...this.retained];
    const removed: TickBundle[] = [];
    while (nextRetained.length > nextCapacity
      || (nextRetained.length > 1
        && manualEncodedBytes + this.encodedBytesFor(nextRetained) > this.maxEncodedPayloadBytes)) {
      if (nextCapacity === 1) {
        removed.push(...nextRetained.splice(0, nextRetained.length - 1));
      } else if (this.lockFirstBundle && nextRetained.length === 2) {
        removed.push(...nextRetained.splice(0, 1));
      } else {
        const removeIndex = this.lockFirstBundle
          ? this.randomMiddleIndex(nextRetained.length)
          : this.randomEvictExceptLatestIndex(nextRetained.length);
        removed.push(...nextRetained.splice(removeIndex, 1));
      }
    }
    this.manualImageCount = manualImageCount;
    this.manualEncodedBytes = manualEncodedBytes;
    this.retained = nextRetained.sort(chronologicalOrder);
    removed.forEach(disposeBundle);
  }

  freeze(): FrozenBundleBatch {
    if (this.disposed || this.frozen) throw new Error("TickBundleSampler does not own an active pool");
    this.frozen = true;
    const batch = new FrozenBundleBatch(this.retained, this.sources);
    this.retained = [];
    return batch;
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    for (const bundle of this.retained) disposeBundle(bundle);
    this.retained = [];
  }

  private randomMiddleIndex(length: number): number {
    const random = this.rng();
    if (!Number.isFinite(random) || random < 0 || random >= 1) {
      throw new RangeError("rng must return a number from 0 (inclusive) to 1 (exclusive)");
    }
    const middleCount = length - 2;
    return 1 + Math.floor(random * middleCount);
  }

  private randomEvictExceptLatestIndex(length: number): number {
    const random = this.rng();
    if (!Number.isFinite(random) || random < 0 || random >= 1) {
      throw new RangeError("rng must return a number from 0 (inclusive) to 1 (exclusive)");
    }
    return Math.floor(random * (length - 1));
  }

  private trimForByteBudget(retained: TickBundle[]): TickBundle[] {
    const removed: TickBundle[] = [];
    while (retained.length > 1
      && this.manualEncodedBytes + this.encodedBytesFor(retained) > this.maxEncodedPayloadBytes) {
      const removeIndex = this.lockFirstBundle
        ? (retained.length === 2 ? 0 : this.randomMiddleIndex(retained.length))
        : this.randomEvictExceptLatestIndex(retained.length);
      removed.push(...retained.splice(removeIndex, 1));
    }
    return removed;
  }

  private encodedBytesFor(bundles: readonly TickBundle[]): number {
    return bundles.reduce((total, bundle) => total + this.bundleEncodedBytes(bundle), 0);
  }

  private bundleEncodedBytes(bundle: TickBundle): number {
    return bundle.frames.reduce((total, frame) => {
      const blob = frame.resource.uploadBlob;
      if (blob === null) return total;
      const mime = blob.type || "image/jpeg";
      return total + new TextEncoder().encode(`data:${mime};base64,`).byteLength
        + 4 * Math.ceil(blob.size / 3);
    }, 0);
  }
}
