import type { Message } from './message-types.ts';
import type { TimelineProjection } from './timeline-projection.ts';

export interface RetrievedLocalMemoryChunk { text: string; }
export type RetrievedLocalMemoryProjection = Extract<TimelineProjection, { kind: 'local_memory_retrieved' }>;

/** Display-only replacement; never searches for the latest user or edits model content. */
export function applyRetrievedLocalMemories(
  messages: Message[],
  projection: RetrievedLocalMemoryProjection,
  boundUserMessageId?: string,
): Message[] {
  const targetId = boundUserMessageId ?? (projection.turnIndex > 0
    ? `${projection.sessionId}-turn-${projection.turnIndex}-user`
    : undefined);
  if (!targetId) return messages;
  const index = messages.findIndex((message) => message.id === targetId && message.role === 'user');
  if (index < 0) return messages;
  const previous = messages[index].localMemories;
  if (previous?.length === projection.chunks.length
    && previous.every((chunk, ordinal) => chunk.text === projection.chunks[ordinal].text)
    && (projection.historyStartRef === undefined || messages[index].chatHistoryStartRef === projection.historyStartRef)) return messages;
  const next = [...messages];
  next[index] = { ...messages[index], localMemories: projection.chunks.map(({ text }) => ({ text })),
    ...(messages[index].chatTurnRef && projection.historyStartRef ? { chatHistoryStartRef: projection.historyStartRef } : {}) };
  return next;
}
