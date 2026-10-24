import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

import {
  VOICE_WAVEFORM_HISTORY_LIMIT,
  createInitialVoiceInputState,
  getVoiceCapabilityError,
  voiceInputReducer,
} from "./voice-input-state.ts";
import {
  buildAsrStartHandshake,
  buildAsrWebSocketUrl,
  createVoiceInputConfig,
  estimatePostGainLevel,
  float32ToLittleEndianBuffer,
  parseAsrServerEvent,
} from "./voice-input.ts";
import { findDictationMagicSendMatch } from "./dictation-magic-send.ts";

const location = {
  protocol: "https:",
  hostname: "192.168.1.88",
};

const frontendRoot = new URL("../../", import.meta.url);

async function createMockWorkletProcessor(
  processorOptions: Record<string, number>,
): Promise<{
  frameOffset: number;
  inFlightFrames: number;
  overflowEmitted: boolean;
  paused: boolean;
  port: {
    messages: unknown[];
    onmessage: ((event: { data: unknown }) => void) | null;
  };
  process: (inputs: Float32Array[][], outputs: Float32Array[][]) => boolean;
}> {
  const source = await readFile(
    new URL("public/asr-pcm-worklet.js", frontendRoot),
    "utf8",
  );
  let Processor: (new (options: { processorOptions: Record<string, number> }) => any) | undefined;

  class MockAudioWorkletProcessor {
    port = {
      messages: [] as unknown[],
      onmessage: null as ((event: { data: unknown }) => void) | null,
      postMessage: (message: unknown) => {
        this.port.messages.push(message);
      },
    };
  }

  runInNewContext(source, {
    AudioWorkletProcessor: MockAudioWorkletProcessor,
    Float32Array,
    Math,
    Number,
    registerProcessor: (
      _name: string,
      RegisteredProcessor: new (options: { processorOptions: Record<string, number> }) => any,
    ) => {
      Processor = RegisteredProcessor;
    },
  });
  assert.ok(Processor);
  return new Processor({ processorOptions });
}

test("LAN HTTP is rejected before requesting microphone", () => {
  assert.equal(
    getVoiceCapabilityError({
      isSecureContext: false,
      hasMediaDevices: true,
      hasAudioWorklet: true,
    })?.code,
    "insecure_context",
  );
});

test("missing browser audio APIs produce classified capability errors", () => {
  assert.equal(
    getVoiceCapabilityError({
      isSecureContext: true,
      hasMediaDevices: false,
      hasAudioWorklet: true,
    })?.code,
    "media_devices_unavailable",
  );
  assert.equal(
    getVoiceCapabilityError({
      isSecureContext: true,
      hasMediaDevices: true,
      hasAudioWorklet: false,
    })?.code,
    "audio_worklet_unavailable",
  );
  assert.equal(
    getVoiceCapabilityError({
      isSecureContext: true,
      hasMediaDevices: true,
      hasAudioWorklet: true,
    }),
    null,
  );
});

test("browser microphone startup errors are actionable and recoverable", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const classifyError = voiceModule.classifyVoiceBrowserError as
    | ((error: unknown) => { code: string; message: string; recoverable: boolean })
    | undefined;
  assert.equal(typeof classifyError, "function");

  const denied = classifyError!(new DOMException("denied", "NotAllowedError"));
  assert.equal(denied.code, "permission_denied");
  assert.equal(denied.recoverable, true);
  assert.match(denied.message, /allow microphone access/i);

  const missing = classifyError!(new DOMException("missing", "NotFoundError"));
  assert.equal(missing.code, "microphone_unavailable");
  assert.equal(missing.recoverable, true);
  assert.match(missing.message, /microphone/i);

  const busy = classifyError!(new DOMException("busy", "NotReadableError"));
  assert.equal(busy.code, "microphone_unavailable");
  assert.equal(busy.recoverable, true);
  assert.match(busy.message, /busy|another app|try again/i);
});

test("post-permission setup errors are classified by their failing stage", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const classifySetupError = voiceModule.classifyVoiceSetupError as
    | ((
        stage: "audio_worklet" | "configuration" | "websocket",
        error: unknown,
      ) => { code: string; message: string; recoverable: boolean })
    | undefined;
  assert.equal(typeof classifySetupError, "function");

  const worklet = classifySetupError!("audio_worklet", new Error("module load failed"));
  assert.equal(worklet.code, "audio_worklet_unavailable");
  assert.equal(worklet.recoverable, false);
  assert.match(worklet.message, /audio|worklet/i);

  const configuration = classifySetupError!("configuration", new Error("bad port 123"));
  assert.equal(configuration.code, "protocol_error");
  assert.equal(configuration.recoverable, false);
  assert.equal(
    configuration.message,
    classifySetupError!("configuration", new Error("different details")).message,
  );

  const websocket = classifySetupError!("websocket", new Error("connection failed"));
  assert.equal(websocket.code, "websocket_error");
  assert.equal(websocket.recoverable, true);
  assert.match(websocket.message, /connect|connection|try again/i);
});

