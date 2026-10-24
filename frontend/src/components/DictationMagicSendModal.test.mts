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
const { DictationMagicSendModal } = require("./DictationMagicSendModal.tsx");
const { VoiceListeningIndicator } = require("./VoiceListeningIndicator.tsx");

const initial = { rules: [{ text: "", enabled: false }, { text: "Go", enabled: true }], pollIntervalMs: 500 };
function element(
  open: boolean,
  onConfirm: (value: unknown) => void,
  onCancel: () => void,
  settings = initial,
) {
  return React.createElement(DictationMagicSendModal, { open, settings, onConfirm, onCancel });
}

test("DictationMagicSendModal renders and edits its draft without committing Cancel", () => {
  const confirmed: unknown[] = [];
  let cancelled = 0;
  const renderer = createRenderer(element(false, (value) => confirmed.push(value), () => { cancelled += 1; }));
  assert.equal(renderer.findAll((node: any) => node.props?.role === "dialog").length, 0);
  renderer.update(element(true, (value) => confirmed.push(value), () => { cancelled += 1; }));
  const checkbox = renderer.findOne((node: any) => node.type === "input" && node.props["aria-label"] === "Enable command 1");
  assert.equal(checkbox.props.disabled, true);
  const command = renderer.findOne((node: any) => node.type === "input" && node.props["aria-label"] === "Command 1");
  renderer.act(() => command.props.onChange({ currentTarget: { value: "Launch " } }));
  assert.equal(checkbox.props.disabled, false);
  const add = renderer.findOne((node: any) => node.type === "button" && renderer.text(node).includes("Add command"));
  renderer.click(add);
  assert.equal(renderer.findAll((node: any) => node.type === "input" && /^Command /.test(node.props["aria-label"] ?? "")).length, 3);
  const cancel = renderer.findOne((node: any) => node.type === "button" && renderer.text(node) === "Cancel");
  renderer.click(cancel);
  assert.equal(cancelled, 1);
  assert.deepEqual(confirmed, []);
  renderer.update(element(false, (value) => confirmed.push(value), () => { cancelled += 1; }));
  renderer.update(element(true, (value) => confirmed.push(value), () => { cancelled += 1; }));
  assert.equal(renderer.findOne((node: any) => node.type === "input" && node.props["aria-label"] === "Command 1").props.value, "");
  assert.ok(renderer.findOne((node: any) => node.props?.["aria-label"] === "Dictation commands").props.className.includes("overflow-y-auto"));
  renderer.unmount();
});

test("DictationMagicSendModal filters blank rows and normalizes interval only on Confirm", () => {
  const confirmed: any[] = [];
  const renderer = createRenderer(element(true, (value) => confirmed.push(value), () => {}));
  const interval = renderer.findOne((node: any) => node.type === "input" && node.props["aria-label"] === "Poll interval (ms)");
  renderer.act(() => interval.props.onChange({ currentTarget: { value: "" } }));
  assert.equal(interval.props.value, "", "draft interval must allow temporary blank editing");
  renderer.act(() => interval.props.onChange({ currentTarget: { value: "99" } }));
  const confirm = renderer.findOne((node: any) => node.type === "button" && renderer.text(node) === "Confirm");
  renderer.click(confirm);
  assert.deepEqual(confirmed, [{ rules: [{ text: "Go", enabled: true }], pollIntervalMs: 100 }]);
  renderer.unmount();
});

test("DictationMagicSendModal preserves an open draft until it is closed, then reloads new settings", () => {
  const first = { rules: [{ text: "First", enabled: true }, { text: "Second", enabled: true }], pollIntervalMs: 500 };
  const newer = { rules: [{ text: "Server replacement", enabled: true }], pollIntervalMs: 700 };
  const renderer = createRenderer(element(false, () => {}, () => {}, first));
  renderer.update(element(true, () => {}, () => {}, first));
  const second = renderer.findOne((node: any) => node.type === "input" && node.props["aria-label"] === "Command 2");
  renderer.act(() => second.props.onChange({ currentTarget: { value: "Unsaved edit" } }));
  renderer.update(element(true, () => {}, () => {}, newer));
  assert.equal(
    renderer.findOne((node: any) => node.type === "input" && node.props["aria-label"] === "Command 2").props.value,
    "Unsaved edit",
  );
  renderer.update(element(false, () => {}, () => {}, newer));
  renderer.update(element(true, () => {}, () => {}, newer));
  assert.equal(
    renderer.findOne((node: any) => node.type === "input" && node.props["aria-label"] === "Command 1").props.value,
    "Server replacement",
  );
  renderer.unmount();
});

test("DictationMagicSendModal deletes rows and confirms the HTML5-dragged order", () => {
  const confirmed: any[] = [];
  const settings = { rules: [{ text: "Alpha", enabled: true }, { text: "Beta", enabled: true }, { text: "Gamma", enabled: true }], pollIntervalMs: 500 };
  const renderer = createRenderer(element(true, (value) => confirmed.push(value), () => {}, settings));
  renderer.click(renderer.findOne((node: any) => node.props?.["aria-label"] === "Delete command 2"));
  assert.equal(renderer.findAll((node: any) => node.type === "input" && /^Command /.test(node.props["aria-label"] ?? "")).length, 2);
  const transfer = {
    data: new Map<string, string>(),
    setData(type: string, value: string) { this.data.set(type, value); },
    getData(type: string) { return this.data.get(type) ?? ""; },
  };
  const gammaHandle = renderer.findOne((node: any) => node.props?.["aria-label"] === "Reorder command 2");
  const alphaHandle = renderer.findOne((node: any) => node.props?.["aria-label"] === "Reorder command 1");
  renderer.act(() => gammaHandle.props.onDragStart({ dataTransfer: transfer }));
  renderer.act(() => alphaHandle.props.onDrop({ dataTransfer: transfer, preventDefault() {} }));
  renderer.click(renderer.findOne((node: any) => node.type === "button" && renderer.text(node) === "Confirm"));
  assert.deepEqual(confirmed, [{ rules: [{ text: "Gamma", enabled: true }, { text: "Alpha", enabled: true }], pollIntervalMs: 500 }]);
  renderer.unmount();
});

