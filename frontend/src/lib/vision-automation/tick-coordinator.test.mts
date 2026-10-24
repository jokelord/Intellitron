import assert from "node:assert/strict";
import { test } from "node:test";

import { sameCaptureCallbackToken, TickCoordinator } from "./tick-coordinator.ts";
import type {
  CaptureCallbackToken,
  CaptureSource,
  FullCaptureCallbackToken,
} from "./capture-source.ts";
import type { ImageResource, SourceDescriptor, TickBundle } from "./types.ts";

const token: FullCaptureCallbackToken = {
  pageInstanceId: "page-instance-1",
  pageGeneration: 1,
  mode: "agent",
  contextId: "top-level-agent-1",
  automationTaskId: "task-1",
  captureCycleId: "cycle-1",
  cycleGeneration: 1,
};

function descriptor(index: number): SourceDescriptor {
  return {
    sourceId: `camera-${index}`,
    kind: "camera",
    stableAlias: `camera_${index}`,
    label: `Camera ${index}`,
  };
}

function ownedResource(label: string, disposed: string[]): ImageResource {
  return {
    uploadBlob: new Blob([label], { type: "image/jpeg" }),
    thumbnailBlob: null,
    disposed: false,
    acquireObjectURL() { throw new Error("unused"); },
    releaseObjectURL() { return false; },
    dispose() {
      if (this.disposed) return;
      this.disposed = true;
      this.uploadBlob = null;
      disposed.push(label);
    },
  };
}

class FakeSource implements CaptureSource {
  readonly descriptor: SourceDescriptor;
  starts = 0;
  disposeCount = 0;
  listenerAdds = 0;
  listenerRemoves = 0;
  private endedListeners = new Set<() => void>();
  private readonly captureImpl: (signal: AbortSignal, attempt: number) => Promise<ImageResource>;

  constructor(
    index: number,
    captureImpl: (signal: AbortSignal, attempt: number) => Promise<ImageResource>,
  ) {
    this.descriptor = descriptor(index);
    this.captureImpl = captureImpl;
  }

  capture(signal: AbortSignal) {
    this.starts += 1;
    return this.captureImpl(signal, this.starts).then((resource) => ({
      source: this.descriptor,
      resource,
    }));
  }

  onEnded(listener: () => void) {
    this.listenerAdds += 1;
    this.endedListeners.add(listener);
    return () => {
      this.listenerRemoves += 1;
      this.endedListeners.delete(listener);
    };
  }

  emitEnded() {
    this.endedListeners.forEach((listener) => listener());
  }

  dispose() {
    this.disposeCount += 1;
    this.endedListeners.clear();
  }
}

function coordinator(
  sources: readonly CaptureSource[],
  options: {
    onBundle?: (bundle: TickBundle) => void;
    onBundleRejected?: (error: unknown) => void;
    onPause?: (reason: string) => void;
    getCurrentToken?: () => FullCaptureCallbackToken | null;
    now?: () => number;
    setTimeout?: (handler: () => void, delay: number) => ReturnType<typeof setTimeout>;
    clearTimeout?: (handle: ReturnType<typeof setTimeout>) => void;
    failureLimit?: number;
    timeoutMs?: number;
  } = {},
) {
  return new TickCoordinator({
    sources,
    token,
    intervalMs: 100,
    timeoutMs: options.timeoutMs ?? 50,
    failureLimit: options.failureLimit ?? 3,
    getCurrentToken: options.getCurrentToken ?? (() => token),
    onBundle: options.onBundle ?? (() => undefined),
    onBundleRejected: options.onBundleRejected,
    onPause: options.onPause ?? (() => undefined),
    now: options.now,
    setTimeout: options.setTimeout,
    clearTimeout: options.clearTimeout,
  });
}

