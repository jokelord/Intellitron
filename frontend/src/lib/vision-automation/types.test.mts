import assert from "node:assert/strict";
import { test } from "node:test";

import type {
  AutomationBinding,
  SourceDescriptor,
  VisionAutomationPhase,
} from "./types.ts";

test("frontend contracts expose the exact lifecycle, binding, and source shapes", () => {
  const phases: VisionAutomationPhase[] = [
    "idle",
    "starting",
    "capturing",
    "freezing",
    "waiting_for_model",
    "capturing_during_model",
    "waiting_for_sources",
    "paused_source_error",
    "retryable_submit_error",
    "aborting",
  ];
  const binding: AutomationBinding = {
    pageInstanceId: "page-1",
    pageGeneration: 2,
    mode: "agent",
    contextId: "agent-1",
    automationTaskId: "task-1",
  };
  const sources: SourceDescriptor[] = [
    {
      sourceId: "display-1",
      kind: "display",
      stableAlias: "desktop",
      label: "Shared screen",
    },
    {
      sourceId: "camera-source-1",
      kind: "camera",
      stableAlias: "camera_1",
      label: "Browser camera label",
      deviceId: "private-device-id",
    },
  ];

  assert.equal(phases.length, 10);
  assert.equal(binding.mode, "agent");
  assert.deepEqual(sources.map((source) => source.stableAlias), ["desktop", "camera_1"]);
});
