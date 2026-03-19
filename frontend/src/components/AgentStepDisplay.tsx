'use client';

import React, { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { AgentStep } from '@/lib/api';

/**
 * 步骤图标映射
 */
const STEP_ICON: Record<string, string> = {
  thought: '🤔',
  tool_call: '🔧',
  observation: '👁',
};

/**
 * 步骤标签映射
 */
const STEP_LABEL: Record<string, string> = {
  thought: '思考',
  tool_call: '工具调用',
  observation: '观察结果',
};

/**
 * 步骤颜色映射
 */
const STEP_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  thought: {
    bg: 'bg-blue-50 dark:bg-blue-900/20',
    border: 'border-blue-200 dark:border-blue-800',
    text: 'text-blue-700 dark:text-blue-300',
  },
  tool_call: {
    bg: 'bg-amber-50 dark:bg-amber-900/20',
    border: 'border-amber-200 dark:border-amber-800',
    text: 'text-amber-700 dark:text-amber-300',
  },
  observation: {
    bg: 'bg-green-50 dark:bg-green-900/20',
    border: 'border-green-200 dark:border-green-800',
    text: 'text-green-700 dark:text-green-300',
  },
};

/**
 * 格式化工具调用输入
 */
function formatToolInput(input: string | Record<string, unknown> | undefined): string {
  if (!input) return '';
  if (typeof input === 'string') return input;
  try {
    return JSON.stringify(input, null, 2);
  } catch {
    return String(input);
  }
}

/**
 * 单个步骤卡片
 */
function StepCard({ step, index }: { step: AgentStep; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const colors = STEP_COLORS[step.step_type] || STEP_COLORS.thought;
  const icon = STEP_ICON[step.step_type] || '📌';
  const label = STEP_LABEL[step.step_type] || step.step_type;

  // 计算预览文本
  const fullContent = step.step_type === 'tool_call'
    ? `${step.name || ''}(${formatToolInput(step.input)})`
    : (step.content || '');
  const preview = fullContent.length > 120 ? fullContent.slice(0, 120) + '…' : fullContent;
  const hasMore = fullContent.length > 120;

  return (
    <div className={`border rounded-lg ${colors.border} ${colors.bg} overflow-hidden`}>
      <button
        onClick={() => hasMore && setExpanded(!expanded)}
        className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm ${hasMore ? 'cursor-pointer hover:opacity-80' : 'cursor-default'}`}
      >
        <span className="text-base flex-shrink-0">{icon}</span>
        <span className={`font-medium flex-shrink-0 ${colors.text}`}>
          #{index + 1} {label}
        </span>
        {step.step_type === 'tool_call' && step.name && (
          <code className={`text-xs px-1.5 py-0.5 rounded ${colors.text} bg-white/50 dark:bg-black/20`}>
            {step.name}
          </code>
        )}
        <span className="flex-1 min-w-0" />
        {hasMore && (
          expanded
            ? <ChevronDown className="w-4 h-4 flex-shrink-0 text-gray-400" />
            : <ChevronRight className="w-4 h-4 flex-shrink-0 text-gray-400" />
        )}
      </button>

      <div className="px-3 pb-2">
        <pre className="text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words font-mono leading-relaxed">
          {expanded ? fullContent : preview}
        </pre>
      </div>
    </div>
  );
}

/**
 * Agent 步骤展示组件
 *
 * 以可折叠的卡片列表展示 Agent 执行过程中的思考、工具调用和观察结果。
 */
export default function AgentStepDisplay({ steps }: { steps: AgentStep[] }) {
  const [collapsed, setCollapsed] = useState(false);

  if (steps.length === 0) return null;

  return (
    <div className="mb-2">
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 mb-1"
      >
        {collapsed ? <ChevronRight className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        <span>思维链 ({steps.length} 步)</span>
      </button>

      {!collapsed && (
        <div className="space-y-1.5">
          {steps.map((step, i) => (
            <StepCard key={i} step={step} index={i} />
          ))}
        </div>
      )}
    </div>
  );
}
