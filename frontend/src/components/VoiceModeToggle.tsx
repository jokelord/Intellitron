"use client";

import { AudioLines, Keyboard, Mic } from "lucide-react";
import { HoverHint } from "./HoverHint";

export type VoiceInputMode = "keyboard" | "dictation" | "voice";

export interface VoiceModeToggleProps {
  mode: VoiceInputMode;
  onModeChange: (mode: VoiceInputMode) => void;
  disabled?: boolean;
  voiceDisabled?: boolean;
  voiceDisabledReason?: string;
  className?: string;
}

export function VoiceModeToggle({
  mode,
  onModeChange,
  disabled = false,
  voiceDisabled = false,
  voiceDisabledReason,
  className = "",
}: VoiceModeToggleProps) {
  const baseClass =
    "inline-flex min-w-[36px] items-center justify-center rounded-lg px-2 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nvidia-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50";
  const activeClass = "bg-white text-gray-950 shadow-sm dark:bg-gray-700 dark:text-white";
  const idleClass = "text-gray-600 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white";
  const hintLabel = (defaultLabel: string, isDisabled: boolean) =>
    isDisabled && voiceDisabledReason ? voiceDisabledReason : defaultLabel;

  return (
    <div className={`flex flex-wrap items-center gap-2 ${className}`}>
      <div
        role="group"
        aria-label="消息输入模式"
        className="inline-flex flex-wrap rounded-xl border border-gray-200 webui-header-tint p-1 dark:border-gray-700"
      >
        <HoverHint label={hintLabel("键盘输入", disabled)}>
          <button
            type="button"
            aria-label="键盘输入"
            aria-pressed={mode === "keyboard"}
            disabled={disabled}
            onClick={() => onModeChange("keyboard")}
            className={`${baseClass} min-h-[36px] ${mode === "keyboard" ? activeClass : idleClass}`}
          >
            <Keyboard aria-hidden="true" className="h-4 w-4" />
          </button>
        </HoverHint>
        <HoverHint label={hintLabel("听写输入，识别后可编辑再发送", disabled || voiceDisabled)}>
          <button
            type="button"
            aria-label="听写输入"
            aria-pressed={mode === "dictation"}
            aria-describedby={voiceDisabledReason ? "voice-mode-disabled-reason" : undefined}
            disabled={disabled || voiceDisabled}
            onClick={() => onModeChange("dictation")}
            className={`${baseClass} min-h-[36px] ${mode === "dictation" ? activeClass : idleClass}`}
          >
            <AudioLines aria-hidden="true" className="h-4 w-4" />
          </button>
        </HoverHint>
        <HoverHint label={hintLabel("语音输入", disabled || voiceDisabled)}>
          <button
            type="button"
            aria-label="语音输入"
            aria-pressed={mode === "voice"}
            aria-describedby={voiceDisabledReason ? "voice-mode-disabled-reason" : undefined}
            disabled={disabled || voiceDisabled}
            onClick={() => onModeChange("voice")}
            className={`${baseClass} min-h-[36px] ${mode === "voice" ? activeClass : idleClass}`}
          >
            <Mic aria-hidden="true" className="h-4 w-4" />
          </button>
        </HoverHint>
      </div>
      {voiceDisabledReason ? (
        <span id="voice-mode-disabled-reason" className="sr-only">
          {voiceDisabledReason}
        </span>
      ) : null}
    </div>
  );
}
