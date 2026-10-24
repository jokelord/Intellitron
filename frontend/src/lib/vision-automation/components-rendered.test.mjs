import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { createRenderer, installTask6TypeScriptHook } from "./react-component-renderer.cjs";

const require = createRequire(import.meta.url);
const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "../../..");
installTask6TypeScriptHook(frontendRoot);

const React = require("react");
const ImageUploader = require("../../components/ImageUploader.tsx").default;
const MessageTemplateMenu = require("../../components/MessageTemplateMenu.tsx").default;
const CameraCapture = require("../../components/CameraCapture.tsx").default;
const VisionAutomationPanel = require("../../components/VisionAutomationPanel.tsx").default;
const VisionAutomationStatusRing = require("../../components/VisionAutomationStatusRing.tsx").default;
const VisionAutomationThumbnailMatrix = require("../../components/VisionAutomationThumbnailMatrix.tsx").default;
const ManagerParallelDispatchCard = require("../../components/ManagerParallelDispatchCard.tsx").default;
const pageIntegration = require("./page-integration.ts");

const desktop = {
  sourceId: "desktop-source",
  kind: "display",
  stableAlias: "desktop",
  label: "Desktop",
};
const camera = {
  sourceId: "camera-source",
  kind: "camera",
  stableAlias: "camera_1",
  label: "Camera A",
};

const config = {
  enabled: true,
  intervalSeconds: 1,
  maxFramesPerSource: 16,
  thumbnailSizePx: 32,
  captureDuringInference: false,
  lockFirstBundle: false,
  tickTimeoutSeconds: 10,
  consecutiveTickFailureLimit: 3,
  maxEncodedPayloadBytes: 128 * 1024 * 1024,
  initialJpegQuality: 85,
  minimumJpegQuality: 40,
  minimumImageDimensionPx: 256,
  maxImageDimensionPx: 1280,
  base64EncodeConcurrency: 4,
  maxRequestBodyBytes: 136 * 1024 * 1024,
  requestStatusTtlSeconds: 600,
};

function imageResource(label) {
  const acquired = [];
  const released = [];
  return {
    acquired,
    released,
    resource: {
      uploadBlob: new Blob([`${label}-upload`], { type: "image/jpeg" }),
      thumbnailBlob: new Blob([`${label}-thumbnail`], { type: "image/jpeg" }),
      disposed: false,
      acquireObjectURL(kind) {
        const url = `blob:${label}-${kind}-${acquired.length}`;
        acquired.push({ kind, url });
        return url;
      },
      releaseObjectURL(url) {
        released.push(url);
        return true;
      },
      dispose() { this.disposed = true; },
    },
  };
}

function panelProps(overrides = {}) {
  return {
    phase: "capturing",
    armed: false,
    collapsed: false,
    cycleRunning: false,
    config,
    availableSources: [desktop, camera],
    selectedSourceIds: new Set([desktop.sourceId, camera.sourceId]),
    activeSources: [desktop, camera],
    bundles: [],
    sourceHealth: [{ sourceId: desktop.sourceId, status: "healthy" }],
    stats: {
      attemptedTicks: 1,
      successfulTicks: 1,
      failedTicks: 0,
      imageCount: 0,
      estimatedEncodedBytes: 0,
    },
    onCollapsedChange: () => undefined,
    onSourceSelectionChange: () => undefined,
    onConfigChange: () => undefined,
    onStart: () => undefined,
    onAbort: () => undefined,
    onRetrySources: () => undefined,
    onRetryFailedSource: () => undefined,
    onRetryStatus: () => undefined,
    onRetrySubmit: () => undefined,
    canRetryStatus: false,
    canRetrySubmit: false,
    onRefreshCameras: () => undefined,
    ...overrides,
  };
}

test("rendered panel executes Refresh and keeps missing active health visible as waiting", () => {
  let refreshes = 0;
  const renderer = createRenderer(React.createElement(VisionAutomationPanel, panelProps({
    onRefreshCameras: () => { refreshes += 1; },
  })));
  renderer.flush();

  const refresh = renderer.findOne((node) => node.type === "button" && renderer.text(node).includes("刷新摄像头"));
  renderer.click(refresh);
  assert.equal(refreshes, 1);
  assert.ok(renderer.findOne((node) => renderer.text(node) === "Camera A"));
  assert.ok(renderer.findOne((node) => renderer.text(node) === "等待中"));
  renderer.unmount();
});

test("rendered panel exposes and updates the first-bundle lock before arming", () => {
  const patches = [];
  const renderer = createRenderer(React.createElement(VisionAutomationPanel, panelProps({
    onConfigChange: (patch) => patches.push(patch),
  })));
  renderer.flush();

  const lock = renderer.findOne((node) => node.type === "input" && node.props.checked === false
    && renderer.text(node.parent) === "锁定首帧");
  renderer.act(() => lock.props.onChange({ target: { checked: true } }));
  assert.deepEqual(patches, [{ lockFirstBundle: true }]);
  renderer.update(React.createElement(VisionAutomationPanel, panelProps({ armed: true })));
  renderer.flush();
  const lockedWhileArmed = renderer.findOne((node) => node.type === "input"
    && renderer.text(node.parent) === "锁定首帧");
  assert.equal(lockedWhileArmed.props.disabled, true);
  renderer.unmount();
});

