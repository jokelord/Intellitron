import { mergeServerDefaultsWithCache } from '../api.ts';
import { resolveAgentToolDefaults, type AgentBusinessToolCatalog } from '../agent-tool-configuration.ts';
import type { ChatSettings } from '../message-types.ts';
/** Config is authoritative; optional discovery never blocks its application. */
export async function loadConversationBootstrap<T, M>(input: {
  getConfig(): Promise<T>;
  applyConfig(config: T): void;
  listModels?(): Promise<M>;
  applyModels?(models: M): void;
  isCurrent(): boolean;
}): Promise<void> {
  const config = await input.getConfig();
  if (!input.isCurrent())
    return;
  input.applyConfig(config);
  if (!input.listModels)
    return;
  void input.listModels().then(models => {
    if (input.isCurrent())
      input.applyModels?.(models);
  }).catch(() => {
  });
}
export function resolveConversationDefaults(defaults: ChatSettings, previous: ChatSettings, loaded: boolean, cached: Record<string, unknown> | null, tools: AgentBusinessToolCatalog | null): ChatSettings {
  const base = mergeServerDefaultsWithCache(defaults, cached);
  return {
    ...base,
    ...resolveAgentToolDefaults(tools, cached),
    maxTokens: Math.max(1, Math.min(base.maxTokens ?? defaults.maxTokens, defaults.maxTokens)),
    agentMode: loaded ? previous.agentMode : base.agentMode,
    enableThinking: loaded ? previous.enableThinking : base.enableThinking,
    enableLocalMemory: loaded ? previous.enableLocalMemory : base.enableLocalMemory,
  };
}
