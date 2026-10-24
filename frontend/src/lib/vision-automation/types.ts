export type VisionAutomationPhase =
  | "idle"
  | "starting"
  | "capturing"
  | "freezing"
  | "waiting_for_model"
  | "capturing_during_model"
  | "waiting_for_sources"
  | "paused_source_error"
  | "retryable_submit_error"
  | "aborting";

export interface AutomationBinding {
  pageInstanceId: string;
  pageGeneration: number;
  mode: "chat" | "agent";
  contextId: string;
  automationTaskId: string;
}

export interface SourceDescriptor {
  sourceId: string;
  kind: "display" | "camera";
  stableAlias: "desktop" | `camera_${number}`;
  label: string;
  deviceId?: string;
}

export interface ImageResource {
  uploadBlob: Blob | null;
  thumbnailBlob: Blob | null;
  disposed: boolean;
  acquireObjectURL(kind: "upload" | "thumbnail"): string;
  releaseObjectURL(url: string): boolean;
  dispose(): void;
}

export interface AutomationFrame {
  source: SourceDescriptor;
  resource: ImageResource;
  capturedAtMs?: number;
}

export interface TickBundle {
  tickIndex: number;
  timestampMs: number;
  relativeTimeSeconds: number;
  frames: readonly AutomationFrame[];
}

export interface CompressionTier {
  quality: number;
  maxDimensionPx: number;
}