test("one synchronous pass starts every source and commits one complete canonical bundle", async () => {
  const disposed: string[] = [];
  let releaseFirst!: (resource: ImageResource) => void;
  let releaseSecond!: (resource: ImageResource) => void;
  const first = new FakeSource(1, () => new Promise((resolve) => { releaseFirst = resolve; }));
  const second = new FakeSource(2, () => new Promise((resolve) => { releaseSecond = resolve; }));
  const committed: TickBundle[] = [];
  let now = 1_000;
  const runner = coordinator([first, second], { onBundle: (bundle) => committed.push(bundle), now: () => now });

  const tick = runner.captureOnce();
  assert.equal(first.starts, 1);
  assert.equal(second.starts, 1);
  releaseSecond(ownedResource("second", disposed));
  now = 1_020;
  releaseFirst(ownedResource("first", disposed));
  assert.equal(await tick, true);

  assert.equal(committed.length, 1);
  assert.equal(committed[0].timestampMs, 1_000);
  assert.equal(committed[0].relativeTimeSeconds, 0);
  assert.deepEqual(committed[0].frames.map((frame) => frame.source.stableAlias), ["camera_1", "camera_2"]);
  assert.deepEqual(disposed, []);
});

test("one failure discards every successful partial frame and never commits a partial bundle", async () => {
  const disposed: string[] = [];
  const good = new FakeSource(1, async () => ownedResource("good", disposed));
  const bad = new FakeSource(2, async () => { throw new Error("capture failed"); });
  const committed: TickBundle[] = [];
  const runner = coordinator([good, bad], { onBundle: (bundle) => committed.push(bundle) });

  assert.equal(await runner.captureOnce(), false);
  assert.deepEqual(committed, []);
  assert.deepEqual(disposed, ["good"]);
});

test("ticks never overlap", async () => {
  let release!: (resource: ImageResource) => void;
  const source = new FakeSource(1, () => new Promise((resolve) => { release = resolve; }));
  const runner = coordinator([source]);
  const first = runner.captureOnce();
  assert.equal(await runner.captureOnce(), false);
  assert.equal(source.starts, 1);
  release(ownedResource("done", []));
  await first;
});

test("late completion schedules one immediate next tick without catch-up bursts", async () => {
  const scheduled: number[] = [];
  let now = 0;
  const source = new FakeSource(1, async () => {
    now = 150;
    return ownedResource("frame", []);
  });
  const runner = coordinator([source], {
    now: () => now,
    setTimeout: ((_handler, delay) => {
      scheduled.push(delay);
      return 1 as unknown as ReturnType<typeof setTimeout>;
    }),
    clearTimeout: () => undefined,
  });
  await runner.start();
  assert.deepEqual(scheduled, [50, 0]);
  await runner.freeze();
});

test("three consecutive failures for one source pause the whole group", async () => {
  const pauses: string[] = [];
  const source = new FakeSource(1, async () => { throw new Error("camera failed"); });
  const runner = coordinator([source], {
    failureLimit: 3,
    onPause: (reason) => pauses.push(reason),
  });
  await runner.captureOnce();
  await runner.captureOnce();
  await runner.captureOnce();
  assert.equal(runner.paused, true);
  assert.deepEqual(pauses, ["consecutive_capture_failures:camera_1"]);
  assert.equal(await runner.captureOnce(), false);
});

test("track ended pauses all only while the callback token remains current", () => {
  const pauses: string[] = [];
  let current = true;
  const source = new FakeSource(1, async () => ownedResource("frame", []));
  const runner = coordinator([source], {
    getCurrentToken: () => current ? token : null,
    onPause: (reason) => pauses.push(reason),
  });
  source.emitEnded();
  assert.equal(runner.paused, true);
  assert.deepEqual(pauses, ["track_ended:camera_1"]);

  const staleSource = new FakeSource(2, async () => ownedResource("frame", []));
  const staleRunner = coordinator([staleSource], {
    getCurrentToken: () => current ? token : null,
    onPause: (reason) => pauses.push(reason),
  });
  current = false;
  staleSource.emitEnded();
  assert.equal(staleRunner.paused, false);
});

