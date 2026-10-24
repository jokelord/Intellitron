import { getChatMessageTransportRef, isChatHistoryStartRef } from './local-memory.ts';
/**
 * Unified message and settings types shared between the text and vision pages.
 *
 * Extracted from the previously-duplicated definitions in `app/page.tsx` and
 * `app/vision/page.tsx` (see AGENTS.md trap #1). The compact-related fields
 * (bubbleKind 'compact_summary', isPreCompact, compactionBoundaryId, etc.) are
 * added as part of the /compact feature.
 */
import type { ChatMessage, AgentStep } from "./api";
import type { AssistantReasoningFields } from "./assistant-reasoning";
import type { ManagerChildStatus, TimelineProjection } from "./timeline-projection";
import type { VoiceReplayAudio } from "./voice-input-state";

export type BubbleKind = "default" | "execution" | "compact_summary" | "cycle";
export type CompactStatus = "streaming" | "completed" | "failed";
export type CompactMode = "agent" | "chat" | "cycle";
export type CompactStrategy = "performance" | "utilization";
export type CompactTrigger = "auto" | "manual";

export type HistoricalImageSourceAlias =
  | "manual"
  | "desktop"
  | `camera_${number}`;

export interface HistoricalImageSummary {
  count: number;
  sources: HistoricalImageSourceAlias[];
  relativeRange?: {
    startSeconds: number;
    endSeconds: number;
  };
  placeholder: string;
  readonly contentPartIndex?: number;
}

export interface CompactMetadata {
  trigger?: CompactTrigger;
  mode?: CompactMode;
  originalTokens?: number;
  threshold?: number;
  strategy?: CompactStrategy;
  summaryTokens?: number;
  messagesCompacted?: number;
  failureReason?: string;
}

export interface ManagerParallelChildView {
  dispatchId: string;
  callId: string;
  childRunId: string;
  childName: string;
  status: ManagerChildStatus;
  assignedTask?: string;
  href?: string;
  finalPreview?: string;
  error?: string;
}

export type ManagerChildAgentProjection = Extract<
  TimelineProjection,
  { kind: "manager_child_agent_update" }
>;

export type Message = ChatMessage &
  AssistantReasoningFields & {
    id: string;
    timestamp: Date;
    isStreaming?: boolean;
    images?: string[];
    historicalImageSummaries?: HistoricalImageSummary[];
    voiceReplay?: VoiceReplayAudio;
    /** Retrieved excerpts for display only; excluded by all model wire projections. */
    localMemories?: import("./retrieved-local-memory").RetrievedLocalMemoryChunk[];
    /** Opaque accepted Chat-turn reference; wire transport metadata only. */
    chatTurnRef?: string;
    /** Exact accepted assistant identity and active submitted history window; transport controls only. */
    chatMessageRef?: string;
    chatHistoryStartRef?: string;
    agentSteps?: AgentStep[];
    bubbleKind?: BubbleKind;
    isPreCompact?: boolean;
    compactionBoundaryId?: string;
    compactStatus?: CompactStatus;
    compactMetadata?: CompactMetadata;
    managerDispatches?: Record<string, ManagerParallelChildView[]>;
    metadata?: Record<string, unknown>;
    /** 普通聊天 Hub journal 指针：流式中断后据此恢复重放；完成后清除。 */
    chatResume?: { sessionId: string };
  };

export function mergeManagerChildAgentUpdate(
  message: Message,
  projection: ManagerChildAgentProjection
): Message {
  const dispatchId = projection.dispatchId;
  if (!dispatchId || !projection.childRunId) {
    return message;
  }

  const existingDispatches = message.managerDispatches ?? {};
  const existingChildren = existingDispatches[dispatchId] ?? [];
  const nextChild: ManagerParallelChildView = {
    dispatchId,
    callId: projection.callId,
    childRunId: projection.childRunId,
    childName: projection.childName || projection.childRunId,
    status: projection.status,
    assignedTask: projection.assignedTask,
    href: projection.href,
    finalPreview: projection.finalPreview,
    error: projection.error,
  };
  const childIndex = existingChildren.findIndex(
    (child) => child.childRunId === projection.childRunId
  );
  const nextChildren = childIndex === -1
    ? [...existingChildren, nextChild]
    : existingChildren.map((child, index) => (
        index === childIndex
          ? {
              ...child,
              ...nextChild,
              callId: nextChild.callId || child.callId,
              assignedTask: nextChild.assignedTask ?? child.assignedTask,
              href: nextChild.href ?? child.href,
              finalPreview: nextChild.finalPreview ?? child.finalPreview,
              error: nextChild.error ?? child.error,
            }
          : child
      ));

  return {
    ...message,
    managerDispatches: {
      ...existingDispatches,
      [dispatchId]: nextChildren,
    },
  };
}

