import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const require = createRequire(import.meta.url);
const { createRenderer, installTask6TypeScriptHook } = require('../lib/vision-automation/react-component-renderer.cjs');
installTask6TypeScriptHook(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..'));
const React = require('react');
const RetrievedLocalMemories = require('./RetrievedLocalMemories.tsx').default;

test('no retrieved chunks renders no showcase control', () => {
  for (const chunks of [undefined, []]) {
    const renderer = createRenderer(React.createElement(RetrievedLocalMemories, { chunks }));
    assert.equal(renderer.findAll((node: any) => node.type === 'button').length, 0);
    renderer.unmount();
  }
});

test('showcase defaults collapsed, displays count and toggles unmodified plain text accessibly', () => {
  const chunks = [{ text: '<script>raw</script>\n[not a link](/private/file.md)' }, { text: 'second\nline' }];
  const renderer = createRenderer(React.createElement(RetrievedLocalMemories, { chunks }));
  let button = renderer.findOne((node: any) => node.type === 'button');
  assert.equal(button.props['aria-expanded'], false);
  assert.equal(renderer.text(button).includes('2'), true);
  assert.equal(renderer.text(renderer.container).includes(chunks[0].text), false);
  renderer.click(button);
  button = renderer.findOne((node: any) => node.type === 'button');
  assert.equal(button.props['aria-expanded'], true);
  assert.equal(renderer.findAll((node: any) => node.type === 'pre').length, 2);
  assert.equal(renderer.findAll((node: any) => node.type === 'a' || node.type === 'script').length, 0);
  assert.equal(renderer.text(renderer.container).includes(chunks[0].text), true);
  renderer.click(button);
  assert.equal(renderer.findOne((node: any) => node.type === 'button').props['aria-expanded'], false);
  assert.equal(renderer.findAll((node: any) => node.type === 'pre').length, 0);
  renderer.unmount();
});