test("rendered thumbnail preview acquires and releases its upload URL on open and close", () => {
  const image = imageResource("preview");
  const bundle = {
    tickIndex: 0,
    timestampMs: 1_000,
    relativeTimeSeconds: 0,
    frames: [{ source: desktop, resource: image.resource }],
  };
  const renderer = createRenderer(React.createElement(VisionAutomationThumbnailMatrix, {
    sources: [desktop],
    bundles: [bundle],
  }));
  renderer.flush();
  assert.deepEqual(image.acquired.map((item) => item.kind), ["thumbnail"]);

  const thumbnail = renderer.findOne((node) => node.type === "img" && node.props.alt === "desktop 0.00s");
  renderer.click(thumbnail.parent);
  renderer.flush();
  assert.deepEqual(image.acquired.map((item) => item.kind), ["thumbnail", "upload"]);
  assert.ok(renderer.findOne((node) => node.props.role === "dialog"));

  const close = renderer.findOne((node) => node.type === "button" && renderer.text(node) === "关闭");
  renderer.click(close);
  renderer.flush();
  assert.equal(renderer.findAll((node) => node.props.role === "dialog").length, 0);
  assert.deepEqual(image.released, [image.acquired[1].url]);
  renderer.unmount();
  assert.deepEqual(image.released, [image.acquired[1].url, image.acquired[0].url]);
});

test("collapsing the rendered panel unmounts the matrix and releases thumbnail URLs", () => {
  const image = imageResource("fold");
  const bundle = {
    tickIndex: 0,
    timestampMs: 1_000,
    relativeTimeSeconds: 0,
    frames: [{ source: desktop, resource: image.resource }],
  };
  const renderer = createRenderer(React.createElement(VisionAutomationPanel, panelProps({
    armed: true,
    bundles: [bundle],
  })));
  renderer.flush();
  assert.deepEqual(image.acquired.map((item) => item.kind), ["thumbnail"]);

  renderer.update(React.createElement(VisionAutomationPanel, panelProps({
    armed: true,
    collapsed: true,
    bundles: [bundle],
  })));
  renderer.flush();
  assert.deepEqual(image.released, [image.acquired[0].url]);
  assert.equal(renderer.findAll((node) => node.type === "img" && node.props.alt === "desktop 0.00s").length, 0);
  renderer.unmount();
});

test("status ring executes as an independent persistent plus-button contract", () => {
  const renderer = createRenderer(React.createElement(VisionAutomationStatusRing, {
    phase: "waiting_for_model",
    armed: true,
    className: "plus-button-status",
  }));
  renderer.flush();
  const status = renderer.findOne((node) => node.props.role === "status");
  assert.match(status.props.className, /plus-button-status/);
  assert.match(status.props.className, /motion-reduce:animate-none/);
  assert.match(status.props["aria-label"], /等待模型/);

  renderer.update(React.createElement(VisionAutomationStatusRing, {
    phase: "idle",
    armed: false,
    className: "plus-button-status",
  }));
  renderer.flush();
  assert.equal(renderer.findAll((node) => node.props.role === "status").length, 0);
  renderer.unmount();
});

test("panel executes Retry Status and Retry Submit only when recovery actions are available", () => {
  const actions = [];
  const renderer = createRenderer(React.createElement(VisionAutomationPanel, panelProps({
    armed: true,
    canRetryStatus: true,
    canRetrySubmit: true,
    onRetryStatus: () => { actions.push("status"); },
    onRetrySubmit: () => { actions.push("submit"); },
  })));
  renderer.flush();

  renderer.click(renderer.findOne((node) => node.type === "button" && renderer.text(node).includes("Retry Status")));
  renderer.click(renderer.findOne((node) => node.type === "button" && renderer.text(node).includes("Retry Submit")));
  assert.deepEqual(actions, ["status", "submit"]);
  renderer.unmount();
});

test("outer ImageUploader automation folding mounts and cleans up presentation effects", () => {
  let mounts = 0;
  let cleanups = 0;
  function AutomationPresentation() {
    React.useEffect(() => {
      mounts += 1;
      return () => { cleanups += 1; };
    }, []);
    return React.createElement("div", { "data-automation-presentation": true }, "Automation Panel");
  }

  const renderer = createRenderer(React.createElement(ImageUploader, {
    images: [],
    onImagesChange: () => undefined,
    visionAutomationEnabled: true,
    automationPanel: React.createElement(AutomationPresentation),
  }));
  renderer.flush();
  assert.equal(mounts, 0);

  const automationButton = renderer.findOne((node) => (
    node.type === "button" && renderer.text(node) === "视觉自动化"
  ));
  renderer.click(automationButton);
  assert.equal(mounts, 1);
  assert.equal(renderer.findAll((node) => node.props["data-automation-presentation"] === true).length, 1);

  renderer.click(automationButton);
  assert.equal(cleanups, 1);
  assert.equal(renderer.findAll((node) => node.props["data-automation-presentation"] === true).length, 0);
  renderer.unmount();
});

