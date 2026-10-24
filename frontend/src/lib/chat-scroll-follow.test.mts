import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as scrollFollow from './chat-scroll-follow.ts';

// EventTarget exercises the native handlers; metrics model a scrollable browser viewport.
class Viewport extends EventTarget {
  scrollTop = 500;
  clientHeight = 500;
  scrollHeight = 1000;
  scrollTo({ top }: { top: number }) {
    this.scrollTop = Math.max(0, Math.min(top, this.scrollHeight - this.clientHeight));
  }
}
function setup() {
  const viewport = new Viewport();
  assert.equal(typeof scrollFollow.attachChatScrollFollow, 'function', 'scroll follow needs persistent wheel-gated state');
  const follower = scrollFollow.attachChatScrollFollow(viewport as unknown as HTMLElement);
  follower.update(true, 'user-1');
  return { viewport, follower };
}
function wheel(viewport: Viewport, deltaY: number, deltaMode = 0, extras = {}) {
  const event = new Event('wheel', { cancelable: true });
  Object.assign(event, { deltaY, deltaMode, ctrlKey: false, ...extras });
  viewport.dispatchEvent(event);
  if (!event.defaultPrevented) {
    viewport.scrollTop = Math.max(0, Math.min(500, viewport.scrollTop + deltaY));
    viewport.dispatchEvent(new Event('scroll'));
  }
  return event.defaultPrevented;
}

test('output growth and browser scroll events cannot release the inference lock', () => {
  const { viewport, follower } = setup();
  viewport.scrollHeight = 1400;
  viewport.dispatchEvent(new Event('scroll'));
  assert.equal(viewport.scrollTop, 900);
  follower.dispose();
});

test('one upward notch is blocked; two upward notches allow reading history', () => {
  const { viewport, follower } = setup();
  assert.equal(wheel(viewport, -100), true);
  assert.equal(viewport.scrollTop, 500);
  assert.equal(wheel(viewport, -100), false);
  assert.equal(viewport.scrollTop, 400);
  viewport.scrollHeight = 1400;
  follower.update(true, 'user-1');
  assert.equal(viewport.scrollTop, 400);
  follower.dispose();
});

test('small trackpad deltas accumulate; downward movement resets upward intent', () => {
  const { viewport, follower } = setup();
  for (let i = 0; i < 19; i++) assert.equal(wheel(viewport, -10), true);
  assert.equal(viewport.scrollTop, 500);
  wheel(viewport, 10);
  assert.equal(wheel(viewport, -10), true);
  for (let i = 0; i < 18; i++) assert.equal(wheel(viewport, -10), true);
  assert.equal(wheel(viewport, -10), false);
  follower.dispose();
});

test('line-mode standard notches unlock after two, and zoom gestures do not count', () => {
  const { viewport, follower } = setup();
  wheel(viewport, -1000, 0, { ctrlKey: true });
  assert.equal(wheel(viewport, -3, 1), true);
  assert.equal(wheel(viewport, -3, 1), false);
  follower.dispose();
});

test('returning near the bottom remains unlocked until the newest line is actually reached', () => {
  const { viewport, follower } = setup();
  wheel(viewport, -200);
  viewport.scrollTop = 490;
  viewport.dispatchEvent(new Event('scroll'));
  viewport.scrollHeight = 1200;
  follower.update(true, 'user-1');
  assert.equal(viewport.scrollTop, 490);
  viewport.scrollTop = 700;
  viewport.dispatchEvent(new Event('scroll'));
  viewport.scrollHeight = 1400;
  follower.update(true, 'user-1');
  assert.equal(viewport.scrollTop, 900);
  assert.equal(wheel(viewport, -100), true, 'relocking resets the wheel accumulator');
  follower.dispose();
});

test('a new user request relocks even when the previous request is still busy', () => {
  const { viewport, follower } = setup();
  wheel(viewport, -200);
  follower.update(true, 'user-2');
  assert.equal(viewport.scrollTop, 500);
  wheel(viewport, -200);
  follower.update(false, 'user-2');
  follower.update(true, 'user-2');
  assert.equal(viewport.scrollTop, 500);
  follower.dispose();
});

