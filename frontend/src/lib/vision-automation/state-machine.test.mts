import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createInitialVisionAutomationState,
  reduceVisionAutomation,
  type VisionAutomationAction,
  type VisionAutomationState,
} from "./state-machine.ts";
import type { AutomationBinding } from "./types.ts";

const binding: AutomationBinding = {
  pageInstanceId: "page-1",
  pageGeneration: 3,
  mode: "chat",
  contextId: "conversation-4",
  automationTaskId: "task-5",
};

function reduce(actions: VisionAutomationAction[], initial = createInitialVisionAutomationState()) {
  return actions.reduce(reduceVisionAutomation, initial);
}

function capturing(captureDuringInference = false): VisionAutomationState {
  return reduce([
    {
      type: "start_requested",
      binding,
      captureDuringInference,
      sourceAttemptId: "source-1",
    },
    {
      type: "sources_ready",
      binding,
      sourceAttemptId: "source-1",
      captureCycleId: "cycle-1",
    },
  ]);
}

function accepted(captureDuringInference: boolean): VisionAutomationState {
  return reduce([
    {
      type: "send_requested",
      binding,
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      submissionAttemptId: "submission-1",
    },
    {
      type: "request_accepted",
      binding,
      submissionAttemptId: "submission-1",
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      requestId: "request-1",
      nextCaptureCycleId: captureDuringInference ? "cycle-2" : undefined,
    },
  ], capturing(captureDuringInference));
}

test("start pins the source attempt and sources-ready creates immediate T=0", () => {
  const state = capturing();
  assert.equal(state.phase, "capturing");
  assert.equal(state.armed, true);
  assert.equal(state.sourceAttemptId, "source-1");
  assert.equal(state.captureCycleId, "cycle-1");
  assert.equal(state.cycleGeneration, 1);
  assert.deepEqual(state.binding, binding);
});

test("a stale source retry callback cannot establish the immutable cohort", () => {
  let state = reduce([
    {
      type: "start_requested",
      binding,
      captureDuringInference: false,
      sourceAttemptId: "source-1",
    },
    {
      type: "sources_unavailable",
      binding,
      sourceAttemptId: "source-1",
      error: "permission denied",
    },
    { type: "retry_sources", binding, sourceAttemptId: "source-2" },
  ]);
  assert.equal(state.phase, "starting");
  assert.equal(state.sourceAttemptId, "source-2");

  const stale = reduceVisionAutomation(state, {
    type: "sources_ready",
    binding,
    sourceAttemptId: "source-1",
    captureCycleId: "stale-cycle",
  });
  assert.strictEqual(stale, state);
  state = reduceVisionAutomation(state, {
    type: "sources_ready",
    binding,
    sourceAttemptId: "source-2",
    captureCycleId: "cycle-1",
  });
  assert.equal(state.phase, "capturing");
});

test("send validates cycle identity and stores the submission attempt identity", () => {
  const initial = capturing();
  for (const stale of [
    { captureCycleId: "old-cycle", cycleGeneration: 1 },
    { captureCycleId: "cycle-1", cycleGeneration: 0 },
  ]) {
    assert.strictEqual(reduceVisionAutomation(initial, {
      type: "send_requested",
      binding,
      ...stale,
      submissionAttemptId: "submission-stale",
    }), initial);
  }
  const freezing = reduceVisionAutomation(initial, {
    type: "send_requested",
    binding,
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
    submissionAttemptId: "submission-1",
  });
  assert.equal(freezing.phase, "freezing");
  assert.equal(freezing.submissionAttemptId, "submission-1");
  assert.equal(freezing.submissionCycleId, "cycle-1");
  assert.equal(freezing.submissionCycleGeneration, 1);
});

test("a stale submission callback cannot accept or fail a newer retry", () => {
  let state = reduce([
    {
      type: "send_requested",
      binding,
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      submissionAttemptId: "submission-1",
    },
    {
      type: "submit_failed",
      binding,
      submissionAttemptId: "submission-1",
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      error: "transport",
    },
    { type: "retry_submit", binding, submissionAttemptId: "submission-2" },
  ], capturing());
  assert.equal(state.phase, "freezing");

  for (const action of [
    {
      type: "request_accepted" as const,
      binding,
      submissionAttemptId: "submission-1",
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      requestId: "stale-request",
    },
    {
      type: "submit_failed" as const,
      binding,
      submissionAttemptId: "submission-1",
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      error: "late failure",
    },
  ]) {
    assert.strictEqual(reduceVisionAutomation(state, action), state);
  }
  assert.strictEqual(reduceVisionAutomation(state, {
    type: "request_accepted",
    binding,
    submissionAttemptId: "submission-2",
    captureCycleId: "stale-cycle",
    cycleGeneration: 1,
    requestId: "request-wrong-cycle",
  }), state);
  state = reduceVisionAutomation(state, {
    type: "request_accepted",
    binding,
    submissionAttemptId: "submission-2",
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
    requestId: "request-2",
  });
  assert.equal(state.phase, "waiting_for_model");
  assert.equal(state.activeRequestId, "request-2");
});

