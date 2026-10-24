export const VOICE_WAVEFORM_HISTORY_LIMIT = 64;

export type VoiceInputPhase =
  | "keyboard"
  | "requesting_permission"
  | "listening"
  | "candidate"
  | "recording"
  | "finalizing"
  | "transcribing"
  | "transcript_ready"
  | "paused_for_response"
  | "error";

export type VoiceErrorCode =
  | "insecure_context"
  | "media_devices_unavailable"
  | "audio_worklet_unavailable"
  | "permission_denied"
  | "microphone_unavailable"
  | "microphone_ended"
  | "sample_rate_mismatch"
  | "transport_overflow"
  | "websocket_error"
  | "protocol_error"
  | "backend_error";

export interface VoiceError {
  code: VoiceErrorCode;
  message: string;
  recoverable: boolean;
}

export interface VoiceReplayAudio {
  utteranceId: string;
  mimeType: string;
  url: string;
}

export interface VoiceCapabilityEnvironment {
  isSecureContext: boolean;
  hasMediaDevices: boolean;
  hasAudioWorklet: boolean;
}

export interface VoiceInputState {
  phase: VoiceInputPhase;
  generation: number;
  sessionId: string | null;
  levelDbfs: number | null;
  clippingRatio: number;
  waveform: number[];
  partialTranscript: string;
  pendingTranscript: string;
  pendingReplay: VoiceReplayAudio | null;
  error: VoiceError | null;
}

export type VoiceInputEvent =
  | { type: "activate"; generation: number }
  | { type: "deactivate"; generation: number; preserveTranscript?: boolean }
  | { type: "startup_failure"; generation: number; error: VoiceError }
  | { type: "ready"; generation: number; sessionId: string }
  | { type: "resume"; generation: number; sessionId?: string | null }
  | {
      type: "level";
      generation: number;
      dbfs: number;
      clippingRatio: number;
      waveformSample?: number;
    }
  | {
      type: "state_changed";
      generation: number;
      state: "buffering" | "candidate" | "recording" | "finalizing";
    }
  | { type: "transcribing"; generation: number }
  | { type: "transcript_delta"; generation: number; text: string }
  | {
      type: "transcript";
      generation: number;
      text: string;
      replay?: VoiceReplayAudio;
    }
  | { type: "utterance_error"; generation: number }
  | { type: "pause_for_response"; generation: number }
  | { type: "error"; generation: number; error: VoiceError };

export function createInitialVoiceInputState(generation = 0): VoiceInputState {
  return {
    phase: "keyboard",
    generation,
    sessionId: null,
    levelDbfs: null,
    clippingRatio: 0,
    waveform: [],
    partialTranscript: "",
    pendingTranscript: "",
    pendingReplay: null,
    error: null,
  };
}

export function getVoiceCapabilityError(
  environment: VoiceCapabilityEnvironment,
): VoiceError | null {
  if (!environment.isSecureContext) {
    return {
      code: "insecure_context",
      message: "Voice input requires HTTPS (or localhost) in this browser.",
      recoverable: false,
    };
  }
  if (!environment.hasMediaDevices) {
    return {
      code: "media_devices_unavailable",
      message: "This browser cannot request microphone access.",
      recoverable: false,
    };
  }
  if (!environment.hasAudioWorklet) {
    return {
      code: "audio_worklet_unavailable",
      message: "This browser does not support AudioWorklet voice capture.",
      recoverable: false,
    };
  }
  return null;
}

const PHASE_BY_BACKEND_STATE: Record<
  Extract<VoiceInputEvent, { type: "state_changed" }>['state'],
  VoiceInputPhase
> = {
  buffering: "listening",
  candidate: "candidate",
  recording: "recording",
  finalizing: "finalizing",
};

export function voiceInputReducer(
  state: VoiceInputState,
  event: VoiceInputEvent,
): VoiceInputState {
  if (event.type === "activate") {
    if (event.generation <= state.generation) return state;
    return {
      ...createInitialVoiceInputState(event.generation),
      phase: "requesting_permission",
    };
  }

  if (event.type === "deactivate") {
    if (event.generation <= state.generation) return state;
    const transcript = event.preserveTranscript ? state.pendingTranscript : "";
    const replay = event.preserveTranscript ? state.pendingReplay : null;
    return {
      ...createInitialVoiceInputState(event.generation),
      pendingTranscript: transcript,
      pendingReplay: replay,
    };
  }

  if (event.type === "startup_failure") {
    if (event.generation <= state.generation) return state;
    return {
      ...createInitialVoiceInputState(event.generation),
      error: event.error,
    };
  }

  if (event.generation !== state.generation) return state;

  switch (event.type) {
    case "resume":
      return {
        ...state, phase: "listening", partialTranscript: "", pendingTranscript: "",
        pendingReplay: null, error: null, sessionId: event.sessionId ?? state.sessionId,
      };
    case "ready":
      return {
        ...state,
        phase: "listening",
        sessionId: event.sessionId,
        error: null,
      };
    case "level": {
      const waveformSample = Number.isFinite(event.waveformSample)
        ? Math.max(0, Math.min(1, event.waveformSample!))
        : Math.max(0, Math.min(1, (event.dbfs + 80) / 80));
      return {
        ...state,
        levelDbfs: event.dbfs,
        clippingRatio: event.clippingRatio,
        waveform: [...state.waveform, waveformSample].slice(
          -VOICE_WAVEFORM_HISTORY_LIMIT,
        ),
      };
    }
    case "state_changed":
      return { ...state, phase: PHASE_BY_BACKEND_STATE[event.state] };
    case "transcribing":
      return { ...state, phase: "transcribing" };
    case "transcript_delta":
      return {
        ...state,
        partialTranscript: state.partialTranscript + event.text,
      };
    case "transcript": {
      const transcript = event.text;
      return {
        ...state,
        phase: "transcript_ready",
        partialTranscript: "",
        pendingTranscript: transcript,
        pendingReplay: event.replay ?? null,
      };
    }
    case "utterance_error":
      return {
        ...state,
        phase: "listening",
        partialTranscript: "",
        pendingTranscript: "",
        pendingReplay: null,
      };
    case "pause_for_response":
      return {
        ...state,
        phase: "paused_for_response",
        partialTranscript: "",
        pendingTranscript: "",
        pendingReplay: null,
      };
    case "error":
      return { ...state, phase: "error", error: event.error };
  }
}