test('completed chat remains manually scrollable and disposal removes scroll enforcement', () => {
  const { viewport, follower } = setup();
  follower.update(false, 'user-1');
  assert.equal(wheel(viewport, -10), false);
  viewport.scrollHeight = 1200;
  follower.update(false, 'user-1');
  assert.equal(viewport.scrollTop, 490);
  follower.update(true, 'user-2');
  follower.dispose();
  viewport.scrollTop = 300;
  viewport.dispatchEvent(new Event('scroll'));
  assert.equal(viewport.scrollTop, 300);
});

test('a queued bottom scroll event cannot relock before the unlocking wheel moves the viewport', () => {
  const { viewport, follower } = setup();
  const event = new Event('wheel', { cancelable: true });
  Object.assign(event, { deltaY: -200, deltaMode: 0, ctrlKey: false });
  viewport.dispatchEvent(event);
  assert.equal(event.defaultPrevented, false);
  viewport.dispatchEvent(new Event('scroll')); // older programmatic scroll delivery
  viewport.scrollTop = 300; // wheel default action is now visible
  viewport.dispatchEvent(new Event('scroll'));
  viewport.scrollHeight = 1400;
  follower.update(true, 'user-1');
  assert.equal(viewport.scrollTop, 300);
  viewport.scrollTop = 900;
  viewport.dispatchEvent(new Event('scroll'));
  viewport.scrollHeight = 1600;
  follower.update(true, 'user-1');
  assert.equal(viewport.scrollTop, 1100);
  follower.dispose();
});

test('idle upward wheel releases follow before a pending layout update can pull history back down', () => {
  const { viewport, follower } = setup();
  follower.update(false, 'user-1');
  const event = new Event('wheel', { cancelable: true });
  Object.assign(event, { deltaY: -10, deltaMode: 0, ctrlKey: false });
  viewport.dispatchEvent(event);
  viewport.scrollTop = 490;
  viewport.scrollHeight = 1200;
  follower.update(false, 'user-1');
  assert.equal(viewport.scrollTop, 490);
  follower.dispose();
});

test('two notches cannot abandon following when short content has no history to scroll into', () => {
  const { viewport, follower } = setup();
  viewport.scrollHeight = 400;
  viewport.scrollTop = 0;
  wheel(viewport, -100);
  wheel(viewport, -100);
  viewport.scrollHeight = 1400;
  follower.update(true, 'user-1');
  assert.equal(viewport.scrollTop, 900);
  follower.dispose();
});

test('page-mode wheel distance unlocks history and nested detail scrolling does not count toward unlock', () => {
  const savedElement = globalThis.Element;
  const savedStyle = globalThis.getComputedStyle;
  class Detail extends EventTarget {
    scrollHeight = 900;
    clientHeight = 200;
    scrollTop = 100;
  }
  globalThis.Element = Detail as any;
  globalThis.getComputedStyle = (() => ({ overflowY: 'auto' })) as any;
  let follower: ReturnType<typeof scrollFollow.attachChatScrollFollow> | undefined;
  try {
    const state = setup();
    follower = state.follower;
    const nested = new Event('wheel', { cancelable: true });
    Object.assign(nested, {
      deltaY: -300, deltaMode: 0, ctrlKey: false,
      composedPath: () => [new Detail(), state.viewport],
    });
    state.viewport.dispatchEvent(nested);
    assert.equal(nested.defaultPrevented, false, 'nested scrollable details remain readable');
    assert.equal(wheel(state.viewport, -100), true, 'nested wheel movement must not count toward outer unlock');
    assert.equal(wheel(state.viewport, -1, 2), false, 'one page exceeds the remaining one-notch distance');
    state.viewport.scrollHeight = 1400;
    follower.update(true, 'user-1');
    assert.equal(state.viewport.scrollTop, 499);
  } finally {
    follower?.dispose();
    globalThis.Element = savedElement;
    globalThis.getComputedStyle = savedStyle;
  }
});
