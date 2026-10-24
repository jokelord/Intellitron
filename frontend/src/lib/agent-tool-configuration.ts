/** Client-side defaults and exact-ID state for configurable Agent tools.
 *
 * The browser values in this module are only candidates for a future Agent
 * context.  Once the backend accepts a request, the accepted snapshot is the
 * source of truth and is never replaced by localStorage/settings changes.
 */

export const AGENT_TOOL_CONFIGURATION_SCHEMA_VERSION = 1;

export const AGENT_BUSINESS_TOOL_NAMES = [
  'read_file', 'write_file', 'edit_file', 'glob', 'grep', 'exec_command',
  'check_process', 'list_skills', 'read_skill',
  'web_search', 'web_fetch', 'search_and_fetch', 'wikipedia_search_and_fetch',
] as const;

export type AgentBusinessToolName = typeof AGENT_BUSINESS_TOOL_NAMES[number];
export type AgentToolProfile = 'manager_agent' | 'basic_agent';
export type AgentToolConfigurationRejectionCode =
  | 'tool_catalog_changed'
  | 'tool_configuration_locked'
  | 'invalid_tool_configuration';

export interface AgentBusinessToolCatalogItem {
  name: AgentBusinessToolName;
  order: number;
  label?: string;
  description?: string;
  available?: boolean;
}

export interface AgentBusinessToolCatalog {
  schema_version: number;
  catalog_revision: string;
  tools: AgentBusinessToolCatalogItem[];
  default_profile_tools: Record<AgentToolProfile, string[]>;
}

export interface AgentToolServerConfiguration {
  schema_version: number;
  catalog_revision: string;
  tools: AgentBusinessToolCatalogItem[];
  default_profile_tools: Record<AgentToolProfile, string[]>;
}

export interface AgentToolConfigurationCandidate {
  schema_version: number;
  catalog_revision: string;
  manager_agent_tools: string[];
  basic_agent_tools: string[];
}

export interface AgentToolConfigurationSnapshot extends AgentToolConfigurationCandidate {
  locked: true;
  fingerprint: string;
}

export type AgentToolSessionRecord =
  | { kind: 'pending'; sessionId: string; requestId: string; candidate: AgentToolConfigurationCandidate }
  | { kind: 'locked'; sessionId: string; snapshot: AgentToolConfigurationSnapshot };

export interface AgentToolSessionState {
  pairedSessionId: string | null;
  sessions: Readonly<Record<string, AgentToolSessionRecord>>;
}

export type AgentToolSessionAction =
  | { type: 'begin'; sessionId: string; requestId: string; candidate: AgentToolConfigurationCandidate }
  | { type: 'accept'; sessionId: string; requestId: string; snapshot: AgentToolConfigurationSnapshot }
  | { type: 'reject'; sessionId: string; requestId: string }
  | { type: 'recover'; sessionId: string; snapshot: AgentToolConfigurationSnapshot }
  | { type: 'clear'; sessionId: string };