test("timeout aborts the barrier and disposes frames that settle late", async () => {
  const disposed: string[] = [];
  let release!: (resource: ImageResource) => void;
  let sawAbort = false;
  const source = new FakeSource(1, (signal) => new Promise((resolve) => {
    release = resolve;
    signal.addEventListener("abort", () => { sawAbort = true; }, { once: true });
  }));
  const runner = coordinator([source], { timeoutMs: 5 });
  assert.equal(await runner.captureOnce(), false);
  assert.equal(sawAbort, true);
  release(ownedResource("late", disposed));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(disposed, ["late"]);
});

test("freeze discards an in-flight complete bundle and abort disposes all sources", async () => {
  const disposed: string[] = [];
  let release!: (resource: ImageResource) => void;
  const source = new FakeSource(1, () => new Promise((resolve) => { release = resolve; }));
  const committed: TickBundle[] = [];
  const runner = coordinator([source], { onBundle: (bundle) => committed.push(bundle) });
  void runner.captureOnce();
  const freezing = runner.freeze();
  release(ownedResource("frozen", disposed));
  await freezing;
  assert.deepEqual(committed, []);
  assert.deepEqual(disposed, ["frozen"]);
  await runner.abort();
  assert.equal(source.disposeCount, 1);
});

test("stale callback tokens dispose complete bundles instead of committing", async () => {
  const disposed: string[] = [];
  const source = new FakeSource(1, async () => ownedResource("stale", disposed));
  const committed: TickBundle[] = [];
  const runner = coordinator([source], {
    getCurrentToken: () => null,
    onBundle: (bundle) => committed.push(bundle),
  });
  assert.equal(await runner.captureOnce(), false);
  assert.deepEqual(committed, []);
  assert.deepEqual(disposed, ["stale"]);
});

test("rejected bundle handoff reports the callback error and disposes the complete bundle once", async () => {
  const disposed: string[] = [];
  const source = new FakeSource(1, async () => ownedResource("rejected", disposed));
  const rejected: unknown[] = [];
  const runner = coordinator([source], {
    onBundle: () => { throw new Error("sampler invariant"); },
    onBundleRejected: (error) => rejected.push(error),
  });

  assert.equal(await runner.captureOnce(), false);
  assert.equal((rejected[0] as Error).message, "sampler invariant");
  assert.deepEqual(disposed, ["rejected"]);
});

test("callback token comparison validates every binding and cycle identity field", () => {
  assert.equal(sameCaptureCallbackToken(token, { ...token }), true);
  const variants: CaptureCallbackToken[] = [
    { ...token, pageInstanceId: "other-page" },
    { ...token, pageGeneration: 2 },
    { ...token, mode: "chat" },
    { ...token, contextId: "other-top-level-context" },
    { ...token, automationTaskId: "other-task" },
    { ...token, captureCycleId: "other-cycle" },
    { ...token, cycleGeneration: 2 },
  ];
  variants.forEach((variant) => assert.equal(sameCaptureCallbackToken(token, variant), false));
});

test("transitionCycle resets T=0 and tick index without adding source listeners", async () => {
  const committed: Array<{ bundle: TickBundle; token: CaptureCallbackToken }> = [];
  const source = new FakeSource(1, async (_signal, attempt) => ownedResource(`frame-${attempt}`, []));
  let now = 1_000;
  let currentToken: FullCaptureCallbackToken = token;
  const runner = new TickCoordinator({
    sources: [source],
    token,
    intervalMs: 100,
    timeoutMs: 50,
    failureLimit: 3,
    getCurrentToken: () => currentToken,
    onBundle: (bundle, callbackToken) => committed.push({ bundle, token: callbackToken }),
    onPause: () => undefined,
    now: () => now,
  });

  assert.equal(await runner.captureOnce(), true);
  await runner.freeze();
  const nextToken: FullCaptureCallbackToken = {
    ...token,
    captureCycleId: "cycle-2",
    cycleGeneration: 2,
  };
  currentToken = nextToken;
  now = 5_000;
  runner.transitionCycle(nextToken);
  assert.equal(await runner.captureOnce(), true);

  assert.deepEqual(committed.map(({ bundle }) => ({
    tickIndex: bundle.tickIndex,
    timestampMs: bundle.timestampMs,
    relativeTimeSeconds: bundle.relativeTimeSeconds,
  })), [
    { tickIndex: 0, timestampMs: 1_000, relativeTimeSeconds: 0 },
    { tickIndex: 0, timestampMs: 5_000, relativeTimeSeconds: 0 },
  ]);
  assert.deepEqual(committed.map((item) => item.token.captureCycleId), ["cycle-1", "cycle-2"]);
  assert.equal(source.listenerAdds, 1);
  await runner.abort();
  assert.equal(source.listenerRemoves, 1);
});

