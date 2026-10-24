'use client';

import React from 'react';
import {
  AGENT_BUSINESS_TOOL_NAMES,
  getAgentToolRecommendations,
  getAgentToolSessionLockState,
  type AgentBusinessToolCatalogItem,
  type AgentToolProfile,
  type AgentToolSessionRecord,
} from '@/lib/agent-tool-configuration';

export interface AgentAdvancedSettingsPanelProps {
  agentMode: boolean;
  systemPrompt: string;
  catalog: AgentBusinessToolCatalogItem[];
  managerTools: string[];
  basicTools: string[];
  sessionRecord: AgentToolSessionRecord | null;
  disabled?: boolean;
  visionAutomationArmed?: boolean;
  toolNotice?: string | null;
  onAgentModeChange(value: boolean): void;
  onSystemPromptChange(value: string): void;
  onProfileToolsChange(profile: AgentToolProfile, tools: string[]): void;
}

function ToolChecklist(props: {
  profile: AgentToolProfile;
  tools: string[];
  catalog: AgentBusinessToolCatalogItem[];
  disabled: boolean;
  onChange(tools: string[]): void;
}) {
  const selected = new Set(props.tools);
  const items: AgentBusinessToolCatalogItem[] = props.catalog.length > 0
    ? props.catalog
    : AGENT_BUSINESS_TOOL_NAMES.map((name, order) => ({ name, order }));
  return <div className="space-y-1">
    <div className="text-xs font-medium text-gray-600 dark:text-gray-300">
      {props.profile === 'manager_agent' ? 'manager_agent 工具' : 'basic_agent 工具'}
    </div>
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-1 rounded-lg border border-gray-200 dark:border-gray-700 p-2">
      {items.map((item) => (
        <label key={item.name} className={`flex items-start gap-2 rounded px-2 py-1 text-xs ${props.disabled ? 'opacity-60' : 'hover:bg-gray-100 dark:hover:bg-gray-700'}`}>
          <input
            type="checkbox"
            checked={selected.has(item.name)}
            disabled={props.disabled}
            onChange={(event) => {
              const next = new Set(selected);
              if (event.target.checked) next.add(item.name);
              else next.delete(item.name);
              const ordered = items.filter((candidate) => next.has(candidate.name)).map((candidate) => candidate.name);
              props.onChange(ordered);
            }}
          />
          <span><span className="font-medium">{item.label ?? item.name}</span>{item.description ? <span className="block text-gray-500 dark:text-gray-400">{item.description}</span> : null}</span>
        </label>
      ))}
    </div>
  </div>;
}

export function AgentAdvancedSettingsPanel({
  agentMode,
  systemPrompt,
  catalog,
  managerTools,
  basicTools,
  sessionRecord,
  disabled = false,
  visionAutomationArmed = false,
  toolNotice = null,
  onAgentModeChange,
  onSystemPromptChange,
  onProfileToolsChange,
}: AgentAdvancedSettingsPanelProps) {
  const locked = sessionRecord?.kind === 'locked';
  const sessionLockState = getAgentToolSessionLockState(sessionRecord);
  const pending = sessionLockState.kind === 'pending';
  const managerSelected = locked ? sessionRecord.snapshot.manager_agent_tools : managerTools;
  const basicSelected = locked ? sessionRecord.snapshot.basic_agent_tools : basicTools;
  const checklistDisabled = disabled || !agentMode || locked || pending;
  const recommendations = [
    ...getAgentToolRecommendations('manager_agent', managerSelected),
    ...getAgentToolRecommendations('basic_agent', basicSelected),
  ];
  return <div className="space-y-4">
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 p-3 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <label className="text-sm font-medium text-gray-700 dark:text-gray-300">Agent 模式（工具调用）</label>
        <input
          type="checkbox"
          checked={agentMode}
          disabled={disabled || visionAutomationArmed}
          onChange={(event) => onAgentModeChange(event.target.checked)}
        />
      </div>
      {visionAutomationArmed ? <p className="text-xs text-amber-600 dark:text-amber-400">Vision automation 运行期间不能切换 Chat/Agent 模式。</p> : null}
      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">系统提示词（仅普通 Chat 生效）</label>
      <textarea
        value={systemPrompt}
        disabled={disabled || agentMode}
        onChange={(event) => onSystemPromptChange(event.target.value)}
        rows={2}
        className={`w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm text-gray-900 dark:text-white resize-none ${agentMode ? 'opacity-60 cursor-not-allowed' : ''}`}
      />
      {agentMode ? <p className="text-xs text-amber-600 dark:text-amber-400">Agent 模式使用后端 Agent 系统提示词，普通 Chat 用户自定义提示词暂不生效。</p> : null}
    </div>
    {pending ? <div className="rounded border border-sky-300 bg-sky-50 dark:border-sky-700 dark:bg-sky-950/30 p-2 text-xs text-sky-800 dark:text-sky-100">当前 Agent 请求正在等待后端接纳；工具列表与服务器默认值暂时锁定，接纳失败后可重新发送并重新配置。</div> : null}
    {locked ? <div className="rounded border border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/30 p-2 text-xs text-amber-800 dark:text-amber-100">当前 Agent context 的工具配置已经锁定；如需重新配置，请先使用“清空对话”清除该 context。</div> : null}
    {toolNotice ? <div className="rounded border border-yellow-300 bg-yellow-50 dark:border-yellow-700 dark:bg-yellow-950/30 p-2 text-xs text-yellow-800 dark:text-yellow-100">{toolNotice}</div> : null}
    <ToolChecklist profile="manager_agent" tools={managerSelected} catalog={catalog} disabled={checklistDisabled} onChange={(tools) => onProfileToolsChange('manager_agent', tools)} />
    <ToolChecklist profile="basic_agent" tools={basicSelected} catalog={catalog} disabled={checklistDisabled} onChange={(tools) => onProfileToolsChange('basic_agent', tools)} />
    {recommendations.map((recommendation) => <div key={`${recommendation.profile}-${recommendation.selectedTool}-${recommendation.recommendedTool}`} className="rounded border border-sky-300 bg-sky-50 dark:border-sky-700 dark:bg-sky-950/30 p-2 text-xs text-sky-800 dark:text-sky-100">{recommendation.message}</div>)}
    {!agentMode && !pending && !locked ? <p className="text-xs text-gray-500 dark:text-gray-400">Agent 工具列表仅在 Agent 模式开启时可编辑，并作为未来空白 context 的默认值。</p> : null}
  </div>;
}

export default AgentAdvancedSettingsPanel;