const DEFAULT_MANAGER_TOOLS: AgentBusinessToolName[] = [
  'read_file', 'write_file', 'edit_file', 'glob', 'grep', 'exec_command',
  'list_skills', 'read_skill', 'web_fetch', 'search_and_fetch',
];
const DEFAULT_BASIC_TOOLS: AgentBusinessToolName[] = [
  'read_file', 'write_file', 'edit_file', 'glob', 'grep', 'exec_command',
  'check_process', 'list_skills', 'read_skill',
  'web_search', 'web_fetch', 'search_and_fetch',
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function catalogNames(catalog: AgentBusinessToolCatalog | null | undefined): string[] {
  return (catalog?.tools ?? []).filter((item) => item.available !== false)
    .sort((a, b) => a.order - b.order)
    .map((item) => item.name);
}

function normalizeSelection(value: unknown, available: readonly string[]): string[] {
  if (!Array.isArray(value)) return [];
  const selected = new Set(value.filter((name): name is string => typeof name === 'string'));
  return available.filter((name) => selected.has(name));
}

function normalizeDefault(
  value: unknown,
  fallback: readonly string[],
  available: readonly string[],
): string[] {
  if (!Array.isArray(value)) return normalizeSelection(fallback, available);
  return normalizeSelection(value, available);
}

/** Read both the canonical /config shape and the short-lived nested shape. */
export function readAgentToolServerConfig(value: unknown): AgentToolServerConfiguration | null {
  if (!isRecord(value)) return null;
  const nested = isRecord(value.tool_configuration) ? value.tool_configuration : null;
  const canonicalCatalog = isRecord(value.business_tool_catalog) ? value.business_tool_catalog : null;
  const rawTools = canonicalCatalog?.tools ?? value.business_tool_catalog ?? nested?.tools ?? value.tools;
  const rawDefaults = value.default_profile_tools ?? canonicalCatalog?.defaults ?? nested?.defaults ?? value.defaults;
  const toolEntries = Array.isArray(rawTools)
    ? rawTools
    : isRecord(rawTools)
      ? Object.entries(rawTools).map(([name, item], index) => ({ ...(isRecord(item) ? item : {}), name, order: isRecord(item) && Number.isInteger(item.order) ? item.order : index }))
      : null;
  if (!toolEntries || !isRecord(rawDefaults)) return null;
  const tools = toolEntries.flatMap((item) => {
    if (!isRecord(item) || typeof item.name !== 'string' || !Number.isInteger(item.order)) return [];
    return [{
      name: item.name as AgentBusinessToolName,
      order: item.order as number,
      ...(typeof item.label === 'string' ? { label: item.label } : {}),
      ...(typeof item.description === 'string' ? { description: item.description } : {}),
      ...(typeof item.available === 'boolean' ? { available: item.available } : {}),
    }];
  });
  const manager = rawDefaults.manager_agent;
  const basic = rawDefaults.basic_agent;
  if (!Array.isArray(manager) || !Array.isArray(basic)
    || !manager.every((item) => typeof item === 'string')
    || !basic.every((item) => typeof item === 'string')) return null;
  const schema = value.tool_configuration_schema_version ?? canonicalCatalog?.schema_version ?? nested?.schema_version ?? value.schema_version;
  const revision = value.tool_catalog_revision ?? canonicalCatalog?.catalog_revision ?? nested?.catalog_revision ?? value.catalog_revision;
  if (!Number.isInteger(schema) || typeof revision !== 'string') return null;
  return {
    schema_version: schema as number,
    catalog_revision: revision,
    tools,
    default_profile_tools: {
      manager_agent: [...manager] as string[],
      basic_agent: [...basic] as string[],
    },
  };
}

export function getUnknownAgentToolIds(
  catalog: AgentBusinessToolCatalog,
  rawCache: Record<string, unknown> | null | undefined,
): string[] {
  const names = new Set(catalogNames(catalog));
  const unknown = new Set<string>();
  for (const key of ['managerAgentTools', 'basicAgentTools']) {
    const values = isRecord(rawCache) ? rawCache[key] : undefined;
    if (!Array.isArray(values)) continue;
    for (const value of values) {
      if (typeof value === 'string' && !names.has(value)) unknown.add(value);
    }
  }
  return Array.from(unknown);
}

export function getAgentToolSessionLockState(
  record: AgentToolSessionRecord | null,
): { kind: 'none' | 'pending' | 'locked'; editable: boolean } {
  if (record?.kind === 'pending') return { kind: 'pending', editable: false };
  if (record?.kind === 'locked') return { kind: 'locked', editable: false };
  return { kind: 'none', editable: true };
}

export function formatAgentToolConfigurationRejection(
  code: AgentToolConfigurationRejectionCode,
  sessionId: string,
): string {
  if (code === 'tool_catalog_changed') {
    return '工具目录已更新，当前请求的工具配置与服务器不一致。配置已刷新，请重试本次请求。';
  }
  if (code === 'tool_configuration_locked') {
    return `Agent session ${sessionId} 的工具配置已经锁定，当前请求不能改变它；请继续使用该 session，或清空该 session 后重新配置。`;
  }
  return '工具配置无效，当前请求未被接纳。请检查工具选择后重试。';
}

/** Merge server defaults with cache by key presence, preserving [] as a value. */
export function resolveAgentToolDefaults(
  serverAgentConfig: Partial<AgentBusinessToolCatalog> | null | undefined,
  rawCache: Record<string, unknown> | null | undefined,
): { managerAgentTools: string[]; basicAgentTools: string[] } {
  const available = catalogNames(serverAgentConfig as AgentBusinessToolCatalog);
  const names = available.length > 0 ? available : [...AGENT_BUSINESS_TOOL_NAMES];
  const serverDefaults: Partial<Record<AgentToolProfile, string[]>> = serverAgentConfig?.default_profile_tools ?? {};
  const managerServerDefault = serverDefaults.manager_agent ?? DEFAULT_MANAGER_TOOLS;
  const basicServerDefault = serverDefaults.basic_agent ?? DEFAULT_BASIC_TOOLS;
  const manager = isRecord(rawCache) && Object.prototype.hasOwnProperty.call(rawCache, 'managerAgentTools')
    ? rawCache.managerAgentTools
    : managerServerDefault;
  const basic = isRecord(rawCache) && Object.prototype.hasOwnProperty.call(rawCache, 'basicAgentTools')
    ? rawCache.basicAgentTools
    : basicServerDefault;
  return {
    managerAgentTools: normalizeDefault(manager, managerServerDefault, names),
    basicAgentTools: normalizeDefault(basic, basicServerDefault, names),
  };
}

export function buildAgentToolConfigurationCandidate(
  defaults: { managerAgentTools: string[]; basicAgentTools: string[] },
  serverAgentConfig: Partial<AgentBusinessToolCatalog> | null | undefined,
): AgentToolConfigurationCandidate {
  const names = catalogNames(serverAgentConfig as AgentBusinessToolCatalog);
  const available = names.length > 0 ? names : [...AGENT_BUSINESS_TOOL_NAMES];
  return {
    schema_version: serverAgentConfig?.schema_version ?? AGENT_TOOL_CONFIGURATION_SCHEMA_VERSION,
    catalog_revision: serverAgentConfig?.catalog_revision ?? '',
    manager_agent_tools: normalizeSelection(defaults.managerAgentTools, available),
    basic_agent_tools: normalizeSelection(defaults.basicAgentTools, available),
  };
}

export interface AgentToolRecommendation {
  profile: AgentToolProfile;
  selectedTool: string;
  recommendedTool: string;
  message: string;
}

/** Non-blocking suggestions; this helper never changes a user's selection. */
export function getAgentToolRecommendations(
  profile: AgentToolProfile,
  selectedTools: readonly string[],
): AgentToolRecommendation[] {
  const selected = new Set(selectedTools);
  const recommendations: AgentToolRecommendation[] = [];
  if (selected.has('read_skill') && !selected.has('list_skills')) {
    recommendations.push({ profile, selectedTool: 'read_skill', recommendedTool: 'list_skills', message: '建议勾选 list_skills，便于先发现可用 Skill。' });
  }
  if (selected.has('write_file') && !selected.has('read_file')) {
    recommendations.push({ profile, selectedTool: 'write_file', recommendedTool: 'read_file', message: '建议勾选 read_file，便于写入前读取现有内容。' });
  }
  if (selected.has('edit_file') && !selected.has('read_file')) {
    recommendations.push({ profile, selectedTool: 'edit_file', recommendedTool: 'read_file', message: '建议勾选 read_file，便于编辑前读取现有内容。' });
  }
  return recommendations;
}

export function toAgentToolConfigurationWire(
  candidate: AgentToolConfigurationCandidate,
): AgentToolConfigurationCandidate {
  return {
    schema_version: candidate.schema_version,
    catalog_revision: candidate.catalog_revision,
    manager_agent_tools: [...candidate.manager_agent_tools],
    basic_agent_tools: [...candidate.basic_agent_tools],
  };
}

export const EMPTY_AGENT_TOOL_SESSION_STATE: AgentToolSessionState = Object.freeze({
  pairedSessionId: null,
  sessions: Object.freeze({}),
});

function sameCandidate(a: AgentToolConfigurationCandidate, b: AgentToolConfigurationCandidate): boolean {
  return a.schema_version === b.schema_version
    && a.catalog_revision === b.catalog_revision
    && JSON.stringify(a.manager_agent_tools) === JSON.stringify(b.manager_agent_tools)
    && JSON.stringify(a.basic_agent_tools) === JSON.stringify(b.basic_agent_tools);
}

/** Exact-ID reducer. Stale callbacks return the original state object. */
export function reduceAgentToolSessionState(
  state: AgentToolSessionState,
  action: AgentToolSessionAction,
): AgentToolSessionState {
  const current = state.sessions[action.sessionId];
  if (action.type === 'begin') {
    if (state.pairedSessionId !== null && state.pairedSessionId !== action.sessionId) return state;
    if (current?.kind === 'locked') return state;
    if (current?.kind === 'pending'
      && current.requestId === action.requestId
      && sameCandidate(current.candidate, action.candidate)) return state;
    return {
      pairedSessionId: state.pairedSessionId ?? action.sessionId,
      sessions: { ...state.sessions, [action.sessionId]: { kind: 'pending', sessionId: action.sessionId, requestId: action.requestId, candidate: toAgentToolConfigurationWire(action.candidate) } },
    };
  }
  if (action.type === 'accept') {
    if (!current || current.kind !== 'pending' || current.requestId !== action.requestId) return state;
    if (action.snapshot.locked !== true || action.snapshot.fingerprint.length === 0) return state;
    return { ...state, pairedSessionId: state.pairedSessionId ?? action.sessionId, sessions: { ...state.sessions, [action.sessionId]: { kind: 'locked', sessionId: action.sessionId, snapshot: { ...action.snapshot, manager_agent_tools: [...action.snapshot.manager_agent_tools], basic_agent_tools: [...action.snapshot.basic_agent_tools] } } } };
  }
  if (action.type === 'reject') {
    if (!current || current.kind !== 'pending' || current.requestId !== action.requestId) return state;
    const sessions = { ...state.sessions };
    delete sessions[action.sessionId];
    return { pairedSessionId: state.pairedSessionId === action.sessionId ? null : state.pairedSessionId, sessions };
  }
  if (action.type === 'recover') {
    if (state.pairedSessionId !== null && state.pairedSessionId !== action.sessionId) return state;
    if (current?.kind === 'locked' && current.snapshot.fingerprint === action.snapshot.fingerprint) return state;
    return { pairedSessionId: action.sessionId, sessions: { ...state.sessions, [action.sessionId]: { kind: 'locked', sessionId: action.sessionId, snapshot: action.snapshot } } };
  }
  if (action.type === 'clear') {
    if (!current && state.pairedSessionId !== action.sessionId) return state;
    const sessions = { ...state.sessions };
    delete sessions[action.sessionId];
    return { pairedSessionId: state.pairedSessionId === action.sessionId ? null : state.pairedSessionId, sessions };
  }
  return state;
}

export function resolvePairedAgentSessionId(
  state: AgentToolSessionState,
  allocate: () => string,
): string {
  if (state.pairedSessionId !== null) return state.pairedSessionId;
  return allocate();
}

export function getAgentToolSessionRecord(
  state: AgentToolSessionState,
  sessionId: string | null | undefined,
): AgentToolSessionRecord | null {
  return sessionId ? state.sessions[sessionId] ?? null : null;
}
