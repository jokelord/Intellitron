import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

import React from "react";

import type { Message } from "../message-types.ts";
import type { CaptureSource } from "./capture-source.ts";
import type { VisionAutomationServerConfig } from "./config.ts";
import { OwnedImageResource } from "./image-resources.ts";
import { estimateDataUrlBytes } from "./payload.ts";
import * as pageIntegration from "./page-integration.ts";
import type { AutomationBinding, SourceDescriptor } from "./types.ts";
import {
  createVisionAutomationController,
  shouldStageVisionAutomationHistoryOmission,
  useVisionAutomation,
  type VisionAutomationControllerOptions,
} from "./use-vision-automation.ts";

const require = createRequire(import.meta.url);
const { createRenderer } = require("./react-component-renderer.cjs") as {
  createRenderer(initialElement: React.ReactElement): {
    flush(): void;
    update(element: React.ReactElement): void;
    unmount(): void;
  };
};

const binding: AutomationBinding = {
  pageInstanceId: "page-1",
  pageGeneration: 1,
  mode: "chat",
  contextId: "chat-1",
  automationTaskId: "task-00000000001",
};

const descriptor: SourceDescriptor = {
  sourceId: "camera-device-1",
  kind: "camera",
  stableAlias: "camera_1",
  label: "Camera 1",
  deviceId: "device-1",
};

const config: VisionAutomationServerConfig = {
  enabled: true,
  intervalSeconds: 60,
  maxFramesPerSource: 2,
  thumbnailSizePx: 32,
  captureDuringInference: false,
  lockFirstBundle: false,
  tickTimeoutSeconds: 1,
  consecutiveTickFailureLimit: 3,
  maxEncodedPayloadBytes: 1024 * 1024,
  initialJpegQuality: 85,
  minimumJpegQuality: 40,
  minimumImageDimensionPx: 256,
  maxImageDimensionPx: 1024,
  base64EncodeConcurrency: 1,
  maxRequestBodyBytes: 75_497_472,
  requestStatusTtlSeconds: 600,
};

function historyMessage(): Message {
  return {
    id: "history-1",
    role: "user",
    content: [
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,old" } },
      { type: "text", text: "old question" },
    ],
    images: ["data:image/jpeg;base64,old"],
    timestamp: new Date(0),
  };
}

function source(disposals: { count: number }): CaptureSource {
  let disposed = false;
  return {
    descriptor,
    async capture(signal) {
      if (signal.aborted) throw signal.reason;
      return {
        source: descriptor,
        resource: new OwnedImageResource(new Blob(["jpeg"], { type: "image/jpeg" })),
        capturedAtMs: 1,
      };
    },
    onEnded() {
      return () => {};
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      disposals.count += 1;
    },
  };
}

async function waitFor(condition: () => boolean, timeoutMs = 1_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() >= deadline) throw new Error("condition timed out");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}

function createOptions(
  overrides: Partial<VisionAutomationControllerOptions> = {},
): VisionAutomationControllerOptions {
  let messages = [historyMessage()];
  let sequence = 0;
  return {
    config,
    acquireSources: async () => [source({ count: 0 })],
    restoreSource: async ({ source: failedSource }) => {
      const replacement = source({ count: 0 });
      return { ...replacement, descriptor: failedSource };
    },
    getManualDataUrls: () => [],
    getMessages: () => messages,
    commitMessages: (next) => { messages = next; },
    isModelBusy: () => false,
    encodeFrame: async ({ blob }) => blob,
    blobToDataUrl: async () => "data:image/jpeg;base64,anBlZw==",
    getRequestStatus: async (requestId) => ({ requestId, status: "terminal" }),
    waitForRequestTerminal: async (requestId) => ({ requestId, status: "terminal" }),
    submit: async ({ lifecycle, requestId }) => {
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
      await lifecycle.onStableTerminal({ requestId, status: "terminal" });
    },
    createId: (kind) => `${kind}-${String(++sequence).padStart(16, "0")}`,
    ...overrides,
  };
}

async function mountUnacceptedSubmitHook(disposeOnUnmount: boolean): Promise<{
  renderer: ReturnType<typeof createRenderer>;
  controller: ReturnType<typeof createVisionAutomationController>;
  requestSignal: AbortSignal;
  send: Promise<boolean>;
  disposeCalls: () => number;
  updateOwnership(disposeOnUnmount: boolean): void;
}> {
  const started = deferred<void>();
  let requestSignal: AbortSignal | undefined;
  const options = createOptions({
    submit: async ({ signal }) => {
      requestSignal = signal;
      started.resolve();
      await new Promise<void>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    },
  });
  let automation: ReturnType<typeof useVisionAutomation> | null = null;
  function Harness({ disposeOnUnmount }: { disposeOnUnmount: boolean }) {
    automation = useVisionAutomation({
      ...options,
      disposeOnUnmount,
    } as VisionAutomationControllerOptions & { disposeOnUnmount?: boolean });
    return null;
  }

  const renderer = createRenderer(React.createElement(Harness, { disposeOnUnmount }));
  renderer.flush();
  assert.ok(automation);
  const controller = automation.controller;
  let calls = 0;
  const dispose = controller.dispose.bind(controller);
  controller.dispose = async () => {
    calls += 1;
    await dispose();
  };
  await controller.start(binding);
  const send = controller.send("unaccepted request");
  await started.promise;
  assert.ok(requestSignal);
  return {
    renderer,
    controller,
    requestSignal,
    send,
    disposeCalls: () => calls,
    updateOwnership: (nextDisposeOnUnmount) => {
      renderer.update(React.createElement(Harness, { disposeOnUnmount: nextDisposeOnUnmount }));
    },
  };
}

test("external lifecycle hook ownership preserves an armed unaccepted request until page-owned ordinary disposal", async () => {
  const mounted = await mountUnacceptedSubmitHook(false);

  mounted.renderer.unmount();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(mounted.requestSignal.aborted, false);
  assert.equal(mounted.disposeCalls(), 0);

  await mounted.controller.dispose();
  assert.equal(mounted.requestSignal.aborted, true);
  assert.equal(mounted.disposeCalls(), 1);
  assert.equal(await mounted.send, false);
});

test("default hook ownership disposes an armed unaccepted request on React unmount", async () => {
  const mounted = await mountUnacceptedSubmitHook(true);

  mounted.renderer.unmount();
  await waitFor(() => mounted.requestSignal.aborted);
  assert.equal(mounted.disposeCalls(), 1);
  assert.equal(await mounted.send, false);
});

test("internal-to-external disposal ownership transfer waits for the external owner", async () => {
  const mounted = await mountUnacceptedSubmitHook(true);

  mounted.updateOwnership(false);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(mounted.requestSignal.aborted, false);
  assert.equal(mounted.disposeCalls(), 0);

  mounted.renderer.unmount();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(mounted.requestSignal.aborted, false);
  assert.equal(mounted.disposeCalls(), 0);

  await mounted.controller.dispose();
  assert.equal(mounted.requestSignal.aborted, true);
  assert.equal(mounted.disposeCalls(), 1);
  assert.equal(await mounted.send, false);
});

test("accepted submission commits omission once and waits for stable terminal before recapturing", async () => {
  let commits = 0;
  let messages = [historyMessage()];
  const submitted: Array<Record<string, unknown>> = [];
  const options = createOptions({
    getMessages: () => messages,
    commitMessages: (next) => {
      commits += 1;
      messages = next;
    },
    submit: async (input) => {
      submitted.push(input as unknown as Record<string, unknown>);
      await input.lifecycle.onRequestAccepted({ type: "request_accepted", requestId: input.requestId });
      await input.lifecycle.onRequestAccepted({ type: "request_accepted", requestId: input.requestId });
      assert.equal(input.getState().phase, "waiting_for_model");
      await input.lifecycle.onStableTerminal({ requestId: input.requestId, status: "terminal" });
    },
  });
  const controller = createVisionAutomationController(options);

  await controller.start(binding);
  assert.equal(controller.getState().phase, "capturing");
  assert.equal(await controller.send("new question"), true);

  assert.equal(commits, 1);
  assert.match(JSON.stringify(messages[0].content), /historical images omitted/);
  assert.equal(controller.getState().phase, "capturing");
  assert.equal(submitted.length, 1);
  assert.equal(submitted[0].userText, "new question");
  assert.doesNotMatch(
    JSON.stringify((submitted[0].payload as { text: string }).text),
    /page_generation|top_level_context_id|automation_task_id|capture_cycle_id/,
  );
  assert.deepEqual(submitted[0].visionAutomation, {
    history_image_policy: "aggregate_prior_image_blocks",
    manual_image_count: 0,
    source_count: 1,
    retained_tick_count: 1,
    binding: {
      page_generation: 1,
      mode: "chat",
      top_level_context_id: "chat-1",
      automation_task_id: "task-00000000001",
      capture_cycle_id: "cycle-0000000000000002",
    },
  });
  assert.equal((submitted[0].stagedMessages as Message[]).length, 1);
  await controller.dispose();
});