test("Abort promptly releases sources and returns one completion promise when capture never settles", async () => {
  const source = new FakeSource(1, () => new Promise(() => undefined));
  const runner = coordinator([source], { timeoutMs: 60_000 });
  void runner.captureOnce();

  const first = runner.abort();
  const second = runner.abort();
  assert.strictEqual(first, second);
  await Promise.race([
    first,
    new Promise<never>((_resolve, reject) => {
      setTimeout(() => reject(new Error("Abort completion remained pending")), 0);
    }),
  ]);
  assert.equal(source.disposeCount, 1);
  assert.equal(source.listenerRemoves, 1);
});

test("Abort immediately disposes completed frames while quarantining unresolved capture work", async () => {
  const disposed: string[] = [];
  let resolveFirst!: (resource: ImageResource) => void;
  let resolveSecond!: (resource: ImageResource) => void;
  const first = new FakeSource(1, () => new Promise((resolve) => { resolveFirst = resolve; }));
  const second = new FakeSource(2, () => new Promise((resolve) => { resolveSecond = resolve; }));
  const runner = coordinator([first, second], { timeoutMs: 60_000 });
  void runner.captureOnce();
  resolveFirst(ownedResource("completed", disposed));
  await Promise.resolve();
  await Promise.resolve();

  await runner.abort();
  assert.deepEqual(disposed, ["completed"]);
  resolveSecond(ownedResource("late", disposed));
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(disposed, ["completed", "late"]);
});

test("timeout disposes settled frames promptly and fails only rejected or unresolved sources", async () => {
  const disposed: string[] = [];
  let resolveUnsettled!: (resource: ImageResource) => void;
  let timeoutHandler: (() => void) | null = null;
  const successful = new FakeSource(1, async () => ownedResource("successful", disposed));
  const rejected = new FakeSource(2, async () => { throw new Error("rejected"); });
  const unresolved = new FakeSource(3, () => new Promise((resolve) => { resolveUnsettled = resolve; }));
  const runner = coordinator([successful, rejected, unresolved], {
    timeoutMs: 50,
    setTimeout: ((handler, delay) => {
      if (delay === 50) timeoutHandler = handler;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    }),
    clearTimeout: () => undefined,
  });

  const tick = runner.captureOnce();
  await Promise.resolve();
  await Promise.resolve();
  assert.ok(timeoutHandler);
  timeoutHandler();
  assert.equal(await tick, false);
  assert.deepEqual(disposed, ["successful"]);
  assert.equal(runner.failureCountFor("camera_1"), 0);
  assert.equal(runner.failureCountFor("camera_2"), 1);
  assert.equal(runner.failureCountFor("camera_3"), 1);

  resolveUnsettled(ownedResource("late", disposed));
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(disposed, ["successful", "late"]);
  await runner.abort();
});

