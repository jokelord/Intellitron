import type {
  CaptureCallbackToken,
  CaptureSource,
  FullCaptureCallbackToken,
} from "./capture-source.ts";
import { disposeBundle } from "./image-resources.ts";
import type { AutomationFrame, TickBundle } from "./types.ts";

export interface TickCoordinatorOptions {
  sources: readonly CaptureSource[];
  token: CaptureCallbackToken;
  intervalMs: number;
  timeoutMs: number;
  failureLimit: number;
  getCurrentToken?: () => FullCaptureCallbackToken | null;
  /** Existing Task 7 compatibility; new integrations must use getCurrentToken. */
  isTokenCurrent?: (token: CaptureCallbackToken) => boolean;
  onBundle: (bundle: TickBundle, token: CaptureCallbackToken) => void;
  onBundleRejected?: (error: unknown, token: CaptureCallbackToken) => void;
  onPause: (reason: string, token: CaptureCallbackToken) => void;
  now?: () => number;
  setTimeout?: (handler: () => void, delay: number) => ReturnType<typeof setTimeout>;
  clearTimeout?: (handle: ReturnType<typeof setTimeout>) => void;
}

type SourceSettlement =
  | { status: "pending" }
  | { status: "fulfilled"; frame: AutomationFrame }
  | { status: "rejected"; error: unknown };

interface TickSettlementBatch {
  controller: AbortController;
  settlements: SourceSettlement[];
  quarantined: boolean;
  ownershipTransferred: boolean;
  timeoutHandle: ReturnType<typeof setTimeout> | null;
  interrupt(outcome: typeof TIMEOUT | typeof CANCELLED): void;
}

const TIMEOUT = Symbol("tick_timeout");
const CANCELLED = Symbol("tick_cancelled");
const COMPLETED_BACKGROUND_CLEANUP = Promise.resolve();

export function sameCaptureCallbackToken(
  left: CaptureCallbackToken | null,
  right: CaptureCallbackToken | null,
): boolean {
  return left !== null
    && right !== null
    && typeof left.pageInstanceId === "string"
    && typeof right.pageInstanceId === "string"
    && typeof left.mode === "string"
    && typeof right.mode === "string"
    && typeof left.contextId === "string"
    && typeof right.contextId === "string"
    && left.pageInstanceId === right.pageInstanceId
    && left.pageGeneration === right.pageGeneration
    && left.mode === right.mode
    && left.contextId === right.contextId
    && left.automationTaskId === right.automationTaskId
    && left.captureCycleId === right.captureCycleId
    && left.cycleGeneration === right.cycleGeneration;
}

function isFullToken(token: CaptureCallbackToken): token is FullCaptureCallbackToken {
  return typeof token.pageInstanceId === "string"
    && typeof token.mode === "string"
    && typeof token.contextId === "string";
}

function validateToken(token: FullCaptureCallbackToken): void {
  const ids = [
    token.pageInstanceId,
    token.contextId,
    token.automationTaskId,
    token.captureCycleId,
  ];
  if (ids.some((value) => !value.trim())) throw new Error("Capture callback token IDs are required");
  if (!Number.isInteger(token.pageGeneration) || token.pageGeneration < 0) {
    throw new RangeError("pageGeneration must be a non-negative integer");
  }
  if (!Number.isInteger(token.cycleGeneration) || token.cycleGeneration < 1) {
    throw new RangeError("cycleGeneration must be a positive integer");
  }
}

function cloneToken<T extends CaptureCallbackToken>(token: T): T {
  return { ...token };
}

export class TickCoordinator {
  readonly sources: readonly CaptureSource[];

  paused = false;

  private currentCycleToken: CaptureCallbackToken;
  private readonly intervalMs: number;
  private readonly timeoutMs: number;
  private readonly failureLimit: number;
  private readonly getCurrentToken?: TickCoordinatorOptions["getCurrentToken"];
  private readonly legacyIsTokenCurrent?: TickCoordinatorOptions["isTokenCurrent"];
  private readonly onBundle: TickCoordinatorOptions["onBundle"];
  private readonly onBundleRejected?: TickCoordinatorOptions["onBundleRejected"];
  private readonly onPause: TickCoordinatorOptions["onPause"];
  private readonly now: () => number;
  private readonly scheduleTimeout: NonNullable<TickCoordinatorOptions["setTimeout"]>;
  private readonly cancelTimeout: NonNullable<TickCoordinatorOptions["clearTimeout"]>;
  private readonly failureCounts = new Map<string, number>();
  private readonly removeEndedListeners: Array<() => void>;
  private inFlight: Promise<boolean> | null = null;
  private settlingBarrier: Promise<void> | null = null;
  private activeBatch: TickSettlementBatch | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private started = false;
  private frozen = false;
  private aborted = false;
  private abortCompletion: Promise<void> | null = null;
  private pendingImmediateCycleToken: FullCaptureCallbackToken | null = null;
  private pendingImmediateCleanupBarrier: Promise<void> | null = null;
  private tickIndex = 0;
  private cycleStartMs: number | null = null;
  private lastStartMs: number | null = null;

