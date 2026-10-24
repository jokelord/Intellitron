import assert from "node:assert/strict";
import { test } from "node:test";

import {
  type FrozenBundleBatch,
  TickBundleSampler,
  calculateBundleCapacity,
} from "./bundle-sampler.ts";
import type {
  CaptureSource,
  FullCaptureCallbackToken,
} from "./capture-source.ts";
import { OwnedImageResource, type ObjectUrlApi } from "./image-resources.ts";
import { TickCoordinator } from "./tick-coordinator.ts";
import type { AutomationFrame, SourceDescriptor, TickBundle } from "./types.ts";

const ATTEMPT_COUNT = 10_000;
const SOURCE_COUNT = 5;
const MAX_FRAMES_PER_SOURCE = 50;
const INTERVAL_MS = 100;
const TIMEOUT_MS = 250;
const SLOW_COMPLETION_MS = 150;
const LATE_COMPLETION_MS = 275;

interface CollectionAuditRoot {
  path: string;
  value: object;
}

// Reflection reaches exposed own properties only. Native #private fields,
// module state, closures, and engine internals require public accounting checks.
function auditProductionCollections(
  roots: readonly CollectionAuditRoot[],
  bounds: ReadonlyMap<string, number>,
): void {
  const auditSize = (path: string, size: number) => {
    const maximum = bounds.get(path);
    if (maximum === undefined) throw new Error(`Unknown collection path: ${path}`);
    if (size > maximum) {
      throw new Error(`Collection ${path} size ${size} exceeds explicit maximum ${maximum}`);
    }
  };
  const visit = (
    value: unknown,
    path: string,
    ancestors: Set<object>,
    inspectNonPlain: boolean,
  ): void => {
    if ((typeof value !== "object" && typeof value !== "function") || value === null) return;
    if (Array.isArray(value)) {
      auditSize(path, value.length);
      if (ancestors.has(value)) return;
      const nextAncestors = new Set(ancestors).add(value);
      value.forEach((item) => visit(item, `${path}[]`, nextAncestors, false));
      return;
    }
    if (value instanceof Map) {
      auditSize(path, value.size);
      if (ancestors.has(value)) return;
      const nextAncestors = new Set(ancestors).add(value);
      value.forEach((item, key) => {
        visit(key, `${path}.keys[]`, nextAncestors, false);
        visit(item, `${path}.values[]`, nextAncestors, false);
      });
      return;
    }
    if (value instanceof Set) {
      auditSize(path, value.size);
      if (ancestors.has(value)) return;
      const nextAncestors = new Set(ancestors).add(value);
      value.forEach((item) => visit(item, `${path}.values[]`, nextAncestors, false));
      return;
    }
    if (ancestors.has(value)) return;
    const prototype = Object.getPrototypeOf(value);
    if (!inspectNonPlain && prototype !== Object.prototype && prototype !== null) return;
    const nextAncestors = new Set(ancestors).add(value);
    for (const property of Reflect.ownKeys(value)) {
      const segment = typeof property === "symbol" ? `[${String(property)}]` : property;
      visit(
        Reflect.get(value, property),
        `${path}.${segment}`,
        nextAncestors,
        false,
      );
    }
  };

  for (const root of roots) {
    visit(root.value, root.path, new Set(), true);
  }
}

