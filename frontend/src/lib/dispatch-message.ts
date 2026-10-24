import type { AgentStep, ChatMessage } from './api';
import {
  appendAssistantDelta,
  createAssistantReasoningState,
  finalizeAssistantReasoning,
  type AssistantReasoningFields,
} from './assistant-reasoning.ts';

type AssistantLikeMessage = AssistantReasoningFields & {
  id: string;
  role: string;
  content?: ChatMessage['content'];
  timestamp?: Date;
  isStreaming?: boolean;
  agentSteps?: AgentStep[];
};

type ProjectedChatChunk = {
  messageId?: string;
  text: string;
  partial: boolean;
  reasoning?: boolean;
};

type ProjectedChatChunkOptions = {
  defaultReasoningCollapsed: boolean;
};

export const ABORTED_DISPATCH_MESSAGE = '任务调度已中止';

export type DispatchTerminalFlags = {
  stopWasClicked: boolean;
  answerNowWasClicked: boolean;
  errorMessage: string;
};

type DispatchCopy = {
  statusText: string;
  subText: string;
};

type TailRetractionResult = {
  text: string;
  retracted: boolean;
};

function extractTrailingBalancedJsonSpan(text: string): { start: number; end: number } | null {
  if (!text) {
    return null;
  }

  let index = text.length - 1;
  while (index >= 0 && /\s/.test(text[index])) {
    index -= 1;
  }
  if (index < 0) {
    return null;
  }

  const closingChar = text[index];
  const openingChar = closingChar === ']' ? '[' : closingChar === '}' ? '{' : null;
  if (!openingChar) {
    return null;
  }

  let depth = 0;
  let inString = false;
  let escapeNext = false;

  for (let cursor = index; cursor >= 0; cursor -= 1) {
    const char = text[cursor];

    if (escapeNext) {
      escapeNext = false;
      continue;
    }

    if (char === '\\' && inString) {
      escapeNext = true;
      continue;
    }

    if (char === '"') {
      inString = !inString;
      continue;
    }

    if (inString) {
      continue;
    }

    if (char === closingChar) {
      depth += 1;
      continue;
    }

    if (char === openingChar) {
      depth -= 1;
      if (depth === 0) {
        return { start: cursor, end: index + 1 };
      }
    }
  }

  return null;
}

export function stripTrailingCanonicalSwitchAgentControl(text: string): TailRetractionResult {
  const trimmedEnd = text.trimEnd();
  const trailingWhitespace = text.slice(trimmedEnd.length);
  const span = extractTrailingBalancedJsonSpan(trimmedEnd);

  if (!span) {
    return { text, retracted: false };
  }

  const candidate = trimmedEnd.slice(span.start, span.end);
  const compactCandidate = candidate.replace(/\s+/g, '');
  if (!compactCandidate.includes('"type":"agent"') || !compactCandidate.includes('switch_agent')) {
    return { text, retracted: false };
  }

  return {
    text: `${trimmedEnd.slice(0, span.start)}${trailingWhitespace}`,
    retracted: true,
  };
}

export function getDispatchMarqueeSegments(
  message: string,
  isRunning: boolean,
): string[] {
  return isRunning ? [message, message] : [message];
}

