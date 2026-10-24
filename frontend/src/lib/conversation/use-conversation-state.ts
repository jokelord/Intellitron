'use client';
import { useState, useRef, useReducer } from 'react';
import { DEFAULT_SETTINGS, type Message, type ChatSettings, type AgentRunState, type RuntimeContextConfig } from '../message-types.ts';
import { classifyAgentSessionRecoveryError } from '../agent-session-recovery.ts';
import { SessionHubRecoveryCoordinator, SessionHubBookmarkTransactionRegistry, SessionHubLiveAttachmentRegistry, type SessionHubRecoveryCandidate } from '../session-hub-recovery.ts';
import { DEFAULT_WEBUI_SKIN_ID, type WebUIConfig } from '../web-ui-config.ts';
import { type VoiceInputConfig } from '../voice-input.ts';
import { type DictationMagicSendSettings } from '../dictation-magic-send.ts';
import { type VoiceInputMode } from '../../components/VoiceModeToggle';
import { reduceAgentToolSessionState, EMPTY_AGENT_TOOL_SESSION_STATE, AGENT_BUSINESS_TOOL_NAMES, type AgentBusinessToolCatalog } from '../agent-tool-configuration.ts';
/** Ordinary state has one owner; deprecated feature declarations remain in the routes. */
export function useConversationState() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [stoppedMessageId, setStoppedMessageId] = useState<string | null>(null);
  const [stoppedTrackedMessageIds, setStoppedTrackedMessageIds] = useState<string[]>([]);
  const [showSettings, setShowSettings] = useState(false);
  const [settingsTab, setSettingsTab] = useState<'general' | 'advanced'>('general');
  const [settings, setSettings] = useState<ChatSettings>(DEFAULT_SETTINGS);
  const settingsRef = useRef(settings);
  const localMemoryRequestsRef = useRef(new Map<string, boolean>());
  const chatHistoryRequestsRef = useRef(new Map<string, string | undefined>());
  const [pendingSettings, setPendingSettings] = useState<ChatSettings | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<'checking' | 'connected' | 'error'>('checking');
  const [modelName, setModelName] = useState<string>('加载中...');
  const [maxContextLength, setMaxContextLength] = useState<number>(1048576);
  const [serverDefaults, setServerDefaults] = useState<ChatSettings | null>(null);
  const [runtimeContextConfig, setRuntimeContextConfig] = useState<RuntimeContextConfig>({
    enable_runtime_time_context: false,
  });
  const [webUIConfig, setWebUIConfig] = useState<WebUIConfig | undefined>(undefined);
  const [yamlMessageTemplates, setYamlMessageTemplates] = useState<string[]>([]);
  const [messageTemplates, setMessageTemplates] = useState<string[]>([]);
  const [messageTemplateImmediateSend, setMessageTemplateImmediateSend] = useState(false);
  const [messageTemplateStorageError, setMessageTemplateStorageError] = useState<string | null>(null);
  const [voiceConfig, setVoiceConfig] = useState<VoiceInputConfig | null>(null);
  const [dictationMagicSendSettings, setDictationMagicSendSettings] = useState<DictationMagicSendSettings | null>(null);
  const [showDictationMagicSendSettings, setShowDictationMagicSendSettings] = useState(false);
  const [inputMode, setInputMode] = useState<VoiceInputMode>('keyboard');
  const inputModeRef = useRef<VoiceInputMode>('keyboard');
  const [selectedSkinId, setSelectedSkinId] = useState<string>(DEFAULT_WEBUI_SKIN_ID);
  const [pendingSkinId, setPendingSkinId] = useState<string>(DEFAULT_WEBUI_SKIN_ID);
  const [runtimeTimeContext, setRuntimeTimeContext] = useState<string | null>(null);
  const [agentSessionId, setAgentSessionId] = useState<string | undefined>(undefined);
  const [agentToolState, dispatchAgentToolState] = useReducer(reduceAgentToolSessionState, EMPTY_AGENT_TOOL_SESSION_STATE);
  const [agentToolCatalog, setAgentToolCatalog] = useState<AgentBusinessToolCatalog>({
    schema_version: 1,
    catalog_revision: '',
    tools: AGENT_BUSINESS_TOOL_NAMES.map((name, order) => ({
      name,
      order
    })),
    default_profile_tools: {
      manager_agent: [],
      basic_agent: []
    },
  });
  const [agentToolNotice, setAgentToolNotice] = useState<string | null>(null);
  const unknownAgentToolNoticeShownRef = useRef(false);
  const [chatSessionId, setChatSessionId] = useState<string | undefined>(undefined);
  const [recoveryCandidate, setRecoveryCandidate] = useState<SessionHubRecoveryCandidate | null>(null);
  const [attachedRecoverySessionId, setAttachedRecoverySessionId] = useState<string | null>(null);
  const [recoveryError, setRecoveryError] = useState<ReturnType<typeof classifyAgentSessionRecoveryError> | null>(null);
  const recoveryCoordinatorRef = useRef(new SessionHubRecoveryCoordinator());
  const bookmarkTransactionsRef = useRef(new SessionHubBookmarkTransactionRegistry());
  const liveChatAttachmentsRef = useRef(new SessionHubLiveAttachmentRegistry());
  const liveAgentAttachmentsRef = useRef(new SessionHubLiveAttachmentRegistry());
  const [currentRequestId, setCurrentRequestId] = useState<string | undefined>(undefined);
  const [agentEntryAgentName, setAgentEntryAgentName] = useState<string>('coordinator_1');
  const [showInputActions, setShowInputActions] = useState(false);
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null);
  const [slashMenu, setSlashMenu] = useState<{
    visible: boolean;
    query: string;
    selectedIndex: number;
  }>({
    visible: false,
    query: "",
    selectedIndex: 0
  });
  const [collapsedCompact, setCollapsedCompact] = useState<Set<string>>(new Set());
  const newlyCompactMarkedRef = useRef<Set<string>>(new Set());
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const hasLoadedSettingsRef = useRef(false);
  const isHydratedRef = useRef(false);
  const skippedInitialSettingsPersistRef = useRef(false);
  const manuallyToggledReasoningIdsRef = useRef<Set<string>>(new Set());
  const pendingKeyboardVoiceReplayRef = useRef<{
    transcript: string;
    replay: NonNullable<Message['voiceReplay']>;
  } | null>(null);
  const releaseVoiceReplayRef = useRef<(replay: NonNullable<Message['voiceReplay']>) => void>(() => {
  });
  const voiceModeTransitionRef = useRef(false);
  const stopWasClickedRef = useRef(false);
  const isLoadingRef = useRef(false);
  const activeAgentRunRef = useRef<AgentRunState | null>(null);
  const messagesRef = useRef<Message[]>([]);
  const currentRequestIdRef = useRef<string | undefined>(undefined);
  return {
    messages,
    setMessages,
    input,
    setInput,
    isLoading,
    setIsLoading,
    stoppedMessageId,
    setStoppedMessageId,
    stoppedTrackedMessageIds,
    setStoppedTrackedMessageIds,
    showSettings,
    setShowSettings,
    settingsTab,
    setSettingsTab,
    settings,
    setSettings,
    settingsRef,
    localMemoryRequestsRef,
    chatHistoryRequestsRef,
    pendingSettings,
    setPendingSettings,
    connectionStatus,
    setConnectionStatus,
    modelName,
    setModelName,
    maxContextLength,
    setMaxContextLength,
    serverDefaults,
    setServerDefaults,
    runtimeContextConfig,
    setRuntimeContextConfig,
    webUIConfig,
    setWebUIConfig,
    yamlMessageTemplates,
    setYamlMessageTemplates,
    messageTemplates,
    setMessageTemplates,
    messageTemplateImmediateSend,
    setMessageTemplateImmediateSend,
    messageTemplateStorageError,
    setMessageTemplateStorageError,
    voiceConfig,
    setVoiceConfig,
    dictationMagicSendSettings,
    setDictationMagicSendSettings,
    showDictationMagicSendSettings,
    setShowDictationMagicSendSettings,
    inputMode,
    setInputMode,
    inputModeRef,
    selectedSkinId,
    setSelectedSkinId,
    pendingSkinId,
    setPendingSkinId,
    runtimeTimeContext,
    setRuntimeTimeContext,
    agentSessionId,
    setAgentSessionId,
    agentToolState,
    dispatchAgentToolState,
    agentToolCatalog,
    setAgentToolCatalog,
    agentToolNotice,
    setAgentToolNotice,
    unknownAgentToolNoticeShownRef,
    chatSessionId,
    setChatSessionId,
    recoveryCandidate,
    setRecoveryCandidate,
    attachedRecoverySessionId,
    setAttachedRecoverySessionId,
    recoveryError,
    setRecoveryError,
    recoveryCoordinatorRef,
    bookmarkTransactionsRef,
    liveChatAttachmentsRef,
    liveAgentAttachmentsRef,
    currentRequestId,
    setCurrentRequestId,
    agentEntryAgentName,
    setAgentEntryAgentName,
    showInputActions,
    setShowInputActions,
    lightboxSrc,
    setLightboxSrc,
    slashMenu,
    setSlashMenu,
    collapsedCompact,
    setCollapsedCompact,
    newlyCompactMarkedRef,
    inputRef,
    abortControllerRef,
    hasLoadedSettingsRef,
    isHydratedRef,
    skippedInitialSettingsPersistRef,
    manuallyToggledReasoningIdsRef,
    pendingKeyboardVoiceReplayRef,
    releaseVoiceReplayRef,
    voiceModeTransitionRef,
    stopWasClickedRef,
    isLoadingRef,
    activeAgentRunRef,
    messagesRef,
    currentRequestIdRef
  };
}
export type ConversationState = ReturnType<typeof useConversationState>;
