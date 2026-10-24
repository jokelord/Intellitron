import { llmClient, parseCachedChatSettings, type KnownRequestRejection } from '../api.ts';
import { formatAgentToolConfigurationRejection, getUnknownAgentToolIds, readAgentToolServerConfig, resolveAgentToolDefaults, } from '../agent-tool-configuration.ts';
import type { ConversationState } from './use-conversation-state.ts';
export type ConversationToolAdmissionState = Pick<ConversationState, 'dispatchAgentToolState' | 'setAgentToolCatalog' | 'setSettings' | 'setServerDefaults' | 'unknownAgentToolNoticeShownRef' | 'setAgentToolNotice'>;
export interface ConversationToolRejectionOptions {
  state: ConversationToolAdmissionState;
  sessionId: string;
  requestId: string;
  rejection: Pick<KnownRequestRejection, 'code' | 'message'>;
  isCurrent(): boolean;
  getConfig?(): Promise<{
    agent?: unknown;
  }>;
  getCachedSettings?(): Record<string, unknown> | null;
}
/** Caller admits the rejection; reducer IDs and the attachment guard protect later writes. */
export function rejectConversationToolAdmission(options: ConversationToolRejectionOptions) {
  const { state, sessionId, requestId, rejection } = options;
  state.dispatchAgentToolState({
    type: 'reject',
    sessionId,
    requestId
  });
  const code = rejection.code;
  const isToolConfigurationError = code === 'tool_catalog_changed'
    || code === 'tool_configuration_locked'
    || code === 'invalid_tool_configuration';
  const visibleMessage = isToolConfigurationError
    ? formatAgentToolConfigurationRejection(code, sessionId)
    : rejection.message;
  const refresh = code === 'tool_catalog_changed'
    ? (options.getConfig ?? (() => llmClient.getConfig()))().then(config => {
      if (!options.isCurrent())
        return;
      const refreshed = readAgentToolServerConfig(config.agent);
      if (!refreshed)
        return;
      const cached = (options.getCachedSettings ?? parseCachedChatSettings)();
      state.setAgentToolCatalog(refreshed);
      const unknown = getUnknownAgentToolIds(refreshed, cached);
      if (unknown.length > 0 && !state.unknownAgentToolNoticeShownRef.current) {
        state.unknownAgentToolNoticeShownRef.current = true;
        state.setAgentToolNotice(`已忽略浏览器缓存中的未知工具：${unknown.join('、')}。它们不会被发送给 Agent。`);
      }
      const defaults = resolveAgentToolDefaults(refreshed, cached);
      state.setSettings(current => ({
        ...current,
        ...defaults
      }));
      state.setServerDefaults(current => current ? {
        ...current,
        ...defaults
      } : current);
    }).catch(() => undefined)
    : Promise.resolve();
  return {
    isToolConfigurationError,
    visibleMessage,
    refresh
  };
}
