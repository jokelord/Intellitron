import {
  appendAssistantDelta,
  createAssistantReasoningState,
  finalizeAssistantReasoning,
} from './assistant-reasoning.ts';
import type { Message } from './message-types.ts';
import type { SessionHubPayload } from './session-hub-client.ts';
import { projectTimelineEvent, type TimelineEvent } from './timeline-projection.ts';
import { applyRetrievedLocalMemories } from './retrieved-local-memory.ts';
import { isChatHistoryStartRef } from './local-memory.ts';
import { applyCompactProjection } from './compact-projection.ts';

export interface ChatSessionHandlersOptions {
  initialMessages?: Message[];
  defaultReasoningCollapsed?: boolean;
  now?: () => Date;
  onMessagesChanged?: (messages: readonly Message[]) => void;
}

export interface ChatSessionHandlers {
  onPayload(payload: SessionHubPayload): void | Promise<void>;
  getMessages(): readonly Message[];
}

type ChatTimelineEvent = TimelineEvent & {
  session_id?: string;
  turn_index?: number;
  user_content?: string;
  images?: unknown[];
  historical_image_placeholder?: string;
};

function userContent(
  text: string,
  images: string[],
  historicalImagePlaceholder?: string,
): Message['content'] {
  if (images.length === 0 && historicalImagePlaceholder === undefined) return text;
  return [
    ...images.map((url) => ({ type: 'image_url', image_url: { url } })),
    { type: 'text', text },
    ...(historicalImagePlaceholder === undefined
      ? []
      : [{ type: 'text', text: historicalImagePlaceholder }]),
  ];
}

export interface ChatLocalMemoryDisplayHandler {
  (event: TimelineEvent): void;
  getUserMessageId(): string;
}

/** Bind a live stream to captured IDs; transport identities never replace assistant UI ownership. */
export function createChatLocalMemoryDisplayHandler(options: {
  sessionId: string;
  userMessageId: string;
  assistantMessageId?: string;
  setMessages: (update: (previous: Message[]) => Message[]) => void;
}): ChatLocalMemoryDisplayHandler {
  let turnIndex: number | undefined;
  const autoCompacts = new Set<string>();
  const userId = () => turnIndex === undefined ? options.userMessageId : `chat:${options.sessionId}:${turnIndex}:user`;
  const handler = (event: TimelineEvent) => {
    if (event.kind === 'turn_started') {
      if (event.session_id !== options.sessionId || !Number.isSafeInteger(event.turn_index)
        || (event.turn_index ?? 0) < 1 || (turnIndex !== undefined && turnIndex !== event.turn_index)) return;
      turnIndex = event.turn_index;
      const canonicalUserId = userId();
      const assistantRef = `chat:${options.sessionId}:${turnIndex}:assistant`;
      const anchor = isChatHistoryStartRef(event.history_start_ref, options.sessionId) ? event.history_start_ref : undefined;
      options.setMessages((previous) => {
        let changed = false;
        const next = previous.map((message) => {
          if (message.role === 'user' && (message.id === options.userMessageId || message.id === canonicalUserId)) {
            if (message.id === canonicalUserId && message.chatTurnRef === canonicalUserId
              && (anchor === undefined || message.chatHistoryStartRef === anchor)) return message;
            changed = true;
            return { ...message, id: canonicalUserId, chatTurnRef: canonicalUserId,
              ...(anchor === undefined ? {} : { chatHistoryStartRef: anchor }) };
          }
          if (message.role === 'assistant' && message.id === options.assistantMessageId && message.chatMessageRef !== assistantRef) {
            changed = true;
            return { ...message, chatMessageRef: assistantRef };
          }
          return message;
        });
        return changed ? next : previous;
      });
      return;
    }
    const projection = projectTimelineEvent(event);
    if (projection.kind === 'compact_started' && projection.trigger === 'auto') autoCompacts.add(projection.summaryMessageId);
    if (projection.kind === 'compact_failed') autoCompacts.delete(projection.summaryMessageId);
    if (projection.kind === 'compact_completed' && autoCompacts.delete(projection.summaryMessageId) && turnIndex !== undefined) {
      options.setMessages((previous) => previous.map((message) => message.id === userId() && message.role === 'user'
        ? { ...message, chatHistoryStartRef: userId() } : message));
    }
    if (projection.kind !== 'local_memory_retrieved' || projection.sessionId !== options.sessionId
      || projection.turnIndex !== turnIndex) return;
    options.setMessages((previous) => applyRetrievedLocalMemories(previous, projection, userId()));
  };
  return Object.assign(handler, { getUserMessageId: userId });
}

