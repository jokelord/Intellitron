"use client";

import React from 'react';
import { Camera, RefreshCw, Upload } from 'lucide-react';
import CycleEditor from '@/components/CycleEditor';
import CycleProgress from '@/components/CycleProgress';
import type { CycleConfig, CycleExecutionStatus, CycleSSEEvent, CycleStep, ScheduledMessage } from '@/lib/cycle-types';

export interface ConversationTextInputActionsProps {
  visible: boolean;
  cycleConfig: CycleConfig;
  cycleStatus: CycleExecutionStatus;
  cycleEvents: CycleSSEEvent[];
  showCycle: boolean;
  onToggleCycle(): void;
  onCycleStart(steps: CycleStep[], scheduledMessages: ScheduledMessage[], shareContext: boolean, showAgentSteps: boolean): void | Promise<void>;
  onCycleStop(): void;
  messageTemplateMenu: React.ReactNode;
  testId?: string;
}

/** Common input actions for routes without active image/camera capability. */
export function ConversationTextInputActions({
  visible, cycleConfig, cycleStatus, cycleEvents, showCycle, onToggleCycle,
  onCycleStart, onCycleStop, messageTemplateMenu, testId = 'text-input-action-grid',
}: ConversationTextInputActionsProps) {
  if (!visible) return null;
  return (
    <div className="space-y-2">
      <div data-testid={testId} className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <button
          disabled
          title="仅视觉模式可用"
          className="flex-1 flex items-center justify-center gap-2 bg-white dark:bg-gray-800 px-4 py-2 border-2 border-dashed rounded-lg border-gray-200 dark:border-gray-700 text-gray-400 disabled:cursor-not-allowed disabled:opacity-60"
        >
          <Upload className="w-5 h-5" />
          <span className="text-sm">上传图片</span>
        </button>
        <button
          disabled
          title="仅视觉模式可用"
          className="flex-1 flex items-center justify-center gap-2 bg-white dark:bg-gray-800 px-4 py-2 border-2 border-dashed rounded-lg border-gray-200 dark:border-gray-700 text-gray-400 disabled:cursor-not-allowed disabled:opacity-60"
        >
          <Camera className="w-5 h-5" />
          <span className="text-sm">摄像头拍照</span>
        </button>
        <button
          disabled={!cycleConfig.enabled}
          title={cycleConfig.enabled ? '编程循环' : '编程循环未启用'}
          onClick={onToggleCycle}
          className={`flex-1 flex items-center justify-center gap-2 px-4 py-2 border-2 border-dashed rounded-lg transition-colors ${showCycle
              ? 'border-nvidia-500 bg-white dark:bg-gray-800 [background-image:linear-gradient(rgba(118,185,0,0.1),rgba(118,185,0,0.1))]'
              : cycleStatus === 'running'
                ? 'border-blue-500 bg-white dark:bg-gray-800 [background-image:linear-gradient(rgba(59,130,246,0.1),rgba(59,130,246,0.1))]'
                : 'bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-600 hover:border-nvidia-500 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:hover:border-gray-300 disabled:hover:bg-transparent disabled:cursor-not-allowed disabled:opacity-60'
            }`}
        >
          <RefreshCw className={`w-5 h-5 ${showCycle ? 'text-nvidia-500'
              : cycleStatus === 'running' ? 'text-blue-400 animate-spin'
                : 'text-gray-500'
            }`} />
          <span className={`text-sm ${showCycle ? 'text-nvidia-500'
              : cycleStatus === 'running' ? 'text-blue-400'
                : 'text-gray-600 dark:text-gray-400'
            }`}>
            {cycleStatus === 'running' ? '循环运行中' : '编程循环'}
          </span>
        </button>
        {messageTemplateMenu}
      </div>
      {showCycle && cycleConfig.enabled && (
        <div className="space-y-2">
          <CycleEditor
            isVisionMode={false}
            maxDepth={cycleConfig.max_depth}
            defaultSteps={cycleConfig.default_sequence}
            maxScheduledMessageDays={cycleConfig.max_scheduled_message_days}
            defaultScheduledMessages={cycleConfig.default_scheduled_messages}
            executionStatus={cycleStatus}
            onStart={onCycleStart}
            onStop={onCycleStop}
          />
          {cycleStatus !== 'idle' && (
            <CycleProgress status={cycleStatus} events={cycleEvents} />
          )}
        </div>
      )}
    </div>
  );
}

export default ConversationTextInputActions;
