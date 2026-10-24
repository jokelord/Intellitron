import assert from "node:assert/strict";
import React from "react";
import { test, type TestContext } from "node:test";
import { useVoiceInput, type VoiceInputConfig, type VoiceInputController } from "./voice-input.ts";

const turn = () => new Promise<void>((resolve) => setImmediate(resolve));

// Execute the actual controller with a deterministic hook dispatcher. Only React
// scheduling and browser-native APIs are replaced; lifecycle/event handlers stay real.
function controllerHarness(t: TestContext, autoSend = true) {
  const slots: any[] = [];
  let index = 0;
  let effects: (() => void)[] = [];
  const internals = (React as any).__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED;
  const depsEqual = (a: unknown[] | undefined, b: unknown[] | undefined) =>
    Boolean(a && b && a.length === b.length && a.every((value, offset) => Object.is(value, b[offset])));
  const dispatcher = {
    useRef(value: unknown) {
      const position = index++;
      return slots[position] ??= { current: value };
    },
    useReducer(reducer: (state: any, event: any) => any, initial: unknown, initialize?: (value: any) => any) {
      const position = index++;
      const slot = slots[position] ??= { value: initialize ? initialize(initial) : initial };
      slot.dispatch ??= (event: unknown) => { slot.value = reducer(slot.value, event); };
      return [slot.value, slot.dispatch];
    },
    useState(initial: unknown) {
      return this.useReducer((state: unknown, next: any) => typeof next === "function" ? next(state) : next,
        initial, (value: any) => typeof value === "function" ? value() : value);
    },
    useCallback(callback: unknown, deps: unknown[]) {
      const position = index++;
      if (!slots[position] || !depsEqual(slots[position].deps, deps)) slots[position] = { callback, deps };
      return slots[position].callback;
    },
    useEffect(effect: () => (() => void) | void, deps: unknown[]) {
      const position = index++;
      if (slots[position] && depsEqual(slots[position].deps, deps)) return;
      const previous = slots[position];
      effects.push(() => {
        previous?.cleanup?.();
        slots[position] = { deps, cleanup: effect() };
      });
    },
  };
  let permissionRequests = 0;
  let focused = true;
  const tracks: { onended: (() => void) | null; stopped: boolean; stop(): void }[] = [];
  const contexts: NativeContext[] = [];
  const worklets: NativeWorklet[] = [];
  const sockets: NativeSocket[] = [];
  const transcripts: string[] = [];
  const errors: unknown[] = [];
  let failSocketCreation = false;

  class NativeNode {
    connect(node: unknown) { return node; }
    disconnect() {}
  }
  class NativeContext {
    sampleRate: number;
    destination = new NativeNode();
    closed = false;
    get audioWorklet() { return { addModule: async () => undefined }; }
    constructor(options: { sampleRate: number }) { this.sampleRate = options.sampleRate; contexts.push(this); }
    createMediaStreamSource() { return new NativeNode(); }
    createGain() { return Object.assign(new NativeNode(), { gain: { value: 1 } }); }
    async resume() {}
    async close() { this.closed = true; }
  }
  class NativeWorklet extends NativeNode {
    messages: { type: string; epoch?: number }[] = [];
    captureEpoch = 0;
    port = {
      onmessage: null as ((event: { data: unknown }) => void) | null,
      postMessage: (message: { type: string; epoch?: number }) => {
        this.messages.push(message);
        if ((message.type === "pause" || message.type === "resume") && message.epoch !== undefined) {
          this.captureEpoch = message.epoch;
        }
      },
      close: () => undefined,
    };
    constructor() { super(); worklets.push(this); }
    frame(epoch = this.captureEpoch) {
      this.port.onmessage?.({ data: { type: "pcm_frame", epoch, samples: new Float32Array(320) } });
    }
  }
  class NativeSocket extends EventTarget {
    static OPEN = 1;
    readyState = 0;
    bufferedAmount = 0;
    binaryType = "";
    sent: unknown[] = [];
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: unknown }) => void) | null = null;
    onclose: (() => void) | null = null;
    onerror: (() => void) | null = null;
    constructor() {
      super();
      if (failSocketCreation) throw new Error("socket unavailable");
      sockets.push(this);
    }
    open() { this.readyState = 1; this.onopen?.(); }
    event(event: unknown) {
      const data = typeof event === "object" && event instanceof ArrayBuffer ? event : JSON.stringify(event);
      this.onmessage?.({ data });
      this.dispatchEvent(new MessageEvent("message", { data }));
    }
    send(payload: unknown) {
      this.sent.push(payload);
      if (typeof payload === "string" && JSON.parse(payload).type === "abort") {
        queueMicrotask(() => this.event({ type: "aborted" }));
      }
    }
    close() {
      this.readyState = 3;
      this.onclose?.();
      this.dispatchEvent(new Event("close"));
    }
  }
  const originalGlobals = new Map<string, PropertyDescriptor | undefined>();
  const setGlobal = (name: string, value: unknown) => {
    originalGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  };
  setGlobal("window", {
    AudioContext: NativeContext, AudioWorkletNode: NativeWorklet,
    isSecureContext: true, location: { protocol: "https:", hostname: "localhost", port: "8090" },
    localStorage: { getItem: () => null, setItem: () => undefined },
  });
  setGlobal("document", { visibilityState: "visible", hasFocus: () => focused });
  setGlobal("navigator", { mediaDevices: { getUserMedia: async () => {
    permissionRequests += 1;
    if (!focused) return new Promise(() => undefined);
    const track = { onended: null as (() => void) | null, stopped: false, stop() { this.stopped = true; } };
    tracks.push(track);
    return { getTracks: () => [track] };
  } } });
  setGlobal("AudioWorkletNode", NativeWorklet);
  setGlobal("WebSocket", NativeSocket);
  const config: VoiceInputConfig = {
    enabled: true, sampleRateHz: 16000, captureFrameMs: 20,
    inputGainDb: 0, inputGainMinDb: -12, inputGainMaxDb: 30, inputGainStepDb: 1,
    clipLimit: 0.999, autoSend, streaming: false,
    websocketPath: "/v1/asr/stream", requireSecureContext: true,
    abortTimeoutSeconds: 0.05, maxBufferedFrames: 250,
    dictationMagicSend: { commands: [], pollIntervalMs: 100 },
    browserConstraints: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    backendPort: 8090,
  };
  const options = {
    config, isLoading: false,
    resumeBlocked: false,
    onFinalTranscript: (text: string) => { transcripts.push(text); },
    onError: (error: unknown) => { errors.push(error); },
  };
  const render = () => {
    const previous = internals.ReactCurrentDispatcher.current;
    internals.ReactCurrentDispatcher.current = dispatcher;
    index = 0;
    let controller: VoiceInputController;
    try { controller = useVoiceInput(options); }
    finally { internals.ReactCurrentDispatcher.current = previous; }
    const pending = effects;
    effects = [];
    pending.forEach((effect) => effect());
    return controller!;
  };
  t.after(async () => {
    slots.forEach((slot) => slot?.cleanup?.());
    await turn();
    for (const [name, descriptor] of originalGlobals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else Reflect.deleteProperty(globalThis, name);
    }
  });
  const start = async () => {
    await render().activateVoice();
    const socket = sockets.at(-1)!;
    socket.open();
    socket.event({ type: "ready", protocol_version: 1, session_id: `session-${sockets.length}`, state: "buffering" });
    return render();
  };
  return {
    render, start, options, contexts, worklets, sockets, tracks, transcripts, errors,
    permissionRequests: () => permissionRequests,
    background() { focused = false; (globalThis as any).document.visibilityState = "hidden"; },
    failNextSocket() { failSocketCreation = true; },
    resume(controller = render()) {
      // Baseline automatic paths used activateVoice; reproducing that path must
      // fail on a second unfocused permission request before resumeVoice exists.
      const resume = (controller as VoiceInputController & { resumeVoice?: () => void }).resumeVoice;
      return resume ? resume() : void controller.activateVoice();
    },
  };
}

