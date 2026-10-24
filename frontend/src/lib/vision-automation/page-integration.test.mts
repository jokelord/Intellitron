import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { LLMClient } from "../api.ts";
import type { VisionAutomationServerConfig } from "./config.ts";
import type { CaptureSource } from "./capture-source.ts";
import { OwnedImageResource } from "./image-resources.ts";
import { stageChatHistoryImageOmission } from "./history-omission.ts";
import * as pageIntegration from "./page-integration.ts";
import { createVisionAutomationController } from "./use-vision-automation.ts";
import {
  AcceptanceMutationRegistry,
  CycleStartGuard,
  VisionPageLifecycle,
  acquireAvailableVisionSources,
  assertBoundAgentSessionId,
  canResumeVisionVoiceCapture,
  canStartVisionAutomation,
  freezeVisionAutomationConfig,
  getVisibleVisionUserText,
  resolveCanonicalVisionAutomationImageBlock,
} from "./page-integration.ts";
import type { SourceDescriptor } from "./types.ts";

test("latest Vision turn shows every submitted image while draft cleanup only consumes manual images", () => {
  const projectVisionTurnImages = (
    pageIntegration as typeof pageIntegration & {
      projectVisionTurnImages?: (
        images: readonly string[],
        automationManualImageCount?: number,
      ) => {
        bubbleImages: string[];
        consumedManualImages: string[];
      };
    }
  ).projectVisionTurnImages;
  assert.equal(typeof projectVisionTurnImages, "function");

  const automationTurn = projectVisionTurnImages!(
    ["manual", "desktop", "camera"],
    1,
  );
  assert.deepEqual(automationTurn.bubbleImages, ["manual", "desktop", "camera"]);
  assert.deepEqual(automationTurn.consumedManualImages, ["manual"]);

  const automationOnlyTurn = projectVisionTurnImages!(["desktop", "camera"], 0);
  assert.deepEqual(automationOnlyTurn.bubbleImages, ["desktop", "camera"]);
  assert.deepEqual(automationOnlyTurn.consumedManualImages, []);

  const ordinaryTurn = projectVisionTurnImages!(["manual-1", "manual-2"]);
  assert.deepEqual(ordinaryTurn.bubbleImages, ["manual-1", "manual-2"]);
  assert.deepEqual(ordinaryTurn.consumedManualImages, ["manual-1", "manual-2"]);
});

test("non-Agent Vision Automation commits only canonical acceptance and releases only at stable Hub terminal", async () => {
  const createAutomationHubLifecycle = (
    pageIntegration as typeof pageIntegration & {
      createAutomationHubLifecycle?: (input: {
        requestId: string;
        onAccepted(requestId: string): void | Promise<void>;
        onStableTerminal(result: { requestId: string; status: "terminal" }): void | Promise<void>;
      }) => {
        onRequestAccepted(event: { requestId: string }): Promise<void>;
        onStableTerminal(result: { requestId: string; status: "terminal" }): Promise<void>;
      };
    }
  ).createAutomationHubLifecycle;
  assert.equal(typeof createAutomationHubLifecycle, "function");

  let accepted = 0;
  let finalContent = "partial final";
  let isLoading = true;
  let phase = "awaiting_model";
  let canRetrySubmit = true;
  let canRetryStatus = true;
  const lifecycle = createAutomationHubLifecycle!({
    requestId: "chat-automation-1",
    onAccepted: () => { accepted += 1; },
    onStableTerminal: () => {
      isLoading = false;
      phase = "capturing";
      canRetrySubmit = false;
      canRetryStatus = false;
    },
  });

  await lifecycle.onRequestAccepted({ requestId: "another-request" });
  assert.equal(accepted, 0, "foreign acceptance must never commit staged omission");
  await lifecycle.onRequestAccepted({ requestId: "chat-automation-1" });
  await lifecycle.onRequestAccepted({ requestId: "chat-automation-1" });
  assert.equal(accepted, 1);
  assert.equal(isLoading, true, "content/acceptance is not stable terminal");

  await lifecycle.onStableTerminal({ requestId: "chat-automation-1", status: "terminal" });
  assert.equal(finalContent, "partial final");
  assert.equal(isLoading, false);
  assert.equal(phase, "capturing");
  assert.equal(canRetrySubmit, false);
  assert.equal(canRetryStatus, false);
});

test("automation lifecycle claims acceptance and terminal before reentrant callbacks", async () => {
  const calls: string[] = [];
  let lifecycle!: ReturnType<typeof pageIntegration.createAutomationHubLifecycle>;
  lifecycle = pageIntegration.createAutomationHubLifecycle({
    requestId: "reentrant",
    onAccepted: async () => {
      calls.push("accepted");
      await lifecycle.onRequestAccepted({ requestId: "reentrant" });
    },
    onStableTerminal: async () => {
      calls.push("terminal");
      await lifecycle.onStableTerminal({ requestId: "reentrant", status: "terminal" });
    },
  });
  await lifecycle.onRequestAccepted({ requestId: "reentrant" });
  await lifecycle.onRequestAccepted({ requestId: "reentrant" });
  await lifecycle.onStableTerminal({ requestId: "reentrant", status: "terminal" });
  await lifecycle.onStableTerminal({ requestId: "reentrant", status: "terminal" });
  assert.deepEqual(calls, ["accepted", "terminal"]);
});

test("non-Agent automation shares one live attachment across terminal and Chat Hub ownership", () => {
  const attachVisionRequestTransport = (
    pageIntegration as typeof pageIntegration & {
      attachVisionRequestTransport?: (input: {
        mode: "chat" | "agent";
        contextId: string;
        attachChat(contextId: string): { sessionId: string; token: symbol };
        attachAgent(contextId: string): { sessionId: string; token: symbol };
      }) => {
        terminalAttachment: { sessionId: string; token: symbol };
        liveChatAttachment: { sessionId: string; token: symbol } | null;
        liveAgentAttachment: { sessionId: string; token: symbol } | null;
      };
    }
  ).attachVisionRequestTransport;
  assert.equal(typeof attachVisionRequestTransport, "function");

  let chatAttachCalls = 0;
  let agentAttachCalls = 0;
  const chatAttachment = { sessionId: "chat-context-1", token: Symbol("chat") };
  const attachments = attachVisionRequestTransport!({
    mode: "chat",
    contextId: chatAttachment.sessionId,
    attachChat: (contextId) => {
      chatAttachCalls += 1;
      assert.equal(contextId, chatAttachment.sessionId);
      return chatAttachment;
    },
    attachAgent: () => {
      agentAttachCalls += 1;
      return { sessionId: "unexpected-agent", token: Symbol("agent") };
    },
  });

  assert.equal(chatAttachCalls, 1, "one Chat request must register exactly one current token");
  assert.equal(agentAttachCalls, 0);
  assert.equal(attachments.terminalAttachment, chatAttachment);
  assert.equal(
    attachments.liveChatAttachment,
    attachments.terminalAttachment,
    "stable terminal and Hub callbacks must validate the same token",
  );
  assert.equal(attachments.liveAgentAttachment, null);

  const gate = new pageIntegration.AutomationTerminalGate("chat-automation-shared-token");
  let releases = 0;
  assert.equal(pageIntegration.completeAutomationTerminal(
    gate,
    { requestId: gate.requestId, status: "terminal" },
    undefined,
    () => { releases += 1; },
    attachments.terminalAttachment,
    (token) => token === attachments.liveChatAttachment,
  ), true);
  assert.equal(releases, 1, "the first round must release page busy state");
});

const config: VisionAutomationServerConfig = {
  enabled: true,
  intervalSeconds: 1,
  maxFramesPerSource: 4,
  thumbnailSizePx: 32,
  captureDuringInference: false,
  lockFirstBundle: false,
  tickTimeoutSeconds: 5,
  consecutiveTickFailureLimit: 3,
  maxEncodedPayloadBytes: 64 * 1024 * 1024,
  initialJpegQuality: 85,
  minimumJpegQuality: 40,
  minimumImageDimensionPx: 256,
  maxImageDimensionPx: 1024,
  base64EncodeConcurrency: 2,
  maxRequestBodyBytes: 75_497_472,
  requestStatusTtlSeconds: 600,
};