test("manual-only and cleanup automation metadata cannot stage first historical omission", () => {
  assert.equal(shouldStageVisionAutomationHistoryOmission({
    sourceCount: 0,
    retainedTickCount: 0,
    sticky: false,
  }), false);
  assert.equal(shouldStageVisionAutomationHistoryOmission({
    sourceCount: 1,
    retainedTickCount: 0,
    sticky: false,
  }), false);
  assert.equal(shouldStageVisionAutomationHistoryOmission({
    sourceCount: 1,
    retainedTickCount: 1,
    sticky: false,
  }), true);
  assert.equal(shouldStageVisionAutomationHistoryOmission({
    sourceCount: 0,
    retainedTickCount: 0,
    sticky: true,
  }), true);
});

test("a summarized foreign page context cannot make a new context sticky", async () => {
  const foreignSummary: Message = {
    id: "foreign-summary",
    role: "user",
    content: "[historical images omitted: count=1; sources=manual; relative_range=unavailable]",
    historicalImageSummaries: [{
      count: 1,
      sources: ["manual"],
      placeholder: "[historical images omitted: count=1; sources=manual; relative_range=unavailable]",
    }],
    timestamp: new Date(0),
  };
  const rawCurrent = {
    ...historyMessage(),
    id: "current-raw",
    content: [{ type: "image_url", image_url: { url: "data:image/jpeg;base64,current" } }, { type: "text", text: "current" }],
    images: ["data:image/jpeg;base64,current"],
  } satisfies Message;
  const currentBinding: AutomationBinding = {
    ...binding,
    pageInstanceId: "page-2",
    pageGeneration: 2,
    mode: "agent",
    contextId: "agent-current",
    automationTaskId: "task-00000000002",
  };
  let messages = [foreignSummary, rawCurrent];
  const controller = createVisionAutomationController(createOptions({
    getMessages: () => messages,
    commitMessages: (next) => { messages = next; },
  }));
  const stage = (controller as unknown as {
    stageHistoricalImageOmission(
      value: AutomationBinding,
      sourceCount: number,
      retainedTickCount: number,
    ): { omittedBlockCount: number; discard(): void };
  }).stageHistoricalImageOmission(currentBinding, 0, 0);
  assert.equal(stage.omittedBlockCount, 0);
  stage.discard();

  await controller.start(currentBinding);
  assert.equal(await controller.send("qualifying current automation"), true);
  assert.match(JSON.stringify(messages.find((message) => message.id === "current-raw")?.content), /historical images omitted/);
  await controller.dispose();
});

test("ambiguous EOF retries only after unknown status and reuses the request ID", async () => {
  const requestIds: string[] = [];
  let attempts = 0;
  const controller = createVisionAutomationController(createOptions({
    getRequestStatus: async (requestId) => ({ requestId, status: "unknown" }),
    submit: async (input) => {
      attempts += 1;
      requestIds.push(input.requestId);
      if (attempts === 1) {
        await input.lifecycle.onTransportEof();
      } else {
        await input.lifecycle.onRequestAccepted({ type: "request_accepted", requestId: input.requestId });
      }
    },
  }));

  await controller.start(binding);
  assert.equal(await controller.send("question"), false);
  assert.equal(controller.getState().phase, "retryable_submit_error");
  assert.equal(await controller.retrySubmit(), true);
  assert.deepEqual(requestIds, [requestIds[0], requestIds[0]]);
  await controller.dispose();
});

test("known busy rejection preserves the frozen request for Retry Submit without querying lifecycle status", async () => {
  const requestIds: string[] = [];
  let statusQueries = 0;
  let busy = false;
  let attempts = 0;
  let firstLifecycle: Parameters<NonNullable<VisionAutomationControllerOptions["submit"]>>[0]["lifecycle"] | null = null;
  const controller = createVisionAutomationController(createOptions({
    isModelBusy: () => busy,
    getRequestStatus: async (requestId) => {
      statusQueries += 1;
      return { requestId, status: "unknown" };
    },
    submit: async (input) => {
      attempts += 1;
      requestIds.push(input.requestId);
      if (attempts === 1) {
        firstLifecycle = input.lifecycle;
        await input.lifecycle.onKnownRejection({
          status: 409,
          detail: "Session busy: active run is still executing",
          message: "Agent request rejected (409): Session busy: active run is still executing",
          retryable: true,
        });
        return;
      }
      await firstLifecycle?.onKnownRejection({
        status: 409,
        detail: "stale busy rejection",
        message: "Agent request rejected (409): stale busy rejection",
        retryable: true,
      });
      assert.equal(input.getState().phase, "freezing");
      await input.lifecycle.onRequestAccepted({ type: "request_accepted", requestId: input.requestId });
    },
  }));

  await controller.start(binding);
  assert.equal(await controller.send("wait for recovered manager"), false);
  assert.equal(controller.getState().phase, "retryable_submit_error");
  assert.equal(controller.getState().lastError, "Agent request rejected (409): Session busy: active run is still executing");
  assert.equal(controller.canRetrySubmit(), true);
  assert.equal(controller.canRetryStatus(), false);
  assert.equal(statusQueries, 0);

  busy = true;
  await assert.rejects(controller.retrySubmit(), /model is busy/);
  busy = false;
  assert.equal(await controller.retrySubmit(), true);
  assert.deepEqual(requestIds, [requestIds[0], requestIds[0]]);
  assert.equal(statusQueries, 0);
});

test("known nonretryable rejection discards staged omission without mutating history and resumes capture", async () => {
  let messages = [historyMessage()];
  let commits = 0;
  const controller = createVisionAutomationController(createOptions({
    getMessages: () => messages,
    commitMessages: (next) => { commits += 1; messages = next; },
    submit: async (input) => {
      await input.lifecycle.onKnownRejection({
        status: 422,
        detail: "Vision automation metadata is invalid",
        message: "Agent request rejected (422): Vision automation metadata is invalid",
        retryable: false,
      });
    },
  }));

  await controller.start(binding);
  assert.equal(await controller.send("invalid request"), false);
  assert.equal(commits, 0);
  assert.equal(messages[0]?.images?.[0], "data:image/jpeg;base64,old");
  assert.equal(controller.canRetrySubmit(), false);
  assert.equal(controller.getState().phase, "capturing");
  assert.equal(controller.getState().lastError, "Agent request rejected (422): Vision automation metadata is invalid");
  await controller.abort();
});

test("controller refuses automation send while the model is busy without queuing", async () => {
  let busy = false;
  let submissions = 0;
  const controller = createVisionAutomationController(createOptions({
    isModelBusy: () => busy,
    submit: async () => { submissions += 1; },
  }));

  await controller.start(binding);
  busy = true;
  await assert.rejects(controller.send("question"), /busy/i);
  assert.equal(submissions, 0);
  assert.equal(controller.getState().phase, "capturing");
  await controller.dispose();
});

