'use client';
import { useCallback, useEffect, useRef } from 'react';
import { llmClient, dispatchAgentSessionPayload } from '../api.ts';
import { createTextAgentSessionHandlers, type TextAgentSessionHandlerEnvironment, type TextAgentSessionRunRef } from '../text-agent-session-handlers.ts';
import { createChatSessionHandlers } from '../chat-session-handlers.ts';
import { selectSessionHubRecoveryCandidate } from '../session-hub-recovery.ts';
import { classifyAgentSessionRecoveryError } from '../agent-session-recovery.ts';
import type { ConversationState } from './use-conversation-state.ts';
export interface ConversationRecoveryOptions {
  pageMode: 'text' | 'vision';
  state: ConversationState;
  agentEnvironment: TextAgentSessionHandlerEnvironment;
  bindSession(sessionId: string): void;
}
export function useConversationRecovery(options: ConversationRecoveryOptions) {
  const latest = useRef(options);
  latest.current = options;
  useEffect(() => {
    const current = latest.current;
    current.state.setRecoveryCandidate(selectSessionHubRecoveryCandidate(current.pageMode));
    return () => {
      current.state.recoveryCoordinatorRef.current.detach();
    };
  }, []);
  return useCallback(() => {
    const { state: s, pageMode, agentEnvironment, bindSession } = latest.current;
    const candidate = s.recoveryCandidate;
    const bookmark = candidate?.bookmark;
    if (!candidate || !bookmark)
      return;
    const ordinaryAttachment = s.liveChatAttachmentsRef.current.getCurrent()
      ?? s.liveAgentAttachmentsRef.current.getCurrent();
    const ordinaryTransportActive = ordinaryAttachment && s.abortControllerRef.current
      && !s.abortControllerRef.current.signal.aborted;
    // A provisional bookmark is already durable before ordinary POST acceptance.
    // Restore must not replace that owner or an existing recovery subscriber.
    if (s.isLoadingRef.current || ordinaryTransportActive) return;
    s.setRecoveryError(null);
    s.setAttachedRecoverySessionId(bookmark.sessionId);
    const recoveryRunRef: TextAgentSessionRunRef = {
      current: null
    };
    if (pageMode === 'text')
      s.activeAgentRunRef.current = null;
    s.setMessages([]);
    const recoveryHandlers = createTextAgentSessionHandlers({
      ...agentEnvironment,
      // Replayed historical terminals cannot release the current Hub subscription.
      setAgentBusy: () => undefined,
      resetStreamingState: () => undefined,
      onAgentToolConfigurationAccepted: accepted => {
        if (accepted.sessionId && accepted.sessionId !== bookmark.sessionId)
          return;
        if (accepted.toolConfiguration)
          s.dispatchAgentToolState({
            type: 'recover',
            sessionId: bookmark.sessionId,
            snapshot: accepted.toolConfiguration
          });
      },
    }, recoveryRunRef);
    const chatHandlers = createChatSessionHandlers({
      defaultReasoningCollapsed: !s.settings.showReasoning,
      onMessagesChanged: next => {
        s.messagesRef.current = [...next];
        s.setMessages([...next]);
      },
    });
    let ownedController: AbortController | null = null;
    let journalRequestId: string | undefined;
    let settledTerminal = false;
    const publishRequest = (requestId: string | undefined) => {
      journalRequestId = requestId;
      s.currentRequestIdRef.current = requestId;
      s.setCurrentRequestId(requestId);
    };
    s.recoveryCoordinatorRef.current.attach(bookmark, (controller, handlers) => {
      ownedController = controller;
      s.abortControllerRef.current = controller;
      publishRequest(undefined);
      return llmClient.sessionHubEvents(bookmark.sessionId, 0, handlers, controller,
        bookmark.interactionMode === 'agent' ? 'Agent' : 'Chat');
    }, {
      handlers: {
        onPayload: async payload => {
          if (!ownedController || ownedController.signal.aborted || s.abortControllerRef.current !== ownedController) return;
          const requestId = typeof payload.request_id === 'string' && payload.request_id.length > 0
            ? payload.request_id : undefined;
          if (payload.type === 'request_accepted' && requestId) {
            publishRequest(requestId);
          } else if (journalRequestId && requestId && requestId !== journalRequestId) {
            // Only explicit foreign IDs can reject a domain record. The Hub forwards
            // ordinary content/timeline/compact records without request_id.
            return;
          } else if (requestId && payload.type !== 'done') {
            publishRequest(requestId);
          }
          if (bookmark.interactionMode === 'agent') await dispatchAgentSessionPayload(payload, recoveryHandlers);
          else await chatHandlers.onPayload(payload);
          if (payload.type === 'done' && requestId === journalRequestId) publishRequest(undefined);
        },
      },
      onSettled: result => {
        if (!ownedController || s.abortControllerRef.current !== ownedController) return;
        settledTerminal = result.kind === 'terminal';
        if (settledTerminal) candidate.commitMigration();
        else {
          s.setAttachedRecoverySessionId(null);
          if (result.kind === 'detached' && result.error) s.setRecoveryError(classifyAgentSessionRecoveryError(result.error));
          else if (result.kind === 'rejected') s.setRecoveryError(classifyAgentSessionRecoveryError(new Error(result.rejection.message)));
        }
      },
      onBusy: busy => {
        if (!busy && ownedController && s.abortControllerRef.current !== ownedController) return;
        s.isLoadingRef.current = busy;
        s.setIsLoading(busy);
        if (!busy) {
          if (!settledTerminal) s.setAttachedRecoverySessionId(null);
          publishRequest(undefined);
          if (s.abortControllerRef.current === ownedController) s.abortControllerRef.current = null;
          if (bookmark.interactionMode === 'agent') recoveryHandlers.onDetached();
        }
      },
    });
    if (bookmark.interactionMode === 'agent')
      bindSession(bookmark.sessionId);
    else
      s.setChatSessionId(bookmark.sessionId);
  }, []);
}
