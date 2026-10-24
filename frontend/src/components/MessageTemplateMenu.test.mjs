import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  createRenderer,
  installTask6TypeScriptHook,
} from "../lib/vision-automation/react-component-renderer.cjs";

const require = createRequire(import.meta.url);
const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "../..");
installTask6TypeScriptHook(frontendRoot);
const React = require("react");
const MessageTemplateMenu = require("./MessageTemplateMenu.tsx").default;

function props(overrides = {}) {
  return {
    actionBarVisible: true,
    templates: ["default template"],
    yamlDefaults: ["default template"],
    onTemplatesChange: () => undefined,
    onRestoreDefaults: () => undefined,
    onInsertTemplate: () => undefined,
    onSendTemplate: () => undefined,
    immediateSend: false,
    onImmediateSendChange: () => undefined,
    sendDisabled: false,
    ...overrides,
  };
}

function renderMenu(overrides = {}) {
  return createRenderer(React.createElement(MessageTemplateMenu, props(overrides)));
}

function byTestId(renderer, testId) {
  return renderer.findOne((node) => node.props["data-testid"] === testId);
}

function StatefulMenu({ initialTemplates = ["one", "two"], onChange, ...overrides }) {
  const [templates, setTemplates] = React.useState(initialTemplates);
  return React.createElement(MessageTemplateMenu, props({
    templates,
    onTemplatesChange(next) {
      setTemplates(next);
      onChange?.(next);
    },
    ...overrides,
  }));
}

function ControlledModeMenu({ initialImmediateSend = false, onModeChange, ...overrides }) {
  const [immediateSend, setImmediateSend] = React.useState(initialImmediateSend);
  return React.createElement(MessageTemplateMenu, props({
    immediateSend,
    onImmediateSendChange(next) {
      setImmediateSend(next);
      onModeChange?.(next);
    },
    ...overrides,
  }));
}

test("panel omits the action heading and controlled false selects insertion", () => {
  const inserts = [];
  const sends = [];
  const renderer = renderMenu({
    templates: ["Hello", "Long\nmessage"],
    onInsertTemplate: (value) => inserts.push(value),
    onSendTemplate: (value) => sends.push(value),
  });

  renderer.click(byTestId(renderer, "message-template-trigger"));
  const panel = byTestId(renderer, "message-template-panel");
  assert.doesNotMatch(renderer.text(panel), /选择模板后的操作/);
  const switches = renderer.findAll((node) => node.props.role === "switch");
  assert.equal(switches.length, 1);
  assert.equal(switches[0].props["aria-checked"], false);
  renderer.click(byTestId(renderer, "message-template-item-0"));

  assert.deepEqual(inserts, ["Hello"]);
  assert.deepEqual(sends, []);
  renderer.unmount();
});

test("toggling immediate send on and off switches between send and insert behavior", () => {
  const inserts = [];
  const sent = [];
  const modeChanges = [];
  const renderer = createRenderer(React.createElement(ControlledModeMenu, {
    templates: ["Send me"],
    onInsertTemplate: (value) => inserts.push(value),
    onSendTemplate: (value) => sent.push(value),
    onModeChange: (value) => modeChanges.push(value),
  }));
  renderer.click(byTestId(renderer, "message-template-trigger"));

  renderer.click(renderer.findOne((node) => node.props.role === "switch"));
  assert.equal(renderer.findOne((node) => node.props.role === "switch").props["aria-checked"], true);
  renderer.click(byTestId(renderer, "message-template-item-0"));
  assert.deepEqual(sent, ["Send me"]);
  assert.deepEqual(inserts, []);

  renderer.click(renderer.findOne((node) => node.props.role === "switch"));
  assert.equal(renderer.findOne((node) => node.props.role === "switch").props["aria-checked"], false);
  renderer.click(byTestId(renderer, "message-template-item-0"));
  assert.deepEqual(inserts, ["Send me"]);
  assert.deepEqual(sent, ["Send me"]);
  assert.deepEqual(modeChanges, [true, false]);
  renderer.unmount();
});

