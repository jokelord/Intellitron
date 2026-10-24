import type { AgentStep, ChatMessage } from './api';
import { finalizeAssistantReasoning } from './assistant-reasoning.ts';

export type AnswerNowUiMessage = ChatMessage & {
  id: string;
  timestamp: Date;
  isStreaming?: boolean;
  images?: string[];
  agentSteps?: AgentStep[];
  reasoning_content?: string;
  reasoningDetected?: boolean;
};

export type StreamingTerminalBehavior = {
  suppressErrorMessage: boolean;
  resetStreamingState: boolean;
  resetStoppedState: boolean;
  clearAnswerNowFlag: boolean;
};

export type AgentInterruptionFlags = {
  stopWasClicked: boolean;
  answerNowWasClicked: boolean;
};

/** Capture interruption intent before any asynchronous lifecycle callback can clear the refs. */
export function captureAgentInterruptionIntent(flags: AgentInterruptionFlags): boolean {
  return flags.stopWasClicked || flags.answerNowWasClicked;
}

export type AnswerNowButtonState = {
  isLoading: boolean;
  isAnswerNowLoading: boolean;
  agentMode: boolean;
  agentSessionId?: string | null;
};

function hasVisibleAssistantContent(message: Pick<AnswerNowUiMessage, 'content' | 'agentSteps' | 'reasoning_content' | 'reasoningDetected'>): boolean {
  if (typeof message.content === 'string' && message.content.trim().length > 0) {
    return true;
  }

  if (
    message.reasoningDetected
    || (typeof message.reasoning_content === 'string' && message.reasoning_content.trim().length > 0)
  ) {
    return true;
  }

  return (message.agentSteps?.length ?? 0) > 0;
}

function finalizeAssistantMessage<T extends AnswerNowUiMessage>(message: T): T {
  const finalizedMessage = finalizeAssistantReasoning(message, {
    defaultReasoningCollapsed: false,
  });
  const steps = message.agentSteps || [];
  const finalizedSteps = steps.length > 0 && steps[steps.length - 1].partial
    ? [
      ...steps.slice(0, -1),
      { ...steps[steps.length - 1], partial: false },
    ]
    : steps;

  return {
    ...finalizedMessage,
    isStreaming: false,
    agentSteps: finalizedSteps,
  };
}

export function updateAnswerNowMessage<T extends AnswerNowUiMessage>(
  messages: T[],
  messageId: string | null | undefined,
  updater: (message: T) => T,
): T[] {
  if (!messageId) {
    return messages;
  }

  const targetIndex = messages.findIndex((message) => message.id === messageId && message.role === 'assistant');
  if (targetIndex === -1) {
    return messages;
  }

  const updated = [...messages];
  updated[targetIndex] = updater({ ...updated[targetIndex] });
  return updated;
}

export function finalizeAnswerNowMessage<T extends AnswerNowUiMessage>(
  messages: T[],
  messageId: string | null | undefined,
): T[] {
  return updateAnswerNowMessage(messages, messageId, finalizeAssistantMessage);
}

export function finalizeStreamingAssistantMessage<T extends AnswerNowUiMessage>(messages: T[]): T[] {
  if (messages.length === 0) {
    return messages;
  }

  const updated = [...messages];
  const targetIndex = updated.map((message, index) => ({ message, index }))
    .filter(({ message }) => message.role === 'assistant' && message.isStreaming)
    .at(-1)?.index;

  if (targetIndex === undefined) {
    return updated;
  }

  updated[targetIndex] = finalizeAssistantMessage(updated[targetIndex]);
  return updated;
}

export function applyAnswerNowReply<T extends AnswerNowUiMessage>(messages: T[], finalReply: T): T[] {
  const finalized = finalizeStreamingAssistantMessage(messages);
  const updated = [...finalized];
  const lastMessage = updated[updated.length - 1];

  if (
    lastMessage &&
    lastMessage.role === 'assistant' &&
    !hasVisibleAssistantContent(lastMessage)
  ) {
    updated.pop();
  }

  updated.push(finalReply);
  return updated;
}

export function getAssistantRenderableText(
  message: Pick<AnswerNowUiMessage, 'content' | 'isStreaming' | 'agentSteps' | 'reasoning_content' | 'reasoningDetected'>,
  isStopped: boolean,
): string {
  if (typeof message.content === 'string' && message.content.trim().length > 0) {
    return isStopped ? `${message.content} [已停止]` : message.content;
  }

  if (
    message.reasoningDetected
    || (typeof message.reasoning_content === 'string' && message.reasoning_content.length > 0)
  ) {
    return isStopped ? '已停止' : '';
  }

  if ((message.agentSteps?.length ?? 0) > 0) {
    if (message.isStreaming) {
      return '';
    }
    return isStopped ? '已停止' : '';
  }

  return isStopped ? '已停止' : '思考中...';
}

export function getStreamingTerminalBehavior(flags: {
  stopWasClicked: boolean;
  answerNowWasClicked: boolean;
}): StreamingTerminalBehavior {
  if (flags.answerNowWasClicked && !flags.stopWasClicked) {
    return {
      suppressErrorMessage: true,
      resetStreamingState: false,
      resetStoppedState: false,
      clearAnswerNowFlag: false,
    };
  }

  return {
    suppressErrorMessage: flags.stopWasClicked,
    resetStreamingState: true,
    resetStoppedState: !flags.stopWasClicked,
    clearAnswerNowFlag: true,
  };
}

export function createAnswerNowStreamingMessage<T extends AnswerNowUiMessage>(
  id: string,
  timestamp: Date,
): T {
  return {
    id,
    role: 'assistant',
    content: '',
    timestamp,
    isStreaming: true,
  } as T;
}

export function appendAnswerNowChunk<T extends AnswerNowUiMessage>(messages: T[], chunk: string): T[] {
  if (!chunk) {
    return messages;
  }

  const updated = [...messages];
  const lastMessage = updated[updated.length - 1];
  if (!lastMessage || lastMessage.role !== 'assistant') {
    return updated;
  }

  updated[updated.length - 1] = {
    ...lastMessage,
    content: `${typeof lastMessage.content === 'string' ? lastMessage.content : ''}${chunk}`,
  };
  return updated;
}

export function appendAnswerNowChunkById<T extends AnswerNowUiMessage>(
  messages: T[],
  messageId: string | null | undefined,
  chunk: string,
): T[] {
  if (!chunk) {
    return messages;
  }

  return updateAnswerNowMessage(messages, messageId, (message) => ({
    ...message,
    content: `${typeof message.content === 'string' ? message.content : ''}${chunk}`,
  }));
}

export function shouldDisableAnswerNowButton(state: AnswerNowButtonState): boolean {
  return !state.agentMode
    || !state.isLoading
    || !state.agentSessionId
    || state.isAnswerNowLoading;
}

export function shouldShowAnswerNowStreamError(stopWasClicked: boolean): boolean {
  return !stopWasClicked;
}
