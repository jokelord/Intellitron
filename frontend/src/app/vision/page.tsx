/**
 * 视觉聊天页面
 * 
 * 提供多模态对话功能，支持图片上传和摄像头拍照。
 */

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
import { hasVisionConversationCapability } from '@/lib/vision-automation/use-vision-conversation-adapter';
import { useConversationSend, createConversationTurnMessages } from '@/lib/conversation/use-conversation-send';
import { rejectConversationToolAdmission } from '@/lib/conversation/conversation-tool-admission';
import React, { useState, useRef, useEffect, useLayoutEffect, useCallback, useMemo, useReducer } from 'react';
import { flushSync } from 'react-dom';
import LocalMemoryToggle from '@/components/LocalMemoryToggle';
import RetrievedLocalMemories from '@/components/RetrievedLocalMemories';
import { freezeLocalMemoryPreference, selectChatLocalMemoryHistoryWindow, getChatRequestHistoryStartRef, freezeChatHistoryStartRef } from '@/lib/local-memory';
import { Send, Trash2, Settings, Loader2, Image as ImageIcon, Zap, Brain, Plus } from 'lucide-react';
import { llmClient, ChatMessage, AgentStep, answerNowStream, stopAgent, stopChat, createClientStepId, dispatchAgentSessionPayload, getSavedChatSettings, mergeServerDefaultsWithCache, parseCachedChatSettings, CycleRecoveryError, type ConfigResponse, type RequestLifecycleCallbacks } from '@/lib/api';
import {
  finalizeStreamingAssistantMessage,
  getAssistantRenderableText,
  getStreamingTerminalBehavior,
  captureAgentInterruptionIntent,
  shouldDisableAnswerNowButton,
} from '@/lib/answer-now-ui';
import {
  appendProjectedChatChunk,
  collectTrackedAssistantMessageIds,
  completeRunningDispatchStep,
  finalizeTrackedAssistantMessage,
  finalizeTrackedAssistantMessages,
  finalizeRunningDispatchStep,
  getActiveTrackedAssistantMessageId,
  getContentTargetAssistantMessageId,
  mergeDispatchStep,
  retractTrackedAssistantTailText,
  stripTrailingCanonicalSwitchAgentControl,
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
  isCurrentSessionHubAttachment,
  resolveSessionHubClearTarget,
  selectSessionHubRecoveryCandidate,
  type SessionHubRecoveryCandidate,
} from '@/lib/session-hub-recovery';
import { createTextAgentSessionHandlers, settleKnownAnswerNowRejection, type TextAgentSessionRunRef } from '@/lib/text-agent-session-handlers';
import { createCycleAgentProjection, updateCycleVisibility } from '@/lib/cycle-agent-projection';
import { clearChatTranscript } from '@/lib/chat-transcript-storage';
import { resizeChatInputTextarea } from '@/lib/chat-input-layout';
import { useChatScrollFollow } from '@/lib/use-chat-scroll-follow';
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
  getWebUIVisionWelcomeConfig,
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
import { clearAcceptedDictationMagicSendSource, clearDictationMagicSendSourceIfUnchanged, findDictationMagicSendMatch, readDictationMagicSendSettings, useDictationMagicSend, writeDictationMagicSendSettings, type DictationMagicSendSettings } from '@/lib/dictation-magic-send';
import { CompactSummaryPanel } from '@/components/CompactSummaryPanel';
import ManagerParallelDispatchCard from '@/components/ManagerParallelDispatchCard';
import GlassJellySurface from '@/components/GlassJellySurface';
import { HoverHint } from '@/components/HoverHint';
import ImageUploader from '@/components/ImageUploader';
import MessageTemplateMenu from '@/components/MessageTemplateMenu';
import ImagePreview from '@/components/ImagePreview';
import AgentStepDisplay from '@/components/AgentStepDisplay';
import AssistantReasoningSection from '@/components/AssistantReasoningSection';
import MarkdownRenderer from '@/components/MarkdownRenderer';
import AssistantBubbleActions from '@/components/AssistantBubbleActions';
import CollapsibleBubbleContent from '@/components/CollapsibleBubbleContent';
import ChatAvatar from '@/components/ChatAvatar';
import { getChatMessageBubbleWidthClass } from '@/lib/chat-message-layout';
import ImageLightbox from '@/components/ImageLightbox';
import { destroyAllCameraStreams } from '@/components/CameraCapture';
import { classifyCycleEvent, clearCycleBookmark, createCycleEventGuard, readCycleBookmark, writeCycleBookmark } from '@/lib/cycle-page';
import { ToggleSwitch } from '@/components/ToggleSwitch';
import AgentAdvancedSettingsPanel from '@/components/AgentAdvancedSettingsPanel';
import {
  AGENT_BUSINESS_TOOL_NAMES,
  EMPTY_AGENT_TOOL_SESSION_STATE,
  buildAgentToolConfigurationCandidate,
  reduceAgentToolSessionState,
  resolveAgentToolDefaults,
  resolvePairedAgentSessionId,
  getUnknownAgentToolIds,
  readAgentToolServerConfig,
  formatAgentToolConfigurationRejection,
  type AgentBusinessToolCatalog,
} from '@/lib/agent-tool-configuration';
import VisionAutomationPanel from '@/components/VisionAutomationPanel';
import VisionAutomationStatusRing from '@/components/VisionAutomationStatusRing';
import HistoricalImageSummaryCard from '@/components/HistoricalImageSummaryCard';
import {
  loadVisionAutomationConfig,
  saveVisionAutomationPreferences,
  validateVisionAutomationServerConfig,
  type VisionAutomationServerConfig,
} from '@/lib/vision-automation/config';
import {
  acquireCameraCaptureSource,
  acquireDisplayCaptureSource,
  requestBrowserDisplayMedia,
} from '@/lib/vision-automation/capture-source';
import { browserMediaSourceRegistry } from '@/lib/vision-automation/media-source-registry';
import {
  useVisionAutomation,
  type VisionAutomationSubmitInput,
} from '@/lib/vision-automation/use-vision-automation';
import type { AutomationBinding, SourceDescriptor } from '@/lib/vision-automation/types';
import type { VisionAutomationSourceHealth } from '@/lib/vision-automation/ui-contracts';
import type { RequestStatusResult } from '@/lib/vision-automation/send-contract';
import {
  AcceptanceMutationRegistry,
  AgentSessionOwnership,
  AutomationTerminalGate,
  CycleStartGuard,
  VisionPageLifecycle,
  abortPageTransportBeforeRetirement,
  abortAcceptedVisionInference,
  acquireAvailableVisionSources,
  attachVisionRequestTransport,
  assertBoundAgentSessionId,
  canResumeVisionVoiceCapture,
  canStartVisionAutomation,
  completeAutomationTerminal,
  createDurableVisionPageGenerationAllocator,
  createAutomationHubLifecycle,
  disposeAutomationTerminalEntries,
  disposeAutomationTerminalEntry,
  executeVisionStop,
  getVisibleVisionUserText,
  installVisionRequestOwnership,
  isVisionRequestBusy,
  projectVisionTurnImages,
  retireAutomationTransportOwnership,
  resolveCanonicalVisionAutomationImageBlock,
  settleKnownCleanupRejection,
} from '@/lib/vision-automation/page-integration';
import {
  deferImperativeAgentSend,
  dequeueVisionMessage,
  enqueueVisionMessage,
  QueuedVisionMessage,
} from '@/lib/agent-queue';

import {
  CycleStep,
  CycleSSEEvent,
  CycleExecutionStatus,
  ScheduledMessage,
  CycleConfig,
  normalizeCycleConfig,
  getBrowserCycleTimezone,
} from '@/lib/cycle-types';

const DISABLED_AUTOMATION_CONFIG: VisionAutomationServerConfig = {
  enabled: false,
  intervalSeconds: 1,
  maxFramesPerSource: 1,
  thumbnailSizePx: 48,
  captureDuringInference: false,
  lockFirstBundle: false,
  tickTimeoutSeconds: 5,
  consecutiveTickFailureLimit: 3,
  maxEncodedPayloadBytes: 1,
  initialJpegQuality: 85,
  minimumJpegQuality: 40,
  minimumImageDimensionPx: 256,
  maxImageDimensionPx: 1024,
  base64EncodeConcurrency: 1,
  maxRequestBodyBytes: 2,
  requestStatusTtlSeconds: 600,
};