function descriptor(alias: "desktop" | "camera_1" | "camera_2"): SourceDescriptor {
  return {
    sourceId: alias,
    kind: alias === "desktop" ? "display" : "camera",
    stableAlias: alias,
    label: alias,
    ...(alias === "desktop" ? {} : { deviceId: alias }),
  };
}

function source(alias: "desktop" | "camera_1" | "camera_2", disposed: string[]): CaptureSource {
  return {
    descriptor: descriptor(alias),
    async capture() {
      return {
        source: descriptor(alias),
        resource: new OwnedImageResource(new Blob([alias], { type: "image/jpeg" })),
      };
    },
    onEnded() { return () => {}; },
    dispose() { disposed.push(alias); },
  };
}

async function runAcceptedAutomationStopScenario(mode: "chat" | "agent"): Promise<void> {
  const executeVisionStop = (
    pageIntegration as typeof pageIntegration & {
      executeVisionStop?: <T extends {
        gate: pageIntegration.AutomationTerminalGate;
        transport: AbortController;
      }>(input: {
        mode: "chat" | "agent";
        agentSessionId?: string;
        terminalEntries: Map<string, T>;
        requestIdRef: { current: string | undefined };
        transportRef: { current: AbortController | null };
        finalizeVisual(): void;
        resetStreamingState(): void;
        stopChat(requestId: string): Promise<unknown>;
        stopAgent(sessionId: string): Promise<unknown>;
      }) => Promise<{
        requestId: string | undefined;
        transport: AbortController | null;
        resetImmediately: boolean;
      }>;
    }
  ).executeVisionStop;
  assert.equal(typeof executeVisionStop, "function");

  let sequence = 0;
  let acceptedTransport: AbortController | null = null;
  const automationController = createVisionAutomationController({
    config: { ...config, intervalSeconds: 60 },
    acquireSources: async () => [source("camera_1", [])],
    restoreSource: async ({ source: failedSource }) => ({
      ...source("camera_1", []),
      descriptor: failedSource,
    }),
    getManualDataUrls: () => [],
    getMessages: () => [],
    commitMessages: () => {},
    isModelBusy: () => false,
    encodeFrame: async ({ blob }) => blob,
    blobToDataUrl: async () => "data:image/jpeg;base64,anBlZw==",
    getRequestStatus: async (requestId) => ({ requestId, status: "running" }),
    waitForRequestTerminal: async (requestId) => ({ requestId, status: "terminal" }),
    submit: async ({ controller, lifecycle, requestId }) => {
      acceptedTransport = controller;
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
    },
    createId: (kind) => `${kind}-${String(++sequence).padStart(16, "0")}`,
  });
  const contextId = mode === "agent" ? "agent-stop-context-1" : "chat-stop-context-1";
  await automationController.start({
    pageInstanceId: `page-stop-${mode}-1`,
    pageGeneration: 1,
    mode,
    contextId,
    automationTaskId: `task-stop-${mode}-0001`,
  });
  assert.equal(await automationController.send(`${mode} accepted inference`), true);
  const requestId = automationController.getState().activeRequestId;
  assert.ok(requestId);
  const transport = acceptedTransport as AbortController | null;
  assert.ok(transport);

  await automationController.abort({ registerAgentCleanup: false });
  assert.equal(transport.signal.aborted, false, "ordinary Automation Abort must not cancel accepted inference");

  const gate = new pageIntegration.AutomationTerminalGate(requestId);
  const transportRef: { current: AbortController | null } = { current: transport };
  const requestIdRef: { current: string | undefined } = { current: requestId };
  let isLoading = true;
  let visualStreaming = true;
  let resets = 0;
  const terminalEntries = new Map<string, {
    gate: pageIntegration.AutomationTerminalGate;
    transport: AbortController;
  }>();
  terminalEntries.set(requestId, { gate, transport });
  const release = (result: { requestId: string; status: "terminal" }) => {
    const entry = terminalEntries.get(requestId);
    if (entry?.gate !== gate) return;
    terminalEntries.delete(requestId);
    pageIntegration.completeAutomationTerminal(
      gate,
      result,
      transportRef.current === transport,
      () => {
        resets += 1;
        transportRef.current = null;
        requestIdRef.current = undefined;
        isLoading = false;
      },
    );
  };

  const originalFetch = globalThis.fetch;
  const stopRequests: Array<{ url: string; body: unknown }> = [];
  let statusCalls = 0;
  let failStatus = true;
  try {
    globalThis.fetch = async (url, init) => {
      const requestUrl = String(url);
      if (requestUrl.includes("/v1/request-status/")) {
        statusCalls += 1;
        if (statusCalls === 1) {
          return Response.json({ request_id: requestId, status: "accepted" });
        }
        if (statusCalls === 2) {
          return Response.json({ request_id: requestId, status: "running" });
        }
        if (failStatus) throw new Error("status unavailable after Stop");
        return Response.json({ request_id: requestId, status: "terminal" });
      }
      stopRequests.push({
        url: requestUrl,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      return Response.json({ status: "stopping" });
    };
    const client = new LLMClient("/api/backend");

    const stopResult = await executeVisionStop!({
      mode,
      agentSessionId: mode === "agent" ? contextId : undefined,
      terminalEntries,
      requestIdRef,
      transportRef,
      finalizeVisual: () => { visualStreaming = false; },
      resetStreamingState: () => {
        resets += 1;
        transportRef.current = null;
        requestIdRef.current = undefined;
        isLoading = false;
      },
      stopChat: (stoppedRequestId) => client.stopChat(stoppedRequestId),
      stopAgent: (sessionId) => client.stopAgent(sessionId),
    });
    gate.markCandidateComplete();

    assert.equal(stopResult.resetImmediately, false);
    assert.equal(stopResult.requestId, requestId);
    assert.equal(stopResult.transport, transport);
    assert.equal(transport.signal.aborted, true, "Stop exclusively cancels accepted inference");
    assert.equal(visualStreaming, false, "Stop still finalizes the visible stream immediately");
    assert.equal(requestIdRef.current, requestId);
    assert.equal(isLoading, true);
    assert.equal(resets, 0);
    assert.deepEqual(stopRequests, mode === "chat"
      ? [{ url: "/api/backend/v1/chat/stop", body: { request_id: requestId } }]
      : [{ url: "/api/backend/v1/agent/stop", body: { session_id: contextId } }]);

    const failedOutcome = await gate.pollForTerminal(
      (gateRequestId, signal) => client.waitForRequestTerminal(gateRequestId, {
        signal,
        pollIntervalMs: 0,
      }),
    );
    assert.deepEqual(failedOutcome, { kind: "failed", error: "status unavailable after Stop" });
    assert.equal(statusCalls, 3);
    assert.equal(gate.modelBusy, true);
    assert.equal(gate.canRetryStatus, true);
    assert.equal(isLoading, true, "status failure must retain page busy ownership");
    assert.equal(canStartVisionAutomation({
      enabled: true,
      cycleRunning: false,
      startGuardActive: false,
      armed: false,
      modelBusy: isLoading,
      pendingAcceptance: false,
    }), false);
    assert.equal(canResumeVisionVoiceCapture({
      modelBusy: isLoading,
      pendingAcceptance: false,
      pausedForResponse: true,
    }), false);
    assert.equal(!isLoading, false, "ordinary send/queue dispatch remains blocked");

    failStatus = false;
    const terminalOutcome = await gate.pollForTerminal(
      (gateRequestId, signal) => client.waitForRequestTerminal(gateRequestId, {
        signal,
        pollIntervalMs: 0,
      }),
    );
    assert.equal(terminalOutcome.kind, "terminal");
    if (terminalOutcome.kind === "terminal") release(terminalOutcome.result);
    assert.equal(isLoading, false);
    assert.equal(resets, 1, "matching stable terminal releases exactly once");
    release({ requestId, status: "terminal" });
    assert.equal(resets, 1);

    const replacementTransport = new AbortController();
    const replacementGate = new pageIntegration.AutomationTerminalGate("request-stop-replacement-1");
    transportRef.current = replacementTransport;
    requestIdRef.current = replacementGate.requestId;
    isLoading = true;
    terminalEntries.set(replacementGate.requestId, {
      gate: replacementGate,
      transport: replacementTransport,
    });
    release({ requestId, status: "terminal" });
    assert.equal(transportRef.current, replacementTransport);
    assert.equal(requestIdRef.current, replacementGate.requestId);
    assert.equal(isLoading, true, "stale terminal cannot release replacement ownership");
    assert.equal(resets, 1);
  } finally {
    globalThis.fetch = originalFetch;
    await automationController.dispose();
  }
}

test("Vision Stop keeps the provider stream attached until the targeted stop settles", async () => {
  for (const mode of ["chat", "agent"] as const) {
    const requestId = "stop-request-001";
    const transport = new AbortController();
    const stopReached = Promise.withResolvers<void>();
    const finishStop = Promise.withResolvers<void>();
    const calls: string[] = [];
    const stopping = pageIntegration.executeVisionStop({
      mode,
      agentSessionId: "agent-stop-session",
      terminalEntries: new Map(),
      requestIdRef: { current: requestId },
      transportRef: { current: transport },
      finalizeVisual: () => { calls.push("visual"); },
      resetStreamingState: () => { calls.push("reset"); },
      stopChat: async (id) => {
        assert.equal(id, requestId);
        calls.push("server");
        stopReached.resolve();
        await finishStop.promise;
      },
      stopAgent: async (id) => {
        assert.equal(id, "agent-stop-session");
        calls.push("server");
        stopReached.resolve();
        await finishStop.promise;
      },
    });
    await stopReached.promise;
    assert.equal(transport.signal.aborted, false, `${mode} transport must remain available for targeted stop`);
    assert.deepEqual(calls, ["server"], "the UI must remain live until the server confirms Stop");
    finishStop.resolve();
    await stopping;
    assert.equal(transport.signal.aborted, true);
    assert.deepEqual(calls, ["server", "visual", "reset"]);
  }
});

test("failed Vision Stop leaves Chat and Agent transport live for a retry", async () => {
  for (const mode of ["chat", "agent"] as const) {
    const transport = new AbortController();
    const visualChanges: string[] = [];
    await assert.rejects(pageIntegration.executeVisionStop({
      mode,
      agentSessionId: "agent-stop-session",
      terminalEntries: new Map(),
      requestIdRef: { current: "stop-request-001" },
      transportRef: { current: transport },
      finalizeVisual: () => { visualChanges.push("stopped"); },
      resetStreamingState: () => { visualChanges.push("reset"); },
      stopChat: async () => { throw new Error("targeted abort failed"); },
      stopAgent: async () => { throw new Error("targeted abort failed"); },
    }), /targeted abort failed/);
    assert.equal(transport.signal.aborted, false);
    assert.deepEqual(visualChanges, []);
  }
});

test("a delayed Agent Stop cannot finalize a newer Vision request", async () => {
  const oldTransport = new AbortController();
  const newTransport = new AbortController();
  const requestIdRef = { current: "old-request" };
  const transportRef: { current: AbortController | null } = { current: oldTransport };
  const reached = Promise.withResolvers<void>();
  const finish = Promise.withResolvers<void>();
  const effects: string[] = [];
  const stopping = pageIntegration.executeVisionStop({
    mode: "agent",
    agentSessionId: "same-session",
    terminalEntries: new Map(),
    requestIdRef,
    transportRef,
    finalizeVisual: () => { effects.push("visual"); },
    resetStreamingState: () => { effects.push("reset"); },
    stopChat: async () => { throw new Error("wrong mode"); },
    stopAgent: async (_sessionId, expectedRequestId) => {
      assert.equal(expectedRequestId, "old-request");
      reached.resolve();
      await finish.promise;
    },
  });
  await reached.promise;
  requestIdRef.current = "new-request";
  transportRef.current = newTransport;
  finish.resolve();
  await stopping;
  assert.equal(oldTransport.signal.aborted, true);
  assert.equal(newTransport.signal.aborted, false);
  assert.deepEqual(effects, []);
  assert.equal(requestIdRef.current, "new-request");
});

test("Agent Stop sends the clicked request ID with its session", async () => {
  const originalFetch = globalThis.fetch;
  let body: unknown;
  try {
    globalThis.fetch = async (_url, init) => {
      body = JSON.parse(String(init?.body));
      return Response.json({ status: "stopped" });
    };
    await new LLMClient("/api/backend").stopAgent("same-session", "clicked-request");
    assert.deepEqual(body, {
      session_id: "same-session",
      expected_request_id: "clicked-request",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Vision Agent Stop refuses to target a session without a clicked request ID", async () => {
  let stopCalls = 0;
  await assert.rejects(pageIntegration.executeVisionStop({
    mode: "agent",
    agentSessionId: "same-session",
    terminalEntries: new Map(),
    requestIdRef: { current: undefined },
    transportRef: { current: new AbortController() },
    finalizeVisual: () => { throw new Error("visual changed"); },
    resetStreamingState: () => { throw new Error("state reset"); },
    stopChat: async () => { throw new Error("wrong mode"); },
    stopAgent: async () => { stopCalls += 1; },
  }), /request ID/);
  assert.equal(stopCalls, 0);
});

test("acceptance mutation preserves draft and manual images until one accepted commit", () => {
  const registry = new AcceptanceMutationRegistry<{ text: string; images: readonly string[] }>();
  const page = { text: "inspect", images: ["manual-1"], messages: [] as string[] };
  const first = registry.stage("request-00000001", () => ({
    text: page.text,
    images: [...page.images],
  }));
  const retry = registry.stage("request-00000001", () => {
    throw new Error("retry must reuse the staged mutation");
  });

  assert.equal(first, retry);
  assert.deepEqual(page, { text: "inspect", images: ["manual-1"], messages: [] });
  assert.equal(registry.accept("request-00000001", (snapshot) => {
    page.messages.push(`${snapshot.text}:${snapshot.images.join(",")}`);
    page.text = "";
    page.images = [];
  }), true);
  assert.equal(registry.accept("request-00000001", () => {
    throw new Error("duplicate acceptance must not mutate the page");
  }), false);
  assert.deepEqual(page, { text: "", images: [], messages: ["inspect:manual-1"] });
});

test("pre-accept failure and busy rejection preserve the staged page mutation", () => {
  const registry = new AcceptanceMutationRegistry<{ text: string; images: readonly string[] }>();
  const page = { text: "keep", images: ["manual"] };
  registry.stage("request-00000002", () => ({ text: page.text, images: [...page.images] }));

  assert.equal(registry.has("request-00000002"), true);
  assert.deepEqual(page, { text: "keep", images: ["manual"] });
});

test("mixed-source startup adopts successes and reports failures without leaking stale leases", async () => {
  const disposed: string[] = [];
  const result = await acquireAvailableVisionSources([
    { source: descriptor("desktop"), acquire: async () => source("desktop", disposed) },
    { source: descriptor("camera_1"), acquire: async () => { throw new Error("denied"); } },
    { source: descriptor("camera_2"), acquire: async () => source("camera_2", disposed) },
  ], new AbortController().signal, () => true);

  assert.deepEqual(result.sources.map((item) => item.descriptor.stableAlias), ["desktop", "camera_2"]);
  assert.deepEqual(result.failures.map((item) => item.source.stableAlias), ["camera_1"]);
  assert.deepEqual(disposed, []);

  const staleDisposed: string[] = [];
  await assert.rejects(() => acquireAvailableVisionSources([
    { source: descriptor("desktop"), acquire: async () => source("desktop", staleDisposed) },
    { source: descriptor("camera_1"), acquire: async () => source("camera_1", staleDisposed) },
  ], new AbortController().signal, () => false), /stale|aborted/i);
  assert.deepEqual(staleDisposed, ["desktop", "camera_1"]);
});

test("Agent response session identity must remain equal to the bound session", () => {
  assert.equal(assertBoundAgentSessionId("agent-session-0001", "agent-session-0001"), "agent-session-0001");
  assert.throws(
    () => assertBoundAgentSessionId("agent-session-0001", "agent-session-0002"),
    /identity/i,
  );
});

test("Cycle guard closes the synchronous start and delayed execution race", () => {
  const guard = new CycleStartGuard();
  const first = guard.begin();
  assert.ok(first);
  assert.equal(guard.begin(), null);
  assert.equal(guard.isCurrent(first), true);
  guard.cancel();
  assert.equal(guard.isCurrent(first), false);
  assert.ok(guard.begin());
});

test("Stop cannot expose Trigger Start while a cleanup-bound ASR send is still unresolved", () => {
  const base = {
    enabled: true,
    cycleRunning: false,
    startGuardActive: false,
    armed: false,
    modelBusy: false,
    pendingAcceptance: false,
  };

  assert.equal(canStartVisionAutomation(base), true);
  assert.equal(canStartVisionAutomation({ ...base, modelBusy: true }), false);
  assert.equal(canStartVisionAutomation({ ...base, pendingAcceptance: true }), false);
});

test("ASR remains paused after Stop until cleanup-bound acceptance resolves", () => {
  const base = {
    modelBusy: false,
    pendingAcceptance: false,
    dictationMagicSendPending: false,
    pausedForResponse: true,
  };
  assert.equal(canResumeVisionVoiceCapture(base), true);
  assert.equal(canResumeVisionVoiceCapture({ ...base, pendingAcceptance: true }), false);
  assert.equal(canResumeVisionVoiceCapture({ ...base, dictationMagicSendPending: true }), false);
});

test("hidden pages resume idle voice capture while preserving request and magic-send guards", () => {
  const hiddenPage = {
    modelBusy: false,
    pendingAcceptance: false,
    dictationMagicSendPending: false,
    pausedForResponse: true,
    pageVisible: false,
  };
  assert.equal(canResumeVisionVoiceCapture(hiddenPage), true);
  assert.equal(canResumeVisionVoiceCapture({ ...hiddenPage, modelBusy: true }), false);
  assert.equal(canResumeVisionVoiceCapture({ ...hiddenPage, pendingAcceptance: true }), false);
  assert.equal(canResumeVisionVoiceCapture({ ...hiddenPage, dictationMagicSendPending: true }), false);
  assert.equal(canResumeVisionVoiceCapture({ ...hiddenPage, pausedForResponse: false }), false);
});

test("visible Vision user text excludes only the located generated placeholder", () => {
  const exactText = "ASR  exact text\nsecond line";
  const placeholder = "[historical images omitted: count=1; sources=manual; relative_range=unavailable]";
  assert.equal(getVisibleVisionUserText({
    id: "omitted",
    role: "user",
    content: [
      { type: "text", text: placeholder },
      { type: "text", text: exactText },
    ],
    timestamp: new Date(0),
    historicalImageSummaries: [{
      count: 1,
      sources: ["manual"],
      placeholder,
      contentPartIndex: 0,
    }],
  }), exactText);
  assert.equal(getVisibleVisionUserText({
    id: "literal",
    role: "user",
    content: [{ type: "text", text: placeholder }],
    timestamp: new Date(0),
  }), placeholder);
});

test("user-authored identical placeholder before the generated part remains visible", () => {
  const placeholder = "[historical images omitted: count=1; sources=manual; relative_range=unavailable]";
  const [committed] = stageChatHistoryImageOmission([{
    id: "literal-before",
    role: "user",
    content: [
      { type: "text", text: placeholder },
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,generated" } },
    ],
    timestamp: new Date(0),
  }], () => ({ sources: ["manual"] })).commit();

  assert.equal(committed.historicalImageSummaries?.[0].contentPartIndex, 1);
  assert.equal(getVisibleVisionUserText(committed), placeholder);
});

test("user-authored identical placeholder after the generated part remains visible", () => {
  const placeholder = "[historical images omitted: count=1; sources=manual; relative_range=unavailable]";
  const [committed] = stageChatHistoryImageOmission([{
    id: "literal-after",
    role: "user",
    content: [
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,generated" } },
      { type: "text", text: placeholder },
    ],
    timestamp: new Date(0),
  }], () => ({ sources: ["manual"] })).commit();

  assert.equal(committed.historicalImageSummaries?.[0].contentPartIndex, 0);
  assert.equal(getVisibleVisionUserText(committed), placeholder);
});

test("generated placeholder identity is positional rather than string-based", () => {
  const placeholder = "[historical images omitted: count=1; sources=manual; relative_range=unavailable]";
  assert.equal(getVisibleVisionUserText({
    id: "located-placeholder",
    role: "user",
    content: [
      { type: "text", text: placeholder },
      { type: "text", text: "generated part selected only by its immutable locator" },
    ],
    timestamp: new Date(0),
    historicalImageSummaries: [{
      count: 1,
      sources: ["manual"],
      placeholder,
      contentPartIndex: 1,
    }],
  }), placeholder);
});

test("pagehide invalidates and aborts without disposing a BFCache-restored controller", async () => {
  let ids = 0;
  const lifecycle = new VisionPageLifecycle(() => `context-${++ids}`);
  let aborts = 0;
  let disposals = 0;
  const originalContext = lifecycle.contextId;

  await lifecycle.pageHide({
    abort: async () => { aborts += 1; },
    dispose: async () => { disposals += 1; },
  });
  assert.equal(lifecycle.generation, 1);
  assert.notEqual(lifecycle.contextId, originalContext);
  assert.equal(aborts, 1);
  assert.equal(disposals, 0);

  await lifecycle.unmount({
    abort: async () => { aborts += 1; },
    dispose: async () => { disposals += 1; },
  });
  assert.equal(aborts, 2);
  assert.equal(disposals, 1);
});

test("durable page generations increase across lifecycle instances and invalidation", () => {
  const createAllocator = (
    pageIntegration as typeof pageIntegration & {
      createDurableVisionPageGenerationAllocator?: (
        storage: { getItem(key: string): string | null; setItem(key: string, value: string): void },
        now?: () => number,
      ) => () => number;
    }
  ).createDurableVisionPageGenerationAllocator;
  assert.equal(typeof createAllocator, "function");
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
  };
  const allocator = createAllocator!(storage, () => 1_000);
  const first = new VisionPageLifecycle(() => "context-1");
  assert.equal(first.generation, 0);
  assert.equal(values.size, 0, "construction must not claim durable storage");
  first.bindGenerationClaimant(allocator);
  const second = new VisionPageLifecycle(() => "context-2");
  second.bindGenerationClaimant(allocator);

  assert.ok(second.generation > first.generation);
  const beforeInvalidate = second.generation;
  second.invalidate();
  assert.ok(second.generation > beforeInvalidate);
  assert.ok(Array.from(values.values()).includes(String(second.generation)));
});

test("Vision page lifecycle accepts an initial claimed generation of zero", () => {
  const claims = [0, 1];
  let claimCalls = 0;
  const claimed = new VisionPageLifecycle(() => "claimed-context");
  assert.equal(claimed.generation, 0);
  claimed.bindGenerationClaimant(() => {
    claimCalls += 1;
    return claims.shift()!;
  });
  claimed.bindGenerationClaimant(() => {
    claimCalls += 100;
    return 100;
  });
  assert.equal(claimCalls, 1, "StrictMode effect replay must not claim twice");
  assert.equal(claimed.generation, 0);
  claimed.invalidate();
  assert.equal(claimCalls, 2);
  assert.equal(claimed.generation, 1);

  const defaultLifecycle = new VisionPageLifecycle(() => "default-context");
  assert.equal(defaultLifecycle.generation, 0);
  defaultLifecycle.invalidate();
  assert.equal(defaultLifecycle.generation, 1);
});

test("durable page generation safely recovers from malformed or unavailable storage", () => {
  const createAllocator = (
    pageIntegration as typeof pageIntegration & {
      createDurableVisionPageGenerationAllocator?: (
        storage: { getItem(key: string): string | null; setItem(key: string, value: string): void },
        now?: () => number,
      ) => () => number;
    }
  ).createDurableVisionPageGenerationAllocator;
  assert.equal(typeof createAllocator, "function");
  const malformedValues = new Map([["llm-deployment.vision-page-generation.v1", "not-an-integer"]]);
  const malformed = createAllocator!({
    getItem: (key) => malformedValues.get(key) ?? null,
    setItem: (key, value) => { malformedValues.set(key, value); },
  }, () => 2_000);
  assert.equal(malformed(), 2_000);
  assert.equal(malformed(), 2_001);

  const unavailable = createAllocator!({
    getItem: () => { throw new Error("storage unavailable"); },
    setItem: () => { throw new Error("storage unavailable"); },
  }, () => 3_000);
  assert.equal(unavailable(), 3_000);
  assert.equal(unavailable(), 3_001);
});

test("Vision page wires durable generation allocation without expanding Hub bookmarks", async () => {
  const source = await readFile(new URL("../../app/vision/page.tsx", import.meta.url), "utf8");
  assert.match(source, /createDurableVisionPageGenerationAllocator/);
  assert.doesNotMatch(source, /useRef\(new VisionPageLifecycle/);
  assert.doesNotMatch(source, /pageGenerationClaimRef/);
  assert.match(
    source,
    /const pageLifecycleRef = useRef<VisionPageLifecycle \| null>\(null\);\s*if\s*\(pageLifecycleRef\.current === null\) \{\s*pageLifecycleRef\.current = new VisionPageLifecycle\(\s*\(\) => createAutomationLifecycleId\('chat-context'\),\s*\);\s*\}\s*const pageLifecycle = pageLifecycleRef\.current;/,
  );
  const lifecycleInitializer = source.slice(
    source.indexOf("const pageLifecycleRef"),
    source.indexOf("useEffect(() => {", source.indexOf("const pageLifecycleRef")),
  );
  assert.doesNotMatch(lifecycleInitializer, /localStorage|createDurableVisionPageGenerationAllocator/);
  assert.match(source, /const \[pageGenerationReady, setPageGenerationReady\] = useState\(false\);/);
  assert.match(source, /useEffect\(\(\) => \{\s*if\s*\(!visionCapabilityConfirmed\) return;[\s\S]*window\.localStorage[\s\S]*createDurableVisionPageGenerationAllocator\(storage\)[\s\S]*pageLifecycle\.bindGenerationClaimant\(claimGeneration\);[\s\S]*setPageGenerationReady\(true\);[\s\S]*\}, \[pageLifecycle, visionCapabilityConfirmed\]\);/);
  assert.match(source, /const pageLifecycleCleanupTimerRef = useRef<number \| null>\(null\);/);
  assert.match(source, /if\s*\(pageLifecycleCleanupTimerRef\.current !== null\) \{\s*clearTimeout\(pageLifecycleCleanupTimerRef\.current\);\s*pageLifecycleCleanupTimerRef\.current = null;\s*\}/);
  assert.match(source, /pageLifecycleCleanupTimerRef\.current = window\.setTimeout\(\(\) => \{[\s\S]*pageLifecycle\.unmount/);
  assert.match(source, /if\s*\(!pageGenerationReady \|\| visionAutomation\.controller\.hasCleanupSendInFlight\(\)/);
  assert.match(source, /startDisabled=\{!pageGenerationReady\}/);
  assert.doesNotMatch(source, /writeSessionHubBookmark\([\s\S]*pageGeneration/);
});

test("automation candidate completion keeps ASR queue send and cycle eligibility blocked until terminal", () => {
  const Gate = (
    pageIntegration as typeof pageIntegration & {
      AutomationTerminalGate?: new (requestId: string) => {
        readonly requestId: string;
        markCandidateComplete(): void;
        markStableTerminal(result: { requestId: string; status: "terminal" }): boolean;
        readonly modelBusy: boolean;
        readonly candidateComplete: boolean;
      };
    }
  ).AutomationTerminalGate;
  assert.equal(typeof Gate, "function");
  const gate = new Gate!("request-candidate-1");
  assert.equal(gate.requestId, "request-candidate-1");

  gate.markCandidateComplete();
  assert.equal(gate.candidateComplete, true, "final text may finalize visually");
  assert.equal(gate.modelBusy, true);
  assert.equal(canResumeVisionVoiceCapture({
    modelBusy: gate.modelBusy,
    pendingAcceptance: false,
    pausedForResponse: true,
  }), false);
  assert.equal(canStartVisionAutomation({
    enabled: true,
    cycleRunning: false,
    startGuardActive: false,
    armed: false,
    modelBusy: gate.modelBusy,
    pendingAcceptance: false,
  }), false);
  assert.equal(!gate.modelBusy, false, "queue and ordinary send remain ineligible");

  const terminal = { requestId: "request-candidate-1", status: "terminal" as const };
  assert.equal(gate.markStableTerminal(terminal), true);
  assert.equal(gate.markStableTerminal(terminal), false, "terminal release is exactly once");
  assert.equal(gate.modelBusy, false);
  assert.equal(canResumeVisionVoiceCapture({
    modelBusy: gate.modelBusy,
    pendingAcceptance: false,
    pausedForResponse: true,
  }), true);
});

test("effective Vision busy remains locked between candidate completion and stable terminal", () => {
  const isVisionRequestBusy = (
    pageIntegration as typeof pageIntegration & {
      isVisionRequestBusy?: (
        streamingBusy: boolean,
        terminalEntries: Iterable<{ gate: { modelBusy: boolean } }>,
      ) => boolean;
    }
  ).isVisionRequestBusy;
  assert.equal(typeof isVisionRequestBusy, "function");

  const gate = new pageIntegration.AutomationTerminalGate("request-second-round-1");
  const entries = new Map([[gate.requestId, { gate }]]);
  gate.markCandidateComplete();

  assert.equal(
    isVisionRequestBusy!(false, entries.values()),
    true,
    "ASR and the next capture cycle must stay blocked after visual completion",
  );

  gate.markStableTerminal({ requestId: gate.requestId, status: "terminal" });
  assert.equal(isVisionRequestBusy!(false, entries.values()), false);
  assert.equal(isVisionRequestBusy!(true, []), true);
});

test("accepted automation Chat Stop stays gate-owned until exact stable terminal", async () => {
  await runAcceptedAutomationStopScenario("chat");
});

test("accepted automation Agent Stop stays gate-owned until exact stable terminal", async () => {
  await runAcceptedAutomationStopScenario("agent");
});

test("normal non-automation Stop keeps its immediate reset timing", () => {
  const transport = new AbortController();
  let resets = 0;

  assert.equal(pageIntegration.resetStreamingStateAfterStop({
    terminalEntries: new Map(),
    requestId: "request-normal-stop-1",
    transport,
    resetStreamingState: () => { resets += 1; },
  }), true);
  assert.equal(resets, 1);
});

test("a stale automation terminal retires its gate without releasing newer page transport state", () => {
  const completeAutomationTerminal = (
    pageIntegration as typeof pageIntegration & {
      completeAutomationTerminal?: (
        gate: pageIntegration.AutomationTerminalGate,
        result: { requestId: string; status: "terminal" },
        isCurrentTransport: boolean,
        release: () => void,
      ) => boolean;
    }
  ).completeAutomationTerminal;
  assert.equal(typeof completeAutomationTerminal, "function");
  const staleGate = new pageIntegration.AutomationTerminalGate("request-stale-1");
  let releases = 0;

  assert.equal(completeAutomationTerminal?.(
    staleGate,
    { requestId: "request-stale-1", status: "terminal" },
    false,
    () => { releases += 1; },
  ), false);
  assert.equal(staleGate.modelBusy, false, "the old lifecycle itself reached terminal");
  assert.equal(releases, 0, "new page transport state must remain untouched");
});

test("cleanup candidate polling failure retains its authoritative gate and exposes Retry Status", async () => {
  const gate = new pageIntegration.AutomationTerminalGate("cleanup-request-1");
  gate.markCandidateComplete();
  const queried: string[] = [];

  const outcome = await gate.pollForTerminal(async (requestId) => {
    queried.push(requestId);
    throw new Error("status unavailable");
  });

  assert.deepEqual(outcome, { kind: "failed", error: "status unavailable" });
  assert.deepEqual(queried, ["cleanup-request-1"]);
  assert.equal(gate.modelBusy, true);
  assert.equal(gate.canRetryStatus, true);
  assert.match(gate.pollError ?? "", /status unavailable/);
});

test("applied mode change aborts the exact old page transport before retirement starts", async () => {
  const abortPageTransportBeforeRetirement = (
    pageIntegration as typeof pageIntegration & {
      abortPageTransportBeforeRetirement?: (
        controller: AbortController | null,
        retire: () => Promise<void>,
      ) => Promise<void>;
    }
  ).abortPageTransportBeforeRetirement;
  assert.equal(typeof abortPageTransportBeforeRetirement, "function");
  const controller = new AbortController();
  const events: string[] = [];

  await abortPageTransportBeforeRetirement?.(controller, async () => {
    events.push(`retire:${String(controller.signal.aborted)}`);
  });

  assert.equal(controller.signal.aborted, true);
  assert.deepEqual(events, ["retire:true"]);
});

test("stale transport retirement preserves terminal ownership installed by a newer request", () => {
  const oldTransport = new AbortController();
  const newerTransport = new AbortController();
  const newerGate = new pageIntegration.AutomationTerminalGate("request-newer-owner-1");
  const oldToken = { sessionId: "old", token: Symbol("old") };
  const newerToken = { sessionId: "newer", token: Symbol("newer") };
  const terminalEntries = new Map([
    [newerGate.requestId, { gate: newerGate }],
  ]);
  let resets = 0;

  const result = pageIntegration.retireAutomationTransportOwnership({
    terminalEntries,
    transportToken: oldToken,
    isCurrentToken: (token) => token === newerToken,
    retireCurrentTransport: () => { resets += 1; },
  });

  assert.equal(result, false);
  assert.equal(terminalEntries.size, 1);
  assert.equal(terminalEntries.get(newerGate.requestId)?.gate, newerGate);
  assert.equal(newerGate.modelBusy, true);
  assert.equal(resets, 0);

  const nullResult = pageIntegration.retireAutomationTransportOwnership({
    terminalEntries,
    transportToken: oldToken,
    isCurrentToken: () => false,
    retireCurrentTransport: () => { resets += 1; },
  });
  assert.equal(nullResult, false);
  assert.equal(terminalEntries.size, 1, "null cleanup state has no destructive ownership");
  assert.equal(resets, 0);
});

test("token-owned retirement rejects stale transport even when its controller is still supplied", () => {
  const oldTransport = new AbortController();
  const oldToken = { sessionId: "agent-old", token: Symbol("old") };
  const currentToken = { sessionId: "agent-new", token: Symbol("new") };
  const gate = new pageIntegration.AutomationTerminalGate("request-token-retirement-1");
  const terminalEntries = new Map([[gate.requestId, { gate }]]);
  let retired = 0;

  const result = pageIntegration.retireAutomationTransportOwnership({
    terminalEntries,
    transportToken: oldToken,
    isCurrentToken: (token) => token === currentToken,
    retireCurrentTransport: () => { retired += 1; },
  });

  assert.equal(result, false);
  assert.equal(retired, 0);
  assert.equal(terminalEntries.size, 1);
  assert.equal(gate.modelBusy, true);
});

test("retirement cancels a pending terminal poll without terminal success or retry state", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let statusCalls = 0;
  let statusSignal: AbortSignal | undefined;
  globalThis.fetch = async (url, init) => {
    assert.match(String(url), /\/v1\/request-status\/request-pending-retirement-1$/);
    statusCalls += 1;
    statusSignal = init?.signal as AbortSignal | undefined;
    return Response.json({
      request_id: "request-pending-retirement-1",
      status: statusCalls === 1 ? "running" : "terminal",
    });
  };

  const client = new LLMClient("/api/backend");
  const transport = new AbortController();
  const gate = new pageIntegration.AutomationTerminalGate("request-pending-retirement-1");
  let transportRetires = 0;
  let terminalReleases = 0;
  const terminalEntries = new Map([
    [gate.requestId, {
      gate,
      release: () => { terminalReleases += 1; },
    }],
  ]);
  const polling = gate.pollForTerminal(
    (requestId, signal) => client.waitForRequestTerminal(requestId, {
      signal,
      pollIntervalMs: 5,
    }),
  );
  while (statusCalls === 0) await Promise.resolve();

  const retired = pageIntegration.retireAutomationTransportOwnership({
    terminalEntries,
    transportToken: { sessionId: "pending", token: Symbol("pending") },
    isCurrentToken: () => true,
    retireCurrentTransport: () => { transportRetires += 1; },
  });
  const outcome = await polling;
  await new Promise((resolve) => setTimeout(resolve, 10));

  assert.equal(retired, true);
  assert.equal(terminalEntries.size, 0);
  assert.deepEqual(outcome, { kind: "cancelled" });
  assert.equal(statusCalls, 1, "retirement must stop repeated status traffic");
  assert.equal(statusSignal?.aborted, true);
  assert.equal(transportRetires, 1, "replacement transport state is retired separately");
  assert.equal(terminalReleases, 0, "cancellation must not fabricate terminal release");
  assert.equal(gate.modelBusy, true, "disposed is not stable terminal");
  assert.equal(gate.canRetryStatus, false, "intentional cancellation is not a polling failure");
  assert.equal(gate.pollError, null);
});

test("Answer Now replacement disposal preserves its fresh gate and normal terminal path", () => {
  const originalGate = new pageIntegration.AutomationTerminalGate("request-answer-original-1");
  const replacementGate = new pageIntegration.AutomationTerminalGate("request-answer-replacement-1");
  const terminalEntries = new Map([
    [originalGate.requestId, { gate: originalGate }],
    [replacementGate.requestId, { gate: replacementGate }],
  ]);

  assert.equal(pageIntegration.disposeAutomationTerminalEntry(
    terminalEntries,
    originalGate.requestId,
    originalGate,
  ), true);

  assert.equal(terminalEntries.size, 1);
  assert.equal(terminalEntries.get(replacementGate.requestId)?.gate, replacementGate);
  assert.equal(originalGate.signal.aborted, true);
  assert.equal(originalGate.modelBusy, true, "retired original is not terminal");
  assert.equal(replacementGate.signal.aborted, false);
  assert.equal(replacementGate.markStableTerminal({
    requestId: replacementGate.requestId,
    status: "terminal",
  }), true);
  assert.equal(replacementGate.modelBusy, false);
});

test("known cleanup rejection retires only its local gate and keeps the staged token retryable", () => {
  const gate = new pageIntegration.AutomationTerminalGate("cleanup-known-rejection-1");
  const terminalEntries = new Map([[gate.requestId, { gate }]]);
  let finishAttempts = 0;
  let visibleError = "";
  const handled = pageIntegration.settleKnownCleanupRejection({
    isCurrentAttempt: true,
    terminalEntries,
    requestId: gate.requestId,
    stage: { finishAttempt: () => { finishAttempts += 1; } },
    message: "Agent request rejected (503): temporary unavailable",
    onVisibleError: (message) => { visibleError = message; },
  });

  assert.equal(handled, true);
  assert.equal(finishAttempts, 1);
  assert.equal(terminalEntries.size, 0);
  assert.equal(gate.signal.aborted, true);
  assert.equal(visibleError, "Agent request rejected (503): temporary unavailable");
});

test("accepted Answer Now replacement Stop uses synchronized replacement ownership", async () => {
  const installVisionRequestOwnership = (
    pageIntegration as typeof pageIntegration & {
      installVisionRequestOwnership?: <T extends {
        gate: pageIntegration.AutomationTerminalGate;
        transport: AbortController;
      }>(input: {
        terminalEntries: Map<string, T>;
        previousRequestId: string | undefined;
        requestId: string;
        transport: AbortController;
        terminalEntry?: T;
        requestIdRef: { current: string | undefined };
        transportRef: { current: AbortController | null };
        setCurrentRequestId(requestId: string): void;
      }) => void;
      executeVisionStop?: <U extends {
        gate: pageIntegration.AutomationTerminalGate;
        transport: AbortController;
      }>(input: {
        mode: "agent";
        agentSessionId: string;
        terminalEntries: Map<string, U>;
        requestIdRef: { current: string | undefined };
        transportRef: { current: AbortController | null };
        finalizeVisual(): void;
        resetStreamingState(): void;
        stopChat(requestId: string): Promise<unknown>;
        stopAgent(sessionId: string): Promise<unknown>;
      }) => Promise<{ resetImmediately: boolean; requestId: string | undefined }>;
    }
  ).installVisionRequestOwnership;
  const executeVisionStop = (
    pageIntegration as typeof pageIntegration & {
      executeVisionStop?: <T extends {
        gate: pageIntegration.AutomationTerminalGate;
        transport: AbortController;
      }>(input: {
        mode: "agent";
        agentSessionId: string;
        terminalEntries: Map<string, T>;
        requestIdRef: { current: string | undefined };
        transportRef: { current: AbortController | null };
        finalizeVisual(): void;
        resetStreamingState(): void;
        stopChat(requestId: string): Promise<unknown>;
        stopAgent(sessionId: string): Promise<unknown>;
      }) => Promise<{ resetImmediately: boolean; requestId: string | undefined }>;
    }
  ).executeVisionStop;
  assert.equal(typeof installVisionRequestOwnership, "function");
  assert.equal(typeof executeVisionStop, "function");

  const originalRequestId = "request-answer-now-original-1";
  const replacementRequestId = "request-answer-now-replacement-1";
  const originalTransport = new AbortController();
  const replacementTransport = new AbortController();
  const originalGate = new pageIntegration.AutomationTerminalGate(originalRequestId);
  const replacementGate = new pageIntegration.AutomationTerminalGate(replacementRequestId);
  const requestIdRef: { current: string | undefined } = { current: originalRequestId };
  const transportRef: { current: AbortController | null } = { current: originalTransport };
  let renderedCurrentRequestId = originalRequestId;
  const scheduledRequestIds: string[] = [];
  let isLoading = true;
  let visualStreaming = true;
  let resets = 0;
  const terminalEntries = new Map<string, {
    gate: pageIntegration.AutomationTerminalGate;
    transport: AbortController;
  }>([[originalRequestId, { gate: originalGate, transport: originalTransport }]]);
  const resetStreamingState = () => {
    resets += 1;
    requestIdRef.current = undefined;
    transportRef.current = null;
    isLoading = false;
  };
  const releaseReplacement = (result: { requestId: string; status: "terminal" }) => {
    const entry = terminalEntries.get(replacementRequestId);
    if (entry?.gate !== replacementGate) return;
    terminalEntries.delete(replacementRequestId);
    pageIntegration.completeAutomationTerminal(
      replacementGate,
      result,
      transportRef.current === replacementTransport,
      resetStreamingState,
    );
  };

  installVisionRequestOwnership!({
    terminalEntries,
    previousRequestId: originalRequestId,
    requestId: replacementRequestId,
    transport: replacementTransport,
    terminalEntry: { gate: replacementGate, transport: replacementTransport },
    requestIdRef,
    transportRef,
    setCurrentRequestId: (requestId) => { scheduledRequestIds.push(requestId); },
  });

  assert.equal(renderedCurrentRequestId, originalRequestId, "React render may still expose the superseded ID");
  assert.deepEqual(scheduledRequestIds, [replacementRequestId]);
  assert.equal(requestIdRef.current, replacementRequestId);
  assert.equal(transportRef.current, replacementTransport);
  assert.equal(terminalEntries.has(originalRequestId), false);
  assert.equal(originalGate.signal.aborted, true);
  assert.equal(terminalEntries.get(replacementRequestId)?.gate, replacementGate);

  const originalFetch = globalThis.fetch;
  let statusCalls = 0;
  let failStatus = true;
  const stopBodies: unknown[] = [];
  try {
    globalThis.fetch = async (url, init) => {
      if (String(url).includes("/v1/request-status/")) {
        statusCalls += 1;
        if (statusCalls === 1) {
          return Response.json({ request_id: replacementRequestId, status: "accepted" });
        }
        if (statusCalls === 2) {
          return Response.json({ request_id: replacementRequestId, status: "running" });
        }
        if (failStatus) throw new Error("replacement status unavailable");
        return Response.json({ request_id: replacementRequestId, status: "terminal" });
      }
      stopBodies.push(init?.body ? JSON.parse(String(init.body)) : undefined);
      return Response.json({ status: "stopping" });
    };
    const client = new LLMClient("/api/backend");
    const stopResult = await executeVisionStop!({
      mode: "agent",
      agentSessionId: "agent-answer-now-session-1",
      terminalEntries,
      requestIdRef,
      transportRef,
      finalizeVisual: () => { visualStreaming = false; },
      resetStreamingState,
      stopChat: (requestId) => client.stopChat(requestId),
      stopAgent: (sessionId) => client.stopAgent(sessionId),
    });
    replacementGate.markCandidateComplete();

    assert.equal(stopResult.requestId, replacementRequestId);
    assert.equal(stopResult.resetImmediately, false);
    assert.equal(replacementTransport.signal.aborted, true);
    assert.equal(visualStreaming, false);
    assert.equal(isLoading, true);
    assert.equal(resets, 0);
    assert.deepEqual(stopBodies, [{ session_id: "agent-answer-now-session-1" }]);

    assert.equal(pageIntegration.completeAutomationTerminal(
      originalGate,
      { requestId: originalRequestId, status: "terminal" },
      false,
      resetStreamingState,
    ), false);
    assert.equal(isLoading, true, "superseded original terminal cannot release replacement");
    assert.equal(resets, 0);

    const failedOutcome = await replacementGate.pollForTerminal(
      (requestId, signal) => client.waitForRequestTerminal(requestId, {
        signal,
        pollIntervalMs: 0,
      }),
    );
    assert.deepEqual(failedOutcome, { kind: "failed", error: "replacement status unavailable" });
    assert.equal(replacementGate.modelBusy, true);
    assert.equal(replacementGate.canRetryStatus, true);
    assert.equal(isLoading, true);

    failStatus = false;
    const terminalOutcome = await replacementGate.pollForTerminal(
      (requestId, signal) => client.waitForRequestTerminal(requestId, {
        signal,
        pollIntervalMs: 0,
      }),
    );
    assert.equal(terminalOutcome.kind, "terminal");
    if (terminalOutcome.kind === "terminal") releaseReplacement(terminalOutcome.result);
    assert.equal(isLoading, false);
    assert.equal(resets, 1);
    releaseReplacement({ requestId: replacementRequestId, status: "terminal" });
    assert.equal(resets, 1, "replacement terminal releases exactly once");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("pre-accept Answer Now ownership keeps the original gate available for rollback", () => {
  const originalGate = new pageIntegration.AutomationTerminalGate("request-answer-original-rollback");
  const replacementGate = new pageIntegration.AutomationTerminalGate("request-answer-replacement-rollback");
  const originalTransport = new AbortController();
  const replacementTransport = new AbortController();
  const terminalEntries = new Map([
    [originalGate.requestId, { gate: originalGate, transport: originalTransport }],
  ]);
  const requestIdRef = { current: originalGate.requestId as string | undefined };
  const transportRef = { current: originalTransport as AbortController | null };
  let visibleRequestId = "";

  pageIntegration.installVisionRequestOwnership({
    terminalEntries,
    previousRequestId: originalGate.requestId,
    requestId: replacementGate.requestId,
    transport: replacementTransport,
    terminalEntry: { gate: replacementGate, transport: replacementTransport },
    preservePreviousTerminalEntry: true,
    requestIdRef,
    transportRef,
    setCurrentRequestId: (requestId) => { visibleRequestId = requestId; },
  });

  assert.equal(terminalEntries.get(originalGate.requestId)?.gate, originalGate);
  assert.equal(terminalEntries.get(replacementGate.requestId)?.gate, replacementGate);
  assert.equal(originalGate.signal.aborted, false);
  assert.equal(requestIdRef.current, replacementGate.requestId);
  assert.equal(visibleRequestId, replacementGate.requestId);
});

test("ordinary Automation Abort followed by mode change retires the accepted gate without stranding replacement loading", async () => {
  const retireAutomationTransportOwnership = (
    pageIntegration as typeof pageIntegration & {
      retireAutomationTransportOwnership?: <T>(input: {
        terminalEntries: Map<string, T>;
        transportToken: { sessionId: string; token: symbol };
        isCurrentToken(token: { sessionId: string; token: symbol }): boolean;
        retireCurrentTransport(): void;
      }) => boolean;
    }
  ).retireAutomationTransportOwnership;
  assert.equal(typeof retireAutomationTransportOwnership, "function");

  let sequence = 0;
  let acceptedTransport: AbortController | null = null;
  const automationController = createVisionAutomationController({
    config: { ...config, intervalSeconds: 60 },
    acquireSources: async () => [source("camera_1", [])],
    restoreSource: async ({ source: failedSource }) => ({
      ...source("camera_1", []),
      descriptor: failedSource,
    }),
    getManualDataUrls: () => [],
    getMessages: () => [],
    commitMessages: () => {},
    isModelBusy: () => false,
    encodeFrame: async ({ blob }) => blob,
    blobToDataUrl: async () => "data:image/jpeg;base64,anBlZw==",
    getRequestStatus: async (requestId) => ({ requestId, status: "terminal" }),
    waitForRequestTerminal: async (requestId) => ({ requestId, status: "terminal" }),
    submit: async ({ controller, lifecycle, requestId }) => {
      acceptedTransport = controller;
      await lifecycle.onRequestAccepted({ type: "request_accepted", requestId });
    },
    createId: (kind) => `${kind}-${String(++sequence).padStart(16, "0")}`,
  });
  await automationController.start({
    pageInstanceId: "page-mode-change-1",
    pageGeneration: 1,
    mode: "chat",
    contextId: "chat-mode-change-1",
    automationTaskId: "task-mode-change-0001",
  });
  assert.equal(await automationController.send("accepted inference"), true);
  const requestId = automationController.getState().activeRequestId;
  assert.ok(requestId);
  const oldTransport = acceptedTransport as AbortController | null;
  assert.ok(oldTransport);
  const gate = new pageIntegration.AutomationTerminalGate(requestId);
  let currentTransport: AbortController | null = oldTransport;
  let isLoading = true;
  const events: string[] = [];
  const currentAttachment = { sessionId: "mode-change-agent", token: Symbol("mode-change") };
  const terminalEntries = new Map<string, {
    gate: pageIntegration.AutomationTerminalGate;
    release(result: { requestId: string; status: "terminal" }): void;
  }>();
  const release = (result: { requestId: string; status: "terminal" }) => {
    const currentEntry = terminalEntries.get(requestId);
    if (currentEntry?.gate !== gate) return;
    terminalEntries.delete(requestId);
    pageIntegration.completeAutomationTerminal(
      gate,
      result,
      currentTransport === oldTransport,
      () => {
        isLoading = false;
        currentTransport = null;
        events.push("terminal-release");
      },
    );
  };
  terminalEntries.set(requestId, { gate, release });
  const streamCompletion = () => gate.markCandidateComplete();
  const streamError = () => gate.markCandidateComplete();

  await automationController.abort({ registerAgentCleanup: false });

  assert.equal(oldTransport.signal.aborted, false);
  assert.equal(automationController.getState().phase, "idle");
  assert.equal(gate.modelBusy, true);
  assert.equal(terminalEntries.has(requestId), true);
  assert.equal(isLoading, true);

  await pageIntegration.abortPageTransportBeforeRetirement(oldTransport, async () => {
    events.push(`retirement-start:${String(oldTransport.signal.aborted)}`);
    retireAutomationTransportOwnership!({
      terminalEntries,
      transportToken: currentAttachment,
      isCurrentToken: (token) => token === currentAttachment,
      retireCurrentTransport: () => {
        isLoading = false;
        currentTransport = null;
        events.push("transport-retired");
      },
    });
  });

  assert.deepEqual(events, ["retirement-start:true", "transport-retired"]);
  assert.equal(terminalEntries.size, 0, "the replaced context must retire its status owner");
  assert.equal(gate.modelBusy, true, "context retirement must not fabricate stable terminal");
  assert.equal(isLoading, false, "the replacement page must be usable immediately");
  assert.equal(canStartVisionAutomation({
    enabled: true,
    cycleRunning: false,
    startGuardActive: false,
    armed: false,
    modelBusy: isLoading,
    pendingAcceptance: false,
  }), true);

  const replacementTransport = new AbortController();
  currentTransport = replacementTransport;
  isLoading = true;
  streamCompletion();
  streamError();
  release({ requestId, status: "terminal" });

  assert.equal(gate.candidateComplete, true);
  assert.equal(gate.modelBusy, true, "a retired stale callback cannot release through cleanup state");
  assert.equal(currentTransport, replacementTransport);
  assert.equal(isLoading, true, "stale completion/error cannot release newer page state");
  assert.deepEqual(events, ["retirement-start:true", "transport-retired"]);
  await automationController.dispose();
});

test("active configuration snapshot is immutable and independent of refreshes", () => {
  const snapshot = freezeVisionAutomationConfig(config);
  const refreshed = { ...config, intervalSeconds: 9, maxFramesPerSource: 1 };

  assert.equal(Object.isFrozen(snapshot), true);
  assert.equal(snapshot.intervalSeconds, 1);
  assert.equal(snapshot.maxFramesPerSource, 4);
  assert.equal(refreshed.intervalSeconds, 9);
});

test("strict canonical manifest resolver retains source aliases and two-decimal range", () => {
  const content = [
    { type: "image_url", image_url: { url: "manual" } },
    { type: "image_url", image_url: { url: "desktop-0" } },
    { type: "image_url", image_url: { url: "desktop-1" } },
    { type: "image_url", image_url: { url: "camera-0" } },
    { type: "image_url", image_url: { url: "camera-1" } },
    { type: "text", text: [
      "<vision_automation_manifest>",
      "Manual images: 1, image indexes 1-1",
      "Source 1: Desktop, image indexes 2-3",
      "Source 2: Camera 1, image indexes 4-5",
      "Relative times: 0.00s, 4.25s",
      "</vision_automation_manifest>",
      "",
      "Inspect.",
    ].join("\n") },
  ];
  const resolved = resolveCanonicalVisionAutomationImageBlock({
    message: { id: "message-1", role: "user", content, timestamp: new Date(0) },
    messageIndex: 0,
    blockIndex: 0,
    imageCount: 5,
  });

  assert.deepEqual(resolved, {
    sources: ["manual", "desktop", "camera_1"],
    relativeRange: { startSeconds: 0, endSeconds: 4.25 },
  });
  const malformed = structuredClone(content);
  (malformed[5] as { text: string }).text = (malformed[5] as { text: string }).text.replace("4.25s", "4.2s");
  assert.equal(resolveCanonicalVisionAutomationImageBlock({
    message: { id: "message-2", role: "user", content: malformed, timestamp: new Date(0) },
    messageIndex: 0,
    blockIndex: 0,
    imageCount: 5,
  }), undefined);
});
