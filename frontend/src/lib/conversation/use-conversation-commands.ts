'use client';

import {useCallback, useMemo} from 'react';
import type React from 'react';
import {llmClient} from '../api';
import {SLASH_COMMANDS, detectSlashCommand, filterCommands, isCompleteSlashCommand, mergeSlashCommandState, parseCompactCustomInstruction, type SlashCommand} from '../slash-commands';
import {shouldSubmitChatInput} from '../chat-input-keyboard';
import {buildCompactRequestBody} from '../compact-client';
import {applyCompactProjection} from '../compact-projection';
import {projectTimelineEvent} from '../timeline-projection';
import {selectChatLocalMemoryHistoryWindow} from '../local-memory';
import {insertMessageTemplateIntoInput, sendMessageTemplateThroughExistingPipeline} from '../message-template-page-actions';
import {writeStoredWebUIMessageTemplates, clearStoredWebUIMessageTemplates, writeStoredWebUIMessageTemplateImmediateSend} from '../web-ui-config';
import type {VoiceInputController} from '../voice-input';
import type {CycleExecutionStatus} from '../cycle-types';
import type {ConversationState} from './use-conversation-state';
import type {ConversationSend} from './use-conversation-voice';

export interface ConversationCommandsOptions {
  state: ConversationState;
  pageMode: 'text' | 'vision';
  busy: boolean;
  cycleStatus: CycleExecutionStatus;
  send: ConversationSend;
  voiceController: VoiceInputController;
  buildRuntimeTimeContext(): string;
  client?: Pick<typeof llmClient, 'runCompact'>;
}

