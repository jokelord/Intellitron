'use client';

import { useCallback, useEffect, useLayoutEffect, useRef } from 'react';
import type { VoiceInputMode } from '../../components/VoiceModeToggle';
import type { Message } from '../message-types';
import { useVoiceInput } from '../voice-input';
import { findDictationMagicSendMatch, useDictationMagicSend } from '../dictation-magic-send';
import { usePageVisibility } from '../page-visibility';
import type { ConversationState } from './use-conversation-state';

export type ConversationSend = (
  text?: string,
  replay?: Message['voiceReplay'],
  magicSource?: string,
) => void | Promise<void>;

export interface ConversationVoiceOptions {
  state: ConversationState;
  send: ConversationSend;
  busy: boolean;
  pendingAcceptance?: boolean;
  cycleRunning: boolean;
}

/** Owns ordinary voice glue; native audio ownership remains in useVoiceInput. */
export function useConversationVoice({state, send, busy, pendingAcceptance = false, cycleRunning}: ConversationVoiceOptions) {
  const {
    input, inputRef, setInput, inputMode, setInputMode, inputModeRef,
    voiceConfig, dictationMagicSendSettings, showDictationMagicSendSettings,
    releaseVoiceReplayRef, pendingKeyboardVoiceReplayRef, voiceModeTransitionRef, setSlashMenu,
  } = state;
  const pageVisible = usePageVisibility();
  const sendRef = useRef(send);
  sendRef.current = send;
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {mountedRef.current = false;};
  }, []);
  useLayoutEffect(() => {inputModeRef.current = inputMode;}, [inputMode, inputModeRef]);

  const insertDictationTextAtCursor = useCallback((textToInsert: string) => {
    if (!textToInsert) return;
    const element = inputRef.current;
    const focused = Boolean(element && document.activeElement === element);
    const base = element ? element.value : input;
    const start = focused && element ? (element.selectionStart ?? base.length) : base.length;
    const end = focused && element ? (element.selectionEnd ?? start) : base.length;
    setInput(base.slice(0, start) + textToInsert + base.slice(end));
    if (focused) {
      const caret = start + textToInsert.length;
      requestAnimationFrame(() => {inputRef.current?.setSelectionRange(caret, caret);});
    }
  }, [input, inputRef, setInput]);
  const dictationMagicSendPending = Boolean(inputMode === 'dictation'
    && findDictationMagicSendMatch(input, dictationMagicSendSettings?.rules ?? []));
  const voiceController = useVoiceInput({
    config: voiceConfig,
    isLoading: busy,
    resumeBlocked: dictationMagicSendPending || pendingAcceptance || cycleRunning,
    onFinalTranscript: (transcript, replay) => {
      if (inputModeRef.current === 'dictation') {
        insertDictationTextAtCursor(transcript);
        if (replay) releaseVoiceReplayRef.current(replay);
        return;
      }
      void sendRef.current(transcript, replay);
    },
    onError: (error) => console.warn('[ASR]', error.message),
  });
  releaseVoiceReplayRef.current = voiceController.releaseReplay;

  useDictationMagicSend({
    mode: inputMode,
    input,
    rules: dictationMagicSendSettings?.rules ?? [],
    pollIntervalMs: dictationMagicSendSettings?.pollIntervalMs ?? 100,
    busy: busy || pendingAcceptance || cycleRunning || showDictationMagicSendSettings,
    send: async (text, sourceInput) => {
      if (voiceController.isVoiceMode) voiceController.pauseForResponse();
      await sendRef.current(text, undefined, sourceInput);
    },
    clear: () => setInput(''),
  });

  const transcriptHandledRef = useRef(false);
  useEffect(() => {
    const transcriptReady = inputMode === 'dictation' && voiceController.state.phase === 'transcript_ready';
    if (!transcriptReady) transcriptHandledRef.current = false;
    if (transcriptReady && !transcriptHandledRef.current) {
      transcriptHandledRef.current = true;
      const {pendingTranscript, pendingReplay} = voiceController.state;
      if (pendingTranscript.trim()) insertDictationTextAtCursor(pendingTranscript);
      if (pendingReplay) releaseVoiceReplayRef.current(pendingReplay);
    }
    if (!busy && !pendingAcceptance && !cycleRunning && !dictationMagicSendPending
      && (voiceController.state.phase === 'paused_for_response' || transcriptReady)) {
      voiceController.resumeVoice();
    }
  }, [inputMode, voiceController.state.phase, voiceController.state.pendingTranscript,
    voiceController.state.pendingReplay, voiceController.resumeVoice, insertDictationTextAtCursor,
    releaseVoiceReplayRef, pageVisible, busy, pendingAcceptance, cycleRunning, dictationMagicSendPending]);

  const handleVoiceModeChange = useCallback(async (mode: VoiceInputMode) => {
    if (voiceModeTransitionRef.current || (mode === inputMode && mode === 'keyboard')) return;
    if (mode !== 'keyboard' && (!voiceController.canActivate || pendingAcceptance || cycleRunning)) return;
    voiceModeTransitionRef.current = true;
    try {
      setSlashMenu({visible: false, query: '', selectedIndex: 0});
      if (mode === 'voice') {
        const pending = pendingKeyboardVoiceReplayRef.current;
        if (pending) releaseVoiceReplayRef.current(pending.replay);
        pendingKeyboardVoiceReplayRef.current = null;
        await voiceController.activateVoice();
        if (mountedRef.current) setInputMode('voice');
        return;
      }
      const preservePendingTranscript = async () => {
        const replay = voiceController.state.pendingReplay;
        const pendingTranscript = await voiceController.deactivateVoice();
        if (!mountedRef.current || !pendingTranscript) return;
        const previous = pendingKeyboardVoiceReplayRef.current;
        if (previous) releaseVoiceReplayRef.current(previous.replay);
        setInput(pendingTranscript);
        pendingKeyboardVoiceReplayRef.current = replay
          ? {transcript: pendingTranscript.trim(), replay} : null;
      };
      if (mode === 'dictation') {
        if (voiceController.isVoiceMode) await preservePendingTranscript();
        if (!mountedRef.current) return;
        await voiceController.activateVoice();
        if (mountedRef.current) setInputMode('dictation');
        return;
      }
      await preservePendingTranscript();
      if (mountedRef.current) setInputMode('keyboard');
    } finally {
      voiceModeTransitionRef.current = false;
    }
  }, [inputMode, voiceController, pendingAcceptance, cycleRunning, voiceModeTransitionRef,
    setSlashMenu, pendingKeyboardVoiceReplayRef, releaseVoiceReplayRef, setInput, setInputMode]);
  return {voiceController, dictationMagicSendPending, handleVoiceModeChange, insertDictationTextAtCursor};
}
