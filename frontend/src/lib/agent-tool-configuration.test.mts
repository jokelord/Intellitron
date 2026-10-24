import assert from 'node:assert/strict';
import test from 'node:test';
import {
  AGENT_BUSINESS_TOOL_NAMES,
  EMPTY_AGENT_TOOL_SESSION_STATE,
  buildAgentToolConfigurationCandidate,
  getAgentToolRecommendations,
  reduceAgentToolSessionState,
  resolveAgentToolDefaults,
  resolvePairedAgentSessionId,
  readAgentToolServerConfig,
  getAgentToolSessionLockState,
  getUnknownAgentToolIds,
  formatAgentToolConfigurationRejection,
  type AgentBusinessToolCatalog,
  type AgentToolConfigurationCandidate,
  type AgentToolConfigurationSnapshot,
} from './agent-tool-configuration.ts';
import { parseRequestAcceptedEvent } from './vision-automation/send-contract.ts';

const catalog: AgentBusinessToolCatalog = {
  schema_version: 1,
  catalog_revision: 'catalog-1',
  tools: AGENT_BUSINESS_TOOL_NAMES.map((name, index) => ({ name, order: index })),
  default_profile_tools: {
    manager_agent: ['read_file', 'write_file', 'edit_file', 'exec_command'],
    basic_agent: ['read_file'],
  },
};

test('cache key presence wins independently and [] remains an explicit value', () => {
  const values = resolveAgentToolDefaults(catalog, {
    managerAgentTools: [],
    basicAgentTools: ['grep', 'unknown', 'grep'],
  });
  assert.deepEqual(values.managerAgentTools, []);
  assert.deepEqual(values.basicAgentTools, ['grep']);
});

test('missing or malformed cache values use independent server defaults', () => {
  const values = resolveAgentToolDefaults(catalog, { managerAgentTools: 'bad' });
  assert.deepEqual(values.managerAgentTools, catalog.default_profile_tools.manager_agent);
  assert.deepEqual(values.basicAgentTools, catalog.default_profile_tools.basic_agent);
});

test('candidate is snake_case and normalizes catalog order without session data', () => {
  const candidate = buildAgentToolConfigurationCandidate({
    managerAgentTools: ['exec_command', 'read_file'],
    basicAgentTools: ['read_file'],
  }, catalog);
  assert.deepEqual(candidate.manager_agent_tools, ['read_file', 'exec_command']);
  assert.deepEqual(candidate.basic_agent_tools, ['read_file']);
  assert.equal('session_id' in candidate, false);
  assert.equal('locked' in candidate, false);
});

test('recommendations contain only the three approved non-blocking cases', () => {
  assert.deepEqual(getAgentToolRecommendations('basic_agent', ['read_skill']).map((item) => item.recommendedTool), ['list_skills']);
  assert.deepEqual(getAgentToolRecommendations('basic_agent', ['write_file']).map((item) => item.recommendedTool), ['read_file']);
  assert.deepEqual(getAgentToolRecommendations('basic_agent', ['edit_file']).map((item) => item.recommendedTool), ['read_file']);
});

const candidate: AgentToolConfigurationCandidate = {
  schema_version: 1,
  catalog_revision: 'catalog-1',
  manager_agent_tools: ['read_file'],
  basic_agent_tools: [],
};
const snapshot: AgentToolConfigurationSnapshot = {
  ...candidate,
  locked: true,
  fingerprint: 'fingerprint-a',
};

test('reducer locks and rejects only exact ID/request, while stale callbacks are no-ops', () => {
  const pending = reduceAgentToolSessionState(EMPTY_AGENT_TOOL_SESSION_STATE, {
    type: 'begin', sessionId: 'A', requestId: 'request-A', candidate,
  });
  const stale = reduceAgentToolSessionState(pending, {
    type: 'accept', sessionId: 'A', requestId: 'wrong', snapshot,
  });
  assert.equal(stale, pending);
  const locked = reduceAgentToolSessionState(pending, {
    type: 'accept', sessionId: 'A', requestId: 'request-A', snapshot,
  });
  assert.equal(locked.sessions.A?.kind, 'locked');
  const staleB = reduceAgentToolSessionState(locked, {
    type: 'recover', sessionId: 'B', snapshot: { ...snapshot, fingerprint: 'B' },
  });
  assert.equal(staleB, locked);
});

test('pair survives OFF/ON and allocator is not called until explicit clear', () => {
  let state = reduceAgentToolSessionState(EMPTY_AGENT_TOOL_SESSION_STATE, {
    type: 'begin', sessionId: 'A', requestId: 'request-A', candidate,
  });
  state = reduceAgentToolSessionState(state, { type: 'accept', sessionId: 'A', requestId: 'request-A', snapshot });
  let allocations = 0;
  assert.equal(resolvePairedAgentSessionId(state, () => { allocations += 1; return 'B'; }), 'A');
  assert.equal(allocations, 0);
  state = reduceAgentToolSessionState(state, { type: 'clear', sessionId: 'A' });
  assert.equal(resolvePairedAgentSessionId(state, () => { allocations += 1; return 'B'; }), 'B');
  assert.equal(allocations, 1);
});

test('accepted metadata is parsed as an immutable locked snapshot with exact session ID', () => {
  const accepted = parseRequestAcceptedEvent({
    type: 'request_accepted',
    request_id: 'request-A',
    session_id: 'A',
    tool_configuration: { ...snapshot },
  }, 'request-A');
  assert.equal(accepted?.sessionId, 'A');
  assert.equal(accepted?.toolConfiguration?.locked, true);
  assert.deepEqual(accepted?.toolConfiguration?.manager_agent_tools, ['read_file']);
});

test('pending and locked records expose distinct non-editable explanations', () => {
  const pending = { kind: 'pending' as const, sessionId: 'A', requestId: 'r', candidate };
  const locked = { kind: 'locked' as const, sessionId: 'A', snapshot };
  assert.deepEqual(getAgentToolSessionLockState(pending), { kind: 'pending', editable: false });
  assert.deepEqual(getAgentToolSessionLockState(locked), { kind: 'locked', editable: false });
  assert.equal(getAgentToolSessionLockState(null).editable, true);
});

test('unknown cached tool IDs are reported once and removed from selections', () => {
  const unknown = getUnknownAgentToolIds(catalog, { managerAgentTools: ['read_file', 'removed_tool'], basicAgentTools: ['old_tool'] });
  assert.deepEqual(unknown, ['removed_tool', 'old_tool']);
});

test('canonical agent business_tool_catalog shape wins over nested transition shape', () => {
  const parsed = readAgentToolServerConfig({
    business_tool_catalog: [{ name: 'read_file', order: 1 }],
    default_profile_tools: { manager_agent: ['read_file'], basic_agent: [] },
    tool_configuration_schema_version: 7,
    tool_catalog_revision: 'canonical-revision',
    tool_configuration: { schema_version: 1, catalog_revision: 'nested', tools: [], defaults: {} },
  });
  assert.deepEqual(parsed, {
    schema_version: 7,
    catalog_revision: 'canonical-revision',
    tools: [{ name: 'read_file', order: 1 }],
    default_profile_tools: { manager_agent: ['read_file'], basic_agent: [] },
  });
});

test('structured tool configuration errors have stable, actionable messages', () => {
  assert.match(formatAgentToolConfigurationRejection('tool_catalog_changed', 'A'), /目录已更新/);
  assert.match(formatAgentToolConfigurationRejection('tool_configuration_locked', 'A'), /session A/);
  assert.match(formatAgentToolConfigurationRejection('invalid_tool_configuration', 'A'), /无效/);
});
