import assert from "node:assert/strict";
import { test } from "node:test";

import {
  formatInputGainLabel,
  formatPostGainEstimate,
} from "./voice-level-format.ts";

test("input gain formatter rounds, signs, and pads integer dB values", () => {
  assert.equal(formatInputGainLabel(9.4), "gain +09 dB");
  assert.equal(formatInputGainLabel(11.4), "gain +11 dB");
  assert.equal(formatInputGainLabel(-5.4), "gain -05 dB");
  assert.equal(formatInputGainLabel(-0.4), "gain +00 dB");
  assert.equal(formatInputGainLabel(-5.5), "gain -06 dB");
  assert.equal(formatInputGainLabel(123.4), "gain +123 dB");
});

test("post-gain formatter keeps one decimal and pads the integer magnitude", () => {
  assert.equal(formatPostGainEstimate(-33.34), "Post-gain estimate -33.3 dBFS");
  assert.equal(formatPostGainEstimate(-3.34), "Post-gain estimate -03.3 dBFS");
  assert.equal(formatPostGainEstimate(9.04), "Post-gain estimate 09.0 dBFS");
  assert.equal(formatPostGainEstimate(123.04), "Post-gain estimate 123.0 dBFS");
});

test("post-gain formatter shows a stable placeholder for unavailable estimates", () => {
  assert.equal(formatPostGainEstimate(null), "Post-gain estimate -- dBFS");
  assert.equal(formatPostGainEstimate(Number.NaN), "Post-gain estimate -- dBFS");
  assert.equal(formatPostGainEstimate(Number.POSITIVE_INFINITY), "Post-gain estimate -- dBFS");
});
