import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const { createRenderer, installTask6TypeScriptHook } = require('./vision-automation/react-component-renderer.cjs');
installTask6TypeScriptHook(frontendRoot);
const React = require('react');
const { useChatScrollFollow } = require('./use-chat-scroll-follow.ts');

test('shared page hook follows resized content, preserves unlocked history, resets a new turn and cleans up', () => {
  const original = globalThis.ResizeObserver;
  let resize: () => void;
  let disconnected = false;
  const observed: unknown[] = [];
  globalThis.ResizeObserver = class {
    constructor(callback: () => void) { resize = callback; }
    observe(node: unknown) { observed.push(node); }
    disconnect() { disconnected = true; }
  } as any;
  let viewport: any;
  const events = new EventTarget();
  function Page({ messages, active }: any) {
    const { messageScrollContainerRef, messageContentRef, messageFooterRef } = useChatScrollFollow(messages, active);
    const assignViewport = React.useCallback((node: any) => {
      if (node && !node.addEventListener) {
        Object.assign(node, {
          scrollTop: 100, clientHeight: 500, scrollHeight: 1000,
          addEventListener: events.addEventListener.bind(events),
          removeEventListener: events.removeEventListener.bind(events),
          scrollTo({ top }: any) { node.scrollTop = Math.min(top, node.scrollHeight - node.clientHeight); },
        });
        viewport = node;
      }
      messageScrollContainerRef.current = node;
    }, []);
    return React.createElement('div', { ref: assignViewport },
      React.createElement('main', { ref: messageContentRef }, messages[messages.length - 1]?.id),
      React.createElement('footer', { ref: messageFooterRef }, 'input')); 
  }
  const user = { id: 'user-1', role: 'user' };
  let renderer: any;
  try {
    renderer = createRenderer(React.createElement(Page, { messages: [user], active: true }));
    assert.equal(viewport.scrollTop, 500, 'inference starts at the latest line even from history');
    assert.equal(observed.length, 3, 'watch shell, content and sticky footer geometry');
    assert.equal((observed[2] as any).type, 'footer');
    viewport.scrollHeight = 1200;
    resize!();
    assert.equal(viewport.scrollTop, 700, 'async image/footer layout growth follows without a message update');
    const wheel = new Event('wheel', { cancelable: true });
    Object.assign(wheel, { deltaY: -200, deltaMode: 0, ctrlKey: false });
    events.dispatchEvent(wheel);
    viewport.scrollTop = 400;
    events.dispatchEvent(new Event('scroll'));
    viewport.scrollHeight = 1600;
    resize!();
    assert.equal(viewport.scrollTop, 400, 'resizing must preserve the unlocked reading position');
    renderer.update(React.createElement(Page, { messages: [user, { id: 'assistant-1', role: 'assistant' }], active: true }));
    assert.equal(viewport.scrollTop, 400, 'a token update must not relock');
    renderer.update(React.createElement(Page, { messages: [user, { id: 'user-2', role: 'user' }], active: true }));
    assert.equal(viewport.scrollTop, 1100, 'next user turn relocks even with continuous busy state');
    renderer.unmount();
    assert.equal(disconnected, true);
    viewport.scrollTop = 100;
    events.dispatchEvent(new Event('scroll'));
    assert.equal(viewport.scrollTop, 100);
  } finally {
    renderer?.unmount();
    globalThis.ResizeObserver = original;
  }
});