export function getManagerDispatchesAfterOrchestrationStep(
  step: AgentStep,
  managerDispatches: Record<string, ManagerParallelChildView[]> | undefined,
): Array<[string, ManagerParallelChildView[]]> {
  if (
    step.step_type !== 'orchestration'
    || step.orchestration_type !== 'plan'
    || !step.stages
    || !managerDispatches
  ) {
    return [];
  }

  const dispatchCallIds = new Set(
    step.stages.flatMap((stage) =>
      stage.tools
        .filter((tool) => tool.name === 'dispatching_parallel_agent')
        .map((tool) => tool.call_id)
    )
  );
  return Object.entries(managerDispatches).filter(([, children]) =>
    children.some((child) => dispatchCallIds.has(child.callId))
  );
}

export function mergeManagerChildAgentUpdateIntoMessages(
  messages: Message[],
  messageId: string | null | undefined,
  projection: ManagerChildAgentProjection
): Message[] {
  if (!messageId) {
    return messages;
  }

  const targetIndex = messages.findIndex(
    (message) => message.id === messageId && message.role === "assistant"
  );
  if (targetIndex === -1) {
    return messages;
  }

  const updated = [...messages];
  updated[targetIndex] = mergeManagerChildAgentUpdate(updated[targetIndex], projection);
  return updated;
}

export interface AgentRunState {
  assistantMessageId: string;
  projectedAssistantMessageIds: Set<string>;
  coordinatorMessageId?: string;
  executionAssistantMessageId?: string;
  secondaryHandoffExecutionBubblePendingAgentSwitch?: boolean;
  stopWasClicked: boolean;
  answerNowWasClicked: boolean;
}

export interface ChatSettings {
  temperature: number;
  topP: number;
  topK: number;
  repetitionPenalty: number;
  maxTokens: number;
  systemPrompt: string;
  showReasoning: boolean;
  enterToSend: boolean;
  enableThinking: boolean;
  enableLocalMemory: boolean;
  agentMode: boolean;
  /** Future Agent-context defaults; never the truth for a locked session. */
  managerAgentTools: string[];
  basicAgentTools: string[];
  foldThinkingAfterFinalAnswer?: boolean;
}

export interface RuntimeContextConfig {
  enable_runtime_time_context: boolean;
}

export const DEFAULT_SETTINGS: ChatSettings = {
  temperature: 0.6,
  topP: 0.95,
  topK: 50,
  repetitionPenalty: 1.0,
  maxTokens: 4096,
  systemPrompt:
    "你是一个有帮助的AI助手，运行在 NVIDIA Jetson Thor 平台上。请用简洁、准确的语言回答问题。",
  showReasoning: true,
  enterToSend: false,
  enableThinking: false,
  enableLocalMemory: false,
  agentMode: false,
  managerAgentTools: [
    "read_file", "write_file", "edit_file", "glob", "grep", "exec_command",
    "list_skills", "read_skill", "web_fetch", "search_and_fetch",
  ],
  basicAgentTools: [
    "read_file", "write_file", "edit_file", "glob", "grep", "exec_command",
    "check_process", "list_skills", "read_skill", "web_search",
    "web_fetch", "search_and_fetch",
  ],
  foldThinkingAfterFinalAnswer: true,
};

export function decideChatSettingsPersist(
  isHydrated: boolean,
  skippedInitialPersist: boolean
): { persist: boolean; skippedInitialPersist: boolean } {
  if (!isHydrated) {
    return { persist: false, skippedInitialPersist };
  }
  if (!skippedInitialPersist) {
    return { persist: false, skippedInitialPersist: true };
  }
  return { persist: true, skippedInitialPersist };
}

