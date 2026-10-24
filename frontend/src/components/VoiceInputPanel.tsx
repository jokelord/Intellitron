"use client";

import { AlertTriangle, Keyboard, RotateCcw } from "lucide-react";

import type { VoiceInputState } from "../lib/voice-input-state";
import type { VoiceInputGainControl } from "../lib/voice-input";

import { VoiceListeningIndicator } from "./VoiceListeningIndicator";

export interface VoiceInputPanelProps {
  state: VoiceInputState;
  inputGain: VoiceInputGainControl | null;
  onRetry?: () => void;
  onSwitchToKeyboard?: (pendingTranscript: string) => void;
  disabled?: boolean;
  className?: string;
}

export function VoiceInputPanel({
  state,
  inputGain,
  onRetry,
  onSwitchToKeyboard,
  disabled = false,
  className = "",
}: VoiceInputPanelProps) {
  const displayTranscript = state.pendingTranscript || state.partialTranscript;

  return (
    <section
      aria-label="Voice input"
      className={`w-full rounded-2xl border border-gray-200 bg-white px-3 py-[4px] shadow-sm dark:border-gray-700 dark:bg-gray-800 ${className}`}
    >
      <VoiceListeningIndicator state={state} inputGain={inputGain} compact />

      {displayTranscript ? (
        <div className="mt-4">
          <p className="mb-1 text-xs font-medium text-gray-600 dark:text-gray-300">
            Pending transcript
          </p>
          <div
            role="textbox"
            aria-readonly="true"
            aria-live="polite"
            className="min-h-[44px] w-full rounded-xl border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-900 outline-none dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
          >
            {displayTranscript}
          </div>
        </div>
      ) : null}

      {state.error ? (
        <div className="mt-4 flex items-start gap-3 rounded-xl border border-amber-300 bg-amber-50 p-3 text-amber-900 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-100">
          <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="min-w-0 flex-1">
            <p className="text-sm">{state.error.message}</p>
            <div className="mt-2 flex flex-wrap gap-2">
              {state.error.recoverable && onRetry ? (
                <button
                  type="button"
                  disabled={disabled}
                  onClick={onRetry}
                  className="inline-flex min-h-[44px] items-center gap-2 rounded-lg border border-amber-400 px-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-600 disabled:opacity-50"
                >
                  <RotateCcw aria-hidden="true" className="h-4 w-4" />
                  Try again
                </button>
              ) : null}
              {onSwitchToKeyboard ? (
                <button
                  type="button"
                  onClick={() => onSwitchToKeyboard(state.pendingTranscript)}
                  className="inline-flex min-h-[44px] items-center gap-2 rounded-lg px-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-600"
                >
                  <Keyboard aria-hidden="true" className="h-4 w-4" />
                  Use keyboard
                </button>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
