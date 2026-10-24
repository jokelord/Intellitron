import {
  SessionAttachmentRegistry,
  type SessionAttachmentToken,
  type SessionHubPayloadHandlers,
  type SessionHubReadResult,
} from './session-hub-client.ts';
import { normalizeAgentSessionId } from './session-id.ts';

export const SESSION_HUB_BOOKMARK_SCHEMA_VERSION = 1;

export type SessionHubPageMode = 'text' | 'vision';
export type SessionHubInteractionMode = 'chat' | 'agent';
export type SessionHubBookmarkStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export interface SessionHubBookmark {
  schemaVersion: 1;
  sessionId: string;
  pageMode: SessionHubPageMode;
  interactionMode: SessionHubInteractionMode;
  createdAt: number;
}

export interface SessionHubRecoveryCandidate {
  bookmark: SessionHubBookmark;
  legacyStorageKey?: string;
  commitMigration(storage?: SessionHubBookmarkStorage): void;
}

export interface SessionHubBookmarkTransaction {
  readonly bookmark: SessionHubBookmark;
  readonly token: symbol;
}

type OwnedBookmarkTransaction = SessionHubBookmarkTransaction & {
  readonly prior: SessionHubBookmark | null;
};

const bookmarkKey = (pageMode: SessionHubPageMode) => `llm-session-hub-bookmark:${pageMode}`;
const legacyAgentKey = (pageMode: SessionHubPageMode) => `llm-agent-session-bookmark:${pageMode}`;
const legacyChatKey = (pageMode: SessionHubPageMode) => `llm-chat-transcript:${pageMode}`;

function browserStorage(): SessionHubBookmarkStorage | undefined {
  return typeof window === 'undefined' ? undefined : window.localStorage;
}

function isBookmark(value: unknown, pageMode: SessionHubPageMode): value is SessionHubBookmark {
  if (typeof value !== 'object' || value === null) return false;
  const record = value as Record<string, unknown>;
  return Object.keys(record).length === 5
    && record.schemaVersion === SESSION_HUB_BOOKMARK_SCHEMA_VERSION
    && typeof record.sessionId === 'string' && record.sessionId.length > 0
    && record.pageMode === pageMode
    && (record.interactionMode === 'chat' || record.interactionMode === 'agent')
    && typeof record.createdAt === 'number' && Number.isFinite(record.createdAt) && record.createdAt > 0;
}

export function readSessionHubBookmark(
  pageMode: SessionHubPageMode,
  storage: SessionHubBookmarkStorage | undefined = browserStorage(),
): SessionHubBookmark | null {
  const key = bookmarkKey(pageMode);
  try {
    const raw = storage?.getItem(key);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (isBookmark(parsed, pageMode)) {
      return parsed.interactionMode === 'agent'
        ? { ...parsed, sessionId: normalizeAgentSessionId(parsed.sessionId) }
        : parsed;
    }
    storage?.removeItem(key);
  } catch {
    storage?.removeItem(key);
  }
  return null;
}

export function writeSessionHubBookmark(
  input: Omit<SessionHubBookmark, 'schemaVersion'>,
  storage: SessionHubBookmarkStorage | undefined = browserStorage(),
): SessionHubBookmark {
  const bookmark: SessionHubBookmark = {
    schemaVersion: SESSION_HUB_BOOKMARK_SCHEMA_VERSION,
    sessionId: input.interactionMode === 'agent'
      ? normalizeAgentSessionId(input.sessionId)
      : input.sessionId,
    pageMode: input.pageMode,
    interactionMode: input.interactionMode,
    createdAt: input.createdAt,
  };
  if (!isBookmark(bookmark, input.pageMode)) throw new TypeError('Session Hub bookmark is invalid');
  storage?.setItem(bookmarkKey(input.pageMode), JSON.stringify(bookmark));
  return bookmark;
}

export function clearSessionHubBookmark(
  pageMode: SessionHubPageMode,
  storage: SessionHubBookmarkStorage | undefined = browserStorage(),
): void {
  storage?.removeItem(bookmarkKey(pageMode));
}

/** Explicit Trash cleanup; ordinary detach/terminal paths never call this. */
export function clearSessionHubRecoveryState(
  pageMode: SessionHubPageMode,
  storage: SessionHubBookmarkStorage | undefined = browserStorage(),
): void {
  storage?.removeItem(bookmarkKey(pageMode));
  storage?.removeItem(legacyAgentKey(pageMode));
  storage?.removeItem(legacyChatKey(pageMode));
}

export function resolveSessionHubClearTarget(input: {
  bookmarkSessionId?: string;
  attachedSessionId?: string;
  staleBoundSessionId?: string;
}): string | undefined {
  // The attached session is the live canonical owner after an interaction-mode
  // transition (for example Agent -> Chat). A recovery bookmark can still be
  // the previous mode until the acceptance projection commits it, so it must
  // not win over a current attachment.
  return input.attachedSessionId
    ?? input.bookmarkSessionId
    ?? input.staleBoundSessionId;
}