export function generateId(): string {
  return `msg-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
}

export function estimateTokenCount(text: string): number {
  const chineseChars = (text.match(/[\u4e00-\u9fff]/g) || []).length;
  const englishWords = text
    .replace(/[\u4e00-\u9fff]/g, "")
    .split(/\s+/)
    .filter((word) => word.length > 0).length;
  const otherChars = text.length - chineseChars - englishWords;
  return Math.ceil(chineseChars * 1.5 + englishWords + otherChars * 0.5);
}

/**
 * Build the wire `metadata` payload for a single message, translating
 * frontend-side compact flags into the OpenAI-compatible metadata dict
 * that the backend's `filter_messages_for_llm` understands.
 *
 *   isPreCompact: true           -> metadata.pre_compact = true
 *   bubbleKind: 'compact_summary' -> metadata.compact_summary = true
 *                                    metadata.summary_id = compactionBoundaryId
 *
 * Any pre-existing `metadata` object on the message is merged in
 * (compact-derived keys win on conflict, so they cannot be clobbered
 * by external clients sending their own metadata).
 *
 * Returns `undefined` when there is no metadata to send, so the
 * ChatMessage wire shape stays clean for messages that have nothing
 * to mark.
 */
export function buildCompactMetadata(msg: Message): Record<string, unknown> | undefined {
  const meta: Record<string, unknown> = { ...(msg.metadata ?? {}) };
  let touched = false;
  if (msg.role === 'user' && typeof msg.chatTurnRef === 'string' && /^chat:.+:[1-9][0-9]*:user$/.test(msg.chatTurnRef)) {
    meta.chat_turn_ref = msg.chatTurnRef;
    touched = true;
  }
  if ((msg.role === 'user' || msg.role === 'assistant') && typeof msg.chatMessageRef === 'string'
    && (new RegExp(`^chat:.+:[1-9][0-9]*:${msg.role}$`).test(msg.chatMessageRef)
      || new RegExp(`^chat:.+:bootstrap:[^:]+:${msg.role}$`).test(msg.chatMessageRef))) {
    meta.chat_message_ref = msg.chatMessageRef;
    touched = true;
  }
  if (msg.isPreCompact === true) {
    meta.pre_compact = true;
    touched = true;
  }
  if (msg.bubbleKind === "compact_summary") {
    meta.compact_summary = true;
    if (msg.compactionBoundaryId) {
      meta.summary_id = msg.compactionBoundaryId;
    }
    touched = true;
  }
  return touched ? meta : (msg.metadata ? meta : undefined);
}

export function truncateMessagesForContext(
  messages: Message[],
  maxContextLength: number,
  systemPrompt?: string
): ChatMessage[] {
  const chatMessages: ChatMessage[] = [];
  let totalTokens = 0;

  const reservedTokens = 8192;
  const availableTokens = maxContextLength - reservedTokens;

  if (systemPrompt) {
    const systemTokens = estimateTokenCount(systemPrompt);
    if (totalTokens + systemTokens <= availableTokens) {
      chatMessages.push({
        role: "system",
        content: systemPrompt,
      });
      totalTokens += systemTokens;
    }
  }

  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    let textContent = "";
    if (typeof msg.content === "string") {
      textContent = msg.content;
    } else if (Array.isArray(msg.content)) {
      textContent = (msg.content as any[])
        .filter((c) => c.type === "text")
        .map((c) => c.text)
        .join(" ");
    }
    const msgTokens = estimateTokenCount(textContent);

    if (totalTokens + msgTokens <= availableTokens) {
      const wireMsg: ChatMessage = {
        role: msg.role,
        content: msg.content,
      };
      const meta = buildCompactMetadata(msg);
      if (meta !== undefined) {
        (wireMsg as ChatMessage & { metadata?: Record<string, unknown> }).metadata = meta;
      }
      chatMessages.unshift(wireMsg);
      totalTokens += msgTokens;
    } else {
      if (chatMessages.length === 0 && msg.role === "user") {
        const truncatedContent =
          typeof msg.content === "string"
            ? msg.content.substring(0, Math.floor(availableTokens * 0.5))
            : msg.content;
        const wireMsg: ChatMessage = {
          role: msg.role,
          content: truncatedContent,
        };
        const meta = buildCompactMetadata(msg);
        if (meta !== undefined) {
          (wireMsg as ChatMessage & { metadata?: Record<string, unknown> }).metadata = meta;
        }
        chatMessages.unshift(wireMsg);
      }
      break;
    }
  }

  return chatMessages;
}

export interface BuildChatWireMessagesArgs {
  chatSessionId?: string;
  messages: Message[];
  maxContextLength: number;
  systemPrompt?: string | null;
  runtimeContext?: string | null;
  currentUserContent: ChatMessage["content"];
  preserveFullHistory?: boolean;
}

export interface BuildAgentContextMessagesArgs {
  messages: Message[];
  maxContextLength: number;
  currentUserContent: ChatMessage["content"];
  preserveFullHistory?: boolean;
}

function projectFullMessagesForWire(messages: readonly Message[]): ChatMessage[] {
  return messages.map((msg) => {
    const wireMsg: ChatMessage = { role: msg.role, content: msg.content };
    const metadata = buildCompactMetadata(msg);
    if (metadata !== undefined) wireMsg.metadata = metadata;
    return wireMsg;
  });
}

export function buildMergedSystemContent(
  systemPrompt?: string | null,
  runtimeContext?: string | null
): string {
  const systemPromptTrimmed = systemPrompt?.trim() ?? "";
  const runtimeContextTrimmed = runtimeContext?.trim() ?? "";
  if (systemPromptTrimmed && runtimeContextTrimmed) {
    return `${systemPromptTrimmed}\n\n${runtimeContextTrimmed}`;
  }
  return systemPromptTrimmed || runtimeContextTrimmed;
}

/** Whitelist opaque transport identities for a specific Chat session; never derive text from UI metadata. */
export function projectChatMessagesWithTransportRefs(messages: readonly Message[], sessionId: string): Message[] {
  return messages.map((message) => {
    if (message.role !== 'user' && message.role !== 'assistant') return message;
    const metadata = { ...message.metadata };
    delete metadata.chat_turn_ref;
    delete metadata.chat_message_ref;
    return { ...message, metadata: Object.keys(metadata).length ? metadata : undefined,
      chatTurnRef: isChatHistoryStartRef(message.chatTurnRef, sessionId) ? message.chatTurnRef : undefined,
      chatMessageRef: getChatMessageTransportRef(message, sessionId) };
  });
}

export function buildChatWireMessages(
  args: BuildChatWireMessagesArgs
): ChatMessage[] {
  const chatMessages: ChatMessage[] = [];
  const sourceMessages = args.chatSessionId ? projectChatMessagesWithTransportRefs(args.messages, args.chatSessionId) : args.messages;
  const systemContent = buildMergedSystemContent(args.systemPrompt, args.runtimeContext);

  if (systemContent) {
    chatMessages.push({ role: "system", content: systemContent });
  }

  const truncatedMessages = args.preserveFullHistory
    ? projectFullMessagesForWire(sourceMessages)
    : truncateMessagesForContext(
        sourceMessages.slice(-20),
        args.maxContextLength,
        systemContent || undefined
      );

  for (const msg of truncatedMessages) {
    const isCompactSummaryBubble =
      msg.role === "system" &&
      (msg as ChatMessage & { metadata?: Record<string, unknown> }).metadata?.[
        "compact_summary"
      ] === true;
    if (msg.role === "system" && !isCompactSummaryBubble) {
      continue;
    }
    chatMessages.push({ ...msg });
  }

  chatMessages.push({ role: "user", content: args.currentUserContent });
  return chatMessages;
}

export function buildAgentContextMessages(
  args: BuildAgentContextMessagesArgs
): ChatMessage[] {
  const contextMessages: ChatMessage[] = [];
  const truncatedMessages = args.preserveFullHistory
    ? projectFullMessagesForWire(args.messages)
    : truncateMessagesForContext(
        args.messages.slice(-20),
        args.maxContextLength,
        undefined
      );

  for (const msg of truncatedMessages) {
    const isCompactSummaryBubble =
      msg.role === "system" &&
      (msg as ChatMessage & { metadata?: Record<string, unknown> }).metadata?.[
        "compact_summary"
      ] === true;
    if (msg.role === "system" && !isCompactSummaryBubble) {
      continue;
    }
    contextMessages.push({ ...msg });
  }

  contextMessages.push({ role: "user", content: args.currentUserContent });
  return contextMessages;
}
