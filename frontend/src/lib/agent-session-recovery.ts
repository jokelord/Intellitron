export const AGENT_SESSION_BOOKMARK_SCHEMA_VERSION = 1;
import type { SessionAttachmentToken } from './session-hub-client.ts';
import { normalizeAgentSessionId } from './session-id.ts';

export type AgentSessionPageMode = "text" | "vision";

export type AgentSessionBookmark = {
  schemaVersion: 1;
  sessionId: string;
  pageMode: AgentSessionPageMode;
  createdAt: number;
};

export type SessionBookmarkStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

const storageKey = (pageMode: AgentSessionPageMode) => `llm-agent-session-bookmark:${pageMode}`;

function getStorage(): SessionBookmarkStorage | undefined {
  return typeof window === "undefined" ? undefined : window.localStorage;
}

function isBookmark(value: unknown, pageMode: AgentSessionPageMode): value is AgentSessionBookmark {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return Object.keys(record).length === 4
    && record.schemaVersion === AGENT_SESSION_BOOKMARK_SCHEMA_VERSION
    && typeof record.sessionId === "string" && record.sessionId.length > 0
    && record.pageMode === pageMode
    && typeof record.createdAt === "number" && Number.isFinite(record.createdAt) && record.createdAt > 0;
}

export function readAgentSessionBookmark(
  pageMode: AgentSessionPageMode,
  storage: SessionBookmarkStorage | undefined = getStorage(),
): AgentSessionBookmark | null {
  try {
    const raw = storage?.getItem(storageKey(pageMode));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (isBookmark(parsed, pageMode)) {
      return { ...parsed, sessionId: normalizeAgentSessionId(parsed.sessionId) };
    }
    storage?.removeItem(storageKey(pageMode));
  } catch {
    storage?.removeItem(storageKey(pageMode));
  }
  return null;
}

export function writeAgentSessionBookmark(
  input: Omit<AgentSessionBookmark, "schemaVersion">,
  storage: SessionBookmarkStorage | undefined = getStorage(),
): AgentSessionBookmark {
  const bookmark: AgentSessionBookmark = {
    schemaVersion: AGENT_SESSION_BOOKMARK_SCHEMA_VERSION,
    sessionId: normalizeAgentSessionId(input.sessionId),
    pageMode: input.pageMode,
    createdAt: input.createdAt,
  };
  if (!isBookmark(bookmark, input.pageMode)) {
    throw new TypeError("Agent session bookmark is invalid");
  }
  storage?.setItem(storageKey(input.pageMode), JSON.stringify(bookmark));
  return bookmark;
}

export function clearAgentSessionBookmark(
  pageMode: AgentSessionPageMode,
  storage: SessionBookmarkStorage | undefined = getStorage(),
): void {
  storage?.removeItem(storageKey(pageMode));
}

export type AgentSessionRecoveryFailure = {
  kind: "expired" | "transient";
  message: string;
};

/** Only the Hub's explicit 404/expiry error authorizes stale-bookmark dismissal. */
export function classifyAgentSessionRecoveryError(error: Error): AgentSessionRecoveryFailure {
  if (error.message === "Agent session is unavailable or expired") {
    return { kind: "expired", message: "Saved Agent session has expired." };
  }
  if (error.message === "Session is unavailable or expired") {
    return { kind: "expired", message: "Saved session has expired." };
  }
  return { kind: "transient", message: error.message };
}

export interface ClearRetainedAgentSessionActions {
  boundSessionId?: string;
  bookmarkSessionId?: string;
  clearBookmark(): void;
  detachLocal(): void | Promise<void>;
  deleteSession(sessionId: string): void | Promise<void>;
  onError(error: unknown): void;
}

/**
 * Trash is the only normal deletion path. Clear its durable recovery handle
 * synchronously, then independently attempt local detach and the exact Hub
 * deletion target so either rejection cannot prevent the other action.
 */
export function clearRetainedAgentSessionBeforeDelete(
  actions: ClearRetainedAgentSessionActions,
): string | undefined {
  const sessionId = actions.boundSessionId ?? actions.bookmarkSessionId;
  actions.clearBookmark();
  try {
    void Promise.resolve(actions.detachLocal()).catch(actions.onError);
  } catch (error) {
    actions.onError(error);
  }
  if (!sessionId) return undefined;
  try {
    void Promise.resolve(actions.deleteSession(sessionId)).catch(actions.onError);
  } catch (error) {
    actions.onError(error);
  }
  return sessionId;
}

export interface DetachRetainedAgentSessionSubscriberActions {
  isCurrentTransport?: boolean;
  transportToken?: SessionAttachmentToken;
  isCurrentToken?: (token: SessionAttachmentToken) => boolean;
  boundSessionId?: string;
  detachedSessionId?: string;
  detachLocal(): void;
}

/**
 * A dropped subscriber is not a retained-session retirement.  It may only
 * clear the local attachment it still owns; the bookmark remains the durable
 * recovery handle and Trash remains responsible for DELETE.
 */
export function detachRetainedAgentSessionSubscriber(
  actions: DetachRetainedAgentSessionSubscriberActions,
): boolean {
  const current = actions.transportToken
    ? (actions.isCurrentToken?.(actions.transportToken) ?? false)
    : actions.isCurrentTransport === true;
  if (!current
    || !actions.detachedSessionId
    || actions.boundSessionId !== actions.detachedSessionId) {
    return false;
  }
  actions.detachLocal();
  return true;
}
