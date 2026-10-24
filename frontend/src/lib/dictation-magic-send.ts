"use client";

import { useEffect, useRef } from "react";

export const DICTATION_MAGIC_SEND_STORAGE_KEY = "llm-asr-dictation-magic-send";
export const MIN_DICTATION_MAGIC_SEND_POLL_INTERVAL_MS = 100;

export interface DictationMagicSendRule {
  text: string;
  enabled: boolean;
}

export interface DictationMagicSendSettings {
  rules: DictationMagicSendRule[];
  pollIntervalMs: number;
}

export interface DictationMagicSendDefaults {
  commands: string[];
  pollIntervalMs: number;
}

export interface DictationMagicSendStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function isValidRule(value: unknown): value is DictationMagicSendRule {
  return Boolean(
    value &&
    typeof value === "object" &&
    typeof (value as DictationMagicSendRule).text === "string" &&
    typeof (value as DictationMagicSendRule).enabled === "boolean",
  );
}

export function normalizeDictationMagicSendSubmittedInterval(value: string): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= MIN_DICTATION_MAGIC_SEND_POLL_INTERVAL_MS
    ? Math.floor(parsed)
    : MIN_DICTATION_MAGIC_SEND_POLL_INTERVAL_MS;
}

export function loadDictationMagicSendSettings(
  stored: string | null | undefined,
  defaults: DictationMagicSendDefaults,
): DictationMagicSendSettings {
  const fallbackRules = defaults.commands.map((text) => ({ text, enabled: true }));
  let parsed: unknown;
  try {
    parsed = stored ? JSON.parse(stored) : undefined;
  } catch {
    parsed = undefined;
  }
  const cached = parsed && typeof parsed === "object" && (parsed as { version?: unknown }).version === 1
    ? parsed as Record<string, unknown>
    : undefined;
  const rules = Array.isArray(cached?.rules) && cached.rules.every(isValidRule)
    ? cached.rules.map((rule) => ({ ...rule, enabled: rule.text.trim() ? rule.enabled : false }))
    : fallbackRules;
  return {
    rules,
    pollIntervalMs: typeof cached?.pollIntervalMs === "number"
      && Number.isFinite(cached.pollIntervalMs)
      && cached.pollIntervalMs >= MIN_DICTATION_MAGIC_SEND_POLL_INTERVAL_MS
      ? Math.floor(cached.pollIntervalMs)
      : defaults.pollIntervalMs,
  };
}

export function readDictationMagicSendSettings(
  storage: DictationMagicSendStorage,
  defaults: DictationMagicSendDefaults,
): DictationMagicSendSettings {
  try {
    return loadDictationMagicSendSettings(storage.getItem(DICTATION_MAGIC_SEND_STORAGE_KEY), defaults);
  } catch {
    return loadDictationMagicSendSettings(null, defaults);
  }
}

export function writeDictationMagicSendSettings(storage: DictationMagicSendStorage, settings: DictationMagicSendSettings): void {
  try {
    storage.setItem(DICTATION_MAGIC_SEND_STORAGE_KEY, JSON.stringify({ version: 1, ...settings }));
  } catch {
    // Browser privacy settings may reject storage; the active settings still apply.
  }
}

export function findDictationMagicSendMatch(
  input: string,
  rules: readonly DictationMagicSendRule[],
): DictationMagicSendRule | undefined {
  return rules
    .filter((rule) => rule.enabled && rule.text.trim() && input.endsWith(rule.text))
    .sort((left, right) => right.text.length - left.text.length)[0];
}

export interface DictationMagicSendPollOptions {
  mode: "keyboard" | "dictation" | "voice";
  input: string;
  rules: readonly DictationMagicSendRule[];
  busy: boolean;
  send: (text: string, sourceInput: string) => Promise<void> | void;
  clear: () => void;
}

export function clearDictationMagicSendSourceIfUnchanged(current: string, source: string): string {
  return current === source ? "" : current;
}

/**
 * A Vision Automation submission can reject a trigger without starting a
 * request.  Only consume the dictation source after it reports acceptance,
 * and still protect edits made while that asynchronous submission was pending.
 */
export function clearAcceptedDictationMagicSendSource(
  accepted: boolean,
  current: string,
  source: string | undefined,
): string {
  return accepted && source
    ? clearDictationMagicSendSourceIfUnchanged(current, source)
    : current;
}

export function createDictationMagicSendPollController() {
  let inFlight = false;
  return {
    async poll(options: DictationMagicSendPollOptions): Promise<boolean> {
      if (options.mode !== "dictation" || options.busy || inFlight) return false;
      const match = findDictationMagicSendMatch(options.input, options.rules);
      if (!match) return false;
      const body = options.input.slice(0, -match.text.length).trim();
      if (!body) {
        options.clear();
        return true;
      }
      inFlight = true;
      try {
        await options.send(body, options.input);
        return true;
      } finally {
        inFlight = false;
      }
    },
  };
}

export interface DictationMagicSendPollScheduler {
  schedule(callback: () => void, delay: number): unknown;
  cancel(id: unknown): void;
  poll(): void;
}

export function createDictationMagicSendPollScheduler({ schedule, cancel, poll }: DictationMagicSendPollScheduler) {
  let timer: unknown;
  const stop = () => {
    if (timer === undefined) return;
    cancel(timer);
    timer = undefined;
  };
  return {
    start(delay: number) {
      stop();
      timer = schedule(poll, delay);
    },
    stop,
  };
}

export function useDictationMagicSend(options: DictationMagicSendPollOptions & { pollIntervalMs: number }): void {
  const latest = useRef(options);
  latest.current = options;
  const controllerRef = useRef(createDictationMagicSendPollController());
  const schedulerRef = useRef(createDictationMagicSendPollScheduler({
    schedule: (callback, delay) => window.setInterval(callback, delay),
    cancel: (timer) => window.clearInterval(timer as number),
    poll: () => { void controllerRef.current.poll(latest.current); },
  }));
  useEffect(() => {
    if (options.mode !== "dictation") return () => schedulerRef.current.stop();
    schedulerRef.current.start(options.pollIntervalMs);
    return () => schedulerRef.current.stop();
  }, [options.mode, options.pollIntervalMs]);
}