test("controlled true sends immediately without inserting", () => {
  const inserts = [];
  const sends = [];
  const renderer = renderMenu({
    templates: ["controlled send"],
    immediateSend: true,
    onInsertTemplate: (value) => inserts.push(value),
    onSendTemplate: (value) => sends.push(value),
  });
  renderer.click(byTestId(renderer, "message-template-trigger"));
  assert.equal(renderer.findOne((node) => node.props.role === "switch").props["aria-checked"], true);
  renderer.click(byTestId(renderer, "message-template-item-0"));
  assert.deepEqual(inserts, []);
  assert.deepEqual(sends, ["controlled send"]);
  renderer.unmount();
});

test("sendDisabled blocks immediate send without blocking insert mode", () => {
  const inserts = [];
  const sends = [];
  const renderer = createRenderer(React.createElement(ControlledModeMenu, {
    templates: ["still editable"],
    sendDisabled: true,
    onInsertTemplate: (value) => inserts.push(value),
    onSendTemplate: (value) => sends.push(value),
  }));
  renderer.click(byTestId(renderer, "message-template-trigger"));
  assert.equal(byTestId(renderer, "message-template-item-0").props.disabled, false);
  renderer.click(byTestId(renderer, "message-template-item-0"));
  assert.deepEqual(inserts, ["still editable"]);

  renderer.click(renderer.findOne((node) => node.props.role === "switch"));
  assert.equal(byTestId(renderer, "message-template-item-0").props.disabled, true);
  assert.deepEqual(sends, []);
  renderer.unmount();
});

test("add, edit, and delete emit complete lists while preserving exact non-blank text", () => {
  const changes = [];
  const renderer = createRenderer(React.createElement(StatefulMenu, {
    onChange: (next) => changes.push(next),
  }));
  renderer.click(byTestId(renderer, "message-template-trigger"));

  renderer.click(byTestId(renderer, "message-template-add"));
  const addEditor = byTestId(renderer, "message-template-editor-new");
  renderer.act(() => addEditor.props.onChange({ target: { value: " three\nline " } }));
  renderer.click(byTestId(renderer, "message-template-save-new"));

  renderer.click(byTestId(renderer, "message-template-edit-0"));
  const editEditor = byTestId(renderer, "message-template-editor-0");
  renderer.act(() => editEditor.props.onChange({ target: { value: " ONE " } }));
  renderer.click(byTestId(renderer, "message-template-save-0"));

  const previousConfirm = globalThis.confirm;
  try {
    globalThis.confirm = () => true;
    renderer.click(byTestId(renderer, "message-template-delete-1"));
  } finally {
    globalThis.confirm = previousConfirm;
  }

  assert.deepEqual(changes, [
    ["one", "two", " three\nline "],
    [" ONE ", "two", " three\nline "],
    [" ONE ", " three\nline "],
  ]);
  renderer.unmount();
});

test("blank saves are disabled and cancelling leaves templates unchanged", () => {
  const changes = [];
  const renderer = createRenderer(React.createElement(StatefulMenu, {
    onChange: (next) => changes.push(next),
  }));
  renderer.click(byTestId(renderer, "message-template-trigger"));
  renderer.click(byTestId(renderer, "message-template-add"));
  renderer.act(() => byTestId(renderer, "message-template-editor-new").props.onChange({
    target: { value: "   \n" },
  }));
  assert.equal(byTestId(renderer, "message-template-save-new").props.disabled, true);
  renderer.click(byTestId(renderer, "message-template-cancel-new"));

  renderer.click(byTestId(renderer, "message-template-edit-0"));
  renderer.act(() => byTestId(renderer, "message-template-editor-0").props.onChange({
    target: { value: "discard this" },
  }));
  renderer.click(byTestId(renderer, "message-template-cancel-0"));
  assert.deepEqual(changes, []);
  assert.equal(renderer.text(byTestId(renderer, "message-template-item-0")), "one");
  renderer.unmount();
});

test("edit and delete controls do not select the template", () => {
  const inserts = [];
  const changes = [];
  const renderer = renderMenu({
    templates: ["one"],
    onInsertTemplate: (value) => inserts.push(value),
    onTemplatesChange: (next) => changes.push(next),
  });
  renderer.click(byTestId(renderer, "message-template-trigger"));
  renderer.click(byTestId(renderer, "message-template-edit-0"));
  assert.deepEqual(inserts, []);
  renderer.click(byTestId(renderer, "message-template-cancel-0"));

  const previousConfirm = globalThis.confirm;
  try {
    globalThis.confirm = () => false;
    renderer.click(byTestId(renderer, "message-template-delete-0"));
  } finally {
    globalThis.confirm = previousConfirm;
  }
  assert.deepEqual(inserts, []);
  assert.deepEqual(changes, []);
  renderer.unmount();
});

