"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import type { ConfigResponse } from "./api";
import {
  normalizeInputGainDb,
  readInputGainDb,
  writeInputGainDb,
  type InputGainRange,
} from "./input-gain-storage.ts";

import {
  createInitialVoiceInputState,
  getVoiceCapabilityError,
  voiceInputReducer,
  type VoiceError,
  type VoiceInputState,
  type VoiceReplayAudio,
} from "./voice-input-state.ts";

export interface VoiceBrowserConstraints {
  echoCancellation: boolean;
  noiseSuppression: boolean;
  autoGainControl: boolean;
}

export interface VoiceInputConfig {
  enabled: boolean;
  sampleRateHz: number;
  captureFrameMs: number;
  inputGainDb: number;
  inputGainMinDb: number;
  inputGainMaxDb: number;
  inputGainStepDb: number;
  clipLimit: number;
  autoSend: boolean;
  streaming: boolean;
  websocketPath: string;
  publicWebSocketUrl?: string | null;
  requireSecureContext: boolean;
  abortTimeoutSeconds: number;
  maxBufferedFrames: number;
  dictationMagicSend: {
    commands: string[];
    pollIntervalMs: number;
  };
  browserConstraints: VoiceBrowserConstraints;
  backendPort?: number;
}

export function createVoiceInputConfig(
  config: NonNullable<ConfigResponse["asr"]>,
  backendPort?: number,
): VoiceInputConfig {
  return {
    enabled: config.enabled,
    sampleRateHz: config.sample_rate_hz,
    captureFrameMs: config.capture_frame_ms,
    inputGainDb: config.input_gain_db,
    inputGainMinDb: config.input_gain_min_db,
    inputGainMaxDb: config.input_gain_max_db,
    inputGainStepDb: config.input_gain_step_db,
    clipLimit: config.clip_limit,
    autoSend: config.auto_send,
    streaming: config.streaming,
    websocketPath: config.websocket_path,
    publicWebSocketUrl: config.public_websocket_url,
    requireSecureContext: config.require_secure_context,
    abortTimeoutSeconds: config.abort_timeout_seconds,
    maxBufferedFrames: config.max_buffered_frames,
    dictationMagicSend: {
      commands: config.dictation_magic_send.commands,
      pollIntervalMs: config.dictation_magic_send.poll_interval_ms,
    },
    browserConstraints: {
      echoCancellation: config.browser_constraints.echo_cancellation,
      noiseSuppression: config.browser_constraints.noise_suppression,
      autoGainControl: config.browser_constraints.auto_gain_control,
    },
    backendPort,
  };
}

export interface VoiceInputOptions {
  config: VoiceInputConfig | null;
  isLoading: boolean;
  resumeBlocked?: boolean;
  onFinalTranscript: (
    transcript: string,
    replay?: VoiceReplayAudio,
  ) => void | Promise<void>;
  onError?: (error: VoiceError) => void;
}

export interface VoiceInputController {
  state: VoiceInputState;
  isVoiceMode: boolean;
  canActivate: boolean;
  activateVoice: () => Promise<void>;
  resumeVoice: () => void;
  deactivateVoice: () => Promise<string>;
  abortVoice: () => Promise<void>;
  pauseForResponse: () => void;
  releaseReplay: (replay: VoiceReplayAudio) => void;
  inputGain: VoiceInputGainControl | null;
}

export interface VoiceInputGainControl {
  valueDb: number;
  minDb: number;
  maxDb: number;
  stepDb: number;
  setValueDb: (valueDb: number) => void;
}

export interface VoiceLocation {
  protocol: string;
  hostname: string;
}

export interface AsrStartHandshake {
  type: "start";
  protocol_version: 1;
  sample_rate_hz: number;
  channels: 1;
  sample_format: "float32";
  frame_samples: number;
}

export type AsrServerEvent =
  | {
      type: "ready";
      sessionId: string;
      protocolVersion: number;
      state: "buffering";
    }
  | { type: "level"; dbfs: number; clippingRatio: number }
  | {
      type: "state_changed";
      state: "buffering" | "candidate" | "recording" | "finalizing";
    }
  | { type: "transcribing" }
  | {
      type: "replay_audio";
      utteranceId: string;
      mimeType: "audio/wav";
      byteLength: number;
    }
  | { type: "transcript_delta"; text: string }
  | { type: "transcript"; text: string; replayUtteranceId?: string }
  | { type: "utterance_error"; code?: string; message: string }
  | { type: "input_gain_changed"; inputGainDb: number }
  | { type: "control_error"; code: "invalid_input_gain"; message: string; inputGainDb: number }
  | { type: "aborted" }
  | { type: "fatal_error"; code?: string; message: string };

export interface VoiceLevelEstimate {
  dbfs: number;
  clippingRatio: number;
  waveformSample: number;
}

