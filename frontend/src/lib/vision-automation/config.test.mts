import assert from "node:assert/strict";
import { test } from "node:test";

import {
  VISION_AUTOMATION_CONFIG_SCHEMA_VERSION,
  VISION_AUTOMATION_STORAGE_KEY,
  loadVisionAutomationConfig,
  saveVisionAutomationPreferences,
  type VisionAutomationServerConfig,
} from "./config.ts";

const serverConfig: VisionAutomationServerConfig = {
  enabled: true,
  intervalSeconds: 1,
  maxFramesPerSource: 16,
  thumbnailSizePx: 32,
  captureDuringInference: false,
  tickTimeoutSeconds: 10,
  consecutiveTickFailureLimit: 3,
  lockFirstBundle: true,
  maxEncodedPayloadBytes: 134_217_728,
  initialJpegQuality: 85,
  minimumJpegQuality: 40,
  minimumImageDimensionPx: 256,
  maxImageDimensionPx: 2048,
  base64EncodeConcurrency: 4,
  maxRequestBodyBytes: 142_606_336,
  requestStatusTtlSeconds: 600,
};

function storageWith(value: string | null) {
  const values = new Map<string, string>();
  if (value !== null) values.set(VISION_AUTOMATION_STORAGE_KEY, value);
  return {
    getItem(key: string) {
      return values.get(key) ?? null;
    },
    setItem(key: string, next: string) {
      values.set(key, next);
    },
    value() {
      return values.get(VISION_AUTOMATION_STORAGE_KEY) ?? null;
    },
  };
}

test("missing localStorage data uses the complete server configuration", () => {
  const loaded = loadVisionAutomationConfig(serverConfig, storageWith(null));
  assert.equal(loaded.source, "server");
  assert.deepEqual(loaded.config, serverConfig);
});

test("valid local preferences override only browser-controlled fields", () => {
  const local = {
    schemaVersion: VISION_AUTOMATION_CONFIG_SCHEMA_VERSION,
    intervalSeconds: 0.25,
    maxFramesPerSource: 8,
    thumbnailSizePx: 64,
    captureDuringInference: true,
    maxEncodedPayloadBytes: 32 * 1024 * 1024,
    lockFirstBundle: false,
  };
  const loaded = loadVisionAutomationConfig(serverConfig, storageWith(JSON.stringify(local)));

  assert.equal(loaded.source, "local");
  assert.equal(loaded.config.intervalSeconds, 0.25);
  assert.equal(loaded.config.maxEncodedPayloadBytes, 32 * 1024 * 1024);
  assert.equal(loaded.config.lockFirstBundle, false);
  assert.equal(loaded.config.initialJpegQuality, 85);
  assert.equal(loaded.config.base64EncodeConcurrency, 4);
});

test("v1 preferences migrate without discarding valid prior choices and inherit the server lock", () => {
  const v1 = {
    schemaVersion: 1,
    intervalSeconds: 0.25,
    maxFramesPerSource: 8,
    thumbnailSizePx: 64,
    captureDuringInference: false,
    maxEncodedPayloadBytes: 32 * 1024 * 1024,
  };
  const loaded = loadVisionAutomationConfig(serverConfig, storageWith(JSON.stringify(v1)));

  assert.equal(loaded.source, "local");
  assert.equal(loaded.config.intervalSeconds, 0.25);
  assert.equal(loaded.config.lockFirstBundle, true);
});

test("invalid JSON, version, missing fields, extras, or bounds discard the entire local object", () => {
  const invalidValues = [
    "not-json",
    JSON.stringify({
      schemaVersion: 999,
      intervalSeconds: 0.25,
      maxFramesPerSource: 8,
      thumbnailSizePx: 64,
      captureDuringInference: true,
      maxEncodedPayloadBytes: 1024,
    }),
    JSON.stringify({
      schemaVersion: VISION_AUTOMATION_CONFIG_SCHEMA_VERSION,
      intervalSeconds: 0.25,
    }),
    JSON.stringify({
      schemaVersion: VISION_AUTOMATION_CONFIG_SCHEMA_VERSION,
      intervalSeconds: 0.09,
      maxFramesPerSource: 8,
      thumbnailSizePx: 64,
      captureDuringInference: true,
      maxEncodedPayloadBytes: 1024,
    }),
    JSON.stringify({
      schemaVersion: VISION_AUTOMATION_CONFIG_SCHEMA_VERSION,
      intervalSeconds: 0.25,
      maxFramesPerSource: 8,
      thumbnailSizePx: 64,
      captureDuringInference: true,
      maxEncodedPayloadBytes: serverConfig.maxEncodedPayloadBytes + 1,
    }),
    JSON.stringify({
      schemaVersion: VISION_AUTOMATION_CONFIG_SCHEMA_VERSION,
      intervalSeconds: 0.25,
      maxFramesPerSource: 8,
      thumbnailSizePx: 64,
      captureDuringInference: true,
      maxEncodedPayloadBytes: 1024,
      deviceId: "must-not-persist",
    }),
  ];

  for (const value of invalidValues) {
    const loaded = loadVisionAutomationConfig(serverConfig, storageWith(value));
    assert.equal(loaded.source, "server");
    assert.deepEqual(loaded.config, serverConfig);
  }
});

test("unavailable localStorage falls back without weakening server validation", () => {
  const storage = {
    getItem() {
      throw new DOMException("blocked", "SecurityError");
    },
    setItem() {
      throw new DOMException("blocked", "SecurityError");
    },
  };
  assert.deepEqual(loadVisionAutomationConfig(serverConfig, storage), {
    config: serverConfig,
    source: "server",
  });
  assert.equal(saveVisionAutomationPreferences(serverConfig, storage), false);
});

test("persistence writes only versioned browser preferences", () => {
  const storage = storageWith(null);
  assert.equal(saveVisionAutomationPreferences({
    ...serverConfig,
    intervalSeconds: 2,
    captureDuringInference: true,
  }, storage), true);

  assert.deepEqual(JSON.parse(storage.value()!), {
    schemaVersion: VISION_AUTOMATION_CONFIG_SCHEMA_VERSION,
    intervalSeconds: 2,
    maxFramesPerSource: 16,
    thumbnailSizePx: 32,
    captureDuringInference: true,
    maxEncodedPayloadBytes: 134_217_728,
    lockFirstBundle: true,
  });
  assert.doesNotMatch(storage.value()!, /quality|dimension|concurrency|device/i);
});

test("invalid server-controlled bounds are rejected", () => {
  assert.throws(
    () => loadVisionAutomationConfig({ ...serverConfig, base64EncodeConcurrency: 9 }, null),
    /base64EncodeConcurrency/,
  );
  assert.throws(
    () => loadVisionAutomationConfig({ ...serverConfig, maxRequestBodyBytes: 64 }, null),
    /maxRequestBodyBytes/,
  );
});