export function getDispatchCardCopy(step: AgentStep): DispatchCopy {
  const isError = step.dispatch_status === 'error';
  const isAborted = step.dispatch_status === 'aborted';
  const isCompleted = step.dispatch_status === 'completed';
  const dispatchKind = step.dispatch_kind || 'routing';

  if (isError) {
    return {
      statusText: step.dispatch_message || '任务调度失败',
      subText: '系统未能完成当前请求的执行路径判定',
    };
  }

  if (isAborted) {
    if (dispatchKind === 'secondary_handoff') {
      return {
        statusText: '执行代理转交已中止',
        subText: '当前请求已停止，未继续进入后续执行阶段',
      };
    }
    return {
      statusText: '任务调度已中止',
      subText: '当前请求已停止，未继续进入后续执行阶段',
    };
  }

  if (dispatchKind === 'secondary_handoff') {
    if (isError) {
      return {
        statusText: step.dispatch_message || '执行代理转交失败',
        subText: '系统未能完成当前请求的执行代理转交',
      };
    }
    if (isCompleted) {
      return {
        statusText: '已转交执行代理',
        subText: `系统已将任务转交给 ${step.dispatch_target_agent || '执行代理'} 继续处理`,
      };
    }
    return {
      statusText: '正在转交执行代理',
      subText: '系统正在将当前任务转交给后续执行代理继续处理',
    };
  }

  if (step.dispatch_stage === 'handoff') {
    return {
      statusText: '已分配执行代理',
      subText: `系统已将任务转交给 ${step.dispatch_target_agent || '执行代理'}`,
    };
  }

  if (step.dispatch_stage === 'direct_answer') {
    return {
      statusText: '已完成响应路径判定',
      subText: '系统已确认当前请求可直接完成响应',
    };
  }

  if (isCompleted) {
    return {
      statusText: '已完成执行路径判定',
      subText: '系统已完成当前请求的执行路径判定',
    };
  }

  return {
    statusText: '正在分配执行代理',
    subText: '系统正在分析任务是否需要浏览器、文件读写或命令执行能力',
  };
}

export function getDispatchCardLiveText(step: AgentStep): string {
  const { subText } = getDispatchCardCopy(step);

  if (step.dispatch_status === 'running' || step.dispatch_status === 'error' || step.dispatch_status === 'aborted') {
    return step.dispatch_message || subText;
  }

  return subText;
}

export function mergeDispatchStep(
  previousStep: AgentStep,
  nextStep: AgentStep,
): AgentStep {
  const mergedDispatchMessage = nextStep.dispatch_message ?? previousStep.dispatch_message;

  return {
    ...previousStep,
    ...nextStep,
    id: previousStep.id ?? nextStep.id,
    dispatch_id: previousStep.dispatch_id ?? nextStep.dispatch_id,
    dispatch_message: mergedDispatchMessage,
    dispatch_kind: nextStep.dispatch_kind ?? previousStep.dispatch_kind,
    display_slot: nextStep.display_slot ?? previousStep.display_slot,
  };
}

export function completeRunningDispatchStep(
  step: AgentStep | null | undefined,
): AgentStep | null | undefined {
  if (step?.step_type !== 'dispatch' || step.dispatch_status !== 'running') {
    return step;
  }

  return {
    ...step,
    dispatch_status: 'completed',
  };
}

export function finalizeRunningDispatchStep(
  step: AgentStep | null | undefined,
  flags: DispatchTerminalFlags,
): AgentStep | null | undefined {
  if (step?.step_type !== 'dispatch' || step.dispatch_status !== 'running') {
    return step;
  }

  if (flags.stopWasClicked) {
    return {
      ...step,
      dispatch_status: 'aborted',
      dispatch_message: ABORTED_DISPATCH_MESSAGE,
    };
  }

  if (flags.answerNowWasClicked) {
    return completeRunningDispatchStep(step);
  }

  return {
    ...step,
    dispatch_status: 'error',
    dispatch_message: flags.errorMessage,
  };
}