interface VoiceResources {
  generation: number;
  stream: MediaStream;
  context: AudioContext | null;
  source: MediaStreamAudioSourceNode | null;
  worklet: AudioWorkletNode | null;
  sink: GainNode | null;
  socket: WebSocket | null;
  socketReady: boolean;
  sessionId: string | null;
  capturePaused: boolean;
  captureEpoch: number;
  serverReady: boolean;
  resumeRequested: boolean;
  continuationPaused: boolean;
  reconnectPending: boolean;
  connectSocket: (() => void) | null;
  transportCleanup: Promise<void>;
  closing: boolean;
  abortTimeoutSeconds: number;
  cleanupPromise: Promise<void> | null;
  expectedReplay: Extract<AsrServerEvent, { type: "replay_audio" }> | null;
  pendingReplay: VoiceReplayAudio | null;
  revokeReplay: (replay: VoiceReplayAudio) => void;
}

interface VoiceActivationSetupBarrier {
  promise: Promise<void>;
  abortTimeoutSeconds: number;
  cleanupDeadlineMs?: number;
}

const WORKLET_PATH = "/asr-pcm-worklet.js";

function pauseVoiceCapture(resources: VoiceResources): void {
  resources.capturePaused = true;
  resources.captureEpoch += 1;
  resources.worklet?.port.postMessage({ type: "pause", epoch: resources.captureEpoch });
}

export function buildAsrWebSocketUrl(
  config: Pick<VoiceInputConfig, "publicWebSocketUrl" | "websocketPath">,
  location: VoiceLocation,
  backendPort: number,
): string {
  const publicUrl = config.publicWebSocketUrl?.trim();
  if (publicUrl) return publicUrl;

  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const path = config.websocketPath.startsWith("/")
    ? config.websocketPath
    : `/${config.websocketPath}`;
  return `${protocol}//${location.hostname}:${backendPort}${path}`;
}

export function buildAsrStartHandshake(
  config: Pick<VoiceInputConfig, "sampleRateHz" | "captureFrameMs">,
): AsrStartHandshake {
  const frameSamples = (config.sampleRateHz * config.captureFrameMs) / 1000;
  if (!Number.isInteger(frameSamples) || frameSamples <= 0) {
    throw new Error("Voice capture frame duration must produce a positive whole number of samples.");
  }
  return {
    type: "start",
    protocol_version: 1,
    sample_rate_hz: config.sampleRateHz,
    channels: 1,
    sample_format: "float32",
    frame_samples: frameSamples,
  };
}

export function float32ToLittleEndianBuffer(samples: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(samples.length * Float32Array.BYTES_PER_ELEMENT);
  const view = new DataView(buffer);
  samples.forEach((sample, index) => {
    view.setFloat32(index * Float32Array.BYTES_PER_ELEMENT, sample, true);
  });
  return buffer;
}

export function canBufferVoiceFrame(
  bufferedBytes: number,
  frameBytes: number,
  maxBufferedFrames: number,
): boolean {
  return bufferedBytes + frameBytes <= frameBytes * maxBufferedFrames;
}

export function createVoiceReplayAudio(
  metadata: Extract<AsrServerEvent, { type: "replay_audio" }>,
  payload: Blob | ArrayBuffer,
  createObjectURL: (blob: Blob) => string = URL.createObjectURL,
): VoiceReplayAudio | null {
  const source = payload instanceof Blob
    ? payload
    : payload instanceof ArrayBuffer
      ? new Blob([payload], { type: metadata.mimeType })
      : null;
  if (!source || source.size !== metadata.byteLength) return null;
  const wav = source.type === metadata.mimeType
    ? source
    : source.slice(0, source.size, metadata.mimeType);
  return {
    utteranceId: metadata.utteranceId,
    mimeType: metadata.mimeType,
    url: createObjectURL(wav),
  };
}

export async function completeVoiceActivationSetup<T>(
  resources: T,
  setup: () => Promise<void>,
  isCurrent: () => boolean,
  dispose: (resources: T) => Promise<void>,
): Promise<boolean> {
  await setup();
  if (isCurrent()) return true;
  await dispose(resources);
  return false;
}

export async function acquireVoiceMediaAfterCleanup<T>(
  requestMedia: () => Promise<T>,
  cleanup: Promise<void>,
  isCurrent: () => boolean,
  disposeStale: (media: T) => void,
): Promise<T | null> {
  const mediaPromise = requestMedia();
  void mediaPromise.catch(() => undefined);
  await cleanup;
  const media = await mediaPromise;
  if (isCurrent()) return media;
  disposeStale(media);
  return null;
}

function settleVoicePromiseByDeadline(
  promise: Promise<void>,
  deadlineMs: number,
): Promise<void> {
  return new Promise((resolve) => {
    const timeoutId = setTimeout(resolve, Math.max(0, deadlineMs - Date.now()));
    promise.then(
      () => {
        clearTimeout(timeoutId);
        resolve();
      },
      () => {
        clearTimeout(timeoutId);
        resolve();
      },
    );
  });
}

