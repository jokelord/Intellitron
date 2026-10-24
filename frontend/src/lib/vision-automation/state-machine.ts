import type {
  AutomationBinding,
  VisionAutomationPhase,
} from "./types.ts";

export type SourceRecoverablePhase =
  | "capturing"
  | "waiting_for_model"
  | "capturing_during_model"
  | "retryable_submit_error";

export interface CleanupPendingToken {
  binding: AutomationBinding;
  cleanupAttemptId: string;
}

export interface CaptureCycleIdentity {
  captureCycleId: string;
  cycleGeneration: number;
}

export interface SubmissionAttemptIdentity extends CaptureCycleIdentity {
  submissionAttemptId: string;
}

export interface VisionAutomationState {
  phase: VisionAutomationPhase;
  armed: boolean;
  binding: AutomationBinding | null;
  captureDuringInference: boolean;
  captureCycleId: string | null;
  cycleGeneration: number;
  sourceAttemptId: string | null;
  submissionAttemptId: string | null;
  submissionCycleId: string | null;
  submissionCycleGeneration: number | null;
  activeRequestId: string | null;
  phaseBeforeSourceError: SourceRecoverablePhase | null;
  pendingSourceError: string | null;
  cleanupPending: CleanupPendingToken | null;
  panelCollapsed: boolean;
  lastError: string | null;
  /** A complete bundle was captured but rejected by the local sampler. */
  bundleRejectionError: string | null;
}

export type VisionAutomationAction =
  | {
      type: "start_requested";
      binding: AutomationBinding;
      captureDuringInference: boolean;
      sourceAttemptId: string;
      cycleActive?: boolean;
    }
  | {
      type: "sources_ready";
      binding: AutomationBinding;
      sourceAttemptId: string;
      captureCycleId: string;
    }
  | {
      type: "sources_unavailable";
      binding: AutomationBinding;
      sourceAttemptId: string;
      error: string;
    }
  | { type: "retry_sources"; binding: AutomationBinding; sourceAttemptId: string }
  | ({
      type: "capture_cycle_failed";
      binding: AutomationBinding;
      sourceAttemptId: string;
      error: string;
    } & CaptureCycleIdentity)
  | ({
      type: "bundle_rejected";
      binding: AutomationBinding;
      sourceAttemptId: string;
      error: string;
    } & CaptureCycleIdentity)
  | ({
      type: "bundle_accepted";
      binding: AutomationBinding;
      sourceAttemptId: string;
    } & CaptureCycleIdentity)
  | ({
      type: "send_requested";
      binding: AutomationBinding;
    } & SubmissionAttemptIdentity)
  | ({
      type: "request_accepted";
      binding: AutomationBinding;
      requestId: string;
      nextCaptureCycleId?: string;
    } & SubmissionAttemptIdentity)
  | ({
      type: "request_progress";
      binding: AutomationBinding;
      requestId: string;
    } & SubmissionAttemptIdentity)
  | ({
      type: "request_superseded";
      binding: AutomationBinding;
      requestId: string;
      replacementRequestId: string;
    } & SubmissionAttemptIdentity)
  | ({
      type: "replacement_rejected";
      binding: AutomationBinding;
      requestId: string;
      originalRequestId: string;
    } & SubmissionAttemptIdentity)
  | ({
      type: "stream_disconnected";
      binding: AutomationBinding;
      requestId: string;
    } & SubmissionAttemptIdentity)
  | ({
      type: "stable_terminal";
      binding: AutomationBinding;
      requestId: string;
      nextCaptureCycleId?: string;
    } & SubmissionAttemptIdentity)
  | ({
      type: "submit_failed";
      binding: AutomationBinding;
      error: string;
    } & SubmissionAttemptIdentity)
  | ({
      type: "submission_rejected";
      binding: AutomationBinding;
      error: string;
      nextCaptureCycleId: string;
    } & SubmissionAttemptIdentity)
  | { type: "retry_submit"; binding: AutomationBinding; submissionAttemptId: string }
  | {
      type: "source_error";
      binding: AutomationBinding;
      sourceAttemptId: string;
      error: string;
    }
  | { type: "retry_failed_source"; binding: AutomationBinding; sourceAttemptId: string }
  | { type: "source_recovered"; binding: AutomationBinding; sourceAttemptId: string }
  | { type: "set_collapsed"; collapsed: boolean }
  | {
      type: "abort_requested";
      binding: AutomationBinding;
      cleanupAttemptId: string;
      preserveCleanup?: boolean;
    }
  | { type: "abort_complete"; binding: AutomationBinding; cleanupAttemptId: string }
  | { type: "cleanup_consumed"; binding: AutomationBinding; cleanupAttemptId: string }
  | { type: "cleanup_cancelled"; binding: AutomationBinding; cleanupAttemptId: string };

