/** Freeze eligible preference at the first POST, retaining it for the same logical retry. */
export function freezeLocalMemoryPreference(
  requests: Map<string, boolean>,
  requestId: string,
  enabled: boolean,
  hasUserText: boolean = true,
): boolean {
  if (!requests.has(requestId)) requests.set(requestId, enabled === true && hasUserText);
  return requests.get(requestId)!;
}

/** A transport identity is qualified by the exact Chat session and ordinary role. */
export function isChatHistoryStartRef(value: unknown, sessionId: string): value is string {
  if (typeof value !== 'string') return false;
  const prefix = `chat:${sessionId}:`;
  if (!value.startsWith(prefix)) return false;
  const tail = value.slice(prefix.length);
  if (/^[1-9][0-9]*:(user|assistant)$/.test(tail)) return true;
  const bootstrap = /^bootstrap:([^:]+):(user|assistant)$/.exec(tail);
  if (!bootstrap) return false;
  try { return encodeURIComponent(decodeURIComponent(bootstrap[1])) === bootstrap[1]; } catch { return false; }
}

export function getChatMessageTransportRef(message: import('./message-types.ts').Message, sessionId: string): string | undefined {
  if (message.role !== 'user' && message.role !== 'assistant') return undefined;
  for (const value of [message.role === 'user' ? message.chatTurnRef : undefined, message.chatMessageRef, message.id]) {
    if (isChatHistoryStartRef(value, sessionId) && value.endsWith(`:${message.role}`)) return value;
  }
  try { return `chat:${sessionId}:bootstrap:${encodeURIComponent(message.id)}:${message.role}`; } catch { return undefined; }
}

/** Keep only the submitted active window; never restore older UI rows on first enable. */
export function selectChatLocalMemoryHistoryWindow(
  messages: readonly import('./message-types.ts').Message[],
  sessionId: string | undefined,
  enabled: boolean,
): { messages: import('./message-types.ts').Message[]; preserveFullHistory: boolean; historyStartRef?: string } {
  const summary = messages.findLast((message) => message.bubbleKind === 'compact_summary' && message.compactStatus === 'completed');
  const eligible = sessionId ? messages.filter((message) => message.role === 'user' && !message.isPreCompact
    && isChatHistoryStartRef(message.chatTurnRef, sessionId)
    && isChatHistoryStartRef(message.chatHistoryStartRef, sessionId)) : [];
  const pinned = eligible.findLast((message) => Array.isArray(message.localMemories) && message.localMemories.length > 0);
  const owner = pinned ?? (enabled === true ? eligible.at(-1) : undefined);
  const index = owner && sessionId ? messages.findIndex((message) => !message.isPreCompact
    && getChatMessageTransportRef(message, sessionId) === owner.chatHistoryStartRef) : -1;
  if (index >= 0) {
    const suffix = messages.slice(index).filter((message) => !message.isPreCompact && (message.bubbleKind !== 'compact_summary' || message === summary));
    return { messages: summary && !suffix.includes(summary) ? [summary, ...suffix] : suffix, preserveFullHistory: true, historyStartRef: owner?.chatHistoryStartRef };
  }
  if (owner?.chatHistoryStartRef?.startsWith(`chat:${sessionId}:bootstrap:`)) {
    // Hub replay can reference an exact server-owned bootstrap prefix without republishing its UI rows/images.
    const suffix = messages.filter((message) => message.bubbleKind === 'compact_summary' ? message === summary
      : sessionId !== undefined && isChatHistoryStartRef(message.chatTurnRef ?? message.chatMessageRef ?? message.id, sessionId));
    return { messages: suffix, preserveFullHistory: true, historyStartRef: owner.chatHistoryStartRef };
  }
  // The existing legacy window remains the initializer when no accepted anchor exists.
  // A successful compact summary is a separate, legitimate prefix boundary.
  if (summary) {
    const suffix = messages.filter((message) => !message.isPreCompact && message.bubbleKind !== 'compact_summary').slice(-20);
    return { messages: [summary, ...suffix], preserveFullHistory: true };
  }
  return { messages: [...messages], preserveFullHistory: false };
}

export function shouldPreserveChatLocalMemoryHistory(
  messages: readonly import('./message-types.ts').Message[], sessionId: string | undefined, enabled: boolean,
): boolean {
  return selectChatLocalMemoryHistoryWindow(messages, sessionId, enabled).preserveFullHistory;
}

/** Read the first actual ordinary history row, excluding current user and compact/system prefix. */
export function getChatRequestHistoryStartRef(
  messages: readonly import('./api.ts').ChatMessage[], sessionId: string, currentUserIsNew: boolean = true,
): string | undefined {
  const first = (currentUserIsNew ? messages.slice(0, -1) : messages).find((message) => (message.role === 'user' || message.role === 'assistant')
    && message.metadata?.pre_compact !== true && message.metadata?.compact_summary !== true);
  if (!first) return undefined;
  const value = first.role === 'user' ? first.metadata?.chat_turn_ref ?? first.metadata?.chat_message_ref : first.metadata?.chat_message_ref;
  return isChatHistoryStartRef(value, sessionId) && value.endsWith(`:${first.role}`) ? value : undefined;
}

export function freezeChatHistoryStartRef(
  requests: Map<string, string | undefined>, requestId: string, value: string | undefined,
): string | undefined {
  if (!requests.has(requestId)) requests.set(requestId, value);
  return requests.get(requestId);
}
