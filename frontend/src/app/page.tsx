'use client';

import { useConversationState } from '@/lib/conversation/use-conversation-state';
import { useConversationBootstrap } from '@/lib/conversation/use-conversation-bootstrap';
import { useConversationRecovery } from '@/lib/conversation/use-conversation-recovery';
import ConversationHeader from '@/components/conversation/ConversationHeader';
import ConversationSettingsDialog from '@/components/conversation/ConversationSettingsDialog';
import ConversationMessageList from '@/components/conversation/ConversationMessageList';
import ConversationComposer from '@/components/conversation/ConversationComposer';
import ConversationTextInputActions from '@/components/conversation/ConversationTextInputActions';
import { useConversationCycle } from '@/lib/use-conversation-cycle';
import { runChatConversationTransport, runAgentConversationTransport } from '@/lib/conversation/conversation-transport';
import { useConversationVoice } from '@/lib/conversation/use-conversation-voice';
import { useConversationCommands } from '@/lib/conversation/use-conversation-commands';
import { clearConversation, confirmConversationSettings, finalizeConversationStop, stopOrdinaryConversation } from '@/lib/conversation/conversation-actions';
import { useConversationSend, createConversationTurnMessages } from '@/lib/conversation/use-conversation-send';
import { rejectConversationToolAdmission } from '@/lib/conversation/conversation-tool-admission';
import React, { useState, useRef, useEffect, useLayoutEffect, useCallback, useMemo, useReducer } from 'react';
import { flushSync } from 'react-dom';
import LocalMemoryToggle from '@/components/LocalMemoryToggle';
import RetrievedLocalMemories from '@/components/RetrievedLocalMemories';
import { freezeLocalMemoryPreference, selectChatLocalMemoryHistoryWindow, getChatRequestHistoryStartRef, freezeChatHistoryStartRef } from '@/lib/local-memory';
import { useRouter } from 'next/navigation';
import { Send, Trash2, Settings, Bot, Loader2, Zap, RefreshCw, Brain, Plus, Upload, Camera } from 'lucide-react';
import { llmClient, ChatMessage, AgentStep, answerNowStream, stopAgent, stopChat, createClientStepId, dispatchAgentSessionPayload, getSavedChatSettings, mergeServerDefaultsWithCache, parseCachedChatSettings, CycleRecoveryError } from '@/lib/api';
import {
  finalizeStreamingAssistantMessage,
  getAssistantRenderableText,
  getStreamingTerminalBehavior,
  captureAgentInterruptionIntent,
  shouldDisableAnswerNowButton,
} from '@/lib/answer-now-ui';
import {
  collectTrackedAssistantMessageIds,
  finalizeTrackedAssistantMessage,
  finalizeTrackedAssistantMessages,
  updateTrackedAssistantMessage,
} from '@/lib/dispatch-message';
import { appendAssistantDelta, type AssistantReasoningFields } from '@/lib/assistant-reasoning';
import { projectTimelineEvent } from '@/lib/timeline-projection';
import { applyCompactProjection } from '@/lib/compact-projection';
import { buildCompactRequestBody } from '@/lib/compact-client';
import { createAgentSessionId } from '@/lib/session-id';
import {
  insertMessageTemplateIntoInput,
  sendMessageTemplateThroughExistingPipeline,
} from '@/lib/message-template-page-actions';
import { getChatInputPlaceholder, shouldSubmitChatInput } from '@/lib/chat-input-keyboard';
import {
  classifyAgentSessionRecoveryError,
  clearRetainedAgentSessionBeforeDelete,
  detachRetainedAgentSessionSubscriber,
} from '@/lib/agent-session-recovery';
import { createChatSessionHandlers, createChatLocalMemoryDisplayHandler } from '@/lib/chat-session-handlers';
import {
  SessionHubRecoveryCoordinator,
  SessionHubBookmarkTransactionRegistry,
  SessionHubLiveAttachmentRegistry,
  clearSessionHubRecoveryState,
  resolveSessionHubClearTarget,
  selectSessionHubRecoveryCandidate,
  type SessionHubRecoveryCandidate,
} from '@/lib/session-hub-recovery';
import {
  createTextAgentSessionHandlers,
  settleKnownAnswerNowRejection,
} from '@/lib/text-agent-session-handlers';
import {
  clearChatTranscript,
} from '@/lib/chat-transcript-storage';
import { resizeChatInputTextarea } from '@/lib/chat-input-layout';
import { useChatScrollFollow } from '@/lib/use-chat-scroll-follow';
import { deferImperativeAgentSend } from '@/lib/agent-queue';
import {
  DEFAULT_WEBUI_SKIN_ID,
  WEBUI_SKINS,
  applyWebUISkinOverride,
  buildWebUIStyleVars,
  getWebUIInputBarResizeOptions,
  getWebUIHeaderSubtitle,
  getWebUIBubbleCharacterLimit,
  getWebUIFoldThinkingAfterFinalAnswer,
  getWebUIMessageTemplateDefaults,
  getWebUIPageHeaderDisplay,
  getWebUIPageTitle,
  getWebUISkin,
  isAuroraGlowSkin,
  readStoredWebUIMessageTemplateImmediateSend,
  readStoredWebUIMessageTemplates,
  readStoredWebUISkinId,
  resolveWebUIMessageTemplates,
  clearStoredWebUIMessageTemplates,
  writeStoredWebUIMessageTemplateImmediateSend,
  writeStoredWebUIMessageTemplates,
  writeStoredWebUISkinId,
  type WebUIConfig,
} from '@/lib/web-ui-config';
import {
  SLASH_COMMANDS,
  detectSlashCommand,
  filterCommands,
  isCompleteSlashCommand,
  mergeSlashCommandState,
  parseCompactCustomInstruction,
  type SlashCommand,
} from '@/lib/slash-commands';
import { SlashCommandMenu } from '@/components/SlashCommandMenu';
import { VoiceInputPanel } from '@/components/VoiceInputPanel';
import { VoiceListeningIndicator } from '@/components/VoiceListeningIndicator';
import { DictationMagicSendModal } from '@/components/DictationMagicSendModal';
import { VoiceModeToggle, type VoiceInputMode } from '@/components/VoiceModeToggle';
import { VoiceReplayPlayer } from '@/components/VoiceReplayPlayer';
import { createVoiceInputConfig, useVoiceInput, type VoiceInputConfig } from '@/lib/voice-input';
import { clearDictationMagicSendSourceIfUnchanged, findDictationMagicSendMatch, readDictationMagicSendSettings, useDictationMagicSend, writeDictationMagicSendSettings, type DictationMagicSendSettings } from '@/lib/dictation-magic-send';
import { CompactSummaryPanel } from '@/components/CompactSummaryPanel';
import ManagerParallelDispatchCard from '@/components/ManagerParallelDispatchCard';
import GlassJellySurface from '@/components/GlassJellySurface';
import { HoverHint } from '@/components/HoverHint';
import AgentStepDisplay from '@/components/AgentStepDisplay';
import AssistantReasoningSection from '@/components/AssistantReasoningSection';
import MarkdownRenderer from '@/components/MarkdownRenderer';
import AssistantBubbleActions from '@/components/AssistantBubbleActions';
import CollapsibleBubbleContent from '@/components/CollapsibleBubbleContent';
import ChatAvatar from '@/components/ChatAvatar';
import { getChatMessageBubbleWidthClass } from '@/lib/chat-message-layout';
import { ToggleSwitch } from '@/components/ToggleSwitch';
import AgentAdvancedSettingsPanel from '@/components/AgentAdvancedSettingsPanel';
import {
  AGENT_BUSINESS_TOOL_NAMES,
  EMPTY_AGENT_TOOL_SESSION_STATE,
  reduceAgentToolSessionState,
  resolvePairedAgentSessionId,
  buildAgentToolConfigurationCandidate,
  resolveAgentToolDefaults,
  getUnknownAgentToolIds,
  readAgentToolServerConfig,
  formatAgentToolConfigurationRejection,
  type AgentBusinessToolCatalog,
  type AgentToolConfigurationSnapshot,
} from '@/lib/agent-tool-configuration';
import ImageLightbox from '@/components/ImageLightbox';
import CycleEditor from '@/components/CycleEditor';
import { classifyCycleEvent, clearCycleBookmark, createCycleEventGuard, readCycleBookmark, writeCycleBookmark } from '@/lib/cycle-page';
import { createCycleAgentProjection, updateCycleVisibility } from '@/lib/cycle-agent-projection';
import CycleProgress from '@/components/CycleProgress';
import MessageTemplateMenu from '@/components/MessageTemplateMenu';
import { destroyAllCameraStreams } from '@/components/CameraCapture';
import {
  CycleStep,
  CycleSSEEvent,
  CycleExecutionStatus,
  ScheduledMessage,
  CycleConfig,
  normalizeCycleConfig,
  getBrowserCycleTimezone,
} from '@/lib/cycle-types';