test("two background dictation utterances reuse the user-activated microphone graph and socket", async (t) => {
  const h = controllerHarness(t);
  await h.start();
  const socket = h.sockets[0];
  h.background();
  socket.event({ type: "transcribing" });
  socket.event({ type: "transcript", text: "first phrase" });
  h.resume();
  await turn();
  assert.equal(h.permissionRequests(), 1, "automatic continuation must not request an unfocused microphone");
  assert.equal(h.contexts.length, 1);
  assert.equal(h.sockets.length, 1);
  assert.equal(h.tracks[0].stopped, false);
  socket.event({ type: "state_changed", state: "buffering" });
  h.worklets[0].frame();
  assert.equal(socket.sent.filter((payload) => payload instanceof ArrayBuffer).length, 1);
  socket.event({ type: "transcribing" });
  socket.event({ type: "transcript", text: "second phrase" });
  h.resume();
  socket.event({ type: "state_changed", state: "buffering" });
  assert.deepEqual(h.transcripts, ["first phrase", "second phrase"]);
  assert.equal(h.render().state.phase, "listening");
  assert.equal(h.permissionRequests(), 1);
});

test("response pauses reject queued PCM and stale levels until both reset readiness and explicit resume", async (t) => {
  const h = controllerHarness(t);
  await h.start();
  const socket = h.sockets[0];
  socket.event({ type: "transcribing" });
  socket.event({ type: "transcript", text: "question" });
  h.options.isLoading = true;
  h.render().pauseForResponse();
  const binaryCount = () => socket.sent.filter((payload) => payload instanceof ArrayBuffer).length;
  h.worklets[0].frame();
  socket.event({ type: "level", post_gain_dbfs: -20, clipping_ratio: 0 });
  socket.event({ type: "state_changed", state: "buffering" });
  assert.equal(binaryCount(), 0);
  assert.equal(h.render().state.phase, "paused_for_response");
  assert.equal(h.render().state.levelDbfs, null);
  h.resume();
  assert.equal(h.render().state.phase, "paused_for_response", "model wait must still guard resume");
  h.options.isLoading = false;
  h.resume();
  h.worklets[0].frame();
  assert.equal(binaryCount(), 1);
  assert.equal(h.permissionRequests(), 1);
});