  constructor(options: TickCoordinatorOptions) {
    if (options.sources.length === 0) throw new Error("TickCoordinator requires at least one source");
    if (!Number.isFinite(options.intervalMs) || options.intervalMs < 100) {
      throw new RangeError("intervalMs must be at least 100");
    }
    if (!Number.isFinite(options.timeoutMs) || options.timeoutMs < 1) {
      throw new RangeError("timeoutMs must be positive");
    }
    if (!Number.isInteger(options.failureLimit) || options.failureLimit < 1) {
      throw new RangeError("failureLimit must be a positive integer");
    }
    if (options.getCurrentToken !== undefined) {
      if (!isFullToken(options.token)) throw new Error("getCurrentToken requires a full capture callback token");
      validateToken(options.token);
    } else if (options.isTokenCurrent === undefined) {
      throw new Error("TickCoordinator requires getCurrentToken");
    }
    this.sources = [...options.sources];
    this.currentCycleToken = cloneToken(options.token);
    this.intervalMs = options.intervalMs;
    this.timeoutMs = options.timeoutMs;
    this.failureLimit = options.failureLimit;
    this.getCurrentToken = options.getCurrentToken;
    this.legacyIsTokenCurrent = options.isTokenCurrent;
    this.onBundle = options.onBundle;
    this.onBundleRejected = options.onBundleRejected;
    this.onPause = options.onPause;
    this.now = options.now ?? Date.now;
    this.scheduleTimeout = options.setTimeout ?? ((handler, delay) => setTimeout(handler, delay));
    this.cancelTimeout = options.clearTimeout ?? ((handle) => clearTimeout(handle));
    this.removeEndedListeners = this.sources.map((source) => source.onEnded(() => {
      const callbackToken = cloneToken(this.currentCycleToken);
      if (!this.isCurrent(callbackToken) || this.aborted) return;
      this.pause(`track_ended:${source.descriptor.stableAlias}`, callbackToken);
    }));
  }

  get token(): CaptureCallbackToken {
    return cloneToken(this.currentCycleToken);
  }

  failureCountFor(alias: string): number {
    return this.failureCounts.get(alias) ?? 0;
  }

  transitionCycle(token: FullCaptureCallbackToken): void {
    validateToken(token);
    if (this.aborted) throw new Error("Cannot transition an aborted TickCoordinator");
    if (this.inFlight !== null) {
      throw new Error("Cannot transition capture cycle while a foreground tick is running");
    }
    this.clearScheduledTick();
    this.currentCycleToken = cloneToken(token);
    this.failureCounts.clear();
    this.tickIndex = 0;
    this.cycleStartMs = null;
    this.lastStartMs = null;
    this.started = false;
    this.frozen = false;
    this.paused = false;
    if (this.settlingBarrier !== null) {
      this.queueImmediateCycleAfterCleanup(token, this.settlingBarrier);
    } else {
      this.pendingImmediateCycleToken = null;
    }
  }

  async start(): Promise<boolean> {
    if (this.aborted || this.started) return false;
    this.started = true;
    this.frozen = false;
    const result = await this.captureOnce();
    this.scheduleAfterBarrier();
    return result;
  }

  async resumeCycle(token: FullCaptureCallbackToken): Promise<boolean> {
    validateToken(token);
    if (this.aborted) throw new Error("Cannot resume an aborted TickCoordinator");
    if (!sameCaptureCallbackToken(token, this.currentCycleToken) || !this.isCurrent(token)) {
      throw new Error("Cannot resume a token outside the current capture cycle");
    }
    if (this.inFlight !== null) {
      throw new Error("Cannot resume while a foreground tick is running");
    }
    this.clearScheduledTick();
    this.failureCounts.clear();
    this.paused = false;
    this.frozen = false;
    this.started = true;
    if (this.settlingBarrier !== null) {
      this.queueImmediateCycleAfterCleanup(token, this.settlingBarrier);
      return false;
    }
    this.pendingImmediateCycleToken = null;
    const result = await this.captureOnce();
    this.scheduleAfterBarrier();
    return result;
  }

