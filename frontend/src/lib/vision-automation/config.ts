export const VISION_AUTOMATION_CONFIG_SCHEMA_VERSION = 2 as const;
export const VISION_AUTOMATION_STORAGE_KEY = "llm-deployment.vision-automation.config.v1";
export const MAX_ENCODED_AUTOMATION_PAYLOAD_BYTES = 134_217_728;
export const MAX_VISION_AUTOMATION_REQUEST_BODY_BYTES = 142_606_336;

export interface VisionAutomationServerConfig {
  enabled: boolean;
  intervalSeconds: number;
  maxFramesPerSource: number;
  thumbnailSizePx: number;
  captureDuringInference: boolean;
  lockFirstBundle: boolean;
  tickTimeoutSeconds: number;
  consecutiveTickFailureLimit: number;
  maxEncodedPayloadBytes: number;
  initialJpegQuality: number;
  minimumJpegQuality: number;
  minimumImageDimensionPx: number;
  maxImageDimensionPx: number;
  base64EncodeConcurrency: number;
  maxRequestBodyBytes: number;
  requestStatusTtlSeconds: number;
}

export interface VisionAutomationPreferences {
  schemaVersion: typeof VISION_AUTOMATION_CONFIG_SCHEMA_VERSION;
  intervalSeconds: number;
  maxFramesPerSource: number;
  thumbnailSizePx: number;
  captureDuringInference: boolean;
  lockFirstBundle: boolean;
  maxEncodedPayloadBytes: number;
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface LoadedVisionAutomationConfig {
  config: VisionAutomationServerConfig;
  source: "server" | "local";
}

const LOCAL_KEYS = [
  "schemaVersion",
  "intervalSeconds",
  "maxFramesPerSource",
  "thumbnailSizePx",
  "captureDuringInference",
  "lockFirstBundle",
  "maxEncodedPayloadBytes",
] as const;

function assertFiniteRange(name: string, value: number, minimum: number, maximum = Infinity): void {
  if (!Number.isFinite(value) || value < minimum || value > maximum) {
    throw new RangeError(`${name} must be between ${minimum} and ${maximum}`);
  }
}

function assertIntegerRange(name: string, value: number, minimum: number, maximum = Infinity): void {
  assertFiniteRange(name, value, minimum, maximum);
  if (!Number.isInteger(value)) throw new RangeError(`${name} must be an integer`);
}

export function validateVisionAutomationServerConfig(
  config: VisionAutomationServerConfig,
): VisionAutomationServerConfig {
  if (typeof config.enabled !== "boolean") throw new TypeError("enabled must be boolean");
  if (typeof config.captureDuringInference !== "boolean") {
    throw new TypeError("captureDuringInference must be boolean");
  }
  if (typeof config.lockFirstBundle !== "boolean") throw new TypeError("lockFirstBundle must be boolean");
  assertFiniteRange("intervalSeconds", config.intervalSeconds, 0.1);
  assertIntegerRange("maxFramesPerSource", config.maxFramesPerSource, 1, 250);
  assertIntegerRange("thumbnailSizePx", config.thumbnailSizePx, 16, 128);
  assertFiniteRange("tickTimeoutSeconds", config.tickTimeoutSeconds, 1);
  assertIntegerRange("consecutiveTickFailureLimit", config.consecutiveTickFailureLimit, 1);
  assertIntegerRange("maxEncodedPayloadBytes", config.maxEncodedPayloadBytes, 1, MAX_ENCODED_AUTOMATION_PAYLOAD_BYTES);
  assertIntegerRange("initialJpegQuality", config.initialJpegQuality, 40, 95);
  assertIntegerRange("minimumJpegQuality", config.minimumJpegQuality, 1, config.initialJpegQuality);
  assertIntegerRange("minimumImageDimensionPx", config.minimumImageDimensionPx, 64);
  assertIntegerRange("maxImageDimensionPx", config.maxImageDimensionPx, 1);
  assertIntegerRange("base64EncodeConcurrency", config.base64EncodeConcurrency, 1, 8);
  assertIntegerRange("maxRequestBodyBytes", config.maxRequestBodyBytes, 1, MAX_VISION_AUTOMATION_REQUEST_BODY_BYTES);
  if (config.maxRequestBodyBytes <= config.maxEncodedPayloadBytes) {
    throw new RangeError("maxRequestBodyBytes must exceed maxEncodedPayloadBytes");
  }
  assertFiniteRange("requestStatusTtlSeconds", config.requestStatusTtlSeconds, 1);
  return { ...config };
}

function parsePreferences(
  raw: string,
  server: VisionAutomationServerConfig,
): VisionAutomationPreferences | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  const isV1 = record.schemaVersion === 1
    && keys.length === LOCAL_KEYS.length - 1
    && LOCAL_KEYS.filter((key) => key !== "lockFirstBundle").every((key) => keys.includes(key));
  const isV2 = record.schemaVersion === VISION_AUTOMATION_CONFIG_SCHEMA_VERSION
    && keys.length === LOCAL_KEYS.length
    && LOCAL_KEYS.every((key) => keys.includes(key));
  if (!isV1 && !isV2) return null;
  if (typeof record.captureDuringInference !== "boolean") return null;
  if (isV2 && typeof record.lockFirstBundle !== "boolean") return null;
  try {
    assertFiniteRange("intervalSeconds", record.intervalSeconds as number, 0.1);
    assertIntegerRange("maxFramesPerSource", record.maxFramesPerSource as number, 1, 250);
    assertIntegerRange("thumbnailSizePx", record.thumbnailSizePx as number, 16, 128);
    assertIntegerRange(
      "maxEncodedPayloadBytes",
      record.maxEncodedPayloadBytes as number,
      1,
      Math.min(server.maxEncodedPayloadBytes, MAX_ENCODED_AUTOMATION_PAYLOAD_BYTES),
    );
  } catch {
    return null;
  }
  return {
    ...record,
    schemaVersion: VISION_AUTOMATION_CONFIG_SCHEMA_VERSION,
    lockFirstBundle: isV2 ? record.lockFirstBundle : server.lockFirstBundle,
  } as VisionAutomationPreferences;
}

