import assert from 'node:assert/strict';
import test from 'node:test';
import { getMermaidThemeName, subscribeToDarkScheme } from './mermaid-theme.ts';

interface FakeMQL {
  matches: boolean;
  listeners: Array<() => void>;
  addEventListener(type: string, listener: () => void): void;
  removeEventListener(type: string, listener: () => void): void;
}

function createFakeMQL(matches: boolean): FakeMQL {
  return {
    matches,
    listeners: [],
    addEventListener(type: string, listener: () => void): void {
      if (type === 'change') {
        this.listeners.push(listener);
      }
    },
    removeEventListener(type: string, listener: () => void): void {
      if (type === 'change') {
        const index = this.listeners.indexOf(listener);
        if (index !== -1) {
          this.listeners.splice(index, 1);
        }
      }
    },
  };
}

test('getMermaidThemeName maps color-scheme preference to mermaid theme names', () => {
  assert.equal(getMermaidThemeName(false), 'default');
  assert.equal(getMermaidThemeName(true), 'dark');
});

test('subscribeToDarkScheme invokes callback synchronously with initial matches', () => {
  const dark = createFakeMQL(true);
  const seenDark: boolean[] = [];
  const unsubscribe = subscribeToDarkScheme(dark, (prefersDark) => seenDark.push(prefersDark));
  unsubscribe();

  assert.deepEqual(seenDark, [true]);

  const light = createFakeMQL(false);
  const seenLight: boolean[] = [];
  const unsubscribeLight = subscribeToDarkScheme(light, (prefersDark) => seenLight.push(prefersDark));
  unsubscribeLight();

  assert.deepEqual(seenLight, [false]);
});

test('subscribeToDarkScheme change listener reads current matches at call time', () => {
  const fake = createFakeMQL(true);
  const seen: boolean[] = [];
  const unsubscribe = subscribeToDarkScheme(fake, (prefersDark) => seen.push(prefersDark));

  assert.equal(fake.listeners.length, 1);

  fake.matches = false;
  fake.listeners[0]!();
  fake.matches = true;
  fake.listeners[0]!();
  unsubscribe();

  assert.deepEqual(seen, [true, false, true]);
});

test('subscribeToDarkScheme unsubscribe removes the listener', () => {
  const fake = createFakeMQL(false);
  const seen: boolean[] = [];
  const unsubscribe = subscribeToDarkScheme(fake, (prefersDark) => seen.push(prefersDark));

  unsubscribe();
  assert.equal(fake.listeners.length, 0);

  fake.matches = true;
  fake.listeners[0]?.();
  assert.deepEqual(seen, [false]);
});

test('subscribeToDarkScheme subscribers are independent', () => {
  const fake = createFakeMQL(false);
  const firstSeen: boolean[] = [];
  const secondSeen: boolean[] = [];
  const unsubscribeFirst = subscribeToDarkScheme(fake, (prefersDark) => firstSeen.push(prefersDark));
  const unsubscribeSecond = subscribeToDarkScheme(fake, (prefersDark) => secondSeen.push(prefersDark));

  assert.equal(fake.listeners.length, 2);

  fake.matches = true;
  for (const listener of [...fake.listeners]) listener();

  assert.deepEqual(firstSeen, [false, true]);
  assert.deepEqual(secondSeen, [false, true]);

  unsubscribeFirst();
  assert.equal(fake.listeners.length, 1);

  fake.matches = false;
  for (const listener of [...fake.listeners]) listener();

  assert.deepEqual(firstSeen, [false, true]);
  assert.deepEqual(secondSeen, [false, true, false]);

  unsubscribeSecond();
  assert.equal(fake.listeners.length, 0);
});
