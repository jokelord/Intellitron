'use client';

import React, { useEffect, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { AgentStep, OrchestrationStage, OrchestrationTool, OrchestrationSummary } from '@/lib/api';
import { getDispatchCardCopy, getDispatchCardLiveText, getDispatchMarqueeSegments } from '@/lib/dispatch-message';
import { getOrdinaryOrchestrationStages } from '@/lib/timeline-projection';
import CollapsibleBubbleContent from './CollapsibleBubbleContent';

/**
 * 步骤图标映射
 */
const STEP_ICON: Record<string, string> = {
  thought: '🤔',
  tool_call: '🔧',
  observation: '👁',
  orchestration: '🎯',
  dispatch: '🟠',
};

/**
 * 步骤标签映射
 */
const STEP_LABEL: Record<string, string> = {
  thought: '思考',
  tool_call: '工具调用',
  observation: '观察结果',
  orchestration: '编排执行',
  dispatch: '任务调度',
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
  orchestration: {
    bg: 'bg-indigo-50 dark:bg-indigo-900/20',
    border: 'border-indigo-200 dark:border-indigo-700/50',
    text: 'text-indigo-700 dark:text-indigo-300',
  },
  dispatch: {
    bg: 'bg-slate-900',
    border: 'border-slate-700',
    text: 'text-slate-100',
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

const WAITING_TOOL_NAMES = new Set([
  'search_and_fetch',
  'wikipedia_search_and_fetch',
]);

/** Status icon for orchestration tool */
const ORCH_STATUS_ICON: Record<string, string> = {
  pending: '⏳',
  running: '🔄',
  completed: '✅',
  failed: '❌',
  skipped: '⏭️',
  timeout: '⏰',
};

const ORCH_STATUS_COLORS: Record<string, string> = {
  pending: 'text-gray-400 dark:text-gray-500',
  running: 'text-blue-500 dark:text-blue-400',
  completed: 'text-green-500 dark:text-green-400',
  failed: 'text-red-500 dark:text-red-400',
  skipped: 'text-yellow-500 dark:text-yellow-400',
  timeout: 'text-orange-500 dark:text-orange-400',
};

function OrchestrationToolCard({
  tool,
  bubbleCharacterLimit,
}: {
  tool: OrchestrationTool;
  bubbleCharacterLimit: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const icon = ORCH_STATUS_ICON[tool.status] || '❓';

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-md p-2 min-w-[140px] bg-white/50 dark:bg-gray-800/50">
      <button
        type="button"
        className="flex w-full items-center gap-1 cursor-pointer text-left"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        <span>{icon}</span>
        <span className="font-mono text-xs text-gray-700 dark:text-gray-200 truncate">{tool.name}</span>
        {tool.duration_ms != null && (
          <span className="text-xs text-gray-400 dark:text-gray-500 ml-auto">{(tool.duration_ms / 1000).toFixed(1)}s</span>
        )}
      </button>
      <div className="text-xs text-gray-500 truncate mt-0.5">
        {Object.entries(tool.args).slice(0, 2).map(([k, v]) => `${k}: ${String(v).slice(0, 30)}`).join(', ')}
      </div>
      {expanded && tool.result_preview && (
        <CollapsibleBubbleContent content={tool.result_preview} limit={bubbleCharacterLimit}>
          {(displayContent) => (
            <pre className="text-xs text-gray-500 dark:text-gray-400 mt-1 whitespace-pre-wrap max-h-40 overflow-auto">
              {displayContent}
            </pre>
          )}
        </CollapsibleBubbleContent>
      )}
    </div>
  );
}

function OrchestrationView({ stages, summary, bubbleCharacterLimit }: {
  stages: OrchestrationStage[];
  summary?: OrchestrationSummary;
  bubbleCharacterLimit: number;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const ordinaryStages = getOrdinaryOrchestrationStages(stages);
  const totalTools = ordinaryStages.reduce((sum, s) => sum + s.tools.length, 0);

  if (ordinaryStages.length === 0) {
    return null;
  }

  return (
    <div className="border border-indigo-200 dark:border-indigo-700/50 rounded-lg p-3 bg-indigo-50/50 dark:bg-indigo-900/20 my-2">
      {/* Header */}
      <button
        className="flex items-center gap-2 w-full text-left"
        onClick={() => setCollapsed(!collapsed)}
      >
        {collapsed ? <ChevronRight className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
        <span className="text-sm font-medium text-indigo-700 dark:text-indigo-300">
          🎯 编排执行 ({totalTools} 个工具, {stages.length} 个阶段)
        </span>
        {summary && (
          <span className="text-xs text-gray-500 dark:text-gray-400 ml-auto">
            {summary.completed}/{summary.total} 成功 · {(summary.duration_ms / 1000).toFixed(1)}s
          </span>
        )}
      </button>

      {!collapsed && (
        <div className="mt-2 space-y-2">
          {ordinaryStages.map((stage) => (
            <div key={stage.stage_id} className="pl-2 border-l-2 border-gray-300 dark:border-gray-700">
              <div className="text-xs text-gray-500 mb-1">
                Stage {stage.stage_id} {stage.parallel ? '(并行)' : '(串行)'}
              </div>
              <div className={stage.parallel ? 'flex flex-wrap gap-2' : 'space-y-2'}>
                {stage.tools.map((tool) => (
                  <OrchestrationToolCard
                    key={tool.call_id}
                    tool={tool}
                    bubbleCharacterLimit={bubbleCharacterLimit}
                  />
                ))}
              </div>
            </div>
          ))}

          {/* Summary bar */}
          {summary && (
            <div className="text-xs text-gray-500 dark:text-gray-400 pt-1 border-t border-gray-300 dark:border-gray-700">
              总耗时: {(summary.duration_ms / 1000).toFixed(1)}s | 
              完成: {summary.completed} | 
              {summary.failed > 0 && `失败: ${summary.failed} | `}
              {summary.timeout > 0 && `超时: ${summary.timeout} | `}
              {summary.skipped > 0 && `跳过: ${summary.skipped}`}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DispatchCard({ step }: { step: AgentStep }) {
  const isRunning = step.dispatch_status === 'running';
  const { statusText, subText } = getDispatchCardCopy(step);
  const liveText = getDispatchCardLiveText(step);
  const marqueeTrackClassName = isRunning
    ? 'dispatch-marquee-track is-running'
    : 'dispatch-marquee-track';
  const marqueeSegments = getDispatchMarqueeSegments(liveText, isRunning);

  return (
    <div className="rounded-xl border border-slate-700 bg-slate-900 text-slate-100 overflow-hidden shadow-sm">
      <div className="px-4 py-3">
        <div className="flex items-start gap-3">
          <span className="relative mt-1 inline-flex h-1.5 w-1.5 flex-shrink-0 rounded-full bg-amber-500">
            {isRunning && <span className="absolute inset-[-4px] rounded-full bg-amber-400/20 animate-ping motion-reduce:animate-none" />}
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-slate-100">{statusText}</div>
            <div
              className="mt-2 overflow-hidden text-xs text-slate-300"
              role="status"
              aria-live={isRunning ? 'polite' : 'off'}
            >
              <div className="dispatch-marquee-window">
                <div className={marqueeTrackClassName}>
                  {marqueeSegments.map((segment, segmentIndex) => (
                    <span
                      key={`${segmentIndex}-${segment}`}
                      className="dispatch-marquee-segment"
                      aria-hidden={segmentIndex > 0 ? true : undefined}
                    >
                      {segment}
                    </span>
                  ))}
                </div>
                {isRunning && (
                  <span className="dispatch-scan-overlay" />
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * 单个步骤卡片
 */
function StepCard({
  step,
  index,
  isLatestStep,
  isLatestActiveStep,
  bubbleCharacterLimit,
}: {
  step: AgentStep;
  index: number;
  isLatestStep: boolean;
  isLatestActiveStep: boolean;
  bubbleCharacterLimit: number;
}) {
  const colors = STEP_COLORS[step.step_type] || STEP_COLORS.thought;
  const icon = STEP_ICON[step.step_type] || '📌';
  const label = STEP_LABEL[step.step_type] || step.step_type;

  const isStreaming = step.partial === true;
  const [rawToolCallExpanded, setRawToolCallExpanded] = useState(false);

  // Handle orchestration steps
  if (step.step_type === 'orchestration' && step.orchestration_type === 'plan' && step.stages) {
    return (
      <OrchestrationView
        stages={step.stages}
        summary={step.orchestration_summary}
        bubbleCharacterLimit={bubbleCharacterLimit}
      />
    );
  }
  // orchestration update/done events are handled in page state management, not rendered directly
  if (step.step_type === 'orchestration') {
    return null;
  }

  if (step.step_type === 'dispatch') {
    return <DispatchCard step={step} />;
  }

  const fullContent = step.step_type === 'tool_call'
    ? `${step.name || ''}(${formatToolInput(step.input)})`
    : (step.content || '');
  const shouldShowWaitingHourglass = step.step_type === 'tool_call'
    && Boolean(step.name && WAITING_TOOL_NAMES.has(step.name))
    && isLatestStep;

  return (
    <div className={`border rounded-lg ${colors.border} ${colors.bg} overflow-hidden`}>
      <div className="w-full flex items-center gap-2 px-3 py-2 text-left text-sm">
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
        {shouldShowWaitingHourglass && (
          <span
            className="tool-wait-hourglass text-base flex-shrink-0"
            aria-label="工具执行中，请耐心等待"
            role="status"
          >
            ⏳
          </span>
        )}
        {/* 流式输出中显示呼吸光标 */}
        {isStreaming && (
          <span className={`inline-block w-1.5 h-3.5 rounded-sm ${colors.text} opacity-80 animate-pulse flex-shrink-0`} />
        )}
      </div>

      <div className="px-3 pb-2">
        <CollapsibleBubbleContent content={fullContent} limit={bubbleCharacterLimit} streaming={isStreaming || isLatestActiveStep}>
          {(displayContent) => (
            <pre className="text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words font-mono leading-relaxed">
              {displayContent}
            </pre>
          )}
        </CollapsibleBubbleContent>
        {step.step_type === 'thought' && step.raw_tool_call && (
          <div className="mt-2 border-t border-blue-200/70 pt-2 dark:border-blue-800/70">
            <button
              type="button"
              className="flex items-center gap-1 text-xs text-blue-700 dark:text-blue-300"
              aria-expanded={rawToolCallExpanded}
              onClick={() => setRawToolCallExpanded((expanded) => !expanded)}
            >
              {rawToolCallExpanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
              {step.raw_tool_call_source === 'provider_payload' ? '查看 API 工具调用' : '查看原始工具调用'}
            </button>
            {rawToolCallExpanded && (
              <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded bg-white/60 p-2 text-xs text-gray-700 dark:bg-black/20 dark:text-gray-300">
                {step.raw_tool_call}
              </pre>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

type AgentStepDisplayProps = {
  steps: AgentStep[];
  renderAfterStep?: (step: AgentStep, index: number) => React.ReactNode;
  bubbleCharacterLimit?: number;
  turnComplete?: boolean;
  autoFoldOnTurnComplete?: boolean;
  defaultCollapsed?: boolean;
};

/**
 * Agent 步骤展示组件
 *
 * 以可折叠的卡片列表展示 Agent 执行过程中的思考、工具调用和观察结果。
 * defaultCollapsed 控制推理开始时的默认状态；完成后可按配置自动折叠。
 * 用户手动展开或收起后，手动状态优先。
 */
export default function AgentStepDisplay({
  steps,
  renderAfterStep,
  bubbleCharacterLimit = 20_000,
  turnComplete = false,
  autoFoldOnTurnComplete = false,
  defaultCollapsed = false,
}: AgentStepDisplayProps) {
  const shouldAutoFold = autoFoldOnTurnComplete && turnComplete;
  const [collapsed, setCollapsed] = useState(defaultCollapsed || shouldAutoFold);
  const [manualOverride, setManualOverride] = useState(false);

  useEffect(() => {
    if (!manualOverride) {
      setCollapsed(defaultCollapsed || shouldAutoFold);
    }
  }, [defaultCollapsed, shouldAutoFold, manualOverride]);

  if (steps.length === 0) return null;

  return (
    <div className="mb-2">
      <button
        type="button"
        aria-expanded={!collapsed}
        onClick={() => {
          setManualOverride(true);
          setCollapsed(!collapsed);
        }}
        className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 mb-1"
      >
        {collapsed ? <ChevronRight className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        <span>思维链 ({steps.length} 步)</span>
      </button>

      {!collapsed && (
        <div className="space-y-1.5">
          {steps.map((step, i) => (
            <React.Fragment key={step.id || `${step.step_type}-${i}`}>
              <StepCard
                step={step}
                index={i}
                isLatestStep={i === steps.length - 1}
                isLatestActiveStep={i === steps.length - 1 && !turnComplete}
                bubbleCharacterLimit={bubbleCharacterLimit}
              />
              {renderAfterStep?.(step, i)}
            </React.Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
