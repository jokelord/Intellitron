import type { Dispatch, SetStateAction } from 'react';
import { llmClient, type LLMClient, type ChatMessage, type AgentChatRequest, type RequestLifecycleCallbacks } from '../api.ts';
import type { Message, ChatSettings, AgentRunState } from '../message-types.ts';
import { buildChatWireMessages, buildAgentContextMessages } from '../message-types.ts';
import { freezeLocalMemoryPreference, selectChatLocalMemoryHistoryWindow, freezeChatHistoryStartRef, getChatRequestHistoryStartRef } from '../local-memory.ts';
import { createChatLocalMemoryDisplayHandler } from '../chat-session-handlers.ts';
import { appendAssistantDelta } from '../assistant-reasoning.ts';
import { projectTimelineEvent } from '../timeline-projection.ts';
import { applyCompactProjection } from '../compact-projection.ts';
import { createTextAgentSessionHandlers, type TextAgentSessionHandlerEnvironment } from '../text-agent-session-handlers.ts';
import { wasConversationStopped } from './conversation-stop.ts';
export interface PreparedConversationTurn {
  pageMode: 'text' | 'vision';
  requestId: string;
  controller: AbortController;
  interactionMode: 'chat' | 'agent';
  sessionId: string;
  historyMessages: readonly Message[];
  userContent: ChatMessage['content'];
  agentText: string;
  images?: string[];
  visionAutomation?: AgentChatRequest['vision_automation'];
  preserveFullHistory: boolean;
  hasUserText: boolean;
  userMessage: Message;
  assistantMessage: Message;
  agentRun: AgentRunState;
}
interface TransportMemoryOptions {
  turn: PreparedConversationTurn;
  settings: ChatSettings;
  maxContextLength: number;
  localMemoryRequestsRef: {
    current: Map<string, boolean>;
  };
  chatHistoryRequestsRef: {
    current: Map<string, string | undefined>;
  };
  localMemoryEnabled: boolean;
  isCurrent(): boolean;
}
export interface ChatConversationTransportOptions extends TransportMemoryOptions {
  runtimeContext: string | null;
  setMessages: Dispatch<SetStateAction<Message[]>>;
  newlyCompactMarkedRef: {
    current: Set<string>;
  };
  onDone(): void;
  onError(error: Error): void;
  lifecycle?: RequestLifecycleCallbacks;
  client?: Pick<LLMClient, 'chatStream'>;
}
function cleanupRequestBookkeeping(options: TransportMemoryOptions, requestId = options.turn.requestId) {
  options.localMemoryRequestsRef.current.delete(requestId);
  options.chatHistoryRequestsRef.current.delete(requestId);
}
function settlementLifecycle(options: TransportMemoryOptions, lifecycle?: RequestLifecycleCallbacks): RequestLifecycleCallbacks {
  return {
    ...lifecycle,
    onStableTerminal: async (result) => {
      cleanupRequestBookkeeping(options, result.requestId);
      await lifecycle?.onStableTerminal?.(result);
    },
    onDetached: async (error, context) => {
      // Vision unknown/admission retries retain their logical-request preferences.
      if (options.turn.pageMode === 'text')
        cleanupRequestBookkeeping(options);
      await lifecycle?.onDetached?.(error, context);
    },
  };
}
/** All ordinary Chat modes share the same wire builder and journal projection. */
export async function runChatConversationTransport(options: ChatConversationTransportOptions): Promise<void> {
  const { turn, settings } = options;
  const localMemory = freezeLocalMemoryPreference(options.localMemoryRequestsRef.current, turn.requestId, options.localMemoryEnabled, turn.hasUserText);
  const historyWindow = turn.preserveFullHistory ? {
    messages: [...turn.historyMessages],
    preserveFullHistory: true,
    historyStartRef: undefined
  }
    : selectChatLocalMemoryHistoryWindow([...turn.historyMessages], turn.sessionId, localMemory);
  const messages = buildChatWireMessages({
    messages: historyWindow.messages,
    chatSessionId: turn.sessionId,
    maxContextLength: options.maxContextLength,
    systemPrompt: settings.systemPrompt,
    runtimeContext: options.runtimeContext,
    currentUserContent: turn.userContent,
    preserveFullHistory: historyWindow.preserveFullHistory
  });
  const historyRef = freezeChatHistoryStartRef(options.chatHistoryRequestsRef.current, turn.requestId, historyWindow.historyStartRef ?? getChatRequestHistoryStartRef(messages, turn.sessionId));
  const projectMemory = createChatLocalMemoryDisplayHandler({
    sessionId: turn.sessionId,
    userMessageId: turn.userMessage.id,
    assistantMessageId: turn.assistantMessage.id,
    setMessages: options.setMessages
  });
  await (options.client ?? llmClient).chatStream(messages, {
    temperature: settings.temperature,
    top_p: settings.topP,
    top_k: settings.topK,
    repetition_penalty: settings.repetitionPenalty,
    max_tokens: settings.maxTokens,
    request_id: turn.requestId,
    enable_thinking: settings.enableThinking,
    enable_local_memory: localMemory,
    chat_history_start_ref: historyRef,
    ...(turn.visionAutomation ? {
      vision_automation: turn.visionAutomation
    } : {}),
    session_id: turn.sessionId,
  }, (content, reasoning) => {
    if (!options.isCurrent() || turn.controller.signal.aborted)
      return;
    options.setMessages(previous => {
      const next = [...previous], last = next[next.length - 1];
      if (last?.role === 'assistant')
        next[next.length - 1] = {
          ...last,
          ...appendAssistantDelta(last, {
            contentDelta: content,
            reasoningDelta: reasoning,
            defaultReasoningCollapsed: !settings.showReasoning
          })
        };
      return next;
    });
  }, () => {
    cleanupRequestBookkeeping(options);
    if (options.isCurrent() && !turn.controller.signal.aborted)
      options.onDone();
  }, error => {
    if (options.isCurrent() && !turn.controller.signal.aborted)
      options.onError(error);
  }, turn.controller, event => {
    if (!options.isCurrent() || turn.controller.signal.aborted)
      return;
    projectMemory(event);
    const projection = projectTimelineEvent(event);
    if (projection.kind === 'compact_started' || projection.kind === 'compact_delta' || projection.kind === 'compact_completed' || projection.kind === 'compact_failed')
      applyCompactProjection(projection, options.setMessages, options.newlyCompactMarkedRef, {
        insertBeforeMessageId: projectMemory.getUserMessageId()
      });
  }, settlementLifecycle(options, options.lifecycle));
}
export interface AgentConversationTransportOptions extends TransportMemoryOptions {
  environment: TextAgentSessionHandlerEnvironment;
  handlers?: ReturnType<typeof createTextAgentSessionHandlers>;
  toolConfiguration?: AgentChatRequest['tool_configuration'];
  onDone(sessionId?: string): void;
  onError(error: Error): void;
  onSessionStart?(sessionId: string): void;
  onDetached?(error: Error | undefined, handlers: ReturnType<typeof createTextAgentSessionHandlers>): void;
  lifecycle?: RequestLifecycleCallbacks;
  client?: Pick<LLMClient, 'agentChatStream'>;
}
/** Canonical Agent handler factory and API body are common to Text and Vision. */
export async function runAgentConversationTransport(options: AgentConversationTransportOptions): Promise<void> {
  const { turn: turn, settings: settings } = options;
  const handlers = options.handlers ?? createTextAgentSessionHandlers(options.environment, {
    current: turn.agentRun
  });
  const context = buildAgentContextMessages({
    messages: [...turn.historyMessages],
    maxContextLength: options.maxContextLength,
    currentUserContent: turn.userContent,
    preserveFullHistory: turn.preserveFullHistory
  });
  await (options.client ?? llmClient).agentChatStream({
    session_id: turn.sessionId,
    message: turn.agentText,
    ...(turn.images?.length ? {
      images: turn.images
    } : {}),
    context_messages: context,
    enable_thinking: settings.enableThinking,
    enable_local_memory: freezeLocalMemoryPreference(options.localMemoryRequestsRef.current, turn.requestId, options.localMemoryEnabled, turn.hasUserText),
    request_id: turn.requestId,
    ...(turn.visionAutomation ? {
      vision_automation: turn.visionAutomation
    } : {}),
    ...(options.toolConfiguration ? {
      tool_configuration: options.toolConfiguration
    } : {}),
  }, step => {
    if (options.isCurrent() && !turn.controller.signal.aborted)
      handlers.onStep(step);
  }, content => {
    if (options.isCurrent() && !turn.controller.signal.aborted)
      handlers.onContent(content);
  }, sessionId => {
    if (turn.pageMode === 'text')
      cleanupRequestBookkeeping(options);
    if (options.isCurrent() && !turn.controller.signal.aborted)
      options.onDone(sessionId);
  }, error => {
    if (options.isCurrent() && !turn.controller.signal.aborted)
      options.onError(error);
  }, sessionId => {
    if (options.isCurrent() && !turn.controller.signal.aborted) {
      if (options.onSessionStart)
        options.onSessionStart(sessionId);
      else
        handlers.onSessionStart(sessionId);
    }
  }, turn.controller, event => {
    if (options.isCurrent() && !turn.controller.signal.aborted)
      handlers.onTimelineEvent(event);
  }, {
    ...settlementLifecycle(options, options.lifecycle),
    onRequestAccepted: async (event) => {
      if (!options.isCurrent())
        return;
      handlers.onRequestAccepted(event);
      await options.lifecycle?.onRequestAccepted?.(event);
    },
    onDetached: (error, context) => {
      if (turn.pageMode === 'text')
        cleanupRequestBookkeeping(options);
      if (!options.isCurrent())
        return;
      // Frozen handlers may clear page flags while Stop awaits its server response.
      // The captured transport keeps successful explicit Stop distinct from idle timeout.
      if (wasConversationStopped(turn.controller)) {
        error = undefined;
        context = { ...context, intentionalInterruption: true };
      }
      if (options.onDetached)
        options.onDetached(error, handlers);
      else
        void options.lifecycle?.onDetached?.(error, context);
    },
  });
}
