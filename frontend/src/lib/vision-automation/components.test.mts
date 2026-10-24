import assert from "node:assert/strict";
import { test } from "node:test";

import {
  appendManualImages,
  createImagePreviewURLLease,
  getImageUploaderActionState,
  getSourceHealthPresentation,
  getVisionAutomationPanelState,
  getVisionAutomationStatus,
  mergeSourceHealthPresentation,
  shouldMountAutomationPresentation,
} from "./ui-contracts.ts";
import type { ImageResource, SourceDescriptor } from "./types.ts";

test("upload and concurrent camera commits enforce the manual nine-image cap atomically", () => {
  const uploaded = Array.from({ length: 8 }, (_, index) => `upload-${index}`);
  const firstCommit = appendManualImages([], uploaded, 20);
  const firstCamera = appendManualImages(firstCommit, ["camera-1"], 20);
  const secondCamera = appendManualImages(firstCamera, ["camera-2"], 20);

  assert.equal(firstCommit.length, 8);
  assert.deepEqual(firstCamera, [...uploaded, "camera-1"]);
  assert.deepEqual(secondCamera, firstCamera);
  assert.equal(secondCamera.length, 9);
});

test("image actions remain ordered and manual camera stays closable at capacity", () => {
  const state = getImageUploaderActionState({
    imageCount: 9,
    configuredMaxImages: 9,
    manualCameraVisible: true,
    automationEnabled: true,
    automationArmed: true,
    automationExpanded: true,
    cycleEnabled: true,
    cycleRunning: false,
  });

  assert.deepEqual(state.actions.map((action) => action.id), ["upload", "manual_photo", "automation", "cycle"]);
  assert.equal(state.actions[0].visible, false);
  assert.equal(state.actions[1].visible, true);
  assert.equal(state.actions[1].mode, "close");
  assert.equal(state.actions[2].disabled, false);
  assert.equal(state.actions[3].disabled, true);
});

test("Cycle running disables automation without silently collapsing it", () => {
  const state = getImageUploaderActionState({
    imageCount: 0,
    configuredMaxImages: 9,
    manualCameraVisible: false,
    automationEnabled: true,
    automationArmed: false,
    automationExpanded: true,
    cycleEnabled: true,
    cycleRunning: true,
  });
  assert.equal(state.actions[2].disabled, true);
  assert.equal(state.automationExpanded, true);
  assert.equal(state.actions[3].disabled, false);
});

test("folded automation unmounts presentation while the helper owns no capture resources", () => {
  assert.equal(shouldMountAutomationPresentation({
    enabled: true,
    expanded: false,
    actionButtonsVisible: true,
    panelAvailable: true,
  }), false);
  assert.equal(shouldMountAutomationPresentation({
    enabled: true,
    expanded: true,
    actionButtonsVisible: true,
    panelAvailable: true,
  }), true);
});

test("panel state locks controls and unmounts thumbnail presentation when collapsed", () => {
  assert.deepEqual(getVisionAutomationPanelState({
    armed: true,
    collapsed: true,
    cycleRunning: false,
    selectedSourceCount: 2,
    bundleCount: 3,
  }), {
    controlsLocked: true,
    canStart: false,
    cameraRefreshDisabled: true,
    showThumbnailMatrix: false,
  });
});

test("panel state blocks Trigger Start until page generation is ready", () => {
  assert.equal(getVisionAutomationPanelState({
    armed: false,
    collapsed: false,
    cycleRunning: false,
    selectedSourceCount: 1,
    bundleCount: 0,
    startDisabled: true,
  }).canStart, false);
});

test("status contract exposes accessible plus-button state with reduced-motion classes", () => {
  const capture = getVisionAutomationStatus("capturing", true);
  const waiting = getVisionAutomationStatus("waiting_for_model", true);
  const error = getVisionAutomationStatus("paused_source_error", true);
  assert.equal(capture?.tone, "blue");
  assert.equal(waiting?.tone, "gray");
  assert.equal(error?.tone, "amber");
  assert.match(capture!.className, /motion-safe:animate-spin/);
  assert.match(capture!.className, /motion-reduce:animate-none/);
  assert.match(error!.label, /视觉源/);
  assert.equal(getVisionAutomationStatus("idle", false), null);
});

test("source health presentation reports per-source state and scoped errors", () => {
  const sources: SourceDescriptor[] = [
    { sourceId: "desktop", kind: "display", stableAlias: "desktop", label: "Desktop" },
    { sourceId: "camera-a", kind: "camera", stableAlias: "camera_1", label: "Camera A" },
  ];
  const health = getSourceHealthPresentation(sources, [
    { sourceId: "desktop", status: "healthy" },
    { sourceId: "camera-a", status: "error", error: "camera disconnected" },
  ]);
  assert.deepEqual(health, [
    { sourceId: "desktop", label: "Desktop", status: "healthy", statusLabel: "正常", error: null },
    { sourceId: "camera-a", label: "Camera A", status: "error", statusLabel: "异常", error: "camera disconnected" },
  ]);
});

test("partial source health overlays every active source and keeps explicit failed sources", () => {
  const active: SourceDescriptor[] = [
    { sourceId: "desktop", kind: "display", stableAlias: "desktop", label: "Desktop" },
    { sourceId: "camera-a", kind: "camera", stableAlias: "camera_1", label: "Camera A" },
  ];
  const available: SourceDescriptor[] = [
    ...active,
    { sourceId: "camera-b", kind: "camera", stableAlias: "camera_2", label: "Camera B" },
  ];
  const merged = mergeSourceHealthPresentation(active, available, [
    { sourceId: "desktop", status: "healthy" },
    { sourceId: "camera-b", status: "error", error: "permission denied" },
  ]);

  assert.deepEqual(merged, [
    { sourceId: "desktop", label: "Desktop", status: "healthy", statusLabel: "正常", error: null },
    { sourceId: "camera-a", label: "Camera A", status: "waiting", statusLabel: "等待中", error: null },
    { sourceId: "camera-b", label: "Camera B", status: "error", statusLabel: "异常", error: "permission denied" },
  ]);
});

test("working image preview acquires upload URL and releases it exactly once", () => {
  const acquiredKinds: string[] = [];
  const releasedURLs: string[] = [];
  const resource: ImageResource = {
    uploadBlob: new Blob(["jpeg"], { type: "image/jpeg" }),
    thumbnailBlob: null,
    disposed: false,
    acquireObjectURL(kind) {
      acquiredKinds.push(kind);
      return "blob:preview";
    },
    releaseObjectURL(url) {
      releasedURLs.push(url);
      return true;
    },
    dispose() { this.disposed = true; },
  };
  const lease = createImagePreviewURLLease(resource);
  assert.equal(lease.url, "blob:preview");
  assert.deepEqual(acquiredKinds, ["upload"]);
  lease.release();
  lease.release();
  assert.deepEqual(releasedURLs, ["blob:preview"]);
  assert.equal(resource.disposed, false);
});