test("ImageUploader renders the message-template slot inside a five-column visible action grid", () => {
  const slot = React.createElement("div", { "data-testid": "message-template-slot-content" }, "Templates");
  const renderer = createRenderer(React.createElement(ImageUploader, {
    images: ["data:image/png;base64,pending"],
    onImagesChange: () => undefined,
    showActionButtons: true,
    messageTemplateMenu: slot,
  }));

  const actionGrid = renderer.findOne((node) => node.props["data-testid"] === "vision-input-action-grid");
  const renderedSlot = renderer.findOne((node) => node.props["data-testid"] === "message-template-slot-content");
  assert.match(String(actionGrid.props.className ?? ""), /(?:^|\s)sm:grid-cols-5(?:\s|$)/);
  assert.equal(renderedSlot.parent, actionGrid);

  renderer.update(React.createElement(ImageUploader, {
    images: ["data:image/png;base64,pending"],
    onImagesChange: () => undefined,
    showActionButtons: false,
    messageTemplateMenu: slot,
  }));
  assert.equal(
    renderer.findAll((node) => node.props["data-testid"] === "message-template-slot-content").length,
    0,
  );
  renderer.unmount();
});

test("Vision uploader remount keeps immediate-send mode in the page-level controlled owner", () => {
  const inserts = [];
  const sends = [];
  function VisionMessageTemplateHarness() {
    const [uploaderGeneration, setUploaderGeneration] = React.useState(0);
    const [immediateSend, setImmediateSend] = React.useState(false);
    return React.createElement(
      React.Fragment,
      null,
      React.createElement("button", {
        type: "button",
        "data-testid": "remount-uploader",
        onClick: () => setUploaderGeneration((current) => current + 1),
      }, "Remount"),
      React.createElement(ImageUploader, {
        key: `uploader-${uploaderGeneration}`,
        images: [],
        onImagesChange: () => undefined,
        showActionButtons: true,
        messageTemplateMenu: React.createElement(MessageTemplateMenu, {
          actionBarVisible: true,
          templates: ["persisted mode"],
          yamlDefaults: ["persisted mode"],
          onTemplatesChange: () => undefined,
          onRestoreDefaults: () => undefined,
          onInsertTemplate: (value) => inserts.push(value),
          onSendTemplate: (value) => sends.push(value),
          immediateSend,
          onImmediateSendChange: setImmediateSend,
        }),
      }),
    );
  }

  const renderer = createRenderer(React.createElement(VisionMessageTemplateHarness));
  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-trigger"));
  renderer.click(renderer.findOne((node) => node.props.role === "switch"));
  assert.equal(renderer.findOne((node) => node.props.role === "switch").props["aria-checked"], true);

  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "remount-uploader"));
  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-trigger"));
  assert.equal(renderer.findOne((node) => node.props.role === "switch").props["aria-checked"], true);
  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-item-0"));
  assert.deepEqual(inserts, []);
  assert.deepEqual(sends, ["persisted mode"]);
  renderer.unmount();
});

test("outer ImageUploader emits concurrent functional camera commits that remain capped at nine", () => {
  const initialImages = Array.from({ length: 8 }, (_, index) => `manual-${index}`);
  const updates = [];
  const renderer = createRenderer(React.createElement(ImageUploader, {
    images: initialImages,
    maxImages: 9,
    onImagesChange: (update) => updates.push(update),
  }));
  renderer.flush();
  const manualButton = renderer.findOne((node) => node.type === "button" && node.props["aria-label"] === "手动拍照");
  renderer.click(manualButton);
  const cameraCapture = renderer.findComponent((fiber) => fiber.type === CameraCapture);

  renderer.act(() => {
    cameraCapture.memoizedProps.onCapture("camera-first");
    cameraCapture.memoizedProps.onCapture("camera-second");
  });
  assert.equal(updates.length, 2);
  assert.ok(updates.every((update) => typeof update === "function"));

  const committed = updates.reduce((images, update) => update(images), initialImages);
  assert.deepEqual(committed, [...initialImages, "camera-first"]);
  assert.equal(committed.length, 9);
  renderer.unmount();
});

test("manager child action is a protected native new-tab link that synchronously arms its owner", () => {
  let activations = 0;
  const renderer = createRenderer(React.createElement(ManagerParallelDispatchCard, {
    dispatchId: "dispatch-1",
    children: [{
      dispatchId: "dispatch-1",
      callId: "call-1",
      childRunId: "run 1",
      childName: "Research child",
      status: "running",
      href: "/agent/child/run%201",
    }],
    onChildLinkActivate: () => { activations += 1; },
  }));
  renderer.flush();

  const childLink = renderer.findOne((node) => node.type === "a" && renderer.text(node) === "Open child");
  assert.equal(childLink.props.href, "/agent/child/run%201");
  assert.equal(childLink.props.target, "_blank");
  assert.equal(childLink.props.rel, "noopener noreferrer");
  renderer.click(childLink);
  assert.equal(activations, 1);
  renderer.unmount();
});
