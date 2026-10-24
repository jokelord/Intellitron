'use client';
import { useEffect, useRef, useState } from 'react';
import { createClientStepId, type ChatMessage } from '../api.ts';
import { generateId, type Message, type AgentRunState } from '../message-types.ts';
import { deferImperativeAgentSend } from '../agent-queue.ts';
import type { ConversationState } from './use-conversation-state.ts';
export interface ConversationDraft {
  text: string;
  images: string[];
}
export function snapshotConversationDraft(text: string, images: readonly string[] = []): ConversationDraft {
  return {
    text: text.trim(),
    images: [...images]
  };
}
export function createConversationTurnMessages(input: {
  userContent: ChatMessage['content'];
  images?: string[];
  replay?: Message['voiceReplay'];
  chatSessionId?: string;
  initialRouting: boolean;
  createId?: () => string;
  createStepId?: () => string;
  now?: () => Date;
}) {
  const id = input.createId ?? generateId, now = input.now ?? (() => new Date());
  const userMessage: Message = {
    id: id(),
    role: 'user',
    content: input.userContent,
    timestamp: now(),
    voiceReplay: input.replay,
    ...(input.images?.length ? {
      images: input.images
    } : {})
  };
  const assistantMessage: Message = {
    id: id(),
    role: 'assistant',
    content: '',
    timestamp: now(),
    isStreaming: true,
    chatResume: input.chatSessionId ? {
      sessionId: input.chatSessionId
    } : undefined,
    agentSteps: input.initialRouting ? [{
      id: input.createStepId?.() ?? createClientStepId('dispatch'),
      step_type: 'dispatch',
      dispatch_status: 'running',
      dispatch_stage: 'routing',
      dispatch_kind: 'routing',
      display_slot: 'before_body'
    }] : undefined
  };
  const agentRun: AgentRunState = {
    assistantMessageId: assistantMessage.id,
    projectedAssistantMessageIds: new Set(),
    coordinatorMessageId: undefined,
    executionAssistantMessageId: undefined,
    secondaryHandoffExecutionBubblePendingAgentSwitch: false,
    stopWasClicked: false,
    answerNowWasClicked: false
  };
  return {
    userMessage,
    assistantMessage,
    agentRun
  };
}
/** One append-only queue and one cancellable drain, with immutable per-turn images. */
export function useConversationSend(options: {
  state: ConversationState;
  busy: boolean;
  isBusyNow(): boolean;
  sendQueued(draft: ConversationDraft): void | Promise<void>;
  restoreImages?(images: string[]): void;
}) {
  const latest = useRef(options);
  latest.current = options;
  const [messageQueue, setMessageQueue] = useState<ConversationDraft[]>([]);
  useEffect(() => {
    if (options.busy || messageQueue.length === 0)
      return;
    const draft = messageQueue[0];
    const timer = window.setTimeout(() => {
      const current = latest.current;
      if (current.isBusyNow())
        return;
      setMessageQueue(previous => previous[0] === draft ? previous.slice(1) : previous);
      current.state.setInput(draft.text);
      current.restoreImages?.([...draft.images]);
      void current.sendQueued(draft);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [options.busy, messageQueue]);
  const enqueueIfBusy = (text: string, images: readonly string[] = []) => {
    const current = latest.current;
    const queued = deferImperativeAgentSend(current.isBusyNow, () => {
      setMessageQueue(previous => [...previous, snapshotConversationDraft(text, images)]);
      current.state.setInput('');
      current.restoreImages?.([]);
    });
    if (!queued) current.state.recoveryCoordinatorRef.current.detach();
    return queued;
  };
  return {
    messageQueue,
    setMessageQueue,
    enqueueIfBusy
  };
}