test("strict collection audit rejects growing and unknown production caches", () => {
  const fixture = { cache: ["first", "second"] };
  assert.throws(
    () => auditProductionCollections(
      [{ path: "fixture", value: fixture }],
      new Map([["fixture.cache", 1]]),
    ),
    /fixture\.cache.*2.*1/i,
  );
  assert.throws(
    () => auditProductionCollections(
      [{ path: "fixture", value: fixture }],
      new Map(),
    ),
    /unknown collection path.*fixture\.cache/i,
  );

  const nestedFixture: {
    nested: { cache: string[]; parent?: object };
  } = { nested: { cache: ["first", "second"] } };
  nestedFixture.nested.parent = nestedFixture;
  assert.throws(
    () => auditProductionCollections(
      [{ path: "fixture", value: nestedFixture }],
      new Map([["fixture.nested.cache", 1]]),
    ),
    /fixture\.nested\.cache.*2.*1/i,
  );
  assert.throws(
    () => auditProductionCollections(
      [{ path: "fixture", value: nestedFixture }],
      new Map(),
    ),
    /unknown collection path.*fixture\.nested\.cache/i,
  );

  const symbolCache = Symbol("cache");
  const symbolFixture = { [symbolCache]: new Set(["first", "second"]) };
  assert.throws(
    () => auditProductionCollections(
      [{ path: "fixture", value: symbolFixture }],
      new Map([["fixture.[Symbol(cache)]", 1]]),
    ),
    /fixture\.\[Symbol\(cache\)\].*2.*1/i,
  );
  assert.throws(
    () => auditProductionCollections(
      [{ path: "fixture", value: symbolFixture }],
      new Map(),
    ),
    /unknown collection path.*fixture\.\[Symbol\(cache\)\]/i,
  );
});

type TimerRole =
  | "coordinator_timeout"
  | "coordinator_interval"
  | "source_slow"
  | "source_late";

interface FakeTimer {
  attempt: number;
  delay: number;
  dueAt: number;
  handler: () => void;
  role: TimerRole;
  sequence: number;
}

class ExecutableFakeClock {
  now = 0;
  maxActiveTimers = 0;
  maxActiveCoordinatorTimers = 0;
  intervalFiredCount = 0;
  zeroDelayIntervalFiredCount = 0;

  readonly timeoutScheduledAttempts = new Set<number>();
  readonly timeoutFiredAttempts = new Set<number>();
  readonly intervalScheduledAttempts = new Set<number>();
  readonly zeroDelayIntervalScheduledAttempts = new Set<number>();

  private readonly activeTimers = new Set<FakeTimer>();
  private attemptProvider: () => number = () => 0;
  private nextSequence = 0;

  setAttemptProvider(provider: () => number): void {
    this.attemptProvider = provider;
  }

  get activeTimerCount(): number {
    return this.activeTimers.size;
  }

  get activeCoordinatorTimerCount(): number {
    return [...this.activeTimers].filter((timer) => timer.role.startsWith("coordinator_")).length;
  }

  readonly setTimeout = (handler: () => void, delay: number): ReturnType<typeof setTimeout> => {
    const role: TimerRole = delay === TIMEOUT_MS
      ? "coordinator_timeout"
      : "coordinator_interval";
    const attempt = this.attemptProvider();
    const activeCoordinatorTimers = [...this.activeTimers].filter((timer) => (
      timer.role.startsWith("coordinator_")
    ));
    if (activeCoordinatorTimers.length !== 0) {
      throw new Error(
        `cannot schedule ${role} for attempt ${attempt} while ${activeCoordinatorTimers
          .map((timer) => `${timer.role}:${timer.attempt}:${timer.delay}`)
          .join(",")}`,
      );
    }
    if (role === "coordinator_timeout") {
      assert.equal(this.timeoutScheduledAttempts.has(attempt), false, `duplicate timeout for attempt ${attempt}`);
      this.timeoutScheduledAttempts.add(attempt);
    } else {
      assert.equal(this.intervalScheduledAttempts.has(attempt), false, `duplicate interval for attempt ${attempt}`);
      this.intervalScheduledAttempts.add(attempt);
      if (delay === 0) this.zeroDelayIntervalScheduledAttempts.add(attempt);
    }
    return this.schedule(role, attempt, delay, handler) as unknown as ReturnType<typeof setTimeout>;
  };

  readonly clearTimeout = (handle: ReturnType<typeof setTimeout>): void => {
    this.activeTimers.delete(handle as unknown as FakeTimer);
  };

  scheduleSource(role: "source_slow" | "source_late", attempt: number, delay: number, handler: () => void): void {
    this.schedule(role, attempt, delay, handler);
  }