export function createChatSessionHandlers(
  options: ChatSessionHandlersOptions = {},
): ChatSessionHandlers {
  let messages = [...(options.initialMessages ?? [])];
  const appliedSequences = new Set<number>();
  const now = options.now ?? (() => new Date());
  const defaultReasoningCollapsed = options.defaultReasoningCollapsed ?? true;
  let activeAssistantId: string | undefined;
  let activeUserId: string | undefined;
  const compactMarkedRef = { current: new Set<string>() };
  const autoCompactUsers = new Map<string, string>();

  const publish = (next: Message[]) => {
    messages = next;
    options.onMessagesChanged?.(messages);
  };

  const onTurnStarted = (event: ChatTimelineEvent) => {
    if (typeof event.session_id !== 'string' || !event.session_id.trim()
      || !Number.isSafeInteger(event.turn_index) || (event.turn_index ?? 0) < 1) return;
    const turnIndex = event.turn_index as number;
    const assistantId = typeof event.message_id === 'string'
      ? event.message_id
      : `chat:${event.session_id}:${turnIndex}:assistant`;
    const userId = `chat:${event.session_id}:${turnIndex}:user`;
    const assistantRef = `chat:${event.session_id}:${turnIndex}:assistant`;
    const anchor = isChatHistoryStartRef(event.history_start_ref, event.session_id) ? event.history_start_ref : undefined;
    const images = Array.isArray(event.images)
      ? event.images.filter((value): value is string => typeof value === 'string')
      : [];
    const text = typeof event.user_content === 'string' ? event.user_content : '';
    const historicalImagePlaceholder = typeof event.historical_image_placeholder === 'string'
      ? event.historical_image_placeholder
      : undefined;
    const retired = messages.map((message) => (
      message.role === 'assistant' && message.isStreaming
        ? { ...message, isStreaming: false }
        : message
    ));
    const existingUser = retired.findIndex((message) => message.id === userId);
    const existingAssistant = retired.findIndex((message) => message.id === assistantId);
    let next = retired;
    if (existingUser < 0) {
      next = [...next, {
        id: userId,
        chatTurnRef: userId,
        ...(anchor === undefined ? {} : { chatHistoryStartRef: anchor }),
        role: 'user',
        content: userContent(text, images, historicalImagePlaceholder),
        images: images.length > 0 ? images : undefined,
        timestamp: now(),
      }];
    }
    if (existingUser >= 0 && (next[existingUser].chatTurnRef !== userId
      || (anchor !== undefined && next[existingUser].chatHistoryStartRef !== anchor))) {
      next = next.map((message, index) => index === existingUser ? { ...message, chatTurnRef: userId,
        ...(anchor === undefined ? {} : { chatHistoryStartRef: anchor }) } : message);
    }
    if (existingAssistant < 0) {
      next = [...next, {
        id: assistantId,
        chatMessageRef: assistantRef,
        role: 'assistant',
        timestamp: now(),
        isStreaming: true,
        ...createAssistantReasoningState({ defaultReasoningCollapsed }),
      }];
    }
    if (existingAssistant >= 0 && next[existingAssistant].chatMessageRef !== assistantRef) {
      next = next.map((message, index) => index === existingAssistant ? { ...message, chatMessageRef: assistantRef } : message);
    }
    activeUserId = userId;
    activeAssistantId = assistantId;
    publish(next);
  };

  const onTimelineEvent = (event: TimelineEvent) => {
    if (event.kind === 'turn_started') {
      onTurnStarted(event as ChatTimelineEvent);
      return;
    }
    const projection = projectTimelineEvent(event);
    if (projection.kind === 'local_memory_retrieved') {
      if (projection.turnIndex < 1) return;
      const next = applyRetrievedLocalMemories(messages, projection,
        `chat:${projection.sessionId}:${projection.turnIndex}:user`);
      if (next !== messages) publish(next);
      return;
    }
    if (projection.kind === 'compact_started' || projection.kind === 'compact_delta'
      || projection.kind === 'compact_completed' || projection.kind === 'compact_failed') {
      if (!projection.summaryMessageId) return;
      if (projection.kind === 'compact_started' && projection.trigger === 'auto' && activeUserId) {
        autoCompactUsers.set(projection.summaryMessageId, activeUserId);
      }
      applyCompactProjection(projection, (update) => publish(typeof update === 'function' ? update(messages) : update),
        compactMarkedRef, { insertBeforeMessageId: autoCompactUsers.get(projection.summaryMessageId) });
      if (projection.kind === 'compact_started') {
        publish(messages.map((message) => message.compactionBoundaryId === projection.summaryMessageId
          ? { ...message, timestamp: now() } : message));
      }
      if (projection.kind === 'compact_completed') {
        const currentUser = autoCompactUsers.get(projection.summaryMessageId);
        if (currentUser) publish(messages.map((message) => message.id === currentUser && message.role === 'user'
          ? { ...message, chatHistoryStartRef: currentUser } : message));
        autoCompactUsers.delete(projection.summaryMessageId);
      }
      if (projection.kind === 'compact_failed') autoCompactUsers.delete(projection.summaryMessageId);
      return;
    }
    if (projection.kind !== 'chat_chunk' || event.source !== 'chat') return;
    const messageId = projection.messageId ?? activeAssistantId;
    if (!messageId) return;
    publish(messages.map((message) => (
      message.id === messageId && message.role === 'assistant'
        ? appendAssistantDelta(message, {
            contentDelta: projection.reasoning ? undefined : projection.text,
            reasoningDelta: projection.reasoning ? projection.text : undefined,
            defaultReasoningCollapsed,
          })
        : message
    )));
  };

  const onDone = () => {
    if (!activeAssistantId) return;
    publish(messages.map((message) => (
      message.id === activeAssistantId && message.role === 'assistant'
        ? {
            ...finalizeAssistantReasoning(message, { defaultReasoningCollapsed }),
            isStreaming: false,
          }
        : message
    )));
  };

  return {
    async onPayload(payload) {
      if (appliedSequences.has(payload.event_sequence)) return;
      if (payload.type === 'timeline_event'
        && typeof payload.event === 'object'
        && payload.event !== null) {
        onTimelineEvent(payload.event as TimelineEvent);
      } else if (payload.type === 'done') {
        onDone();
      }
      appliedSequences.add(payload.event_sequence);
    },
    getMessages: () => messages,
  };
}