export function useConversationCommands({state, pageMode, busy, cycleStatus, send,
  voiceController, buildRuntimeTimeContext, client = llmClient}: ConversationCommandsOptions) {
  const {setMessageTemplates, setMessageTemplateStorageError, yamlMessageTemplates,
    setMessageTemplateImmediateSend, setInput, inputRef, slashMenu, setSlashMenu,
    settings, agentToolState, agentSessionId, setMessages, newlyCompactMarkedRef,
    messagesRef, runtimeContextConfig, runtimeTimeContext, chatSessionId, input, inputMode} = state;
  const handleMessageTemplatesChange = useCallback((next: string[]) => {
    setMessageTemplates(next);
    if (!writeStoredWebUIMessageTemplates(next)) {
      setMessageTemplateStorageError('消息模板无法保存到浏览器缓存');
    } else {
      setMessageTemplateStorageError(null);
    }
  }, []);

  const handleRestoreMessageTemplates = useCallback(() => {
    if (!clearStoredWebUIMessageTemplates()) {
      setMessageTemplateStorageError('无法清除消息模板浏览器缓存');
      return;
    }
    setMessageTemplates(yamlMessageTemplates.slice());
    setMessageTemplateStorageError(null);
  }, [yamlMessageTemplates]);

  const handleMessageTemplateImmediateSendChange = useCallback((enabled: boolean) => {
    setMessageTemplateImmediateSend(enabled);
    if (!writeStoredWebUIMessageTemplateImmediateSend(enabled)) {
      setMessageTemplateStorageError('消息模板无法保存到浏览器缓存');
    } else {
      setMessageTemplateStorageError(null);
    }
  }, []);

  const handleInsertMessageTemplate = useCallback((text: string) => {
    insertMessageTemplateIntoInput(text, setInput, () => inputRef.current?.focus());
  }, []);

  const handleSendMessageTemplate = useCallback((text: string) => {
    void sendMessageTemplateThroughExistingPipeline(text, send);
  }, [send]);

  const filteredSlashCommands = useMemo(
    () => filterCommands(slashMenu.query),
    [slashMenu.query]
  );
  const disabledCommandMap = useMemo(() => {
    const m = new Map<string, string | null>();
    const ctx = {
      mode: pageMode,
      agentMode: settings.agentMode,
      cycleRunning: cycleStatus === 'running',
      isLoading: busy,
      sessionId: undefined,
      agentSessionId: settings.agentMode ? (agentToolState.pairedSessionId ?? undefined) : agentSessionId,
      llmClient: client,
      setMessages,
      setInput,
    };
    for (const c of SLASH_COMMANDS) m.set(c.name, c.disabledWhen?.(ctx) ?? null);
    return m;
  }, [pageMode, settings.agentMode, cycleStatus, busy, agentSessionId, agentToolState.pairedSessionId, client, setMessages, setInput]);

  const handleSelectSlashCommand = useCallback(
    async (cmd: SlashCommand, args = '') => {
      try {
        const disabled = disabledCommandMap.get(cmd.name);
        if (disabled) {
          return;
        }
        const parsedCustom = parseCompactCustomInstruction(args);
        if (!parsedCustom.ok) {
          setInput('');
          setSlashMenu({ visible: false, query: '', selectedIndex: 0 });
          const failedId = `compact-failed-${Date.now()}`;
          applyCompactProjection(
            {
              kind: 'compact_failed',
              summaryMessageId: failedId,
              reason: 'invalid_custom_instruction',
            },
            setMessages,
            newlyCompactMarkedRef
          );
          return;
        }
        const currentMessages = messagesRef.current;
        if (currentMessages.length === 0) {
          setInput('');
          setSlashMenu({ visible: false, query: '', selectedIndex: 0 });
          const failedId = `compact-failed-${Date.now()}`;
          applyCompactProjection(
            {
              kind: 'compact_failed',
              summaryMessageId: failedId,
              reason: 'empty_history',
            },
            setMessages,
            newlyCompactMarkedRef
          );
          return;
        }
        if (settings.agentMode && !agentToolState.pairedSessionId) {
          applyCompactProjection(
            { kind: 'compact_failed', summaryMessageId: `compact-failed-${Date.now()}`, reason: 'missing_agent_session' },
            setMessages,
            newlyCompactMarkedRef,
          );
          return;
        }
        setInput('');
        setSlashMenu({ visible: false, query: '', selectedIndex: 0 });
        const compactWindow = settings.agentMode ? undefined
          : selectChatLocalMemoryHistoryWindow(currentMessages, chatSessionId, true);
        const body = buildCompactRequestBody({
          agentMode: settings.agentMode,
          sessionId: settings.agentMode ? agentToolState.pairedSessionId : chatSessionId,
          messages: compactWindow?.messages ?? currentMessages,
          chatHistoryStartRef: compactWindow?.historyStartRef,
          customInstructions: parsedCustom.customInstructions,
          systemPrompt: settings.systemPrompt,
          runtimeContext: runtimeContextConfig.enable_runtime_time_context
            ? (runtimeTimeContext ?? buildRuntimeTimeContext())
            : null,
        });
        await client.runCompact(body, (ev) => {
          const projection = projectTimelineEvent(
            ev as Parameters<typeof projectTimelineEvent>[0]
          );
          if (projection.kind === 'noop') return;
          if (
            projection.kind === 'compact_started' ||
            projection.kind === 'compact_delta' ||
            projection.kind === 'compact_completed' ||
            projection.kind === 'compact_failed'
          ) {
            applyCompactProjection(
              projection,
              setMessages,
              newlyCompactMarkedRef
            );
          }
        });
      } catch (err) {
        console.error('[compact] handleSelectSlashCommand crashed:', err);
        const failedId = `compact-failed-${Date.now()}`;
        applyCompactProjection(
          {
            kind: 'compact_failed',
            summaryMessageId: failedId,
            reason: 'crash',
          },
          setMessages,
          newlyCompactMarkedRef
        );
      }
    },
    [
      disabledCommandMap,
      client,
      setInput, setSlashMenu, setMessages, messagesRef, newlyCompactMarkedRef,
      settings.agentMode,
      settings.systemPrompt,
      runtimeContextConfig.enable_runtime_time_context,
      runtimeTimeContext,
      buildRuntimeTimeContext,
      agentSessionId,
      chatSessionId,
      agentToolState.pairedSessionId,
    ]
  );

  const handleSubmit = useCallback(
    () => {
      if (voiceController.isVoiceMode && inputMode === 'voice') {
        const transcript = voiceController.state.pendingTranscript.trim();
        if (!transcript) return;
        voiceController.pauseForResponse();
        void send(transcript, voiceController.state.pendingReplay ?? undefined);
        return;
      }
      const matched = isCompleteSlashCommand(input);
      if (matched) {
        const args = input.slice(matched.name.length);
        handleSelectSlashCommand(matched, args);
        return;
      }
      if (voiceController.isVoiceMode && inputMode === 'dictation') {
        voiceController.pauseForResponse();
      }
      send();
    },
    [handleSelectSlashCommand, send, input, inputMode, voiceController]
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (slashMenu.visible && filteredSlashCommands.length > 0) {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          setSlashMenu((s) => ({
            ...s,
            selectedIndex:
              (s.selectedIndex + 1) % filteredSlashCommands.length,
          }));
          return;
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          setSlashMenu((s) => ({
            ...s,
            selectedIndex:
              (s.selectedIndex - 1 + filteredSlashCommands.length) %
              filteredSlashCommands.length,
          }));
          return;
        }
        if (
          e.key === 'Enter' &&
          !e.nativeEvent.isComposing &&
          ((e.ctrlKey || e.metaKey) || !e.shiftKey)
        ) {
          e.preventDefault();
          handleSelectSlashCommand(
            filteredSlashCommands[slashMenu.selectedIndex],
            ''
          );
          return;
        }
        if (e.key === 'Escape') {
          e.preventDefault();
          setSlashMenu({ visible: false, query: '', selectedIndex: 0 });
          return;
        }
        if (e.key === 'Tab') {
          e.preventDefault();
          const cmd = filteredSlashCommands[slashMenu.selectedIndex];
          setInput(cmd.name + ' ');
          setSlashMenu({ visible: false, query: '', selectedIndex: 0 });
          return;
        }
      }
      if (
        shouldSubmitChatInput({
          key: e.key,
          shiftKey: e.shiftKey,
          ctrlKey: e.ctrlKey,
          metaKey: e.metaKey,
          isComposing: e.nativeEvent.isComposing,
        }, settings.enterToSend)
      ) {
        e.preventDefault();
        handleSubmit();
      }
    },
    [slashMenu, filteredSlashCommands, handleSelectSlashCommand, handleSubmit, settings.enterToSend]
  );
  const onInputChange = useCallback((value: string, selectionStart: number) => {
    setInput(value);
    setSlashMenu((previous) => mergeSlashCommandState(previous, detectSlashCommand(value, selectionStart)));
  }, [setInput, setSlashMenu]);
  return {handleMessageTemplatesChange, handleRestoreMessageTemplates,
    handleMessageTemplateImmediateSendChange, handleInsertMessageTemplate,
    handleSendMessageTemplate, filteredSlashCommands, disabledCommandMap,
    handleSelectSlashCommand, handleSubmit, handleKeyDown, onInputChange};
}