  runNext(): FakeTimer {
    const timer = [...this.activeTimers].sort((left, right) => (
      left.dueAt - right.dueAt || left.sequence - right.sequence
    ))[0];
    assert.ok(timer, "scheduler must always have executable work before 10,000 attempts settle");
    this.activeTimers.delete(timer);
    this.now = timer.dueAt;
    const startsBefore = this.attemptProvider();
    timer.handler();
    if (timer.role === "coordinator_interval") {
      this.intervalFiredCount += 1;
      if (timer.delay === 0) this.zeroDelayIntervalFiredCount += 1;
      assert.equal(
        this.attemptProvider(),
        startsBefore + 1,
        "each recurring interval callback starts exactly one synchronized tick",
      );
    } else if (timer.role === "coordinator_timeout") {
      this.timeoutFiredAttempts.add(timer.attempt);
    }
    return timer;
  }

  private schedule(role: TimerRole, attempt: number, delay: number, handler: () => void): FakeTimer {
    assert.ok(Number.isFinite(delay) && delay >= 0);
    const timer = {
      attempt,
      delay,
      dueAt: this.now + delay,
      handler,
      role,
      sequence: this.nextSequence,
    };
    this.nextSequence += 1;
    this.activeTimers.add(timer);
    this.maxActiveTimers = Math.max(this.maxActiveTimers, this.activeTimerCount);
    this.maxActiveCoordinatorTimers = Math.max(
      this.maxActiveCoordinatorTimers,
      this.activeCoordinatorTimerCount,
    );
    return timer;
  }
}

class TrackedImageResource extends OwnedImageResource {
  disposeCalls = 0;
  effectiveDisposals = 0;

  override dispose(): void {
    this.disposeCalls += 1;
    if (!this.disposed) this.effectiveDisposals += 1;
    super.dispose();
  }
}

class SoakSource implements CaptureSource {
  readonly descriptor: SourceDescriptor;
  starts = 0;
  disposeCount = 0;
  listenerAdds = 0;
  listenerRemoves = 0;
  leaseActive = true;

  private readonly endedListeners = new Set<() => void>();
  private readonly captureAttempt: (
    sourceIndex: number,
    attempt: number,
    signal: AbortSignal,
  ) => Promise<AutomationFrame>;
  private readonly sourceIndex: number;

  constructor(
    sourceDescriptor: SourceDescriptor,
    captureAttempt: (
      sourceIndex: number,
      attempt: number,
      signal: AbortSignal,
    ) => Promise<AutomationFrame>,
    sourceIndex: number,
  ) {
    this.descriptor = sourceDescriptor;
    this.captureAttempt = captureAttempt;
    this.sourceIndex = sourceIndex;
  }

  capture(signal: AbortSignal): Promise<AutomationFrame> {
    assert.equal(this.leaseActive, true, "capture requires an active fake source lease");
    this.starts += 1;
    return this.captureAttempt(this.sourceIndex, this.starts, signal);
  }

  onEnded(listener: () => void): () => void {
    this.listenerAdds += 1;
    this.endedListeners.add(listener);
    return () => {
      if (!this.endedListeners.delete(listener)) return;
      this.listenerRemoves += 1;
    };
  }

  dispose(): void {
    if (!this.leaseActive) return;
    this.disposeCount += 1;
    this.leaseActive = false;
    this.endedListeners.clear();
  }

  get activeListenerCount(): number {
    return this.endedListeners.size;
  }
}

function descriptor(index: number): SourceDescriptor {
  return {
    sourceId: `soak-camera-${index + 1}`,
    kind: "camera",
    stableAlias: `camera_${index + 1}`,
    label: `Soak Camera ${index + 1}`,
  };
}

function tokenForCycle(cycleGeneration: number): FullCaptureCallbackToken {
  return {
    pageInstanceId: "soak-page",
    pageGeneration: 1,
    mode: "agent",
    contextId: "soak-context",
    automationTaskId: "soak-task",
    captureCycleId: `soak-cycle-${cycleGeneration}`,
    cycleGeneration,
  };
}

function isTransitionBoundary(attempt: number): boolean {
  return attempt < ATTEMPT_COUNT && attempt % 1_000 === 0;
}

function isTimedOutAttempt(attempt: number): boolean {
  return attempt % 257 === 0 || isTransitionBoundary(attempt);
}