test("automatic resume waits for backend reset before it forwards the next utterance", async (t) => {
  const h = controllerHarness(t);
  await h.start();
  const socket = h.sockets[0];
  socket.event({ type: "transcribing" });
  socket.event({ type: "transcript", text: "first" });
  h.resume();
  h.worklets[0].frame();
  assert.equal(socket.sent.filter((payload) => payload instanceof ArrayBuffer).length, 0);
  assert.equal(h.render().state.phase, "paused_for_response");
  socket.event({ type: "state_changed", state: "buffering" });
  h.worklets[0].frame();
  assert.equal(socket.sent.filter((payload) => payload instanceof ArrayBuffer).length, 1);
});

test("manual speech keeps its transcript and replay until resume and explicit keyboard teardown still releases", async (t) => {
  const h = controllerHarness(t, false);
  await h.start();
  const socket = h.sockets[0];
  socket.event({ type: "transcribing" });
  socket.event({ type: "replay_audio", utterance_id: "replay-1", mime_type: "audio/wav", byte_length: 8 });
  socket.event(new Uint8Array(8).buffer);
  socket.event({ type: "transcript", text: "manual phrase", replay_utterance_id: "replay-1" });
  socket.event({ type: "state_changed", state: "buffering" });
  assert.equal(h.render().state.phase, "transcript_ready");
  assert.equal(h.render().state.pendingTranscript, "manual phrase");
  const replay = h.render().state.pendingReplay;
  assert.ok(replay?.url);
  assert.equal(h.tracks[0].stopped, false);
  assert.equal(await h.render().deactivateVoice(), "manual phrase");
  assert.equal(h.tracks[0].stopped, true);
  assert.equal(h.contexts[0].closed, true);
  socket.event({ type: "state_changed", state: "buffering" });
  assert.equal(h.render().state.phase, "keyboard");
  assert.strictEqual(h.render().state.pendingReplay, replay);
});

test("an explicit retry replaces the activation and ignores stale readiness", async (t) => {
  const h = controllerHarness(t);
  await h.start();
  const oldSocket = h.sockets[0];
  await h.start();
  oldSocket.event({ type: "state_changed", state: "buffering" });
  oldSocket.event({ type: "transcript", text: "late old result" });
  assert.equal(h.permissionRequests(), 2);
  assert.equal(h.render().state.sessionId, "session-2");
  assert.deepEqual(h.transcripts, []);
  assert.equal(h.tracks[0].stopped, true);
});

test("manual send during recording discards only the ASR transport and resumes a fresh utterance", async (t) => {
  const h = controllerHarness(t);
  await h.start();
  const old = h.sockets[0];
  old.event({ type: "state_changed", state: "recording" });
  const oldEpoch = h.worklets[0].captureEpoch;
  h.render().pauseForResponse();
  h.background();
  h.resume();
  h.resume();
  await turn();
  assert.equal(h.sockets.length, 2, "repeated effects must not duplicate transport reconnect");
  assert.equal(h.permissionRequests(), 1);
  assert.equal(h.contexts.length, 1);
  assert.equal(h.tracks[0].stopped, false);
  assert.ok(old.sent.some((payload) => typeof payload === "string" && JSON.parse(payload).type === "abort"));
  const next = h.sockets[1];
  next.open();
  next.event({ type: "ready", protocol_version: 1, session_id: "fresh-asr", state: "buffering" });
  old.event({ type: "transcript", text: "discarded partial utterance" });
  old.event({ type: "state_changed", state: "buffering" });
  h.worklets[0].frame(oldEpoch);
  assert.equal(next.sent.filter((payload) => payload instanceof ArrayBuffer).length, 0);
  h.worklets[0].frame();
  assert.equal(next.sent.filter((payload) => payload instanceof ArrayBuffer).length, 1);
  assert.equal(h.render().state.sessionId, "fresh-asr");
  assert.deepEqual(h.transcripts, []);
});