test("capture during inference creates one next cycle and terminal cannot reset it", () => {
  let state = accepted(true);
  assert.equal(state.phase, "capturing_during_model");
  assert.equal(state.captureCycleId, "cycle-2");
  assert.equal(state.cycleGeneration, 2);
  assert.strictEqual(reduceVisionAutomation(state, {
    type: "send_requested",
    binding,
    submissionAttemptId: "submission-2",
    captureCycleId: "cycle-2",
    cycleGeneration: 2,
  }), state);
  state = reduceVisionAutomation(state, {
    type: "stable_terminal",
    binding,
    submissionAttemptId: "submission-1",
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
    requestId: "request-1",
    nextCaptureCycleId: "must-not-be-used",
  });
  assert.equal(state.phase, "capturing");
  assert.equal(state.captureCycleId, "cycle-2");
  assert.equal(state.cycleGeneration, 2);
});

test("capture disabled ignores progress, disconnect, and stale terminal until stable current terminal", () => {
  let state = accepted(false);
  assert.equal(state.phase, "waiting_for_model");
  assert.equal(state.captureCycleId, null);
  for (const action of [
    {
      type: "request_progress" as const,
      binding,
      submissionAttemptId: "submission-1",
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      requestId: "request-1",
    },
    {
      type: "stream_disconnected" as const,
      binding,
      submissionAttemptId: "submission-1",
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      requestId: "request-1",
    },
    {
      type: "stable_terminal" as const,
      binding,
      submissionAttemptId: "submission-stale",
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      requestId: "request-1",
      nextCaptureCycleId: "stale-cycle",
    },
  ]) {
    assert.strictEqual(reduceVisionAutomation(state, action), state);
  }
  state = reduceVisionAutomation(state, {
    type: "stable_terminal",
    binding,
    submissionAttemptId: "submission-1",
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
    requestId: "request-1",
    nextCaptureCycleId: "cycle-2",
  });
  assert.equal(state.phase, "capturing");
  assert.equal(state.captureCycleId, "cycle-2");
  assert.equal(state.cycleGeneration, 2);
});

for (const phase of ["capturing", "waiting_for_model", "capturing_during_model"] as const) {
  test(`source recovery restores the exact ${phase} phase and rejects stale retry callbacks`, () => {
    let state = phase === "capturing"
      ? capturing()
      : accepted(phase === "capturing_during_model");
    const cycleBeforeError = state.captureCycleId;
    const generationBeforeError = state.cycleGeneration;
    const requestBeforeError = state.activeRequestId;
    state = reduceVisionAutomation(state, {
      type: "source_error",
      binding,
      sourceAttemptId: "source-1",
      error: "track ended",
    });
    assert.equal(state.phase, "paused_source_error");
    assert.equal(state.phaseBeforeSourceError, phase);
    state = reduceVisionAutomation(state, {
      type: "retry_failed_source",
      binding,
      sourceAttemptId: "source-2",
    });
    assert.equal(state.phase, "starting");
    assert.strictEqual(reduceVisionAutomation(state, {
      type: "sources_ready",
      binding,
      sourceAttemptId: "source-2",
      captureCycleId: "wrong-restart-cycle",
    }), state);
    const stale = reduceVisionAutomation(state, {
      type: "source_recovered",
      binding,
      sourceAttemptId: "source-1",
    });
    assert.strictEqual(stale, state);
    state = reduceVisionAutomation(state, {
      type: "source_recovered",
      binding,
      sourceAttemptId: "source-2",
    });

    assert.equal(state.phase, phase);
    assert.equal(state.phaseBeforeSourceError, null);
    assert.equal(state.captureCycleId, cycleBeforeError);
    assert.equal(state.cycleGeneration, generationBeforeError);
    assert.equal(state.activeRequestId, requestBeforeError);
  });
}

