class ConversationStopReason extends DOMException {
  constructor() {
    super('Conversation stopped by user', 'AbortError');
  }
}

/** Mark only a transport actually closed by an explicit Stop, preserving prior timeout reasons. */
export function abortStoppedConversation(controller: AbortController | null): void {
  if (controller && !controller.signal.aborted)
    controller.abort(new ConversationStopReason());
}

export function wasConversationStopped(controller: AbortController): boolean {
  return controller.signal.reason instanceof ConversationStopReason;
}
