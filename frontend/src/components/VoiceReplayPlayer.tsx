"use client";

import type { VoiceReplayAudio } from "../lib/voice-input-state";

export interface VoiceReplayPlayerProps {
  replay: VoiceReplayAudio;
}

export function VoiceReplayPlayer({ replay }: VoiceReplayPlayerProps) {
  return (
    <audio
      aria-label="语音输入回放"
      className="mt-3 h-10 w-full max-w-sm"
      controls
      preload="metadata"
      src={replay.url}
    />
  );
}