test("freeze returns after timeout while detached cleanup quarantines late work across a cycle transition", async () => {
  const disposed: string[] = [];
  let resolveTimedOut!: (resource: ImageResource) => void;
  let timeoutHandler: (() => void) | null = null;
  const source = new FakeSource(1, (_signal, attempt) => {
    if (attempt === 1) return new Promise((resolve) => { resolveTimedOut = resolve; });
    return Promise.resolve(ownedResource("next-cycle", disposed));
  });
  let currentToken: FullCaptureCallbackToken = token;
  const committed: TickBundle[] = [];
  const committedTokens: CaptureCallbackToken[] = [];
  let resolveAutomaticCommit!: () => void;
  const automaticCommit = new Promise<void>((resolve) => { resolveAutomaticCommit = resolve; });
  const runner = new TickCoordinator({
    sources: [source],
    token,
    intervalMs: 100,
    timeoutMs: 50,
    failureLimit: 3,
    getCurrentToken: () => currentToken,
    onBundle: (bundle, callbackToken) => {
      committed.push(bundle);
      committedTokens.push(callbackToken);
      resolveAutomaticCommit();
    },
    onPause: () => undefined,
    setTimeout: ((handler, delay) => {
      if (delay === 50) timeoutHandler = handler;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    }),
    clearTimeout: () => undefined,
  });

  const timedOutTick = runner.captureOnce();
  assert.ok(timeoutHandler);
  timeoutHandler();
  assert.equal(await timedOutTick, false);
  const freezeOutcome = await Promise.race([
    runner.freeze().then(() => "frozen"),
    new Promise<string>((resolve) => setTimeout(() => resolve("blocked"), 0)),
  ]);
  assert.equal(freezeOutcome, "frozen");

  const backgroundCleanup = runner.waitForBackgroundCleanup();
  assert.strictEqual(runner.waitForBackgroundCleanup(), backgroundCleanup);
  const nextToken: FullCaptureCallbackToken = {
    ...token,
    captureCycleId: "cycle-after-timeout",
    cycleGeneration: 2,
  };
  currentToken = nextToken;
  runner.transitionCycle(nextToken);
  assert.equal(await runner.captureOnce(), false, "unsettled old work must block overlap");
  assert.equal(source.starts, 1);

  resolveTimedOut(ownedResource("late-timeout", disposed));
  await backgroundCleanup;
  await Promise.race([
    automaticCommit,
    new Promise<never>((_resolve, reject) => {
      setTimeout(() => reject(new Error("transitioned cycle did not capture immediately")), 0);
    }),
  ]);
  assert.deepEqual(disposed, ["late-timeout"]);
  assert.equal(source.starts, 2);
  assert.equal(committed.length, 1);
  assert.equal(committed[0].tickIndex, 0);
  assert.equal(committed[0].relativeTimeSeconds, 0);
  assert.equal(committedTokens[0].captureCycleId, "cycle-after-timeout");
  assert.equal(committedTokens[0].cycleGeneration, 2);
  await runner.abort();
});

test("slow post-cleanup immediate capture owns scheduling without interval or zero-delay polling", async () => {
  const disposed: string[] = [];
  let resolveOld!: (resource: ImageResource) => void;
  let resolveImmediate!: (resource: ImageResource) => void;
  let now = 0;
  const timers: Array<{
    handler: () => void;
    delay: number;
    cancelled: boolean;
  }> = [];
  const source = new FakeSource(1, (_signal, attempt) => {
    if (attempt === 1) return new Promise((resolve) => { resolveOld = resolve; });
    if (attempt === 2) return new Promise((resolve) => { resolveImmediate = resolve; });
    return Promise.resolve(ownedResource(`unexpected-${attempt}`, disposed));
  });
  let currentToken: FullCaptureCallbackToken = token;
  const runner = new TickCoordinator({
    sources: [source],
    token,
    intervalMs: 100,
    timeoutMs: 50,
    failureLimit: 3,
    getCurrentToken: () => currentToken,
    onBundle: () => undefined,
    onPause: () => undefined,
    now: () => now,
    setTimeout: ((handler, delay) => {
      const timer = { handler, delay, cancelled: false };
      timers.push(timer);
      return timer as unknown as ReturnType<typeof setTimeout>;
    }),
    clearTimeout: ((handle) => {
      (handle as unknown as { cancelled: boolean }).cancelled = true;
    }),
  });

  const oldTick = runner.captureOnce();
  const oldTimeout = timers.find((timer) => timer.delay === 50 && !timer.cancelled);
  assert.ok(oldTimeout);
  oldTimeout.cancelled = true;
  oldTimeout.handler();
  assert.equal(await oldTick, false);
  await runner.freeze();

  const nextToken: FullCaptureCallbackToken = {
    ...token,
    captureCycleId: "slow-immediate-cycle",
    cycleGeneration: 2,
  };
  currentToken = nextToken;
  runner.transitionCycle(nextToken);
  assert.equal(await runner.start(), false);
  resolveOld(ownedResource("old-late", disposed));
  await runner.waitForBackgroundCleanup();
  await Promise.resolve();
  assert.equal(source.starts, 2);

  now = 500;
  const expiredIntervals = timers.filter((timer) => timer.delay === 100 && !timer.cancelled);
  assert.equal(
    expiredIntervals.length,
    0,
    "transition plus start must not leave an old-barrier interval competing with immediate capture",
  );
  expiredIntervals.forEach((timer) => {
    timer.cancelled = true;
    timer.handler();
  });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(source.starts, 2);
  assert.equal(timers.filter((timer) => timer.delay === 0 && !timer.cancelled).length, 0);

  resolveImmediate(ownedResource("slow-immediate", disposed));
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
  assert.equal(timers.filter((timer) => timer.delay === 0 && !timer.cancelled).length, 1);
  await runner.abort();
});

