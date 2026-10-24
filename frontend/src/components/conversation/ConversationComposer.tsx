"use client";

import React from 'react';
import { Plus, Send } from 'lucide-react';
import { getChatInputPlaceholder } from '@/lib/chat-input-keyboard';
import { DEFAULT_SETTINGS } from '@/lib/message-types';
import type { VoiceInputController } from '@/lib/voice-input';
import type { SlashCommand, SlashMenuState } from '@/lib/slash-commands';
import { isAuroraGlowSkin, type WebUIConfig } from '@/lib/web-ui-config';
import GlassJellySurface from '@/components/GlassJellySurface';
import type { VoiceInputMode } from '@/components/VoiceModeToggle';
import { VoiceInputPanel } from '@/components/VoiceInputPanel';
import { VoiceListeningIndicator } from '@/components/VoiceListeningIndicator';
import { SlashCommandMenu } from '@/components/SlashCommandMenu';

export interface ConversationComposerProps {
  footerRef?: React.Ref<HTMLElement>;
  webUIConfig?: WebUIConfig;
  busy: boolean;
  cycleRunning: boolean;
  connectionError: boolean;
  sendDisabled: boolean;
  input: string;
  inputRef?: React.Ref<HTMLTextAreaElement>;
  inputMode: VoiceInputMode;
  voiceController: Pick<VoiceInputController, 'state' | 'inputGain' | 'isVoiceMode' | 'activateVoice'>;
  showInputActions: boolean;
  onToggleInputActions(): void;
  onInputChange(value: string, selectionStart: number): void;
  onKeyDown: React.KeyboardEventHandler<HTMLTextAreaElement>;
  onSubmit(): void;
  onStop(): void;
  onSwitchToKeyboard(): void;
  onOpenDictationSettings?: () => void;
  slashMenu: SlashMenuState;
  filteredSlashCommands: SlashCommand[];
  disabledCommandMap: Map<string, string | null>;
  onSelectSlashCommand(command: SlashCommand): void;
  onCloseSlashMenu(): void;
  actions: React.ReactNode;
  attachments?: React.ReactNode;
  notices?: React.ReactNode;
  actionIndicator?: React.ReactNode;
  enterToSend?: boolean;
}

export function ConversationComposer({
  footerRef, webUIConfig, busy, cycleRunning, connectionError, sendDisabled,
  input, inputRef, inputMode, voiceController, showInputActions, onToggleInputActions,
  onInputChange, onKeyDown, onSubmit, onStop, onSwitchToKeyboard,
  onOpenDictationSettings, slashMenu, filteredSlashCommands, disabledCommandMap,
  onSelectSlashCommand, onCloseSlashMenu, actions, attachments, notices,
  actionIndicator, enterToSend = DEFAULT_SETTINGS.enterToSend,
}: ConversationComposerProps) {
  const glassJellyEnabled = isAuroraGlowSkin(webUIConfig);
  const glassFooterZoneClass = glassJellyEnabled
    ? 'webui-glass-zone border-transparent'
    : 'webui-zone-footer-bg border-gray-200 dark:border-gray-700';
  return (
    <footer ref={footerRef} className={`sticky bottom-0 z-20 chat-input-footer flex-shrink-0 border-t px-4 pt-1 ${glassFooterZoneClass}`}>
      {glassJellyEnabled && <GlassJellySurface config={webUIConfig} zone="footer" />}
      <div className="webui-glass-zone-content max-w-4xl mx-auto space-y-1">
        {actions}
        {attachments}
        {notices}
        <div className="flex items-center space-x-3">
          <button
            type="button"
            onClick={onToggleInputActions}
            aria-expanded={showInputActions}
            aria-label="展开输入操作"
            title="展开输入操作"
            className={`webui-action-button relative flex items-center justify-center rounded-2xl border transition-colors ${showInputActions ? 'border-nvidia-500 bg-nvidia-500/10 text-nvidia-500' : 'border-gray-300 dark:border-gray-600 text-gray-500 hover:text-nvidia-500 hover:border-nvidia-500 dark:hover:border-nvidia-500'}`}
          >
            <Plus className="w-5 h-5" />
            {actionIndicator}
          </button>
          {inputMode === 'voice' ? (
            <div className="min-w-0 flex-1">
              <VoiceInputPanel
                state={voiceController.state}
                inputGain={voiceController.inputGain}
                disabled={busy || cycleRunning}
                onRetry={() => void voiceController.activateVoice()}
                onSwitchToKeyboard={onSwitchToKeyboard}
              />
            </div>
          ) : (
            <div className="min-w-0 flex-1 space-y-2">
              {inputMode === 'dictation' && voiceController.isVoiceMode ? (
                <VoiceListeningIndicator
                  state={voiceController.state}
                  inputGain={voiceController.inputGain}
                  onMagicSendSettings={onOpenDictationSettings}
                />
              ) : null}
              <div className="flex-1 relative">
                <SlashCommandMenu
                  visible={slashMenu.visible}
                  commands={filteredSlashCommands}
                  selectedIndex={slashMenu.selectedIndex}
                  disabledMap={disabledCommandMap}
                  onSelect={onSelectSlashCommand}
                  onClose={onCloseSlashMenu}
                />
                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={(event) => onInputChange(event.target.value, event.target.selectionStart ?? event.target.value.length)}
                  onKeyDown={onKeyDown}
                  placeholder={getChatInputPlaceholder(enterToSend)}
                  className="chat-input-textarea block w-full px-4 py-3 border border-gray-300 dark:border-gray-600 rounded-2xl bg-gray-50 dark:bg-gray-700 text-gray-900 dark:text-white resize-none overflow-y-hidden leading-6 focus:outline-none focus:ring-2 focus:ring-nvidia-500 focus:border-transparent"
                  rows={1}
                  disabled={connectionError || cycleRunning}
                />
              </div>
            </div>
          )}
          {busy ? (
            <div className="flex-shrink-0 flex space-x-2">
              <button
                type="button"
                onClick={onStop}
                className="webui-action-button webui-stop-button flex-shrink-0 bg-red-500 text-white rounded-2xl flex items-center justify-center hover:bg-red-600 transition-colors"
                title="停止"
                aria-label="停止"
              >
                <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="10" />
                  <rect x="9" y="9" width="6" height="6" rx="1" fill="currentColor" />
                </svg>
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={onSubmit}
              disabled={sendDisabled || connectionError || cycleRunning}
              aria-label="发送"
              className="webui-action-button webui-send-button flex-shrink-0 bg-nvidia-500 text-white rounded-2xl flex items-center justify-center hover:bg-nvidia-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <Send className="w-5 h-5" />
            </button>
          )}
        </div>
      </div>
    </footer>
  );
}

export default ConversationComposer;