export function updateTrackedAssistantMessage<T extends AssistantLikeMessage>(
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

export function retractTrackedAssistantTailText<T extends AssistantLikeMessage>(
  messages: T[],
  messageId: string | null | undefined,
  retractor: (text: string) => TailRetractionResult,
): T[] {
  return updateTrackedAssistantMessage(messages, messageId, (message) => {
    if (typeof message.content !== 'string') {
      return message;
    }

    const result = retractor(message.content);
    if (!result.retracted) {
      return message;
    }

    return {
      ...message,
      content: result.text,
    };
  });
}

export function finalizeTrackedAssistantMessage<T extends AssistantLikeMessage>(
  messages: T[],
  messageId: string | null | undefined,
): T[] {
  return updateTrackedAssistantMessage(messages, messageId, (message) => {
    const finalizedMessage = finalizeAssistantReasoning(message, {
      defaultReasoningCollapsed: message.reasoningCollapsed ?? false,
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
  });
}

export function collectTrackedAssistantMessageIds(
  assistantMessageId: string | null | undefined,
  projectedAssistantMessageIds?: Iterable<string>,
  executionAssistantMessageId?: string | null | undefined,
): string[] {
  const messageIds = assistantMessageId ? [assistantMessageId] : [];

  if (!projectedAssistantMessageIds) {
    if (executionAssistantMessageId && !messageIds.includes(executionAssistantMessageId)) {
      messageIds.push(executionAssistantMessageId);
    }
    return messageIds;
  }

  for (const projectedId of Array.from(projectedAssistantMessageIds)) {
    if (!messageIds.includes(projectedId)) {
      messageIds.push(projectedId);
    }
  }

  if (executionAssistantMessageId && !messageIds.includes(executionAssistantMessageId)) {
    messageIds.push(executionAssistantMessageId);
  }

  return messageIds;
}

export function getActiveTrackedAssistantMessageId(
  assistantMessageId: string | null | undefined,
  projectedAssistantMessageIds?: Iterable<string>,
  executionAssistantMessageId?: string | null | undefined,
): string | null | undefined {
  if (executionAssistantMessageId) {
    return executionAssistantMessageId;
  }

  if (!projectedAssistantMessageIds) {
    return assistantMessageId;
  }

  const projectedIds = Array.from(projectedAssistantMessageIds);
  return projectedIds.length > 0 ? projectedIds[projectedIds.length - 1] : assistantMessageId;
}

export function getContentTargetAssistantMessageId(
  assistantMessageId: string | null | undefined,
  projectedAssistantMessageIds?: Iterable<string>,
  executionAssistantMessageId?: string | null | undefined,
): string | null | undefined {
  if (executionAssistantMessageId) {
    return executionAssistantMessageId;
  }

  if (!projectedAssistantMessageIds) {
    return assistantMessageId;
  }

  const projectedIds = Array.from(projectedAssistantMessageIds);
  if (projectedIds.length > 0) {
    return null;
  }

  return assistantMessageId;
}

export function finalizeTrackedAssistantMessages<T extends AssistantLikeMessage>(
  messages: T[],
  messageIds: Iterable<string | null | undefined>,
): T[] {
  let next = messages;
  const trackedMessageIds = Array.from(messageIds);

  for (let index = 0; index < trackedMessageIds.length; index += 1) {
    next = finalizeTrackedAssistantMessage(next, trackedMessageIds[index]);
  }

  return next;
}

export function appendProjectedChatChunk<T extends AssistantLikeMessage>(
  messages: T[],
  chunk: ProjectedChatChunk,
  createTimestamp: () => Date,
  updateExisting?: (message: T) => T,
  options?: ProjectedChatChunkOptions,
): T[] {
  const defaultReasoningCollapsed = options?.defaultReasoningCollapsed ?? false;

  const appendChunkToMessage = (message: T): T => {
    const currentMessage = updateExisting ? updateExisting({ ...message }) : { ...message };

    return {
      ...appendAssistantDelta(
        currentMessage,
        chunk.reasoning
          ? {
              reasoningDelta: chunk.text,
              defaultReasoningCollapsed,
            }
          : {
              contentDelta: chunk.text,
              defaultReasoningCollapsed,
            },
      ),
      isStreaming: chunk.partial,
    };
  };

  if (chunk.messageId) {
    const targetIndex = messages.findIndex((message) => message.id === chunk.messageId && message.role === 'assistant');

    if (targetIndex !== -1) {
      const updated = [...messages];
      updated[targetIndex] = appendChunkToMessage(updated[targetIndex]);
      return updated;
    }

    const baseMessage = createAssistantReasoningState({
      defaultReasoningCollapsed,
    });

    return [
      ...messages,
      appendChunkToMessage({
        ...baseMessage,
        id: chunk.messageId,
        role: 'assistant',
        timestamp: createTimestamp(),
      } as T),
    ];
  }

  return messages;
}