export function createInitialVisionAutomationState(): VisionAutomationState {
  return {
    phase: "idle",
    armed: false,
    binding: null,
    captureDuringInference: false,
    captureCycleId: null,
    cycleGeneration: 0,
    sourceAttemptId: null,
    submissionAttemptId: null,
    submissionCycleId: null,
    submissionCycleGeneration: null,
    activeRequestId: null,
    phaseBeforeSourceError: null,
    pendingSourceError: null,
    cleanupPending: null,
    panelCollapsed: false,
    lastError: null,
    bundleRejectionError: null,
  };
}

function isNonEmptyId(value: string): boolean {
  return value.trim().length > 0;
}

function sameBinding(left: AutomationBinding | null, right: AutomationBinding): boolean {
  return left !== null
    && left.pageInstanceId === right.pageInstanceId
    && left.pageGeneration === right.pageGeneration
    && left.mode === right.mode
    && left.contextId === right.contextId
    && left.automationTaskId === right.automationTaskId;
}

function sameContext(left: AutomationBinding | null, right: AutomationBinding): boolean {
  return left !== null
    && left.pageInstanceId === right.pageInstanceId
    && left.pageGeneration === right.pageGeneration
    && left.mode === right.mode
    && left.contextId === right.contextId;
}

function matchesCleanup(
  token: CleanupPendingToken | null,
  binding: AutomationBinding,
  cleanupAttemptId: string,
): boolean {
  return token !== null
    && sameContext(token.binding, binding)
    && token.cleanupAttemptId === cleanupAttemptId;
}

function matchesSubmission(
  state: VisionAutomationState,
  identity: SubmissionAttemptIdentity,
): boolean {
  return state.submissionAttemptId === identity.submissionAttemptId
    && state.submissionCycleId === identity.captureCycleId
    && state.submissionCycleGeneration === identity.cycleGeneration;
}

function matchesRequest(
  state: VisionAutomationState,
  identity: SubmissionAttemptIdentity,
  requestId: string,
): boolean {
  return matchesSubmission(state, identity) && state.activeRequestId === requestId;
}

function pauseForPendingSourceError(
  state: VisionAutomationState,
  targetPhase: SourceRecoverablePhase,
): VisionAutomationState {
  if (state.pendingSourceError === null) return { ...state, phase: targetPhase };
  return {
    ...state,
    phase: "paused_source_error",
    phaseBeforeSourceError: targetPhase,
  };
}

function canReceiveInFlightBundle(phase: VisionAutomationPhase): boolean {
  return phase === "capturing"
    || phase === "freezing"
    || phase === "capturing_during_model";
}