test("controller applies explicit setup-stage classification after permission", async () => {
  const source = await readFile(
    new URL("src/lib/voice-input.ts", frontendRoot),
    "utf8",
  );
  const postPermissionSetup = source.slice(source.indexOf("const resources: VoiceResources"));
  assert.match(postPermissionSetup, /classifyVoiceSetupError\(["']audio_worklet["']/);
  assert.match(postPermissionSetup, /classifyVoiceSetupError\(["']configuration["']/);
  assert.match(postPermissionSetup, /classifyVoiceSetupError\(["']websocket["']/);
  assert.doesNotMatch(postPermissionSetup, /classifyVoiceBrowserError/);
  assert.match(postPermissionSetup, /code: ["']sample_rate_mismatch["']/);
});

test("explicit public wss URL wins", () => {
  assert.equal(
    buildAsrWebSocketUrl(
      {
        publicWebSocketUrl: "wss://voice.test/v1/asr/stream",
        websocketPath: "/ignored",
      },
      location,
      8090,
    ),
    "wss://voice.test/v1/asr/stream",
  );
});

test("browser-safe server config maps explicitly to the voice controller", () => {
  assert.deepEqual(
    createVoiceInputConfig(
      {
        enabled: true,
        sample_rate_hz: 16000,
        capture_frame_ms: 20,
        input_gain_db: 12,
        input_gain_min_db: -24,
        input_gain_max_db: 36,
        input_gain_step_db: 0.5,
        clip_limit: 0.999,
        auto_send: true,
        streaming: false,
        websocket_path: "/v1/asr/stream",
        public_websocket_url: null,
        require_secure_context: true,
        abort_timeout_seconds: 2.5,
        max_buffered_frames: 6,
        dictation_magic_send: {
          commands: ["Over, over. "],
          poll_interval_ms: 500,
        },
        browser_constraints: {
          echo_cancellation: false,
          noise_suppression: true,
          auto_gain_control: false,
        },
      },
      8090,
    ),
    {
      enabled: true,
      sampleRateHz: 16000,
      captureFrameMs: 20,
      inputGainDb: 12,
      inputGainMinDb: -24,
      inputGainMaxDb: 36,
      inputGainStepDb: 0.5,
      clipLimit: 0.999,
      autoSend: true,
      streaming: false,
      websocketPath: "/v1/asr/stream",
      publicWebSocketUrl: null,
      requireSecureContext: true,
      abortTimeoutSeconds: 2.5,
      maxBufferedFrames: 6,
      dictationMagicSend: {
        commands: ["Over, over. "],
        pollIntervalMs: 500,
      },
      browserConstraints: {
        echoCancellation: false,
        noiseSuppression: true,
        autoGainControl: false,
      },
      backendPort: 8090,
    },
  );
});

test("WebSocket URL follows page security and normalizes the configured path", () => {
  assert.equal(
    buildAsrWebSocketUrl(
      { websocketPath: "v1/asr/stream" },
      location,
      8090,
    ),
    "wss://192.168.1.88:8090/v1/asr/stream",
  );
  assert.equal(
    buildAsrWebSocketUrl(
      { websocketPath: "/v1/asr/stream" },
      { protocol: "http:", hostname: "localhost" },
      8090,
    ),
    "ws://localhost:8090/v1/asr/stream",
  );
});

test("activation starts a new generation and stale events are ignored", () => {
  const activated = voiceInputReducer(createInitialVoiceInputState(), {
    type: "activate",
    generation: 3,
  });
  assert.equal(activated.phase, "requesting_permission");
  assert.equal(activated.generation, 3);

  const stale = voiceInputReducer(activated, {
    type: "ready",
    generation: 2,
    sessionId: "stale",
  });
  assert.strictEqual(stale, activated);

  const listening = voiceInputReducer(activated, {
    type: "ready",
    generation: 3,
    sessionId: "current",
  });
  assert.equal(listening.phase, "listening");
  assert.equal(listening.sessionId, "current");
});

test("backend lifecycle events map to the exact voice phases", () => {
  let state = voiceInputReducer(createInitialVoiceInputState(), {
    type: "activate",
    generation: 1,
  });

  for (const [backendState, expectedPhase] of [
    ["buffering", "listening"],
    ["candidate", "candidate"],
    ["recording", "recording"],
    ["finalizing", "finalizing"],
  ] as const) {
    state = voiceInputReducer(state, {
      type: "state_changed",
      generation: 1,
      state: backendState,
    });
    assert.equal(state.phase, expectedPhase);
  }

  state = voiceInputReducer(state, { type: "transcribing", generation: 1 });
  assert.equal(state.phase, "transcribing");
  state = voiceInputReducer(state, {
    type: "transcript",
    generation: 1,
    text: " final words ",
  });
  assert.equal(state.phase, "transcript_ready");
  assert.equal(state.pendingTranscript, " final words ");
  state = voiceInputReducer(state, { type: "pause_for_response", generation: 1 });
  assert.equal(state.phase, "paused_for_response");
  assert.equal(state.pendingTranscript, "");
});

test("waveform sample history is bounded", () => {
  let state = voiceInputReducer(createInitialVoiceInputState(), {
    type: "activate",
    generation: 8,
  });
  for (let index = 0; index < VOICE_WAVEFORM_HISTORY_LIMIT + 10; index += 1) {
    state = voiceInputReducer(state, {
      type: "level",
      generation: 8,
      dbfs: -60 + index,
      clippingRatio: 0,
      waveformSample: index / 100,
    });
  }
  assert.equal(state.waveform.length, VOICE_WAVEFORM_HISTORY_LIMIT);
  assert.equal(state.waveform[0], 0.1);
});

test("backend level event updates the meter and waveform without a local peak", () => {
  let state = voiceInputReducer(createInitialVoiceInputState(), {
    type: "activate",
    generation: 9,
  });

  state = voiceInputReducer(state, {
    type: "level",
    generation: 9,
    dbfs: -40,
    clippingRatio: 0.25,
  });

  assert.equal(state.levelDbfs, -40);
  assert.equal(state.clippingRatio, 0.25);
  assert.deepEqual(state.waveform, [0.5]);
});

test("partial transcript deltas are display-only and cannot become pending send text", () => {
  let state = voiceInputReducer(createInitialVoiceInputState(), {
    type: "activate",
    generation: 4,
  });
  state = voiceInputReducer(state, {
    type: "transcript_delta",
    generation: 4,
    text: "draft",
  });
  assert.equal(state.partialTranscript, "draft");
  assert.equal(state.pendingTranscript, "");

  state = voiceInputReducer(state, {
    type: "deactivate",
    generation: 5,
    preserveTranscript: true,
  });
  assert.equal(state.phase, "keyboard");
  assert.equal(state.generation, 5);
  assert.equal(state.pendingTranscript, "");
  assert.deepEqual(state.waveform, []);
  assert.strictEqual(
    voiceInputReducer(state, {
      type: "transcript",
      generation: 4,
      text: "late result",
    }),
    state,
  );
});

test("recoverable utterance errors clear streaming draft text before listening resumes", () => {
  let state = voiceInputReducer(createInitialVoiceInputState(), {
    type: "activate",
    generation: 5,
  });
  state = voiceInputReducer(state, {
    type: "transcript_delta",
    generation: 5,
    text: "failed partial",
  });
  state = { ...state, pendingTranscript: "draft must not survive" };
  state = voiceInputReducer(state, {
    type: "utterance_error",
    generation: 5,
  });
  assert.equal(state.phase, "listening");
  assert.equal(state.partialTranscript, "");
  assert.equal(state.pendingTranscript, "");
});

test("deactivation preserves only a final transcript", () => {
  let state = voiceInputReducer(createInitialVoiceInputState(), {
    type: "activate",
    generation: 6,
  });
  state = voiceInputReducer(state, {
    type: "transcript_delta",
    generation: 6,
    text: "partial ",
  });
  state = voiceInputReducer(state, {
    type: "transcript",
    generation: 6,
    text: " final words ",
  });
  state = voiceInputReducer(state, {
    type: "deactivate",
    generation: 7,
    preserveTranscript: true,
  });
  assert.equal(state.pendingTranscript, " final words ");
  assert.equal(state.partialTranscript, "");
});

test("voice abort cleanup waits for the session-local acknowledgment", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const waitForAcknowledgement = voiceModule.waitForVoiceAbortAcknowledgement as
    | ((socket: unknown, timeoutSeconds: number) => Promise<void>)
    | undefined;
  assert.equal(typeof waitForAcknowledgement, "function");

  const listeners = new Map<string, Set<(event: { data?: unknown }) => void>>();
  const sent: string[] = [];
  let closeCount = 0;
  const socket = {
    readyState: 1,
    send(payload: string) {
      sent.push(payload);
    },
    close() {
      closeCount += 1;
    },
    addEventListener(type: string, listener: (event: { data?: unknown }) => void) {
      const current = listeners.get(type) ?? new Set();
      current.add(listener);
      listeners.set(type, current);
    },
    removeEventListener(type: string, listener: (event: { data?: unknown }) => void) {
      listeners.get(type)?.delete(listener);
    },
  };

  let cleaned = false;
  const cleanup = waitForAcknowledgement!(socket, 1).then(() => {
    cleaned = true;
  });
  await Promise.resolve();
  assert.equal(cleaned, false);
  assert.deepEqual(sent, [JSON.stringify({ type: "abort" })]);
  assert.doesNotMatch(sent.join("\n"), /abort[-_ ]?all/i);

  for (const listener of listeners.get("message") ?? []) {
    listener({ data: JSON.stringify({ type: "aborted" }) });
  }
  await cleanup;
  assert.equal(cleaned, true);
  assert.equal(closeCount, 1);
});

test("abort send failure waits for the terminal close event", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const waitForAcknowledgement = voiceModule.waitForVoiceAbortAcknowledgement as
    | ((socket: unknown, timeoutSeconds: number) => Promise<void>)
    | undefined;
  assert.equal(typeof waitForAcknowledgement, "function");

  const listeners = new Map<string, Set<(event: { data?: unknown }) => void>>();
  let closeCount = 0;
  const socket = {
    readyState: 1,
    send() {
      throw new Error("send failed");
    },
    close() {
      closeCount += 1;
      this.readyState = 2;
    },
    addEventListener(type: string, listener: (event: { data?: unknown }) => void) {
      const current = listeners.get(type) ?? new Set();
      current.add(listener);
      listeners.set(type, current);
    },
    removeEventListener(type: string, listener: (event: { data?: unknown }) => void) {
      listeners.get(type)?.delete(listener);
    },
  };

  let cleaned = false;
  const cleanup = waitForAcknowledgement!(socket, 1).then(() => {
    cleaned = true;
  });
  await Promise.resolve();
  assert.equal(closeCount, 1);
  assert.equal(cleaned, false);

  for (const listener of listeners.get("close") ?? []) listener({});
  await cleanup;
  assert.equal(cleaned, true);
});

test("abort send failure waits for the configured timeout when close is not observed", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const waitForAcknowledgement = voiceModule.waitForVoiceAbortAcknowledgement as
    | ((socket: unknown, timeoutSeconds: number) => Promise<void>)
    | undefined;
  assert.equal(typeof waitForAcknowledgement, "function");

  let closeCount = 0;
  const socket = {
    readyState: 1,
    send() {
      throw new Error("send failed");
    },
    close() {
      closeCount += 1;
      this.readyState = 2;
    },
    addEventListener() {},
    removeEventListener() {},
  };

  let cleaned = false;
  const cleanup = waitForAcknowledgement!(socket, 0.01).then(() => {
    cleaned = true;
  });
  await Promise.resolve();
  assert.equal(cleaned, false);
  assert.equal(closeCount, 1);
  await cleanup;
  assert.equal(cleaned, true);
});

test("stale activation setup disposes only its captured resources and leaves B active", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const completeSetup = voiceModule.completeVoiceActivationSetup as
    | (<T>(
        resources: T,
        setup: () => Promise<void>,
        isCurrent: () => boolean,
        dispose: (resources: T) => Promise<void>,
      ) => Promise<boolean>)
    | undefined;
  assert.equal(typeof completeSetup, "function");

  const activationA = { id: "A" };
  const activationB = { id: "B" };
  let active = activationA;
  let finishSetupA!: () => void;
  const disposed: string[] = [];
  const setupA = completeSetup!(
    activationA,
    () => new Promise<void>((resolve) => {
      finishSetupA = resolve;
    }),
    () => active === activationA,
    async (resources) => {
      disposed.push(resources.id);
    },
  );

  active = activationB;
  finishSetupA();
  assert.equal(await setupA, false);
  assert.strictEqual(active, activationB);
  assert.deepEqual(disposed, ["A"]);
});

test("media permission is requested before unresolved prior cleanup completes", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const acquireMedia = voiceModule.acquireVoiceMediaAfterCleanup as
    | (<T>(
        requestMedia: () => Promise<T>,
        cleanup: Promise<void>,
        isCurrent: () => boolean,
        disposeStale: (media: T) => void,
      ) => Promise<T | null>)
    | undefined;
  assert.equal(typeof acquireMedia, "function");

  let finishCleanup!: () => void;
  const cleanup = new Promise<void>((resolve) => {
    finishCleanup = resolve;
  });
  const stream = { id: "stream-B" };
  let requestCount = 0;
  let settled = false;
  const acquisition = acquireMedia!(
    () => {
      requestCount += 1;
      return Promise.resolve(stream);
    },
    cleanup,
    () => true,
    () => undefined,
  ).then((result) => {
    settled = true;
    return result;
  });

  assert.equal(requestCount, 1);
  await Promise.resolve();
  assert.equal(settled, false);
  finishCleanup();
  assert.strictEqual(await acquisition, stream);
});

test("media returned after a generation change is stopped instead of connected", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const acquireMedia = voiceModule.acquireVoiceMediaAfterCleanup as
    | (<T>(
        requestMedia: () => Promise<T>,
        cleanup: Promise<void>,
        isCurrent: () => boolean,
        disposeStale: (media: T) => void,
      ) => Promise<T | null>)
    | undefined;
  assert.equal(typeof acquireMedia, "function");

  const stream = { id: "stale-stream" };
  const disposed: string[] = [];
  const result = await acquireMedia!(
    () => Promise.resolve(stream),
    Promise.resolve(),
    () => false,
    (media) => disposed.push(media.id),
  );
  assert.equal(result, null);
  assert.deepEqual(disposed, ["stale-stream"]);
});

test("release barrier bounds unresolved setup without disposing the newer generation", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const waitForRelease = voiceModule.waitForVoiceReleaseBarrier as
    | (<T>(
        previousCleanup: Promise<void>,
        resources: T | null,
        activationSetup: {
          promise: Promise<void>;
          abortTimeoutSeconds: number;
          cleanupDeadlineMs?: number;
        } | null,
        fallbackTimeoutSeconds: number | undefined,
        dispose: (resources: T, deadlineMs: number) => Promise<void>,
      ) => Promise<void>)
    | undefined;
  assert.equal(typeof waitForRelease, "function");

  const activationA = { id: "A" };
  const activationB = { id: "B" };
  const neverCompletes = new Promise<void>(() => undefined);
  const disposed: string[] = [];
  let active = activationA;
  let cleanupDeadline = 0;
  const startedAt = Date.now();
  const release = waitForRelease!(
    Promise.resolve(),
    activationA,
    { promise: neverCompletes, abortTimeoutSeconds: 0.01 },
    undefined,
    async (resources, deadlineMs) => {
      disposed.push(resources.id);
      cleanupDeadline = deadlineMs;
    },
  );

  active = activationB;
  const outcome = await Promise.race([
    release.then(() => "released" as const),
    new Promise<"hung">((resolve) => setTimeout(() => resolve("hung"), 100)),
  ]);
  assert.equal(outcome, "released");
  assert.ok(Date.now() - startedAt >= 5, "release resolved before the configured timeout");
  assert.ok(cleanupDeadline >= startedAt + 5);
  assert.strictEqual(active, activationB);
  assert.deepEqual(disposed, ["A"]);
});

test("repeated cleanup requests for one activation reuse its first deadline", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const waitForRelease = voiceModule.waitForVoiceReleaseBarrier as
    | (<T>(
        previousCleanup: Promise<void>,
        resources: T | null,
        activationSetup: {
          promise: Promise<void>;
          abortTimeoutSeconds: number;
          cleanupDeadlineMs?: number;
        } | null,
        fallbackTimeoutSeconds: number | undefined,
        dispose: (resources: T, deadlineMs: number) => Promise<void>,
      ) => Promise<void>)
    | undefined;
  assert.equal(typeof waitForRelease, "function");

  const activationSetup = {
    promise: new Promise<void>(() => undefined),
    abortTimeoutSeconds: 0.02,
    cleanupDeadlineMs: undefined as number | undefined,
  };
  const firstRelease = waitForRelease!(
    Promise.resolve(),
    null,
    activationSetup,
    undefined,
    async () => undefined,
  );
  const firstDeadline = activationSetup.cleanupDeadlineMs;
  assert.equal(typeof firstDeadline, "number");

  const secondRelease = waitForRelease!(
    firstRelease,
    null,
    activationSetup,
    undefined,
    async () => undefined,
  );
  assert.equal(activationSetup.cleanupDeadlineMs, firstDeadline);
  await secondRelease;
});

test("voice frame buffering enforces the configured frame-count limit", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const canBufferVoiceFrame = voiceModule.canBufferVoiceFrame as
    | ((bufferedBytes: number, frameBytes: number, maxBufferedFrames: number) => boolean)
    | undefined;
  assert.equal(typeof canBufferVoiceFrame, "function");
  assert.equal(canBufferVoiceFrame!(639, 320, 3), true);
  assert.equal(canBufferVoiceFrame!(641, 320, 3), false);
});

test("error state keeps a classified recovery message", () => {
  const activated = voiceInputReducer(createInitialVoiceInputState(), {
    type: "activate",
    generation: 10,
  });
  const failed = voiceInputReducer(activated, {
    type: "error",
    generation: 10,
    error: {
      code: "sample_rate_mismatch",
      message: "Browser opened 48000 Hz instead of 16000 Hz.",
      recoverable: false,
    },
  });
  assert.equal(failed.phase, "error");
  assert.equal(failed.error?.code, "sample_rate_mismatch");
});

test("startup failure invalidates capture and returns to Keyboard with its error", () => {
  let state = voiceInputReducer(createInitialVoiceInputState(), {
    type: "activate",
    generation: 20,
  });
  state = voiceInputReducer(state, {
    type: "level",
    generation: 20,
    dbfs: -10,
    clippingRatio: 0.2,
    waveformSample: 0.8,
  });
  state = voiceInputReducer(state, {
    type: "transcript_delta",
    generation: 20,
    text: "draft",
  });
  state = voiceInputReducer(state, {
    type: "startup_failure",
    generation: 21,
    error: {
      code: "permission_denied",
      message: "Allow microphone access and try again.",
      recoverable: true,
    },
  });
  assert.equal(state.phase, "keyboard");
  assert.equal(state.generation, 21);
  assert.equal(state.partialTranscript, "");
  assert.equal(state.pendingTranscript, "");
  assert.deepEqual(state.waveform, []);
  assert.equal(state.error?.code, "permission_denied");

  const retried = voiceInputReducer(state, { type: "activate", generation: 22 });
  assert.equal(retried.phase, "requesting_permission");
  assert.equal(retried.error, null);
});

test("start handshake is serialized before frames with the configured frame size", () => {
  assert.deepEqual(
    buildAsrStartHandshake({ sampleRateHz: 16000, captureFrameMs: 20 }),
    {
      type: "start",
      protocol_version: 1,
      sample_rate_hz: 16000,
      channels: 1,
      sample_format: "float32",
      frame_samples: 320,
    },
  );
  assert.throws(
    () => buildAsrStartHandshake({ sampleRateHz: 16000, captureFrameMs: 20.1 }),
    /whole number of samples/,
  );
});

test("post-gain level estimate applies gain once and reports clipping", () => {
  const estimate = estimatePostGainLevel(new Float32Array([0.5, -0.5]), 6.0206, 0.75);
  assert.ok(Math.abs(estimate.dbfs - -2.4988) < 0.01);
  assert.equal(estimate.clippingRatio, 1);
  assert.equal(estimate.waveformSample, 0.75);
});

test("PCM frames are encoded as explicit little-endian float32", () => {
  const encoded = float32ToLittleEndianBuffer(new Float32Array([1, -0.5]));
  assert.deepEqual(Array.from(new Uint8Array(encoded)), [
    0x00, 0x00, 0x80, 0x3f,
    0x00, 0x00, 0x00, 0xbf,
  ]);
});

test("server event parser accepts protocol events and rejects malformed payloads", () => {
  assert.deepEqual(
    parseAsrServerEvent('{"type":"ready","session_id":"abc","protocol_version":1,"state":"buffering"}'),
    {
      type: "ready",
      sessionId: "abc",
      protocolVersion: 1,
      state: "buffering",
    },
  );
  assert.deepEqual(parseAsrServerEvent('{"type":"transcript","text":"hello"}'), {
    type: "transcript",
    text: "hello",
  });
  assert.deepEqual(
    parseAsrServerEvent('{"type":"input_gain_changed","input_gain_db":6.5}'),
    { type: "input_gain_changed", inputGainDb: 6.5 },
  );
  assert.deepEqual(
    parseAsrServerEvent(
      '{"type":"control_error","code":"invalid_input_gain","message":"bad","input_gain_db":0}',
    ),
    {
      type: "control_error",
      code: "invalid_input_gain",
      message: "bad",
      inputGainDb: 0,
    },
  );
  assert.throws(() => parseAsrServerEvent("not json"), /Invalid ASR server event/);
  assert.throws(
    () => parseAsrServerEvent('{"type":"transcript","text":4}'),
    /Invalid ASR server event/,
  );
});

test("controller sends the cached input gain before it enables PCM frames", async () => {
  const source = await readFile(
    new URL("src/lib/voice-input.ts", frontendRoot),
    "utf8",
  );
  const readyBranch = source.slice(
    source.lastIndexOf('case "ready":'),
    source.lastIndexOf('case "level":'),
  );
  assert.match(readyBranch, /type:\s*"set_input_gain"/);
  assert.match(readyBranch, /input_gain_db:\s*inputGainDbRef\.current/);
  assert.ok(
    readyBranch.indexOf("input_gain_db") < readyBranch.indexOf("resources.socketReady = true"),
    "gain control must be ordered before PCM forwarding becomes ready",
  );
  const setterStart = source.indexOf("const setInputGainDb");
  const setterEnd = source.indexOf("\n  }, [config]);", setterStart)
    + "\n  }, [config]);".length;
  const setter = source.slice(setterStart, setterEnd);
  assert.doesNotMatch(setter, /activateVoice|deactivateVoice|releaseCurrent/);
});

test("AudioWorklet emits fixed transferable Float32 frames and supports pausing", async () => {
  const source = await readFile(
    new URL("public/asr-pcm-worklet.js", frontendRoot),
    "utf8",
  );
  assert.match(source, /new Float32Array\(this\.frameSamples\)/);
  assert.match(source, /postMessage\(\{ type: "pcm_frame", epoch: this\.captureEpoch, samples: frame \},\s*\[frame\.buffer\]\)/);
  assert.match(source, /message\.type === ["']pause["']/);
  assert.doesNotMatch(source, /this\.(?:history|frames)\s*=\s*\[\]/);
});

test("AudioWorklet bounds in-flight frames and emits one overflow control", async () => {
  const processor = await createMockWorkletProcessor({
    frameSamples: 2,
    maxBufferedFrames: 2,
  });
  processor.process(
    [[new Float32Array([1, 2, 3, 4, 5, 6])]],
    [[new Float32Array(6)]],
  );

  const frames = processor.port.messages.filter(
    (message) => (message as { type?: string })?.type === "pcm_frame",
  );
  const overflows = processor.port.messages.filter(
    (message) =>
      typeof message === "object" &&
      message !== null &&
      (message as { type?: string }).type === "transport_overflow",
  );
  assert.equal(frames.length, 2);
  assert.equal(overflows.length, 1);
  assert.equal(processor.inFlightFrames, 2);
  assert.equal(processor.paused, true);

  processor.process(
    [[new Float32Array([7, 8, 9, 10])]],
    [[new Float32Array(4)]],
  );
  assert.equal(
    processor.port.messages.filter(
      (message) =>
        typeof message === "object" &&
        message !== null &&
        (message as { type?: string }).type === "transport_overflow",
    ).length,
    1,
  );
});

test("AudioWorklet acknowledgments and pause/resume reset counters safely", async () => {
  const processor = await createMockWorkletProcessor({
    frameSamples: 2,
    maxBufferedFrames: 2,
  });
  processor.process(
    [[new Float32Array([1, 2])]],
    [[new Float32Array(2)]],
  );
  assert.equal(processor.inFlightFrames, 1);

  processor.port.onmessage?.({ data: { type: "frame_ack", epoch: 0 } });
  assert.equal(processor.inFlightFrames, 0);
  processor.port.onmessage?.({ data: { type: "frame_ack", epoch: 0 } });
  assert.equal(processor.inFlightFrames, 0);

  processor.port.onmessage?.({ data: { type: "pause" } });
  assert.equal(processor.inFlightFrames, 0);
  assert.equal(processor.frameOffset, 0);
  processor.port.onmessage?.({ data: { type: "resume" } });
  assert.equal(processor.inFlightFrames, 0);
  assert.equal(processor.overflowEmitted, false);
  assert.equal(processor.paused, false);
});

test("old-epoch acknowledgments cannot consume resumed worklet frame capacity", async () => {
  const processor = await createMockWorkletProcessor({ frameSamples: 2, maxBufferedFrames: 2 });
  processor.process([[new Float32Array([1, 2])]], [[new Float32Array(2)]]);
  processor.port.onmessage?.({ data: { type: "pause", epoch: 1 } });
  processor.port.onmessage?.({ data: { type: "resume", epoch: 1 } });
  processor.process([[new Float32Array([3, 4])]], [[new Float32Array(2)]]);
  processor.port.onmessage?.({ data: { type: "frame_ack", epoch: 0 } });
  assert.equal(processor.inFlightFrames, 1);
  processor.port.onmessage?.({ data: { type: "frame_ack", epoch: 1 } });
  assert.equal(processor.inFlightFrames, 0);
  const frames = processor.port.messages.filter((message) => (message as { type?: string })?.type === "pcm_frame") as { epoch: number; samples: Float32Array }[];
  assert.deepEqual(frames.map((frame) => frame.epoch), [0, 1]);
  assert.deepEqual([...frames[1].samples], [3, 4]);
});

test("controller propagates the worklet bound, acknowledges frames, and handles overflow", async () => {
  const source = await readFile(
    new URL("src/lib/voice-input.ts", frontendRoot),
    "utf8",
  );
  assert.match(
    source,
    /processorOptions:\s*\{[^}]*frameSamples:[^}]*maxBufferedFrames:\s*config\.maxBufferedFrames/s,
  );
  assert.match(source, /postMessage\(\{ type: ["']frame_ack["'], epoch: event\.data\.epoch \}\)/);
  assert.match(source, /event\.data\.type === ["']transport_overflow["']/);
  assert.match(source, /code:\s*["']transport_overflow["']/);
});

test("PCM handler leaves React level projection to backend-throttled events", async () => {
  const source = await readFile(
    new URL("src/lib/voice-input.ts", frontendRoot),
    "utf8",
  );
  const pcmHandler = source.slice(
    source.indexOf("worklet.port.onmessage"),
    source.indexOf("socket.onopen"),
  );

  assert.match(pcmHandler, /postMessage\(\{ type: ["']frame_ack["'], epoch: event\.data\.epoch \}\)/);
  assert.match(pcmHandler, /socket\.send\(frame\)/);
  assert.doesNotMatch(pcmHandler, /estimatePostGainLevel/);
  assert.doesNotMatch(pcmHandler, /dispatch\(\{ type: ["']level["']/);
  assert.match(source.slice(source.indexOf('case "level"')), /dispatch\(\{[\s\S]*type: "level"/);
});

test("input-mode toggle uses icon-only 44 px targets with Chinese accessible names", async () => {
  const source = await readFile(
    new URL("src/components/VoiceModeToggle.tsx", frontendRoot),
    "utf8",
  );
  assert.match(source, /role="group"/);
  assert.equal((source.match(/aria-pressed=/g) ?? []).length, 3);
  assert.ok((source.match(/min-h-\[36px\]/g) ?? []).length >= 3);
  assert.match(source, /aria-label="键盘输入"/);
  assert.match(source, /aria-label="听写输入"/);
  assert.match(source, /aria-label="语音输入"/);
  assert.doesNotMatch(source, />\s*Keyboard\s*</);
  assert.doesNotMatch(source, />\s*Voice\s*</);
  assert.match(source, /HoverHint label=/);
  assert.doesNotMatch(source, /语音\/听写输入暂不可用/);
  assert.doesNotMatch(source, /\btitle="/);
  assert.match(
    source,
    /id="voice-mode-disabled-reason"[\s\S]*?className="sr-only"/,
  );
});

test("replay metadata and binary WAV create a page-local audio attachment", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const createReplay = voiceModule.createVoiceReplayAudio as
    | ((
        metadata: { utteranceId: string; mimeType: string; byteLength: number },
        payload: Blob | ArrayBuffer,
        createObjectURL: (blob: Blob) => string,
      ) => { utteranceId: string; mimeType: string; url: string } | null)
    | undefined;
  assert.equal(typeof createReplay, "function");

  const metadata = parseAsrServerEvent(JSON.stringify({
    type: "replay_audio",
    utterance_id: "utterance-1",
    mime_type: "audio/wav",
    byte_length: 8,
  }));
  assert.deepEqual(metadata, {
    type: "replay_audio",
    utteranceId: "utterance-1",
    mimeType: "audio/wav",
    byteLength: 8,
  });

  const wav = new Uint8Array([82, 73, 70, 70, 0, 0, 0, 0]).buffer;
  const replay = createReplay!(metadata as never, wav, (blob) => {
    assert.equal(blob.size, 8);
    assert.equal(blob.type, "audio/wav");
    return "blob:voice-replay";
  });
  assert.deepEqual(replay, {
    utteranceId: "utterance-1",
    mimeType: "audio/wav",
    url: "blob:voice-replay",
  });

  assert.equal(
    createReplay!(metadata as never, new Uint8Array(7).buffer, () => "unused"),
    null,
  );
  assert.deepEqual(
    parseAsrServerEvent(JSON.stringify({
      type: "transcript",
      text: "测试",
      replay_utterance_id: "utterance-1",
    })),
    {
      type: "transcript",
      text: "测试",
      replayUtteranceId: "utterance-1",
    },
  );
});

test("final transcript keeps its replay attachment for manual send", () => {
  const activated = voiceInputReducer(createInitialVoiceInputState(), {
    type: "activate",
    generation: 1,
  });
  const replay = {
    utteranceId: "utterance-1",
    mimeType: "audio/wav",
    url: "blob:voice-replay",
  };

  const completed = voiceInputReducer(activated, {
    type: "transcript",
    generation: 1,
    text: "测试",
    replay,
  } as never);

  assert.equal(completed.pendingTranscript, "测试");
  assert.deepEqual(completed.pendingReplay, replay);
});

test("controller revokes replay blobs that are never linked to a transcript", async () => {
  const source = await readFile(
    new URL("src/lib/voice-input.ts", frontendRoot),
    "utf8",
  );
  assert.match(source, /revokeReplay/);
  assert.match(source, /URL\.revokeObjectURL\(replay\.url\)/);
  assert.match(source, /resources\.pendingReplay[\s\S]*?resources\.revokeReplay/);
});

test("voice panel renders bounded waveform and read-only transcript without textarea", async () => {
  const panel = await readFile(
    new URL("src/components/VoiceInputPanel.tsx", frontendRoot),
    "utf8",
  );
  const indicator = await readFile(
    new URL("src/components/VoiceListeningIndicator.tsx", frontendRoot),
    "utf8",
  );
  assert.match(indicator, /<svg/);
  assert.match(indicator, /state\.waveform/);
  assert.match(panel, /pendingTranscript/);
  assert.match(panel, /aria-readonly="true"/);
  assert.doesNotMatch(panel, /<textarea\b/i);
  assert.doesNotMatch(panel, /<input\b/i);
});

test("listening indicator scopes polite announcements away from the waveform and meter", async () => {
  const panel = await readFile(
    new URL("src/components/VoiceInputPanel.tsx", frontendRoot),
    "utf8",
  );
  const indicator = await readFile(
    new URL("src/components/VoiceListeningIndicator.tsx", frontendRoot),
    "utf8",
  );
  const sectionTag = panel.match(/<section[\s\S]*?>/)?.[0] ?? "";
  assert.doesNotMatch(sectionTag, /aria-live/);
  const meterAndWaveform = indicator.match(
    /Post-gain estimate[\s\S]*?<\/svg>/,
  )?.[0] ?? "";
  assert.doesNotMatch(meterAndWaveform, /aria-live/);
  assert.match(indicator, /aria-live="polite"/);
  assert.match(panel, /aria-live="polite"/);
});

test("controller checks AudioWorklet support without invoking a prototype getter", async () => {
  const source = await readFile(
    new URL("src/lib/voice-input.ts", frontendRoot),
    "utf8",
  );
  assert.match(source, /["']audioWorklet["']\s+in\s+AudioContextConstructor\.prototype/);
  assert.doesNotMatch(source, /prototype\.audioWorklet/);
});

test("recoverable utterance errors do not dispose the microphone session", async () => {
  const source = await readFile(
    new URL("src/lib/voice-input.ts", frontendRoot),
    "utf8",
  );
  const recoverableBlock = source.match(
    /case ["']utterance_error["']:\s*\{([\s\S]*?)case ["']fatal_error["']:/,
  )?.[1];
  assert.ok(recoverableBlock);
  assert.match(recoverableBlock, /resumeRetainedCapture\(resources\)/);
  assert.match(recoverableBlock, /type: ["']utterance_error["']/);
  assert.match(recoverableBlock, /shouldNotifyVoiceUtteranceError\(event\.code\)/);
  assert.match(recoverableBlock, /callbacksRef\.current\.onError/);
  assert.doesNotMatch(recoverableBlock, /reportFailure|releaseCurrent/);
});

test("candidate timeout stays recoverable without surfacing a user-level ASR warning", async () => {
  const voiceModule = await import("./voice-input.ts") as Record<string, unknown>;
  const shouldNotify = voiceModule.shouldNotifyVoiceUtteranceError as
    | ((code: string | undefined) => boolean)
    | undefined;
  assert.equal(typeof shouldNotify, "function");
  assert.equal(shouldNotify!("candidate_timeout"), false);
  assert.equal(shouldNotify!("transcription_failed"), true);
  assert.equal(shouldNotify!(undefined), true);
});

test("controller delivers callbacks only for final transcripts", async () => {
  const source = await readFile(
    new URL("src/lib/voice-input.ts", frontendRoot),
    "utf8",
  );
  const controllerSource = source.slice(source.indexOf("socket.onmessage"));
  const deltaBlock = controllerSource.match(
    /case ["']transcript_delta["']:\s*([\s\S]*?)case ["']transcript["']:/,
  )?.[1] ?? "";
  const finalBlock = controllerSource.match(
    /case ["']transcript["']:\s*\{([\s\S]*?)case ["']utterance_error["']:/,
  )?.[1] ?? "";
  const errorBlock = controllerSource.match(
    /case ["']utterance_error["']:\s*\{([\s\S]*?)case ["']fatal_error["']:/,
  )?.[1] ?? "";
  assert.doesNotMatch(deltaBlock, /onFinalTranscript/);
  assert.match(finalBlock, /config\.autoSend && transcript/);
  assert.doesNotMatch(finalBlock, /event\.text\.trim\(\)/);
  assert.match(finalBlock, /onFinalTranscript\(transcript, replay\)/);
  assert.doesNotMatch(errorBlock, /onFinalTranscript/);
});

test("trailing-space transcript remains available to dictation magic-send matching", () => {
  const state = voiceInputReducer(createInitialVoiceInputState(), {
    type: "activate",
    generation: 1,
  });
  const transcript = voiceInputReducer(state, {
    type: "transcript",
    generation: 1,
    text: "dictated body Over, over. ",
  });
  assert.equal(transcript.pendingTranscript, "dictated body Over, over. ");
  assert.deepEqual(
    findDictationMagicSendMatch(transcript.pendingTranscript, [{ text: "Over, over. ", enabled: true }]),
    { text: "Over, over. ", enabled: true },
  );
});

test("visibility and unmount cleanup remain session-local and update-safe", async () => {
  const source = await readFile(
    new URL("src/lib/voice-input.ts", frontendRoot),
    "utf8",
  );
  // 切换标签页/应用不得终止语音采集：后台持续录音是有意行为。
  assert.doesNotMatch(source, /visibilitychange/);
  assert.match(source, /mountedRef\.current = false;[\s\S]*?void releaseCurrent\(true\)/);
  assert.doesNotMatch(source, /abort[-_ ]?all/i);
});
