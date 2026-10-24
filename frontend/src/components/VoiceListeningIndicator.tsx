"use client";

import { Mic, WandSparkles } from "lucide-react";

import type { VoiceInputGainControl } from "../lib/voice-input";
import type { VoiceInputPhase, VoiceInputState } from "../lib/voice-input-state";
import {
  formatInputGainLabel,
  formatPostGainEstimate,
} from "../lib/voice-level-format";

export interface VoiceListeningIndicatorProps {
  state: VoiceInputState;
  inputGain: VoiceInputGainControl | null;
  className?: string;
  compact?: boolean;
  onMagicSendSettings?: () => void;
}

const PHASE_LABELS: Record<VoiceInputPhase, string> = {
  keyboard: "Keyboard input",
  requesting_permission: "Requesting microphone permission",
  listening: "Listening - pre-trigger active",
  candidate: "Sound detected - confirming speech",
  recording: "Recording",
  finalizing: "Collecting final audio",
  transcribing: "SGLang is transcribing",
  transcript_ready: "Transcript ready",
  paused_for_response: "Sent - waiting for response",
  error: "Voice input error",
};

export function waveformPoints(samples: readonly number[]): string {
  const bounded = samples.slice(-64);
  if (bounded.length === 0) return "0,56 256,56";
  return bounded
    .map((sample, index) => {
      const x = bounded.length === 1 ? 128 : (index / (bounded.length - 1)) * 256;
      const y = 56 - Math.max(0, Math.min(1, sample)) * 50;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

export function VoiceListeningIndicator({
  state,
  inputGain,
  className = "",
  compact = false,
  onMagicSendSettings,
}: VoiceListeningIndicatorProps) {
  const inputGainDb = inputGain?.valueDb ?? 0;
  const isCapturing =
    state.phase === "listening" ||
    state.phase === "candidate" ||
    state.phase === "recording" ||
    state.phase === "finalizing";
  const points = waveformPoints(state.waveform);

  return (
    <div className={`flex flex-wrap items-center justify-between ${compact ? "gap-2" : "gap-3"} ${className}`}>
      <div className={`flex min-w-0 items-center ${compact ? "gap-2" : "gap-3"}`}>
        {onMagicSendSettings ? (
          <button
            type="button"
            onClick={onMagicSendSettings}
            aria-label="Dictation magic send settings"
            title="Dictation magic send settings"
            className={`flex ${compact ? "h-9 w-9" : "h-10 w-10"} shrink-0 items-center justify-center rounded-full border border-nvidia-400 bg-nvidia-50 text-nvidia-700 shadow-sm transition-colors hover:bg-nvidia-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nvidia-500 focus-visible:ring-offset-2 dark:border-nvidia-400 dark:bg-gray-950 dark:text-nvidia-300 dark:hover:bg-nvidia-900 dark:hover:text-nvidia-100 dark:focus-visible:ring-offset-gray-900`}
          >
            <WandSparkles aria-hidden="true" className={compact ? "h-4 w-4" : "h-5 w-5"} />
          </button>
        ) : (
        <span
          className={`flex ${compact ? "h-9 w-9" : "h-10 w-10"} shrink-0 items-center justify-center rounded-full ${
            isCapturing
              ? "bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-300"
              : "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-200"
          }`}
        >
          <Mic aria-hidden="true" className={compact ? "h-4 w-4" : "h-5 w-5"} />
        </span>
        )}
        <div className="min-w-0">
          <p
            aria-live="polite"
            aria-atomic="true"
            className="text-sm font-semibold text-gray-950 dark:text-white"
          >
            {PHASE_LABELS[state.phase]}
          </p>
          <p className="flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <span>
              {formatPostGainEstimate(state.levelDbfs)} / {formatInputGainLabel(inputGainDb)}
            </span>
            {inputGain ? (
              <input
                type="range"
                min={inputGain.minDb}
                max={inputGain.maxDb}
                step={inputGain.stepDb}
                value={inputGain.valueDb}
                onChange={(event) => inputGain.setValueDb(event.currentTarget.valueAsNumber)}
                aria-label="Input gain"
                aria-valuemin={inputGain.minDb}
                aria-valuemax={inputGain.maxDb}
                aria-valuenow={inputGain.valueDb}
                className="h-2 min-w-[120px] flex-1 cursor-pointer accent-nvidia-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nvidia-500"
              />
            ) : null}
          </p>
        </div>
      </div>
      <div
        className={
          compact
            ? "min-w-[130px] flex-1 overflow-hidden rounded-xl border border-gray-200 bg-gray-950 px-1.5 py-0.5 dark:border-gray-700"
            : "min-w-[130px] flex-1 overflow-hidden rounded-xl border border-gray-200 bg-gray-950 px-2 py-1 dark:border-gray-700"
        }
      >
        <svg
          viewBox="0 0 256 64"
          role="img"
          aria-label="Recent microphone level"
          className={compact ? "h-8 w-full" : "h-10 w-full"}
          preserveAspectRatio="none"
        >
          <line x1="0" y1="56" x2="256" y2="56" stroke="#374151" strokeWidth="1" />
          <polyline
            points={points}
            fill="none"
            stroke={state.phase === "recording" ? "#ef4444" : "#76b900"}
            strokeWidth="3"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
    </div>
  );
}