test("stable terminal during source recovery updates the restore target without restarting early", () => {
  let state = accepted(false);
  state = reduceVisionAutomation(state, {
    type: "source_error",
    binding,
    sourceAttemptId: "source-1",
    error: "track ended",
  });
  state = reduceVisionAutomation(state, {
    type: "retry_failed_source",
    binding,
    sourceAttemptId: "source-2",
  });
  state = reduceVisionAutomation(state, {
    type: "stable_terminal",
    binding,
    submissionAttemptId: "submission-1",
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
    requestId: "request-1",
    nextCaptureCycleId: "cycle-2",
  });
  assert.equal(state.phase, "starting");
  assert.equal(state.phaseBeforeSourceError, "capturing");
  state = reduceVisionAutomation(state, {
    type: "source_recovered",
    binding,
    sourceAttemptId: "source-2",
  });
  assert.equal(state.phase, "capturing");
  assert.equal(state.captureCycleId, "cycle-2");
});

test("source error during freezing is deferred and pauses before capture-during-model can restart", () => {
  let state = reduceVisionAutomation(capturing(true), {
    type: "send_requested",
    binding,
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
    submissionAttemptId: "submission-1",
  });
  state = reduceVisionAutomation(state, {
    type: "source_error",
    binding,
    sourceAttemptId: "source-1",
    error: "track ended during freeze",
  });
  assert.equal(state.phase, "freezing");
  assert.equal(state.pendingSourceError, "track ended during freeze");

  state = reduceVisionAutomation(state, {
    type: "request_accepted",
    binding,
    submissionAttemptId: "submission-1",
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
    requestId: "request-1",
    nextCaptureCycleId: "cycle-2",
  });
  assert.equal(state.phase, "paused_source_error");
  assert.equal(state.phaseBeforeSourceError, "capturing_during_model");
  assert.equal(state.captureCycleId, "cycle-2");
  assert.strictEqual(reduceVisionAutomation(state, {
    type: "send_requested",
    binding,
    submissionAttemptId: "submission-2",
    captureCycleId: "cycle-2",
    cycleGeneration: 2,
  }), state);
});

test("source error in retryable submit state pauses and restores retryability before another attempt", () => {
  let state = reduce([
    {
      type: "send_requested",
      binding,
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      submissionAttemptId: "submission-1",
    },
    {
      type: "submit_failed",
      binding,
      submissionAttemptId: "submission-1",
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      error: "transport failed",
    },
  ], capturing());
  assert.equal(state.phase, "retryable_submit_error");
  state = reduceVisionAutomation(state, {
    type: "source_error",
    binding,
    sourceAttemptId: "source-1",
    error: "camera disconnected",
  });
  assert.equal(state.phase, "paused_source_error");
  assert.equal(state.phaseBeforeSourceError, "retryable_submit_error");
  assert.equal(state.pendingSourceError, "camera disconnected");
  state = reduceVisionAutomation(state, {
    type: "retry_failed_source",
    binding,
    sourceAttemptId: "source-2",
  });
  state = reduceVisionAutomation(state, {
    type: "source_recovered",
    binding,
    sourceAttemptId: "source-2",
  });
  assert.equal(state.phase, "retryable_submit_error");
  assert.equal(state.lastError, "transport failed");
  assert.equal(state.pendingSourceError, null);
  state = reduceVisionAutomation(state, {
    type: "retry_submit",
    binding,
    submissionAttemptId: "submission-2",
  });
  assert.equal(state.phase, "freezing");
});

test("collapse changes presentation only and Cycle conflicts cannot arm", () => {
  const initial = createInitialVisionAutomationState();
  assert.strictEqual(reduceVisionAutomation(initial, {
    type: "start_requested",
    binding,
    captureDuringInference: false,
    sourceAttemptId: "source-1",
    cycleActive: true,
  }), initial);
  const state = capturing();
  const collapsed = reduceVisionAutomation(state, { type: "set_collapsed", collapsed: true });
  assert.equal(collapsed.panelCollapsed, true);
  assert.equal(collapsed.phase, "capturing");
  assert.equal(collapsed.captureCycleId, "cycle-1");
});

test("Abort invalidates work and cleanup completion, cancellation, and consumption require its token", () => {
  let state = reduceVisionAutomation(capturing(), {
    type: "abort_requested",
    binding,
    cleanupAttemptId: "cleanup-1",
  });
  assert.equal(state.phase, "aborting");
  assert.equal(state.armed, false);
  assert.equal(state.sourceAttemptId, null);
  assert.equal(state.submissionAttemptId, null);
  assert.equal(state.captureCycleId, null);
  assert.equal(state.cycleGeneration, 2);
  assert.deepEqual(state.cleanupPending, { binding, cleanupAttemptId: "cleanup-1" });

  assert.strictEqual(reduceVisionAutomation(state, {
    type: "abort_complete",
    binding,
    cleanupAttemptId: "cleanup-old",
  }), state);
  state = reduceVisionAutomation(state, {
    type: "abort_complete",
    binding,
    cleanupAttemptId: "cleanup-1",
  });
  assert.equal(state.phase, "idle");
  for (const type of ["cleanup_cancelled", "cleanup_consumed"] as const) {
    assert.strictEqual(reduceVisionAutomation(state, {
      type,
      binding,
      cleanupAttemptId: "cleanup-old",
    }), state);
  }
  state = reduceVisionAutomation(state, {
    type: "cleanup_cancelled",
    binding,
    cleanupAttemptId: "cleanup-1",
  });
  assert.equal(state.cleanupPending, null);
});