export function toVisionAutomationPreferences(
  config: VisionAutomationServerConfig,
): VisionAutomationPreferences {
  return {
    schemaVersion: VISION_AUTOMATION_CONFIG_SCHEMA_VERSION,
    intervalSeconds: config.intervalSeconds,
    maxFramesPerSource: config.maxFramesPerSource,
    thumbnailSizePx: config.thumbnailSizePx,
    captureDuringInference: config.captureDuringInference,
    lockFirstBundle: config.lockFirstBundle,
    maxEncodedPayloadBytes: config.maxEncodedPayloadBytes,
  };
}

export function loadVisionAutomationConfig(
  serverConfig: VisionAutomationServerConfig,
  storage: StorageLike | null,
): LoadedVisionAutomationConfig {
  const server = validateVisionAutomationServerConfig(serverConfig);
  let raw: string | null = null;
  try {
    raw = storage?.getItem(VISION_AUTOMATION_STORAGE_KEY) ?? null;
  } catch {
    return { config: server, source: "server" };
  }
  if (raw === null) return { config: server, source: "server" };
  const local = parsePreferences(raw, server);
  if (local === null) return { config: server, source: "server" };
  return {
    config: {
      ...server,
      intervalSeconds: local.intervalSeconds,
      maxFramesPerSource: local.maxFramesPerSource,
      thumbnailSizePx: local.thumbnailSizePx,
      captureDuringInference: local.captureDuringInference,
      lockFirstBundle: local.lockFirstBundle,
      maxEncodedPayloadBytes: local.maxEncodedPayloadBytes,
    },
    source: "local",
  };
}

export function saveVisionAutomationPreferences(
  config: VisionAutomationServerConfig,
  storage: StorageLike | null,
): boolean {
  if (storage === null) return false;
  try {
    storage.setItem(
      VISION_AUTOMATION_STORAGE_KEY,
      JSON.stringify(toVisionAutomationPreferences(config)),
    );
    return true;
  } catch {
    return false;
  }
}
