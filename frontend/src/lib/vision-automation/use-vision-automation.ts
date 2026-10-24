import { useEffect, useRef, useState } from "react";

import type { KnownRequestRejection } from "../api.ts";
import type { Message } from "../message-types.ts";
import { TickBundleSampler, type FrozenBundleBatch } from "./bundle-sampler.ts";
import type {
  CaptureCallbackToken,
  CaptureSource,
  FullCaptureCallbackToken,
} from "./capture-source.ts";
import type { VisionAutomationServerConfig } from "./config.ts";
import {
  stageChatHistoryImageOmission,
  type HistoricalImageBlockResolver,
  type StagedChatHistoryOmission,
} from "./history-omission.ts";
import {
  AUTOMATION_JPEG_MIME,
  estimateDataUrlBytes,
  prepareVisionPayload,
  type AutomationFrameEncoder,
  type BlobToDataUrlEncoder,
  type PreparedVisionPayload,
} from "./payload.ts";
import {
  buildAgentVisionAutomationAbortRequest,
  buildVisionAutomationRequestContract,
  isValidRequestId,
  RequestAcceptanceIdentityError,
  type RequestAcceptedEvent,
  type RequestStatusResult,
  type AgentVisionAutomationAbortRequest,
  type VisionAutomationTransportMetadata,
} from "./send-contract.ts";
import {
  createInitialVisionAutomationState,
  reduceVisionAutomation,
  type CleanupPendingToken,
  type SubmissionAttemptIdentity,
  type VisionAutomationAction,
  type VisionAutomationState,
} from "./state-machine.ts";
import { sameCaptureCallbackToken, TickCoordinator } from "./tick-coordinator.ts";
import type { AutomationBinding, SourceDescriptor, TickBundle } from "./types.ts";
import { freezeVisionAutomationConfig } from "./page-integration.ts";

export type VisionAutomationIdKind =
  | "source"
  | "cycle"
  | "submission"
  | "request"
  | "cleanup";

export interface VisionAutomationSubmitLifecycle {
  onRequestAccepted(event: RequestAcceptedEvent): Promise<void>;
  /** True only when this lifecycle instance owns and handled the rejection. */
  onKnownRejection(rejection: KnownRequestRejection): Promise<boolean>;
  onTransportEof(): Promise<void>;
  onStableTerminal(result: RequestStatusResult): Promise<void>;
}

export interface VisionAutomationSubmitInput {
  binding: AutomationBinding;
  submission: SubmissionAttemptIdentity;
  requestId: string;
  userText: string;
  payload: PreparedVisionPayload;
  stagedMessages: readonly Message[];
  visionAutomation: VisionAutomationTransportMetadata;
  controller: AbortController;
  signal: AbortSignal;
  lifecycle: VisionAutomationSubmitLifecycle;
  getState(): VisionAutomationState;
  isAccepted(): boolean;
}

export interface VisionAutomationControllerOptions {
  config: VisionAutomationServerConfig;
  acquireSources(input: {
    binding: AutomationBinding;
    ownerId: string;
    signal: AbortSignal;
    config: Readonly<VisionAutomationServerConfig>;
  }): Promise<readonly CaptureSource[]>;
  restoreSource(input: {
    binding: AutomationBinding;
    ownerId: string;
    source: SourceDescriptor;
    requiresDirectDisplayChooser: boolean;
    signal: AbortSignal;
    config: Readonly<VisionAutomationServerConfig>;
  }): Promise<CaptureSource>;
  getManualDataUrls(): readonly string[];
  getMessages(): readonly Message[];
  commitMessages(messages: Message[]): void;
  isModelBusy(): boolean;
  encodeFrame: AutomationFrameEncoder;
  blobToDataUrl?: BlobToDataUrlEncoder;
  submit(input: VisionAutomationSubmitInput): Promise<void>;
  getRequestStatus(requestId: string, signal?: AbortSignal): Promise<RequestStatusResult>;
  waitForRequestTerminal(
    requestId: string,
    options?: { signal?: AbortSignal; pollIntervalMs?: number },
  ): Promise<RequestStatusResult>;
  registerAgentAbort?(request: AgentVisionAutomationAbortRequest, signal?: AbortSignal): Promise<void>;
  cancelAgentAbort?(request: AgentVisionAutomationAbortRequest, signal?: AbortSignal): Promise<void>;
  onRequestAccepted?(requestId: string): void | Promise<void>;
  onStableTerminal?(requestId: string): void | Promise<void>;
  resolveHistoricalImageBlock?: HistoricalImageBlockResolver;
  createId?: (kind: VisionAutomationIdKind) => string;
}

export interface FailedSourceIdentity {
  stableAlias: SourceDescriptor["stableAlias"];
  sourceId: string;
}

interface FailedSourceState {
  descriptor: SourceDescriptor;
  sourceAttemptId: string;
}

interface PendingRecoveryCycleState {
  token: FullCaptureCallbackToken;
  failedDescriptor: SourceDescriptor;
  lifecycle: "request_accepted" | "stable_terminal";
}

export interface StagedVisionAutomationCleanupSend {
  readonly token: CleanupPendingToken;
  readonly requestId: string;
  readonly stagedMessages: readonly Message[];
  readonly visionAutomation: VisionAutomationTransportMetadata;
  beginAttempt(): boolean;
  finishAttempt(): void;
  commitAccepted(event: RequestAcceptedEvent): boolean;
  discard(): void;
}

interface AcceptedImageBlockState {
  binding: AutomationBinding;
  captureCycleId: string;
}

interface CleanupTransportState extends AcceptedImageBlockState {
  cleanupAttemptId: string;
  cleanupToken: string;
  registration: "not_required" | "pending" | "registered";
  registrationError: string | null;
}

export interface VisionAutomationCumulativeStats {
  attemptedTicks: number;
  successfulTicks: number;
  failedTicks: number;
  imageCount: number;
  encodedBytes: number;
}

interface CaptureAttemptStats {
  id: number;
  remaining: number;
  failed: boolean;
}

interface SubmissionTransaction {
  binding: AutomationBinding;
  submission: SubmissionAttemptIdentity;
  requestId: string;
  userText: string;
  manualDataUrls: readonly string[];
  batch: FrozenBundleBatch | null;
  payload: PreparedVisionPayload | null;
  omission: StagedChatHistoryOmission | null;
  visionAutomation: VisionAutomationTransportMetadata | null;
  attemptController: AbortController | null;
  accepted: boolean;
  stableTerminal: boolean;
  transportResolved: boolean;
  inheritedCleanup: CleanupPendingToken | null;
  answerNowReplacement: {
    originalRequestId: string;
    replacementRequestId: string;
    replacementAccepted: boolean;
    bufferedOriginalTerminal: RequestStatusResult | null;
  } | null;
}

export function shouldStageVisionAutomationHistoryOmission(input: {
  sourceCount: number;
  retainedTickCount: number;
  sticky: boolean;
}): boolean {
  return input.sticky || (
    input.sourceCount > 0 && input.retainedTickCount > 0
  );
}

function stagePassThroughHistory(messages: readonly Message[]): StagedChatHistoryOmission {
  let resolved = false;
  return {
    stagedMessages: messages,
    omittedBlockCount: 0,
    commit(currentMessages) {
      if (resolved) throw new Error("Staged history pass-through was already resolved");
      resolved = true;
      return [...(currentMessages ?? messages)];
    },
    discard() {
      resolved = true;
    },
  };
}

function sameBinding(left: AutomationBinding | null, right: AutomationBinding): boolean {
  return left !== null
    && left.pageInstanceId === right.pageInstanceId
    && left.pageGeneration === right.pageGeneration
    && left.mode === right.mode
    && left.contextId === right.contextId
    && left.automationTaskId === right.automationTaskId;
}

function sameContext(left: AutomationBinding, right: AutomationBinding): boolean {
  return left.pageInstanceId === right.pageInstanceId
    && left.pageGeneration === right.pageGeneration
    && left.mode === right.mode
    && left.contextId === right.contextId;
}

function sameCleanupToken(
  left: CleanupPendingToken | null,
  right: CleanupPendingToken,
): boolean {
  return left !== null
    && left.cleanupAttemptId === right.cleanupAttemptId
    && sameContext(left.binding, right.binding);
}

