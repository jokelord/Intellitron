export interface QueuedVisionMessage {
  text: string;
  images: string[];
}

/**
 * React state can lag a synchronous lifecycle update.  Imperative send paths
 * use a ref-backed getter so they queue instead of issuing a second POST.
 */
export function deferImperativeAgentSend(
  isBusyNow: () => boolean,
  enqueue: () => void,
): boolean {
  if (!isBusyNow()) return false;
  enqueue();
  return true;
}

export function enqueueVisionMessage(
  queue: QueuedVisionMessage[],
  text: string,
  images: string[],
): QueuedVisionMessage[] {
  const trimmedText = text.trim();
  if (!trimmedText && images.length === 0) {
    return queue;
  }

  return [
    ...queue,
    {
      text: trimmedText,
      images: [...images],
    },
  ];
}

export function dequeueVisionMessage(queue: QueuedVisionMessage[]): {
  next: QueuedVisionMessage | null;
  remaining: QueuedVisionMessage[];
} {
  if (queue.length === 0) {
    return { next: null, remaining: [] };
  }

  return {
    next: queue[0],
    remaining: queue.slice(1),
  };
}
