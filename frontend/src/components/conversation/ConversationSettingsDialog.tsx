"use client";

import React from 'react';
import type { ChatSettings } from '@/lib/message-types';
import type { AgentBusinessToolCatalog, AgentToolSessionRecord } from '@/lib/agent-tool-configuration';
import { WEBUI_SKINS } from '@/lib/web-ui-config';
import { ToggleSwitch } from '@/components/ToggleSwitch';
import AgentAdvancedSettingsPanel from '@/components/AgentAdvancedSettingsPanel';

export interface ConversationSettingsDialogProps {
  open: boolean;
  pendingSettings: ChatSettings | null;
  setPendingSettings: React.Dispatch<React.SetStateAction<ChatSettings | null>>;
  settingsTab: 'general' | 'advanced';
  setSettingsTab: React.Dispatch<React.SetStateAction<'general' | 'advanced'>>;
  pendingSkinId: string;
  setPendingSkinId: React.Dispatch<React.SetStateAction<string>>;
  maxContextLength: number;
  agentToolCatalog: AgentBusinessToolCatalog;
  sessionRecord: AgentToolSessionRecord | null;
  toolNotice?: string | null;
  visionAutomationArmed?: boolean;
  resetDisabled: boolean;
  onReset(): void;
  onCancel(): void;
  onDismiss(): void;
  onConfirm(): void | Promise<void>;
}

export function ConversationSettingsDialog({
  open, pendingSettings, setPendingSettings, settingsTab, setSettingsTab,
  pendingSkinId, setPendingSkinId, maxContextLength, agentToolCatalog,
  sessionRecord, toolNotice, visionAutomationArmed = false, resetDisabled,
  onReset, onCancel, onDismiss, onConfirm,
}: ConversationSettingsDialogProps) {
  if (!open || !pendingSettings) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div
        className="absolute inset-0 bg-black/50"
        onClick={onDismiss}
      />

      <div className="relative bg-white dark:bg-gray-800 rounded-xl shadow-2xl w-full max-w-lg max-h-[80vh] overflow-hidden mx-4">
        <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <h3 className="font-medium text-gray-900 dark:text-white text-lg">设置</h3>
          <div className="mt-3 flex gap-2 border-b border-gray-200 dark:border-gray-700">
            <button type="button" className={`px-3 py-2 text-sm ${settingsTab === 'general' ? 'border-b-2 border-nvidia-500 text-nvidia-600' : 'text-gray-500'}`} onClick={() => setSettingsTab('general')}>常规设置</button>
            <button type="button" className={`px-3 py-2 text-sm ${settingsTab === 'advanced' ? 'border-b-2 border-nvidia-500 text-nvidia-600' : 'text-gray-500'}`} onClick={() => setSettingsTab('advanced')}>高级设置</button>
          </div>
        </div>

        <div className="px-6 py-4 overflow-y-auto max-h-[60vh] space-y-4">
          {settingsTab === 'general' ? <>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Temperature: {pendingSettings.temperature}
              </label>
              <input
                type="range"
                min="0"
                max="2"
                step="0.1"
                value={pendingSettings.temperature}
                onChange={(e) => setPendingSettings((prev) => prev ? ({ ...prev, temperature: parseFloat(e.target.value) }) : null)}
                className="w-full"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Top-P: {pendingSettings.topP}
              </label>
              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={pendingSettings.topP}
                onChange={(e) => setPendingSettings((prev) => prev ? ({ ...prev, topP: parseFloat(e.target.value) }) : null)}
                className="w-full"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Top-K: {pendingSettings.topK}
              </label>
              <input
                type="range"
                min="1"
                max="100"
                step="1"
                value={pendingSettings.topK}
                onChange={(e) => setPendingSettings((prev) => prev ? ({ ...prev, topK: parseInt(e.target.value) }) : null)}
                className="w-full"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Repetition Penalty: {pendingSettings.repetitionPenalty}
              </label>
              <input
                type="range"
                min="1.0"
                max="2.0"
                step="0.1"
                value={pendingSettings.repetitionPenalty}
                onChange={(e) => setPendingSettings((prev) => prev ? ({ ...prev, repetitionPenalty: parseFloat(e.target.value) }) : null)}
                className="w-full"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                最大 Token: {pendingSettings.maxTokens}
              </label>
              <input
                type="range"
                min="256"
                max={maxContextLength}
                step="1024"
                value={pendingSettings.maxTokens}
                onChange={(e) => setPendingSettings((prev) => prev ? ({ ...prev, maxTokens: parseInt(e.target.value) }) : null)}
                className="w-full"
              />
            </div>
          </div>

          <div className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                界面皮肤
              </label>
              <select
                value={pendingSkinId}
                onChange={(e) => setPendingSkinId(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
              >
                {WEBUI_SKINS.map((skin) => (
                  <option key={skin.id} value={skin.id}>
                    {skin.label}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                皮肤选择会保存在当前浏览器；无法识别的旧配置会自动回退到默认皮肤。
              </p>
            </div>

            <ToggleSwitch
              checked={pendingSettings.showReasoning}
              onChange={(checked: boolean) => setPendingSettings((prev) => prev ? ({ ...prev, showReasoning: checked }) : null)}
              label="显示推理过程"
            />

            <ToggleSwitch
              checked={pendingSettings.enterToSend}
              onChange={(checked: boolean) => setPendingSettings((prev) => prev ? ({ ...prev, enterToSend: checked }) : null)}
              label="Enter 发送消息"
            />

            <ToggleSwitch
              checked={pendingSettings.foldThinkingAfterFinalAnswer ?? true}
              onChange={(checked: boolean) => setPendingSettings((prev) => prev ? ({ ...prev, foldThinkingAfterFinalAnswer: checked }) : null)}
              label="答案生成后折叠思维链"
            />

          </div>
          </> : null}

          {settingsTab === 'advanced' ? <AgentAdvancedSettingsPanel
            agentMode={pendingSettings.agentMode}
            systemPrompt={pendingSettings.systemPrompt}
            catalog={agentToolCatalog.tools}
            managerTools={pendingSettings.managerAgentTools}
            basicTools={pendingSettings.basicAgentTools}
            sessionRecord={sessionRecord}
            toolNotice={toolNotice}
            visionAutomationArmed={visionAutomationArmed}
            onAgentModeChange={(agentMode) => setPendingSettings((prev) => prev ? ({ ...prev, agentMode }) : null)}
            onSystemPromptChange={(systemPrompt) => setPendingSettings((prev) => prev ? ({ ...prev, systemPrompt }) : null)}
            onProfileToolsChange={(profile, tools) => setPendingSettings((prev) => prev ? ({ ...prev, [profile === 'manager_agent' ? 'managerAgentTools' : 'basicAgentTools']: tools }) : null)}
          /> : null}

          <button
            onClick={onReset}
            disabled={resetDisabled}
            className="px-4 py-2 bg-gray-500 text-white rounded-lg hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed text-sm"
          >
            重置为服务器默认值
          </button>
        </div>

        <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex justify-end space-x-3">
          <button
            onClick={onCancel}
            className="px-4 py-2 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
          >
            取消
          </button>
          <button
            onClick={onConfirm}
            className="px-4 py-2 bg-nvidia-500 text-white rounded-lg hover:bg-nvidia-600 transition-colors"
          >
            确认
          </button>
        </div>
      </div>
    </div>
  );
}

export default ConversationSettingsDialog;