function isPostTransitionImmediateAttempt(attempt: number): boolean {
  return attempt > 1 && isTransitionBoundary(attempt - 1);
}

function isSlowAttempt(attempt: number): boolean {
  return !isTimedOutAttempt(attempt)
    && (attempt % 41 === 0 || isPostTransitionImmediateAttempt(attempt));
}

async function flushMicrotasks(rounds = 24): Promise<void> {
  for (let index = 0; index < rounds; index += 1) await Promise.resolve();
}

test("10,000 recurring scheduled ticks keep scheduler, collections, and resources bounded", async () => {
  const clock = new ExecutableFakeClock();

  // These ledgers are audit-only and are never attached to a production ownership object.
  const resources: TrackedImageResource[] = [];
  const createdUrls = new Set<string>();
  const revokedUrlCounts = new Map<string, number>();
  let nextUrlId = 0;
  const objectUrlApi: ObjectUrlApi = {
    createObjectURL() {
      const url = `blob:soak-${nextUrlId}`;
      nextUrlId += 1;
      assert.equal(createdUrls.has(url), false);
      createdUrls.add(url);
      return url;
    },
    revokeObjectURL(url) {
      revokedUrlCounts.set(url, (revokedUrlCounts.get(url) ?? 0) + 1);
    },
  };

  let pendingLate: { attempt: number; frame: AutomationFrame } | null = null;
  const createFrame = (sourceIndex: number, attempt: number): AutomationFrame => {
    const source = descriptor(sourceIndex);
    const resource = new TrackedImageResource(
      new Blob([`${attempt}:${sourceIndex}:upload`], { type: "image/jpeg" }),
      new Blob([`${attempt}:${sourceIndex}:thumbnail`], { type: "image/jpeg" }),
      objectUrlApi,
    );
    resources.push(resource);
    const uploadUrl = resource.acquireObjectURL("upload");
    resource.acquireObjectURL("thumbnail");
    if ((attempt + sourceIndex) % 7 === 0) {
      assert.equal(resource.releaseObjectURL(uploadUrl), true);
      assert.equal(resource.releaseObjectURL(uploadUrl), false);
    }
    return { source, resource, capturedAtMs: clock.now };
  };
  const captureAttempt = (
    sourceIndex: number,
    attempt: number,
    _signal: AbortSignal,
  ): Promise<AutomationFrame> => {
    if (sourceIndex === 1 && attempt % 37 === 0) {
      return Promise.reject(new Error("deterministic source 2 failure"));
    }
    if (sourceIndex === 4 && attempt % 113 === 0) {
      return Promise.reject(new Error("deterministic source 5 failure"));
    }
    const frame = createFrame(sourceIndex, attempt);
    if (sourceIndex === 2 && isTimedOutAttempt(attempt)) {
      assert.equal(pendingLate, null);
      pendingLate = { attempt, frame };
      return new Promise((resolve) => {
        clock.scheduleSource("source_late", attempt, LATE_COMPLETION_MS, () => {
          assert.equal(pendingLate?.attempt, attempt);
          pendingLate = null;
          resolve(frame);
        });
      });
    }
    if (sourceIndex === 2 && isSlowAttempt(attempt)) {
      return new Promise((resolve) => {
        clock.scheduleSource("source_slow", attempt, SLOW_COMPLETION_MS, () => resolve(frame));
      });
    }
    return Promise.resolve(frame);
  };

  const sources = Array.from(
    { length: SOURCE_COUNT },
    (_, sourceIndex) => new SoakSource(descriptor(sourceIndex), captureAttempt, sourceIndex),
  );
  clock.setAttemptProvider(() => sources[0].starts);

  let rngState = 0;
  const rng = () => {
    rngState = (rngState + 37) % 997;
    return rngState / 997;
  };
  let manualImageCount = 0;
  const createSampler = () => new TickBundleSampler({
    maxFramesPerSource: MAX_FRAMES_PER_SOURCE,
    manualImageCount,
    sources: sources.map((source) => source.descriptor),
    rng,
  });
  let sampler = createSampler();
  let sentBatch: FrozenBundleBatch | null = null;
  let sentBatchCapacity = 0;
  let currentToken = tokenForCycle(1);
  let committedBundleCount = 0;
  let cycleFirstBundleCount = 0;
  let transactionCount = 0;
  let automaticTransitionStartCount = 0;
  let boundaryStartFalseCount = 0;
  let slowImmediateCompetitionCount = 0;
  let samplerEvictionCount = 0;
  let maxLiveBundles = 0;
  let maxLiveResources = 0;
  const observedCapacities = new Set<number>([sampler.capacity]);
  const processedAttempts = new Set<number>();

  const coordinator = new TickCoordinator({
    sources,
    token: currentToken,
    intervalMs: INTERVAL_MS,
    timeoutMs: TIMEOUT_MS,
    failureLimit: ATTEMPT_COUNT,
    getCurrentToken: () => currentToken,
    onBundle(bundle) {
      committedBundleCount += 1;
      if (bundle.tickIndex === 0) cycleFirstBundleCount += 1;
      const previouslyRetained = sampler.bundles;
      sampler.add(bundle);
      const retainedAfterAdd = new Set(sampler.bundles);
      samplerEvictionCount += previouslyRetained.filter((item) => !retainedAfterAdd.has(item)).length;
    },
    onPause(reason) {
      assert.fail(`soak must not pause: ${reason}`);
    },
    now: () => clock.now,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  const currentOwnedBundles = (): readonly TickBundle[] => [
    ...sampler.bundles,
    ...(sentBatch?.bundles ?? []),
  ];

  const assertProductionCollectionBounds = () => {
    const coordinatorState = coordinator as unknown as {
      activeBatch: { settlements: readonly object[] } | null;
    };
    const roots: CollectionAuditRoot[] = [
      { path: "coordinator", value: coordinator },
      { path: "sampler", value: sampler },
      ...sources.map((source) => ({ path: "source", value: source })),
      ...resources.map((resource) => ({
        path: "resource",
        value: resource,
      })),
    ];
    if (sentBatch !== null) roots.push({ path: "sentBatch", value: sentBatch });
    assert.ok(coordinatorState.activeBatch === null || Array.isArray(coordinatorState.activeBatch.settlements));
    const bounds = new Map<string, number>([
      ["coordinator.sources", SOURCE_COUNT],
      ["coordinator.failureCounts", SOURCE_COUNT],
      ["coordinator.removeEndedListeners", SOURCE_COUNT],
      ["coordinator.activeBatch.settlements", SOURCE_COUNT],
      ["sampler.retained", sampler.capacity],
      ["sampler.sources", SOURCE_COUNT],
      ["sampler.retained[].frames", SOURCE_COUNT],
      ["sentBatch.ownedBundles", sentBatchCapacity],
      ["sentBatch.sources", SOURCE_COUNT],
      ["sentBatch.ownedBundles[].frames", SOURCE_COUNT],
      ["source.endedListeners", 1],
      ["resource.objectUrls", 2],
    ]);
    auditProductionCollections(roots, bounds);
  };

  const assertBoundedOwnership = (atTransactionBoundary = false) => {
    const expectedCapacity = calculateBundleCapacity({
      maxFramesPerSource: MAX_FRAMES_PER_SOURCE,
      manualImageCount,
      sourceCount: SOURCE_COUNT,
    });
    assert.equal(sampler.capacity, expectedCapacity);
    assert.ok(sampler.bundles.length <= expectedCapacity);
    assert.ok(sampler.bundles.length * SOURCE_COUNT <= expectedCapacity * SOURCE_COUNT);
    assert.ok(sentBatch === null || sentBatch.bundles.length <= sentBatchCapacity);

    const currentlyOwned = new Set(
      currentOwnedBundles().flatMap((bundle) => bundle.frames.map((frame) => frame.resource)),
    );
    if (pendingLate !== null) currentlyOwned.add(pendingLate.frame.resource);
    const liveResources = resources.filter((resource) => !resource.disposed);
    assert.equal(liveResources.length, currentlyOwned.size, "only active, sent, or pending owners retain Blobs");
    liveResources.forEach((resource) => assert.equal(currentlyOwned.has(resource), true));
    assert.ok(clock.activeCoordinatorTimerCount <= 1);
    assert.ok(clock.activeTimerCount <= 2);
    sources.forEach((source) => {
      assert.ok(source.activeListenerCount <= 1);
      assert.equal(source.listenerAdds, 1);
    });
    maxLiveBundles = Math.max(maxLiveBundles, currentOwnedBundles().length);
    maxLiveResources = Math.max(maxLiveResources, liveResources.length);
    if (atTransactionBoundary) {
      assert.equal(sentBatch === null ? 0 : 1, 1, "one frozen/sent batch exists at a boundary");
      assert.equal(sampler instanceof TickBundleSampler, true, "one current pool exists at a boundary");
    }
  };

  const processSettledAttempts = () => {
    while (processedAttempts.size < ATTEMPT_COUNT) {
      const attempt = processedAttempts.size + 1;
      if (!clock.intervalScheduledAttempts.has(attempt) && !clock.timeoutFiredAttempts.has(attempt)) return;
      if (attempt % 73 === 0) {
        manualImageCount = manualImageCount === 0 ? 9 : 0;
        const previouslyRetained = sampler.bundles;
        sampler.setManualImageCount(manualImageCount);
        const retainedAfterShrink = new Set(sampler.bundles);
        samplerEvictionCount += previouslyRetained.filter((item) => !retainedAfterShrink.has(item)).length;
        observedCapacities.add(sampler.capacity);
      }
      processedAttempts.add(attempt);
      assertBoundedOwnership();
      if (attempt % 100 === 0 || clock.timeoutFiredAttempts.has(attempt)) {
        assertProductionCollectionBounds();
      }
    }
  };

  const rotateTransaction = async (attempt: number, transition: boolean) => {
    await coordinator.freeze();
    sentBatch?.dispose();
    sentBatchCapacity = sampler.capacity;
    sentBatch = sampler.freeze();
    sampler = createSampler();
    transactionCount += 1;
    if (transition) {
      currentToken = tokenForCycle(transactionCount + 1);
      coordinator.transitionCycle(currentToken);
      assert.equal(
        await coordinator.start(),
        false,
        "boundary start must join the pending cleanup barrier without starting overlap",
      );
      boundaryStartFalseCount += 1;
    }
    assertBoundedOwnership(true);
    assertProductionCollectionBounds();
    assert.equal(sources[0].starts, attempt);
  };

  assert.equal(await coordinator.start(), true);
  await flushMicrotasks();
  processSettledAttempts();
  assert.equal(processedAttempts.has(1), true);

  let expectedAutomaticStart: number | null = null;
  while (!processedAttempts.has(ATTEMPT_COUNT)) {
    const timer = clock.runNext();
    await flushMicrotasks();
    processSettledAttempts();

    if (timer.role === "coordinator_timeout" && isTransitionBoundary(timer.attempt)) {
      await rotateTransaction(timer.attempt, true);
      expectedAutomaticStart = timer.attempt + 1;
    }

    if (timer.role === "source_late" && expectedAutomaticStart !== null) {
      await flushMicrotasks();
      assert.equal(
        sources[0].starts,
        expectedAutomaticStart,
        "late cleanup must automatically start the transitioned cycle",
      );
      assert.equal(isPostTransitionImmediateAttempt(expectedAutomaticStart), true);
      assert.equal(
        clock.intervalScheduledAttempts.has(expectedAutomaticStart),
        false,
        "dual barrier schedulers must not install an interval during slow immediate capture",
      );
      assert.equal(clock.activeCoordinatorTimerCount, 1);
      assert.equal(clock.activeTimerCount, 2);
      slowImmediateCompetitionCount += 1;
      automaticTransitionStartCount += 1;
      expectedAutomaticStart = null;
      processSettledAttempts();
    }
    if (timer.role === "source_slow" && isPostTransitionImmediateAttempt(timer.attempt)) {
      for (
        let drain = 0;
        drain < 128 && !clock.zeroDelayIntervalScheduledAttempts.has(timer.attempt);
        drain += 1
      ) {
        await Promise.resolve();
      }
      assert.equal(
        clock.zeroDelayIntervalScheduledAttempts.has(timer.attempt),
        true,
        "slow immediate completion must install exactly one deterministic delay-0 interval",
      );
    }
    assert.ok(sources[0].starts <= ATTEMPT_COUNT, "scheduler must not start attempt 10,001");
  }

  await rotateTransaction(ATTEMPT_COUNT, false);
  assert.equal(clock.activeTimerCount, 0);
  assert.equal(expectedAutomaticStart, null);

  const expectedTimeoutAttempts = Array.from(
    { length: ATTEMPT_COUNT },
    (_, index) => index + 1,
  ).filter(isTimedOutAttempt);
  const expectedFailedAttempts = Array.from(
    { length: ATTEMPT_COUNT },
    (_, index) => index + 1,
  ).filter((attempt) => (
    attempt % 37 === 0 || attempt % 113 === 0 || isTimedOutAttempt(attempt)
  )).length;
  const expectedZeroDelayIntervals = Array.from(
    { length: ATTEMPT_COUNT },
    (_, index) => index + 1,
  ).filter((attempt) => (
    !isTransitionBoundary(attempt) && (isTimedOutAttempt(attempt) || isSlowAttempt(attempt))
  )).length;

  assert.equal(processedAttempts.size, ATTEMPT_COUNT);
  assert.equal(committedBundleCount, ATTEMPT_COUNT - expectedFailedAttempts);
  assert.equal(clock.timeoutScheduledAttempts.size, ATTEMPT_COUNT);
  assert.deepEqual([...clock.timeoutFiredAttempts], expectedTimeoutAttempts);
  assert.equal(clock.intervalScheduledAttempts.size, ATTEMPT_COUNT - 9);
  assert.equal(clock.intervalFiredCount, ATTEMPT_COUNT - 1 - 9);
  assert.equal(clock.zeroDelayIntervalScheduledAttempts.size, expectedZeroDelayIntervals);
  assert.equal(clock.zeroDelayIntervalFiredCount, expectedZeroDelayIntervals);
  assert.equal(clock.maxActiveCoordinatorTimers, 1);
  assert.ok(clock.maxActiveTimers <= 2);
  assert.equal(transactionCount, 10);
  assert.equal(automaticTransitionStartCount, 9);
  assert.equal(boundaryStartFalseCount, 9);
  assert.equal(slowImmediateCompetitionCount, 9);
  assert.equal(cycleFirstBundleCount, 10);
  assert.ok(samplerEvictionCount > 0);
  assert.deepEqual([...observedCapacities].sort((left, right) => left - right), [48, 50]);
  assert.ok(maxLiveBundles <= 100, "no all-captured TickBundle history is retained");
  assert.ok(maxLiveResources <= 100 * SOURCE_COUNT + 1, "live image history stays transaction-bounded");
  sources.forEach((source) => assert.equal(source.starts, ATTEMPT_COUNT));

  await coordinator.abort();
  sampler.dispose();
  sentBatch?.dispose();

  assert.equal(clock.activeTimerCount, 0);
  sources.forEach((source) => {
    assert.equal(source.leaseActive, false);
    assert.equal(source.disposeCount, 1);
    assert.equal(source.activeListenerCount, 0);
    assert.equal(source.listenerAdds, 1);
    assert.equal(source.listenerRemoves, 1);
  });
  assert.ok(resources.length > 0);
  resources.forEach((resource) => {
    assert.equal(resource.disposeCalls, 1);
    assert.equal(resource.effectiveDisposals, 1);
    assert.equal(resource.disposed, true);
    assert.equal(resource.uploadBlob, null);
    assert.equal(resource.thumbnailBlob, null);
  });
  assert.equal(createdUrls.size, resources.length * 2);
  assert.equal(revokedUrlCounts.size, createdUrls.size);
  createdUrls.forEach((url) => assert.equal(revokedUrlCounts.get(url), 1));
});