export function waitForVoiceReleaseBarrier<T>(
  previousCleanup: Promise<void>,
  resources: T | null,
  activationSetup: VoiceActivationSetupBarrier | null,
  fallbackTimeoutSeconds: number | undefined,
  dispose: (resources: T, deadlineMs: number) => Promise<void>,
): Promise<void> {
  const timeoutSeconds = activationSetup?.abortTimeoutSeconds
    ?? fallbackTimeoutSeconds
    ?? 0;
  const deadlineMs = activationSetup
    ? activationSetup.cleanupDeadlineMs
      ?? (activationSetup.cleanupDeadlineMs = Date.now() + timeoutSeconds * 1000)
    : Date.now() + timeoutSeconds * 1000;
  const boundedActivationSetup = activationSetup
    ? settleVoicePromiseByDeadline(activationSetup.promise, deadlineMs)
    : Promise.resolve();
  const cleanup = resources
    ? dispose(resources, deadlineMs)
    : Promise.resolve();
  return Promise.all([
    previousCleanup,
    boundedActivationSetup,
    cleanup,
  ]).then(() => undefined);
}

export function waitForVoiceAbortAcknowledgement(
  socket: WebSocket,
  timeoutSeconds: number,
): Promise<void> {
  const isClosed = () => socket.readyState === 3;
  if (isClosed()) return Promise.resolve();

  return new Promise((resolve) => {
    let settled = false;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;

    const requestLocalClose = () => {
      if (socket.readyState >= 2) return;
      try {
        socket.close();
      } catch {
        // The configured timeout remains the terminal recovery path.
      }
    };

    const finish = (closeLocally: boolean) => {
      if (settled) return;
      settled = true;
      if (timeoutId !== undefined) clearTimeout(timeoutId);
      socket.removeEventListener("message", handleMessage);
      socket.removeEventListener("close", handleClose);
      if (closeLocally) requestLocalClose();
      resolve();
    };
    const handleMessage = (message: MessageEvent<unknown>) => {
      if (typeof message.data !== "string") return;
      try {
        if (parseAsrServerEvent(message.data).type === "aborted") finish(true);
      } catch {
        // The active protocol handler owns validation of non-abort events.
      }
    };
    const handleClose = () => finish(false);

    socket.addEventListener("message", handleMessage);
    socket.addEventListener("close", handleClose);
    timeoutId = setTimeout(() => finish(true), timeoutSeconds * 1000);

    if (socket.readyState === 1) {
      try {
        socket.send(JSON.stringify({ type: "abort" }));
      } catch {
        requestLocalClose();
        if (isClosed()) finish(false);
      }
    } else if (socket.readyState === 0) {
      requestLocalClose();
      if (isClosed()) finish(false);
    }
  });
}

