import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const require = createRequire(import.meta.url);
const { createRenderer, installTask6TypeScriptHook } = require('../lib/vision-automation/react-component-renderer.cjs');
installTask6TypeScriptHook(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..'));
const React = require('react');
const LocalMemoryToggle = require('./LocalMemoryToggle.tsx').default;
for (const agentMode of [false, true]) {
  test(`memory toggle remains usable with agentMode=${agentMode}`, () => {
    const changes: boolean[] = [];
    const renderer = createRenderer(React.createElement(LocalMemoryToggle, { enabled: false, agentMode, onChange: (enabled: boolean) => changes.push(enabled) }));
    const button = renderer.findOne((node: any) => node.type === 'button');
    assert.notEqual(button.props.disabled, true);
    assert.equal(button.props['aria-pressed'], false);
    renderer.click(button);
    assert.deepEqual(changes, [true]);
    renderer.unmount();
  });
}
