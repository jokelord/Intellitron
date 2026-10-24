/**
 * Local chat-transcript persistence for non-Agent (normal chat) recovery.
 *
 * UI history preserves raw user messages and opaque accepted Chat-turn refs.
 * The backend resolves refs to frozen canonical model content; display-only
 * retrieved chunks never become request content. Runtime-only fields
 * (voice replay blob URLs, streaming flags) never survive a reload and are
 * dropped or neutralized here.
 */
import type { Message } from "./message-types";

export const CHAT_TRANSCRIPT_SCHEMA_VERSION = 1;

export type ChatTranscriptPageMode = "text" | "vision";

export type ChatTranscriptStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export type PersistedChatMessage = Omit<Message, "timestamp" | "voiceReplay" | "isStreaming"> & {
  timestamp: string;
  isStreaming?: false;
};

export interface PersistedChatTranscript {
  schemaVersion: number;
  savedAt: number;
  messages: PersistedChatMessage[];
}

/** readChatTranscript 的返回形态：messages 已 revive 为 UI 可用的 Message。 */
export interface RestoredChatTranscript {
  schemaVersion: number;
  savedAt: number;
  messages: Message[];
}

const storageKey = (pageMode: ChatTranscriptPageMode) => `llm-chat-transcript:${pageMode}`;

function getStorage(): ChatTranscriptStorage | undefined {
  return typeof window === "undefined" ? undefined : window.localStorage;
}

function sanitize(message: Message): PersistedChatMessage | null {
  if (typeof message !== "object" || message === null) return null;
  if (typeof message.id !== "string" || message.id.length === 0) return null;
  if (message.role !== "user" && message.role !== "assistant" && message.role !== "system") {
    return null;
  }
  const { voiceReplay: _voiceReplay, ...rest } = message;
  return {
    ...rest,
    timestamp: message.timestamp instanceof Date
      ? message.timestamp.toISOString()
      : String(message.timestamp),
    isStreaming: false,
  };
}

function revive(entry: unknown): Message | null {
  if (typeof entry !== "object" || entry === null) return null;
  const record = entry as Record<string, unknown>;
  if (typeof record.id !== "string" || record.id.length === 0) return null;
  if (record.role !== "user" && record.role !== "assistant" && record.role !== "system") {
    return null;
  }
  const timestamp = new Date(record.timestamp as string);
  const { isStreaming: _isStreaming, ...rest } = record;
  return {
    ...(rest as unknown as Message),
    timestamp: Number.isNaN(timestamp.getTime()) ? new Date() : timestamp,
    isStreaming: false,
  };
}

export function readChatTranscript(
  pageMode: ChatTranscriptPageMode,
  storage: ChatTranscriptStorage | undefined = getStorage(),
): RestoredChatTranscript | null {
  try {
    const raw = storage?.getItem(storageKey(pageMode));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed !== "object" || parsed === null
      || (parsed as Record<string, unknown>).schemaVersion !== CHAT_TRANSCRIPT_SCHEMA_VERSION
      || !Array.isArray((parsed as Record<string, unknown>).messages)
    ) {
      storage?.removeItem(storageKey(pageMode));
      return null;
    }
    const transcript = parsed as PersistedChatTranscript;
    const messages = transcript.messages
      .map(revive)
      .filter((message): message is Message => message !== null);
    return { schemaVersion: transcript.schemaVersion, savedAt: transcript.savedAt, messages };
  } catch {
    storage?.removeItem(storageKey(pageMode));
    return null;
  }
}

function writePayload(
  pageMode: ChatTranscriptPageMode,
  messages: PersistedChatMessage[],
  storage: ChatTranscriptStorage,
): void {
  const payload: PersistedChatTranscript = {
    schemaVersion: CHAT_TRANSCRIPT_SCHEMA_VERSION,
    savedAt: Date.now(),
    messages,
  };
  storage.setItem(storageKey(pageMode), JSON.stringify(payload));
}

export function writeChatTranscript(
  pageMode: ChatTranscriptPageMode,
  messages: Message[],
  storage: ChatTranscriptStorage | undefined = getStorage(),
): void {
  if (!storage) return;
  const sanitized = messages
    .map(sanitize)
    .filter((entry): entry is PersistedChatMessage => entry !== null);
  try {
    writePayload(pageMode, sanitized, storage);
    return;
  } catch {
    // 配额不足：退而只保留最新一半，再失败则放弃本次持久化。
  }
  try {
    writePayload(pageMode, sanitized.slice(Math.floor(sanitized.length / 2)), storage);
  } catch {
    /* ignore */
  }
}

export function clearChatTranscript(
  pageMode: ChatTranscriptPageMode,
  storage: ChatTranscriptStorage | undefined = getStorage(),
): void {
  storage?.removeItem(storageKey(pageMode));
}