function defaultId(kind: VisionAutomationIdKind): string {
  const random = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${kind}-${random}`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function sameLogicalSource(left: SourceDescriptor, right: SourceDescriptor): boolean {
  return left.stableAlias === right.stableAlias
    && left.sourceId === right.sourceId
    && left.kind === right.kind
    && left.deviceId === right.deviceId;
}

class RecoverableCaptureSource implements CaptureSource {
  readonly descriptor: SourceDescriptor;

  private delegate: CaptureSource;
  private readonly endedListeners = new Set<() => void>();
  private removeDelegateEnded: (() => void) | null = null;
  private disposed = false;
  private readonly reportEnded: (descriptor: SourceDescriptor) => void;
  private readonly beginCapture: () => number;
  private readonly settleCapture: (attemptId: number, succeeded: boolean) => void;

  constructor(
    source: CaptureSource,
    reportEnded: (descriptor: SourceDescriptor) => void,
    beginCapture: () => number,
    settleCapture: (attemptId: number, succeeded: boolean) => void,
  ) {
    this.delegate = source;
    this.descriptor = source.descriptor;
    this.reportEnded = reportEnded;
    this.beginCapture = beginCapture;
    this.settleCapture = settleCapture;
    this.bindDelegateEnded();
  }

  capture(signal: AbortSignal) {
    const attemptId = this.beginCapture();
    let capture: ReturnType<CaptureSource["capture"]>;
    try {
      capture = this.delegate.capture(signal);
    } catch (error) {
      this.settleCapture(attemptId, false);
      throw error;
    }
    return capture.then(
      (frame) => {
        this.settleCapture(attemptId, true);
        return frame;
      },
      (error) => {
        this.settleCapture(attemptId, false);
        throw error;
      },
    );
  }

  onEnded(listener: () => void): () => void {
    this.endedListeners.add(listener);
    return () => this.endedListeners.delete(listener);
  }

  replace(source: CaptureSource): void {
    if (this.disposed) throw new Error("Cannot restore a disposed capture source");
    if (!sameLogicalSource(this.descriptor, source.descriptor)) {
      throw new Error("Restored source identity does not match the failed source");
    }
    const previous = this.delegate;
    this.removeDelegateEnded?.();
    this.delegate = source;
    this.bindDelegateEnded();
    previous.dispose();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.removeDelegateEnded?.();
    this.removeDelegateEnded = null;
    this.endedListeners.clear();
    this.delegate.dispose();
  }

  private bindDelegateEnded(): void {
    this.removeDelegateEnded = this.delegate.onEnded(() => {
      this.reportEnded(this.descriptor);
      this.endedListeners.forEach((listener) => listener());
    });
  }
}

export class VisionAutomationController {
  private options: VisionAutomationControllerOptions;
  private state = createInitialVisionAutomationState();
  private readonly listeners = new Set<(state: VisionAutomationState) => void>();
  private acquisitionController: AbortController | null = null;
  private coordinator: TickCoordinator | null = null;
  private sampler: TickBundleSampler | null = null;
  private sources: readonly RecoverableCaptureSource[] = [];
  private transaction: SubmissionTransaction | null = null;
  private disposed = false;
  private failedSourceState: FailedSourceState | null = null;
  private pendingRecoveryCycleState: PendingRecoveryCycleState | null = null;
  private acceptedImageBlockState: AcceptedImageBlockState | null = null;
  private historicalImageOmissionStickyBinding: AutomationBinding | null = null;
  private cleanupTransportState: CleanupTransportState | null = null;
  private cleanupRegistrationController: AbortController | null = null;
  private cleanupRegistrationPromise: Promise<void> | null = null;
  private cleanupCancellationController: AbortController | null = null;
  private cleanupCancellationPromise: Promise<boolean> | null = null;
  private cleanupStagePromise: Promise<StagedVisionAutomationCleanupSend | null> | null = null;
  private cleanupStage: StagedVisionAutomationCleanupSend | null = null;
  private cleanupStageAttemptActive = false;
  private activeConfig: Readonly<VisionAutomationServerConfig> | null = null;
  private recoveryError: string | null = null;
  private retrySubmitConfirmedUnknown = false;
  private stats: VisionAutomationCumulativeStats = {
    attemptedTicks: 0,
    successfulTicks: 0,
    failedTicks: 0,
    imageCount: 0,
    encodedBytes: 0,
  };
  private captureAttemptSequence = 0;
  private activeCaptureAttempt: CaptureAttemptStats | null = null;

  constructor(options: VisionAutomationControllerOptions) {
    this.options = options;
  }

  updateOptions(options: VisionAutomationControllerOptions): void {
    this.options = options;
  }

  getState(): VisionAutomationState {
    return this.state;
  }

  getBundles(): readonly TickBundle[] {
    return this.sampler?.bundles ?? [];
  }

  getActiveConfig(): Readonly<VisionAutomationServerConfig> | null {
    return this.activeConfig;
  }

  getStats(): VisionAutomationCumulativeStats {
    this.refreshCurrentGauges();
    return { ...this.stats };
  }

  getRecoveryError(): string | null {
    return this.recoveryError ?? this.cleanupTransportState?.registrationError ?? null;
  }

  canRetryStatus(): boolean {
    return this.transaction !== null
      && this.transaction.accepted
      && !this.transaction.stableTerminal
      && this.recoveryError !== null;
  }

  canRetrySubmit(): boolean {
    return this.transaction !== null
      && !this.transaction.accepted
      && this.state.phase === "retryable_submit_error"
      && this.retrySubmitConfirmedUnknown;
  }

  hasPendingCleanupRegistration(): boolean {
    return this.cleanupTransportState?.registration === "pending";
  }

  hasCleanupSendInFlight(): boolean {
    return this.cleanupStagePromise !== null || this.cleanupStageAttemptActive;
  }

  getRecoverableRequestId(): string | null {
    return this.transaction?.requestId ?? null;
  }

  getFailedSourceIdentity(): FailedSourceIdentity | null {
    const descriptor = this.failedSourceState?.descriptor;
    return descriptor === undefined
      ? null
      : { stableAlias: descriptor.stableAlias, sourceId: descriptor.sourceId };
  }

  subscribe(listener: (state: VisionAutomationState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async start(binding: AutomationBinding): Promise<boolean> {
    if (this.disposed) throw new Error("Vision automation controller is disposed");
    if (!this.options.config.enabled) throw new Error("Vision automation is disabled");
    if (this.state.phase !== "idle") {
      throw new Error("Vision automation cannot start from the current state");
    }
    if (this.hasCleanupSendInFlight()) {
      throw new Error("Vision automation cannot re-arm while a cleanup send is in flight");
    }
    if (this.historicalImageOmissionStickyBinding !== null
      && !sameContext(this.historicalImageOmissionStickyBinding, binding)) {
      this.historicalImageOmissionStickyBinding = null;
    }
    this.activeConfig = freezeVisionAutomationConfig(this.options.config);
    this.stats = {
      attemptedTicks: 0,
      successfulTicks: 0,
      failedTicks: 0,
      imageCount: 0,
      encodedBytes: 0,
    };
    this.captureAttemptSequence = 0;
    this.activeCaptureAttempt = null;
    this.recoveryError = null;
    this.retrySubmitConfirmedUnknown = false;
    if (this.cleanupTransportState !== null
      && !sameContext(this.cleanupTransportState.binding, binding)) {
      const cleanup = this.state.cleanupPending;
      if (cleanup !== null) {
        await this.cancelCleanup(cleanup, { destroyContext: true });
      }
    }
    const sourceAttemptId = this.id("source");
    this.dispatch({
      type: "start_requested",
      binding,
      captureDuringInference: this.requireActiveConfig().captureDuringInference,
      sourceAttemptId,
    });
    if (this.getState().phase !== "starting" || !sameBinding(this.state.binding, binding)) {
      throw new Error("Vision automation cannot start from the current state");
    }

    return this.acquireSourceCohort(binding, sourceAttemptId);
  }

  async retrySources(): Promise<boolean> {
    const binding = this.state.binding;
    if (binding === null || this.state.phase !== "waiting_for_sources") {
      throw new Error("Vision automation is not waiting for sources");
    }
    const sourceAttemptId = this.id("source");
    this.dispatch({ type: "retry_sources", binding, sourceAttemptId });
    const retryState = this.getState();
    if (retryState.phase !== "starting" || retryState.sourceAttemptId !== sourceAttemptId) {
      throw new Error("Vision automation source retry identity is stale");
    }
    return this.acquireSourceCohort(binding, sourceAttemptId);
  }

  async retryFailedSource(failedSource: FailedSourceIdentity): Promise<boolean> {
    const binding = this.state.binding;
    const failedState = this.failedSourceState;
    if (binding === null || this.state.phase !== "paused_source_error" || failedState === null) {
      throw new Error("Vision automation has no failed source to retry");
    }
    if (failedState.sourceAttemptId !== this.state.sourceAttemptId) {
      throw new Error("Vision automation failed-source identity is stale");
    }
    if (failedState.descriptor.stableAlias !== failedSource.stableAlias
      || failedState.descriptor.sourceId !== failedSource.sourceId) {
      throw new Error("Requested source does not match the failed source");
    }
    const sourceIndex = this.sources.findIndex((source) => (
      sameLogicalSource(source.descriptor, failedState.descriptor)
    ));
    if (sourceIndex === -1) {
      throw new Error("Failed source identity is not in the immutable cohort");
    }
    const sourceAttemptId = this.id("source");
    this.dispatch({ type: "retry_failed_source", binding, sourceAttemptId });
    const retryState = this.getState();
    if (retryState.phase !== "starting" || retryState.sourceAttemptId !== sourceAttemptId) {
      throw new Error("Vision automation failed-source retry identity is stale");
    }
    this.failedSourceState = {
      descriptor: failedState.descriptor,
      sourceAttemptId,
    };
    const acquisition = new AbortController();
    this.acquisitionController = acquisition;
    const expectedSource = failedState.descriptor;
    let restore: Promise<CaptureSource>;
    try {
      // Keep display restoration in the direct Retry user-gesture call stack.
      restore = Promise.resolve(this.options.restoreSource({
        binding,
        ownerId: `${binding.automationTaskId}:${sourceAttemptId}`,
        source: expectedSource,
        requiresDirectDisplayChooser: expectedSource.kind === "display",
        signal: acquisition.signal,
        config: this.requireActiveConfig(),
      }));
    } catch (error) {
      restore = Promise.reject(error);
    }
    return this.completeFailedSourceRestore(
      binding,
      sourceAttemptId,
      sourceIndex,
      expectedSource,
      acquisition,
      restore,
    );
  }

  setManualImageCount(manualImageCount: number): void {
    if (this.sampler === null) return;
    this.sampler.setManualImageCount(
      manualImageCount,
      this.dataUrlBytes(this.options.getManualDataUrls()),
    );
    this.refreshCurrentGauges();
    this.emit();
  }

  async send(userText: string): Promise<boolean> {
    if (this.options.isModelBusy()) {
      throw new Error("Cannot send Vision automation while the model is busy");
    }
    const binding = this.state.binding;
    const captureCycleId = this.state.captureCycleId;
    if (binding === null || captureCycleId === null || this.state.phase !== "capturing"
      || this.coordinator === null || this.sampler === null) {
      throw new Error("Vision automation has no sendable capture cycle");
    }
    const submissionAttemptId = this.id("submission");
    const submission: SubmissionAttemptIdentity = {
      submissionAttemptId,
      captureCycleId,
      cycleGeneration: this.state.cycleGeneration,
    };
    this.dispatch({ type: "send_requested", binding, ...submission });
    if (this.getState().phase !== "freezing") {
      throw new Error("Vision automation send identity is stale");
    }

    await this.coordinator.freeze();
    const batch = this.sampler.freeze();
    this.sampler = null;
    const transaction: SubmissionTransaction = {
      binding,
      submission,
      requestId: this.id("request"),
      userText,
      manualDataUrls: [...this.options.getManualDataUrls()],
      batch,
      payload: null,
      omission: null,
      visionAutomation: null,
      attemptController: new AbortController(),
      accepted: false,
      stableTerminal: false,
      transportResolved: false,
      inheritedCleanup: sameContext(this.state.cleanupPending?.binding ?? binding, binding)
        ? this.state.cleanupPending
        : null,
      answerNowReplacement: null,
    };
    this.transaction = transaction;
    this.refreshCurrentGauges();
    this.recoveryError = null;
    this.retrySubmitConfirmedUnknown = false;
    try {
      await this.prepareTransaction(transaction);
      return await this.submitTransaction(transaction);
    } catch (error) {
      if (!this.isTransactionCurrent(transaction)) return false;
      this.failTransaction(transaction, errorMessage(error));
      return false;
    }
  }

  async retrySubmit(): Promise<boolean> {
    const transaction = this.transaction;
    const binding = this.state.binding;
    if (transaction === null || binding === null || transaction.accepted
      || this.state.phase !== "retryable_submit_error") {
      throw new Error("Vision automation has no retryable submission");
    }
    if (this.options.isModelBusy()) {
      throw new Error("Cannot retry Vision automation while the model is busy");
    }
    const submissionAttemptId = this.id("submission");
    this.dispatch({ type: "retry_submit", binding, submissionAttemptId });
    transaction.submission = {
      submissionAttemptId,
      captureCycleId: transaction.submission.captureCycleId,
      cycleGeneration: transaction.submission.cycleGeneration,
    };
    transaction.transportResolved = false;
    this.recoveryError = null;
    this.retrySubmitConfirmedUnknown = false;
    try {
      await this.prepareTransaction(transaction);
      return await this.submitTransaction(transaction);
    } catch (error) {
      if (!this.isTransactionCurrent(transaction)) return false;
      this.failTransaction(transaction, errorMessage(error));
      return false;
    }
  }

  async retryStatus(): Promise<RequestStatusResult | null> {
    const transaction = this.transaction;
    if (transaction === null) return null;
    try {
      const transactionSignal = transaction.attemptController?.signal;
      const signal = transactionSignal?.aborted ? undefined : transactionSignal;
      const result = await this.options.getRequestStatus(transaction.requestId, signal);
      if (result.status === "unknown") {
        this.markUnresolved(transaction, "Request acceptance is unknown", true);
        return result;
      }
      await this.acceptTransaction(transaction, result.requestId);
      if (result.status === "terminal") {
        await this.stableTransaction(transaction, result);
        return result;
      }
      return await this.waitForStableTerminal(transaction, signal);
    } catch (error) {
      this.markUnresolved(transaction, `Request status query failed: ${errorMessage(error)}`, false);
      return null;
    }
  }

  beginAnswerNowReplacement(
    requestId: string,
    signal?: AbortSignal,
  ): VisionAutomationSubmitLifecycle {
    if (!isValidRequestId(requestId)) {
      throw new TypeError("Answer Now requestId must match the backend lifecycle grammar");
    }
    const transaction = this.transaction;
    if (transaction === null || !transaction.accepted || transaction.stableTerminal
      || this.state.activeRequestId !== transaction.requestId) {
      throw new Error("Vision automation has no active accepted request to replace");
    }
    const supersededRequestId = transaction.requestId;
    transaction.answerNowReplacement = {
      originalRequestId: supersededRequestId,
      replacementRequestId: requestId,
      replacementAccepted: false,
      bufferedOriginalTerminal: null,
    };
    transaction.transportResolved = false;
    this.recoveryError = null;
    this.retrySubmitConfirmedUnknown = false;
    return {
      onRequestAccepted: async (event) => {
        if (!this.isTransactionCurrent(transaction)) return;
        if (event.requestId !== requestId) {
          throw new RequestAcceptanceIdentityError(
            `Accepted Answer Now request ID ${event.requestId} does not match ${requestId}`,
          );
        }
        const replacement = transaction.answerNowReplacement;
        if (replacement === null) return;
        replacement.replacementAccepted = true;
        this.dispatch({
          type: "request_superseded",
          binding: transaction.binding,
          requestId: replacement.originalRequestId,
          replacementRequestId: replacement.replacementRequestId,
          ...transaction.submission,
        });
        transaction.requestId = replacement.replacementRequestId;
        replacement.bufferedOriginalTerminal = null;
      },
      onKnownRejection: async () => {
        const replacement = transaction.answerNowReplacement;
        if (!replacement || replacement.replacementAccepted || !this.isTransactionCurrent(transaction)) return false;
        transaction.answerNowReplacement = null;
        const buffered = replacement.bufferedOriginalTerminal;
        if (buffered) {
          // Stable terminal dispatch updates lifecycle state synchronously;
          // capture-cycle recovery may remain asynchronous and must not hold
          // the rejection callback open behind a browser capture barrier.
          await this.stableTransaction(transaction, buffered, true);
        }
        return true;
      },
      onTransportEof: async () => {
        if (!this.isTransactionCurrent(transaction)) return;
        transaction.transportResolved = true;
        await this.resolveAmbiguousTransport(transaction, signal);
      },
      onStableTerminal: async (result) => {
        if (!this.isTransactionCurrent(transaction)) return;
        await this.stableTransaction(transaction, result);
      },
    };
  }

  async abort(options: { registerAgentCleanup?: boolean } = {}): Promise<void> {
    const binding = this.state.binding;
    if (binding === null || !this.state.armed) return;
    const cleanupAttemptId = this.id("cleanup");
    const acceptedImageBlock = this.acceptedImageBlockState !== null
      && sameBinding(this.acceptedImageBlockState.binding, binding);
    const inheritedCleanup = this.state.cleanupPending;
    const inheritedTransport = this.cleanupTransportState;
    const hasValidInheritedCleanup = inheritedCleanup !== null
      && inheritedTransport !== null
      && inheritedTransport.cleanupAttemptId === inheritedCleanup.cleanupAttemptId
      && sameContext(inheritedCleanup.binding, binding)
      && sameContext(inheritedTransport.binding, binding);
    this.recoveryError = null;
    this.retrySubmitConfirmedUnknown = false;
    this.dispatch({
      type: "abort_requested",
      binding,
      cleanupAttemptId,
      preserveCleanup: acceptedImageBlock || hasValidInheritedCleanup,
    });
    if (acceptedImageBlock) {
      this.cleanupTransportState = {
        ...this.acceptedImageBlockState!,
        cleanupAttemptId,
        cleanupToken: cleanupAttemptId,
        registration: binding.mode === "agent" ? "pending" : "not_required",
        registrationError: null,
      };
    } else if (hasValidInheritedCleanup) {
      this.cleanupTransportState = {
        ...inheritedTransport!,
        cleanupAttemptId,
      };
    } else {
      this.cleanupTransportState = null;
    }
    this.acceptedImageBlockState = null;
    this.failedSourceState = null;
    this.pendingRecoveryCycleState = null;
    this.acquisitionController?.abort(new DOMException("Vision automation aborted", "AbortError"));
    const transaction = this.transaction;
    if (transaction !== null && !transaction.accepted) {
      transaction.attemptController?.abort(new DOMException("Vision automation aborted", "AbortError"));
      transaction.omission?.discard();
      transaction.batch?.dispose();
      this.releaseTransactionPayload(transaction);
    }
    this.transaction = null;
    this.sampler?.dispose();
    this.sampler = null;
    await this.coordinator?.abort();
    this.coordinator = null;
    this.sources = [];
    this.dispatch({ type: "abort_complete", binding, cleanupAttemptId });
    if (options.registerAgentCleanup !== false
      && this.cleanupTransportState?.registration === "pending") {
      try {
        await this.ensureCleanupRegistration();
      } catch {
        // Registration remains pending; local media teardown is already complete.
      }
    }
    this.activeConfig = null;
    this.activeCaptureAttempt = null;
    this.refreshCurrentGauges();
    this.emit();
  }

  stageCleanupSend(
    binding: AutomationBinding,
    requestId: string,
  ): Promise<StagedVisionAutomationCleanupSend | null> {
    const token = this.state.cleanupPending;
    const cleanupTransport = this.cleanupTransportState;
    if (token === null
      || cleanupTransport === null
      || cleanupTransport.cleanupAttemptId !== token.cleanupAttemptId
      || !sameContext(token.binding, binding)
      || !sameContext(cleanupTransport.binding, binding)) return Promise.resolve(null);
    if (this.cleanupStage !== null) return Promise.resolve(this.cleanupStage);
    if (this.cleanupStagePromise !== null) return this.cleanupStagePromise;
    if (!isValidRequestId(requestId)) {
      return Promise.reject(new TypeError("Cleanup requestId must match the backend lifecycle grammar"));
    }
    const stagePromise = this.createCleanupStage(token, cleanupTransport, requestId);
    this.cleanupStagePromise = stagePromise;
    void stagePromise.finally(() => {
      if (this.cleanupStagePromise === stagePromise) this.cleanupStagePromise = null;
    }).catch(() => undefined);
    return stagePromise;
  }

  private async createCleanupStage(
    token: CleanupPendingToken,
    cleanupTransport: CleanupTransportState,
    requestId: string,
  ): Promise<StagedVisionAutomationCleanupSend | null> {
    await this.ensureCleanupRegistration();
    if (this.state.cleanupPending !== token
      || this.cleanupTransportState !== cleanupTransport
      || (cleanupTransport.binding.mode === "agent"
        && cleanupTransport.registration !== "registered")) return null;
    const omission = this.stageHistoricalImageOmission(cleanupTransport.binding, 0, 0);
    const contract = buildVisionAutomationRequestContract({
      requestId,
      manualImageCount: 0,
      sourceCount: 0,
      retainedTickCount: 0,
      binding: {
        pageGeneration: cleanupTransport.binding.pageGeneration,
        mode: cleanupTransport.binding.mode,
        topLevelContextId: cleanupTransport.binding.contextId,
        automationTaskId: cleanupTransport.binding.automationTaskId,
        captureCycleId: cleanupTransport.captureCycleId,
        cleanupToken: cleanupTransport.cleanupToken,
      },
    });
    let completed = false;
    let discarded = false;
    let attemptActive = false;
    let stage!: StagedVisionAutomationCleanupSend;
    stage = {
      token,
      requestId,
      stagedMessages: omission.stagedMessages,
      visionAutomation: contract.visionAutomation,
      beginAttempt: () => {
        if (completed || discarded || attemptActive) return false;
        attemptActive = true;
        this.cleanupStageAttemptActive = true;
        return true;
      },
      finishAttempt: () => {
        attemptActive = false;
        this.cleanupStageAttemptActive = false;
      },
      commitAccepted: (event) => {
        if (discarded) throw new Error("Staged cleanup omission was discarded");
        if (completed) throw new Error("Staged cleanup omission was already committed");
        if (event.requestId !== requestId) {
          throw new RequestAcceptanceIdentityError(
            `Accepted cleanup request ID ${event.requestId} does not match ${requestId}`,
          );
        }
        const currentToken = this.state.cleanupPending;
        if (currentToken === null
          || currentToken.cleanupAttemptId !== token.cleanupAttemptId
          || !sameContext(currentToken.binding, token.binding)) {
          throw new Error("Staged cleanup permission is stale");
        }
        const committed = omission.commit(this.options.getMessages());
        this.options.commitMessages(committed);
        completed = true;
        this.dispatch({
          type: "cleanup_consumed",
          binding: token.binding,
          cleanupAttemptId: token.cleanupAttemptId,
        });
        this.cleanupTransportState = null;
        attemptActive = false;
        this.cleanupStageAttemptActive = false;
        if (this.cleanupStage === stage) this.cleanupStage = null;
        return true;
      },
      discard: () => {
        if (completed || discarded) return;
        discarded = true;
        attemptActive = false;
        this.cleanupStageAttemptActive = false;
        omission.discard();
        if (this.cleanupStage === stage) this.cleanupStage = null;
      },
    };
    this.cleanupStage = stage;
    return stage;
  }

  async cancelCleanup(
    token: CleanupPendingToken,
    options: { signal?: AbortSignal; destroyContext?: boolean } = {},
  ): Promise<boolean> {
    const current = this.state.cleanupPending;
    const cleanup = this.cleanupTransportState;
    if (current === null
      || cleanup === null
      || current.cleanupAttemptId !== token.cleanupAttemptId
      || cleanup.cleanupAttemptId !== token.cleanupAttemptId
      || !sameContext(current.binding, token.binding)
      || !sameContext(cleanup.binding, token.binding)) return false;
    if (this.cleanupCancellationPromise !== null) return this.cleanupCancellationPromise;

    const cancellation = (async () => {
      this.cleanupRegistrationController?.abort(
        new DOMException("Vision automation cleanup registration cancelled", "AbortError"),
      );
      try {
        await this.cleanupRegistrationPromise;
      } catch {
        // The exact token may still have reached the backend; cancellation remains required.
      }

      try {
        if (cleanup.binding.mode === "agent") {
          if (this.options.cancelAgentAbort === undefined) {
            throw new Error("Agent cleanup cancellation client is unavailable");
          }
          const controller = new AbortController();
          this.cleanupCancellationController = controller;
          const abortFromParent = () => controller.abort(options.signal?.reason);
          if (options.signal?.aborted) abortFromParent();
          else options.signal?.addEventListener("abort", abortFromParent, { once: true });
          try {
            await this.options.cancelAgentAbort(
              this.buildCleanupRegistrationRequest(cleanup),
              controller.signal,
            );
          } finally {
            options.signal?.removeEventListener("abort", abortFromParent);
            if (this.cleanupCancellationController === controller) {
              this.cleanupCancellationController = null;
            }
          }
        }
        if (this.cleanupTransportState !== cleanup) return false;
        this.dispatch({
          type: "cleanup_cancelled",
          binding: token.binding,
          cleanupAttemptId: token.cleanupAttemptId,
        });
        const cancelled = this.state.cleanupPending === null;
        if (cancelled) {
          this.cleanupStage?.discard();
          this.cleanupStage = null;
          this.cleanupTransportState = null;
        }
        return cancelled;
      } catch (error) {
        if (this.cleanupTransportState === cleanup) {
          cleanup.registrationError = `Agent cleanup cancellation failed: ${errorMessage(error)}`;
          this.emit();
        }
        if (!options.destroyContext) throw error;
        this.dispatch({
          type: "cleanup_cancelled",
          binding: token.binding,
          cleanupAttemptId: token.cleanupAttemptId,
        });
        if (this.cleanupTransportState === cleanup) {
          this.cleanupStage?.discard();
          this.cleanupStage = null;
          this.cleanupTransportState = null;
        }
        return true;
      }
    })();
    this.cleanupCancellationPromise = cancellation;
    try {
      return await cancellation;
    } finally {
      if (this.cleanupCancellationPromise === cancellation) {
        this.cleanupCancellationPromise = null;
      }
    }
  }

  async dispose(): Promise<void> {
    if (this.disposed) return;
    if (this.state.armed) await this.abort({ registerAgentCleanup: false });
    const cleanup = this.state.cleanupPending;
    if (cleanup !== null) await this.cancelCleanup(cleanup, { destroyContext: true });
    this.cleanupCancellationController?.abort(
      new DOMException("Vision automation controller disposed", "AbortError"),
    );
    this.listeners.clear();
    this.disposed = true;
  }

  private id(kind: VisionAutomationIdKind): string {
    return (this.options.createId ?? defaultId)(kind);
  }

  private dispatch(action: VisionAutomationAction): void {
    const next = reduceVisionAutomation(this.state, action);
    if (next === this.state) return;
    this.state = next;
    this.emit();
  }

  private emit(): void {
    this.listeners.forEach((listener) => listener(this.state));
  }

  private async acquireSourceCohort(
    binding: AutomationBinding,
    sourceAttemptId: string,
  ): Promise<boolean> {
    const acquisition = new AbortController();
    this.acquisitionController = acquisition;
    let sources: readonly CaptureSource[] = [];
    let installedSources: readonly RecoverableCaptureSource[] | null = null;
    let captureCycle: FullCaptureCallbackToken | null = null;
    try {
      sources = await this.options.acquireSources({
        binding,
        ownerId: `${binding.automationTaskId}:${sourceAttemptId}`,
        signal: acquisition.signal,
        config: this.requireActiveConfig(),
      });
      if (sources.length === 0) throw new Error("No Vision automation source is available");
      if (!sameBinding(this.state.binding, binding)
        || this.state.sourceAttemptId !== sourceAttemptId
        || this.state.phase !== "starting"
        || acquisition.signal.aborted) {
        sources.forEach((source) => source.dispose());
        return false;
      }

      const sourceCount = sources.length;
      installedSources = sources.map((source, index) => new RecoverableCaptureSource(
        source,
        (descriptor) => this.recordEndedSource(descriptor),
        () => this.beginCaptureAttempt(index, sourceCount),
        (attemptId, succeeded) => this.settleCaptureAttempt(attemptId, succeeded),
      ));
      this.sources = installedSources;
      const captureCycleId = this.id("cycle");
      this.dispatch({ type: "sources_ready", binding, sourceAttemptId, captureCycleId });
      captureCycle = this.currentCaptureToken();
      if (captureCycle === null) throw new Error("Sources became stale before capture initialization");
      await this.startCaptureCycle(captureCycleId);
      return true;
    } catch (error) {
      const message = errorMessage(error);
      const ownsFailedCycle = installedSources !== null
        && captureCycle !== null
        && this.sources === installedSources
        && sameBinding(this.state.binding, binding)
        && this.state.phase === "capturing"
        && this.state.sourceAttemptId === sourceAttemptId
        && sameCaptureCallbackToken(captureCycle, this.currentCaptureToken());
      if (ownsFailedCycle && installedSources !== null && captureCycle !== null) {
        const sampler = this.sampler;
        this.sampler = null;
        sampler?.dispose();
        const coordinator = this.coordinator;
        this.coordinator = null;
        if (coordinator !== null) await coordinator.abort();
        else installedSources.forEach((source) => source.dispose());
        this.sources = [];
        this.failedSourceState = null;
        this.pendingRecoveryCycleState = null;
        this.dispatch({
          type: "capture_cycle_failed",
          binding,
          sourceAttemptId,
          captureCycleId: captureCycle.captureCycleId,
          cycleGeneration: captureCycle.cycleGeneration,
          error: message,
        });
      } else if (installedSources !== null) {
        installedSources.forEach((source) => source.dispose());
        if (this.sources === installedSources) this.sources = [];
      } else {
        sources.forEach((source) => source.dispose());
      }
      if (sameBinding(this.state.binding, binding)
        && this.state.phase === "starting"
        && this.state.sourceAttemptId === sourceAttemptId) {
        this.dispatch({
          type: "sources_unavailable",
          binding,
          sourceAttemptId,
          error: message,
        });
      }
      return false;
    } finally {
      if (this.acquisitionController === acquisition) this.acquisitionController = null;
    }
  }

  private async completeFailedSourceRestore(
    binding: AutomationBinding,
    sourceAttemptId: string,
    sourceIndex: number,
    expectedSource: SourceDescriptor,
    acquisition: AbortController,
    restore: Promise<CaptureSource>,
  ): Promise<boolean> {
    let replacement: CaptureSource | null = null;
    try {
      replacement = await restore;
      if (Array.isArray(replacement)
        || replacement === null
        || typeof replacement !== "object"
        || !sameLogicalSource(expectedSource, replacement.descriptor)) {
        throw new Error("Restored source identity or cohort shape does not match the failed source");
      }
      if (!sameBinding(this.state.binding, binding)
        || this.state.phase !== "starting"
        || this.state.sourceAttemptId !== sourceAttemptId
        || acquisition.signal.aborted) {
        replacement.dispose();
        return false;
      }

      await this.coordinator?.freeze();
      if (!sameBinding(this.state.binding, binding)
        || this.state.phase !== "starting"
        || this.state.sourceAttemptId !== sourceAttemptId
        || acquisition.signal.aborted) {
        replacement.dispose();
        return false;
      }
      const source = this.sources[sourceIndex];
      if (source === undefined || !sameLogicalSource(source.descriptor, expectedSource)) {
        throw new Error("Immutable source cohort changed during recovery");
      }
      source.replace(replacement);
      replacement = null;
      this.dispatch({ type: "source_recovered", binding, sourceAttemptId });
      const recoveredState = this.getState();
      if (recoveredState.phase === "capturing" || recoveredState.phase === "capturing_during_model") {
        await this.restartCaptureAfterSourceRestore(expectedSource);
      }
      if (this.getState().phase === "paused_source_error") return false;
      this.failedSourceState = null;
      return true;
    } catch (error) {
      replacement?.dispose();
      if (sameBinding(this.state.binding, binding)
        && this.state.sourceAttemptId === sourceAttemptId) {
        this.pendingRecoveryCycleState = null;
        this.failedSourceState = { descriptor: expectedSource, sourceAttemptId };
        if (this.state.phase === "starting") {
          this.dispatch({ type: "source_recovered", binding, sourceAttemptId });
        }
        if (this.state.phase !== "paused_source_error") {
          this.dispatch({
            type: "source_error",
            binding,
            sourceAttemptId,
            error: errorMessage(error),
          });
        }
      }
      return false;
    } finally {
      if (this.acquisitionController === acquisition) this.acquisitionController = null;
    }
  }

  private async restartCaptureAfterSourceRestore(expectedSource: SourceDescriptor): Promise<void> {
    if (this.coordinator === null) {
      throw new Error("Cannot restart source recovery without the existing coordinator");
    }
    const token = this.currentCaptureToken();
    if (token === null) throw new Error("Cannot restart source recovery without a full token");
    const pending = this.pendingRecoveryCycleState;
    let transitionToken: FullCaptureCallbackToken | null = null;
    if (pending !== null) {
      if (sameLogicalSource(pending.failedDescriptor, expectedSource)
        && sameCaptureCallbackToken(pending.token, token)) {
        transitionToken = pending.token;
      }
      this.pendingRecoveryCycleState = null;
    }
    if (transitionToken !== null) {
      await this.startCaptureCycle(transitionToken.captureCycleId, transitionToken);
      return;
    }
    if (!sameCaptureCallbackToken(this.coordinator.token, token)) {
      await this.startCaptureCycle(token.captureCycleId, token);
      return;
    }
    if (this.sampler === null) {
      throw new Error("Cannot resume source recovery without the existing sampler");
    }
    await this.coordinator.resumeCycle(token);
  }

  private currentCaptureToken(): FullCaptureCallbackToken | null {
    const binding = this.state.binding;
    const captureCycleId = this.state.captureCycleId;
    if (binding === null || captureCycleId === null) return null;
    return {
      ...binding,
      captureCycleId,
      cycleGeneration: this.state.cycleGeneration,
    };
  }

  private async startCaptureCycle(
    captureCycleId: string,
    selectedToken?: FullCaptureCallbackToken,
  ): Promise<void> {
    const binding = this.state.binding;
    if (binding === null || this.state.captureCycleId !== captureCycleId) {
      throw new Error("Cannot create a stale Vision automation capture cycle");
    }
    const currentToken = this.currentCaptureToken();
    if (currentToken === null) throw new Error("Cannot create a capture cycle without a full token");
    if (selectedToken !== undefined && !sameCaptureCallbackToken(selectedToken, currentToken)) {
      throw new Error("Cannot create a capture cycle from a stale lifecycle token");
    }
    const token = selectedToken ?? currentToken;
    const sampler = new TickBundleSampler({
      sources: this.sources.map((source) => source.descriptor),
      maxFramesPerSource: this.requireActiveConfig().maxFramesPerSource,
      manualImageCount: this.options.getManualDataUrls().length,
      manualEncodedBytes: this.dataUrlBytes(this.options.getManualDataUrls()),
      maxEncodedPayloadBytes: this.requireActiveConfig().maxEncodedPayloadBytes,
      lockFirstBundle: this.requireActiveConfig().lockFirstBundle,
    });
    this.sampler = sampler;
    this.refreshCurrentGauges();
    if (this.coordinator === null) {
      this.coordinator = new TickCoordinator({
        sources: this.sources,
        token,
        intervalMs: this.requireActiveConfig().intervalSeconds * 1_000,
        timeoutMs: this.requireActiveConfig().tickTimeoutSeconds * 1_000,
        failureLimit: this.requireActiveConfig().consecutiveTickFailureLimit,
        getCurrentToken: () => this.currentCaptureToken(),
        onBundle: (bundle, callbackToken) => {
          const currentSampler = this.sampler;
          if (currentSampler === null) {
            bundle.frames.forEach((frame) => frame.resource.dispose());
            return;
          }
          currentSampler.add(bundle);
          this.stats.successfulTicks += 1;
          this.recordAcceptedBundle(callbackToken);
          this.refreshCurrentGauges();
          this.emit();
        },
        onBundleRejected: (error, callbackToken) => {
          this.recordRejectedBundle(error, callbackToken);
        },
        onPause: (reason) => {
          const currentBinding = this.state.binding;
          const sourceAttemptId = this.state.sourceAttemptId;
          if (currentBinding !== null && sourceAttemptId !== null) {
            this.recordFailedSourceFromCoordinator(sourceAttemptId);
            this.dispatch({
              type: "source_error",
              binding: currentBinding,
              sourceAttemptId,
              error: reason,
            });
          }
        },
      });
    } else {
      this.coordinator.transitionCycle(token);
    }
    await this.coordinator.start();
  }

  private deferLifecycleCycleForRecovery(
    captureCycleId: string,
    lifecycle: PendingRecoveryCycleState["lifecycle"],
  ): boolean {
    if (this.state.captureCycleId !== captureCycleId) {
      this.pendingRecoveryCycleState = null;
      return false;
    }
    const token = this.currentCaptureToken();
    const failedState = this.failedSourceState;
    const recoveryPending = failedState !== null
      && (this.state.phase === "starting" || this.state.phase === "paused_source_error");
    if (token === null || !recoveryPending
      || this.coordinator === null
      || sameCaptureCallbackToken(this.coordinator.token, token)) {
      this.pendingRecoveryCycleState = null;
      return false;
    }
    this.pendingRecoveryCycleState = {
      token,
      failedDescriptor: failedState.descriptor,
      lifecycle,
    };
    return true;
  }

  private reconcilePendingRecoveryCycle(
    lifecycle: PendingRecoveryCycleState["lifecycle"],
  ): void {
    const pending = this.pendingRecoveryCycleState;
    if (pending === null) return;
    const currentToken = this.currentCaptureToken();
    const failedState = this.failedSourceState;
    if (currentToken === null
      || failedState === null
      || (this.state.phase !== "starting" && this.state.phase !== "paused_source_error")
      || !sameLogicalSource(pending.failedDescriptor, failedState.descriptor)
      || !sameCaptureCallbackToken(pending.token, currentToken)) {
      this.pendingRecoveryCycleState = null;
      return;
    }
    this.pendingRecoveryCycleState = { ...pending, lifecycle };
  }

  private recordEndedSource(descriptor: SourceDescriptor): void {
    const sourceAttemptId = this.state.sourceAttemptId;
    if (sourceAttemptId === null || !this.state.armed || this.state.phase === "aborting"
      || this.failedSourceState !== null) return;
    this.failedSourceState = { descriptor, sourceAttemptId };
  }

  private recordFailedSourceFromCoordinator(sourceAttemptId: string): void {
    if (this.failedSourceState !== null) return;
    const failedSource = this.sources.find((source) => (
      (this.coordinator?.failureCountFor(source.descriptor.stableAlias) ?? 0)
        >= this.requireActiveConfig().consecutiveTickFailureLimit
    ));
    if (failedSource !== undefined) {
      this.failedSourceState = {
        descriptor: failedSource.descriptor,
        sourceAttemptId,
      };
    }
  }

  private recordAcceptedBundle(callbackToken: CaptureCallbackToken): void {
    const binding = this.state.binding;
    const sourceAttemptId = this.state.sourceAttemptId;
    if (binding === null || sourceAttemptId === null
      || !sameCaptureCallbackToken(callbackToken, this.currentCaptureToken())) return;
    this.dispatch({
      type: "bundle_accepted",
      binding,
      sourceAttemptId,
      captureCycleId: callbackToken.captureCycleId,
      cycleGeneration: callbackToken.cycleGeneration,
    });
  }

  private recordRejectedBundle(error: unknown, callbackToken: CaptureCallbackToken): void {
    const binding = this.state.binding;
    const sourceAttemptId = this.state.sourceAttemptId;
    if (binding === null || sourceAttemptId === null
      || !sameCaptureCallbackToken(callbackToken, this.currentCaptureToken())) return;
    this.dispatch({
      type: "bundle_rejected",
      binding,
      sourceAttemptId,
      captureCycleId: callbackToken.captureCycleId,
      cycleGeneration: callbackToken.cycleGeneration,
      error: `Captured bundle was rejected: ${errorMessage(error)}`,
    });
  }

  private stageHistoricalImageOmission(
    binding: AutomationBinding,
    sourceCount: number,
    retainedTickCount: number,
  ): StagedChatHistoryOmission {
    const messages = this.options.getMessages();
    const sticky = (
      this.historicalImageOmissionStickyBinding !== null
      && sameContext(this.historicalImageOmissionStickyBinding, binding)
    );
    if (!shouldStageVisionAutomationHistoryOmission({
      sourceCount,
      retainedTickCount,
      sticky,
    })) {
      return stagePassThroughHistory(messages);
    }
    return stageChatHistoryImageOmission(
      messages,
      this.options.resolveHistoricalImageBlock,
    );
  }

  private async prepareTransaction(transaction: SubmissionTransaction): Promise<void> {
    if (transaction.payload !== null && transaction.omission !== null
      && transaction.visionAutomation !== null) return;
    if (transaction.batch === null) throw new Error("Frozen Vision automation batch is unavailable");
    if (transaction.inheritedCleanup !== null) {
      await this.ensureCleanupRegistration();
      this.reconcileInheritedCleanup(transaction);
    }
    const payload = await prepareVisionPayload({
      manualDataUrls: transaction.manualDataUrls,
      batch: transaction.batch,
      userText: transaction.userText,
      maxEncodedPayloadBytes: this.requireActiveConfig().maxEncodedPayloadBytes,
      lockFirstBundle: this.requireActiveConfig().lockFirstBundle,
      compression: {
        initialQuality: this.requireActiveConfig().initialJpegQuality,
        minimumQuality: this.requireActiveConfig().minimumJpegQuality,
        maxDimensionPx: this.requireActiveConfig().maxImageDimensionPx,
        minimumDimensionPx: this.requireActiveConfig().minimumImageDimensionPx,
      },
      encodeConcurrency: this.requireActiveConfig().base64EncodeConcurrency,
      encodeFrame: this.options.encodeFrame,
      blobToDataUrl: this.options.blobToDataUrl,
      signal: transaction.attemptController?.signal,
    });
    this.reconcileInheritedCleanup(transaction);
    const omission = this.stageHistoricalImageOmission(
      transaction.binding,
      payload.sourceCount,
      payload.retainedTickCount,
    );
    const contract = buildVisionAutomationRequestContract({
      requestId: transaction.requestId,
      manualImageCount: transaction.manualDataUrls.length,
      sourceCount: payload.sourceCount,
      retainedTickCount: payload.retainedTickCount,
      binding: {
        pageGeneration: transaction.binding.pageGeneration,
        mode: transaction.binding.mode,
        topLevelContextId: transaction.binding.contextId,
        automationTaskId: transaction.binding.automationTaskId,
        captureCycleId: transaction.submission.captureCycleId,
        cleanupToken: transaction.inheritedCleanup !== null
          && this.cleanupTransportState?.cleanupAttemptId
            === transaction.inheritedCleanup.cleanupAttemptId
          ? this.cleanupTransportState.cleanupToken
          : undefined,
      },
    });
    transaction.payload = payload;
    transaction.omission = omission;
    transaction.visionAutomation = contract.visionAutomation;
  }

  private async submitTransaction(transaction: SubmissionTransaction): Promise<boolean> {
    if (transaction.payload === null || transaction.omission === null
      || transaction.visionAutomation === null) {
      throw new Error("Vision automation transaction is not prepared");
    }
    const attemptController = transaction.attemptController;
    if (attemptController === null || attemptController.signal.aborted) {
      throw new Error("Vision automation transaction was aborted");
    }
    const lifecycleRequestId = transaction.requestId;
    const lifecycleSubmissionAttemptId = transaction.submission.submissionAttemptId;
    const isCurrentLifecycle = () => (
      transaction.requestId === lifecycleRequestId
      && transaction.submission.submissionAttemptId === lifecycleSubmissionAttemptId
    );
    try {
      await this.options.submit({
        binding: transaction.binding,
        submission: transaction.submission,
        requestId: transaction.requestId,
        userText: transaction.userText,
        payload: transaction.payload,
        stagedMessages: transaction.omission.stagedMessages,
        visionAutomation: transaction.visionAutomation,
        controller: attemptController,
        signal: attemptController.signal,
        lifecycle: {
          onRequestAccepted: async (event) => {
            if (!isCurrentLifecycle()) return;
            await this.acceptTransaction(transaction, event.requestId);
          },
          onKnownRejection: async (rejection) => {
            if (!isCurrentLifecycle()) return false;
            await this.rejectKnownPreAcceptance(transaction, rejection);
            return true;
          },
          onTransportEof: async () => {
            if (!isCurrentLifecycle()) return;
            transaction.transportResolved = true;
            await this.resolveAmbiguousTransport(
              transaction,
              attemptController.signal.aborted ? undefined : attemptController.signal,
            );
          },
          onStableTerminal: async (result) => {
            if (!isCurrentLifecycle()) return;
            await this.acceptTransaction(transaction, result.requestId);
            await this.stableTransaction(transaction, result);
          },
        },
        getState: () => this.state,
        isAccepted: () => transaction.accepted,
      });
      if (transaction.accepted) return true;
      if (transaction.transportResolved) return false;
      return await this.resolveAmbiguousTransport(transaction, attemptController.signal);
    } catch (error) {
      if (!this.isTransactionCurrent(transaction)) return false;
      return await this.resolveAmbiguousTransport(
        transaction,
        attemptController.signal.aborted ? undefined : attemptController.signal,
        error,
      );
    }
  }

  private async resolveAmbiguousTransport(
    transaction: SubmissionTransaction,
    signal?: AbortSignal,
    transportError?: unknown,
  ): Promise<boolean> {
    if (!this.isTransactionCurrent(transaction)) return false;
    try {
      const result = await this.options.getRequestStatus(transaction.requestId, signal);
      if (result.status === "unknown") {
        this.markUnresolved(
          transaction,
          transportError === undefined
            ? "Request acceptance is unknown"
            : `Request acceptance is unknown: ${errorMessage(transportError)}`,
          true,
        );
        this.failTransaction(
          transaction,
          transportError === undefined
            ? "Request acceptance is unknown"
            : `Request acceptance is unknown: ${errorMessage(transportError)}`,
        );
        return false;
      }
      await this.acceptTransaction(transaction, result.requestId);
      if (result.status === "terminal") await this.stableTransaction(transaction, result);
      else await this.waitForStableTerminal(transaction, signal);
      return true;
    } catch (statusError) {
      if (!this.isTransactionCurrent(transaction)) return false;
      const message = `Request status query failed: ${errorMessage(statusError)}`;
      this.markUnresolved(transaction, message, false);
      if (!transaction.accepted) this.failTransaction(transaction, message);
      return transaction.accepted;
    }
  }

  private async rejectKnownPreAcceptance(
    transaction: SubmissionTransaction,
    rejection: KnownRequestRejection,
  ): Promise<void> {
    if (!this.isTransactionCurrent(transaction) || transaction.accepted) return;
    transaction.transportResolved = true;
    this.recoveryError = rejection.message;
    // A known busy/temporary refusal has never reached admission.  Its frozen
    // staged omission and exact request ID are therefore safe to retry.
    this.retrySubmitConfirmedUnknown = rejection.retryable;
    if (rejection.retryable) {
      this.failTransaction(transaction, rejection.message);
      return;
    }

    // Validation/conflict rejections that are not retryable must never commit
    // or retain an omission transaction.  Discarding is local only and leaves
    // canonical conversation history untouched.
    transaction.omission?.discard();
    transaction.batch?.dispose();
    transaction.batch = null;
    this.releaseTransactionPayload(transaction);
    transaction.attemptController = null;
    if (this.transaction === transaction) this.transaction = null;
    const nextCaptureCycleId = this.id("cycle");
    this.dispatch({
      type: "submission_rejected",
      binding: transaction.binding,
      error: rejection.message,
      nextCaptureCycleId,
      ...transaction.submission,
    });
    if (this.state.phase === "capturing" && this.state.captureCycleId === nextCaptureCycleId) {
      await this.startCaptureCycle(nextCaptureCycleId);
    }
  }

  private async acceptTransaction(transaction: SubmissionTransaction, requestId: string): Promise<void> {
    if (!this.isTransactionCurrent(transaction)) return;
    if (requestId !== transaction.requestId) throw new Error("Accepted request identity does not match");
    if (transaction.accepted) return;
    if (this.state.phase === "retryable_submit_error") {
      const submissionAttemptId = this.id("submission");
      this.dispatch({
        type: "retry_submit",
        binding: transaction.binding,
        submissionAttemptId,
      });
      transaction.submission = {
        ...transaction.submission,
        submissionAttemptId,
      };
      if (!this.isTransactionCurrent(transaction) || this.getState().phase !== "freezing") {
        throw new Error("Delayed request acceptance identity is stale");
      }
    }
    if (transaction.omission === null) throw new Error("Accepted omission transaction is unavailable");
    const acceptedImageBlock = transaction.payload !== null && transaction.payload.images.length > 0;
    const committed = transaction.omission.commit(this.options.getMessages());
    this.options.commitMessages(committed);
    transaction.accepted = true;
    this.recoveryError = null;
    this.retrySubmitConfirmedUnknown = false;
    await this.options.onRequestAccepted?.(requestId);
    if (acceptedImageBlock) {
      this.acceptedImageBlockState = {
        binding: transaction.binding,
        captureCycleId: transaction.submission.captureCycleId,
      };
    }
    if (transaction.payload !== null && shouldStageVisionAutomationHistoryOmission({
      sourceCount: transaction.payload.sourceCount,
      retainedTickCount: transaction.payload.retainedTickCount,
      sticky: false,
    })) {
      this.historicalImageOmissionStickyBinding = transaction.binding;
    }
    const inheritedCleanup = transaction.inheritedCleanup;
    if (inheritedCleanup !== null) {
      if (sameCleanupToken(this.state.cleanupPending, inheritedCleanup)
        && this.cleanupTransportState?.cleanupAttemptId === inheritedCleanup.cleanupAttemptId
        && sameContext(this.cleanupTransportState.binding, inheritedCleanup.binding)) {
        this.dispatch({
          type: "cleanup_consumed",
          binding: inheritedCleanup.binding,
          cleanupAttemptId: inheritedCleanup.cleanupAttemptId,
        });
        this.cleanupTransportState = null;
      }
      transaction.inheritedCleanup = null;
    }
    transaction.batch?.dispose();
    transaction.batch = null;
    transaction.omission = null;
    transaction.payload = null;
    transaction.manualDataUrls = [];
    transaction.visionAutomation = null;
    this.refreshCurrentGauges();

    const nextCaptureCycleId = this.state.captureDuringInference ? this.id("cycle") : undefined;
    this.dispatch({
      type: "request_accepted",
      binding: transaction.binding,
      requestId,
      ...transaction.submission,
      nextCaptureCycleId,
    });
    if (nextCaptureCycleId !== undefined && this.state.captureCycleId === nextCaptureCycleId) {
      if (!this.deferLifecycleCycleForRecovery(nextCaptureCycleId, "request_accepted")) {
        await this.startCaptureCycle(nextCaptureCycleId);
      }
    } else {
      this.reconcilePendingRecoveryCycle("request_accepted");
    }
  }

  private async stableTransaction(
    transaction: SubmissionTransaction,
    result: RequestStatusResult,
    deferCaptureCycle = false,
  ): Promise<void> {
    if (!this.isTransactionCurrent(transaction)) return;
    const replacement = transaction.answerNowReplacement;
    if (replacement && !replacement.replacementAccepted
      && result.requestId === replacement.originalRequestId
      && result.status === "terminal") {
      if (replacement.bufferedOriginalTerminal === null) {
        replacement.bufferedOriginalTerminal = result;
      }
      return;
    }
    if (result.requestId !== transaction.requestId || result.status !== "terminal") {
      throw new Error("Stable terminal identity or status does not match");
    }
    if (transaction.stableTerminal || !transaction.accepted) return;
    transaction.stableTerminal = true;
    this.recoveryError = null;
    this.retrySubmitConfirmedUnknown = false;
    const nextCaptureCycleId = this.state.captureDuringInference ? undefined : this.id("cycle");
    this.dispatch({
      type: "stable_terminal",
      binding: transaction.binding,
      requestId: transaction.requestId,
      ...transaction.submission,
      nextCaptureCycleId,
    });
    if (nextCaptureCycleId !== undefined && this.state.captureCycleId === nextCaptureCycleId) {
      if (!this.deferLifecycleCycleForRecovery(nextCaptureCycleId, "stable_terminal")) {
        if (deferCaptureCycle) {
          void this.startCaptureCycle(nextCaptureCycleId).catch((error) => {
            if (this.state.captureCycleId === nextCaptureCycleId) {
              this.markUnresolved(transaction, errorMessage(error), false);
            }
          });
        } else {
          await this.startCaptureCycle(nextCaptureCycleId);
        }
      }
    } else {
      this.reconcilePendingRecoveryCycle("stable_terminal");
    }
    if (this.transaction === transaction) this.transaction = null;
    await this.options.onStableTerminal?.(transaction.requestId);
  }

  private async waitForStableTerminal(
    transaction: SubmissionTransaction,
    signal?: AbortSignal,
  ): Promise<RequestStatusResult> {
    try {
      const result = await this.options.waitForRequestTerminal(transaction.requestId, { signal });
      await this.stableTransaction(transaction, result);
      return result;
    } catch (error) {
      this.markUnresolved(transaction, `Request status query failed: ${errorMessage(error)}`, false);
      throw error;
    }
  }

  private markUnresolved(
    transaction: SubmissionTransaction,
    error: string,
    retrySubmitConfirmedUnknown: boolean,
  ): void {
    if (!this.isTransactionCurrent(transaction)) return;
    this.recoveryError = error;
    this.retrySubmitConfirmedUnknown = !transaction.accepted && retrySubmitConfirmedUnknown;
    this.emit();
  }

  private requireActiveConfig(): Readonly<VisionAutomationServerConfig> {
    if (this.activeConfig === null) {
      throw new Error("Vision automation has no active configuration snapshot");
    }
    return this.activeConfig;
  }

  private beginCaptureAttempt(sourceIndex: number, sourceCount: number): number {
    if (sourceIndex === 0) {
      this.captureAttemptSequence += 1;
      this.activeCaptureAttempt = {
        id: this.captureAttemptSequence,
        remaining: sourceCount,
        failed: false,
      };
      this.stats.attemptedTicks += 1;
      this.emit();
    }
    if (this.activeCaptureAttempt === null) {
      throw new Error("Capture source started outside a coordinator attempt");
    }
    return this.activeCaptureAttempt.id;
  }

  private settleCaptureAttempt(attemptId: number, succeeded: boolean): void {
    const attempt = this.activeCaptureAttempt;
    if (attempt === null || attempt.id !== attemptId || attempt.remaining <= 0) return;
    if (!succeeded) attempt.failed = true;
    attempt.remaining -= 1;
    if (attempt.remaining !== 0) return;
    if (attempt.failed) {
      this.stats.failedTicks += 1;
      this.emit();
    }
    this.activeCaptureAttempt = null;
  }

  private async ensureCleanupRegistration(): Promise<void> {
    const cleanup = this.cleanupTransportState;
    if (cleanup === null || cleanup.registration !== "pending") return;
    if (this.cleanupRegistrationPromise !== null) {
      await this.cleanupRegistrationPromise;
      return;
    }
    if (cleanup.binding.mode !== "agent") {
      cleanup.registration = "not_required";
      cleanup.registrationError = null;
      return;
    }
    if (this.options.registerAgentAbort === undefined) {
      cleanup.registrationError = "Agent cleanup registration client is unavailable";
      this.emit();
      throw new Error(cleanup.registrationError);
    }
    const controller = new AbortController();
    this.cleanupRegistrationController = controller;
    const registration = (async () => {
      try {
        await this.options.registerAgentAbort!(
          this.buildCleanupRegistrationRequest(cleanup),
          controller.signal,
        );
        if (this.cleanupTransportState !== cleanup) return;
        cleanup.registration = "registered";
        cleanup.registrationError = null;
        this.emit();
      } catch (error) {
        if (this.cleanupTransportState === cleanup) {
          cleanup.registration = "pending";
          cleanup.registrationError = `Agent cleanup registration failed: ${errorMessage(error)}`;
          this.emit();
        }
        throw error;
      }
    })();
    this.cleanupRegistrationPromise = registration;
    try {
      await registration;
    } finally {
      if (this.cleanupRegistrationPromise === registration) {
        this.cleanupRegistrationPromise = null;
      }
      if (this.cleanupRegistrationController === controller) {
        this.cleanupRegistrationController = null;
      }
    }
  }

  private buildCleanupRegistrationRequest(
    cleanup: CleanupTransportState,
  ): AgentVisionAutomationAbortRequest {
    return buildAgentVisionAutomationAbortRequest({
      sessionId: cleanup.binding.contextId,
      pageGeneration: cleanup.binding.pageGeneration,
      automationTaskId: cleanup.binding.automationTaskId,
      captureCycleId: cleanup.captureCycleId,
      cleanupToken: cleanup.cleanupToken,
    });
  }

  private reconcileInheritedCleanup(transaction: SubmissionTransaction): void {
    const inheritedCleanup = transaction.inheritedCleanup;
    if (inheritedCleanup === null) return;
    const currentCleanup = this.state.cleanupPending;
    const transport = this.cleanupTransportState;
    if (sameCleanupToken(currentCleanup, inheritedCleanup)
      && transport !== null
      && transport.cleanupAttemptId === inheritedCleanup.cleanupAttemptId
      && sameContext(transport.binding, inheritedCleanup.binding)
      && (transport.binding.mode !== "agent" || transport.registration === "registered")) {
      return;
    }
    if (currentCleanup === null && transport === null) {
      transaction.inheritedCleanup = null;
      return;
    }
    throw new Error("Inherited cleanup registration became stale");
  }

  private failTransaction(transaction: SubmissionTransaction, error: string): void {
    if (!this.isTransactionCurrent(transaction) || transaction.accepted) return;
    this.dispatch({
      type: "submit_failed",
      binding: transaction.binding,
      ...transaction.submission,
      error,
    });
  }

  private isTransactionCurrent(transaction: SubmissionTransaction): boolean {
    return this.transaction === transaction
      && sameBinding(this.state.binding, transaction.binding)
      && this.state.submissionAttemptId === transaction.submission.submissionAttemptId
      && this.state.submissionCycleId === transaction.submission.captureCycleId
      && this.state.submissionCycleGeneration === transaction.submission.cycleGeneration;
  }

  private releaseTransactionPayload(transaction: SubmissionTransaction): void {
    transaction.payload = null;
    transaction.manualDataUrls = [];
    transaction.visionAutomation = null;
    this.refreshCurrentGauges();
  }

  private refreshCurrentGauges(): void {
    const manualDataUrls = this.transaction?.batch !== null
      && this.transaction?.batch !== undefined
      ? this.transaction.manualDataUrls
      : this.options.getManualDataUrls();
    const bundles = this.sampler?.bundles ?? this.transaction?.batch?.bundles ?? [];
    this.stats.imageCount = manualDataUrls.length + bundles.reduce(
      (total, bundle) => total + bundle.frames.length,
      0,
    );
    this.stats.encodedBytes = this.dataUrlBytes(manualDataUrls)
      + bundles.reduce((bundleTotal, bundle) => bundleTotal + bundle.frames.reduce(
      (frameTotal, frame) => {
        const blob = frame.resource.uploadBlob;
        return frameTotal + (blob === null
          ? 0
          : estimateDataUrlBytes(blob.size, blob.type || AUTOMATION_JPEG_MIME));
      },
      0,
    ), 0);
  }

  private dataUrlBytes(dataUrls: readonly string[]): number {
    return dataUrls.reduce(
      (total, dataUrl) => total + new TextEncoder().encode(dataUrl).byteLength,
      0,
    );
  }
}

export function createVisionAutomationController(
  options: VisionAutomationControllerOptions,
): VisionAutomationController {
  return new VisionAutomationController(options);
}

export interface UseVisionAutomationResult {
  state: VisionAutomationState;
  bundles: readonly TickBundle[];
  controller: VisionAutomationController;
  stats: VisionAutomationCumulativeStats;
  activeConfig: Readonly<VisionAutomationServerConfig> | null;
  start(binding: AutomationBinding): Promise<boolean>;
  retrySources(): Promise<boolean>;
  retryFailedSource(failedSource: FailedSourceIdentity): Promise<boolean>;
  setManualImageCount(manualImageCount: number): void;
  send(userText: string): Promise<boolean>;
  retrySubmit(): Promise<boolean>;
  retryStatus(): Promise<RequestStatusResult | null>;
  abort(): Promise<void>;
  stageCleanupSend(
    binding: AutomationBinding,
    requestId: string,
  ): Promise<StagedVisionAutomationCleanupSend | null>;
}

export interface UseVisionAutomationOptions extends VisionAutomationControllerOptions {
  disposeOnUnmount?: boolean;
}

export function useVisionAutomation(
  options: UseVisionAutomationOptions,
): UseVisionAutomationResult {
  const { disposeOnUnmount = true, ...controllerOptions } = options;
  const controllerRef = useRef<VisionAutomationController | null>(null);
  const disposeOnUnmountRef = useRef(disposeOnUnmount);
  disposeOnUnmountRef.current = disposeOnUnmount;
  if (controllerRef.current === null) {
    controllerRef.current = createVisionAutomationController(controllerOptions);
  }
  const controller = controllerRef.current;
  controller.updateOptions(controllerOptions);
  const [state, setState] = useState(() => controller.getState());
  const manualImageCount = controllerOptions.getManualDataUrls().length;

  useEffect(() => controller.subscribe((next) => setState({ ...next })), [controller]);
  useEffect(() => controller.setManualImageCount(manualImageCount), [controller, manualImageCount]);
  useEffect(() => {
    return () => {
      if (disposeOnUnmountRef.current) void controller.dispose();
    };
  }, [controller]);

  return {
    state,
    bundles: controller.getBundles(),
    controller,
    stats: controller.getStats(),
    activeConfig: controller.getActiveConfig(),
    start: (binding) => controller.start(binding),
    retrySources: () => controller.retrySources(),
    retryFailedSource: (failedSource) => controller.retryFailedSource(failedSource),
    setManualImageCount: (manualImageCount) => controller.setManualImageCount(manualImageCount),
    send: (userText) => controller.send(userText),
    retrySubmit: () => controller.retrySubmit(),
    retryStatus: () => controller.retryStatus(),
    abort: () => controller.abort(),
    stageCleanupSend: (binding, requestId) => controller.stageCleanupSend(binding, requestId),
  };
}