test("Abort propagates to a pre-accept submission without creating a cleanup transport token", async () => {
  const disposals = { count: 0 };
  let observedSignal: AbortSignal | undefined;
  let releaseSubmit: (() => void) | undefined;
  const submitStarted = new Promise<void>((resolve) => { releaseSubmit = resolve; });
  const controller = createVisionAutomationController(createOptions({
    acquireSources: async () => [source(disposals)],
    submit: async ({ signal }) => {
      observedSignal = signal;
      releaseSubmit?.();
      await new Promise<void>((resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    },
  }));

  await controller.start(binding);
  const sending = controller.send("question");
  await submitStarted;
  await controller.abort();

  assert.equal(await sending, false);
  assert.equal(observedSignal?.aborted, true);
  assert.equal(disposals.count, 1);
  assert.equal(controller.getState().phase, "idle");
  assert.equal(await controller.stageCleanupSend(binding, "cleanup-request-1"), null);
});

test("Vision Automation Abort does not cancel an already accepted model transport", async () => {
  let transportSignal: AbortSignal | undefined;
  const controller = createVisionAutomationController(createOptions({
    submit: async ({ lifecycle, requestId, signal }) => {
      transportSignal = signal;
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
    },
  }));
  await controller.start(binding);
  assert.equal(await controller.send("accepted inference remains active"), true);
  assert.equal(transportSignal?.aborted, false);

  await controller.abort();

  assert.equal(transportSignal?.aborted, false);
  assert.equal(controller.getState().phase, "idle");
});

test("ordinary Vision Automation Abort leaves the bound Agent session ownership intact", async () => {
  const ownership = new pageIntegration.AgentSessionOwnership();
  ownership.bind("agent-session-owned");
  const controller = createVisionAutomationController(createOptions({
    submit: async ({ lifecycle, requestId }) => {
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
    },
  }));
  await controller.start({
    ...binding,
    mode: "agent",
    contextId: "agent-session-owned",
    automationTaskId: "agent-task-00000001",
  });
  await controller.send("accepted agent automation");

  await controller.abort({ registerAgentCleanup: false });

  assert.equal(ownership.sessionId, "agent-session-owned");
  await controller.dispose();
});

test("accepted request after Automation Abort keeps its gate busy when terminal polling fails", async () => {
  let requestId = "";
  const controller = createVisionAutomationController(createOptions({
    submit: async ({ lifecycle, requestId: submittedRequestId }) => {
      requestId = submittedRequestId;
      await lifecycle.onRequestAccepted({
        type: "request_accepted",
        requestId: submittedRequestId,
      });
    },
  }));
  await controller.start(binding);
  await controller.send("accepted before automation abort");
  const gate = new pageIntegration.AutomationTerminalGate(requestId);

  await controller.abort({ registerAgentCleanup: false });
  assert.equal(controller.getState().activeRequestId, null);
  assert.equal(typeof gate.pollForTerminal, "function");
  const outcome = await gate.pollForTerminal(async (queriedRequestId) => {
    assert.equal(queriedRequestId, requestId);
    throw new Error("status polling failed after abort");
  });

  assert.deepEqual(outcome, {
    kind: "failed",
    error: "status polling failed after abort",
  });
  assert.equal(gate.modelBusy, true);
  assert.equal(gate.canRetryStatus, true);
  await controller.dispose();
});

test("controller notifies the page only when an accepted request reaches stable terminal", async () => {
  let submitLifecycle: Parameters<NonNullable<VisionAutomationControllerOptions["submit"]>>[0]["lifecycle"] | undefined;
  const terminalRequestIds: string[] = [];
  const controller = createVisionAutomationController(createOptions({
    onStableTerminal: (requestId) => { terminalRequestIds.push(requestId); },
    submit: async ({ lifecycle, requestId }) => {
      submitLifecycle = lifecycle;
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
    },
  } as Partial<VisionAutomationControllerOptions> & {
    onStableTerminal(requestId: string): void;
  }));

  await controller.start(binding);
  assert.equal(await controller.send("candidate completes first"), true);
  assert.deepEqual(terminalRequestIds, []);
  const requestId = controller.getState().activeRequestId;
  assert.ok(requestId);

  await submitLifecycle?.onStableTerminal({ requestId, status: "terminal" });

  assert.deepEqual(terminalRequestIds, [requestId]);
  await submitLifecycle?.onStableTerminal({ requestId, status: "terminal" });
  assert.deepEqual(terminalRequestIds, [requestId]);
  await controller.dispose();
});

test("page Stop adapter exclusively cancels accepted Chat and Agent inference after Automation Abort", async () => {
  const abortAcceptedInference = (
    pageIntegration as typeof pageIntegration & {
      abortAcceptedVisionInference?: (controller: AbortController | null) => void;
    }
  ).abortAcceptedVisionInference;
  assert.equal(typeof abortAcceptedInference, "function");

  for (const mode of ["chat", "agent"] as const) {
    const accepted = deferred<void>();
    let transportController: AbortController | undefined;
    const controller = createVisionAutomationController(createOptions({
      submit: async ({ controller: requestController, lifecycle, requestId }) => {
        transportController = requestController;
        await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
        accepted.resolve();
        await new Promise<void>((resolve, reject) => {
          requestController.signal.addEventListener(
            "abort",
            () => reject(requestController.signal.reason),
            { once: true },
          );
        });
      },
    }));
    const modeBinding = {
      ...binding,
      mode,
      contextId: `${mode}-context`,
      automationTaskId: `${mode}-task-00000001`,
    };

    await controller.start(modeBinding);
    const sending = controller.send(`${mode} accepted inference`);
    await accepted.promise;
    assert.equal(transportController?.signal.aborted, false);

    await controller.abort();
    assert.equal(transportController?.signal.aborted, false);

    abortAcceptedInference?.(transportController ?? null);
    assert.equal(transportController?.signal.aborted, true);
    assert.equal(await sending, false);
    await controller.dispose();
  }
});

test("discarding a post-Abort cleanup stage preserves history and the cleanup permission", async () => {
  let messages = [historyMessage()];
  const controller = createVisionAutomationController(createOptions({
    getMessages: () => messages,
    commitMessages: (next) => { messages = next; },
  }));
  await controller.start(binding);
  await controller.send("accepted image block");
  await controller.abort();
  const before = JSON.stringify(messages);
  const cleanup = await controller.stageCleanupSend(binding, "cleanup-request-2");
  assert.ok(cleanup);
  const cleanupToken = cleanup.visionAutomation.binding.cleanup_token;
  assert.match(cleanupToken ?? "", /^cleanup-.{16,}$/);
  assert.deepEqual(cleanup.visionAutomation, {
    history_image_policy: "aggregate_prior_image_blocks",
    manual_image_count: 0,
    source_count: 0,
    retained_tick_count: 0,
    binding: {
      page_generation: 1,
      mode: "chat",
      top_level_context_id: "chat-1",
      automation_task_id: "task-00000000001",
      capture_cycle_id: "cycle-0000000000000002",
      cleanup_token: cleanupToken,
    },
  });

  cleanup.discard();

  assert.equal(JSON.stringify(messages), before);
  assert.ok(controller.getState().cleanupPending);
  assert.throws(
    () => cleanup.commitAccepted({ type: "request_accepted", requestId: "cleanup-request-2" }),
    /discarded/,
  );
  const restaged = await controller.stageCleanupSend(binding, "cleanup-request-3");
  assert.ok(restaged);
  assert.equal(restaged.visionAutomation.binding.cleanup_token, cleanupToken);
  assert.equal(restaged.commitAccepted({
    type: "request_accepted",
    requestId: "cleanup-request-3",
  }), true);
  assert.equal(await controller.stageCleanupSend(binding, "cleanup-request-4"), null);
  await controller.dispose();
});

test("post-Abort cleanup staging is single-flight and reuses one stage across pre-accept retries", async () => {
  const controller = createVisionAutomationController(createOptions());
  await controller.start(binding);
  await controller.send("accepted image block");
  await controller.abort();

  const [first, concurrent] = await Promise.all([
    controller.stageCleanupSend(binding, "cleanup-request-0001"),
    controller.stageCleanupSend(binding, "cleanup-request-0002"),
  ]);

  assert.ok(first);
  assert.equal(concurrent, first);
  assert.equal(first.requestId, "cleanup-request-0001");
  assert.equal(first.beginAttempt(), true);
  assert.equal(concurrent.beginAttempt(), false);
  first.finishAttempt();

  const retry = await controller.stageCleanupSend(binding, "cleanup-request-0003");
  assert.equal(retry, first);
  assert.equal(retry?.beginAttempt(), true);
  retry?.finishAttempt();

  const cleanupToken = controller.getState().cleanupPending;
  assert.ok(cleanupToken);
  assert.equal(await controller.cancelCleanup(cleanupToken), true);
  assert.throws(
    () => first.commitAccepted({ type: "request_accepted", requestId: first.requestId }),
    /discarded/,
  );
  await controller.dispose();
});

test("post-Abort cleanup 409 and 503 rejections finish only the attempt and retain its token", async () => {
  for (const status of [409, 503]) {
    const controller = createVisionAutomationController(createOptions());
    await controller.start(binding);
    await controller.send("accepted image block");
    await controller.abort();
    const stage = await controller.stageCleanupSend(binding, `cleanup-known-${status}`);
    assert.ok(stage);
    assert.equal(stage.beginAttempt(), true);
    const gate = new pageIntegration.AutomationTerminalGate(stage.requestId);
    const terminalEntries = new Map([[stage.requestId, { gate }]]);
    const message = `Agent request rejected (${status}): cleanup rejected`;

    assert.equal(pageIntegration.settleKnownCleanupRejection({
      isCurrentAttempt: true,
      terminalEntries,
      requestId: stage.requestId,
      stage,
      message,
      onVisibleError: (actual) => assert.equal(actual, message),
    }), true);
    assert.equal(terminalEntries.size, 0);
    assert.ok(controller.getState().cleanupPending, 'known rejection must retain the cleanup permission');
    const retry = await controller.stageCleanupSend(binding, `cleanup-known-retry-${status}`);
    assert.equal(retry, stage);
    assert.equal(retry?.beginAttempt(), true, 'the frozen cleanup stage must be reusable after known rejection');
    retry?.finishAttempt();
    await controller.dispose();
  }
});

test("re-arm is blocked while an ordinary cleanup send owns the inherited token", async () => {
  const controller = createVisionAutomationController(createOptions());
  await controller.start(binding);
  await controller.send("accepted image block");
  await controller.abort();
  const cleanup = await controller.stageCleanupSend(binding, "cleanup-request-0005");
  assert.ok(cleanup);
  assert.equal(cleanup.beginAttempt(), true);

  const rearmedBinding = { ...binding, automationTaskId: "task-00000000002" };
  let startError: unknown;
  try {
    await controller.start(rearmedBinding);
  } catch (error) {
    startError = error;
  }
  if (controller.getState().armed) await controller.abort();

  assert.match(startError instanceof Error ? startError.message : "", /cleanup send/i);
  cleanup.finishAttempt();
  assert.equal(await controller.start(rearmedBinding), true);
  await controller.dispose();
});

test("callbacks from an aborted submission are ignored before request identity validation", async () => {
  let lifecycle: Parameters<VisionAutomationControllerOptions["submit"]>[0]["lifecycle"] | undefined;
  let releaseSubmit: (() => void) | undefined;
  const controller = createVisionAutomationController(createOptions({
    submit: async (input) => {
      lifecycle = input.lifecycle;
      await new Promise<void>((resolve) => { releaseSubmit = resolve; });
    },
  }));
  await controller.start(binding);
  const sending = controller.send("question");
  while (lifecycle === undefined) await Promise.resolve();

  await controller.abort();
  await assert.doesNotReject(
    lifecycle.onRequestAccepted({ type: "request_accepted", requestId: "different-request" }),
  );
  releaseSubmit?.();
  assert.equal(await sending, false);
  assert.equal(controller.getState().phase, "idle");
});

test("repeated accepted turns reuse one coordinator and one ended-listener set", async () => {
  const listenerStats = { registered: 0, active: 0 };
  const captureSource: CaptureSource = {
    descriptor,
    async capture() {
      return {
        source: descriptor,
        resource: new OwnedImageResource(new Blob(["jpeg"], { type: "image/jpeg" })),
      };
    },
    onEnded() {
      listenerStats.registered += 1;
      listenerStats.active += 1;
      return () => { listenerStats.active -= 1; };
    },
    dispose() {},
  };
  const controller = createVisionAutomationController(createOptions({
    acquireSources: async () => [captureSource],
  }));

  await controller.start(binding);
  await controller.send("one");
  await controller.send("two");
  await controller.send("three");

  assert.deepEqual(listenerStats, { registered: 1, active: 1 });
  await controller.dispose();
  assert.equal(listenerStats.active, 0);
});

test("manual image count synchronization shrinks capacity and disposes evicted bundles", async () => {
  const resources: OwnedImageResource[] = [];
  const sources = Array.from({ length: 121 }, (_, index): CaptureSource => {
    const sourceDescriptor: SourceDescriptor = {
      sourceId: `camera-device-${index + 1}`,
      kind: "camera",
      stableAlias: `camera_${index + 1}`,
      label: `Camera ${index + 1}`,
      deviceId: `device-${index + 1}`,
    };
    return {
      descriptor: sourceDescriptor,
      async capture() {
        const resource = new OwnedImageResource(new Blob(["jpeg"], { type: "image/jpeg" }));
        resources.push(resource);
        return { source: sourceDescriptor, resource };
      },
      onEnded() { return () => {}; },
      dispose() {},
    };
  });
  const controller = createVisionAutomationController(createOptions({
    config: { ...config, intervalSeconds: 0.1, maxFramesPerSource: 2 },
    acquireSources: async () => sources,
  }));
  await controller.start(binding);
  await waitFor(() => controller.getBundles().length === 2);

  controller.setManualImageCount(9);

  assert.equal(controller.getBundles().length, 1);
  assert.equal(resources.slice(0, 121).every((resource) => resource.disposed), true);
  assert.equal(resources.slice(121, 242).every((resource) => !resource.disposed), true);
  await controller.dispose();
});

test("the first accepted armed send consumes inherited same-context cleanup", async () => {
  const controller = createVisionAutomationController(createOptions());
  await controller.start(binding);
  await controller.send("accepted image block");
  await controller.abort();
  const inheritedCleanupId = controller.getState().cleanupPending?.cleanupAttemptId;
  assert.ok(inheritedCleanupId);
  const nextBinding = { ...binding, automationTaskId: "task-00000000002" };

  await controller.start(nextBinding);
  assert.equal(controller.getState().cleanupPending?.cleanupAttemptId, inheritedCleanupId);
  await controller.send("accepted cleanup send");

  assert.equal(controller.getState().cleanupPending, null);
  await controller.dispose();
});

test("retrySources uses a new source-attempt identity and starts only its acquisition", async () => {
  const owners: string[] = [];
  let attempts = 0;
  const controller = createVisionAutomationController(createOptions({
    acquireSources: async ({ ownerId }) => {
      owners.push(ownerId);
      attempts += 1;
      if (attempts === 1) throw new Error("permission denied");
      return [source({ count: 0 })];
    },
  }));

  assert.equal(await controller.start(binding), false);
  const firstAttemptId = controller.getState().sourceAttemptId;
  assert.equal(controller.getState().phase, "waiting_for_sources");
  assert.equal(await controller.retrySources(), true);

  assert.equal(controller.getState().phase, "capturing");
  assert.notEqual(controller.getState().sourceAttemptId, firstAttemptId);
  assert.equal(owners.length, 2);
  assert.notEqual(owners[0], owners[1]);
  await controller.dispose();
});

test("chooser cancellation aborts without a cleanup token and a same-context restart can send", async () => {
  let attempts = 0;
  const controller = createVisionAutomationController(createOptions({
    acquireSources: async () => {
      attempts += 1;
      if (attempts === 1) throw new DOMException("chooser cancelled", "NotAllowedError");
      return [source({ count: 0 })];
    },
  }));

  assert.equal(await controller.start(binding), false);
  assert.equal(controller.getState().phase, "waiting_for_sources");
  await controller.abort();
  assert.equal(controller.getState().phase, "idle");
  assert.equal(controller.getState().cleanupPending, null);

  assert.equal(await controller.start(binding), true);
  assert.equal(await controller.send("restart after chooser cancel"), true);
  assert.doesNotMatch(controller.getRecoveryError() ?? "", /Inherited cleanup registration became stale/);
  await controller.dispose();
});

test("capture-cycle initialization failure tears down sources, becomes retryable, and retries cleanly", async () => {
  let manualCount = 10;
  const firstDisposals = { count: 0 };
  const controller = createVisionAutomationController(createOptions({
    getManualDataUrls: () => Array.from({ length: manualCount }, () => "data:image/jpeg;base64,AA=="),
    acquireSources: async () => [source(firstDisposals)],
  }));

  assert.equal(await controller.start(binding), false);
  assert.equal(controller.getState().phase, "waiting_for_sources");
  assert.match(controller.getState().lastError ?? "", /manualImageCount/);
  assert.equal(firstDisposals.count, 1);

  manualCount = 0;
  assert.equal(await controller.retrySources(), true);
  assert.equal(controller.getState().phase, "capturing");
  await controller.dispose();
});

test("sampler bundle rejection is visible without a false successful tick or source failure", async () => {
  let captures = 0;
  const rejectingSource: CaptureSource = {
    descriptor,
    async capture(signal) {
      if (signal.aborted) throw signal.reason;
      captures += 1;
      return {
        source: captures === 1 ? { ...descriptor, sourceId: "wrong-source" } : descriptor,
        resource: new OwnedImageResource(new Blob(["jpeg"], { type: "image/jpeg" })),
        capturedAtMs: captures,
      };
    },
    onEnded() {
      return () => {};
    },
    dispose() {},
  };
  const controller = createVisionAutomationController(createOptions({
    config: { ...config, intervalSeconds: 0.1 },
    acquireSources: async () => [rejectingSource],
  }));

  assert.equal(await controller.start(binding), true);
  assert.match(controller.getState().lastError ?? "", /Captured bundle was rejected/);
  assert.equal(controller.getState().pendingSourceError, null);
  assert.equal(controller.getStats().successfulTicks, 0);

  await waitFor(() => controller.getStats().successfulTicks === 1, 1_500);
  assert.equal(controller.getState().lastError, null);
  await controller.dispose();
});

test("retryFailedSource restores only camera_1 and preserves camera_2, cohort order, bundles, and cycle", async () => {
  const camera1: SourceDescriptor = {
    sourceId: "camera-device-1",
    kind: "camera",
    stableAlias: "camera_1",
    label: "Camera 1",
    deviceId: "device-1",
  };
  const camera2: SourceDescriptor = {
    sourceId: "camera-device-2",
    kind: "camera",
    stableAlias: "camera_2",
    label: "Camera 2",
    deviceId: "device-2",
  };
  const disposals = {
    oldCamera1: 0,
    healthyCamera2: 0,
    replacementCamera1: 0,
  };
  let endCamera1: (() => void) | undefined;
  function captureSource(
    sourceDescriptor: SourceDescriptor,
    disposalKey: keyof typeof disposals,
  ): CaptureSource {
    let disposed = false;
    return {
      descriptor: sourceDescriptor,
      async capture() {
        return {
          source: sourceDescriptor,
          resource: new OwnedImageResource(new Blob([sourceDescriptor.stableAlias], { type: "image/jpeg" })),
        };
      },
      onEnded(listener) {
        if (sourceDescriptor.stableAlias === "camera_1") endCamera1 = listener;
        return () => {
          if (sourceDescriptor.stableAlias === "camera_1") endCamera1 = undefined;
        };
      },
      dispose() {
        if (disposed) return;
        disposed = true;
        disposals[disposalKey] += 1;
      },
    };
  }
  let fullCohortAcquisitions = 0;
  let restoreOwnerId = "";
  const controller = createVisionAutomationController(createOptions({
    acquireSources: async () => {
      fullCohortAcquisitions += 1;
      if (fullCohortAcquisitions > 1) {
        return [{
          ...captureSource(camera2, "healthyCamera2"),
          descriptor: { ...camera2, stableAlias: "camera_3", sourceId: "camera-device-3" },
        }];
      }
      return [
        captureSource(camera1, "oldCamera1"),
        captureSource(camera2, "healthyCamera2"),
      ];
    },
    restoreSource: async ({ ownerId, source: failedSource, requiresDirectDisplayChooser }) => {
      restoreOwnerId = ownerId;
      assert.equal(failedSource, camera1);
      assert.equal(requiresDirectDisplayChooser, false);
      return captureSource(camera1, "replacementCamera1");
    },
  }));
  await controller.start(binding);
  const retainedBefore = controller.getBundles();
  const lastRetainedBefore = retainedBefore.at(-1);
  assert.ok(lastRetainedBefore);
  const cycleBefore = {
    id: controller.getState().captureCycleId,
    generation: controller.getState().cycleGeneration,
  };
  const firstAttemptId = controller.getState().sourceAttemptId;
  endCamera1?.();
  assert.deepEqual(controller.getFailedSourceIdentity(), {
    stableAlias: "camera_1",
    sourceId: "camera-device-1",
  });

  assert.equal(await controller.retryFailedSource({
    stableAlias: "camera_1",
    sourceId: "camera-device-1",
  }), true);

  assert.equal(fullCohortAcquisitions, 1, "recovery must not reacquire or replace the cohort");
  assert.notEqual(controller.getState().sourceAttemptId, firstAttemptId);
  assert.match(restoreOwnerId, /source-/);
  assert.deepEqual(controller.getBundles().slice(0, retainedBefore.length), retainedBefore);
  assert.deepEqual({
    id: controller.getState().captureCycleId,
    generation: controller.getState().cycleGeneration,
  }, cycleBefore);
  assert.deepEqual(
    controller.getBundles().at(-1)?.frames.map((frame) => frame.source.stableAlias),
    ["camera_1", "camera_2"],
  );
  assert.equal(controller.getBundles().at(-1)?.tickIndex, lastRetainedBefore.tickIndex + 1);
  assert.ok(
    (controller.getBundles().at(-1)?.relativeTimeSeconds ?? -1)
      >= lastRetainedBefore.relativeTimeSeconds,
  );
  assert.equal(disposals.oldCamera1, 1);
  assert.equal(disposals.healthyCamera2, 0);
  await controller.dispose();
  assert.equal(disposals.healthyCamera2, 1);
  assert.equal(disposals.replacementCamera1, 1);
});

test("retryFailedSource rejects arbitrary replacement alias without changing camera cohort", async () => {
  const camera1 = { ...descriptor };
  const camera2: SourceDescriptor = {
    sourceId: "camera-device-2",
    kind: "camera",
    stableAlias: "camera_2",
    label: "Camera 2",
    deviceId: "device-2",
  };
  let endCamera1: (() => void) | undefined;
  const healthyDisposals = { count: 0 };
  const replacementDisposals = { count: 0 };
  const camera1Source = source({ count: 0 });
  camera1Source.onEnded = (listener) => {
    endCamera1 = listener;
    return () => { endCamera1 = undefined; };
  };
  const camera2Source = {
    ...source(healthyDisposals),
    descriptor: camera2,
  };
  const controller = createVisionAutomationController(createOptions({
    acquireSources: async () => [camera1Source, camera2Source],
    restoreSource: async () => ({
      ...source(replacementDisposals),
      descriptor: {
        ...camera1,
        stableAlias: "camera_3",
        sourceId: "camera-device-3",
      },
    }),
  }));
  await controller.start(binding);
  const retainedBefore = controller.getBundles();
  endCamera1?.();

  assert.equal(await controller.retryFailedSource({
    stableAlias: "camera_1",
    sourceId: "camera-device-1",
  }), false);

  assert.equal(controller.getState().phase, "paused_source_error");
  assert.deepEqual(controller.getBundles(), retainedBefore);
  assert.equal(healthyDisposals.count, 0);
  assert.equal(replacementDisposals.count, 1);
  await controller.dispose();
});

test("retryFailedSource invokes desktop restoration synchronously as a direct chooser", async () => {
  const desktop: SourceDescriptor = {
    sourceId: "desktop-source",
    kind: "display",
    stableAlias: "desktop",
    label: "Desktop",
  };
  let endDesktop: (() => void) | undefined;
  const desktopSource = {
    ...source({ count: 0 }),
    descriptor: desktop,
    onEnded(listener: () => void) {
      endDesktop = listener;
      return () => { endDesktop = undefined; };
    },
  };
  let chooserInvoked = false;
  const controller = createVisionAutomationController(createOptions({
    acquireSources: async () => [desktopSource],
    restoreSource: async ({ source: failedSource, requiresDirectDisplayChooser }) => {
      chooserInvoked = true;
      assert.equal(failedSource, desktop);
      assert.equal(requiresDirectDisplayChooser, true);
      return { ...source({ count: 0 }), descriptor: desktop };
    },
  }));
  await controller.start(binding);
  endDesktop?.();

  const recovery = controller.retryFailedSource({
    stableAlias: "desktop",
    sourceId: "desktop-source",
  });
  assert.equal(chooserInvoked, true, "desktop chooser must run in the direct retry call stack");
  assert.equal(await recovery, true);
  await controller.dispose();
});

test("a failed retry retains camera_1 identity and rejects a healthy camera_2 attempt", async () => {
  const camera1 = { ...descriptor };
  const camera2: SourceDescriptor = {
    sourceId: "camera-device-2",
    kind: "camera",
    stableAlias: "camera_2",
    label: "Camera 2",
    deviceId: "device-2",
  };
  let endCamera1: (() => void) | undefined;
  let restoreAttempts = 0;
  function captureSource(sourceDescriptor: SourceDescriptor): CaptureSource {
    return {
      descriptor: sourceDescriptor,
      async capture() {
        return {
          source: sourceDescriptor,
          resource: new OwnedImageResource(new Blob([sourceDescriptor.stableAlias], { type: "image/jpeg" })),
        };
      },
      onEnded(listener) {
        if (sourceDescriptor.stableAlias === "camera_1") endCamera1 = listener;
        return () => {
          if (sourceDescriptor.stableAlias === "camera_1") endCamera1 = undefined;
        };
      },
      dispose() {},
    };
  }
  const controller = createVisionAutomationController(createOptions({
    acquireSources: async () => [captureSource(camera1), captureSource(camera2)],
    restoreSource: async ({ source: failedSource }) => {
      restoreAttempts += 1;
      if (restoreAttempts === 1) throw new Error("camera permission still denied");
      return captureSource(failedSource);
    },
  }));
  await controller.start(binding);
  endCamera1?.();

  assert.equal(await controller.retryFailedSource({
    stableAlias: "camera_1",
    sourceId: "camera-device-1",
  }), false);
  assert.equal(controller.getState().phase, "paused_source_error");
  await assert.rejects(controller.retryFailedSource({
    stableAlias: "camera_2",
    sourceId: "camera-device-2",
  }), /failed source|does not match/i);
  assert.equal(restoreAttempts, 1, "a healthy source must not be passed to restoreSource");
  assert.equal(await controller.retryFailedSource({
    stableAlias: "camera_1",
    sourceId: "camera-device-1",
  }), true);
  assert.equal(restoreAttempts, 2);
  await controller.dispose();
});

test("recovery before the first successful bundle preserves elapsed T=0 and next tick index", async () => {
  let attempts = 0;
  function captureSource(fail: boolean): CaptureSource {
    return {
      descriptor,
      async capture() {
        attempts += 1;
        if (fail) throw new Error("camera unavailable");
        return {
          source: descriptor,
          resource: new OwnedImageResource(new Blob(["recovered"], { type: "image/jpeg" })),
        };
      },
      onEnded() { return () => {}; },
      dispose() {},
    };
  }
  const controller = createVisionAutomationController(createOptions({
    config: { ...config, consecutiveTickFailureLimit: 1 },
    acquireSources: async () => [captureSource(true)],
    restoreSource: async () => captureSource(false),
  }));
  await controller.start(binding);
  assert.equal(controller.getState().phase, "paused_source_error");
  assert.equal(controller.getBundles().length, 0);
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.equal(await controller.retryFailedSource({
    stableAlias: "camera_1",
    sourceId: "camera-device-1",
  }), true);

  assert.equal(attempts, 2);
  assert.equal(controller.getBundles().length, 1);
  assert.equal(controller.getBundles()[0].tickIndex, 1);
  assert.ok(controller.getBundles()[0].relativeTimeSeconds >= 0.01);
  await controller.dispose();
});

test("Abort clears failed identity before a new task reports another failed source", async () => {
  const camera1 = { ...descriptor };
  const camera2: SourceDescriptor = {
    sourceId: "camera-device-2",
    kind: "camera",
    stableAlias: "camera_2",
    label: "Camera 2",
    deviceId: "device-2",
  };
  const ended = new Map<string, () => void>();
  function captureSource(sourceDescriptor: SourceDescriptor): CaptureSource {
    return {
      descriptor: sourceDescriptor,
      async capture() {
        return {
          source: sourceDescriptor,
          resource: new OwnedImageResource(new Blob([sourceDescriptor.stableAlias], { type: "image/jpeg" })),
        };
      },
      onEnded(listener) {
        ended.set(sourceDescriptor.stableAlias, listener);
        return () => { ended.delete(sourceDescriptor.stableAlias); };
      },
      dispose() {},
    };
  }
  const controller = createVisionAutomationController(createOptions({
    acquireSources: async ({ binding: activeBinding }) => [
      captureSource(activeBinding.automationTaskId === "task-00000000001" ? camera1 : camera2),
    ],
    restoreSource: async ({ source: failedSource }) => captureSource(failedSource),
  }));
  await controller.start(binding);
  ended.get("camera_1")?.();
  assert.equal(controller.getState().phase, "paused_source_error");
  await controller.abort();

  const nextBinding = { ...binding, automationTaskId: "task-00000000002" };
  await controller.start(nextBinding);
  ended.get("camera_2")?.();
  assert.equal(controller.getState().phase, "paused_source_error");
  await assert.rejects(controller.retryFailedSource({
    stableAlias: "camera_1",
    sourceId: "camera-device-1",
  }), /failed source|immutable cohort|does not match/i);
  assert.equal(await controller.retryFailedSource({
    stableAlias: "camera_2",
    sourceId: "camera-device-2",
  }), true);
  await controller.dispose();
});

test("stable terminal defers a new non-inference cycle until exact source recovery", async () => {
  const allowStableTerminal = deferred<void>();
  const accepted = deferred<void>();
  const terminalCompleted = deferred<void>();
  const restore = deferred<CaptureSource>();
  let endOldSource: (() => void) | undefined;
  let oldCaptures = 0;
  let replacementCaptures = 0;
  function captureSource(kind: "old" | "replacement"): CaptureSource {
    return {
      descriptor,
      async capture() {
        if (kind === "old") oldCaptures += 1;
        else replacementCaptures += 1;
        return {
          source: descriptor,
          capturedAtMs: kind === "old" ? 100 : 200,
          resource: new OwnedImageResource(new Blob([kind], { type: "image/jpeg" })),
        };
      },
      onEnded(listener) {
        if (kind === "old") endOldSource = listener;
        return () => {
          if (kind === "old") endOldSource = undefined;
        };
      },
      dispose() {},
    };
  }
  const controller = createVisionAutomationController(createOptions({
    acquireSources: async () => [captureSource("old")],
    restoreSource: async () => restore.promise,
    submit: async ({ lifecycle, requestId }) => {
      endOldSource?.();
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
      accepted.resolve(undefined);
      await allowStableTerminal.promise;
      await lifecycle.onStableTerminal({ requestId, status: "terminal" });
      terminalCompleted.resolve(undefined);
    },
  }));
  await controller.start(binding);
  const sending = controller.send("new cycle after terminal");
  await accepted.promise;
  const recovery = controller.retryFailedSource({
    stableAlias: "camera_1",
    sourceId: "camera-device-1",
  });

  allowStableTerminal.resolve(undefined);
  await terminalCompleted.promise;
  restore.resolve(captureSource("replacement"));
  assert.equal(await recovery, true);
  assert.equal(await sending, true);
  const retained = controller.getBundles().map((bundle) => ({
    tickIndex: bundle.tickIndex,
    capturedAtMs: bundle.frames[0]?.capturedAtMs,
  }));
  await controller.dispose();

  assert.equal(oldCaptures, 1, "the ended wrapper must not capture the lifecycle-selected cycle");
  assert.equal(replacementCaptures, 1, "the replacement cycle must start exactly once");
  assert.deepEqual(retained, [{ tickIndex: 0, capturedAtMs: 200 }]);
});

test("acceptance defers a new capture-during-inference cycle until exact source recovery", async () => {
  const allowAcceptance = deferred<void>();
  const sourceFailed = deferred<void>();
  const acceptanceCompleted = deferred<void>();
  const allowStableTerminal = deferred<void>();
  let endOldSource: (() => void) | undefined;
  let oldCaptures = 0;
  let replacementCaptures = 0;
  function captureSource(kind: "old" | "replacement"): CaptureSource {
    return {
      descriptor,
      async capture() {
        if (kind === "old") oldCaptures += 1;
        else replacementCaptures += 1;
        return {
          source: descriptor,
          capturedAtMs: kind === "old" ? 100 : 200,
          resource: new OwnedImageResource(new Blob([kind], { type: "image/jpeg" })),
        };
      },
      onEnded(listener) {
        if (kind === "old") endOldSource = listener;
        return () => {
          if (kind === "old") endOldSource = undefined;
        };
      },
      dispose() {},
    };
  }
  const controller = createVisionAutomationController(createOptions({
    config: { ...config, captureDuringInference: true },
    acquireSources: async () => [captureSource("old")],
    restoreSource: async () => captureSource("replacement"),
    submit: async ({ lifecycle, requestId }) => {
      endOldSource?.();
      sourceFailed.resolve(undefined);
      await allowAcceptance.promise;
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
      acceptanceCompleted.resolve(undefined);
      await allowStableTerminal.promise;
      await lifecycle.onStableTerminal({ requestId, status: "terminal" });
    },
  }));
  await controller.start(binding);
  const sending = controller.send("capture during inference");
  await sourceFailed.promise;
  allowAcceptance.resolve(undefined);
  await acceptanceCompleted.promise;

  assert.equal(await controller.retryFailedSource({
    stableAlias: "camera_1",
    sourceId: "camera-device-1",
  }), true);
  allowStableTerminal.resolve(undefined);
  assert.equal(await sending, true);
  const retained = controller.getBundles().map((bundle) => ({
    tickIndex: bundle.tickIndex,
    capturedAtMs: bundle.frames[0]?.capturedAtMs,
  }));
  await controller.dispose();

  assert.equal(oldCaptures, 1, "acceptance must not start the ended wrapper");
  assert.equal(replacementCaptures, 1, "the replacement cycle must start exactly once");
  assert.deepEqual(retained, [{ tickIndex: 0, capturedAtMs: 200 }]);
});

test("Abort cancels the one transaction signal created before payload encoding", async () => {
  const encodingStarted = deferred<void>();
  const releaseEncoding = deferred<void>();
  let encodingSignal: AbortSignal | undefined;
  const controller = createVisionAutomationController(createOptions({
    encodeFrame: async ({ blob }, _tier, signal) => {
      encodingSignal = signal;
      encodingStarted.resolve(undefined);
      await releaseEncoding.promise;
      if (signal.aborted) throw signal.reason;
      return blob;
    },
  }));
  await controller.start(binding);
  const sending = controller.send("cancel preparation");
  await encodingStarted.promise;

  await controller.abort();
  const wasAborted = encodingSignal?.aborted;
  releaseEncoding.resolve(undefined);

  assert.equal(await sending, false);
  assert.equal(wasAborted, true);
});

test("active task keeps its Trigger Start config snapshot across option refreshes", async () => {
  const qualities: number[] = [];
  const options = createOptions({
    config: { ...config, lockFirstBundle: true },
    encodeFrame: async ({ blob }, tier) => {
      qualities.push(tier.quality);
      return blob;
    },
  });
  const controller = createVisionAutomationController(options);
  await controller.start(binding);
  controller.updateOptions({
    ...options,
    config: { ...config, initialJpegQuality: 50, maxFramesPerSource: 1, lockFirstBundle: false },
  });

  await controller.send("frozen config");

  assert.equal(qualities[0], 85);
  assert.equal(controller.getActiveConfig()?.maxFramesPerSource, 2);
  assert.equal(controller.getActiveConfig()?.lockFirstBundle, true);
  await controller.dispose();
});

test("accepted disconnect waits through running status to stable terminal", async () => {
  let terminalWaits = 0;
  const controller = createVisionAutomationController(createOptions({
    getRequestStatus: async (requestId) => ({ requestId, status: "running" }),
    waitForRequestTerminal: async (requestId) => {
      terminalWaits += 1;
      return { requestId, status: "terminal" };
    },
    submit: async ({ lifecycle, requestId }) => {
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
      await lifecycle.onTransportEof();
    },
  }));
  await controller.start(binding);

  assert.equal(await controller.send("disconnect"), true);
  assert.equal(terminalWaits, 1);
  assert.equal(controller.getState().phase, "capturing");
  assert.equal(controller.canRetryStatus(), false);
  await controller.dispose();
});

test("Answer Now replacement supersedes the original terminal and exclusively restarts capture", async () => {
  let originalLifecycle: Parameters<VisionAutomationControllerOptions["submit"]>[0]["lifecycle"] | undefined;
  const queriedRequestIds: string[] = [];
  const terminalRequestIds: string[] = [];
  const controller = createVisionAutomationController(createOptions({
    onStableTerminal: (requestId) => { terminalRequestIds.push(requestId); },
    getRequestStatus: async (requestId) => {
      queriedRequestIds.push(requestId);
      return { requestId, status: "running" };
    },
    waitForRequestTerminal: async (requestId) => {
      queriedRequestIds.push(requestId);
      return { requestId, status: "terminal" };
    },
    submit: async ({ lifecycle, requestId }) => {
      originalLifecycle = lifecycle;
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
    },
  }));
  await controller.start(binding);
  assert.equal(await controller.send("replace this run"), true);
  assert.equal(controller.getState().phase, "waiting_for_model");
  const originalRequestId = controller.getState().activeRequestId;
  assert.ok(originalRequestId);

  const replacement = controller.beginAnswerNowReplacement("answer-now-request-0001");
  assert.equal(controller.getState().activeRequestId, originalRequestId);
  await originalLifecycle?.onStableTerminal({ requestId: originalRequestId, status: "terminal" });
  assert.equal(controller.getState().phase, "waiting_for_model");

  await replacement.onRequestAccepted({
    type: "request_accepted",
    requestId: "answer-now-request-0001",
  });
  assert.equal(controller.getState().activeRequestId, "answer-now-request-0001");
  await replacement.onStableTerminal({ requestId: "answer-now-request-0001", status: "terminal" });
  assert.deepEqual(queriedRequestIds, []);
  assert.deepEqual(terminalRequestIds, ["answer-now-request-0001"]);
  assert.equal(controller.getState().phase, "capturing");
  await controller.dispose();
});

test("Answer Now known rejection before replacement acceptance restores the original terminal owner", async () => {
  let originalLifecycle: Parameters<VisionAutomationControllerOptions["submit"]>[0]["lifecycle"] | undefined;
  const controller = createVisionAutomationController(createOptions({
    submit: async ({ lifecycle, requestId }) => {
      originalLifecycle = lifecycle;
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
    },
  }));
  await controller.start(binding);
  assert.equal(await controller.send("original request"), true);
  const originalRequestId = controller.getState().activeRequestId;
  assert.ok(originalRequestId);

  const replacement = controller.beginAnswerNowReplacement("answer-now-request-0002");
  await replacement.onKnownRejection({
    status: 503,
    detail: "replacement unavailable",
    message: "Agent request rejected (503): replacement unavailable",
    retryable: true,
  });

  assert.equal(controller.getState().activeRequestId, originalRequestId);
  await originalLifecycle?.onStableTerminal({ requestId: originalRequestId, status: "terminal" });
  assert.equal(controller.getState().phase, "capturing");
  await controller.dispose();
});

test("Answer Now buffers original terminal until replacement rejection, without status probing", async () => {
  let originalLifecycle: Parameters<VisionAutomationControllerOptions["submit"]>[0]["lifecycle"] | undefined;
  const statusQueries: string[] = [];
  const controller = createVisionAutomationController(createOptions({
    getRequestStatus: async (requestId) => {
      statusQueries.push(requestId);
      return { requestId, status: "running" };
    },
    submit: async ({ lifecycle, requestId }) => {
      originalLifecycle = lifecycle;
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
    },
  }));
  await controller.start(binding);
  await controller.send("original");
  const originalRequestId = controller.getState().activeRequestId!;
  const replacement = controller.beginAnswerNowReplacement("answer-now-request-0003");
  await originalLifecycle!.onStableTerminal({ requestId: originalRequestId, status: "terminal" });
  assert.equal(controller.getState().phase, "waiting_for_model");
  const handled = await replacement.onKnownRejection({
    status: 503, detail: "busy", message: "Agent request rejected (503): busy", retryable: true,
  });
  assert.equal(handled, true);
  assert.equal(controller.getState().phase, "capturing");
  assert.equal(controller.getState().activeRequestId, null);
  assert.deepEqual(statusQueries, []);
  await controller.dispose();
});

test("Answer Now replacement keeps the existing capture-during-inference cycle without another restart", async () => {
  let captures = 0;
  let originalLifecycle: Parameters<VisionAutomationControllerOptions["submit"]>[0]["lifecycle"] | undefined;
  const captureSource = source({ count: 0 });
  captureSource.capture = async () => {
    captures += 1;
    return {
      source: descriptor,
      resource: new OwnedImageResource(new Blob(["jpeg"], { type: "image/jpeg" })),
    };
  };
  const controller = createVisionAutomationController(createOptions({
    config: { ...config, captureDuringInference: true },
    acquireSources: async () => [captureSource],
    submit: async ({ lifecycle, requestId }) => {
      originalLifecycle = lifecycle;
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
    },
  }));
  await controller.start(binding);
  await controller.send("keep capturing");
  assert.equal(controller.getState().phase, "capturing_during_model");
  const capturesAfterOriginalAcceptance = captures;
  const originalRequestId = controller.getState().activeRequestId;
  assert.ok(originalRequestId);

  const replacement = controller.beginAnswerNowReplacement("answer-now-request-0002");
  await originalLifecycle?.onStableTerminal({ requestId: originalRequestId, status: "terminal" });
  await replacement.onRequestAccepted({
    type: "request_accepted",
    requestId: "answer-now-request-0002",
  });

  assert.equal(controller.getState().phase, "capturing_during_model");
  assert.equal(captures, capturesAfterOriginalAcceptance);
  await replacement.onStableTerminal({ requestId: "answer-now-request-0002", status: "terminal" });
  assert.equal(controller.getState().phase, "capturing");
  assert.equal(captures, capturesAfterOriginalAcceptance);
  await controller.dispose();
});

test("failed terminal polling exposes Retry Status but never Retry Submit after acceptance", async () => {
  const controller = createVisionAutomationController(createOptions({
    getRequestStatus: async (requestId) => ({ requestId, status: "running" }),
    waitForRequestTerminal: async () => { throw new Error("status unavailable"); },
    submit: async ({ lifecycle, requestId }) => {
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
      await lifecycle.onTransportEof();
    },
  }));
  await controller.start(binding);
  assert.equal(await controller.send("unresolved"), true);

  assert.equal(controller.canRetryStatus(), true);
  assert.equal(controller.canRetrySubmit(), false);
  assert.match(controller.getRecoveryError() ?? "", /status unavailable/);
  await controller.dispose();
});

test("Retry Status notifies the page exactly once when it discovers delayed acceptance", async () => {
  const statuses: Array<"unknown" | "terminal"> = ["unknown", "terminal"];
  const accepted: string[] = [];
  const controller = createVisionAutomationController(createOptions({
    getRequestStatus: async (requestId) => ({ requestId, status: statuses.shift() ?? "terminal" }),
    onRequestAccepted: (requestId) => { accepted.push(requestId); },
    submit: async ({ lifecycle }) => { await lifecycle.onTransportEof(); },
  }));
  await controller.start(binding);
  assert.equal(await controller.send("delayed acceptance"), false);
  assert.deepEqual(accepted, []);

  const result = await controller.retryStatus();

  assert.equal(result?.status, "terminal");
  assert.equal(accepted.length, 1);
  assert.equal(controller.getState().phase, "capturing");
  await controller.dispose();
});

test("Agent Abort registers exact accepted binding and retries registration before cleanup send", async () => {
  const agentBinding: AutomationBinding = {
    ...binding,
    mode: "agent",
    contextId: "agent-session-0001",
  };
  const registrations: unknown[] = [];
  let failRegistration = true;
  const disposals = { count: 0 };
  const controller = createVisionAutomationController(createOptions({
    acquireSources: async () => [source(disposals)],
    registerAgentAbort: async (payload) => {
      registrations.push(payload);
      if (failRegistration) throw new Error("registration offline");
    },
  }));
  await controller.start(agentBinding);
  await controller.send("accepted Agent image");

  await controller.abort();

  assert.equal(disposals.count, 1);
  assert.equal(registrations.length, 1);
  assert.deepEqual(registrations[0], {
    session_id: "agent-session-0001",
    binding: {
      page_generation: 1,
      mode: "agent",
      top_level_context_id: "agent-session-0001",
      automation_task_id: "task-00000000001",
      capture_cycle_id: "cycle-0000000000000002",
      cleanup_token: null,
    },
    cleanup_token: "cleanup-0000000000000006",
  });
  assert.equal(controller.hasPendingCleanupRegistration(), true);

  failRegistration = false;
  const cleanup = await controller.stageCleanupSend(agentBinding, "cleanup-request-0001");
  assert.ok(cleanup);
  assert.equal(registrations.length, 2);
  assert.equal(controller.hasPendingCleanupRegistration(), false);
  assert.equal(cleanup.visionAutomation.binding.cleanup_token, "cleanup-0000000000000006");
  await controller.dispose();
});

test("same-context Stop and ASR re-arm use semantic inherited cleanup identity", async () => {
  const agentBinding: AutomationBinding = {
    ...binding,
    mode: "agent",
    contextId: "agent-session-0001",
  };
  const registrationStarted = deferred<void>();
  const allowRegistration = deferred<void>();
  const registrations: unknown[] = [];
  const submittedCleanupTokens: Array<string | undefined> = [];
  const controller = createVisionAutomationController(createOptions({
    registerAgentAbort: async (request) => {
      registrations.push(request);
      registrationStarted.resolve(undefined);
      await allowRegistration.promise;
    },
    submit: async ({ lifecycle, requestId, visionAutomation }) => {
      submittedCleanupTokens.push(visionAutomation.binding.cleanup_token);
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
      await lifecycle.onStableTerminal({ requestId, status: "terminal" });
    },
  }));
  await controller.start(agentBinding);
  await controller.send("accepted image before Abort");
  await controller.abort({ registerAgentCleanup: false });

  const rearmedBinding = {
    ...agentBinding,
    automationTaskId: "task-00000000002",
  };
  await controller.start(rearmedBinding);
  const asrSending = controller.send("ASR text after Stop");
  await registrationStarted.promise;
  const projectedCleanup = controller.getState().cleanupPending;
  assert.ok(projectedCleanup);
  controller.getState().cleanupPending = {
    cleanupAttemptId: projectedCleanup.cleanupAttemptId,
    binding: { ...projectedCleanup.binding },
  };
  allowRegistration.resolve(undefined);

  assert.equal(await asrSending, true);
  assert.equal(controller.getRecoveryError(), null);
  assert.equal(registrations.length, 1);
  assert.equal(submittedCleanupTokens[1], projectedCleanup.cleanupAttemptId);
  assert.equal(controller.getState().cleanupPending, null);
  await controller.dispose();
});

test("Agent cleanup cancellation aborts registration and clears local permission only after one exact remote confirmation", async () => {
  const agentBinding: AutomationBinding = {
    ...binding,
    mode: "agent",
    contextId: "agent-session-0001",
  };
  const registrationStarted = deferred<void>();
  const cancellationResponse = deferred<void>();
  const registrations: Array<{
    request: unknown;
    signal: AbortSignal | undefined;
  }> = [];
  const cancellations: Array<{
    request: unknown;
    signal: AbortSignal | undefined;
  }> = [];
  const controller = createVisionAutomationController(createOptions({
    registerAgentAbort: async (request, signal) => {
      registrations.push({ request, signal });
      registrationStarted.resolve(undefined);
      await new Promise<void>((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    },
    cancelAgentAbort: async (request, signal) => {
      cancellations.push({ request, signal });
      await cancellationResponse.promise;
    },
  }));
  await controller.start(agentBinding);
  await controller.send("accepted Agent image");
  const aborting = controller.abort();
  await registrationStarted.promise;
  const cleanup = controller.getState().cleanupPending;
  assert.ok(cleanup);

  const firstCancellation = controller.cancelCleanup(cleanup);
  const secondCancellation = controller.cancelCleanup(cleanup);
  await waitFor(() => cancellations.length === 1);

  assert.equal(registrations[0].signal?.aborted, true);
  assert.deepEqual(cancellations[0].request, registrations[0].request);
  assert.ok(cancellations[0].signal);
  assert.equal(controller.getState().cleanupPending, cleanup);
  cancellationResponse.resolve(undefined);

  assert.equal(await firstCancellation, true);
  assert.equal(await secondCancellation, true);
  assert.equal(controller.getState().cleanupPending, null);
  await aborting;
  await controller.dispose();
});

test("tick counters stay cumulative while image and exact Data URL byte gauges reflect the retained pool", async () => {
  let captures = 0;
  const manualImages = ["data:image/jpeg;base64,bQ=="];
  const captureSource: CaptureSource = {
    descriptor,
    async capture() {
      captures += 1;
      return {
        source: descriptor,
        resource: new OwnedImageResource(new Blob(["jpeg"], { type: "image/jpeg" })),
      };
    },
    onEnded() { return () => {}; },
    dispose() {},
  };
  const controller = createVisionAutomationController(createOptions({
    config: { ...config, intervalSeconds: 0.1, maxFramesPerSource: 1 },
    acquireSources: async () => [captureSource],
    getManualDataUrls: () => manualImages,
  }));
  await controller.start(binding);
  await waitFor(() => captures >= 3);
  const stats = controller.getStats();

  assert.equal(controller.getBundles().length, 1);
  assert.ok(stats.attemptedTicks >= 3);
  assert.ok(stats.successfulTicks >= 3);
  assert.equal(stats.failedTicks, 0);
  assert.equal(stats.imageCount, 2);
  assert.equal(
    stats.encodedBytes,
    new TextEncoder().encode(manualImages[0]).byteLength + estimateDataUrlBytes(4),
  );
  await controller.dispose();
});
