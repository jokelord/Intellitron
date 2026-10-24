import { llmClient, stopChat, stopAgent } from '../api.ts';
import { clearRetainedAgentSessionBeforeDelete } from '../agent-session-recovery.ts';
import { clearChatTranscript } from '../chat-transcript-storage.ts';
import { resolveSessionHubClearTarget, clearSessionHubRecoveryState, type SessionHubLiveAttachmentRegistry } from '../session-hub-recovery.ts';
import { finalizeStreamingAssistantMessage } from '../answer-now-ui.ts';
import { collectTrackedAssistantMessageIds, finalizeTrackedAssistantMessages } from '../dispatch-message.ts';
import { writeStoredWebUISkinId, getWebUISkin, applyWebUISkinOverride } from '../web-ui-config.ts';
import type { ConversationState } from './use-conversation-state.ts';
import type { VoiceInputController } from '../voice-input.ts';
import { abortStoppedConversation } from './conversation-stop.ts';
export function retireConversationMode(input: {
  registry: SessionHubLiveAttachmentRegistry;
  transportRef: {
    current: AbortController | null;
  };
  reset(): void;
  invalidate?(): void | Promise<void>;
}) {
  const token = input.registry.getCurrent(), transport = input.transportRef.current;
  transport?.abort(new DOMException('Conversation mode changed', 'AbortError'));
  if (token)
    input.registry.detach(token, () => {
      if (input.transportRef.current === transport)
        input.reset();
    });
  void input.invalidate?.();
}
export function finalizeConversationStop(state: ConversationState, mode: 'chat' | 'agent') {
  state.stopWasClickedRef.current = true;
  const run = mode === 'agent' ? state.activeAgentRunRef.current : null;
  const stoppedIds = run
    ? collectTrackedAssistantMessageIds(run.assistantMessageId, run.projectedAssistantMessageIds, run.executionAssistantMessageId)
    : [];
  const chatMessageId = state.messagesRef.current.findLast(message => message.role === 'assistant' && message.isStreaming)?.id ?? null;
  // Recovery detach finalizes and releases the active run, so capture its identity and
  // interruption intent first. The frozen handler must observe Stop, not a plain detach.
  if (run) run.stopWasClicked = true;
  state.recoveryCoordinatorRef.current.detach();
  state.stopWasClickedRef.current = true;
  if (run) {
    state.setStoppedMessageId(run.assistantMessageId);
    state.setStoppedTrackedMessageIds(stoppedIds);
    state.setMessages(previous => finalizeTrackedAssistantMessages(previous, stoppedIds));
  } else {
    state.setStoppedMessageId(chatMessageId);
    state.setStoppedTrackedMessageIds(chatMessageId ? [chatMessageId] : []);
    state.setMessages(previous => finalizeStreamingAssistantMessage(previous));
  }
}
export async function stopOrdinaryConversation(state: ConversationState, mode: 'chat' | 'agent', reset: () => void) {
  const requestId = state.currentRequestIdRef.current ?? state.currentRequestId;
  const sessionId = state.agentSessionId;
  const recovered = state.recoveryCoordinatorRef.current.currentAttachmentToken();
  if (recovered && ((mode === 'chat' && !requestId) || (mode === 'agent' && !sessionId))) {
    state.setRecoveryError({ kind: 'transient', message: 'Cannot stop recovered request before its current request identity is available. Please retry.' });
    return;
  }
  finalizeConversationStop(state, mode);
  abortStoppedConversation(state.abortControllerRef.current);
  reset();
  try {
    if (mode === 'agent') {
      if (sessionId)
        await stopAgent(sessionId, requestId);
    }
    else if (requestId)
      await stopChat(requestId);
  }
  catch (error) {
    console.error('停止请求失败:', error);
  }
}
export function clearConversation(options: {
  pageMode: 'text' | 'vision';
  state: ConversationState;
  voice: VoiceInputController;
  cancelPendingCycleStart(): void;
  resetDraft(): void;
  detachSession?(): string | undefined;
  detachLocal?(): void | Promise<void>;
  afterClear?(): void;
}) {
  const { state: state, pageMode } = options;
  options.cancelPendingCycleStart();
  state.localMemoryRequestsRef.current.clear();
  state.chatHistoryRequestsRef.current.clear();
  clearChatTranscript(pageMode);
  state.setRecoveryError(null);
  const paired = state.agentToolState.pairedSessionId;
  const detached = options.detachSession?.() ?? state.agentSessionId;
  const target = paired ?? resolveSessionHubClearTarget({
    bookmarkSessionId: state.recoveryCandidate?.bookmark.sessionId,
    attachedSessionId: state.attachedRecoverySessionId ?? state.chatSessionId,
    staleBoundSessionId: detached
  });
  clearRetainedAgentSessionBeforeDelete({
    boundSessionId: target,
    bookmarkSessionId: undefined,
    clearBookmark: () => clearSessionHubRecoveryState(pageMode),
    detachLocal: options.detachLocal ?? (() => undefined),
    deleteSession: id => llmClient.deleteSessionHub(id),
    onError: console.error
  });
  if (paired)
    state.dispatchAgentToolState({
      type: 'clear',
      sessionId: paired
    });
  state.setRecoveryCandidate(null);
  state.setAttachedRecoverySessionId(null);
  state.recoveryCoordinatorRef.current.detach();
  for (const registry of [state.liveAgentAttachmentsRef.current, state.liveChatAttachmentsRef.current]) {
    const token = registry.getCurrent();
    if (token)
      registry.detach(token, () => undefined);
  }
  state.abortControllerRef.current?.abort();
  void options.voice.abortVoice();
  if (state.inputModeRef.current === 'voice')
    state.setInputMode('keyboard');
  state.messagesRef.current.forEach(message => {
    if (message.voiceReplay)
      state.releaseVoiceReplayRef.current(message.voiceReplay);
  });
  const pending = state.pendingKeyboardVoiceReplayRef.current;
  if (pending)
    state.releaseVoiceReplayRef.current(pending.replay);
  state.pendingKeyboardVoiceReplayRef.current = null;
  state.setMessages([]);
  options.resetDraft();
  // Vision keeps its existing binding state setter semantics; its detach port clears the authoritative ref.
  if (pageMode === 'text')
    state.setAgentSessionId(undefined);
  state.setChatSessionId(undefined);
  state.currentRequestIdRef.current = undefined;
  state.setCurrentRequestId(undefined);
  state.setRuntimeTimeContext(null);
  state.setStoppedTrackedMessageIds([]);
  state.activeAgentRunRef.current = null;
  state.abortControllerRef.current = null;
  state.isLoadingRef.current = false;
  state.setIsLoading(false);
  state.setStoppedMessageId(null);
  options.afterClear?.();
}
export function confirmConversationSettings(state: ConversationState, options: {
  canChangeMode?(): boolean;
  invalidateMode?(): void | Promise<void>;
  resetStreamingState(): void;
}) {
  const pending = state.pendingSettings;
  if (!pending)
    return;
  if (pending.agentMode !== state.settings.agentMode) {
    if (options.canChangeMode?.() === false)
      return;
    // Recovery owns its own subscriber registry; detach it without touching its producer.
    if (state.recoveryCoordinatorRef.current.currentAttachmentToken()) {
      state.recoveryCoordinatorRef.current.detach();
      options.resetStreamingState();
    }
    retireConversationMode({
      registry: state.settings.agentMode ? state.liveAgentAttachmentsRef.current : state.liveChatAttachmentsRef.current,
      transportRef: state.abortControllerRef,
      reset: options.resetStreamingState,
      invalidate: options.invalidateMode
    });
  }
  const previous = state.selectedSkinId, id = writeStoredWebUISkinId(state.pendingSkinId), skin = getWebUISkin(id);
  state.setSelectedSkinId(id);
  state.setWebUIConfig(current => applyWebUISkinOverride(current, id));
  state.setSettings(pending);
  localStorage.setItem('llm-chat-settings', JSON.stringify(pending));
  if (id !== previous && skin.requiresReload && window.confirm('该皮肤需要刷新页面后才能完整生效。是否现在刷新？'))
    window.location.reload();
  state.setShowSettings(false);
  state.setPendingSettings(null);
}