test("restore defaults requires confirmation and delegates state restoration", () => {
  let restores = 0;
  const renderer = renderMenu({ onRestoreDefaults: () => { restores += 1; } });
  renderer.click(byTestId(renderer, "message-template-trigger"));
  const previousConfirm = globalThis.confirm;
  try {
    globalThis.confirm = () => false;
    renderer.click(byTestId(renderer, "message-template-restore"));
    assert.equal(restores, 0);
    globalThis.confirm = () => true;
    renderer.click(byTestId(renderer, "message-template-restore"));
    assert.equal(restores, 1);
  } finally {
    globalThis.confirm = previousConfirm;
  }
  renderer.unmount();
});

test("closing the parent action bar closes the panel and discards an unsaved draft", () => {
  const changes = [];
  const visibleProps = props({
    templates: ["original"],
    onTemplatesChange: (next) => changes.push(next),
  });
  const renderer = createRenderer(React.createElement(MessageTemplateMenu, visibleProps));
  renderer.click(byTestId(renderer, "message-template-trigger"));
  renderer.click(byTestId(renderer, "message-template-edit-0"));
  renderer.act(() => byTestId(renderer, "message-template-editor-0").props.onChange({
    target: { value: "unsaved" },
  }));

  renderer.update(React.createElement(MessageTemplateMenu, {
    ...visibleProps,
    actionBarVisible: false,
  }));
  assert.equal(renderer.findAll((node) => node.props["data-testid"] === "message-template-panel").length, 0);
  renderer.update(React.createElement(MessageTemplateMenu, visibleProps));
  renderer.click(byTestId(renderer, "message-template-trigger"));
  assert.equal(renderer.findAll((node) => node.type === "textarea").length, 0);
  assert.deepEqual(changes, []);
  renderer.unmount();
});

test("long template labels stay visually truncated and expose exact text only through aria-label", () => {
  const template = "A very long template with_a_single_unbroken_word_that_must_wrap\nand a second line";
  const renderer = renderMenu({ templates: [template] });
  renderer.click(byTestId(renderer, "message-template-trigger"));

  const item = byTestId(renderer, "message-template-item-0");
  assert.equal(item.props["aria-label"], template);
  assert.equal(item.props.title, undefined);
  assert.equal(item.props["aria-describedby"], undefined);
  assert.equal(renderer.findAll((node) => node.props.role === "tooltip").length, 0);
  assert.match(String(item.children[0]?.props.className ?? ""), /truncate/);
  renderer.unmount();
});

test("trigger and full-width panel render as sibling cells in the caller grid", () => {
  const renderer = createRenderer(React.createElement(
    "div",
    { "data-testid": "message-template-grid" },
    React.createElement(MessageTemplateMenu, props()),
  ));
  renderer.click(byTestId(renderer, "message-template-trigger"));

  const grid = byTestId(renderer, "message-template-grid");
  const trigger = byTestId(renderer, "message-template-trigger");
  const panel = byTestId(renderer, "message-template-panel");
  assert.equal(trigger.parent, grid);
  assert.equal(panel.parent, grid);
  assert.match(String(panel.props.className ?? ""), /(?:^|\s)col-span-full(?:\s|$)/);
  renderer.unmount();
});

test("storage failures render a non-blocking dismissible alert", () => {
  let dismissals = 0;
  const renderer = renderMenu({
    storageError: "消息模板无法保存到浏览器缓存",
    onDismissStorageError: () => { dismissals += 1; },
  });
  renderer.click(byTestId(renderer, "message-template-trigger"));
  const alert = renderer.findOne((node) => node.props.role === "alert");
  assert.match(renderer.text(alert), /无法保存/);
  renderer.click(byTestId(renderer, "message-template-error-dismiss"));
  assert.equal(dismissals, 1);
  renderer.unmount();
});