  captureOnce(): Promise<boolean> {
    if (this.aborted || this.frozen || this.paused || this.inFlight !== null || this.settlingBarrier !== null) {
      return Promise.resolve(false);
    }
    const run = this.runTick();
    this.inFlight = run;
    void run.finally(() => {
      if (this.inFlight === run) this.inFlight = null;
    });
    return run;
  }

  async freeze(): Promise<void> {
    this.frozen = true;
    this.started = false;
    this.pendingImmediateCycleToken = null;
    this.clearScheduledTick();
    await this.inFlight;
  }

  waitForBackgroundCleanup(): Promise<void> {
    return this.settlingBarrier ?? COMPLETED_BACKGROUND_CLEANUP;
  }

  abort(): Promise<void> {
    if (this.abortCompletion !== null) return this.abortCompletion;
    this.aborted = true;
    this.frozen = true;
    this.started = false;
    this.pendingImmediateCycleToken = null;
    this.clearScheduledTick();
    const batch = this.activeBatch;
    if (batch !== null) {
      batch.interrupt(CANCELLED);
      batch.controller.abort(new DOMException("Capture aborted", "AbortError"));
      this.quarantineBatch(batch);
      if (batch.timeoutHandle !== null) {
        this.cancelTimeout(batch.timeoutHandle);
        batch.timeoutHandle = null;
      }
    }
    this.removeEndedListeners.forEach((remove) => remove());
    this.sources.forEach((source) => source.dispose());
    this.abortCompletion = Promise.resolve();
    return this.abortCompletion;
  }

  private async runTick(): Promise<boolean> {
    const callbackToken = cloneToken(this.currentCycleToken);
    const actualStart = this.now();
    if (this.cycleStartMs === null) this.cycleStartMs = actualStart;
    this.lastStartMs = actualStart;
    const tickIndex = this.tickIndex;
    this.tickIndex += 1;
    const controller = new AbortController();
    let resolveInterrupt!: (outcome: typeof TIMEOUT | typeof CANCELLED) => void;
    let interrupted = false;
    const interruptOutcome = new Promise<typeof TIMEOUT | typeof CANCELLED>((resolve) => {
      resolveInterrupt = resolve;
    });
    const batch: TickSettlementBatch = {
      controller,
      settlements: this.sources.map(() => ({ status: "pending" })),
      quarantined: false,
      ownershipTransferred: false,
      timeoutHandle: null,
      interrupt(outcome) {
        if (interrupted) return;
        interrupted = true;
        resolveInterrupt(outcome);
      },
    };
    this.activeBatch = batch;

    // Invoke every source before awaiting any result: this is the software sync barrier.
    const captures = this.sources.map((source, index) => {
      let capture: Promise<AutomationFrame>;
      try {
        capture = Promise.resolve(source.capture(controller.signal));
      } catch (error) {
        capture = Promise.reject(error);
      }
      return capture.then(
        (frame) => {
          batch.settlements[index] = { status: "fulfilled", frame };
          if (batch.quarantined && !batch.ownershipTransferred) frame.resource.dispose();
          return frame;
        },
        (error) => {
          batch.settlements[index] = { status: "rejected", error };
          throw error;
        },
      );
    });
    const settled = Promise.allSettled(captures);
    batch.timeoutHandle = this.scheduleTimeout(() => {
      batch.interrupt(TIMEOUT);
      controller.abort(new DOMException("Tick timed out", "TimeoutError"));
    }, this.timeoutMs);

    let detached = false;
    try {
      const outcome = await Promise.race([settled, interruptOutcome]);
      if (batch.timeoutHandle !== null) {
        this.cancelTimeout(batch.timeoutHandle);
        batch.timeoutHandle = null;
      }
      if (outcome === TIMEOUT || outcome === CANCELLED) {
        this.quarantineBatch(batch);
        if (outcome === TIMEOUT && this.isCurrent(callbackToken) && !this.aborted) {
          batch.settlements.forEach((settlement, index) => {
            const alias = this.sources[index].descriptor.stableAlias;
            if (settlement.status === "fulfilled") this.failureCounts.set(alias, 0);
            else this.recordFailure(alias, callbackToken);
          });
        }
        this.detachUntilSettled(batch, settled);
        detached = true;
        return false;
      }

      const frames: AutomationFrame[] = [];
      let complete = true;
      outcome.forEach((result, index) => {
        const alias = this.sources[index].descriptor.stableAlias;
        if (result.status === "fulfilled") {
          this.failureCounts.set(alias, 0);
          frames.push(result.value);
        } else {
          complete = false;
          if (this.isCurrent(callbackToken) && !this.aborted) this.recordFailure(alias, callbackToken);
        }
      });
      const bundle: TickBundle = {
        tickIndex,
        timestampMs: actualStart,
        relativeTimeSeconds: (actualStart - this.cycleStartMs) / 1_000,
        frames,
      };
      if (!complete || frames.length !== this.sources.length) {
        disposeBundle(bundle);
        return false;
      }
      if (this.frozen || this.aborted || !this.isCurrent(callbackToken)) {
        disposeBundle(bundle);
        return false;
      }
      try {
        this.onBundle(bundle, cloneToken(callbackToken));
        batch.ownershipTransferred = true;
        return true;
      } catch (error) {
        try {
          this.onBundleRejected?.(error, cloneToken(callbackToken));
        } catch {
          // A diagnostic listener must not alter bundle ownership cleanup.
        }
        disposeBundle(bundle);
        return false;
      }
    } finally {
      if (batch.timeoutHandle !== null) this.cancelTimeout(batch.timeoutHandle);
      if (!detached && this.activeBatch === batch) this.activeBatch = null;
    }
  }

