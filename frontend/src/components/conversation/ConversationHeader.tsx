"use client";

import React from 'react';
import { Brain, Settings, Trash2, Zap } from 'lucide-react';
import { getWebUIPageTitle, getWebUIHeaderSubtitle, getWebUIPageHeaderDisplay, isAuroraGlowSkin, type WebUIConfig } from '@/lib/web-ui-config';
import GlassJellySurface from '@/components/GlassJellySurface';
import { HoverHint } from '@/components/HoverHint';
import LocalMemoryToggle from '@/components/LocalMemoryToggle';
import { VoiceModeToggle, type VoiceInputMode } from '@/components/VoiceModeToggle';

export interface ConversationHeaderProps {
  pageMode: 'text' | 'vision';
  webUIConfig?: WebUIConfig;
  modelName: string;
  agentMode: boolean;
  connectionStatus: 'checking' | 'connected' | 'error';
  inputMode: VoiceInputMode;
  onVoiceModeChange(mode: VoiceInputMode): void;
  voiceDisabled: boolean;
  voiceDisabledReason?: string;
  canRecover: boolean;
  onRecover(): void;
  enableThinking: boolean;
  onToggleThinking(): void;
  enableLocalMemory: boolean;
  onToggleLocalMemory(enabled: boolean): void;
  onOpenSettings(): void;
  onClear(): void;
}

export function ConversationHeader({
  pageMode, webUIConfig, modelName, agentMode, connectionStatus, inputMode, onVoiceModeChange,
  voiceDisabled, voiceDisabledReason, canRecover, onRecover, enableThinking,
  onToggleThinking, enableLocalMemory, onToggleLocalMemory, onOpenSettings, onClear,
}: ConversationHeaderProps) {
  const glassJellyEnabled = isAuroraGlowSkin(webUIConfig);
  const showPageHeader = getWebUIPageHeaderDisplay(webUIConfig);
  const glassHeaderZoneClass = glassJellyEnabled ? 'webui-glass-zone border-transparent' : 'webui-zone-header-bg border-gray-200 dark:border-gray-700';
  const status = {
    checking: {color: 'bg-yellow-500', text: '检查连接中...'},
    connected: {color: 'bg-green-500', text: '已连接'},
    error: {color: 'bg-red-500', text: '连接失败'},
  }[connectionStatus];
  return (
    <header className={`sticky top-0 z-20 flex-shrink-0 border-b px-4 py-[7px] ${glassHeaderZoneClass}`}>
      {glassJellyEnabled && <GlassJellySurface config={webUIConfig} zone="header" />}
      <div className="webui-glass-zone-content max-w-4xl mx-auto flex flex-wrap items-center justify-between gap-2">
        {showPageHeader && (
          <div className="flex min-w-0 items-end space-x-3">
            <div>
              <h1 className="truncate text-base font-semibold text-gray-900 dark:text-white">
                {getWebUIPageTitle(webUIConfig, pageMode)}
              </h1>
              <p className="truncate text-xs text-gray-500 dark:text-gray-400">
                {modelName} | {getWebUIHeaderSubtitle(webUIConfig)}
                {agentMode && (
                  <span className="ml-2 inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 text-xs font-medium">
                    <Zap className="w-3 h-3" /> Agent
                  </span>
                )}
              </p>
            </div>
          </div>
        )}

        <VoiceModeToggle
          mode={inputMode}
          onModeChange={onVoiceModeChange}
          voiceDisabled={voiceDisabled}
          voiceDisabledReason={voiceDisabledReason}
        />

        <div className="flex items-center space-x-4">
          <HoverHint label={status.text}><span role="status" aria-label={status.text} className={`block w-2 h-2 rounded-full ${status.color}`} /></HoverHint>

          {canRecover && (
            <button
              type="button"
              onClick={onRecover}
              className="rounded-lg border border-amber-400 px-2 py-1 text-xs font-medium text-amber-700 hover:bg-amber-50 dark:text-amber-300 dark:hover:bg-amber-900/30"
            >
              恢复运行中的任务
            </button>
          )}

          <HoverHint label={enableThinking ? '思考模式已开启' : '思考模式已关闭'}>
            <button
              onClick={onToggleThinking}
              className={`relative p-2 rounded-lg transition-all duration-200 ${enableThinking
                ? 'text-fuchsia-500 bg-fuchsia-500/10 shadow-[0_0_18px_rgba(217,70,239,0.45)] ring-1 ring-fuchsia-400/50'
                : 'text-gray-500 hover:text-fuchsia-500 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
              aria-label={enableThinking ? '思考模式已开启' : '思考模式已关闭'}
              aria-pressed={enableThinking}
            >
              <Brain className={`relative z-10 w-5 h-5 ${enableThinking ? 'drop-shadow-[0_0_8px_rgba(217,70,239,0.95)]' : ''}`} />
              {enableThinking && (
                <span className="pointer-events-none absolute inset-1 rounded-lg bg-fuchsia-400/20 blur-sm" aria-hidden="true" />
              )}
            </button>
          </HoverHint>

          <LocalMemoryToggle
            enabled={enableLocalMemory}
            onChange={onToggleLocalMemory}
          />

          <HoverHint label="设置">
            <button
              onClick={onOpenSettings}
              className="p-2 text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
              aria-label="设置"
            >
              <Settings className="w-5 h-5" />
            </button>
          </HoverHint>

          <HoverHint label="清空对话">
            <button
              onClick={onClear}
              className="p-2 text-gray-500 hover:text-red-500 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
              aria-label="清空对话"
            >
              <Trash2 className="w-5 h-5" />
            </button>
          </HoverHint>
        </div>
      </div>
    </header>
  );
}

export default ConversationHeader;