export function reduceVisionAutomation(
  state: VisionAutomationState,
  action: VisionAutomationAction,
): VisionAutomationState {
  if (action.type === "set_collapsed") {
    return state.panelCollapsed === action.collapsed
      ? state
      : { ...state, panelCollapsed: action.collapsed };
  }
  if (action.type === "cleanup_cancelled" || action.type === "cleanup_consumed") {
    if (!matchesCleanup(state.cleanupPending, action.binding, action.cleanupAttemptId)) return state;
    return { ...state, cleanupPending: null };
  }
  if (action.type === "start_requested") {
    if (state.phase !== "idle" || action.cycleActive || !isNonEmptyId(action.sourceAttemptId)) return state;
    return {
      ...state,
      phase: "starting",
      armed: true,
      binding: action.binding,
      captureDuringInference: action.captureDuringInference,
      captureCycleId: null,
      sourceAttemptId: action.sourceAttemptId,
      submissionAttemptId: null,
      submissionCycleId: null,
      submissionCycleGeneration: null,
      activeRequestId: null,
      phaseBeforeSourceError: null,
      pendingSourceError: null,
      cleanupPending: sameContext(state.cleanupPending?.binding ?? null, action.binding)
        ? state.cleanupPending
        : null,
      lastError: null,
      bundleRejectionError: null,
    };
  }
  if (!sameBinding(state.binding, action.binding)) return state;

  switch (action.type) {
    case "sources_ready":
      if (state.phase !== "starting"
        || state.phaseBeforeSourceError !== null
        || action.sourceAttemptId !== state.sourceAttemptId) return state;
      return {
        ...state,
        phase: "capturing",
        captureCycleId: action.captureCycleId,
        cycleGeneration: state.cycleGeneration + 1,
        phaseBeforeSourceError: null,
        lastError: null,
      };
    case "sources_unavailable":
      if (state.phase !== "starting"
        || state.phaseBeforeSourceError !== null
        || action.sourceAttemptId !== state.sourceAttemptId) return state;
      return { ...state, phase: "waiting_for_sources", lastError: action.error };
    case "retry_sources":
      if (state.phase !== "waiting_for_sources"
        || !isNonEmptyId(action.sourceAttemptId)
        || action.sourceAttemptId === state.sourceAttemptId) return state;
      return {
        ...state,
        phase: "starting",
        sourceAttemptId: action.sourceAttemptId,
        lastError: null,
      };
    case "capture_cycle_failed":
      if (state.phase !== "capturing"
        || state.sourceAttemptId !== action.sourceAttemptId
        || state.captureCycleId !== action.captureCycleId
        || state.cycleGeneration !== action.cycleGeneration) return state;
      return {
        ...state,
        phase: "waiting_for_sources",
        captureCycleId: null,
        cycleGeneration: state.cycleGeneration + 1,
        phaseBeforeSourceError: null,
        pendingSourceError: null,
        lastError: action.error,
        bundleRejectionError: null,
      };
    case "bundle_rejected":
      if (!canReceiveInFlightBundle(state.phase)
        || state.sourceAttemptId !== action.sourceAttemptId
        || state.captureCycleId !== action.captureCycleId
        || state.cycleGeneration !== action.cycleGeneration) return state;
      return {
        ...state,
        lastError: action.error,
        bundleRejectionError: action.error,
      };
    case "bundle_accepted":
      if (!canReceiveInFlightBundle(state.phase)
        || state.sourceAttemptId !== action.sourceAttemptId
        || state.captureCycleId !== action.captureCycleId
        || state.cycleGeneration !== action.cycleGeneration
        || state.bundleRejectionError === null) return state;
      return {
        ...state,
        lastError: state.lastError === state.bundleRejectionError ? null : state.lastError,
        bundleRejectionError: null,
      };
    case "send_requested":
      if (state.phase !== "capturing"
        || action.captureCycleId !== state.captureCycleId
        || action.cycleGeneration !== state.cycleGeneration
        || !isNonEmptyId(action.submissionAttemptId)) return state;
      return {
        ...state,
        phase: "freezing",
        submissionAttemptId: action.submissionAttemptId,
        submissionCycleId: action.captureCycleId,
        submissionCycleGeneration: action.cycleGeneration,
        lastError: null,
      };
    case "request_accepted":
      if (state.phase !== "freezing" || !matchesSubmission(state, action)) return state;
      if (state.captureDuringInference) {
        if (!action.nextCaptureCycleId) {
          return pauseForPendingSourceError({
            ...state,
            lastError: "Accepted capture-during-inference request requires a next capture cycle",
          }, "retryable_submit_error");
        }
        return pauseForPendingSourceError({
          ...state,
          captureCycleId: action.nextCaptureCycleId,
          cycleGeneration: state.cycleGeneration + 1,
          activeRequestId: action.requestId,
        }, "capturing_during_model");
      }
      return pauseForPendingSourceError({
        ...state,
        captureCycleId: null,
        activeRequestId: action.requestId,
      }, "waiting_for_model");
    case "request_progress":
    case "stream_disconnected":
      if (!matchesRequest(state, action, action.requestId)) return state;
      return state;
    case "request_superseded":
      if (!matchesRequest(state, action, action.requestId)
        || !isNonEmptyId(action.replacementRequestId)
        || action.replacementRequestId === action.requestId) return state;
      return { ...state, activeRequestId: action.replacementRequestId };
    case "replacement_rejected":
      if (!matchesRequest(state, action, action.requestId)
        || !isNonEmptyId(action.originalRequestId)
        || action.originalRequestId === action.requestId) return state;
      return { ...state, activeRequestId: action.originalRequestId };
    case "stable_terminal": {
      if (!matchesRequest(state, action, action.requestId)) return state;
      const sourceRecoveryPending = state.phase === "paused_source_error" || state.phase === "starting";
      const effectivePhase = sourceRecoveryPending ? state.phaseBeforeSourceError : state.phase;
      if (state.captureDuringInference) {
        if (effectivePhase !== "capturing_during_model") return state;
        return {
          ...state,
          phase: sourceRecoveryPending ? state.phase : "capturing",
          phaseBeforeSourceError: sourceRecoveryPending ? "capturing" : null,
          activeRequestId: null,
          submissionAttemptId: null,
          submissionCycleId: null,
          submissionCycleGeneration: null,
        };
      }
      if (effectivePhase !== "waiting_for_model" || !action.nextCaptureCycleId) return state;
      return {
        ...state,
        phase: sourceRecoveryPending ? state.phase : "capturing",
        phaseBeforeSourceError: sourceRecoveryPending ? "capturing" : null,
        captureCycleId: action.nextCaptureCycleId,
        cycleGeneration: state.cycleGeneration + 1,
        activeRequestId: null,
        submissionAttemptId: null,
        submissionCycleId: null,
        submissionCycleGeneration: null,
      };
    }
    case "submit_failed":
      if (state.phase !== "freezing" || !matchesSubmission(state, action)) return state;
      return pauseForPendingSourceError({ ...state, lastError: action.error }, "retryable_submit_error");
    case "submission_rejected":
      if (state.phase !== "freezing" || !matchesSubmission(state, action)
        || !isNonEmptyId(action.nextCaptureCycleId)) return state;
      return {
        ...state,
        phase: "capturing",
        captureCycleId: action.nextCaptureCycleId,
        cycleGeneration: state.cycleGeneration + 1,
        submissionAttemptId: null,
        submissionCycleId: null,
        submissionCycleGeneration: null,
        activeRequestId: null,
        lastError: action.error,
      };
    case "retry_submit":
      if (state.phase !== "retryable_submit_error"
        || !isNonEmptyId(action.submissionAttemptId)
        || action.submissionAttemptId === state.submissionAttemptId) return state;
      return {
        ...state,
        phase: "freezing",
        submissionAttemptId: action.submissionAttemptId,
        lastError: null,
      };
    case "source_error": {
      if (action.sourceAttemptId !== state.sourceAttemptId) return state;
      if (state.phase === "freezing") {
        return { ...state, pendingSourceError: action.error };
      }
      const recoverablePhase = state.phase === "capturing"
        || state.phase === "waiting_for_model"
        || state.phase === "capturing_during_model"
        || state.phase === "retryable_submit_error"
        ? state.phase
        : null;
      if (recoverablePhase === null) return state;
      return {
        ...state,
        phase: "paused_source_error",
        phaseBeforeSourceError: recoverablePhase,
        pendingSourceError: action.error,
        lastError: recoverablePhase === "retryable_submit_error" ? state.lastError : action.error,
      };
    }
    case "retry_failed_source":
      if (state.phase !== "paused_source_error"
        || !isNonEmptyId(action.sourceAttemptId)
        || action.sourceAttemptId === state.sourceAttemptId) return state;
      return {
        ...state,
        phase: "starting",
        sourceAttemptId: action.sourceAttemptId,
        lastError: state.phaseBeforeSourceError === "retryable_submit_error" ? state.lastError : null,
      };
    case "source_recovered":
      if (state.phase !== "starting"
        || action.sourceAttemptId !== state.sourceAttemptId
        || state.phaseBeforeSourceError === null) return state;
      return {
        ...state,
        phase: state.phaseBeforeSourceError,
        phaseBeforeSourceError: null,
        pendingSourceError: null,
        lastError: state.phaseBeforeSourceError === "retryable_submit_error" ? state.lastError : null,
      };
    case "abort_requested":
      if (!state.armed || state.phase === "aborting" || !isNonEmptyId(action.cleanupAttemptId)) return state;
      return {
        ...state,
        phase: "aborting",
        armed: false,
        captureCycleId: null,
        cycleGeneration: state.cycleGeneration + 1,
        sourceAttemptId: null,
        submissionAttemptId: null,
        submissionCycleId: null,
        submissionCycleGeneration: null,
        activeRequestId: null,
        phaseBeforeSourceError: null,
        pendingSourceError: null,
        cleanupPending: action.preserveCleanup === false
          ? null
          : {
              binding: state.binding!,
              cleanupAttemptId: action.cleanupAttemptId,
            },
      };
    case "abort_complete":
      if (state.phase !== "aborting") return state;
      if (state.cleanupPending !== null
        && !matchesCleanup(state.cleanupPending, action.binding, action.cleanupAttemptId)) return state;
      return {
        ...createInitialVisionAutomationState(),
        cycleGeneration: state.cycleGeneration,
        cleanupPending: state.cleanupPending,
        panelCollapsed: state.panelCollapsed,
      };
    default:
      return state;
  }
}
