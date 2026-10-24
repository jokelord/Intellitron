import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "../..");
const { createRenderer, installTask6TypeScriptHook } = require("../lib/vision-automation/react-component-renderer.cjs");

installTask6TypeScriptHook(frontendRoot);

const React = require("react");
const AgentStepDisplay = require("./AgentStepDisplay.tsx").default;
const steps = [{ id: "thought-1", step_type: "thought", content: "正在推理" }];

function render(defaultCollapsed: boolean) {
  return createRenderer(React.createElement(AgentStepDisplay, {
    steps,
    defaultCollapsed,
  }));
}

test("AgentStepDisplay starts collapsed during inference when requested and remains manually expandable", () => {
  const renderer = render(true);
  const toggle = renderer.findOne((node: any) => node.type === "button" && renderer.text(node).includes("思维链"));

  assert.equal(toggle.props["aria-expanded"], false);
  assert.equal(renderer.text(renderer.container).includes("正在推理"), false);

  renderer.click(toggle);
  assert.equal(toggle.props["aria-expanded"], true);
  assert.equal(renderer.text(renderer.container).includes("正在推理"), true);
  renderer.unmount();
});

test("AgentStepDisplay keeps its existing expanded default when collapsing is not requested", () => {
  const renderer = render(false);
  const toggle = renderer.findOne((node: any) => node.type === "button" && renderer.text(node).includes("思维链"));

  assert.equal(toggle.props["aria-expanded"], true);
  assert.equal(renderer.text(renderer.container).includes("正在推理"), true);
  renderer.unmount();
});

test('streaming thought cards render their newest line beyond the configured character limit', () => {
  const renderer = createRenderer(React.createElement(AgentStepDisplay, {
    steps: [{ id: 'thought-live', step_type: 'thought', partial: true, content: 'old line\nLATEST' }],
    bubbleCharacterLimit: 8,
  }));
  assert.equal(renderer.text(renderer.container).includes('LATEST'), true);
  renderer.unmount();
});

const AssistantReasoningSection = require('./AssistantReasoningSection.tsx').default;
test('streaming Chat reasoning exposes its latest line and keeps the user collapse preference', () => {
  const render = (collapsed: boolean) => React.createElement(AssistantReasoningSection, {
    messageId: 'chat-live', reasoning: 'old line\nLATEST', isStreaming: true,
    defaultExpanded: true, collapsed, bubbleCharacterLimit: 8, onToggle() {},
  });
  const renderer = createRenderer(render(false));
  assert.equal(renderer.text(renderer.container).includes('LATEST'), true);
  renderer.update(render(true));
  assert.equal(renderer.text(renderer.container).includes('LATEST'), false);
  renderer.unmount();
});

test('the latest observation exposes its bottom line during an active turn and completed turns retain collapse defaults', () => {
  const props = {
    steps: [{ id: 'observation', step_type: 'observation', content: 'old line\nLATEST' }],
    bubbleCharacterLimit: 8,
  };
  const renderer = createRenderer(React.createElement(AgentStepDisplay, { ...props, turnComplete: false }));
  assert.equal(renderer.text(renderer.container).includes('LATEST'), true);
  renderer.update(React.createElement(AgentStepDisplay, { ...props, turnComplete: true }));
  assert.equal(renderer.text(renderer.container).includes('LATEST'), false);
  renderer.unmount();
});