function createAutomationLifecycleId(prefix: string): string {
  const entropy = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${entropy}`;
}

function toAutomationConfig(response: ConfigResponse): VisionAutomationServerConfig | null {
  const automation = response.vision?.automation;
  if(!automation) return null;
  return validateVisionAutomationServerConfig({
    enabled: automation.enabled,
    intervalSeconds: automation.interval_seconds,
    maxFramesPerSource: automation.max_frames_per_source,
    thumbnailSizePx: automation.thumbnail_size_px,
    captureDuringInference: automation.capture_during_inference,
    lockFirstBundle: automation.lock_first_bundle,
    tickTimeoutSeconds: automation.tick_timeout_seconds,
    consecutiveTickFailureLimit: automation.consecutive_tick_failure_limit,
    maxEncodedPayloadBytes: automation.max_encoded_payload_bytes,
    initialJpegQuality: automation.initial_jpeg_quality,
    minimumJpegQuality: automation.minimum_jpeg_quality,
    minimumImageDimensionPx: automation.minimum_image_dimension_px,
    maxImageDimensionPx: response.vision?.max_image_dimension
      ?? response.vision?.camera_max_dimension
      ?? 1024,
    base64EncodeConcurrency: automation.base64_encode_concurrency,
    maxRequestBodyBytes: automation.max_request_body_bytes,
    requestStatusTtlSeconds: automation.request_status_ttl_seconds,
  });
}

async function encodeAutomationFrame(
  { blob }: { blob: Blob },
  tier: { quality: number; maxDimensionPx: number },
  signal: AbortSignal,
): Promise<Blob> {
  if(signal.aborted) throw signal.reason;
  const bitmap = await createImageBitmap(blob);
  try {
    if(signal.aborted) throw signal.reason;
    const scale = Math.min(1, tier.maxDimensionPx / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.floor(bitmap.width * scale));
    canvas.height = Math.max(1, Math.floor(bitmap.height * scale));
    const context = canvas.getContext('2d');
    if(!context) throw new Error('Canvas 2D context is unavailable');
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((encoded) => {
        if(signal.aborted) reject(signal.reason);
        else if(!encoded) reject(new Error('Automation JPEG encoding returned null'));
        else resolve(encoded);
      }, 'image/jpeg', tier.quality / 100);
    });
  } finally {
    bitmap.close();
  }
}

/**
 * 对话消息接口（包含UI状态）
 */
import {
  Message, AgentRunState, ChatSettings, RuntimeContextConfig,
  DEFAULT_SETTINGS, generateId, buildAgentContextMessages, buildChatWireMessages,
  decideChatSettingsPersist, getManagerDispatchesAfterOrchestrationStep,
  mergeManagerChildAgentUpdateIntoMessages,
} from "../../lib/message-types";
/**
 * 主页面组件
 */
export default function VisionChat() {
  const conversationState = useConversationState();
  const { messages, setMessages, input, setInput, isLoading, setIsLoading, stoppedMessageId, setStoppedMessageId, stoppedTrackedMessageIds, setStoppedTrackedMessageIds, showSettings, setShowSettings, settingsTab, setSettingsTab, pendingSettings, setPendingSettings, agentSessionId, setAgentSessionId, agentToolState, dispatchAgentToolState, agentToolCatalog, setAgentToolCatalog, agentToolNotice, setAgentToolNotice, unknownAgentToolNoticeShownRef, chatSessionId, setChatSessionId, recoveryCandidate, setRecoveryCandidate, attachedRecoverySessionId, setAttachedRecoverySessionId, recoveryError, setRecoveryError, currentRequestId, setCurrentRequestId, settings, setSettings, settingsRef, localMemoryRequestsRef, chatHistoryRequestsRef, connectionStatus, setConnectionStatus, modelName, setModelName, maxContextLength, setMaxContextLength, serverDefaults, setServerDefaults, runtimeContextConfig, setRuntimeContextConfig, webUIConfig, setWebUIConfig, yamlMessageTemplates, setYamlMessageTemplates, messageTemplates, setMessageTemplates, messageTemplateImmediateSend, setMessageTemplateImmediateSend, messageTemplateStorageError, setMessageTemplateStorageError, voiceConfig, setVoiceConfig, dictationMagicSendSettings, setDictationMagicSendSettings, showDictationMagicSendSettings, setShowDictationMagicSendSettings, inputMode, setInputMode, inputModeRef, selectedSkinId, setSelectedSkinId, pendingSkinId, setPendingSkinId, showInputActions, setShowInputActions, runtimeTimeContext, setRuntimeTimeContext, agentEntryAgentName, setAgentEntryAgentName, isLoadingRef, inputRef, abortControllerRef, recoveryCoordinatorRef, bookmarkTransactionsRef, liveChatAttachmentsRef, liveAgentAttachmentsRef, currentRequestIdRef, hasLoadedSettingsRef, pendingKeyboardVoiceReplayRef, releaseVoiceReplayRef, voiceModeTransitionRef, isHydratedRef, skippedInitialSettingsPersistRef, manuallyToggledReasoningIdsRef, stopWasClickedRef, activeAgentRunRef, messagesRef, lightboxSrc, setLightboxSrc, slashMenu, setSlashMenu, collapsedCompact, setCollapsedCompact, newlyCompactMarkedRef } = conversationState;

  const [images, setImages] = useState<string[]>([]);
  const [imagesKey, setImagesKey] = useState(0);

  const [isAnswerNowLoading, setIsAnswerNowLoading] = useState(false);

  const recoveryBookmark = recoveryCandidate?.bookmark ?? null;

  const [visionCapabilityConfirmed, setVisionCapabilityConfirmed] = useState(false);
  const [hasPendingAcceptance, setHasPendingAcceptance] = useState(false);

  // Cycle state

  const [cycleConfig, setCycleConfig] = useState<CycleConfig>(normalizeCycleConfig());

  // 监听图片被清空时，强制 remount ImageUploader（重置 file input 等内部状态）
  useEffect(() => {
    if(images.length === 0) {
      setImagesKey(prev => prev + 1);
    }
  }, [images.length]);

  settingsRef.current = settings;

  const [visionConfig, setVisionConfig] = useState<{
    maxImageSize: number;
    supportedFormats: string[];
    maxImageTokens: number;
    cameraDimension: number;
    captureDimension: number;
  } | null>(null);
  const [automationConfig, setAutomationConfig] = useState<VisionAutomationServerConfig | null>(null);
  const [availableAutomationSources, setAvailableAutomationSources] = useState<SourceDescriptor[]>([
    { sourceId: 'desktop', kind: 'display', stableAlias: 'desktop', label: 'Desktop' },
  ]);
  const [selectedAutomationSourceIds, setSelectedAutomationSourceIds] = useState<Set<string>>(
    () => new Set(['desktop']),
  );
  const [activeAutomationSources, setActiveAutomationSources] = useState<SourceDescriptor[]>([]);
  const [automationPanelCollapsed, setAutomationPanelCollapsed] = useState(false);
  const [automationPageError, setAutomationPageError] = useState<string | null>(null);
  const [automationTerminalRevision, setAutomationTerminalRevision] = useState(0);
  const [pageGenerationReady, setPageGenerationReady] = useState(false);
  const pageInstanceIdRef = useRef<string>(createAutomationLifecycleId('page'));
  const pageLifecycleRef = useRef<VisionPageLifecycle | null>(null);
  if(pageLifecycleRef.current === null) {
    pageLifecycleRef.current = new VisionPageLifecycle(
      () => createAutomationLifecycleId('chat-context'),
    );
  }
  const pageLifecycle = pageLifecycleRef.current;
  const pageLifecycleCleanupTimerRef = useRef<number | null>(null);
  useEffect(() => {
    if(!visionCapabilityConfirmed) return;
    if(pageLifecycleCleanupTimerRef.current !== null) {
      clearTimeout(pageLifecycleCleanupTimerRef.current);
      pageLifecycleCleanupTimerRef.current = null;
    }
    let storage: Pick<Storage, 'getItem' | 'setItem'> | undefined;
    try {
      storage = window.localStorage;
    } catch {
      storage = undefined;
    }
    const claimGeneration = createDurableVisionPageGenerationAllocator(storage);
    pageLifecycle.bindGenerationClaimant(claimGeneration);
    setPageGenerationReady(true);
  }, [pageLifecycle, visionCapabilityConfirmed]);
  const agentSessionIdRef = useRef<string | undefined>(undefined);
  const agentSessionOwnershipRef = useRef(new AgentSessionOwnership());

  // Keep the delayed Cycle start token available to terminal-event cleanup.
  // Terminal events invalidate the stream generation before followCycle may
  // invoke its completion callback.

  const pendingAcceptanceRequestRef = useRef<string | null>(null);
  const acceptanceMutationsRef = useRef(new AcceptanceMutationRegistry<{
    userMessage: Message;
    assistantMessage: Message;
    agentRun: AgentRunState;
    draftText: string;
    magicSendSourceInput?: string;
    manualImages: readonly string[];
    replay?: Message['voiceReplay'];
    commit?: () => void;
  }>());
  const acceptanceCommitHandlerRef = useRef<(requestId: string) => boolean>(() => false);
  const automationSubmitRef = useRef<(input: VisionAutomationSubmitInput) => Promise<void>>(
    async () => { throw new Error('Vision automation submit adapter is not ready'); },
  );

  const automationTerminalEntriesRef = useRef(new Map<string, {
    gate: AutomationTerminalGate;
    transport: AbortController;
    release(result: RequestStatusResult): void;
    pollTerminal(): Promise<boolean>;
  }>());
  const isVisionRequestBusyNow = useCallback(() => isVisionRequestBusy(
    isLoadingRef.current,
    automationTerminalEntriesRef.current.values(),
  ), []);
  const visionRequestBusy = isVisionRequestBusy(
    isLoading,
    automationTerminalEntriesRef.current.values(),
  );

  const { messageScrollContainerRef, messageContentRef, messageFooterRef } = useChatScrollFollow(messages, visionRequestBusy);

  const handleSendRef = useRef<(
    explicitText?: string,
    replay?: Message['voiceReplay'],
    automationInput?: VisionAutomationSubmitInput,
    magicSendSourceInput?: string,
  ) => Promise<void> | void>(() => { });

  const answerNowWasClickedRef = useRef(false);

  // Mirror of messages state, read by handleSelectSlashCommand so the
  // /compact request body always uses the latest conversation without
  // re-creating the callback on every state change.

  const imagesRef = useRef<string[]>([]);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);
  useEffect(() => {
    imagesRef.current = images;
  }, [images]);

  const bindAgentSessionId = useCallback((sessionId: string) => {
    agentSessionOwnershipRef.current.bind(sessionId);
    agentSessionIdRef.current = sessionId;
    setAgentSessionId((current) => current === sessionId ? current : sessionId);
    return sessionId;
  }, []);

  const resetStreamingState = useCallback(() => {
    abortControllerRef.current = null;
    currentRequestIdRef.current = undefined;
    isLoadingRef.current = false;
    setIsLoading(false);
    setCurrentRequestId(undefined);
  }, []);

  const normalAgentEnvironment = {
    activeRunRef: activeAgentRunRef,
    setMessages: (update: (previous: Message[]) => Message[]) => setMessages(update),
    setAgentSessionId: (sessionId: string | undefined) => { if(sessionId) bindAgentSessionId(sessionId); },
    setAgentBusy: (busy: boolean) => { isLoadingRef.current = busy; setIsLoading(busy); },
    resetStreamingState,
    clearStoppedState: () => setStoppedMessageId(null),
    clearAnswerNowFlag: () => { answerNowWasClickedRef.current = false; },
    clearStopFlag: () => { stopWasClickedRef.current = false; },
    getTerminalBehavior: (run: AgentRunState) => getStreamingTerminalBehavior({ stopWasClicked: run.stopWasClicked, answerNowWasClicked: run.answerNowWasClicked }),
    newlyCompactMarkedRef, defaultReasoningCollapsed: !settings.showReasoning,
    now: () => new Date(), createId: generateId,
  };
  const restoreAgentSession = useConversationRecovery({ pageMode: 'vision', state: conversationState, agentEnvironment: normalAgentEnvironment, bindSession: bindAgentSessionId });

  const visionAutomation = useVisionAutomation({
    disposeOnUnmount: false,
    config: automationConfig ?? DISABLED_AUTOMATION_CONFIG,
    acquireSources: async ({ ownerId, signal, config: activeConfig }) => {
      const selected = availableAutomationSources.filter((source) => (
        selectedAutomationSourceIds.has(source.sourceId)
      ));
      if(selected.length === 0) throw new Error('Select at least one Vision automation source');
      setActiveAutomationSources(selected);
      const frame = {
        maxDimensionPx: activeConfig.maxImageDimensionPx,
        thumbnailSizePx: activeConfig.thumbnailSizePx,
        jpegQuality: activeConfig.initialJpegQuality,
      };
      const captureDimensionPx = visionConfig?.captureDimension ?? 1280;
      const acquisition = await acquireAvailableVisionSources(selected.map((source) => ({
        source,
        acquire: () => source.kind === 'display'
          ? acquireDisplayCaptureSource({
            ownerId,
            descriptor: source,
            requestDisplay: requestBrowserDisplayMedia,
            frame,
            signal,
          })
          : acquireCameraCaptureSource({
            ownerId,
            descriptor: source,
            captureDimensionPx,
            frame,
            signal,
          }),
      })), signal, () => !signal.aborted);
      setActiveAutomationSources(acquisition.sources.map((source) => source.descriptor));
      if(acquisition.failures.length > 0) {
        setAutomationPageError(acquisition.failures.map((failure) => (
          `${failure.source.stableAlias}: ${failure.error instanceof Error ? failure.error.message : String(failure.error)}`
        )).join('; '));
      }
      return acquisition.sources;
    },
    restoreSource: ({ ownerId, source, signal, config: activeConfig }) => {
      const frame = {
        maxDimensionPx: activeConfig.maxImageDimensionPx,
        thumbnailSizePx: activeConfig.thumbnailSizePx,
        jpegQuality: activeConfig.initialJpegQuality,
      };
      if(source.kind === 'display') {
        return acquireDisplayCaptureSource({
          ownerId,
          descriptor: source,
          requestDisplay: requestBrowserDisplayMedia,
          frame,
          signal,
        });
      }
      return acquireCameraCaptureSource({
        ownerId,
        descriptor: source,
        captureDimensionPx: visionConfig?.captureDimension ?? 1280,
        frame,
        signal,
      });
    },
    getManualDataUrls: () => imagesRef.current,
    getMessages: () => messagesRef.current,
    commitMessages: (nextMessages) => {
      messagesRef.current = nextMessages;
      setMessages(nextMessages);
    },
    isModelBusy: isVisionRequestBusyNow,
    encodeFrame: encodeAutomationFrame,
    submit: (automationInput) => automationSubmitRef.current(automationInput),
    getRequestStatus: (requestId, signal) => llmClient.getRequestStatus(requestId, signal),
    waitForRequestTerminal: (requestId, options) => llmClient.waitForRequestTerminal(requestId, options),
    registerAgentAbort: (request, signal) => llmClient.registerAgentVisionAutomationAbort(request, signal).then(() => undefined),
    cancelAgentAbort: (request, signal) => llmClient.cancelAgentVisionAutomationAbort(request, signal).then(() => undefined),
    onRequestAccepted: (requestId) => { acceptanceCommitHandlerRef.current(requestId); },
    onStableTerminal: (requestId) => {
      localMemoryRequestsRef.current.delete(requestId);
      chatHistoryRequestsRef.current.delete(requestId);
      automationTerminalEntriesRef.current.get(requestId)?.release({
        requestId,
        status: 'terminal',
      });
    },
    resolveHistoricalImageBlock: resolveCanonicalVisionAutomationImageBlock,
    createId: (kind) => createAutomationLifecycleId(kind),
  });
  const [showCycle, setShowCycle] = useState(false);
  const { cycleStatus, cycleSessionId, cycleEvents, cycleValidationError, cycleRecoveryNotice, cycleStartGuardRef, handleCycleStart, handleCycleStop, cancelCycleStart, resetCycle } = useConversationCycle({
    pageMode: 'vision', allowPhotos: visionCapabilityConfirmed, showThumbnails: cycleConfig.show_thumbnails, setMessages,
    getRequestSettings: () => ({
      agent_mode: settingsRef.current.agentMode, enable_thinking: settingsRef.current.enableThinking, enable_local_memory: settingsRef.current.enableLocalMemory,
      ...(settingsRef.current.agentMode ? { tool_configuration: buildAgentToolConfigurationCandidate({ managerAgentTools: settingsRef.current.managerAgentTools, basicAgentTools: settingsRef.current.basicAgentTools }, agentToolCatalog) } : {}),
    }),
    isStartBlocked: () => visionAutomation.controller.getState().armed || isVisionRequestBusyNow(),
    beforeCameraStart: async signal => {
      destroyAllCameraStreams();
      await new Promise<void>((resolve, reject) => {
        const finish = () => { signal.removeEventListener('abort', abort); resolve(); };
        const timer = window.setTimeout(finish, 2000);
        const abort = () => { window.clearTimeout(timer); signal.removeEventListener('abort', abort); reject(new DOMException('Cycle camera start cancelled', 'AbortError')); };
        if(signal.aborted) abort(); else signal.addEventListener('abort', abort, { once: true });
      });
    },
  });

  acceptanceCommitHandlerRef.current = (requestId) => acceptanceMutationsRef.current.accept(
    requestId,
    (mutation) => mutation.commit?.(),
  );

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


  const welcomeConfig = getWebUIVisionWelcomeConfig(webUIConfig);

  // Slash command popup state

  useConversationBootstrap({
    pageMode: 'vision', state: conversationState, setCycleConfig, onConfig: (configResponse) => {
      const capable = hasVisionConversationCapability(configResponse);
      setVisionCapabilityConfirmed(capable);
      if(!capable) { setVisionConfig(null); setAutomationConfig(null); return; }
      if(configResponse.vision) setVisionConfig({ maxImageSize: configResponse.vision.max_image_size, supportedFormats: configResponse.vision.supported_formats.split(','), maxImageTokens: configResponse.vision.max_image_tokens, cameraDimension: configResponse.vision.camera_max_dimension ?? 1024, captureDimension: configResponse.vision.capture_dimension ?? 1280 });
      const serverAutomationConfig = toAutomationConfig(configResponse);
      setAutomationConfig(serverAutomationConfig ? loadVisionAutomationConfig(serverAutomationConfig, window.localStorage).config : null);
    }
  });

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
   * 获取配置信息
   */


  /**
   * 检查后端连接状态
   */


  /**
   * 获取 Cycle 配置
   */


  /**
   * 构建多模态消息内容
   */
  const buildMultimodalContent = useCallback((text: string, imageList: string[]): ChatMessage['content'] => {
    if(imageList.length === 0) {
      return text;
    }

    const content: any[] = [];

    // Qwen3-VL 要求图片先于文字，先添加图片
    for(const img of imageList) {
      content.push({
        type: 'image_url',
        image_url: { url: img },
      });
    }

    // 再添加文本
    if(text.trim()) {
      content.push({ type: 'text', text });
    }

    return content;
  }, []);

  /**
   * 发送消息
   */
  const { messageQueue, setMessageQueue, enqueueIfBusy } = useConversationSend({ state: conversationState, busy: visionRequestBusy, isBusyNow: isVisionRequestBusyNow, sendQueued: draft => handleSend(draft.text, undefined, undefined, undefined, draft.images), restoreImages: images => { imagesRef.current = images; setImages(images); }, });
  const handleSend = useCallback(async (
    explicitText?: string,
    replay?: Message['voiceReplay'],
    automationInput?: VisionAutomationSubmitInput,
    magicSendSourceInput?: string,
    queuedImages?: string[],
  ) => {
    const turnImages = queuedImages ?? images;
    if(!visionCapabilityConfirmed && (automationInput || turnImages.length > 0)) { setAutomationPageError('Vision input requires an enabled Vision model.'); return; }
    const trimmedInput = (automationInput?.userText ?? explicitText ?? input).trim();
    const pending = pendingKeyboardVoiceReplayRef.current;
    const effectiveReplay = replay ?? (
      pending && pending.transcript === trimmedInput ? pending.replay : undefined
    );

    // 检查是否可以发送
    if(!automationInput && !trimmedInput && turnImages.length === 0) {
      return;
    }
    if(!automationInput && pendingAcceptanceRequestRef.current !== null) {
      setAutomationPageError('A Vision request is awaiting backend acceptance; the draft and manual images were preserved.');
      return;
    }

    if(!automationInput && visionAutomation.state.armed && isVisionRequestBusyNow()) {
      setAutomationPageError('Vision automation is busy; the draft and captured resources were preserved.');
      return;
    }
    if(!automationInput && visionAutomation.state.armed) {
      try {
        const accepted = await visionAutomation.send(trimmedInput);
        if(magicSendSourceInput) {
          setInput((current) => clearAcceptedDictationMagicSendSource(
            accepted,
            current,
            magicSendSourceInput,
          ));
        }
      } catch(error) {
        setAutomationPageError(error instanceof Error ? error.message : String(error));
      }
      return;
    }

    // If currently loading, queue the message for later with its attached images.
    if(!automationInput && enqueueIfBusy(trimmedInput, turnImages)) return;
    // Generate a unique request ID for this chat request
    let requestId = automationInput?.requestId
      ?? `req_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
    const effectiveChatSessionId = settings.agentMode
      ? undefined
      : chatSessionId ?? pageLifecycle.contextId;

    const cleanupBinding: AutomationBinding = {
      pageInstanceId: pageInstanceIdRef.current,
      pageGeneration: pageLifecycle.generation,
      mode: settings.agentMode ? 'agent' : 'chat',
      contextId: settings.agentMode
        ? (agentSessionIdRef.current ?? createAutomationLifecycleId('agent-context'))
        : effectiveChatSessionId!,
      automationTaskId: createAutomationLifecycleId('ordinary-task'),
    };
    let cleanupStage: Awaited<ReturnType<typeof visionAutomation.stageCleanupSend>> = null;
    if(!automationInput) {
      try {
        cleanupStage = await visionAutomation.stageCleanupSend(cleanupBinding, requestId);
        if(cleanupStage) {
          requestId = cleanupStage.requestId;
          if(!cleanupStage.beginAttempt()) return;
        }
      } catch(error) {
        setAutomationPageError(error instanceof Error ? error.message : String(error));
        return;
      }
    }
    const acceptanceBound = Boolean(automationInput || cleanupStage);
    const controller = automationInput?.controller ?? new AbortController();
    abortControllerRef.current = controller;
    const {
      terminalAttachment: automationAttachment,
      liveAgentAttachment,
      liveChatAttachment,
    } = attachVisionRequestTransport({
      mode: settings.agentMode ? 'agent' : 'chat',
      contextId: cleanupBinding.contextId,
      attachChat: (contextId) => liveChatAttachmentsRef.current.attach(contextId),
      attachAgent: (contextId) => liveAgentAttachmentsRef.current.attach(contextId),
    });
    if(acceptanceBound) {
      pendingAcceptanceRequestRef.current = requestId;
      setHasPendingAcceptance(true);
    }

    // 保存当前图片副本（用于消息）
    const currentImages = automationInput ? [...automationInput.payload.images] : [...turnImages];
    const { bubbleImages, consumedManualImages } = projectVisionTurnImages(
      currentImages,
      automationInput?.visionAutomation.manual_image_count,
    );
    const wireUserText = automationInput?.payload.text ?? trimmedInput;
    const historyMessages = automationInput
      ? [...automationInput.stagedMessages]
      : cleanupStage
        ? [...cleanupStage.stagedMessages]
        : messages;

    const shouldShowInitialRoutingDispatch = settings.agentMode && agentEntryAgentName === 'coordinator_1';
    // 普通聊天走 Hub journal：页面意外关闭后推理继续，可恢复续流。
    const createPageMutation = () => {
      const { userMessage, assistantMessage, agentRun } = createConversationTurnMessages({ userContent: buildMultimodalContent(wireUserText, currentImages), images: bubbleImages, replay: effectiveReplay, chatSessionId: effectiveChatSessionId, initialRouting: shouldShowInitialRoutingDispatch });
      return {
        userMessage,
        assistantMessage,
        agentRun,
        draftText: trimmedInput,
        magicSendSourceInput,
        manualImages: consumedManualImages,
        replay: effectiveReplay,
        commit: undefined as (() => void) | undefined,
      };
    };
    const pageMutation = acceptanceBound
      ? acceptanceMutationsRef.current.stage(requestId, createPageMutation)
      : createPageMutation();
    const { userMessage, assistantMessage, agentRun } = pageMutation;
    const automationTerminalGate = automationInput || cleanupStage
      ? new AutomationTerminalGate(requestId)
      : null;
    const releaseAutomationTerminal = (result: RequestStatusResult) => {
      if(!automationTerminalGate) return;
      const currentEntry = automationTerminalEntriesRef.current.get(automationTerminalGate.requestId);
      if(currentEntry?.gate !== automationTerminalGate) return;
      automationTerminalEntriesRef.current.delete(requestId);
      const completed = completeAutomationTerminal(
        automationTerminalGate,
        result,
        undefined,
        () => {
          const terminalBehavior = getStreamingTerminalBehavior({
            stopWasClicked: agentRun.stopWasClicked || stopWasClickedRef.current,
            answerNowWasClicked: agentRun.answerNowWasClicked || answerNowWasClickedRef.current,
          });
          if(terminalBehavior.resetStreamingState) resetStreamingState();
          if(terminalBehavior.resetStoppedState) setStoppedMessageId(null);
          if(terminalBehavior.clearAnswerNowFlag) answerNowWasClickedRef.current = false;
          stopWasClickedRef.current = false;
          if(activeAgentRunRef.current === agentRun) activeAgentRunRef.current = null;
          setAutomationPageError(null);
        },
        automationAttachment,
        (token) => settings.agentMode
          ? liveAgentAttachmentsRef.current.isCurrent(token)
          : liveChatAttachmentsRef.current.isCurrent(token),
      );
      if(completed) {
        if(settings.agentMode) liveAgentAttachmentsRef.current.detach(automationAttachment, () => undefined);
        else liveChatAttachmentsRef.current.detach(automationAttachment, () => undefined);
      }
      setAutomationTerminalRevision((revision) => revision + 1);
    };
    if(automationTerminalGate) {
      const entry = {
        gate: automationTerminalGate,
        transport: controller,
        release: releaseAutomationTerminal,
        pollTerminal: async () => {
          const outcome = await automationTerminalGate.pollForTerminal(
            (gateRequestId, signal) => llmClient.waitForRequestTerminal(gateRequestId, { signal }),
          );
          if(outcome.kind === 'cancelled') return false;
          if(outcome.kind === 'failed') {
            setAutomationPageError(outcome.error);
            setAutomationTerminalRevision((revision) => revision + 1);
            return false;
          }
          releaseAutomationTerminal(outcome.result);
          return true;
        },
      };
      automationTerminalEntriesRef.current.set(requestId, entry);
    }
    let pageCommitted = !acceptanceBound;
    const applyPageMutation = () => {
      stopWasClickedRef.current = false;
      answerNowWasClickedRef.current = false;
      activeAgentRunRef.current = agentRun;
      isLoadingRef.current = true;
      flushSync(() => {
        setMessages((prev: Message[]) => {
          const updated = prev.map(msg =>
            msg.role === 'assistant' ? { ...msg, isStreaming: false } : msg
          );
          const nextMessages = [...updated, userMessage, assistantMessage];
          messagesRef.current = nextMessages;
          return nextMessages;
        });
        setInput((current) => pageMutation.magicSendSourceInput
          ? clearDictationMagicSendSourceIfUnchanged(current, pageMutation.magicSendSourceInput)
          : current.trim() === pageMutation.draftText ? '' : current);
        setImages((current) => {
          const sent = pageMutation.manualImages;
          return sent.every((image, index) => current[index] === image)
            ? current.slice(sent.length)
            : current;
        });
        setIsLoading(true);
        currentRequestIdRef.current = requestId;
        setCurrentRequestId(requestId);
        setStoppedMessageId(null);
        setStoppedTrackedMessageIds([]);
        setAutomationPageError(null);
      });
      if(pending && pageMutation.replay !== pending.replay) {
        releaseVoiceReplayRef.current(pending.replay);
      }
      pendingKeyboardVoiceReplayRef.current = null;
    };
    if(pageMutation.commit === undefined) {
      pageMutation.commit = () => {
        applyPageMutation();
        pageCommitted = true;
      };
    }
    const commitPageAcceptance = (acceptedRequestId: string) => {
      if(acceptedRequestId !== requestId) throw new Error('Accepted page request identity does not match');
      if(!acceptanceBound) return;
      acceptanceCommitHandlerRef.current(requestId);
      pageCommitted = acceptanceMutationsRef.current.isAccepted(requestId);
      if(pendingAcceptanceRequestRef.current === requestId) {
        pendingAcceptanceRequestRef.current = null;
        setHasPendingAcceptance(false);
      }
    };
    if(!acceptanceBound) applyPageMutation();

    let effectiveRuntimeTimeContext: string | null = null;
    if(runtimeContextConfig.enable_runtime_time_context) {
      effectiveRuntimeTimeContext = runtimeTimeContext ?? buildRuntimeTimeContext();
      if(!runtimeTimeContext) {
        setRuntimeTimeContext(effectiveRuntimeTimeContext);
      }
    }

    let cleanupCommitted = false;
    let cleanupDiscarded = false;
    let cleanupKnownRejected = false;
    const commitCleanup = async (acceptedRequestId: string) => {
      if(!cleanupStage || cleanupCommitted || cleanupDiscarded) return;
      cleanupStage.commitAccepted({ type: 'request_accepted', requestId: acceptedRequestId });
      cleanupCommitted = true;
      commitPageAcceptance(acceptedRequestId);
    };
    const resolveCleanupStatus = async () => {
      if(!cleanupStage || cleanupCommitted || cleanupDiscarded) return;
      const entry = automationTerminalEntriesRef.current.get(requestId);
      if(!entry) return;
      const result = await llmClient.getRequestStatus(requestId, entry.gate.signal);
      if(result.status === 'unknown') {
        cleanupStage.finishAttempt();
        return;
      }
      await commitCleanup(result.requestId);
      if(result.status === 'terminal') entry.release(result);
      else await entry.pollTerminal();
    };
    const lifecycleCallbacks: RequestLifecycleCallbacks | undefined = automationInput ? {
      onRequestAccepted: async (event: { requestId: string }) => {
        await automationInput.lifecycle.onRequestAccepted({ type: 'request_accepted', requestId: event.requestId });
        if(automationInput.isAccepted()) commitPageAcceptance(event.requestId);
      },
      onKnownRejection: async (rejection) => {
        await automationInput.lifecycle.onKnownRejection(rejection);
      },
      onTransportEof: async () => {
        try {
          await automationInput.lifecycle.onTransportEof();
        } catch {
          // The page-owned request gate performs the authoritative status poll.
        }
        if(automationInput.isAccepted()) commitPageAcceptance(requestId);
        await automationTerminalEntriesRef.current.get(requestId)?.pollTerminal();
      },
      onStableTerminal: async (result: RequestStatusResult) => {
        if(result.status !== 'terminal') throw new Error('Stable terminal callback requires terminal status');
        await automationInput.lifecycle.onStableTerminal(result);
        if(automationInput.isAccepted()) commitPageAcceptance(result.requestId);
        automationTerminalEntriesRef.current.get(result.requestId)?.release(result);
      },
    } : cleanupStage ? {
      onRequestAccepted: (event: { requestId: string }) => commitCleanup(event.requestId),
      onKnownRejection: (rejection) => {
        // A non-2xx is definitive even if this page attempt has gone stale;
        // neither case may fall through into an ambiguous status lookup.
        cleanupKnownRejected = true;
        settleKnownCleanupRejection({
          isCurrentAttempt: pendingAcceptanceRequestRef.current === requestId,
          terminalEntries: automationTerminalEntriesRef.current,
          requestId,
          stage: cleanupStage,
          message: rejection.message,
          onVisibleError: setAutomationPageError,
        });
      },
      onTransportEof: async () => {
        try {
          await resolveCleanupStatus();
        } catch {
          // Keep the page gate pending and expose Retry Status below.
        }
        await automationTerminalEntriesRef.current.get(requestId)?.pollTerminal();
      },
      onStableTerminal: async (result: { requestId: string }) => {
        await commitCleanup(result.requestId);
        automationTerminalEntriesRef.current.get(result.requestId)?.release({
          requestId: result.requestId,
          status: 'terminal',
        });
      },
    } : undefined;
    const boundAgentSessionId = automationInput?.binding.contextId;
    if(settings.agentMode
      && agentToolState.pairedSessionId
      && boundAgentSessionId
      && agentToolState.pairedSessionId !== boundAgentSessionId) {
      setAutomationPageError('Vision automation context does not match the current Agent session pair.');
      return;
    }
    const effectiveAgentSessionId = settings.agentMode
      ? boundAgentSessionId
      ?? agentToolState.pairedSessionId
      ?? agentSessionIdRef.current
      ?? resolvePairedAgentSessionId(agentToolState, createAgentSessionId)
      : undefined;
    const toolCandidate = settings.agentMode
      ? buildAgentToolConfigurationCandidate({ managerAgentTools: settings.managerAgentTools, basicAgentTools: settings.basicAgentTools }, agentToolCatalog)
      : undefined;
    if(effectiveAgentSessionId && toolCandidate && !agentToolState.sessions[effectiveAgentSessionId]) {
      dispatchAgentToolState({ type: 'begin', sessionId: effectiveAgentSessionId, requestId, candidate: toolCandidate });
    }
    if(effectiveAgentSessionId) bindAgentSessionId(effectiveAgentSessionId);
    const bookmarkTransaction = bookmarkTransactionsRef.current.begin({
      sessionId: settings.agentMode ? effectiveAgentSessionId! : effectiveChatSessionId!,
      pageMode: 'vision',
      interactionMode: settings.agentMode ? 'agent' : 'chat',
      createdAt: Date.now(),
    });
    const isCurrentLiveAttachment = () => settings.agentMode
      ? isCurrentSessionHubAttachment(liveAgentAttachment, liveAgentAttachmentsRef.current)
      : isCurrentSessionHubAttachment(liveChatAttachment, liveChatAttachmentsRef.current);
    setRecoveryCandidate(selectSessionHubRecoveryCandidate('vision'));
    const displayDetachedChatError = (error: Error) => {
      if(!pageCommitted) {
        setAutomationPageError(error.message);
        return;
      }
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
    const finalizeDetachedChat = () => {
      stopStreamingMessage();
      automationTerminalGate?.markCandidateComplete();
      resetStreamingState();
    };
    const canonicalHubLifecycle = createAutomationHubLifecycle({
      requestId,
      onAccepted: async (acceptedRequestId) => {
        if(settings.agentMode) {
          if(!liveAgentAttachment || !liveAgentAttachmentsRef.current.isCurrent(liveAgentAttachment)) return;
        } else if(liveChatAttachment
          && !liveChatAttachmentsRef.current.isCurrent(liveChatAttachment)) return;
        await lifecycleCallbacks?.onRequestAccepted?.({
          type: 'request_accepted',
          requestId: acceptedRequestId,
        });
        const interactionMode = settings.agentMode ? 'agent' : 'chat';
        const sessionId = settings.agentMode
          ? (automationInput?.binding.contextId ?? agentSessionIdRef.current)
          : effectiveChatSessionId;
        if(!sessionId) return;
        bookmarkTransactionsRef.current.accept(bookmarkTransaction);
        if(interactionMode === 'chat') setChatSessionId(sessionId);
        setAttachedRecoverySessionId(sessionId);
        setRecoveryCandidate(selectSessionHubRecoveryCandidate('vision'));
      },
      onStableTerminal: async (result) => {
        localMemoryRequestsRef.current.delete(result.requestId);
        chatHistoryRequestsRef.current.delete(result.requestId);
        if(!isCurrentLiveAttachment()) return;
        await lifecycleCallbacks?.onStableTerminal?.(result);
        // Acceptance-bound automation releases the attachment only after the
        // terminal gate validates ownership; otherwise the stable terminal is
        // the final owner callback for this ordinary transport.
        if(!automationTerminalGate) {
          if(liveChatAttachment) liveChatAttachmentsRef.current.detach(liveChatAttachment, () => undefined);
          if(liveAgentAttachment) liveAgentAttachmentsRef.current.detach(liveAgentAttachment, () => undefined);
        }
        bookmarkTransactionsRef.current.retain(bookmarkTransaction);
      },
    });
    const sessionLifecycleCallbacks: RequestLifecycleCallbacks = {
      ...lifecycleCallbacks,
      onRequestAccepted: async (event) => {
        if(!isCurrentLiveAttachment()) return;
        await canonicalHubLifecycle.onRequestAccepted(event);
      },
      onKnownRejection: async (rejection) => {
        if(!acceptanceBound) { localMemoryRequestsRef.current.delete(requestId); chatHistoryRequestsRef.current.delete(requestId); }
        if(!isCurrentLiveAttachment()) return;
        await lifecycleCallbacks?.onKnownRejection?.(rejection);
        bookmarkTransactionsRef.current.rollback(bookmarkTransaction);
        setRecoveryCandidate(selectSessionHubRecoveryCandidate('vision'));
        if(liveChatAttachment) {
          liveChatAttachmentsRef.current.applyError(liveChatAttachment, () => {
            displayDetachedChatError(new Error(rejection.message));
          });
          liveChatAttachmentsRef.current.detach(liveChatAttachment, () => {
            finalizeDetachedChat();
          });
        }
        if(liveAgentAttachment) {
          liveAgentAttachmentsRef.current.applyError(liveAgentAttachment, () => {
            displayDetachedChatError(new Error(rejection.message));
          });
          liveAgentAttachmentsRef.current.detach(liveAgentAttachment, () => undefined);
        }
      },
      onDetached: async (error, context) => {
        if(!isCurrentLiveAttachment()) return;
        await lifecycleCallbacks?.onDetached?.(error, context);
        if(!isCurrentLiveAttachment()) return;
        bookmarkTransactionsRef.current.retain(bookmarkTransaction);
        if(liveChatAttachment
          && liveChatAttachmentsRef.current.isCurrent(liveChatAttachment)) {
          setAttachedRecoverySessionId(null);
        }
        if(liveChatAttachment) {
          if(error) {
            liveChatAttachmentsRef.current.applyError(liveChatAttachment, () => {
              displayDetachedChatError(error);
            });
          }
          liveChatAttachmentsRef.current.detach(liveChatAttachment, () => {
            finalizeDetachedChat();
          });
        }
        if(liveAgentAttachment) {
          if(error && !context?.intentionalInterruption) {
            liveAgentAttachmentsRef.current.applyError(liveAgentAttachment, () => {
              if(!pageCommitted) setAutomationPageError(error.message);
              else setRecoveryError(classifyAgentSessionRecoveryError(error));
            });
          }
          liveAgentAttachmentsRef.current.detach(liveAgentAttachment, () => {
            finalizeDetachedChat();
          });
        }
      },
      onStableTerminal: canonicalHubLifecycle.onStableTerminal,
    };

    try {
      if(settings.agentMode) {
        // ====== Agent 模式 ======
        // Automation wire text can contain a generated capture manifest.
        // Only original user-authored text makes this request eligible for lookup.


        const effectiveSessionId = effectiveAgentSessionId!;
        const liveRunRef: TextAgentSessionRunRef = { current: agentRun };
        // Automation terminal ownership remains page-local. The shared handler
        // projects every Hub payload; while an automation gate is pending it
        // must not consume the gate's terminal cleanup.
        const handlerActiveRunRef = automationTerminalGate ? { current: null } : activeAgentRunRef;
        const agentHandlers = createTextAgentSessionHandlers({
          activeRunRef: handlerActiveRunRef,
          setMessages: (update) => setMessages(update),
          setAgentSessionId: (sessionId) => { if(sessionId) bindAgentSessionId(sessionId); },
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
        await runAgentConversationTransport({          
turn: { pageMode: 'vision', requestId, controller, interactionMode: 'agent', sessionId: effectiveSessionId, historyMessages: historyMessages, userContent: buildMultimodalContent(wireUserText, currentImages), agentText: wireUserText || '(图片已附加，请分析)', images: currentImages, visionAutomation: automationInput?.visionAutomation ?? cleanupStage?.visionAutomation, preserveFullHistory: Boolean(automationInput || cleanupStage), hasUserText: trimmedInput.trim().length > 0, userMessage, assistantMessage, agentRun }, settings, maxContextLength, localMemoryRequestsRef, chatHistoryRequestsRef, localMemoryEnabled: settingsRef.current.enableLocalMemory, environment: normalAgentEnvironment, handlers: agentHandlers, toolConfiguration: agentToolState.sessions[effectiveSessionId] ? undefined : toolCandidate, isCurrent: () => Boolean(liveAgentAttachment && liveAgentAttachmentsRef.current.isCurrent(liveAgentAttachment)), onDone: (sessionId) => {
            if(!liveAgentAttachment || !liveAgentAttachmentsRef.current.isCurrent(liveAgentAttachment)) return;
            assertBoundAgentSessionId(effectiveSessionId, sessionId);
            agentHandlers.onDone(sessionId);
            automationTerminalGate?.markCandidateComplete();
          }, onError: (error) => {
            if(!liveAgentAttachment || !liveAgentAttachmentsRef.current.isCurrent(liveAgentAttachment)) return;
            if(!pageCommitted) {
              setAutomationPageError(error.message);
              return;
            }
            agentHandlers.onPayloadError(error);
            automationTerminalGate?.markCandidateComplete();
          }, onSessionStart: (sessionId) => {
            if(!liveAgentAttachment || !liveAgentAttachmentsRef.current.isCurrent(liveAgentAttachment)) return;
            assertBoundAgentSessionId(effectiveSessionId, sessionId);
            agentHandlers.onSessionStart(sessionId);
          }, lifecycle: {
            ...sessionLifecycleCallbacks,
            onRequestAccepted: async (event) => {
              if(!liveAgentAttachment || !liveAgentAttachmentsRef.current.isCurrent(liveAgentAttachment)) return;
              await sessionLifecycleCallbacks.onRequestAccepted?.(event);
            },
            onKnownRejection: async (rejection) => {
              if(!liveAgentAttachment || !liveAgentAttachmentsRef.current.isCurrent(liveAgentAttachment)) return;
              await sessionLifecycleCallbacks.onKnownRejection?.(rejection);
              const { isToolConfigurationError, visibleMessage } = rejectConversationToolAdmission({
                state: conversationState,
                sessionId: effectiveSessionId,
                requestId,
                rejection,
                isCurrent: () => Boolean(liveAgentAttachment && liveAgentAttachmentsRef.current.isCurrent(liveAgentAttachment)),
              });
              if(isToolConfigurationError) setAutomationPageError(visibleMessage);
              if(!pageCommitted) setAutomationPageError(visibleMessage);
              else agentHandlers.onPayloadError(new Error(visibleMessage));
            },
            onDetached: (error?) => {
              if(!liveAgentAttachment || !liveAgentAttachmentsRef.current.isCurrent(liveAgentAttachment)) return;
              const intentionalInterruption = captureAgentInterruptionIntent({
                stopWasClicked: stopWasClickedRef.current,
                answerNowWasClicked: answerNowWasClickedRef.current,
              });
              void sessionLifecycleCallbacks.onDetached?.(error, { intentionalInterruption });
              if(intentionalInterruption) return;
              setAttachedRecoverySessionId(null);
              agentHandlers.onDetached();
              agentSessionOwnershipRef.current.detach();
              if(error) setRecoveryError(classifyAgentSessionRecoveryError(error));
            },
          }        
});
      } else {
        // ====== 普通聊天模式 ======
        await runChatConversationTransport({          
turn: { pageMode: 'vision', requestId, controller, interactionMode: 'chat', sessionId: effectiveChatSessionId!, historyMessages: historyMessages, userContent: buildMultimodalContent(wireUserText, currentImages), agentText: wireUserText || '(图片已附加，请分析)', images: currentImages, visionAutomation: automationInput?.visionAutomation ?? cleanupStage?.visionAutomation, preserveFullHistory: Boolean(automationInput || cleanupStage), hasUserText: trimmedInput.trim().length > 0, userMessage, assistantMessage, agentRun }, settings, maxContextLength, localMemoryRequestsRef, chatHistoryRequestsRef, localMemoryEnabled: settingsRef.current.enableLocalMemory, runtimeContext: effectiveRuntimeTimeContext, setMessages, newlyCompactMarkedRef, isCurrent: () => Boolean(liveChatAttachment && liveChatAttachmentsRef.current.isCurrent(liveChatAttachment)), onDone: () => {
            localMemoryRequestsRef.current.delete(requestId);
            chatHistoryRequestsRef.current.delete(requestId);
            if(!liveChatAttachment
              || !liveChatAttachmentsRef.current.isCurrent(liveChatAttachment)) return;
            if(automationTerminalGate === null) {
              liveChatAttachmentsRef.current.detach(liveChatAttachment, () => undefined);
              bookmarkTransactionsRef.current.retain(bookmarkTransaction);
            }
            setMessages((prev: Message[]) => prev.map((msg) => (
              msg.id === assistantMessage.id ? { ...msg, chatResume: undefined } : msg
            )));
            const terminalBehavior = getStreamingTerminalBehavior({
              stopWasClicked: stopWasClickedRef.current,
              answerNowWasClicked: answerNowWasClickedRef.current,
            });
            stopStreamingMessage();
            automationTerminalGate?.markCandidateComplete();
            if(automationTerminalGate === null && terminalBehavior.resetStreamingState) {
              resetStreamingState();
            }
            if(automationTerminalGate === null && terminalBehavior.resetStoppedState) {
              setStoppedMessageId(null);
            }
            if(automationTerminalGate === null && terminalBehavior.clearAnswerNowFlag) {
              answerNowWasClickedRef.current = false;
            }
            if(automationTerminalGate === null) stopWasClickedRef.current = false;
          }, onError: (error: Error) => {
            if(!liveChatAttachment) return;
            console.error('聊天错误:', error);
            liveChatAttachmentsRef.current.applyError(liveChatAttachment, () => {
              displayDetachedChatError(error);
            });
          }, lifecycle: sessionLifecycleCallbacks        
});
      }
      if(cleanupStage && !cleanupCommitted && !cleanupDiscarded && !cleanupKnownRejected) {
        await resolveCleanupStatus();
      }
    } catch(error) {
      console.error('请求失败:', error);
      if(!pageCommitted) {
        setAutomationPageError(error instanceof Error ? error.message : String(error));
        return;
      }
      const terminalBehavior = getStreamingTerminalBehavior({
        stopWasClicked: stopWasClickedRef.current,
        answerNowWasClicked: answerNowWasClickedRef.current,
      });
      stopStreamingMessage();
      automationTerminalGate?.markCandidateComplete();
      if(automationTerminalGate === null && terminalBehavior.resetStreamingState) {
        resetStreamingState();
      }
      if(automationTerminalGate === null && terminalBehavior.resetStoppedState) {
        setStoppedMessageId(null);
      }
      if(automationTerminalGate === null && terminalBehavior.clearAnswerNowFlag) {
        answerNowWasClickedRef.current = false;
      }
      if(automationTerminalGate === null) stopWasClickedRef.current = false;
    } finally {
      if(cleanupStage && !cleanupCommitted && !cleanupDiscarded) {
        cleanupStage.finishAttempt();
      }
      if(pendingAcceptanceRequestRef.current === requestId) {
        pendingAcceptanceRequestRef.current = null;
        setHasPendingAcceptance(false);
      }
      if(pageCommitted) acceptanceMutationsRef.current.delete(requestId);
      if(!pageCommitted && automationTerminalGate) {
        disposeAutomationTerminalEntry(
          automationTerminalEntriesRef.current,
          requestId,
          automationTerminalGate,
        );
      }
      if(!pageCommitted && liveAgentAttachment
        && liveAgentAttachmentsRef.current.isCurrent(liveAgentAttachment)) {
        abortControllerRef.current = null;
      }
    }
  }, [
    input,
    images,
    messages,
    settings,
    connectionStatus,
    agentEntryAgentName,
    agentToolCatalog,
    agentToolState,
    runtimeContextConfig.enable_runtime_time_context,
    runtimeTimeContext,
    buildRuntimeTimeContext,
    maxContextLength,
    buildMultimodalContent,
    agentSessionId,
    chatSessionId,
    bindAgentSessionId,
    resetStreamingState,
    stopStreamingMessage,
    visionAutomation,
    isVisionRequestBusyNow,
    visionCapabilityConfirmed,
  ]);

  useLayoutEffect(() => {
    handleSendRef.current = handleSend;
    automationSubmitRef.current = (automationInput) => handleSend(undefined, undefined, automationInput);
  }, [handleSend]);

  const sendConversationInput = (text?: string, replay?: Message['voiceReplay'], source?: string) => handleSend(text, replay, undefined, source);
  const { voiceController, dictationMagicSendPending, handleVoiceModeChange, insertDictationTextAtCursor } = useConversationVoice({ state: conversationState, send: sendConversationInput, busy: visionRequestBusy, pendingAcceptance: hasPendingAcceptance, cycleRunning: cycleStatus === 'running' });
  const { handleMessageTemplatesChange, handleRestoreMessageTemplates, handleMessageTemplateImmediateSendChange, handleInsertMessageTemplate, handleSendMessageTemplate, filteredSlashCommands, disabledCommandMap, handleSelectSlashCommand, handleSubmit, handleKeyDown, onInputChange } = useConversationCommands({ state: conversationState, pageMode: 'vision', busy: visionRequestBusy, cycleStatus, send: sendConversationInput, voiceController, buildRuntimeTimeContext });

  // 听写：把最终转写插入输入框光标处（未聚焦则追加到末尾）

  // 听写兜底：auto_send=false 或空转写时 hook 停在 transcript_ready，这里落字并立即重开麦（持续听写）

  // 处理消息队列：当加载完成后，自动发送队列中的下一条消息


  /**
   * 处理输入框按键
   */

  const ensureAgentSessionId = useCallback(() => {
    const existing = agentSessionIdRef.current;
    if(existing) return existing;
    const allocated = createAgentSessionId();
    return bindAgentSessionId(allocated);
  }, [bindAgentSessionId]);

  const handleRefreshAutomationCameras = useCallback(async () => {
    try {
      browserMediaSourceRegistry.clearDeviceCache();
      const cameras = await browserMediaSourceRegistry.enumerateCameraDevices(true);
      const descriptors: SourceDescriptor[] = cameras.map((camera, index) => ({
        sourceId: `camera:${camera.deviceId}`,
        kind: 'camera',
        stableAlias: `camera_${index + 1}`,
        label: camera.label,
        deviceId: camera.deviceId,
      }));
      setAvailableAutomationSources([
        { sourceId: 'desktop', kind: 'display', stableAlias: 'desktop', label: 'Desktop' },
        ...descriptors,
      ]);
      setAutomationPageError(null);
    } catch(error) {
      setAutomationPageError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  const handleAutomationConfigChange = useCallback((patch: Partial<VisionAutomationServerConfig>) => {
    if(!automationConfig || visionAutomation.state.armed) return;
    try {
      const next = validateVisionAutomationServerConfig({ ...automationConfig, ...patch });
      setAutomationConfig(next);
      saveVisionAutomationPreferences(next, window.localStorage);
      setAutomationPageError(null);
    } catch(error) {
      setAutomationPageError(error instanceof Error ? error.message : String(error));
    }
  }, [automationConfig, visionAutomation.state.armed]);

  const handleAutomationStart = useCallback(async () => {
    if(!pageGenerationReady || visionAutomation.controller.hasCleanupSendInFlight() || !canStartVisionAutomation({
      enabled: Boolean(automationConfig?.enabled),
      cycleRunning: cycleStatus === 'running',
      startGuardActive: cycleStartGuardRef.current.active,
      armed: visionAutomation.state.armed,
      modelBusy: isVisionRequestBusyNow(),
      pendingAcceptance: pendingAcceptanceRequestRef.current !== null,
    })) return;
    const effectiveAgentSessionId = settings.agentMode ? ensureAgentSessionId() : undefined;
    const selected = availableAutomationSources.filter((source) => (
      selectedAutomationSourceIds.has(source.sourceId)
    ));
    setActiveAutomationSources(selected);
    const automationBinding: AutomationBinding = {
      pageInstanceId: pageInstanceIdRef.current,
      pageGeneration: pageLifecycle.generation,
      mode: settings.agentMode ? 'agent' : 'chat',
      contextId: settings.agentMode
        ? effectiveAgentSessionId!
        : chatSessionId ?? pageLifecycle.contextId,
      automationTaskId: createAutomationLifecycleId('automation-task'),
    };
    setAutomationPageError(null);
    try {
      await visionAutomation.start(automationBinding);
    } catch(error) {
      setAutomationPageError(error instanceof Error ? error.message : String(error));
    }
  }, [
    automationConfig?.enabled,
    availableAutomationSources,
    cycleStatus,
    ensureAgentSessionId,
    pageGenerationReady,
    selectedAutomationSourceIds,
    settings.agentMode,
    chatSessionId,
    visionAutomation,
    isVisionRequestBusyNow,
  ]);

  const handleAutomationAbort = useCallback(async () => {
    await visionAutomation.abort();
    setActiveAutomationSources([]);
    setAutomationPageError(visionAutomation.controller.getRecoveryError());
  }, [visionAutomation]);

  const handleAutomationRetryFailedSource = useCallback(async () => {
    const failed = visionAutomation.controller.getFailedSourceIdentity();
    if(!failed) return;
    const restored = await visionAutomation.retryFailedSource(failed);
    if(!restored) setAutomationPageError(visionAutomation.controller.getState().lastError);
  }, [visionAutomation]);

  const abortAutomationAndInvalidate = useCallback(async () => {
    pageLifecycle.invalidate();
    disposeAutomationTerminalEntries(automationTerminalEntriesRef.current);
    setActiveAutomationSources([]);
    if(visionAutomation.controller.getState().armed) {
      await visionAutomation.controller.abort({ registerAgentCleanup: false });
    }
    const cleanup = visionAutomation.controller.getState().cleanupPending;
    if(cleanup) {
      await visionAutomation.controller.cancelCleanup(cleanup, { destroyContext: true });
    }
  }, [visionAutomation]);

  useEffect(() => {
    const controller = visionAutomation.controller;
    const cancelTransientWork = () => {
      cancelCycleStart();
      acceptanceMutationsRef.current.clear();
      disposeAutomationTerminalEntries(automationTerminalEntriesRef.current);
      pendingAcceptanceRequestRef.current = null;
      setHasPendingAcceptance(false);
      abortControllerRef.current?.abort(new DOMException('Vision page lifecycle invalidated', 'AbortError'));
      setActiveAutomationSources([]);
    };
    const abortContext = async () => {
      if(controller.getState().armed) {
        await controller.abort({ registerAgentCleanup: false });
      }
      const cleanup = controller.getState().cleanupPending;
      if(cleanup) await controller.cancelCleanup(cleanup, { destroyContext: true });
    };
    const handlePageHide = () => {
      cancelTransientWork();
      void pageLifecycle.pageHide({ abort: abortContext, dispose: () => controller.dispose() });
    };
    window.addEventListener('pagehide', handlePageHide);
    return () => {
      window.removeEventListener('pagehide', handlePageHide);
      pageLifecycleCleanupTimerRef.current = window.setTimeout(() => {
        pageLifecycleCleanupTimerRef.current = null;
        cancelTransientWork();
        void pageLifecycle.unmount({ abort: abortContext, dispose: () => controller.dispose() });
      }, 0);
    };
  }, [visionAutomation.controller]);

  /**
   * 清空对话
   */
  const handleClear = () => clearConversation({ pageMode: 'vision', state: conversationState, voice: voiceController, cancelPendingCycleStart: cancelCycleStart, resetDraft: () => { setMessageQueue([]); setImages([]); }, detachSession: () => { const detached = agentSessionOwnershipRef.current.detach()?.sessionId ?? agentSessionIdRef.current; agentSessionIdRef.current = undefined; return detached; }, detachLocal: abortAutomationAndInvalidate, afterClear: () => { acceptanceMutationsRef.current.clear(); pendingAcceptanceRequestRef.current = null; setHasPendingAcceptance(false); }, });

  /**
   * 停止当前聊天（普通模式）
   */
  const handleStopChat = useCallback(async () => {
    stopWasClickedRef.current = true;
    try {
      await executeVisionStop({
        mode: 'chat',
        terminalEntries: automationTerminalEntriesRef.current,
        requestIdRef: currentRequestIdRef,
        transportRef: abortControllerRef,
        finalizeVisual: () => finalizeConversationStop(conversationState, 'chat'),
        resetStreamingState,
        stopChat,
        stopAgent,
      });
      setAutomationPageError(null);
    } catch(error) {
      stopWasClickedRef.current = false;
      if (recoveryCoordinatorRef.current.currentAttachmentToken()) setRecoveryError(classifyAgentSessionRecoveryError(error instanceof Error ? error : new Error(String(error))));
      console.error('停止聊天失败:', error);
      setAutomationPageError(`停止聊天失败，请重试：${error instanceof Error ? error.message : String(error)}`);
    }
  }, [messages, resetStreamingState, stopStreamingMessage]);

  /**
   * 停止当前 Agent 执行
   */
  const handleStopAgent = useCallback(async () => {
    console.log('[STOP] handleStopAgent called, agentSessionId:', agentSessionId, 'isLoading:', isLoading);
    // Legacy recovered journals may have only a session identity. Agent Stop keeps
    // its existing session target and optional expected_request_id contract.
    if (recoveryCoordinatorRef.current.currentAttachmentToken() && !currentRequestIdRef.current && agentSessionId) {
      await stopOrdinaryConversation(conversationState, 'agent', resetStreamingState);
      return;
    }
    stopWasClickedRef.current = true;
    try {
      await executeVisionStop({
        mode: 'agent',
        agentSessionId,
        terminalEntries: automationTerminalEntriesRef.current,
        requestIdRef: currentRequestIdRef,
        transportRef: abortControllerRef,
        finalizeVisual: () => finalizeConversationStop(conversationState, 'agent'),
        resetStreamingState,
        stopChat,
        stopAgent,
      });
      setAutomationPageError(null);
    } catch(error) {
      stopWasClickedRef.current = false;
      if (recoveryCoordinatorRef.current.currentAttachmentToken()) setRecoveryError(classifyAgentSessionRecoveryError(error instanceof Error ? error : new Error(String(error))));
      console.error('停止 Agent 失败:', error);
      setAutomationPageError(`停止 Agent 失败，请重试：${error instanceof Error ? error.message : String(error)}`);
    }
  }, [agentSessionId, isLoading, resetStreamingState, stopStreamingMessage]);

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
        const answerNowRequestId = createAutomationLifecycleId('answer-now-request');
        const answerNowSessionId = agentSessionId;
        const answerNowAttachment = liveAgentAttachmentsRef.current.attach(answerNowSessionId);
        const originalAutomationRequestId = currentRequestIdRef.current;
        const originalController = abortControllerRef.current;
        const controller = new AbortController();
        const answerNowRunRef: TextAgentSessionRunRef = { current: answerNowRun };
        const answerNowHandlers = createTextAgentSessionHandlers({
          activeRunRef: activeAgentRunRef,
          setMessages: (update) => setMessages(update),
          setAgentSessionId: (sessionId) => { if (sessionId) bindAgentSessionId(sessionId); },
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
        const automationReplacementLifecycle = visionAutomation.controller.getState().activeRequestId
          ? visionAutomation.controller.beginAnswerNowReplacement(
              answerNowRequestId,
              controller.signal,
            )
          : undefined;
        abortAcceptedVisionInference(originalController);
        const replacementAutomationTerminalEntry = automationReplacementLifecycle
          ? (() => {
              const gate = new AutomationTerminalGate(answerNowRequestId);
              const entry = {
              gate,
              transport: controller,
              release: (result: RequestStatusResult) => {
                const entry = automationTerminalEntriesRef.current.get(answerNowRequestId);
                if (entry?.gate !== gate) return;
                automationTerminalEntriesRef.current.delete(answerNowRequestId);
      const completed = completeAutomationTerminal(
                  gate,
                  result,
                  undefined,
                  () => {
                    answerNowHandlers.onDetached();
                    resetStreamingState();
                    setIsAnswerNowLoading(false);
                    answerNowWasClickedRef.current = false;
                    stopWasClickedRef.current = false;
                    activeAgentRunRef.current = null;
                    setAutomationPageError(null);
                  },
                  answerNowAttachment,
                  (token) => liveAgentAttachmentsRef.current.isCurrent(token),
                );
                setAutomationTerminalRevision((revision) => revision + 1);
              },
              pollTerminal: async () => {
                const outcome = await gate.pollForTerminal(
                  (gateRequestId, signal) => llmClient.waitForRequestTerminal(gateRequestId, { signal }),
                );
                if (outcome.kind === 'cancelled') return false;
                if (outcome.kind === 'failed') {
                  setAutomationPageError(outcome.error);
                  setAutomationTerminalRevision((revision) => revision + 1);
                  return false;
                }
                entry.release(outcome.result);
                return true;
              },
              };
              return entry;
            })()
          : undefined;
        installVisionRequestOwnership({
          terminalEntries: automationTerminalEntriesRef.current,
          previousRequestId: originalAutomationRequestId,
          requestId: answerNowRequestId,
          transport: controller,
          terminalEntry: replacementAutomationTerminalEntry,
          preservePreviousTerminalEntry: true,
          requestIdRef: currentRequestIdRef,
          transportRef: abortControllerRef,
          setCurrentRequestId,
        });
        await answerNowStream(
          agentSessionId,
          answerNowRequestId,
          () => undefined,
          () => {
            replacementAutomationTerminalEntry?.gate.markCandidateComplete();
            if (!replacementAutomationTerminalEntry) {
              resetStreamingState();
              setIsAnswerNowLoading(false);
            }
          },
          () => undefined,
          undefined,
          controller,
          automationReplacementLifecycle ? {
            onRequestAccepted: (event) => automationReplacementLifecycle.onRequestAccepted(event),
            onKnownRejection: async (rejection) => {
              if (!liveAgentAttachmentsRef.current.isCurrent(answerNowAttachment)) return;
              const restored = await automationReplacementLifecycle.onKnownRejection(rejection);
              if (!restored || !liveAgentAttachmentsRef.current.isCurrent(answerNowAttachment)) return;
              const settled = settleKnownAnswerNowRejection({
                transportToken: answerNowAttachment,
                isCurrentToken: (token) => liveAgentAttachmentsRef.current.isCurrent(token),
                run: answerNowRun,
                handlers: answerNowHandlers,
                rejection: new Error(rejection.message),
                clearAnswerNowFlag: () => { answerNowWasClickedRef.current = false; },
                setAnswerNowLoading: setIsAnswerNowLoading,
                onVisibleError: setAutomationPageError,
              });
              if (!settled) return;
              disposeAutomationTerminalEntry(
                automationTerminalEntriesRef.current,
                answerNowRequestId,
                replacementAutomationTerminalEntry?.gate,
              );
              // The original controller was superseded (and may be aborted);
              // never restore it as current ownership after rejection.
              currentRequestIdRef.current = undefined;
              setCurrentRequestId(undefined);
              setAutomationTerminalRevision((revision) => revision + 1);
            },
            onTransportEof: () => {
              void automationReplacementLifecycle.onTransportEof().catch(() => undefined);
              void automationTerminalEntriesRef.current.get(answerNowRequestId)?.pollTerminal();
            },
            onDetached: () => {
              detachRetainedAgentSessionSubscriber({
                transportToken: answerNowAttachment,
                isCurrentToken: (token) => liveAgentAttachmentsRef.current.isCurrent(token),
                boundSessionId: agentSessionIdRef.current,
                detachedSessionId: answerNowSessionId,
                detachLocal: () => {
                  answerNowHandlers.onDetached();
                  agentSessionOwnershipRef.current.detach();
                  resetStreamingState();
                  setIsAnswerNowLoading(false);
                },
              });
            },
            onStableTerminal: async (result) => {
              await automationReplacementLifecycle.onStableTerminal(result);
              automationTerminalEntriesRef.current.get(answerNowRequestId)?.release(result);
            },
          } : {
            onTransportEof: () => {
              void llmClient.waitForRequestTerminal(answerNowRequestId, { signal: controller.signal }).catch(() => undefined);
            },
            onDetached: () => {
              detachRetainedAgentSessionSubscriber({
                transportToken: answerNowAttachment,
                isCurrentToken: (token) => liveAgentAttachmentsRef.current.isCurrent(token),
                boundSessionId: agentSessionIdRef.current,
                detachedSessionId: answerNowSessionId,
                detachLocal: () => {
                  answerNowHandlers.onDetached();
                  agentSessionOwnershipRef.current.detach();
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
  }, [agentSessionId, isAnswerNowLoading, resetStreamingState, stopStreamingMessage, visionAutomation.controller]);

  /**
   * 处理图片变化（支持函数式更新，透传给 React setState 以保证拿到最新 state）
   */
  const handleImagesChange = useCallback((newImages: string[] | ((prev: string[]) => string[])) => {
    setImages(newImages);
  }, []);

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
        : visionRequestBusy && !voiceController.isVoiceMode
          ? 'Wait for the current response before starting voice input.'
          : undefined;
  const voiceDisabledReason = voiceAvailabilityDisabledReason ?? voiceController.state.error?.message;
  const voiceDisabled = Boolean(voiceAvailabilityDisabledReason) ||
    voiceController.state.error?.recoverable === false;
  const failedAutomationSource = visionAutomation.controller.getFailedSourceIdentity();
  const automationSourceHealth: VisionAutomationSourceHealth[] = activeAutomationSources.map((source) => ({
    sourceId: source.sourceId,
    status: failedAutomationSource?.sourceId === source.sourceId ? 'error' : 'healthy',
    error: failedAutomationSource?.sourceId === source.sourceId
      ? visionAutomation.state.lastError
      : undefined,
  }));
  const retryablePageTerminalEntries = automationTerminalRevision >= 0
    ? Array.from(automationTerminalEntriesRef.current.values()).filter(
      (entry) => entry.gate.canRetryStatus,
    )
    : [];
  const messageTemplateMenu = (
    <MessageTemplateMenu
      actionBarVisible={showInputActions}
      templates={messageTemplates}
      yamlDefaults={yamlMessageTemplates}
      onTemplatesChange={handleMessageTemplatesChange}
      onRestoreDefaults={handleRestoreMessageTemplates}
      onInsertTemplate={handleInsertMessageTemplate}
      onSendTemplate={handleSendMessageTemplate}
      immediateSend={messageTemplateImmediateSend}
      onImmediateSendChange={handleMessageTemplateImmediateSendChange}
      sendDisabled={
        visionRequestBusy
        || hasPendingAcceptance
        || connectionStatus === 'error'
        || cycleStatus === 'running'
      }
      storageError={messageTemplateStorageError}
      onDismissStorageError={() => setMessageTemplateStorageError(null)}
    />
  );
  const automationPanel = automationConfig ? (
    <VisionAutomationPanel
      phase={visionAutomation.state.phase}
      armed={visionAutomation.state.armed}
      collapsed={automationPanelCollapsed}
      cycleRunning={cycleStatus === 'running'}
      config={visionAutomation.activeConfig ?? automationConfig}
      availableSources={availableAutomationSources}
      selectedSourceIds={selectedAutomationSourceIds}
      activeSources={activeAutomationSources}
      bundles={visionAutomation.bundles}
      sourceHealth={automationSourceHealth}
      stats={{
        attemptedTicks: visionAutomation.stats.attemptedTicks,
        successfulTicks: visionAutomation.stats.successfulTicks,
        failedTicks: visionAutomation.stats.failedTicks,
        imageCount: visionAutomation.stats.imageCount,
        estimatedEncodedBytes: visionAutomation.stats.encodedBytes,
      }}
      error={automationPageError ?? visionAutomation.controller.getRecoveryError() ?? visionAutomation.state.lastError}
      onCollapsedChange={setAutomationPanelCollapsed}
      onSourceSelectionChange={(sourceId, selected) => {
        setSelectedAutomationSourceIds((current) => {
          const next = new Set(current);
          if(selected) next.add(sourceId);
          else next.delete(sourceId);
          return next;
        });
      }}
      onConfigChange={handleAutomationConfigChange}
      startDisabled={!pageGenerationReady}
      onStart={() => { void handleAutomationStart(); }}
      onAbort={() => { void handleAutomationAbort(); }}
      onRetrySources={() => {
        void visionAutomation.retrySources().then((started) => {
          if(!started) setAutomationPageError(visionAutomation.controller.getState().lastError);
        });
      }}
      onRetryFailedSource={() => { void handleAutomationRetryFailedSource(); }}
      canRetryStatus={
        retryablePageTerminalEntries.length > 0
        || visionAutomation.controller.canRetryStatus()
      }
      canRetrySubmit={visionAutomation.controller.canRetrySubmit()}
      onRetryStatus={() => {
        const recoveryRequestId = visionAutomation.controller.getRecoverableRequestId();
        void Promise.all(
          retryablePageTerminalEntries.map((entry) => entry.pollTerminal()),
        ).then(async () => {
          if(visionAutomation.controller.canRetryStatus()) {
            await visionAutomation.retryStatus();
          }
          if(recoveryRequestId
            && acceptanceMutationsRef.current.isAccepted(recoveryRequestId)) {
            acceptanceMutationsRef.current.delete(recoveryRequestId);
          }
          const pendingGateError = Array.from(automationTerminalEntriesRef.current.values())
            .find((entry) => entry.gate.canRetryStatus)?.gate.pollError;
          setAutomationPageError(
            pendingGateError ?? visionAutomation.controller.getRecoveryError(),
          );
        });
      }}
      onRetrySubmit={() => {
        void visionAutomation.retrySubmit().then(() => {
          setAutomationPageError(visionAutomation.controller.getRecoveryError());
        });
      }}
      onRefreshCameras={() => { void handleRefreshAutomationCameras(); }}
    />
  ) : null;

  const handleConfirmSettings = () => confirmConversationSettings(conversationState, { resetStreamingState, canChangeMode: () => { if(visionAutomation.state.armed) { setAutomationPageError('Abort Vision automation before changing Chat/Agent mode.'); return false; } return true; }, invalidateMode: abortAutomationAndInvalidate, });

  return (
    <div className={`app-shell flex flex-col overflow-y-auto bg-gray-50 dark:bg-gray-900 ${appSkinClass}`} style={buildWebUIStyleVars(webUIConfig)} ref={messageScrollContainerRef}>
      {/* 头部 */}
      <ConversationHeader pageMode="vision" webUIConfig={webUIConfig} modelName={modelName} agentMode={settings.agentMode} connectionStatus={connectionStatus} inputMode={inputMode} onVoiceModeChange={handleVoiceModeChange} voiceDisabled={voiceDisabled} voiceDisabledReason={voiceDisabledReason} canRecover={Boolean(recoveryBookmark && attachedRecoverySessionId !== recoveryBookmark.sessionId)} onRecover={restoreAgentSession} enableThinking={settings.enableThinking} onToggleThinking={() => setSettings(prev => ({ ...prev, enableThinking: !prev.enableThinking }))} enableLocalMemory={settings.enableLocalMemory} onToggleLocalMemory={enabled => setSettings(prev => ({ ...prev, enableLocalMemory: enabled }))} onOpenSettings={() => { setPendingSkinId(selectedSkinId); setPendingSettings({ ...settings }); setShowSettings(true); }} onClear={handleClear} />

      {recoveryError && (
        <div className="mx-auto mt-3 flex max-w-4xl items-center gap-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-100">
          <span>{recoveryError.message}</span>
          {recoveryError.kind === 'expired' ? (
            <button type="button" className="underline" onClick={() => {
              clearSessionHubRecoveryState('vision');
              setRecoveryCandidate(null);
              setRecoveryError(null);
            }}>Dismiss</button>
          ) : (
            <button type="button" className="underline" onClick={restoreAgentSession}>Retry</button>
          )}
        </div>
      )}

      {/* 设置对话框 */}
      <ConversationSettingsDialog open={showSettings} pendingSettings={pendingSettings} setPendingSettings={setPendingSettings} settingsTab={settingsTab} setSettingsTab={setSettingsTab} pendingSkinId={pendingSkinId} setPendingSkinId={setPendingSkinId} maxContextLength={maxContextLength} agentToolCatalog={agentToolCatalog} sessionRecord={agentToolState.pairedSessionId ? agentToolState.sessions[agentToolState.pairedSessionId] ?? null : null} toolNotice={agentToolNotice} visionAutomationArmed={visionAutomation.state.armed} resetDisabled={!serverDefaults || Boolean(agentToolState.pairedSessionId && ['pending', 'locked'].includes(agentToolState.sessions[agentToolState.pairedSessionId]?.kind ?? ''))} onReset={() => { if(serverDefaults) { setPendingSettings(serverDefaults); setPendingSkinId(DEFAULT_WEBUI_SKIN_ID); } }} onCancel={() => { setShowSettings(false); setPendingSettings(null); }} onDismiss={() => setShowSettings(false)} onConfirm={handleConfirmSettings} />

      {/* 消息列表 */}
      <ConversationMessageList messages={messages} contentRef={messageContentRef} webUIConfig={webUIConfig} settings={settings} bubbleCharacterLimit={bubbleCharacterLimit} foldThinkingAfterFinalAnswer={foldThinkingAfterFinalAnswer} stoppedMessageIds={trackedStoppedMessageIds} collapsedCompact={collapsedCompact} onToggleCompact={id => setCollapsedCompact(previous => { const next = new Set(previous); if(next.has(id)) next.delete(id); else next.add(id); return next; })} onToggleReasoning={id => { manuallyToggledReasoningIdsRef.current.add(id); setMessages(previous => updateTrackedAssistantMessage(previous, id, target => ({ ...target, reasoningCollapsed: !(target.reasoningCollapsed ?? !settings.showReasoning) }))); }} onOpenImage={setLightboxSrc} getUserText={getVisibleVisionUserText} emptyContent={(
        <div className="text-center py-20">
          {welcomeConfig.iconImagePath ? (
            <img
              src={welcomeConfig.iconImagePath}
              alt="Vision welcome icon"
              className="mx-auto mb-4 max-h-64 max-w-full object-contain"
            />
          ) : (
            <div className="w-16 h-16 bg-nvidia-500 rounded-2xl flex items-center justify-center mx-auto mb-4">
              <ImageIcon className="w-8 h-8 text-white" />
            </div>
          )}
          <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">
            {welcomeConfig.title}
          </h2>
          <p className="text-gray-500 dark:text-gray-400 max-w-md mx-auto">
            {welcomeConfig.description}
          </p>
        </div>
      )} />

      {/* 输入区域 */}
      <ConversationComposer footerRef={messageFooterRef} webUIConfig={webUIConfig} busy={visionRequestBusy} cycleRunning={cycleStatus === 'running'} connectionError={connectionStatus === 'error'} sendDisabled={(voiceController.isVoiceMode && inputMode === 'voice'
        ? !voiceController.state.pendingTranscript.trim()
        : !input.trim() && images.length === 0) ||
        visionRequestBusy ||
        connectionStatus === 'error'} input={input} inputRef={inputRef} inputMode={inputMode} voiceController={voiceController} showInputActions={showInputActions} onToggleInputActions={() => setShowInputActions(current => !current)} onInputChange={onInputChange} onKeyDown={handleKeyDown} onSubmit={handleSubmit} onStop={() => { if(settings.agentMode) void handleStopAgent(); else void handleStopChat(); }} onSwitchToKeyboard={() => void handleVoiceModeChange('keyboard')} onOpenDictationSettings={dictationMagicSendSettings ? () => setShowDictationMagicSendSettings(true) : undefined} slashMenu={slashMenu} filteredSlashCommands={filteredSlashCommands} disabledCommandMap={disabledCommandMap} onSelectSlashCommand={handleSelectSlashCommand} onCloseSlashMenu={() => setSlashMenu({ visible: false, query: '', selectedIndex: 0 })} notices={<>{cycleValidationError ? <p role="alert" className="text-sm text-red-600">{cycleValidationError}</p> : null}{cycleRecoveryNotice ? <p role="status" className="text-sm text-amber-700">{cycleRecoveryNotice} </p> : null}</>} actions={visionCapabilityConfirmed ? <ImageUploader
          key={`uploader-${imagesKey}`}
          images={images}
          onImagesChange={handleImagesChange}
          maxImages={9}
          maxFileSize={visionConfig?.maxImageSize || 10 * 1024 * 1024}
          acceptedFormats={visionConfig?.supportedFormats || ['png', 'jpg', 'jpeg', 'gif', 'webp']}
          cameraDimension={visionConfig?.cameraDimension || 1024}
          captureDimension={visionConfig?.captureDimension || 1280}
          cycleEnabled={cycleConfig.enabled}
          cycleMaxDepth={cycleConfig.max_depth}
          cycleDefaultSteps={cycleConfig.default_sequence}
          cycleMaxScheduledMessageDays={cycleConfig.max_scheduled_message_days}
          cycleDefaultScheduledMessages={cycleConfig.default_scheduled_messages}
          cycleExecutionStatus={cycleStatus}
          cycleEvents={cycleEvents}
          onCycleStart={handleCycleStart}
          onCycleStop={handleCycleStop}
          showActionButtons={showInputActions}
          messageTemplateMenu={messageTemplateMenu}
          visionAutomationEnabled={Boolean(automationConfig?.enabled)}
          automationArmed={visionAutomation.state.armed}
          automationPanel={automationPanel}
        /> : <ConversationTextInputActions
          visible={showInputActions}
          cycleConfig={cycleConfig}
          cycleStatus={cycleStatus}
          cycleEvents={cycleEvents}
          showCycle={showCycle}
          onToggleCycle={() => setShowCycle(current => !current)}
          onCycleStart={handleCycleStart}
          onCycleStop={handleCycleStop}
          messageTemplateMenu={messageTemplateMenu}
        />} attachments={images.length > 0 && (
          <div key={`preview-${imagesKey}`} className="flex gap-2 overflow-x-auto pb-2">
            {images.map((img, idx) => (
              <div key={idx} className="relative group flex-shrink-0">
                <img
                  src={img}
                  alt={`待发送图片 ${idx + 1}`}
                  className="w-16 h-16 object-cover rounded-lg border border-gray-300 dark:border-gray-600 cursor-pointer hover:opacity-80 transition-opacity"
                  onClick={() => setLightboxSrc(img)}
                />
                {/* 删除按钮 */}
                <button
                  onClick={() => setImages(imgs => imgs.filter((_, i) => i !== idx))}
                  className="absolute -top-2 -right-2 w-5 h-5 bg-red-500 text-white rounded-full 
                      flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )} actionIndicator={<VisionAutomationStatusRing
          phase={visionAutomation.state.phase}
          armed={visionAutomation.state.armed}
          className="absolute right-0.5 top-0.5"
        />} enterToSend={settings.enterToSend} />

      {/* 图片全屏预览 */}
      <ImageLightbox src={lightboxSrc} onClose={() => setLightboxSrc(null)} />
      {dictationMagicSendSettings ? <DictationMagicSendModal open={showDictationMagicSendSettings} settings={dictationMagicSendSettings} onCancel={() => setShowDictationMagicSendSettings(false)} onConfirm={(next) => { setDictationMagicSendSettings(next); writeDictationMagicSendSettings(window.localStorage, next); setShowDictationMagicSendSettings(false); }} /> : null}
    </div>
  );
}
