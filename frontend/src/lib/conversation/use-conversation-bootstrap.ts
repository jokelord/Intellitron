'use client';
import { useEffect, useRef } from 'react';
import { llmClient, getSavedChatSettings, parseCachedChatSettings, type ConfigResponse, type ModelInfo } from '../api.ts';
import { DEFAULT_SETTINGS, decideChatSettingsPersist } from '../message-types.ts';
import { loadConversationBootstrap, resolveConversationDefaults } from './conversation-bootstrap.ts';
import { createVoiceInputConfig } from '../voice-input.ts';
import { readDictationMagicSendSettings } from '../dictation-magic-send.ts';
import { readAgentToolServerConfig, getUnknownAgentToolIds } from '../agent-tool-configuration.ts';
import { applyWebUISkinOverride, readStoredWebUISkinId, getWebUIMessageTemplateDefaults, readStoredWebUIMessageTemplates, resolveWebUIMessageTemplates, readStoredWebUIMessageTemplateImmediateSend, getWebUIFoldThinkingAfterFinalAnswer } from '../web-ui-config.ts';
import { normalizeCycleConfig, type CycleConfig } from '../cycle-types.ts';
import type { ConversationState } from './use-conversation-state.ts';
interface BootstrapOptions {
  pageMode: 'text' | 'vision';
  state: ConversationState;
  onConfig?(config: ConfigResponse): void;
  onModelType?(config: ConfigResponse): boolean;
  onModels?(models: ModelInfo[]): void;
  setCycleConfig(config: CycleConfig): void;
}
export function useConversationBootstrap(options: BootstrapOptions) {
  const latest = useRef(options);
  latest.current = options;
  const { state } = options;
  useEffect(() => {
    const saved = localStorage.getItem('llm-chat-settings');
    state.setSettings(getSavedChatSettings(saved ? DEFAULT_SETTINGS : {
      ...DEFAULT_SETTINGS,
      agentMode: true
    }));
    state.isHydratedRef.current = true;
  }, []);
  useEffect(() => {
    const decision = decideChatSettingsPersist(state.isHydratedRef.current, state.skippedInitialSettingsPersistRef.current);
    state.skippedInitialSettingsPersistRef.current = decision.skippedInitialPersist;
    if (decision.persist)
      localStorage.setItem('llm-chat-settings', JSON.stringify(state.settings));
  }, [state.settings]);
  useEffect(() => {
    let active = true;
    let pollInFlight = false;
    const applyConfig = (config: ConfigResponse) => {
      const { state: s } = latest.current;
      if (latest.current.onModelType?.(config))
        return;
      const storedSkinId = readStoredWebUISkinId();
      s.setSelectedSkinId(storedSkinId);
      s.setWebUIConfig(applyWebUISkinOverride(config.webUI, storedSkinId));
      const yamlTemplates = getWebUIMessageTemplateDefaults(config.webUI);
      s.setYamlMessageTemplates(yamlTemplates);
      s.setMessageTemplates(resolveWebUIMessageTemplates(yamlTemplates, readStoredWebUIMessageTemplates()));
      s.setMessageTemplateImmediateSend(readStoredWebUIMessageTemplateImmediateSend());
      const voice = config.asr ? createVoiceInputConfig(config.asr, config.server?.port) : null;
      s.setVoiceConfig(voice);
      s.setDictationMagicSendSettings(voice ? readDictationMagicSendSettings(window.localStorage, {
        commands: voice.dictationMagicSend.commands,
        pollIntervalMs: voice.dictationMagicSend.pollIntervalMs,
      }) : null);
      if (config.model) {
        s.setModelName(config.model.model_name);
        if (config.model.max_context_length)
          s.setMaxContextLength(config.model.max_context_length);
      }
      latest.current.setCycleConfig(normalizeCycleConfig(config.cycle));
      latest.current.onConfig?.(config);
      if (!config.inference)
        return;
      s.setRuntimeContextConfig({
        enable_runtime_time_context: config.runtime_prompts?.enable_runtime_time_context ?? false
      });
      s.setAgentEntryAgentName(config.agent?.entry_agent_name ?? 'coordinator_1');
      const tools = readAgentToolServerConfig(config.agent);
      if (tools) {
        s.setAgentToolCatalog(tools);
        const unknown = getUnknownAgentToolIds(tools, parseCachedChatSettings());
        if (unknown.length && !s.unknownAgentToolNoticeShownRef.current) {
          s.unknownAgentToolNoticeShownRef.current = true;
          s.setAgentToolNotice(`已忽略浏览器缓存中的未知工具：${unknown.join('、')}。它们不会被发送给 Agent。`);
        }
      }
      const defaults = {
        temperature: config.inference.temperature,
        topP: config.inference.top_p,
        topK: config.inference.top_k,
        repetitionPenalty: config.inference.repetition_penalty,
        maxTokens: config.inference.max_tokens,
        systemPrompt: DEFAULT_SETTINGS.systemPrompt,
        showReasoning: DEFAULT_SETTINGS.showReasoning,
        enterToSend: DEFAULT_SETTINGS.enterToSend,
        enableThinking: DEFAULT_SETTINGS.enableThinking,
        enableLocalMemory: DEFAULT_SETTINGS.enableLocalMemory,
        agentMode: config.agent?.enabled ?? false,
        managerAgentTools: tools?.default_profile_tools.manager_agent ?? DEFAULT_SETTINGS.managerAgentTools,
        basicAgentTools: tools?.default_profile_tools.basic_agent ?? DEFAULT_SETTINGS.basicAgentTools,
        foldThinkingAfterFinalAnswer: getWebUIFoldThinkingAfterFinalAnswer(config.webUI),
      };
      s.setServerDefaults(defaults);
      const loaded = s.hasLoadedSettingsRef.current;
      s.setSettings(previous => resolveConversationDefaults(defaults, previous, loaded, parseCachedChatSettings(), tools));
      s.hasLoadedSettingsRef.current = true;
    };
    const poll = async () => {
      if (pollInFlight)
        return;
      pollInFlight = true;
      try {
        await loadConversationBootstrap({
          getConfig: () => llmClient.getConfig(),
          applyConfig,
          isCurrent: () => active,
          listModels: latest.current.onModels ? () => llmClient.listModels() : undefined,
          applyModels: models => latest.current.onModels?.(models),
        });
        if (!active)
          return;
        await llmClient.healthCheck();
        if (active)
          latest.current.state.setConnectionStatus('connected');
      }
      catch (error) {
        if (active) {
          latest.current.state.setConnectionStatus('error');
          console.error('获取配置失败:', error);
        }
      }
      finally {
        pollInFlight = false;
      }
    };
    void poll();
    const interval = window.setInterval(() => void poll(), 30000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, []);
}