test("Abort while a transport reconnect is pending leaves no replacement socket or graph", async (t) => {
  const h = controllerHarness(t);
  await h.start();
  h.render().pauseForResponse();
  h.resume();
  await h.render().abortVoice();
  await turn();
  assert.equal(h.sockets.length, 1);
  assert.equal(h.tracks[0].stopped, true);
  assert.equal(h.contexts[0].closed, true);
  assert.equal(h.render().state.phase, "keyboard");
});

test("a failed transport-only reconnect releases retained audio and reports recoverable error", async (t) => {
  const h = controllerHarness(t);
  await h.start();
  h.render().pauseForResponse();
  h.failNextSocket();
  h.resume();
  await turn();
  assert.equal(h.tracks[0].stopped, true);
  assert.equal(h.contexts[0].closed, true);
  assert.equal(h.render().state.error?.code, "websocket_error");
  assert.equal(h.permissionRequests(), 1);
});

test("candidate rejection resumes without waiting for a nonexistent inference readiness event", async (t) => {
  const h = controllerHarness(t);
  await h.start();
  h.sockets[0].event({ type: "state_changed", state: "candidate" });
  h.sockets[0].event({ type: "utterance_error", code: "candidate_timeout", message: "candidate_timeout" });
  h.worklets[0].frame();
  assert.equal(h.render().state.phase, "listening");
  assert.equal(h.sockets[0].sent.filter((payload) => payload instanceof ArrayBuffer).length, 1);
  assert.deepEqual(h.errors, []);
});

test("an inference error resumes only after readiness while post-transcript errors preserve manual text", async (t) => {
  const h = controllerHarness(t, false);
  await h.start();
  const socket = h.sockets[0];
  socket.event({ type: "transcribing" });
  socket.event({ type: "utterance_error", code: "asr_failed", message: "failed" });
  h.worklets[0].frame();
  assert.equal(socket.sent.filter((payload) => payload instanceof ArrayBuffer).length, 0);
  socket.event({ type: "state_changed", state: "buffering" });
  h.worklets[0].frame();
  assert.equal(socket.sent.filter((payload) => payload instanceof ArrayBuffer).length, 1);
  socket.event({ type: "transcribing" });
  socket.event({ type: "transcript", text: "keep manual text" });
  socket.event({ type: "utterance_error", code: "vad_reset_failed", message: "reset failed" });
  socket.event({ type: "state_changed", state: "buffering" });
  assert.equal(h.render().state.pendingTranscript, "keep manual text");
  assert.equal(h.render().state.phase, "transcript_ready");
});

test("a delayed old-epoch overflow cannot destroy resumed capture", async (t) => {
  const h = controllerHarness(t);
  await h.start();
  const socket = h.sockets[0];
  const oldEpoch = h.worklets[0].captureEpoch;
  socket.event({ type: "transcribing" });
  socket.event({ type: "transcript", text: "first" });
  h.resume();
  socket.event({ type: "state_changed", state: "buffering" });
  h.worklets[0].port.onmessage?.({ data: { type: "transport_overflow", epoch: oldEpoch } });
  assert.equal(h.render().state.phase, "listening");
  assert.equal(h.tracks[0].stopped, false);
  h.worklets[0].port.onmessage?.({ data: { type: "transport_overflow", epoch: h.worklets[0].captureEpoch } });
  assert.equal(h.render().state.error?.code, "transport_overflow");
  assert.equal(h.tracks[0].stopped, true);
});

test("manual model pause cancels an ASR inference and ignores its later transcript", async (t) => {
  const h = controllerHarness(t);
  await h.start();
  const socket = h.sockets[0];
  socket.event({ type: "transcribing" });
  h.render().pauseForResponse();
  socket.event({ type: "transcript", text: "late unwanted second send" });
  assert.deepEqual(h.transcripts, []);
  assert.ok(socket.sent.some((payload) => typeof payload === "string" && JSON.parse(payload).type === "abort"));
  assert.equal(h.tracks[0].stopped, false);
});

for (const guard of ["isLoading", "resumeBlocked"] as const) {
  test(`pending resume rechecks ${guard} at readiness instead of forwarding queued PCM`, async (t) => {
    const h = controllerHarness(t);
    await h.start();
    const socket = h.sockets[0];
    socket.event({ type: "transcribing" });
    socket.event({ type: "transcript", text: "first" });
    h.resume();
    h.options[guard] = true;
    h.render();
    socket.event({ type: "state_changed", state: "buffering" });
    h.worklets[0].frame();
    assert.equal(socket.sent.filter((payload) => payload instanceof ArrayBuffer).length, 0);
    assert.equal(h.render().state.phase, "paused_for_response");
    h.options[guard] = false;
    h.resume();
    h.worklets[0].frame();
    assert.equal(socket.sent.filter((payload) => payload instanceof ArrayBuffer).length, 1);
  });
}