/**
 * Reversible ownership for the pre-observation acceptance window. The durable
 * value remains the exact public bookmark schema; transaction state is memory-only.
 */
export class SessionHubBookmarkTransactionRegistry {
  private current: OwnedBookmarkTransaction | null = null;

  begin(
    input: Omit<SessionHubBookmark, 'schemaVersion'>,
    storage: SessionHubBookmarkStorage | undefined = browserStorage(),
  ): SessionHubBookmarkTransaction {
    const prior = readSessionHubBookmark(input.pageMode, storage);
    const transaction: OwnedBookmarkTransaction = Object.freeze({
      bookmark: writeSessionHubBookmark(input, storage),
      prior,
      token: Symbol(input.sessionId),
    });
    this.current = transaction;
    return transaction;
  }

  accept(transaction: SessionHubBookmarkTransaction): boolean {
    if (!this.isCurrent(transaction)) return false;
    this.current = null;
    return true;
  }

  retain(transaction: SessionHubBookmarkTransaction): boolean {
    if (!this.isCurrent(transaction)) return false;
    this.current = null;
    return true;
  }

  rollback(
    transaction: SessionHubBookmarkTransaction,
    storage: SessionHubBookmarkStorage | undefined = browserStorage(),
  ): boolean {
    if (!this.isCurrent(transaction)) return false;
    const owned = this.current;
    this.current = null;
    if (owned?.prior) writeSessionHubBookmark(owned.prior, storage);
    else clearSessionHubBookmark(transaction.bookmark.pageMode, storage);
    return true;
  }

  isCurrent(transaction: SessionHubBookmarkTransaction): boolean {
    return this.current?.token === transaction.token;
  }
}

export class SessionHubLiveAttachmentRegistry {
  private readonly registry = new SessionAttachmentRegistry();
  private errorDisplayedFor: SessionAttachmentToken | null = null;

  attach(sessionId: string): SessionAttachmentToken {
    const attachment = this.registry.attach(sessionId);
    this.errorDisplayedFor = null;
    return attachment;
  }

  isCurrent(attachment: SessionAttachmentToken): boolean {
    return this.registry.isCurrent(attachment);
  }

  getCurrent(): SessionAttachmentToken | null {
    return this.registry.getCurrent();
  }

  applyError(attachment: SessionAttachmentToken, display: () => void): boolean {
    if (!this.registry.isCurrent(attachment) || this.errorDisplayedFor === attachment) return false;
    this.errorDisplayedFor = attachment;
    display();
    return true;
  }

  detach(attachment: SessionAttachmentToken, finalize: () => void): boolean {
    if (!this.registry.detach(attachment)) return false;
    if (this.errorDisplayedFor === attachment) this.errorDisplayedFor = null;
    finalize();
    return true;
  }
}

/** Re-check ownership after an async lifecycle callback before mutating page state. */
export function isCurrentSessionHubAttachment(
  attachment: SessionAttachmentToken | null | undefined,
  registry: Pick<SessionHubLiveAttachmentRegistry, 'isCurrent'>,
): attachment is SessionAttachmentToken {
  return attachment !== null && attachment !== undefined && registry.isCurrent(attachment);
}

function parseLegacyAgent(
  pageMode: SessionHubPageMode,
  storage: SessionHubBookmarkStorage,
): SessionHubBookmark | null {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(legacyAgentKey(pageMode)) ?? 'null');
    if (typeof parsed !== 'object' || parsed === null) return null;
    const record = parsed as Record<string, unknown>;
    if (record.schemaVersion !== 1 || record.pageMode !== pageMode
      || typeof record.sessionId !== 'string' || record.sessionId.length === 0
      || typeof record.createdAt !== 'number' || !Number.isFinite(record.createdAt)) return null;
    return {
      schemaVersion: 1,
      sessionId: normalizeAgentSessionId(record.sessionId),
      pageMode,
      interactionMode: 'agent',
      createdAt: record.createdAt,
    };
  } catch {
    return null;
  }
}

function parseLegacyChat(
  pageMode: SessionHubPageMode,
  storage: SessionHubBookmarkStorage,
): SessionHubBookmark | null {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(legacyChatKey(pageMode)) ?? 'null');
    if (typeof parsed !== 'object' || parsed === null) return null;
    const record = parsed as Record<string, unknown>;
    if (record.schemaVersion !== 1 || !Array.isArray(record.messages)) return null;
    const target = [...record.messages].reverse().find((entry) => {
      if (typeof entry !== 'object' || entry === null) return false;
      const resume = (entry as Record<string, unknown>).chatResume;
      return typeof resume === 'object' && resume !== null
        && typeof (resume as Record<string, unknown>).sessionId === 'string';
    }) as Record<string, unknown> | undefined;
    const resume = target?.chatResume as Record<string, unknown> | undefined;
    if (typeof resume?.sessionId !== 'string' || resume.sessionId.length === 0) return null;
    return {
      schemaVersion: 1,
      sessionId: resume.sessionId,
      pageMode,
      interactionMode: 'chat',
      createdAt: typeof record.savedAt === 'number' && Number.isFinite(record.savedAt)
        ? record.savedAt : Date.now(),
    };
  } catch {
    return null;
  }
}