test("bundle rejection is observable without becoming a source failure and clears after the next accepted bundle", () => {
  let state = capturing();
  state = reduceVisionAutomation(state, {
    type: "bundle_rejected",
    binding,
    sourceAttemptId: "source-1",
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
    error: "sampler rejected complete bundle",
  });
  assert.equal(state.phase, "capturing");
  assert.equal(state.pendingSourceError, null);
  assert.match(state.lastError ?? "", /sampler rejected complete bundle/);

  state = reduceVisionAutomation(state, {
    type: "bundle_accepted",
    binding,
    sourceAttemptId: "source-1",
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
  });
  assert.equal(state.lastError, null);
});

test("bundle diagnostics retain exact in-flight capture identity while freezing or capturing during inference", () => {
  const phases = [
    reduceVisionAutomation(capturing(), {
      type: "send_requested",
      binding,
      captureCycleId: "cycle-1",
      cycleGeneration: 1,
      submissionAttemptId: "submission-1",
    }),
    accepted(true),
  ];

  for (const initial of phases) {
    const sourceAttemptId = initial.sourceAttemptId!;
    const captureCycleId = initial.captureCycleId!;
    const cycleGeneration = initial.cycleGeneration;
    let state = reduceVisionAutomation(initial, {
      type: "bundle_rejected",
      binding,
      sourceAttemptId,
      captureCycleId,
      cycleGeneration,
      error: `rejected while ${initial.phase}`,
    });
    assert.equal(state.phase, initial.phase);
    assert.equal(state.submissionAttemptId, initial.submissionAttemptId);
    assert.equal(state.pendingSourceError, null);
    assert.match(state.lastError ?? "", /rejected while/);

    state = reduceVisionAutomation(state, {
      type: "bundle_accepted",
      binding,
      sourceAttemptId,
      captureCycleId,
      cycleGeneration,
    });
    assert.equal(state.phase, initial.phase);
    assert.equal(state.submissionAttemptId, initial.submissionAttemptId);
    assert.equal(state.pendingSourceError, null);
    assert.equal(state.lastError, null);
  }
});

test("a later submission error survives the next bundle acceptance while its older bundle diagnostic clears", () => {
  let state = reduceVisionAutomation(capturing(), {
    type: "send_requested",
    binding,
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
    submissionAttemptId: "submission-1",
  });
  state = reduceVisionAutomation(state, {
    type: "bundle_rejected",
    binding,
    sourceAttemptId: "source-1",
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
    error: "sampler rejected complete bundle",
  });
  state = reduceVisionAutomation(state, {
    type: "submission_rejected",
    binding,
    submissionAttemptId: "submission-1",
    captureCycleId: "cycle-1",
    cycleGeneration: 1,
    nextCaptureCycleId: "cycle-2",
    error: "HTTP 422",
  });
  assert.equal(state.phase, "capturing");
  assert.equal(state.captureCycleId, "cycle-2");
  assert.equal(state.lastError, "HTTP 422");
  assert.equal(state.bundleRejectionError, "sampler rejected complete bundle");

  state = reduceVisionAutomation(state, {
    type: "bundle_accepted",
    binding,
    sourceAttemptId: "source-1",
    captureCycleId: "cycle-2",
    cycleGeneration: 2,
  });
  assert.equal(state.bundleRejectionError, null);
  assert.equal(state.lastError, "HTTP 422");
});

test("callbacks from another page generation, mode, context, or sampling task are ignored", () => {
  const state = reduceVisionAutomation(createInitialVisionAutomationState(), {
    type: "start_requested",
    binding,
    captureDuringInference: false,
    sourceAttemptId: "source-1",
  });
  for (const changed of [
    { ...binding, pageGeneration: 4 },
    { ...binding, mode: "agent" as const },
    { ...binding, contextId: "other" },
    { ...binding, automationTaskId: "other" },
  ]) {
    assert.strictEqual(reduceVisionAutomation(state, {
      type: "sources_ready",
      binding: changed,
      sourceAttemptId: "source-1",
      captureCycleId: "stale",
    }), state);
  }
});
