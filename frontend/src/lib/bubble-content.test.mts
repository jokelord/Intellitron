import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  getWebUIBubbleCharacterLimit,
  type WebUIConfig,
} from "./web-ui-config.ts";

const require = createRequire(import.meta.url);
const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "../..");
const { createRenderer, installTask6TypeScriptHook } = require("./vision-automation/react-component-renderer.cjs");

installTask6TypeScriptHook(frontendRoot);

const React = require("react");
const CollapsibleBubbleContent = require("../components/CollapsibleBubbleContent.tsx").default;

test("getWebUIBubbleCharacterLimit preserves a supplied positive integer and falls back to 20000", () => {
  assert.equal(
    getWebUIBubbleCharacterLimit({ content: { bubble_character_limit: 17_321 } }),
    17_321,
  );
  assert.equal(getWebUIBubbleCharacterLimit(undefined), 20_000);

  for (const invalidValue of [0, -1, 2.5, Number.NaN, Number.POSITIVE_INFINITY, true, "20000"]) {
    const config = {
      content: { bubble_character_limit: invalidValue },
    } as unknown as WebUIConfig;
    assert.equal(getWebUIBubbleCharacterLimit(config), 20_000);
  }
});

test("CollapsibleBubbleContent renders a code-point window without mutating the full input", () => {
  const collapsedPrefix = `${"观".repeat(19_999)}🚀`;
  const suffix = "边界后的完整内容";
  const source = { content: `${collapsedPrefix}${suffix}` };

  assert.equal(Array.from(source.content).length, 20_000 + Array.from(suffix).length);

  const renderer = createRenderer(React.createElement(
    CollapsibleBubbleContent,
    { content: source.content, limit: 20_000 },
    (displayContent: string) => React.createElement("pre", { "data-testid": "bubble-body" }, displayContent),
  ));
  renderer.flush();

  const collapsedBody = renderer.findOne((node: any) => node.props["data-testid"] === "bubble-body");
  assert.equal(renderer.text(collapsedBody), collapsedPrefix);
  assert.equal(renderer.text(collapsedBody).includes(suffix), false);
  assert.equal(source.content, `${collapsedPrefix}${suffix}`);
  assert.ok(renderer.findOne((node: any) => renderer.text(node) === "Content collapsed"));

  const expand = renderer.findOne((node: any) => node.type === "button" && renderer.text(node) === "Expand all");
  renderer.click(expand);
  const expandedBody = renderer.findOne((node: any) => node.props["data-testid"] === "bubble-body");
  assert.equal(renderer.text(expandedBody), source.content);

  const collapse = renderer.findOne((node: any) => node.type === "button" && renderer.text(node) === "Collapse");
  renderer.click(collapse);
  const recollapsedBody = renderer.findOne((node: any) => node.props["data-testid"] === "bubble-body");
  assert.equal(renderer.text(recollapsedBody), collapsedPrefix);
  assert.equal(renderer.text(recollapsedBody).includes(suffix), false);
  renderer.unmount();
});

test("streaming bubbles show the latest line beyond the collapse limit and restore completed defaults", () => {
  const content = 'old line\nlatest inference line';
  const element = (streaming: boolean) => React.createElement(
    CollapsibleBubbleContent,
    { content, limit: 8, streaming },
    (text: string) => React.createElement('pre', { 'data-testid': 'body' }, text),
  );
  const renderer = createRenderer(element(true));
  const body = () => renderer.findOne((node: any) => node.props['data-testid'] === 'body');
  assert.equal(renderer.text(body()), content);
  assert.equal(renderer.findAll((node: any) => node.type === 'button').length, 0);
  renderer.update(element(false));
  assert.equal(renderer.text(body()), 'old line');
  renderer.unmount();
});
