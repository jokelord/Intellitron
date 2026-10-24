export const INPUT_GAIN_STORAGE_KEY = "llm-asr-input-gain";

export interface InputGainRange {
  defaultDb: number;
  minDb: number;
  maxDb: number;
  stepDb: number;
}

export interface InputGainStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export function normalizeInputGainDb(value: number, range: InputGainRange): number {
  const bounded = Math.max(range.minDb, Math.min(range.maxDb, value));
  const ticks = Math.round((bounded - range.minDb) / range.stepDb);
  const snapped = range.minDb + ticks * range.stepDb;
  return Math.max(range.minDb, Math.min(range.maxDb, Number(snapped.toFixed(10))));
}

function removeInvalidValue(storage: InputGainStorage): void {
  try {
    storage.removeItem(INPUT_GAIN_STORAGE_KEY);
  } catch {
    // localStorage can be unavailable in privacy-restricted browser contexts.
  }
}

export function writeInputGainDb(storage: InputGainStorage, value: number): void {
  try {
    storage.setItem(
      INPUT_GAIN_STORAGE_KEY,
      JSON.stringify({ version: 1, inputGainDb: value }),
    );
  } catch {
    // Persistence is optional; the controller keeps the value in memory.
  }
}

export function readInputGainDb(storage: InputGainStorage, range: InputGainRange): number {
  let raw: string | null;
  try {
    raw = storage.getItem(INPUT_GAIN_STORAGE_KEY);
  } catch {
    return range.defaultDb;
  }
  if (raw === null) return range.defaultDb;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    removeInvalidValue(storage);
    return range.defaultDb;
  }
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    (parsed as { version?: unknown }).version !== 1 ||
    typeof (parsed as { inputGainDb?: unknown }).inputGainDb !== "number" ||
    !Number.isFinite((parsed as { inputGainDb: number }).inputGainDb)
  ) {
    removeInvalidValue(storage);
    return range.defaultDb;
  }

  const cached = (parsed as { inputGainDb: number }).inputGainDb;
  const normalized = normalizeInputGainDb(cached, range);
  if (normalized !== cached) writeInputGainDb(storage, normalized);
  return normalized;
}