  private detachUntilSettled(
    batch: TickSettlementBatch,
    settled: Promise<PromiseSettledResult<AutomationFrame>[]>,
  ): void {
    const barrier = settled.then(() => undefined).finally(() => {
      if (this.settlingBarrier === barrier) this.settlingBarrier = null;
      if (this.activeBatch === batch) this.activeBatch = null;
    });
    this.settlingBarrier = barrier;
    void barrier.then(() => {
      if (this.pendingImmediateCycleToken === null) this.scheduleNext();
    });
  }

  private queueImmediateCycleAfterCleanup(
    token: FullCaptureCallbackToken,
    barrier: Promise<void>,
  ): void {
    this.pendingImmediateCycleToken = cloneToken(token);
    if (this.pendingImmediateCleanupBarrier === barrier) return;
    this.pendingImmediateCleanupBarrier = barrier;
    void barrier.then(() => {
      if (this.pendingImmediateCleanupBarrier !== barrier) return;
      this.pendingImmediateCleanupBarrier = null;
      const pendingToken = this.pendingImmediateCycleToken;
      if (pendingToken === null) return;
      this.pendingImmediateCycleToken = null;
      if (
        this.aborted
        || this.frozen
        || this.paused
        || !sameCaptureCallbackToken(pendingToken, this.currentCycleToken)
        || !this.isCurrent(pendingToken)
      ) return;
      this.started = true;
      void this.captureOnce().then(() => this.scheduleAfterBarrier());
    });
  }

  private quarantineBatch(batch: TickSettlementBatch): void {
    if (batch.ownershipTransferred) return;
    batch.quarantined = true;
    batch.settlements.forEach((settlement) => {
      if (settlement.status === "fulfilled") settlement.frame.resource.dispose();
    });
  }

  private recordFailure(alias: string, callbackToken: CaptureCallbackToken): void {
    const count = (this.failureCounts.get(alias) ?? 0) + 1;
    this.failureCounts.set(alias, count);
    if (count >= this.failureLimit) this.pause(`consecutive_capture_failures:${alias}`, callbackToken);
  }

  private pause(reason: string, callbackToken: CaptureCallbackToken): void {
    if (this.paused || !this.isCurrent(callbackToken) || this.aborted) return;
    this.paused = true;
    this.started = false;
    this.clearScheduledTick();
    const batch = this.activeBatch;
    if (batch !== null) {
      batch.interrupt(CANCELLED);
      batch.controller.abort(new DOMException(reason, "AbortError"));
      this.quarantineBatch(batch);
    }
    this.onPause(reason, cloneToken(callbackToken));
  }

  private isCurrent(token: CaptureCallbackToken): boolean {
    if (this.getCurrentToken !== undefined) {
      return sameCaptureCallbackToken(token, this.getCurrentToken());
    }
    return this.legacyIsTokenCurrent?.(token) ?? false;
  }

  private scheduleAfterBarrier(): void {
    if (this.settlingBarrier !== null) {
      void this.settlingBarrier.then(() => {
        if (this.pendingImmediateCycleToken === null) this.scheduleNext();
      });
      return;
    }
    this.scheduleNext();
  }

  private scheduleNext(): void {
    if (
      !this.started
      || this.frozen
      || this.paused
      || this.aborted
      || this.timer !== null
      || this.inFlight !== null
    ) return;
    const target = (this.lastStartMs ?? this.now()) + this.intervalMs;
    const delay = Math.max(0, target - this.now());
    this.timer = this.scheduleTimeout(() => {
      this.timer = null;
      if (this.inFlight !== null) return;
      void this.captureOnce().then(() => this.scheduleAfterBarrier());
    }, delay);
  }

  private clearScheduledTick(): void {
    if (this.timer === null) return;
    this.cancelTimeout(this.timer);
    this.timer = null;
  }
}