export function selectSessionHubRecoveryCandidate(
  pageMode: SessionHubPageMode,
  storage: SessionHubBookmarkStorage | undefined = browserStorage(),
): SessionHubRecoveryCandidate | null {
  const current = readSessionHubBookmark(pageMode, storage);
  if (current) return { bookmark: current, commitMigration: () => undefined };
  if (!storage) return null;
  const legacy = parseLegacyAgent(pageMode, storage) ?? parseLegacyChat(pageMode, storage);
  if (!legacy) return null;
  const legacyStorageKey = legacy.interactionMode === 'agent'
    ? legacyAgentKey(pageMode) : legacyChatKey(pageMode);
  return {
    bookmark: legacy,
    legacyStorageKey,
    commitMigration(target = storage) {
      writeSessionHubBookmark(legacy, target);
      target?.removeItem(legacyStorageKey);
    },
  };
}

export interface SessionHubRecoveryAttachOptions {
  handlers: SessionHubPayloadHandlers;
  onBusy(busy: boolean): void;
  /** Publish terminal/retry state before retiring this attachment token. */
  onSettled?(result: SessionHubReadResult): void;
}

export type SessionHubRecoveryConnect = (
  controller: AbortController,
  handlers: SessionHubPayloadHandlers,
) => Promise<SessionHubReadResult>;

/** One page-local attachment owner shared by Chat and Agent projection adapters. */
export class SessionHubRecoveryCoordinator {
  private readonly registry = new SessionAttachmentRegistry();
  private currentController: AbortController | null = null;
  private currentPromise: Promise<SessionHubReadResult> | null = null;
  private currentOnBusy: ((busy: boolean) => void) | null = null;

  attach(
    bookmark: SessionHubBookmark,
    connect: SessionHubRecoveryConnect,
    options: SessionHubRecoveryAttachOptions,
  ): Promise<SessionHubReadResult> {
    const current = this.registry.getCurrent();
    if (current?.sessionId === bookmark.sessionId && this.currentPromise !== null) {
      return this.currentPromise;
    }
    this.detach();
    const attachment = this.registry.attach(bookmark.sessionId);
    const controller = new AbortController();
    this.currentController = controller;
    this.currentOnBusy = options.onBusy;
    options.onBusy(true);
    const isCurrent = () => this.registry.isCurrent(attachment);
    const guarded: SessionHubPayloadHandlers = {
      onPayload: async (payload, context) => {
        if (isCurrent()) await options.handlers.onPayload?.(payload, context);
      },
      onRequestAccepted: async (payload) => {
        if (isCurrent()) await options.handlers.onRequestAccepted?.(payload);
      },
      onKnownRejection: async (rejection) => {
        if (isCurrent()) await options.handlers.onKnownRejection?.(rejection);
      },
    };
    let connection: Promise<SessionHubReadResult>;
    try {
      connection = connect(controller, guarded);
    } catch (error) {
      connection = Promise.reject(error);
    }
    const promise = connection
      .catch((error): SessionHubReadResult => ({
        kind: 'detached',
        lastSequence: 0,
        error: error instanceof Error ? error : new Error('Session Hub recovery failed'),
      }))
      .then((result) => {
        if (!isCurrent()) return result;
        options.onSettled?.(result);
        // Settlement may synchronously attach a replacement subscriber.
        if (!isCurrent()) return result;
        options.onBusy(false);
        if (result.kind !== 'terminal') this.registry.detach(attachment);
        if (this.currentController === controller) {
          this.currentController = null;
          this.currentOnBusy = null;
        }
        return result;
      })
      .finally(() => {
        if (this.currentPromise === promise) this.currentPromise = null;
      });
    this.currentPromise = promise;
    return promise;
  }

  /** Snapshot the permanent attachment token for guarding async completion. */
  currentAttachmentToken(): SessionAttachmentToken | null {
    return this.registry.getCurrent();
  }

  isCurrentAttachment(token: SessionAttachmentToken): boolean {
    return this.registry.isCurrent(token);
  }

  /** Explicitly detach only the current browser subscriber; never Stop the producer. */
  detach(): boolean {
    const current = this.registry.getCurrent();
    if (!current) return false;
    const detached = this.registry.detach(current);
    if (!detached) return false;
    this.currentController?.abort(new DOMException('Session Hub subscriber detached', 'AbortError'));
    this.currentController = null;
    this.currentPromise = null;
    this.currentOnBusy?.(false);
    this.currentOnBusy = null;
    return true;
  }
}