export function estimatePostGainLevel(
  samples: Float32Array,
  inputGainDb: number,
  clipLimit: number,
): VoiceLevelEstimate {
  if (samples.length === 0) {
    return { dbfs: -100, clippingRatio: 0, waveformSample: 0 };
  }

  const gain = 10 ** (inputGainDb / 20);
  let sumSquares = 0;
  let clippingCount = 0;
  let peak = 0;
  for (let index = 0; index < samples.length; index += 1) {
    const sample = samples[index];
    const amplified = sample * gain;
    if (Math.abs(amplified) >= clipLimit) clippingCount += 1;
    const clipped = Math.max(-clipLimit, Math.min(clipLimit, amplified));
    sumSquares += clipped * clipped;
    peak = Math.max(peak, Math.abs(clipped));
  }
  const rms = Math.sqrt(sumSquares / samples.length);
  return {
    dbfs: 20 * Math.log10(Math.max(rms, 1e-5)),
    clippingRatio: clippingCount / samples.length,
    waveformSample: peak,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function readString(record: Record<string, unknown>, key: string): string | null {
  return typeof record[key] === "string" ? record[key] : null;
}

function readFiniteNumber(record: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function invalidServerEvent(): never {
  throw new Error("Invalid ASR server event.");
}

export function parseAsrServerEvent(payload: string): AsrServerEvent {
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    return invalidServerEvent();
  }
  if (!isRecord(parsed) || typeof parsed.type !== "string") return invalidServerEvent();

  switch (parsed.type) {
    case "ready": {
      const sessionId = readString(parsed, "session_id");
      const protocolVersion = readFiniteNumber(parsed, "protocol_version");
      if (!sessionId || protocolVersion !== 1 || parsed.state !== "buffering") {
        return invalidServerEvent();
      }
      return { type: "ready", sessionId, protocolVersion, state: "buffering" };
    }
    case "level": {
      const dbfs = readFiniteNumber(parsed, "post_gain_dbfs", "dbfs", "rms_dbfs");
      const clippingRatio = readFiniteNumber(parsed, "clipping_ratio");
      if (dbfs === null || clippingRatio === null) return invalidServerEvent();
      return { type: "level", dbfs, clippingRatio };
    }
    case "state_changed":
      if (
        parsed.state !== "buffering" &&
        parsed.state !== "candidate" &&
        parsed.state !== "recording" &&
        parsed.state !== "finalizing"
      ) {
        return invalidServerEvent();
      }
      return { type: "state_changed", state: parsed.state };
    case "transcribing":
      return { type: "transcribing" };
    case "replay_audio": {
      const utteranceId = readString(parsed, "utterance_id");
      const mimeType = readString(parsed, "mime_type");
      const byteLength = readFiniteNumber(parsed, "byte_length");
      if (
        !utteranceId ||
        mimeType !== "audio/wav" ||
        byteLength === null ||
        !Number.isSafeInteger(byteLength) ||
        byteLength <= 0
      ) {
        return invalidServerEvent();
      }
      return { type: "replay_audio", utteranceId, mimeType, byteLength };
    }
    case "transcript_delta":
    case "transcript": {
      const text = readString(parsed, "text") ?? readString(parsed, "delta");
      if (text === null) return invalidServerEvent();
      if (parsed.type === "transcript") {
        const replayUtteranceId = readString(parsed, "replay_utterance_id") ?? undefined;
        if ("replay_utterance_id" in parsed && !replayUtteranceId) {
          return invalidServerEvent();
        }
        return replayUtteranceId
          ? { type: "transcript", text, replayUtteranceId }
          : { type: "transcript", text };
      }
      return { type: "transcript_delta", text };
    }
    case "utterance_error":
    case "fatal_error": {
      const message = readString(parsed, "message");
      const code = readString(parsed, "code") ?? undefined;
      if (!message) return invalidServerEvent();
      return { type: parsed.type, code, message };
    }
    case "input_gain_changed": {
      const inputGainDb = readFiniteNumber(parsed, "input_gain_db");
      if (inputGainDb === null) return invalidServerEvent();
      return { type: "input_gain_changed", inputGainDb };
    }
    case "control_error": {
      const code = readString(parsed, "code");
      const message = readString(parsed, "message");
      const inputGainDb = readFiniteNumber(parsed, "input_gain_db");
      if (code !== "invalid_input_gain" || !message || inputGainDb === null) {
        return invalidServerEvent();
      }
      return { type: "control_error", code, message, inputGainDb };
    }
    case "aborted":
      return { type: "aborted" };
    default:
      return invalidServerEvent();
  }
}

function stopStream(stream: MediaStream): void {
  for (const track of stream.getTracks()) {
    track.onended = null;
    track.stop();
  }
}

export function disposeVoiceResources(
  resources: VoiceResources,
  sendAbort: boolean,
  deadlineMs?: number,
): Promise<void> {
  if (resources.cleanupPromise) return resources.cleanupPromise;
  resources.closing = true;
  if (resources.pendingReplay) {
    resources.revokeReplay(resources.pendingReplay);
    resources.pendingReplay = null;
  }

  stopStream(resources.stream);
  resources.worklet?.port.postMessage({ type: "pause" });
  resources.worklet?.port.close();
  resources.source?.disconnect();
  resources.worklet?.disconnect();
  resources.sink?.disconnect();
  const cleanupDeadlineMs = deadlineMs
    ?? Date.now() + resources.abortTimeoutSeconds * 1000;
  let contextClose = Promise.resolve();
  try {
    contextClose = resources.context?.close() ?? contextClose;
  } catch {
    // Native teardown errors must not prevent session-local socket cleanup.
  }
  const audioCleanup = settleVoicePromiseByDeadline(contextClose, cleanupDeadlineMs);

  let socketCleanup: Promise<void>;
  if (!resources.socket) {
    socketCleanup = Promise.resolve();
  } else if (sendAbort) {
    const timeoutSeconds = Math.max(0, cleanupDeadlineMs - Date.now()) / 1000;
    socketCleanup = waitForVoiceAbortAcknowledgement(
      resources.socket,
      timeoutSeconds,
    );
  } else {
    resources.socket.close();
    socketCleanup = Promise.resolve();
  }
  // Admission of a replacement graph waits for native audio and ASR together.
  const previousTransportCleanup = settleVoicePromiseByDeadline(
    resources.transportCleanup ?? Promise.resolve(), cleanupDeadlineMs,
  );
  resources.cleanupPromise = Promise.all([audioCleanup, socketCleanup, previousTransportCleanup]).then(() => undefined);
  return resources.cleanupPromise;
}

export function classifyVoiceBrowserError(error: unknown): VoiceError {
  if (error instanceof DOMException && error.name === "NotAllowedError") {
    return {
      code: "permission_denied",
      message: "Microphone permission was denied. Allow microphone access and try again.",
      recoverable: true,
    };
  }
  if (error instanceof DOMException && error.name === "NotFoundError") {
    return {
      code: "microphone_unavailable",
      message: "No usable microphone was found.",
      recoverable: true,
    };
  }
  if (error instanceof DOMException && error.name === "NotReadableError") {
    return {
      code: "microphone_unavailable",
      message: "The microphone is busy or unavailable. Close other apps using it and try again.",
      recoverable: true,
    };
  }
  return {
    code: "microphone_unavailable",
    message: error instanceof Error ? error.message : "Voice input could not be started.",
    recoverable: true,
  };
}

export function shouldNotifyVoiceUtteranceError(code: string | undefined): boolean {
  return code !== "candidate_timeout";
}

export type VoiceSetupStage = "audio_worklet" | "configuration" | "websocket";

export function classifyVoiceSetupError(
  stage: VoiceSetupStage,
  _error: unknown,
): VoiceError {
  if (stage === "audio_worklet") {
    return {
      code: "audio_worklet_unavailable",
      message: "Browser audio worklet setup is unavailable in this deployment.",
      recoverable: false,
    };
  }
  if (stage === "configuration") {
    return {
      code: "protocol_error",
      message: "Voice input backend configuration is invalid.",
      recoverable: false,
    };
  }
  return {
    code: "websocket_error",
    message: "Could not connect to the voice transcription service. Check the connection and try again.",
    recoverable: true,
  };
}

export function useVoiceInput({
  config,
  isLoading,
  resumeBlocked = false,
  onFinalTranscript,
  onError,
}: VoiceInputOptions): VoiceInputController {
  const [state, dispatch] = useReducer(
    voiceInputReducer,
    undefined,
    createInitialVoiceInputState,
  );
  const [inputGainDbState, setInputGainDbState] = useState<number | null>(null);
  const stateRef = useRef(state);
  const generationRef = useRef(0);
  const resourcesRef = useRef<VoiceResources | null>(null);
  const replayObjectUrlsRef = useRef(new Set<string>());
  const cleanupBarrierRef = useRef<Promise<void>>(Promise.resolve());
  const activationSetupBarrierRef = useRef<VoiceActivationSetupBarrier | null>(null);
  const mountedRef = useRef(true);
  const callbacksRef = useRef({ onFinalTranscript, onError });
  const resumeEligibleRef = useRef(false);
  const inputGainDbRef = useRef(config?.inputGainDb ?? 0);
  const lastConfirmedInputGainDbRef = useRef(config?.inputGainDb ?? 0);
  const inputGainInitializedForConfigRef = useRef<VoiceInputConfig | null>(null);
  stateRef.current = state;
  callbacksRef.current = { onFinalTranscript, onError };
  resumeEligibleRef.current = Boolean(config?.enabled) && !isLoading && !resumeBlocked;

  useEffect(() => {
    if (!config) {
      inputGainInitializedForConfigRef.current = null;
      setInputGainDbState(null);
      return;
    }
    const range: InputGainRange = {
      defaultDb: config.inputGainDb,
      minDb: config.inputGainMinDb,
      maxDb: config.inputGainMaxDb,
      stepDb: config.inputGainStepDb,
    };
    const value = readInputGainDb(window.localStorage, range);
    inputGainDbRef.current = value;
    lastConfirmedInputGainDbRef.current = value;
    inputGainInitializedForConfigRef.current = config;
    setInputGainDbState(value);
  }, [config]);

  const setInputGainDb = useCallback((valueDb: number) => {
    if (!config) return;
    const range: InputGainRange = {
      defaultDb: config.inputGainDb,
      minDb: config.inputGainMinDb,
      maxDb: config.inputGainMaxDb,
      stepDb: config.inputGainStepDb,
    };
    const value = normalizeInputGainDb(valueDb, range);
    inputGainDbRef.current = value;
    setInputGainDbState(value);
    writeInputGainDb(window.localStorage, value);
    const resources = resourcesRef.current;
    const socket = resources?.socket;
    if (
      socket &&
      resources.socketReady &&
      !resources.closing &&
      socket.readyState === WebSocket.OPEN
    ) {
      try {
        socket.send(JSON.stringify({ type: "set_input_gain", input_gain_db: value }));
      } catch {
        callbacksRef.current.onError?.({
          code: "websocket_error",
          message: "Could not update the ASR input gain on this connection.",
          recoverable: true,
        });
      }
    }
  }, [config]);

  const releaseReplay = useCallback((replay: VoiceReplayAudio) => {
    URL.revokeObjectURL(replay.url);
    replayObjectUrlsRef.current.delete(replay.url);
  }, []);

  const releaseCurrent = useCallback((
    sendAbort: boolean,
    previousActivationSetup = activationSetupBarrierRef.current,
  ): Promise<void> => {
    const resources = resourcesRef.current;
    if (resources) resourcesRef.current = null;
    const timeoutSeconds = previousActivationSetup?.abortTimeoutSeconds
      ?? resources?.abortTimeoutSeconds;
    if (timeoutSeconds === undefined) return cleanupBarrierRef.current;
    cleanupBarrierRef.current = waitForVoiceReleaseBarrier(
      cleanupBarrierRef.current,
      resources,
      previousActivationSetup,
      timeoutSeconds,
      (capturedResources, deadlineMs) =>
        disposeVoiceResources(capturedResources, sendAbort, deadlineMs),
    );
    return cleanupBarrierRef.current;
  }, []);

  const reportFailure = useCallback(
    (generation: number, error: VoiceError) => {
      if (!mountedRef.current || generation !== generationRef.current) return;
      const failureGeneration = generationRef.current + 1;
      generationRef.current = failureGeneration;
      void releaseCurrent(true);
      dispatch({
        type: "startup_failure",
        generation: failureGeneration,
        error,
      });
      callbacksRef.current.onError?.(error);
    },
    [releaseCurrent],
  );

  const abortVoice = useCallback(async () => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    const cleanup = releaseCurrent(true);
    await cleanup;
    if (mountedRef.current) dispatch({ type: "deactivate", generation });
  }, [releaseCurrent]);

  const deactivateVoice = useCallback(async () => {
    const transcript = stateRef.current.pendingTranscript;
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    const cleanup = releaseCurrent(true);
    await cleanup;
    if (mountedRef.current) {
      dispatch({
        type: "deactivate",
        generation,
        preserveTranscript: true,
      });
    }
    return transcript;
  }, [releaseCurrent]);

  const resumeRetainedCapture = useCallback((resources: VoiceResources) => {
    if (!mountedRef.current || !resumeEligibleRef.current || resources.closing || resourcesRef.current !== resources
      || resources.generation !== generationRef.current || !resources.resumeRequested
      || !resources.serverReady || !resources.socketReady
      || resources.socket?.readyState !== WebSocket.OPEN) return;
    resources.capturePaused = false;
    resources.resumeRequested = false;
    resources.continuationPaused = false;
    resources.worklet?.port.postMessage({ type: "resume", epoch: resources.captureEpoch });
    dispatch({ type: "resume", generation: resources.generation, sessionId: resources.sessionId });
  }, []);

  const resumeVoice = useCallback(() => {
    if (!resumeEligibleRef.current) return;
    const resources = resourcesRef.current;
    if (!resources || resources.closing || !resources.capturePaused || resources.generation !== generationRef.current) return;
    resources.resumeRequested = true;
    if (!resources.socket && !resources.reconnectPending) {
      resources.reconnectPending = true;
      void resources.transportCleanup.then(() => {
        if (mountedRef.current && resumeEligibleRef.current && resourcesRef.current === resources && !resources.closing
          && resources.generation === generationRef.current && resources.resumeRequested
          && !resources.socket) resources.connectSocket?.();
      }).finally(() => { resources.reconnectPending = false; });
    }
    resumeRetainedCapture(resources);
  }, [config, isLoading, resumeBlocked, resumeRetainedCapture]);

  useEffect(() => {
    if (resourcesRef.current?.resumeRequested) resumeVoice();
  }, [resumeVoice]);

  const pauseForResponse = useCallback(() => {
    const generation = stateRef.current.generation;
    dispatch({ type: "pause_for_response", generation });
    const resources = resourcesRef.current;
    if (!resources || resources.closing) return;
    const discardUnfinishedUtterance = !resources.continuationPaused;
    pauseVoiceCapture(resources);
    resources.resumeRequested = false;
    resources.continuationPaused = true;
    // A manual send can interrupt an unfinished ASR utterance. Discard only
    // that transport/session; the user-activated microphone graph stays live.
    if (discardUnfinishedUtterance && resources.socket) {
      const socket = resources.socket;
      resources.expectedReplay = null;
      if (resources.pendingReplay) resources.revokeReplay(resources.pendingReplay);
      resources.pendingReplay = null;
      resources.socket = null;
      resources.socketReady = false;
      resources.serverReady = false;
      resources.transportCleanup = waitForVoiceAbortAcknowledgement(socket, resources.abortTimeoutSeconds);
    }
  }, []);

  const activateVoice = useCallback(async () => {
    if (!config?.enabled || isLoading) return;

    const previousActivationSetup = activationSetupBarrierRef.current;
    let finishActivationSetup!: () => void;
    const activationSetupBarrier = new Promise<void>((resolve) => {
      finishActivationSetup = resolve;
    });
    activationSetupBarrierRef.current = {
      promise: activationSetupBarrier,
      abortTimeoutSeconds: config.abortTimeoutSeconds,
    };

    try {
      const generation = generationRef.current + 1;
      generationRef.current = generation;
      const cleanup = releaseCurrent(true, previousActivationSetup);
      if (mountedRef.current) dispatch({ type: "activate", generation });

      const AudioContextConstructor = window.AudioContext;
      const capabilityError = getVoiceCapabilityError({
        isSecureContext: !config.requireSecureContext || window.isSecureContext,
        hasMediaDevices:
          typeof navigator.mediaDevices?.getUserMedia === "function" &&
          typeof AudioContextConstructor === "function",
        hasAudioWorklet:
          typeof window.AudioWorkletNode === "function" &&
          "audioWorklet" in AudioContextConstructor.prototype,
      });
      if (capabilityError) {
        reportFailure(generation, capabilityError);
        return;
      }

      let stream: MediaStream | null;
      try {
        stream = await acquireVoiceMediaAfterCleanup(
          () => navigator.mediaDevices.getUserMedia({
            audio: {
              channelCount: 1,
              sampleRate: config.sampleRateHz,
              echoCancellation: config.browserConstraints.echoCancellation,
              noiseSuppression: config.browserConstraints.noiseSuppression,
              autoGainControl: config.browserConstraints.autoGainControl,
            },
            video: false,
          }),
          cleanup,
          () => mountedRef.current && generation === generationRef.current,
          stopStream,
        );
      } catch (error) {
        reportFailure(generation, classifyVoiceBrowserError(error));
        return;
      }
      if (!stream) return;

      try {
      const resources: VoiceResources = {
        generation,
        stream,
        context: null,
        source: null,
        worklet: null,
        sink: null,
        socket: null,
        socketReady: false,
        sessionId: null,
        capturePaused: false,
        captureEpoch: 0,
        serverReady: false,
        resumeRequested: false,
        continuationPaused: false,
        reconnectPending: false,
        connectSocket: null,
        transportCleanup: Promise.resolve(),
        closing: false,
        abortTimeoutSeconds: config.abortTimeoutSeconds,
        cleanupPromise: null,
        expectedReplay: null,
        pendingReplay: null,
        revokeReplay: releaseReplay,
      };
      resourcesRef.current = resources;
      for (const track of stream.getTracks()) {
        track.onended = () => {
          reportFailure(generation, {
            code: "microphone_ended",
            message: "The microphone stopped providing audio.",
            recoverable: true,
          });
        };
      }

      const context = new AudioContextConstructor({ sampleRate: config.sampleRateHz });
      resources.context = context;
      if (context.sampleRate !== config.sampleRateHz) {
        reportFailure(generation, {
          code: "sample_rate_mismatch",
          message: `Browser opened ${context.sampleRate} Hz instead of ${config.sampleRateHz} Hz.`,
          recoverable: false,
        });
        return;
      }

      let handshake: AsrStartHandshake;
      try {
        handshake = buildAsrStartHandshake(config);
      } catch (error) {
        reportFailure(generation, classifyVoiceSetupError("configuration", error));
        return;
      }
      const setupCompleted = await completeVoiceActivationSetup(
        resources,
        () => context.audioWorklet.addModule(WORKLET_PATH),
        () =>
          generation === generationRef.current &&
          resourcesRef.current === resources,
        (capturedResources) =>
          disposeVoiceResources(capturedResources, false),
      );
      if (!setupCompleted) return;

      const source = context.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(context, "asr-pcm-capture", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: {
          frameSamples: handshake.frame_samples,
          maxBufferedFrames: config.maxBufferedFrames,
        },
      });
      const sink = context.createGain();
      sink.gain.value = 0;
      source.connect(worklet).connect(sink).connect(context.destination);
      resources.source = source;
      resources.worklet = worklet;
      resources.sink = sink;

      const hasPublicUrl = Boolean(config.publicWebSocketUrl?.trim());
      const backendPort = config.backendPort ?? Number(window.location.port);
      if (
        !hasPublicUrl &&
        (!Number.isInteger(backendPort) || backendPort <= 0 || backendPort > 65535)
      ) {
        reportFailure(
          generation,
          classifyVoiceSetupError("configuration", new Error("Invalid ASR backend port")),
        );
        return;
      }
      worklet.port.onmessage = (event: MessageEvent<unknown>) => {
        if (generation !== generationRef.current || resources.closing) return;
        if (isRecord(event.data) && event.data.type === "transport_overflow") {
          if (resources.capturePaused || event.data.epoch !== resources.captureEpoch) return;
          reportFailure(generation, {
            code: "transport_overflow",
            message: "Voice input paused because audio capture could not keep up.",
            recoverable: true,
          });
          return;
        }
        if (!isRecord(event.data) || event.data.type !== "pcm_frame"
          || !(event.data.samples instanceof Float32Array)) return;
        worklet.port.postMessage({ type: "frame_ack", epoch: event.data.epoch });
        if (resources.capturePaused || event.data.epoch !== resources.captureEpoch) return;
        const socket = resources.socket;
        if (resources.socketReady && socket?.readyState === WebSocket.OPEN) {
          const frame = float32ToLittleEndianBuffer(event.data.samples);
          if (
            !canBufferVoiceFrame(
              socket.bufferedAmount,
              frame.byteLength,
              config.maxBufferedFrames,
            )
          ) {
            reportFailure(generation, {
              code: "transport_overflow",
              message: "Voice input paused because the ASR connection could not keep up.",
              recoverable: true,
            });
            return;
          }
          socket.send(frame);
        }
      };

      const connectSocket = () => {
        let socket: WebSocket;
        try {
          socket = new WebSocket(
            buildAsrWebSocketUrl(config, window.location, hasPublicUrl ? 1 : backendPort),
          );
          socket.binaryType = "arraybuffer";
        } catch (error) {
          reportFailure(generation, classifyVoiceSetupError("websocket", error));
          return;
        }
        resources.socket = socket;

        socket.onopen = () => {
          if (generation !== generationRef.current || resources.closing || resources.socket !== socket) return;
          try {
            socket.send(JSON.stringify(handshake));
          } catch (error) {
            reportFailure(generation, classifyVoiceSetupError("websocket", error));
          }
        };
        socket.onmessage = (message: MessageEvent<unknown>) => {
          if (
            generation !== generationRef.current ||
            resources.closing || resources.socket !== socket
          ) {
            return;
          }
          if (typeof message.data !== "string") {
            const metadata = resources.expectedReplay;
            resources.expectedReplay = null;
            if (!metadata || !(message.data instanceof Blob || message.data instanceof ArrayBuffer)) {
              return;
            }
            const replay = createVoiceReplayAudio(metadata, message.data);
            if (replay) {
              resources.pendingReplay = replay;
              replayObjectUrlsRef.current.add(replay.url);
            }
            return;
          }
          let event: AsrServerEvent;
          try {
            event = parseAsrServerEvent(message.data);
          } catch {
            reportFailure(generation, {
              code: "protocol_error",
              message: "The ASR server returned an invalid event.",
              recoverable: true,
            });
            return;
          }

          switch (event.type) {
            case "ready":
              try {
                socket.send(JSON.stringify({
                  type: "set_input_gain",
                  input_gain_db: inputGainDbRef.current,
                }));
              } catch (error) {
                reportFailure(generation, classifyVoiceSetupError("websocket", error));
                return;
              }
              resources.socketReady = true;
              resources.sessionId = event.sessionId;
              resources.serverReady = true;
              if (resources.capturePaused) {
                resumeRetainedCapture(resources);
                break;
              }
              dispatch({
                type: "ready",
                generation,
                sessionId: event.sessionId,
              });
              break;
            case "level":
              if (resources.capturePaused) break;
              dispatch({
                type: "level",
                generation,
                dbfs: event.dbfs,
                clippingRatio: event.clippingRatio,
              });
              break;
            case "state_changed":
              if (event.state === "buffering") {
                resources.serverReady = true;
                if (resources.capturePaused) {
                  resumeRetainedCapture(resources);
                  break;
                }
              }
              if (resources.capturePaused) break;
              dispatch({
                type: "state_changed",
                generation,
                state: event.state,
              });
              break;
            case "transcribing":
              pauseVoiceCapture(resources);
              resources.serverReady = false;
              resources.resumeRequested = false;
              dispatch({ type: "transcribing", generation });
              break;
            case "replay_audio":
              if (resources.pendingReplay) {
                resources.revokeReplay(resources.pendingReplay);
              }
              resources.expectedReplay = event;
              resources.pendingReplay = null;
              break;
            case "transcript_delta":
              dispatch({
                type: "transcript_delta",
                generation,
                text: event.text,
              });
              break;
            case "transcript": {
              const transcript = event.text;
              const pendingReplay = resources.pendingReplay;
              const replay = event.replayUtteranceId
                && pendingReplay?.utteranceId === event.replayUtteranceId
                ? pendingReplay
                : undefined;
              if (pendingReplay && !replay) resources.revokeReplay(pendingReplay);
              resources.pendingReplay = null;
              pauseVoiceCapture(resources);
              resources.serverReady = false;
              resources.continuationPaused = true;
              dispatch({ type: "transcript", generation, text: transcript, replay });
              if (config.autoSend && transcript.trim()) {
                dispatch({ type: "pause_for_response", generation });
                void Promise.resolve(
                  callbacksRef.current.onFinalTranscript(transcript, replay),
                );
              }
              break;
            }
            case "utterance_error": {
              if (!resources.continuationPaused) {
                resources.resumeRequested = true;
                dispatch({ type: "utterance_error", generation });
                resumeRetainedCapture(resources);
              }
              if (shouldNotifyVoiceUtteranceError(event.code)) {
                callbacksRef.current.onError?.({
                  code: "backend_error",
                  message: event.message,
                  recoverable: true,
                });
              }
              break;
            }
            case "input_gain_changed":
              lastConfirmedInputGainDbRef.current = event.inputGainDb;
              break;
            case "control_error":
              inputGainDbRef.current = event.inputGainDb;
              lastConfirmedInputGainDbRef.current = event.inputGainDb;
              setInputGainDbState(event.inputGainDb);
              writeInputGainDb(window.localStorage, event.inputGainDb);
              callbacksRef.current.onError?.({
                code: "backend_error",
                message: event.message,
                recoverable: true,
              });
              break;
            case "fatal_error":
              reportFailure(generation, {
                code: "backend_error",
                message: event.message,
                recoverable: false,
              });
              break;
            case "aborted":
              void abortVoice();
              break;
          }
        };
        socket.onerror = () => {
          if (resources.socket !== socket) return;
          reportFailure(
            generation,
            classifyVoiceSetupError("websocket", new Error("ASR WebSocket connection failed")),
          );
        };
        socket.onclose = () => {
          if (generation === generationRef.current && !resources.closing && resources.socket === socket) {
            reportFailure(generation, {
              code: "websocket_error",
              message: "The ASR WebSocket connection closed unexpectedly.",
              recoverable: true,
            });
          }
        };

      };
      resources.connectSocket = connectSocket;
      connectSocket();

      await context.resume();
      } catch (error) {
        reportFailure(generation, classifyVoiceSetupError("audio_worklet", error));
      }
    } finally {
      finishActivationSetup();
    }
  }, [abortVoice, config, isLoading, releaseCurrent, releaseReplay, reportFailure, resumeRetainedCapture]);

  useEffect(() => {
    mountedRef.current = true;
    // 切换标签页/应用（visibilityState === "hidden"）不终止语音采集：
    // getUserMedia + AudioWorklet + WebSocket 在后台标签页持续运行，
    // 转写与发送照常完成；真正的页面卸载由 unmount 清理。
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
      void releaseCurrent(true);
      replayObjectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
      replayObjectUrlsRef.current.clear();
    };
  }, [abortVoice, releaseCurrent]);

  return {
    state,
    isVoiceMode: state.phase !== "keyboard",
    canActivate: Boolean(config?.enabled) && !isLoading,
    activateVoice,
    resumeVoice,
    deactivateVoice,
    abortVoice,
    pauseForResponse,
    releaseReplay,
    inputGain: config && inputGainDbState !== null
      ? {
          valueDb: inputGainDbState,
          minDb: config.inputGainMinDb,
          maxDb: config.inputGainMaxDb,
          stepDb: config.inputGainStepDb,
          setValueDb: setInputGainDb,
        }
      : null,
  };
}
