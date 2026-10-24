import assert from "node:assert/strict";
import { test } from "node:test";

import {
  INPUT_GAIN_STORAGE_KEY,
  readInputGainDb,
  writeInputGainDb,
  type InputGainStorage,
} from "./input-gain-storage.ts";

const range = { defaultDb: 0, minDb: -24, maxDb: 36, stepDb: 0.5 };

class MemoryStorage implements InputGainStorage {
  values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

test("input gain cache uses default only when no cached value exists", () => {
  assert.equal(readInputGainDb(new MemoryStorage(), range), 0);
});

test("input gain cache overrides the YAML default", () => {
  const storage = new MemoryStorage();
  storage.setItem(INPUT_GAIN_STORAGE_KEY, '{"version":1,"inputGainDb":6.5}');

  assert.equal(readInputGainDb(storage, range), 6.5);
});

test("malformed or invalid input gain caches fall back without throwing", () => {
  for (const cached of [
    "{",
    '{"version":2,"inputGainDb":6.5}',
    '{"version":1}',
    '{"version":1,"inputGainDb":"6.5"}',
    '{"version":1,"inputGainDb":null}',
    '{"version":1,"inputGainDb":NaN}',
    '{"version":1,"inputGainDb":Infinity}',
  ]) {
    const storage = new MemoryStorage();
    storage.setItem(INPUT_GAIN_STORAGE_KEY, cached);
    assert.equal(readInputGainDb(storage, range), 0);
    assert.equal(storage.getItem(INPUT_GAIN_STORAGE_KEY), null);
  }
});

test("input gain cache clamps and snaps values using the YAML grid", () => {
  for (const [cached, expected] of [[100, 36], [-100, -24], [6.26, 6.5]] as const) {
    const storage = new MemoryStorage();
    storage.setItem(
      INPUT_GAIN_STORAGE_KEY,
      JSON.stringify({ version: 1, inputGainDb: cached }),
    );
    assert.equal(readInputGainDb(storage, range), expected);
    assert.equal(
      storage.getItem(INPUT_GAIN_STORAGE_KEY),
      JSON.stringify({ version: 1, inputGainDb: expected }),
    );
  }
});

test("input gain storage failures keep voice input usable", () => {
  const unavailable: InputGainStorage = {
    getItem: () => { throw new Error("unavailable"); },
    setItem: () => { throw new Error("unavailable"); },
    removeItem: () => { throw new Error("unavailable"); },
  };

  assert.equal(readInputGainDb(unavailable, range), 0);
  assert.doesNotThrow(() => writeInputGainDb(unavailable, 6.5));
});

test("input gain cache writes versioned values", () => {
  const storage = new MemoryStorage();
  writeInputGainDb(storage, 6.5);
  assert.equal(
    storage.getItem(INPUT_GAIN_STORAGE_KEY),
    '{"version":1,"inputGainDb":6.5}',
  );
});