/**
 * 对话消息接口（包含UI状态）
 */
import {
  Message, AgentRunState, ChatSettings, RuntimeContextConfig,
  DEFAULT_SETTINGS, generateId, buildAgentContextMessages, buildChatWireMessages,
  decideChatSettingsPersist, getManagerDispatchesAfterOrchestrationStep,
} from "../lib/message-types";


/**
 * 主页面组件
 * 
 * 提供完整的聊天界面，包括：
 * - 消息列表显示
 * - 流式响应渲染
 * - 设置面板
 */
export default function Home() {
  const router = useRouter();

  // ============================================================
  // 性能优化：预编译 /vision 路由 + sessionStorage 缓存
  // 解决 Next.js 开发模式下按需编译导致的跳转延迟
  // ============================================================
  useEffect(() => {
    // 1. 立即预编译 /vision 页面（与 API 调用并行，消除编译等待）
    router.prefetch('/vision');

    // 2. 检查 sessionStorage 缓存，实现后续访问瞬间跳转
    const cachedModelType = sessionStorage.getItem('llm-model-type');
    if(cachedModelType === 'vision') {
      console.log('[LLM-Test] 命中缓存：vision 模式，瞬间跳转');
      router.replace('/vision');
    }
  }, [router]);

  // 状态管理
  const conversationState = useConversationState();
  const { messages, setMessages, input, setInput, isLoading, setIsLoading, stoppedMessageId, setStoppedMessageId, stoppedTrackedMessageIds, setStoppedTrackedMessageIds, showSettings, setShowSettings, settingsTab, setSettingsTab, settings, setSettings, settingsRef, localMemoryRequestsRef, chatHistoryRequestsRef, pendingSettings, setPendingSettings, connectionStatus, setConnectionStatus, modelName, setModelName, maxContextLength, setMaxContextLength, serverDefaults, setServerDefaults, runtimeContextConfig, setRuntimeContextConfig, webUIConfig, setWebUIConfig, yamlMessageTemplates, setYamlMessageTemplates, messageTemplates, setMessageTemplates, messageTemplateImmediateSend, setMessageTemplateImmediateSend, messageTemplateStorageError, setMessageTemplateStorageError, voiceConfig, setVoiceConfig, dictationMagicSendSettings, setDictationMagicSendSettings, showDictationMagicSendSettings, setShowDictationMagicSendSettings, inputMode, setInputMode, inputModeRef, selectedSkinId, setSelectedSkinId, pendingSkinId, setPendingSkinId, runtimeTimeContext, setRuntimeTimeContext, agentSessionId, setAgentSessionId, agentToolState, dispatchAgentToolState, agentToolCatalog, setAgentToolCatalog, agentToolNotice, setAgentToolNotice, unknownAgentToolNoticeShownRef, chatSessionId, setChatSessionId, recoveryCandidate, setRecoveryCandidate, attachedRecoverySessionId, setAttachedRecoverySessionId, recoveryError, setRecoveryError, recoveryCoordinatorRef, bookmarkTransactionsRef, liveChatAttachmentsRef, liveAgentAttachmentsRef, currentRequestId, setCurrentRequestId, agentEntryAgentName, setAgentEntryAgentName, showInputActions, setShowInputActions, lightboxSrc, setLightboxSrc, slashMenu, setSlashMenu, collapsedCompact, setCollapsedCompact, newlyCompactMarkedRef, inputRef, abortControllerRef, hasLoadedSettingsRef, isHydratedRef, skippedInitialSettingsPersistRef, manuallyToggledReasoningIdsRef, pendingKeyboardVoiceReplayRef, releaseVoiceReplayRef, voiceModeTransitionRef, stopWasClickedRef, isLoadingRef, activeAgentRunRef, messagesRef } = conversationState;


  const [isAnswerNowLoading, setIsAnswerNowLoading] = useState(false);

  settingsRef.current = settings;

  const [modelInfo, setModelInfo] = useState<{ name: string, description: string } | null>(null);
  // 默认1M

  const recoveryBookmark = recoveryCandidate?.bookmark ?? null;

  // Cycle state

  const [cycleConfig, setCycleConfig] = useState<CycleConfig>(normalizeCycleConfig());
  const { cycleStatus, cycleSessionId, cycleEvents, cycleValidationError, cycleRecoveryNotice, cycleStartGuardRef, handleCycleStart, handleCycleStop, cancelCycleStart, resetCycle } = useConversationCycle({
    pageMode: 'text', showThumbnails: cycleConfig.show_thumbnails, setMessages,
    getRequestSettings: () => ({
      agent_mode: settingsRef.current.agentMode, enable_thinking: settingsRef.current.enableThinking, enable_local_memory: settingsRef.current.enableLocalMemory,
      ...(settingsRef.current.agentMode ? { tool_configuration: buildAgentToolConfigurationCandidate({ managerAgentTools: settingsRef.current.managerAgentTools, basicAgentTools: settingsRef.current.basicAgentTools }, agentToolCatalog) } : {}),
    }),
    isStartBlocked: () => isLoadingRef.current,

  });

  const [showCycle, setShowCycle] = useState(false);

  // Slash command popup state (single source of truth for both /c detection and UI)

  // Compact summary panel collapse state (per-panel-id)

  // Ids that were newly marked isPreCompact by the most recent compact_started
  // (for precise rollback on compact_failed)


  // refs
  const { messageScrollContainerRef, messageContentRef, messageFooterRef } = useChatScrollFollow(messages, isLoading);

  const handleSendRef = useRef<(
    explicitText?: string,
    replay?: Message['voiceReplay'],
    magicSendSourceInput?: string,
  ) => Promise<void> | void>(() => { });

  const answerNowWasClickedRef = useRef(false);

  // Mirror of the messages state, read by handleSelectSlashCommand so the
  // /compact request body always uses the latest conversation without
  // re-creating the callback on every state change.

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);


  const resetStreamingState = useCallback(() => {
    abortControllerRef.current = null;
    isLoadingRef.current = false;
    setIsLoading(false);
    setCurrentRequestId(undefined);
  }, []);

  const normalAgentEnvironment = {
    activeRunRef: activeAgentRunRef,
    setMessages: (update: (previous: Message[]) => Message[]) => setMessages(update),
    setAgentSessionId: setAgentSessionId,
    setAgentBusy: (busy: boolean) => { isLoadingRef.current = busy; setIsLoading(busy); },
    resetStreamingState,
    clearStoppedState: () => setStoppedMessageId(null),
    clearAnswerNowFlag: () => { answerNowWasClickedRef.current = false; },
    clearStopFlag: () => { stopWasClickedRef.current = false; },
    getTerminalBehavior: (run: AgentRunState) => getStreamingTerminalBehavior({ stopWasClicked: run.stopWasClicked, answerNowWasClicked: run.answerNowWasClicked }),
    newlyCompactMarkedRef, defaultReasoningCollapsed: !settings.showReasoning,
    now: () => new Date(), createId: generateId,
  };
  const restoreAgentSession = useConversationRecovery({ pageMode: 'text', state: conversationState, agentEnvironment: normalAgentEnvironment, bindSession: setAgentSessionId });

  useEffect(() => {
    if(inputRef.current) resizeChatInputTextarea(inputRef.current, getWebUIInputBarResizeOptions(webUIConfig));
  }, [input, webUIConfig]);

  const stopStreamingMessage = useCallback(() => {
    setMessages((prev: Message[]) => finalizeStreamingAssistantMessage(prev));
  }, []);

  const buildRuntimeTimeContext = useCallback(() => {
    return `Current local time: ${new Date().toLocaleString('en-US', {
      weekday: 'long',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })}`;
  }, []);

  const trackedStoppedMessageIds = new Set(stoppedTrackedMessageIds);

  // 客户端挂载后从 localStorage 恢复设置（避免服务端/客户端 hydration 不一致）
  useConversationBootstrap({
    pageMode: 'text', state: conversationState, setCycleConfig,
    onModelType: (config) => {
      const modelType = config.model?.model_type || 'text';
      sessionStorage.setItem('llm-model-type', modelType);
      if(modelType === 'vision') { router.replace('/vision'); return true; }
      return false;
    },
    onModels: (models) => { if(models[0]) setModelInfo({ name: models[0].id, description: `这是一个基于 ${models[0].id} 模型的对话体验程序。` }); },
  });

  /**
   * 保存设置到 localStorage
   */


  // 答案生成完成后自动折叠已完成消息的推理过程；用户手动展开/收起后以手动状态为准
  useEffect(() => {
    const autoFold = settings.foldThinkingAfterFinalAnswer
      ?? getWebUIFoldThinkingAfterFinalAnswer(webUIConfig);
    if(!autoFold) return;
    setMessages((prev: Message[]) => {
      let changed = false;
      const next = prev.map((message) => {
        if(message.role !== 'assistant' || !message.reasoning_content || message.isStreaming) return message;
        if(manuallyToggledReasoningIdsRef.current.has(message.id)) return message;
        if(message.reasoningCollapsed ?? !settings.showReasoning) return message;
        changed = true;
        return { ...message, reasoningCollapsed: true };
      });
      return changed ? next : prev;
    });
  }, [messages, settings.showReasoning, settings.foldThinkingAfterFinalAnswer, webUIConfig]);

  /**
   * 获取模型信息和配置
   */


  /**
   * 检查后端连接状态
   */


  /**
   * 获取 Cycle 配置
   */


  /**
   * 发送消息
   */
  const { messageQueue, setMessageQueue, enqueueIfBusy } = useConversationSend({ state: conversationState, busy: isLoading, isBusyNow: () => isLoadingRef.current, sendQueued: draft => handleSend(draft.text), });
  const handleSend = useCallback(async (
    explicitText?: string,
    replay?: Message['voiceReplay'],
    magicSendSourceInput?: string,
  ) => {
    const trimmedInput = (explicitText ?? input).trim();
    if(!trimmedInput) {
      return;
    }
    const pending = pendingKeyboardVoiceReplayRef.current;
    const effectiveReplay = replay ?? (
      pending && pending.transcript === trimmedInput ? pending.replay : undefined
    );

    // If currently loading, queue the message for later
    if(enqueueIfBusy(trimmedInput)) return;
    if(pending && effectiveReplay !== pending.replay) {
      releaseVoiceReplayRef.current(pending.replay);
    }
    pendingKeyboardVoiceReplayRef.current = null;

    // Reset stop state for new request
    stopWasClickedRef.current = false;
    answerNowWasClickedRef.current = false;
    setStoppedMessageId(null);
    setStoppedTrackedMessageIds([]);
    isLoadingRef.current = true;
    const controller = new AbortController();
    abortControllerRef.current = controller;
    console.log('[FRONTEND] handleSend: setting isLoading=true, agentSessionId:', agentSessionId);
    setIsLoading(true);

    // Generate a unique request ID for this chat request
    const requestId = `req_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
    setCurrentRequestId(requestId);

    // 创建用户消息


    const shouldShowInitialRoutingDispatch = settings.agentMode && agentEntryAgentName === 'coordinator_1';

    // 普通聊天走 Hub journal：页面意外关闭后推理继续，可恢复续流。
    const effectiveChatSessionId = settings.agentMode
      ? undefined
      : chatSessionId ?? `chat-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

    // 创建助手消息占位符
    const { userMessage, assistantMessage, agentRun } = createConversationTurnMessages({ userContent: trimmedInput, replay: effectiveReplay, chatSessionId: effectiveChatSessionId, initialRouting: shouldShowInitialRoutingDispatch });

    activeAgentRunRef.current = agentRun;

    // 更新状态 - 同时将之前的助手消息标记为已完成
    flushSync(() => {
      setMessages((prev: Message[]) => {
        const updated = prev.map(msg =>
          msg.role === 'assistant' ? { ...msg, isStreaming: false } : msg
        );
        return [...updated, userMessage, assistantMessage];
      });
      setInput((current) => magicSendSourceInput ? clearDictationMagicSendSourceIfUnchanged(current, magicSendSourceInput) : '');
      setIsLoading(true);
      setStoppedMessageId(null);
    });

    let effectiveRuntimeTimeContext: string | null = null;
    if(runtimeContextConfig.enable_runtime_time_context) {
      effectiveRuntimeTimeContext = runtimeTimeContext ?? buildRuntimeTimeContext();
      if(!runtimeTimeContext) {
        setRuntimeTimeContext(effectiveRuntimeTimeContext);
      }
    }

    try {
      if(settings.agentMode) {
        // ====== Agent 模式 ======
        const effectiveSessionId = agentToolState.pairedSessionId
          ?? agentSessionId
          ?? resolvePairedAgentSessionId(agentToolState, createAgentSessionId);
        const toolCandidate = buildAgentToolConfigurationCandidate({
          managerAgentTools: settings.managerAgentTools,
          basicAgentTools: settings.basicAgentTools,
        }, agentToolCatalog);
        if(!agentToolState.sessions[effectiveSessionId]) {
          dispatchAgentToolState({ type: 'begin', sessionId: effectiveSessionId, requestId, candidate: toolCandidate });
        }
        const agentAttachment = liveAgentAttachmentsRef.current.attach(effectiveSessionId);
        if(!agentSessionId) {
          setAgentSessionId(effectiveSessionId);
        }
        const liveRunRef: { current: AgentRunState | null } = { current: agentRun };
        const agentHandlers = createTextAgentSessionHandlers({
          activeRunRef: activeAgentRunRef,
          setMessages: (update) => setMessages(update),
          setAgentSessionId,
          onAgentToolConfigurationAccepted: (accepted) => {
            if(accepted.sessionId && accepted.sessionId !== effectiveSessionId) return;
            if(accepted.toolConfiguration) {
              dispatchAgentToolState({ type: 'accept', sessionId: effectiveSessionId, requestId, snapshot: accepted.toolConfiguration });
            }
          },
          setAgentBusy: (busy) => {
            isLoadingRef.current = busy;
            setIsLoading(busy);
          },
          resetStreamingState,
          clearStoppedState: () => setStoppedMessageId(null),
          clearAnswerNowFlag: () => { answerNowWasClickedRef.current = false; },
          clearStopFlag: () => { stopWasClickedRef.current = false; },
          getTerminalBehavior: (run) => getStreamingTerminalBehavior({
            stopWasClicked: run.stopWasClicked,
            answerNowWasClicked: run.answerNowWasClicked,
          }),
          newlyCompactMarkedRef,
          defaultReasoningCollapsed: !settings.showReasoning,
          now: () => new Date(),
          createId: generateId,
        }, liveRunRef);
        const bookmarkTransaction = bookmarkTransactionsRef.current.begin({
          sessionId: effectiveSessionId,
          pageMode: 'text',
          interactionMode: 'agent',
          createdAt: Date.now(),
        });
        setRecoveryCandidate(selectSessionHubRecoveryCandidate('text'));
        await runAgentConversationTransport({          
turn: { pageMode: 'text', requestId, controller, interactionMode: 'agent', sessionId: effectiveSessionId, historyMessages: messages, userContent: trimmedInput, agentText: trimmedInput, preserveFullHistory: false, hasUserText: true, userMessage, assistantMessage, agentRun }, settings, maxContextLength, localMemoryRequestsRef, chatHistoryRequestsRef, localMemoryEnabled: settingsRef.current.enableLocalMemory, environment: normalAgentEnvironment, handlers: agentHandlers, toolConfiguration: agentToolState.sessions[effectiveSessionId] ? undefined : toolCandidate, isCurrent: () => liveAgentAttachmentsRef.current.isCurrent(agentAttachment), onDone: (...args) => {
            localMemoryRequestsRef.current.delete(requestId);
            chatHistoryRequestsRef.current.delete(requestId);
            if(!liveAgentAttachmentsRef.current.isCurrent(agentAttachment)) return;
            agentHandlers.onDone(...args);
            liveAgentAttachmentsRef.current.detach(agentAttachment, () => undefined);
          }, onError: (...args) => { if(liveAgentAttachmentsRef.current.isCurrent(agentAttachment)) agentHandlers.onPayloadError(...args); }, onSessionStart: (...args) => { if(liveAgentAttachmentsRef.current.isCurrent(agentAttachment)) agentHandlers.onSessionStart(...args); }, lifecycle: {
            onRequestAccepted: (accepted) => {
              if(!liveAgentAttachmentsRef.current.isCurrent(agentAttachment)) return;
              bookmarkTransactionsRef.current.accept(bookmarkTransaction);
              setAttachedRecoverySessionId(effectiveSessionId);
              setRecoveryCandidate(selectSessionHubRecoveryCandidate('text'));
            },
            onKnownRejection: (rejection) => {
              localMemoryRequestsRef.current.delete(requestId);
              chatHistoryRequestsRef.current.delete(requestId);
              if(!liveAgentAttachmentsRef.current.isCurrent(agentAttachment)) return;
              const { isToolConfigurationError, visibleMessage } = rejectConversationToolAdmission({
                state: conversationState,
                sessionId: effectiveSessionId,
                requestId,
                rejection,
                isCurrent: () => liveAgentAttachmentsRef.current.isCurrent(agentAttachment),
              });
              if(isToolConfigurationError) {
                setRecoveryError({ kind: 'transient', message: visibleMessage });
              }
              bookmarkTransactionsRef.current.rollback(bookmarkTransaction);
              setRecoveryCandidate(selectSessionHubRecoveryCandidate('text'));
              agentHandlers.onPayloadError(new Error(visibleMessage));
            },
            onDetached: (error?) => {
              if(!liveAgentAttachmentsRef.current.isCurrent(agentAttachment)) return;
              bookmarkTransactionsRef.current.retain(bookmarkTransaction);
              const intentionalInterruption = captureAgentInterruptionIntent({
                stopWasClicked: stopWasClickedRef.current,
                answerNowWasClicked: answerNowWasClickedRef.current,
              });
              if(intentionalInterruption) {
                liveAgentAttachmentsRef.current.detach(agentAttachment, () => undefined);
                return;
              }
              setAttachedRecoverySessionId(null);
              agentHandlers.onDetached();
              if(error) setRecoveryError(classifyAgentSessionRecoveryError(error));
              liveAgentAttachmentsRef.current.detach(agentAttachment, () => undefined);
            },
          }        
});
      } else {
        // ====== 普通聊天模式 ======
        console.log('[CHAT] Starting chat with requestId:', requestId, 'currentRequestId state:', currentRequestId);

        const bookmarkTransaction = bookmarkTransactionsRef.current.begin({
          sessionId: effectiveChatSessionId!,
          pageMode: 'text',
          interactionMode: 'chat',
          createdAt: Date.now(),
        });
        const liveAttachment = liveChatAttachmentsRef.current.attach(effectiveChatSessionId!);
        setRecoveryCandidate(selectSessionHubRecoveryCandidate('text'));
        const displayLiveChatError = (error: Error) => {
          const terminalBehavior = getStreamingTerminalBehavior({
            stopWasClicked: stopWasClickedRef.current,
            answerNowWasClicked: answerNowWasClickedRef.current,
          });
          if(terminalBehavior.suppressErrorMessage) return;
          setMessages((prev: Message[]) => prev.map((message) => {
            if(message.id !== assistantMessage.id) return message;
            const content = typeof message.content === 'string' ? message.content : '';
            return {
              ...message,
              content: content.trim().length > 0
                ? `${content}\n\n[流式中断: ${error.message}]`
                : `抱歉，发生了错误: ${error.message}`,
            };
          }));
        };
        const finalizeLiveChat = (stableTerminal = false) => {
          const terminalBehavior = getStreamingTerminalBehavior({
            stopWasClicked: stopWasClickedRef.current,
            answerNowWasClicked: answerNowWasClickedRef.current,
          });
          if(stableTerminal) {
            setMessages((prev: Message[]) => prev.map((message) => (
              message.id === assistantMessage.id ? { ...message, chatResume: undefined } : message
            )));
          }
          stopStreamingMessage();
          if(terminalBehavior.resetStreamingState) resetStreamingState();
          if(terminalBehavior.resetStoppedState) setStoppedMessageId(null);
          if(terminalBehavior.clearAnswerNowFlag) answerNowWasClickedRef.current = false;
          stopWasClickedRef.current = false;
        };

        await runChatConversationTransport({          
turn: { pageMode: 'text', requestId, controller, interactionMode: 'chat', sessionId: effectiveChatSessionId!, historyMessages: messages, userContent: trimmedInput, agentText: trimmedInput, preserveFullHistory: false, hasUserText: true, userMessage, assistantMessage, agentRun }, settings, maxContextLength, localMemoryRequestsRef, chatHistoryRequestsRef, localMemoryEnabled: settingsRef.current.enableLocalMemory, runtimeContext: effectiveRuntimeTimeContext, setMessages, newlyCompactMarkedRef, isCurrent: () => liveChatAttachmentsRef.current.isCurrent(liveAttachment), onDone: () => {
            localMemoryRequestsRef.current.delete(requestId);
            chatHistoryRequestsRef.current.delete(requestId);
            if(!liveChatAttachmentsRef.current.detach(liveAttachment, () => undefined)) return;
            bookmarkTransactionsRef.current.retain(bookmarkTransaction);
            finalizeLiveChat(true);
          }, onError: (error: Error) => {
            console.error('聊天错误:', error);
            liveChatAttachmentsRef.current.applyError(liveAttachment, () => {
              displayLiveChatError(error);
            });
          }, lifecycle: {
            onRequestAccepted: (event) => {
              if(event.requestId !== requestId || !effectiveChatSessionId) return;
              if(!liveChatAttachmentsRef.current.isCurrent(liveAttachment)) return;
              bookmarkTransactionsRef.current.accept(bookmarkTransaction);
              setChatSessionId(effectiveChatSessionId);
              setAttachedRecoverySessionId(effectiveChatSessionId);
              setRecoveryCandidate(selectSessionHubRecoveryCandidate('text'));
            },
            onKnownRejection: (rejection) => {
              localMemoryRequestsRef.current.delete(requestId);
              chatHistoryRequestsRef.current.delete(requestId);
              bookmarkTransactionsRef.current.rollback(bookmarkTransaction);
              setRecoveryCandidate(selectSessionHubRecoveryCandidate('text'));
              liveChatAttachmentsRef.current.applyError(liveAttachment, () => {
                displayLiveChatError(new Error(rejection.message));
              });
              liveChatAttachmentsRef.current.detach(liveAttachment, () => {
                finalizeLiveChat();
              });
            },
            onDetached: (error) => {
              bookmarkTransactionsRef.current.retain(bookmarkTransaction);
              if(liveChatAttachmentsRef.current.isCurrent(liveAttachment)) {
                setAttachedRecoverySessionId(null);
              }
              if(error) {
                liveChatAttachmentsRef.current.applyError(liveAttachment, () => {
                  displayLiveChatError(error);
                });
              }
              liveChatAttachmentsRef.current.detach(liveAttachment, () => {
                finalizeLiveChat();
              });
            },
          }        
});
      }
    } catch(error) {
      console.error('请求失败:', error);
      const terminalBehavior = getStreamingTerminalBehavior({
        stopWasClicked: stopWasClickedRef.current,
        answerNowWasClicked: answerNowWasClickedRef.current,
      });
      stopStreamingMessage();
      if(terminalBehavior.resetStreamingState) {
        resetStreamingState();
      }
      if(terminalBehavior.resetStoppedState) {
        setStoppedMessageId(null);
      }
      if(terminalBehavior.clearAnswerNowFlag) {
        answerNowWasClickedRef.current = false;
      }
      stopWasClickedRef.current = false;
    }
  }, [
    input,
    isLoading,
    messages,
    settings,
    connectionStatus,
    agentEntryAgentName,
    agentToolCatalog,
    agentToolState,
    runtimeContextConfig.enable_runtime_time_context,
    runtimeTimeContext,
    buildRuntimeTimeContext,
    agentSessionId,
    chatSessionId,
    maxContextLength,
    resetStreamingState,
    stopStreamingMessage,
  ]);

  useLayoutEffect(() => {
    handleSendRef.current = handleSend;
  }, [handleSend]);

  const sendConversationInput = (text?: string, replay?: Message['voiceReplay'], source?: string) => handleSend(text, replay, source);
  const { voiceController, dictationMagicSendPending, handleVoiceModeChange, insertDictationTextAtCursor } = useConversationVoice({ state: conversationState, send: sendConversationInput, busy: isLoading, cycleRunning: cycleStatus === 'running' });
  const { handleMessageTemplatesChange, handleRestoreMessageTemplates, handleMessageTemplateImmediateSendChange, handleInsertMessageTemplate, handleSendMessageTemplate, filteredSlashCommands, disabledCommandMap, handleSelectSlashCommand, handleSubmit, handleKeyDown, onInputChange } = useConversationCommands({ state: conversationState, pageMode: 'text', busy: isLoading, cycleStatus, send: sendConversationInput, voiceController, buildRuntimeTimeContext });

  // 听写：把最终转写插入输入框光标处（未聚焦则追加到末尾）

  // 听写兜底：auto_send=false 或空转写时 hook 停在 transcript_ready，这里落字并立即重开麦（持续听写）

  // 处理消息队列：当加载完成后，自动发送队列中的下一条消息


  /**
   * 处理输入框按键
   */

  /**
   * 清空对话
   */
  const handleClear = () => clearConversation({ pageMode: 'text', state: conversationState, voice: voiceController, cancelPendingCycleStart: cancelCycleStart, resetDraft: () => { setMessageQueue([]); }, });

  /**
   * 停止当前聊天（普通模式）
   */
  const handleStopChat = () => stopOrdinaryConversation(conversationState, 'chat', resetStreamingState);

  /**
   * 停止当前 Agent 执行
   */
  const handleStopAgent = () => stopOrdinaryConversation(conversationState, 'agent', resetStreamingState);

  /**
   * 强制当前 Agent 立即返回答案
   */
  const handleAnswerNow = useCallback(async () => {
    if (agentSessionId && !isAnswerNowLoading && !answerNowWasClickedRef.current) {
      try {
        answerNowWasClickedRef.current = true;
        const answerNowRun = activeAgentRunRef.current;
        if (activeAgentRunRef.current) {
          activeAgentRunRef.current.answerNowWasClicked = true;
          setMessages((prev: Message[]) => finalizeTrackedAssistantMessages(
            prev,
            collectTrackedAssistantMessageIds(
              activeAgentRunRef.current?.assistantMessageId,
              activeAgentRunRef.current?.projectedAssistantMessageIds,
              activeAgentRunRef.current?.executionAssistantMessageId,
            ),
          ));
        } else {
          stopStreamingMessage();
        }
        setIsAnswerNowLoading(true);
        setStoppedMessageId(null);
        abortControllerRef.current?.abort();
        const answerNowEntropy = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
          ? crypto.randomUUID()
          : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
        const answerNowRequestId = `answer-now-request-${answerNowEntropy}`;
        const controller = new AbortController();
        abortControllerRef.current = controller;
        const answerNowAttachment = liveAgentAttachmentsRef.current.attach(agentSessionId);
        const answerNowRunRef = { current: answerNowRun };
        const answerNowHandlers = createTextAgentSessionHandlers({
          activeRunRef: activeAgentRunRef,
          setMessages: (update) => setMessages(update),
          setAgentSessionId,
          resetStreamingState,
          clearStoppedState: () => setStoppedMessageId(null),
          clearAnswerNowFlag: () => { answerNowWasClickedRef.current = false; },
          clearStopFlag: () => { stopWasClickedRef.current = false; },
          getTerminalBehavior: (run) => getStreamingTerminalBehavior({
            stopWasClicked: run.stopWasClicked,
            answerNowWasClicked: run.answerNowWasClicked,
          }),
          newlyCompactMarkedRef,
          defaultReasoningCollapsed: !settings.showReasoning,
          now: () => new Date(),
          createId: generateId,
        }, answerNowRunRef);
        await answerNowStream(
          agentSessionId,
          answerNowRequestId,
          () => undefined,
          () => {
            resetStreamingState();
            setIsAnswerNowLoading(false);
          },
          () => undefined,
          undefined,
          controller,
          {
            onKnownRejection: (rejection) => {
              void settleKnownAnswerNowRejection({
                transportToken: answerNowAttachment,
                isCurrentToken: (token) => liveAgentAttachmentsRef.current.isCurrent(token),
                run: answerNowRun,
                handlers: answerNowHandlers,
                rejection: new Error(rejection.message),
                clearAnswerNowFlag: () => { answerNowWasClickedRef.current = false; },
                setAnswerNowLoading: setIsAnswerNowLoading,
              });
            },
            onTransportEof: () => {
              void llmClient.waitForRequestTerminal(answerNowRequestId, { signal: controller.signal }).catch(() => undefined);
            },
            onDetached: () => {
              detachRetainedAgentSessionSubscriber({
                transportToken: answerNowAttachment,
                isCurrentToken: (token) => liveAgentAttachmentsRef.current.isCurrent(token),
                boundSessionId: agentSessionId,
                detachedSessionId: agentSessionId,
                detachLocal: () => {
                  answerNowHandlers.onDetached();
                  resetStreamingState();
                  setIsAnswerNowLoading(false);
                },
              });
            },
          },
          answerNowHandlers,
        );
      } catch (error) {
        resetStreamingState();
        setIsAnswerNowLoading(false);
        console.error('Answer Now 失败:', error);
      } finally {
        answerNowWasClickedRef.current = false;
      }
    }
  }, [agentSessionId, isAnswerNowLoading, resetStreamingState, stopStreamingMessage]);

  /**
   * 启动 Cycle 执行
   */

  /**
   * 停止 Cycle 执行
   */


  /**
   * 渲染连接状态指示器
   */
  const renderConnectionStatus = () => {
    const statusConfig: Record<typeof connectionStatus, { color: string; text: string }> = {
      checking: { color: 'bg-yellow-500', text: '检查连接中...' },
      connected: { color: 'bg-green-500', text: '已连接' },
      error: { color: 'bg-red-500', text: '连接失败' },
    };

    const { color, text } = statusConfig[connectionStatus];

    return (
      <HoverHint label={text}>
        <span className={`block w-2 h-2 rounded-full ${color}`}></span>
      </HoverHint>
    );
  };

  const glassJellyEnabled = isAuroraGlowSkin(webUIConfig);
  const showPageHeader = getWebUIPageHeaderDisplay(webUIConfig);
  const bubbleCharacterLimit = getWebUIBubbleCharacterLimit(webUIConfig);
  const foldThinkingAfterFinalAnswer = settings.foldThinkingAfterFinalAnswer
    ?? getWebUIFoldThinkingAfterFinalAnswer(webUIConfig);
  const appSkinClass = glassJellyEnabled ? 'webui-skin-aurora-glow-r3f-v1' : '';
  const glassHeaderZoneClass = glassJellyEnabled
    ? 'webui-glass-zone border-transparent'
    : 'webui-zone-header-bg border-gray-200 dark:border-gray-700';
  const glassFooterZoneClass = glassJellyEnabled
    ? 'webui-glass-zone border-transparent'
    : 'webui-zone-footer-bg border-gray-200 dark:border-gray-700';
  const voiceAvailabilityDisabledReason = !voiceConfig?.enabled
    ? 'Voice input is unavailable.'
    : connectionStatus !== 'connected'
      ? 'Voice input requires a backend connection.'
      : cycleStatus === 'running'
        ? 'Voice input is unavailable while a cycle is running.'
        : isLoading && !voiceController.isVoiceMode
          ? 'Wait for the current response before starting voice input.'
          : undefined;
  const voiceDisabledReason = voiceAvailabilityDisabledReason ?? voiceController.state.error?.message;
  const voiceDisabled = Boolean(voiceAvailabilityDisabledReason) ||
    voiceController.state.error?.recoverable === false;

  const handleConfirmSettings = () => confirmConversationSettings(conversationState, { resetStreamingState, });

  return (
    <div className={`app-shell flex flex-col overflow-y-auto bg-gray-50 dark:bg-gray-900 ${appSkinClass}`} style={buildWebUIStyleVars(webUIConfig)} ref={messageScrollContainerRef}>
      {/* 头部 */}
      <ConversationHeader pageMode="text" webUIConfig={webUIConfig} modelName={modelName} agentMode={settings.agentMode} connectionStatus={connectionStatus} inputMode={inputMode} onVoiceModeChange={handleVoiceModeChange} voiceDisabled={voiceDisabled} voiceDisabledReason={voiceDisabledReason} canRecover={Boolean(recoveryBookmark && attachedRecoverySessionId !== recoveryBookmark.sessionId)} onRecover={restoreAgentSession} enableThinking={settings.enableThinking} onToggleThinking={() => setSettings(prev => ({ ...prev, enableThinking: !prev.enableThinking }))} enableLocalMemory={settings.enableLocalMemory} onToggleLocalMemory={enabled => setSettings(prev => ({ ...prev, enableLocalMemory: enabled }))} onOpenSettings={() => { setPendingSkinId(selectedSkinId); setPendingSettings({ ...settings }); setShowSettings(true); }} onClear={handleClear} />

      {recoveryError && (
        <div className="mx-auto mt-3 flex max-w-4xl items-center gap-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-100">
          <span>{recoveryError.message}</span>
          {recoveryError.kind === 'expired' ? (
            <button type="button" className="underline" onClick={() => {
              clearSessionHubRecoveryState('text');
              setRecoveryCandidate(null);
              setRecoveryError(null);
            }}>Dismiss</button>
          ) : (
            <button type="button" className="underline" onClick={restoreAgentSession}>Retry</button>
          )}
        </div>
      )}

      {/* 设置对话框 */}
      <ConversationSettingsDialog open={showSettings} pendingSettings={pendingSettings} setPendingSettings={setPendingSettings} settingsTab={settingsTab} setSettingsTab={setSettingsTab} pendingSkinId={pendingSkinId} setPendingSkinId={setPendingSkinId} maxContextLength={maxContextLength} agentToolCatalog={agentToolCatalog} sessionRecord={agentToolState.pairedSessionId ? agentToolState.sessions[agentToolState.pairedSessionId] ?? null : null} toolNotice={agentToolNotice} resetDisabled={!serverDefaults || Boolean(agentToolState.pairedSessionId && ['pending', 'locked'].includes(agentToolState.sessions[agentToolState.pairedSessionId]?.kind ?? ''))} onReset={() => { if(serverDefaults) { setPendingSettings(serverDefaults); setPendingSkinId(DEFAULT_WEBUI_SKIN_ID); } }} onCancel={() => { setShowSettings(false); setPendingSettings(null); }} onDismiss={() => setShowSettings(false)} onConfirm={handleConfirmSettings} />

      {/* 消息列表 */}
      <ConversationMessageList messages={messages} contentRef={messageContentRef} webUIConfig={webUIConfig} settings={settings} bubbleCharacterLimit={bubbleCharacterLimit} foldThinkingAfterFinalAnswer={foldThinkingAfterFinalAnswer} stoppedMessageIds={trackedStoppedMessageIds} collapsedCompact={collapsedCompact} onToggleCompact={id => setCollapsedCompact(previous => { const next = new Set(previous); if(next.has(id)) next.delete(id); else next.add(id); return next; })} onToggleReasoning={id => { manuallyToggledReasoningIdsRef.current.add(id); setMessages(previous => updateTrackedAssistantMessage(previous, id, target => ({ ...target, reasoningCollapsed: !(target.reasoningCollapsed ?? !settings.showReasoning) }))); }} onOpenImage={setLightboxSrc} getUserText={message => typeof message.content === 'string' ? message.content : ''} emptyContent={(
        <div className="text-center py-20">
          <div className="w-16 h-16 bg-nvidia-500 rounded-2xl flex items-center justify-center mx-auto mb-4">
            <Bot className="w-8 h-8 text-white" />
          </div>
          <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">
            欢迎使用 LLM-Test
          </h2>
          <p className="text-gray-500 dark:text-gray-400 max-w-md mx-auto">
            {modelInfo?.description || '这是一个基于 AI 模型的对话体验程序。'}
            开始输入您的问题吧！
          </p>
        </div>
      )} />

      {/* 输入区域 */}
      <ConversationComposer footerRef={messageFooterRef} webUIConfig={webUIConfig} busy={isLoading} cycleRunning={cycleStatus === 'running'} connectionError={connectionStatus === 'error'} sendDisabled={(voiceController.isVoiceMode && inputMode === 'voice'
        ? !voiceController.state.pendingTranscript.trim()
        : !input.trim()) ||
        isLoading ||
        connectionStatus === 'error'} input={input} inputRef={inputRef} inputMode={inputMode} voiceController={voiceController} showInputActions={showInputActions} onToggleInputActions={() => setShowInputActions(current => !current)} onInputChange={onInputChange} onKeyDown={handleKeyDown} onSubmit={handleSubmit} onStop={() => { if(settings.agentMode) void handleStopAgent(); else void handleStopChat(); }} onSwitchToKeyboard={() => void handleVoiceModeChange('keyboard')} onOpenDictationSettings={dictationMagicSendSettings ? () => setShowDictationMagicSendSettings(true) : undefined} slashMenu={slashMenu} filteredSlashCommands={filteredSlashCommands} disabledCommandMap={disabledCommandMap} onSelectSlashCommand={handleSelectSlashCommand} onCloseSlashMenu={() => setSlashMenu({ visible: false, query: '', selectedIndex: 0 })} notices={<>{cycleValidationError ? <p role="alert" className="text-sm text-red-600">{cycleValidationError}</p> : null}{cycleRecoveryNotice ? <p role="status" className="text-sm text-amber-700">{cycleRecoveryNotice} <a href="/vision" className="underline">前往 Vision 恢复</a></p> : null}</>} actions={<ConversationTextInputActions
          visible={showInputActions}
          cycleConfig={cycleConfig}
          cycleStatus={cycleStatus}
          cycleEvents={cycleEvents}
          showCycle={showCycle}
          onToggleCycle={() => setShowCycle(current => !current)}
          onCycleStart={handleCycleStart}
          onCycleStop={handleCycleStop}
          messageTemplateMenu={<MessageTemplateMenu
                actionBarVisible={showInputActions}
                templates={messageTemplates}
                yamlDefaults={yamlMessageTemplates}
                onTemplatesChange={handleMessageTemplatesChange}
                onRestoreDefaults={handleRestoreMessageTemplates}
                onInsertTemplate={handleInsertMessageTemplate}
                onSendTemplate={handleSendMessageTemplate}
                immediateSend={messageTemplateImmediateSend}
                onImmediateSendChange={handleMessageTemplateImmediateSendChange}
                sendDisabled={isLoading || connectionStatus === 'error' || cycleStatus === 'running'}
                storageError={messageTemplateStorageError}
                onDismissStorageError={() => setMessageTemplateStorageError(null)}
              />}
        />} attachments={undefined} actionIndicator={undefined} enterToSend={settings.enterToSend} />

      {/* 图片全屏预览 */}
      <ImageLightbox src={lightboxSrc} onClose={() => setLightboxSrc(null)} />
      {dictationMagicSendSettings ? <DictationMagicSendModal open={showDictationMagicSendSettings} settings={dictationMagicSendSettings} onCancel={() => setShowDictationMagicSendSettings(false)} onConfirm={(next) => { setDictationMagicSendSettings(next); writeDictationMagicSendSettings(window.localStorage, next); setShowDictationMagicSendSettings(false); }} /> : null}
    </div>
  );
}