test("DictationMagicSendModal supports bounded keyboard reordering and edits an added row after reorder", () => {
  const confirmed: any[] = [];
  const settings = { rules: [{ text: "Alpha", enabled: true }, { text: "Beta", enabled: true }], pollIntervalMs: 500 };
  const renderer = createRenderer(element(true, (value) => confirmed.push(value), () => {}, settings));
  const keyEvent = (key: string) => ({ key, preventDefault() {} });
  renderer.act(() => renderer.findOne((node: any) => node.props?.["aria-label"] === "Reorder command 1").props.onKeyDown(keyEvent("ArrowUp")));
  assert.equal(renderer.findOne((node: any) => node.type === "input" && node.props["aria-label"] === "Command 1").props.value, "Alpha");
  renderer.click(renderer.findOne((node: any) => node.type === "button" && renderer.text(node).includes("Add command")));
  const added = renderer.findOne((node: any) => node.type === "input" && node.props["aria-label"] === "Command 3");
  renderer.act(() => added.props.onChange({ currentTarget: { value: "Gamma" } }));
  renderer.act(() => renderer.findOne((node: any) => node.props?.["aria-label"] === "Reorder command 3").props.onKeyDown(keyEvent("ArrowUp")));
  const movedAdded = renderer.findOne((node: any) => node.type === "input" && node.props["aria-label"] === "Command 2");
  renderer.act(() => movedAdded.props.onChange({ currentTarget: { value: "Gamma edited" } }));
  renderer.act(() => renderer.findOne((node: any) => node.props?.["aria-label"] === "Reorder command 1").props.onKeyDown(keyEvent("ArrowUp")));
  assert.equal(renderer.findOne((node: any) => node.type === "input" && node.props["aria-label"] === "Command 1").props.value, "Alpha");
  renderer.click(renderer.findOne((node: any) => node.type === "button" && renderer.text(node) === "Confirm"));
  assert.deepEqual(confirmed, [{ rules: [{ text: "Alpha", enabled: true }, { text: "Gamma edited", enabled: false }, { text: "Beta", enabled: true }], pollIntervalMs: 500 }]);
  renderer.unmount();
});

test("DictationMagicSendModal ignores numeric text dragged from outside the command list", () => {
  const confirmed: any[] = [];
  const settings = { rules: [{ text: "Alpha", enabled: true }, { text: "Beta", enabled: true }], pollIntervalMs: 500 };
  const renderer = createRenderer(element(true, (value) => confirmed.push(value), () => {}, settings));
  const betaHandle = renderer.findOne((node: any) => node.props?.["aria-label"] === "Reorder command 2");
  renderer.act(() => betaHandle.props.onDrop({
    dataTransfer: { getData: () => "0" },
    preventDefault() {},
  }));
  renderer.click(renderer.findOne((node: any) => node.type === "button" && renderer.text(node) === "Confirm"));
  assert.deepEqual(confirmed, [{ rules: [{ text: "Alpha", enabled: true }, { text: "Beta", enabled: true }], pollIntervalMs: 500 }]);
  renderer.unmount();
});

test("DictationMagicSendModal exposes dark controls and a padded scrolling command rail", () => {
  const renderer = createRenderer(element(true, () => {}, () => {}));
  const dialog = renderer.findOne((node: any) => node.props?.role === "dialog");
  assert.match(dialog.props.className, /dark:bg-/);
  assert.match(dialog.props.className, /dark:text-/);
  const commands = renderer.findOne((node: any) => node.props?.["aria-label"] === "Dictation commands");
  assert.match(commands.props.className, /pr-\d+/);
  const close = renderer.findOne((node: any) => node.props?.["aria-label"] === "Close dictation magic send settings");
  assert.match(close.props.className, /dark:/);
  const confirm = renderer.findOne((node: any) => node.type === "button" && renderer.text(node) === "Confirm");
  assert.match(confirm.props.className, /bg-nvidia-700/);
  assert.match(confirm.props.className, /hover:bg-nvidia-800/);
  assert.doesNotMatch(confirm.props.className, /bg-nvidia-(?:500|600)/);
  renderer.unmount();
});

test("VoiceListeningIndicator keeps the magic button distinct in dark mode", () => {
  const renderer = createRenderer(React.createElement(VoiceListeningIndicator, {
    state: { phase: "listening", waveform: [], levelDbfs: -80 },
    inputGain: null,
    onMagicSendSettings: () => {},
  }));
  const button = renderer.findOne(
    (node: any) => node.type === "button" && node.props?.["aria-label"] === "Dictation magic send settings",
  );
  assert.match(button.props.className, /border-nvidia-/);
  assert.match(button.props.className, /dark:bg-gray-950/);
  assert.match(button.props.className, /dark:text-nvidia-/);
  assert.match(button.props.className, /dark:hover:bg-nvidia-900/);
  renderer.unmount();
});