test("resumeCycle before any successful bundle preserves failed-attempt T=0 and next tick", async () => {
  const committed: Array<{ bundle: TickBundle; token: CaptureCallbackToken }> = [];
  let now = 1_000;
  const source = new FakeSource(1, async (_signal, attempt) => {
    if (attempt === 1) throw new Error("first capture failed");
    return ownedResource("recovered-first-bundle", []);
  });
  const runner = new TickCoordinator({
    sources: [source],
    token,
    intervalMs: 100,
    timeoutMs: 50,
    failureLimit: 1,
    getCurrentToken: () => token,
    onBundle: (bundle, callbackToken) => committed.push({ bundle, token: callbackToken }),
    onPause: () => undefined,
    now: () => now,
  });

  assert.equal(await runner.captureOnce(), false);
  assert.equal(runner.paused, true);
  assert.equal(runner.failureCountFor("camera_1"), 1);
  now = 1_500;
  assert.equal(await runner.resumeCycle(token), true);

  assert.equal(committed.length, 1);
  assert.equal(committed[0].bundle.tickIndex, 1);
  assert.equal(committed[0].bundle.relativeTimeSeconds, 0.5);
  assert.equal(committed[0].token.captureCycleId, token.captureCycleId);
  assert.equal(committed[0].token.cycleGeneration, token.cycleGeneration);
  assert.equal(runner.failureCountFor("camera_1"), 0);
  assert.equal(source.listenerAdds, 1);
  await runner.abort();
});

test("resumeCycle after retained bundles preserves cycle timeline, next tick, and listeners", async () => {
  const committed: TickBundle[] = [];
  let now = 2_000;
  const source = new FakeSource(1, async (_signal, attempt) => ownedResource(`retained-${attempt}`, []));
  const runner = new TickCoordinator({
    sources: [source],
    token,
    intervalMs: 100,
    timeoutMs: 50,
    failureLimit: 3,
    getCurrentToken: () => token,
    onBundle: (bundle) => committed.push(bundle),
    onPause: () => undefined,
    now: () => now,
  });

  assert.equal(await runner.captureOnce(), true);
  source.emitEnded();
  assert.equal(runner.paused, true);
  now = 2_600;
  assert.equal(await runner.resumeCycle(token), true);

  assert.deepEqual(committed.map((bundle) => ({
    tickIndex: bundle.tickIndex,
    relativeTimeSeconds: bundle.relativeTimeSeconds,
  })), [
    { tickIndex: 0, relativeTimeSeconds: 0 },
    { tickIndex: 1, relativeTimeSeconds: 0.6 },
  ]);
  assert.equal(source.listenerAdds, 1);
  await assert.rejects(runner.resumeCycle({ ...token, cycleGeneration: 2 }), /current cycle|token/i);
  await runner.abort();
  assert.equal(source.listenerRemoves, 1);
});
