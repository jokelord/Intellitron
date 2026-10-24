import type {
  ImageResource,
  SourceDescriptor,
  VisionAutomationPhase,
} from "./types.ts";

export const MANUAL_IMAGE_HARD_LIMIT = 9;

export function getManualImageLimit(configuredMaxImages: number): number {
  if (!Number.isFinite(configuredMaxImages)) return MANUAL_IMAGE_HARD_LIMIT;
  return Math.max(0, Math.min(MANUAL_IMAGE_HARD_LIMIT, Math.floor(configuredMaxImages)));
}

export function appendManualImages(
  previous: readonly string[],
  incoming: readonly string[],
  configuredMaxImages: number,
): string[] {
  const limit = getManualImageLimit(configuredMaxImages);
  const retained = previous.slice(0, limit);
  const available = Math.max(0, limit - retained.length);
  if (available === 0 || incoming.length === 0) return retained;
  return [...retained, ...incoming.slice(0, available)];
}

export type ImageUploaderActionId = "upload" | "manual_photo" | "automation" | "cycle";

export interface ImageUploaderActionStateInput {
  imageCount: number;
  configuredMaxImages: number;
  manualCameraVisible: boolean;
  automationEnabled: boolean;
  automationArmed: boolean;
  automationExpanded: boolean;
  cycleEnabled: boolean;
  cycleRunning: boolean;
}

export interface ImageUploaderActionState {
  actions: Array<{
    id: ImageUploaderActionId;
    visible: boolean;
    disabled: boolean;
    mode: "open" | "close";
  }>;
  automationExpanded: boolean;
}

export function getImageUploaderActionState(
  input: ImageUploaderActionStateInput,
): ImageUploaderActionState {
  const canAddMore = input.imageCount < getManualImageLimit(input.configuredMaxImages);
  return {
    actions: [
      { id: "upload", visible: canAddMore, disabled: false, mode: "open" },
      {
        id: "manual_photo",
        visible: canAddMore || input.manualCameraVisible,
        disabled: !canAddMore && !input.manualCameraVisible,
        mode: input.manualCameraVisible ? "close" : "open",
      },
      {
        id: "automation",
        visible: input.automationEnabled,
        disabled: input.cycleRunning,
        mode: input.automationExpanded ? "close" : "open",
      },
      {
        id: "cycle",
        visible: input.cycleEnabled,
        disabled: input.automationArmed,
        mode: "open",
      },
    ],
    automationExpanded: input.automationExpanded,
  };
}

export function shouldMountAutomationPresentation(input: {
  enabled: boolean;
  expanded: boolean;
  actionButtonsVisible: boolean;
  panelAvailable: boolean;
}): boolean {
  return input.enabled && input.expanded && input.actionButtonsVisible && input.panelAvailable;
}

export function getVisionAutomationPanelState(input: {
  armed: boolean;
  collapsed: boolean;
  cycleRunning: boolean;
  selectedSourceCount: number;
  bundleCount: number;
  startDisabled?: boolean;
}) {
  return {
    controlsLocked: input.armed,
    canStart: !input.startDisabled
      && !input.armed
      && !input.cycleRunning
      && input.selectedSourceCount > 0,
    cameraRefreshDisabled: input.armed,
    showThumbnailMatrix: !input.collapsed && input.bundleCount > 0,
  };
}

export interface VisionAutomationStatusPresentation {
  label: string;
  tone: "blue" | "gray" | "amber";
  className: string;
}

export function getVisionAutomationStatus(
  phase: VisionAutomationPhase,
  armed: boolean,
): VisionAutomationStatusPresentation | null {
  if (!armed) return null;
  const motion = "motion-safe:animate-spin motion-reduce:animate-none";
  if (phase === "paused_source_error" || phase === "waiting_for_sources") {
    return {
      label: "视觉自动化正在等待视觉源或视觉源异常",
      tone: "amber",
      className: `border-amber-400 ${motion}`,
    };
  }
  if (phase === "waiting_for_model") {
    return {
      label: "视觉自动化正在等待模型稳定结束",
      tone: "gray",
      className: `border-gray-400 ${motion}`,
    };
  }
  if (phase === "capturing" || phase === "capturing_during_model" || phase === "freezing") {
    return {
      label: "视觉自动化正在采集",
      tone: "blue",
      className: `border-blue-400 ${motion}`,
    };
  }
  return {
    label: "视觉自动化已启动",
    tone: "gray",
    className: `border-gray-400 ${motion}`,
  };
}

export type VisionAutomationSourceHealthStatus = "starting" | "healthy" | "waiting" | "error";

export interface VisionAutomationSourceHealth {
  sourceId: string;
  status: VisionAutomationSourceHealthStatus;
  error?: string | null;
}

export interface SourceHealthPresentation {
  sourceId: string;
  label: string;
  status: VisionAutomationSourceHealthStatus;
  statusLabel: string;
  error: string | null;
}

const SOURCE_STATUS_LABELS: Record<VisionAutomationSourceHealthStatus, string> = {
  starting: "启动中",
  healthy: "正常",
  waiting: "等待中",
  error: "异常",
};

export function getSourceHealthPresentation(
  sources: readonly SourceDescriptor[],
  health: readonly VisionAutomationSourceHealth[],
): SourceHealthPresentation[] {
  const bySourceId = new Map(health.map((item) => [item.sourceId, item]));
  return sources.map((source) => {
    const item = bySourceId.get(source.sourceId) ?? {
      sourceId: source.sourceId,
      status: "waiting" as const,
      error: null,
    };
    return {
      sourceId: source.sourceId,
      label: source.label,
      status: item.status,
      statusLabel: SOURCE_STATUS_LABELS[item.status],
      error: item.error ?? null,
    };
  });
}

export function mergeSourceHealthPresentation(
  activeSources: readonly SourceDescriptor[],
  availableSources: readonly SourceDescriptor[],
  health: readonly VisionAutomationSourceHealth[],
): SourceHealthPresentation[] {
  const activeIds = new Set(activeSources.map((source) => source.sourceId));
  const updatedIds = new Set(health.map((item) => item.sourceId));
  const explicitlyUpdatedInactiveSources = availableSources.filter((source) => (
    !activeIds.has(source.sourceId) && updatedIds.has(source.sourceId)
  ));
  return getSourceHealthPresentation(
    [...activeSources, ...explicitlyUpdatedInactiveSources],
    health,
  );
}

export interface ImagePreviewURLLease {
  url: string;
  release(): void;
}

export function createImagePreviewURLLease(resource: ImageResource): ImagePreviewURLLease {
  if (resource.disposed || resource.uploadBlob === null) {
    throw new Error("Image preview resource is unavailable");
  }
  const url = resource.acquireObjectURL("upload");
  let released = false;
  return {
    url,
    release() {
      if (released) return;
      released = true;
      resource.releaseObjectURL(url);
    },
  };
}
