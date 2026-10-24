import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { getStreamingTerminalBehavior } from './answer-now-ui.ts';
import { LLMClient, type AgentSessionPayloadHandlers } from './api.ts';
import {
  createTextAgentSessionHandlers,
  settleKnownAnswerNowRejection,
  type TextAgentSessionHandlerEnvironment,
  type TextAgentSessionRunRef,
} from './text-agent-session-handlers.ts';
import type { AgentRunState, Message } from './message-types.ts';

type SessionPayload = Record<string, unknown> & { event_sequence: number };

const fixedNow = new Date('2026-08-22T00:00:00.000Z');
const fullObservation = `${'观'.repeat(200_000)}尾`;
const testDir = path.dirname(fileURLToPath(import.meta.url));

const firstTurn: SessionPayload[] = [
  { event_sequence: 1, type: 'session_start', session_id: 'session-1' },
  { event_sequence: 2, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'session-1', turn_index: 1, user_content: 'first question', images: [], mode: 'tool_calling' } },
  { event_sequence: 3, type: 'timeline_event', event: { kind: 'dispatch_delta', display: 'dispatch_card', dispatch_id: 'dispatch-1', dispatch_kind: 'routing', display_slot: 'before_body', text: 'routing', partial: true } },
  { event_sequence: 4, type: 'agent_step', step_type: 'thought', content: 'reasoning', partial: true },
  { event_sequence: 5, type: 'orchestration_plan', step_id: 'plan-1', stages: [{ stage_id: 1, parallel: true, tools: [{ call_id: 'call-1', name: 'dispatching_parallel_agent', args: { child_id: 'reader', task: 'read' }, status: 'running' }, { call_id: 'call-2', name: 'read_file', args: { path: 'a.txt' }, status: 'running' }] }] },
  { event_sequence: 6, type: 'orchestration_update', call_id: 'call-2', status: 'completed', result_preview: 'read', duration_ms: 3 },
  { event_sequence: 7, type: 'agent_step', step_id: 'observation-1', step_type: 'observation', content: fullObservation, partial: false },
  { event_sequence: 8, type: 'timeline_event', event: { kind: 'manager_child_agent_update', dispatch_id: 'dispatch-1', call_id: 'call-1', child_run_id: 'child-1', child_name: 'reader', status: 'completed', final_preview: 'done' } },
  { event_sequence: 9, type: 'orchestration_done', total_tools: 2, completed: 2, failed: 0, skipped: 0, timeout: 0, duration_ms: 8 },
  { event_sequence: 10, type: 'timeline_event', event: { kind: 'compact_started', summary_message_id: 'compact-1', trigger: 'auto', mode: 'agent', original_tokens: 42, threshold: 40, strategy: 'utilization' } },
  { event_sequence: 11, type: 'timeline_event', event: { kind: 'compact_delta', summary_message_id: 'compact-1', text: 'summary partial', partial: true } },
  { event_sequence: 12, type: 'timeline_event', event: { kind: 'compact_completed', summary_message_id: 'compact-1', text: 'summary complete', partial: false, metadata: { summary_tokens: 7 } } },
  { event_sequence: 13, type: 'timeline_event', event: { kind: 'message_delta', source: 'coordinator_2', display: 'chat_bubble', message_id: 'session-1-turn-1-coordinator-2', text: '<think>plan</think>answer {"type":"agent","name":"switch_agent"}', partial: false } },
  { event_sequence: 14, type: 'agent_step', step_type: 'dispatch', dispatch_id: 'handoff-1', dispatch_kind: 'secondary_handoff', display_slot: 'after_body', dispatch_status: 'completed', dispatch_stage: 'handoff', dispatch_target_agent: 'basic_agent', dispatch_message: 'handoff', partial: false },
  { event_sequence: 15, type: 'timeline_event', event: { kind: 'agent_switched', source: 'coordinator_2', target: 'basic_agent' } },
  { event_sequence: 16, type: 'content', content: 'executed answer' },
  { event_sequence: 17, type: 'done', session_id: 'session-1' },
];

const secondTurn: SessionPayload[] = [
  { event_sequence: 18, type: 'session_start', session_id: 'session-1' },
  { event_sequence: 19, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'session-1', turn_index: 2, user_content: 'second question', images: [], mode: 'tool_calling' } },
  { event_sequence: 20, type: 'timeline_event', event: { kind: 'dispatch_delta', display: 'dispatch_card', dispatch_id: 'dispatch-2', dispatch_kind: 'routing', display_slot: 'before_body', text: 'direct', partial: false } },
  { event_sequence: 21, type: 'content', content: 'second answer' },
  { event_sequence: 22, type: 'done', session_id: 'session-1' },
];

function responseFor(payloads: SessionPayload[]): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const payload of payloads) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
      }
      controller.close();
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

function createHarness(initialMessages: Message[] = []) {
  let messages = initialMessages;
  let sessionId: string | undefined;
  let createdId = 0;
  const terminalCalls: string[] = [];
  const activeRunRef: TextAgentSessionRunRef = { current: null };
  const environment: TextAgentSessionHandlerEnvironment = {
    activeRunRef,
    setMessages: (update) => { messages = update(messages); },
    setAgentSessionId: (value) => { sessionId = value; },
    resetStreamingState: () => { terminalCalls.push('reset-streaming'); },
    clearStoppedState: () => { terminalCalls.push('clear-stopped'); },
    clearAnswerNowFlag: () => { terminalCalls.push('clear-answer-now'); },
    clearStopFlag: () => { terminalCalls.push('clear-stop'); },
    getTerminalBehavior: (run) => getStreamingTerminalBehavior({
      stopWasClicked: run.stopWasClicked,
      answerNowWasClicked: run.answerNowWasClicked,
    }),
    newlyCompactMarkedRef: { current: new Set<string>() },
    defaultReasoningCollapsed: false,
    now: () => fixedNow,
    createId: (prefix) => `${prefix}-${++createdId}`,
  };

  const addOptimisticTurn = (content: string, ordinal: number) => {
    const userId = `optimistic-user-${ordinal}`;
    const assistantId = `optimistic-assistant-${ordinal}`;
    const run: AgentRunState = {
      assistantMessageId: assistantId,
      projectedAssistantMessageIds: new Set<string>(),
      stopWasClicked: false,
      answerNowWasClicked: false,
    };
    messages = [...messages, {
      id: userId,
      role: 'user',
      content,
      timestamp: fixedNow,
    }, {
      id: assistantId,
      role: 'assistant',
      content: '',
      timestamp: fixedNow,
      isStreaming: true,
      agentSteps: [{
        id: `optimistic-dispatch-${ordinal}`,
        step_type: 'dispatch',
        dispatch_status: 'running',
        dispatch_stage: 'routing',
        dispatch_kind: 'routing',
        display_slot: 'before_body',
      }],
    }];
    activeRunRef.current = run;
    return run;
  };

  return {
    environment,
    activeRunRef,
    addOptimisticTurn,
    resetSessionProjection: () => {
      messages = [];
      activeRunRef.current = null;
    },
    getMessages: () => messages,
    getSessionId: () => sessionId,
    getTerminalCalls: () => terminalCalls,
  };
}

test('raw tool-call debug merges into preceding partial thought without duplicating text', () => {
  const raw = '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{\n  "file_path": "/tmp/a"\n}}\n<<END_TOOL_CALL>>';
  const harness = createHarness();
  const run = harness.addOptimisticTurn('inspect', 1);
  const handlers = createTextAgentSessionHandlers(harness.environment, { current: run });

  handlers.onStep({ step_type: 'thought', content: 'reasoning', partial: true });
  handlers.onStep({ step_type: 'thought', raw_tool_call: raw, partial: false });

  const steps = harness.getMessages().find((message) => message.id === run.assistantMessageId)?.agentSteps ?? [];
  const thoughts = steps.filter((step) => step.step_type === 'thought');
  assert.equal(thoughts.length, 1);
  assert.equal(thoughts[0].content, 'reasoning');
  assert.equal(thoughts[0].raw_tool_call, raw);
  assert.equal(thoughts[0].partial, false);
});

test('raw tool-call debug also attaches to a preceding completed thought', () => {
  const raw = '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>';
  const harness = createHarness();
  const run = harness.addOptimisticTurn('inspect', 1);
  const handlers = createTextAgentSessionHandlers(harness.environment, { current: run });

  handlers.onStep({ step_type: 'thought', content: 'complete reasoning', partial: false });
  handlers.onStep({ step_type: 'thought', raw_tool_call: raw, partial: false });

  const thoughts = (harness.getMessages().find((message) => message.id === run.assistantMessageId)?.agentSteps ?? [])
    .filter((step) => step.step_type === 'thought');
  assert.equal(thoughts.length, 1);
  assert.equal(thoughts[0].content, 'complete reasoning');
  assert.equal(thoughts[0].raw_tool_call, raw);
});

async function sendLiveTurn(
  client: LLMClient,
  handlers: AgentSessionPayloadHandlers,
  message: string,
): Promise<void> {
  await client.agentChatStream(
    { session_id: 'session-1', message },
    handlers.onStep!,
    handlers.onContent!,
    handlers.onDone,
    handlers.onPayloadError,
    handlers.onSessionStart,
    undefined,
    handlers.onTimelineEvent,
  );
}

test('Text live POST and from-zero recovery GET use one handler operation and produce the same full multi-turn projection', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let liveRequest = 0;
  globalThis.fetch = async (_url, init) => {
    if (init?.method === 'POST') {
      const payloads = liveRequest++ === 0 ? firstTurn : secondTurn;
      return responseFor(payloads);
    }
    return responseFor([...firstTurn, ...secondTurn]);
  };

  const client = new LLMClient('/api/backend');
  const live = createHarness();
  const firstLiveRun = live.addOptimisticTurn('first question', 1);
  const firstLiveRunRef: TextAgentSessionRunRef = { current: firstLiveRun };
  const firstLiveHandlers = createTextAgentSessionHandlers(live.environment, firstLiveRunRef);
  await sendLiveTurn(client, firstLiveHandlers, 'first question');
  assert.equal(firstLiveRunRef.current, null, 'terminal must retire the completed operation ref');

  const secondLiveRun = live.addOptimisticTurn('second question', 2);
  const secondLiveRunRef: TextAgentSessionRunRef = { current: secondLiveRun };
  const secondLiveHandlers = createTextAgentSessionHandlers(live.environment, secondLiveRunRef);
  await sendLiveTurn(client, secondLiveHandlers, 'second question');

  const recovery = createHarness([{
    id: 'stale-session-projection',
    role: 'assistant',
    content: 'must be reset before from-zero replay',
    timestamp: fixedNow,
  }]);
  recovery.resetSessionProjection();
  const recoveryRunRef: TextAgentSessionRunRef = { current: null };
  const recoveryHandlers = createTextAgentSessionHandlers(recovery.environment, recoveryRunRef);
  await client.agentSessionEvents('session-1', 0, recoveryHandlers);

  assert.deepEqual(recovery.getMessages(), live.getMessages());
  assert.equal(recovery.getSessionId(), 'session-1');
  assert.equal(recoveryRunRef.current, null);
  assert.equal(recovery.activeRunRef.current, null);
  assert.equal(recovery.getMessages().some((message) => message.id.startsWith('optimistic-')), false);

  const firstPrimary = recovery.getMessages().find((message) => message.id === 'session-1-turn-1-assistant');
  assert.ok(firstPrimary);
  assert.equal(firstPrimary.agentSteps?.[1]?.id, 'agent_step-4-thought');
  assert.equal(firstPrimary.agentSteps?.find((step) => step.id === 'observation-1')?.content, fullObservation);
  assert.equal(firstPrimary.managerDispatches?.['dispatch-1']?.[0]?.childRunId, 'child-1');
  assert.equal(firstPrimary.agentSteps?.find((step) => step.dispatch_id === 'dispatch-1')?.id, 'dispatch-1');

  const compact = recovery.getMessages().find((message) => message.id === 'compact-1');
  assert.equal(compact?.content, 'summary complete');
  assert.equal(compact?.compactStatus, 'completed');
  const coordinator = recovery.getMessages().find((message) => message.id === 'session-1-turn-1-coordinator-2');
  assert.equal(coordinator?.content, 'answer ');
  assert.equal(coordinator?.reasoning_content, 'plan');
  const execution = recovery.getMessages().find((message) => message.bubbleKind === 'execution');
  assert.equal(execution?.id, 'execution-1');
  assert.equal(execution?.content, 'executed answer');
  assert.equal(execution?.isStreaming, false);

  assert.equal(recovery.getMessages().find((message) => message.id === 'session-1-turn-2-user')?.content, 'second question');
  assert.equal(recovery.getMessages().find((message) => message.id === 'session-1-turn-2-assistant')?.content, 'second answer');
  assert.equal(recovery.getMessages().filter((message) => message.id.endsWith('-turn-2-user')).length, 1);
  assert.equal(recovery.getMessages().filter((message) => message.id.endsWith('-turn-2-assistant')).length, 1);
});

test('shared recovery handler relocks busy for Answer Now replacement after done or error', () => {
  const harness = createHarness();
  const busy: boolean[] = [];
  harness.environment.setAgentBusy = (value) => { busy.push(value); };
  const replayRunRef: TextAgentSessionRunRef = { current: null };
  const handlers = createTextAgentSessionHandlers(harness.environment, replayRunRef);

  handlers.onTimelineEvent({
    kind: 'turn_started', session_id: 'session-1', turn_index: 1,
    user_content: 'completed', images: [],
  });
  handlers.onDone('session-1');
  handlers.onSessionStart('session-1');
  handlers.onContent('replacement after done');
  handlers.onDone('session-1');
  handlers.onTimelineEvent({
    kind: 'turn_started', session_id: 'session-1', turn_index: 2,
    user_content: 'still running', images: [],
  });
  handlers.onPayloadError(new Error('old generation failed'));
  handlers.onSessionStart('session-1');
  handlers.onContent('replacement after error');
  handlers.onDone('session-1');

  assert.deepEqual(busy, [true, false, true, false, true, false, true, false]);
});

test('known Answer Now rejection finalizes the original bubble without detaching its session', () => {
  const harness = createHarness();
  const run = harness.addOptimisticTurn('original request', 1);
  run.answerNowWasClicked = true;
  const runRef: TextAgentSessionRunRef = { current: run };
  const handlers = createTextAgentSessionHandlers(harness.environment, runRef);
  const loading: boolean[] = [];
  const errors: string[] = [];

  const handled = settleKnownAnswerNowRejection({
    isCurrentTransport: true,
    run,
    handlers,
    rejection: new Error('Agent request rejected (409): Session busy'),
    clearAnswerNowFlag: () => { run.answerNowWasClicked = false; },
    setAnswerNowLoading: (value) => loading.push(value),
    onVisibleError: (message) => errors.push(message),
  });

  assert.equal(handled, true);
  const assistant = harness.getMessages().find((message) => message.id === run.assistantMessageId);
  assert.equal(assistant?.isStreaming, false);
  assert.equal(assistant?.content, '抱歉，Agent 执行出错: Agent request rejected (409): Session busy');
  assert.deepEqual(loading, [false]);
  assert.deepEqual(errors, ['Agent request rejected (409): Session busy']);
  assert.equal(harness.getSessionId(), undefined);
});

test('Text terminal error preserves Stop and Answer Now finalization behavior while releasing operation refs', () => {
  const stopped = createHarness();
  const stoppedRun = stopped.addOptimisticTurn('stop me', 1);
  stoppedRun.stopWasClicked = true;
  const stoppedRunRef: TextAgentSessionRunRef = { current: stoppedRun };
  createTextAgentSessionHandlers(stopped.environment, stoppedRunRef).onPayloadError(new Error('transport ended'));

  const stoppedAssistant = stopped.getMessages().find((message) => message.role === 'assistant');
  assert.equal(stoppedAssistant?.content, '');
  assert.equal(stoppedAssistant?.agentSteps?.[0]?.dispatch_status, 'aborted');
  assert.equal(stoppedAssistant?.isStreaming, false);
  assert.deepEqual(stopped.getTerminalCalls(), ['reset-streaming', 'clear-answer-now', 'clear-stop']);
  assert.equal(stoppedRunRef.current, null);
  assert.equal(stopped.activeRunRef.current, null);

  const answerNow = createHarness();
  const answerNowRun = answerNow.addOptimisticTurn('answer now', 1);
  answerNowRun.answerNowWasClicked = true;
  const answerNowRunRef: TextAgentSessionRunRef = { current: answerNowRun };
  createTextAgentSessionHandlers(answerNow.environment, answerNowRunRef).onPayloadError(new Error('rejected attempt aborted'));

  const answerNowAssistant = answerNow.getMessages().find((message) => message.role === 'assistant');
  assert.equal(answerNowAssistant?.content, '');
  assert.equal(answerNowAssistant?.agentSteps?.[0]?.dispatch_status, 'completed');
  assert.equal(answerNowAssistant?.isStreaming, false);
  assert.deepEqual(answerNow.getTerminalCalls(), ['clear-stop']);
  assert.equal(answerNowRunRef.current, null);
  assert.equal(answerNow.activeRunRef.current, null);
});

test('Text handler detaches a nonterminal subscriber without rendering a fatal transport error', () => {
  const harness = createHarness();
  const run = harness.addOptimisticTurn('recoverable request', 1);
  const runRef: TextAgentSessionRunRef = { current: run };
  const handlers = createTextAgentSessionHandlers(harness.environment, runRef);

  handlers.onContent('partial answer');
  handlers.onDetached();

  const assistant = harness.getMessages().find((message) => message.role === 'assistant');
  assert.equal(assistant?.content, 'partial answer');
  assert.equal(assistant?.isStreaming, false);
  assert.deepEqual(harness.getTerminalCalls(), ['reset-streaming', 'clear-stopped', 'clear-answer-now', 'clear-stop']);
  assert.equal(runRef.current, null);
  assert.equal(harness.activeRunRef.current, null);
});

test('Answer Now continuation projects into the original run-owned bubble exactly like from-zero Hub replay', () => {
  const applyReplacementGeneration = (handlers: ReturnType<typeof createTextAgentSessionHandlers>) => {
    handlers.onTimelineEvent({
      kind: 'turn_started', session_id: 'session-1', turn_index: 1,
      user_content: 'question', images: [], mode: 'tool_calling',
    });
    handlers.onContent('partial');
    handlers.onDone('session-1');
    handlers.onSessionStart('session-1');
    handlers.onContent(' final');
    handlers.onDone('session-1');
  };

  const liveRun: AgentRunState = {
    assistantMessageId: 'session-1-turn-1-assistant',
    projectedAssistantMessageIds: new Set<string>(),
    stopWasClicked: false,
    answerNowWasClicked: false,
  };
  const live = createHarness([
    { id: 'session-1-turn-1-user', role: 'user', content: 'question', images: [], timestamp: fixedNow },
    { id: 'session-1-turn-1-assistant', role: 'assistant', content: '', timestamp: fixedNow, isStreaming: true, agentSteps: [] },
  ]);
  live.activeRunRef.current = liveRun;
  applyReplacementGeneration(createTextAgentSessionHandlers(live.environment, { current: liveRun }));

  const recovery = createHarness();
  applyReplacementGeneration(createTextAgentSessionHandlers(recovery.environment, { current: null }));

  assert.deepEqual(live.getMessages(), recovery.getMessages());
  assert.equal(recovery.getMessages().find((message) => message.id === 'session-1-turn-1-assistant')?.content, 'partial final');
  assert.equal(recovery.getMessages().filter((message) => message.role === 'assistant').length, 1);
});

test('Answer Now error then accepted replacement reuses the original run bubble without a fatal suffix', () => {
  const applyReplacementAfterError = (handlers: ReturnType<typeof createTextAgentSessionHandlers>) => {
    handlers.onTimelineEvent({
      kind: 'turn_started', session_id: 'session-1', turn_index: 1,
      user_content: 'question', images: [], mode: 'tool_calling',
    });
    handlers.onContent('partial');
    handlers.onPayloadError(new Error('old generation failed'));
    handlers.onDone('session-1');
    handlers.onSessionStart('session-1');
    handlers.onContent(' replacement');
    handlers.onDone('session-1');
  };

  const liveRun: AgentRunState = {
    assistantMessageId: 'session-1-turn-1-assistant',
    projectedAssistantMessageIds: new Set<string>(),
    stopWasClicked: false,
    answerNowWasClicked: true,
  };
  const live = createHarness([
    { id: 'session-1-turn-1-user', role: 'user', content: 'question', images: [], timestamp: fixedNow },
    { id: 'session-1-turn-1-assistant', role: 'assistant', content: '', timestamp: fixedNow, isStreaming: true, agentSteps: [] },
  ]);
  live.activeRunRef.current = liveRun;
  const liveRunRef: TextAgentSessionRunRef = { current: liveRun };
  applyReplacementAfterError(createTextAgentSessionHandlers(live.environment, liveRunRef));

  const recovery = createHarness();
  const recoveryRunRef: TextAgentSessionRunRef = { current: null };
  applyReplacementAfterError(createTextAgentSessionHandlers(recovery.environment, recoveryRunRef));

  assert.deepEqual(live.getMessages(), recovery.getMessages());
  const assistant = recovery.getMessages().find((message) => message.id === 'session-1-turn-1-assistant');
  assert.equal(assistant?.content, 'partial replacement');
  assert.doesNotMatch(assistant?.content ?? '', /流式中断|Agent 执行出错/);
  assert.equal(recovery.getMessages().filter((message) => message.role === 'assistant').length, 1);
  assert.equal(liveRunRef.current, null);
  assert.equal(recoveryRunRef.current, null);
  assert.equal(live.activeRunRef.current, null);
  assert.equal(recovery.activeRunRef.current, null);
});

test('a normal next turn cancels an errored continuation instead of appending into it', () => {
  const harness = createHarness();
  const runRef: TextAgentSessionRunRef = { current: null };
  const handlers = createTextAgentSessionHandlers(harness.environment, runRef);

  handlers.onTimelineEvent({ kind: 'turn_started', session_id: 'session-1', turn_index: 1, user_content: 'first', images: [] });
  handlers.onContent('partial');
  handlers.onPayloadError(new Error('old generation failed'));
  handlers.onDone('session-1');
  handlers.onSessionStart('session-1');
  handlers.onTimelineEvent({ kind: 'turn_started', session_id: 'session-1', turn_index: 2, user_content: 'next', images: [] });
  handlers.onContent('new answer');

  assert.equal(harness.getMessages().find((message) => message.id === 'session-1-turn-1-assistant')?.content, 'partial\n\n[流式中断: old generation failed]');
  assert.equal(harness.getMessages().find((message) => message.id === 'session-1-turn-2-assistant')?.content, 'new answer');
});

test('Text page exposes session DELETE only through explicit Clear, never page or unmount lifecycle', async () => {
  const source = await readFile(path.join(testDir, '../app/page.tsx'), 'utf8');
  const actions = await readFile(new URL('./conversation/conversation-actions.ts', import.meta.url), 'utf8');
  const recovery = await readFile(new URL('./conversation/use-conversation-recovery.ts', import.meta.url), 'utf8');
  const clear = actions.slice(actions.indexOf('export function clearConversation'), actions.indexOf('export function confirmConversationSettings'));
  assert.match(source, /clearConversation\(/); assert.match(source, /useConversationRecovery\(/);
  assert.match(clear, /clearRetainedAgentSessionBeforeDelete\(/);
  assert.match(clear, /boundSessionId:\s*target,\s*bookmarkSessionId:\s*undefined/);
  assert.match(clear, /resolveSessionHubClearTarget\(/);
  assert.match(clear, /attachedSessionId:\s*state\.attachedRecoverySessionId \?\? state\.chatSessionId/);
  assert.match(clear, /deleteSession:\s*id\s*=>\s*llmClient\.deleteSessionHub\(id\)/);
  assert.equal(actions.match(/clearRetainedAgentSessionBeforeDelete\(/g)?.length, 1);
  assert.equal(actions.match(/llmClient\.deleteSessionHub\(/g)?.length, 1);
  assert.doesNotMatch(source, /llmClient\.deleteSessionHub\(|writeAgentSessionBookmark|writeChatTranscript|pagehide|beforeunload/);
  assert.match(recovery, /llmClient\.sessionHubEvents\(bookmark\.sessionId, 0/);
  assert.match(recovery, /bookmark\.interactionMode === 'agent'[\s\S]*dispatchAgentSessionPayload[\s\S]*chatHandlers\.onPayload/);
  assert.match(recovery, /onSettled: result => \{\s*if \(!ownedController \|\| s\.abortControllerRef\.current !== ownedController\) return;/);
  assert.match(recovery, /settledTerminal = result\.kind === 'terminal'/);
  assert.match(recovery, /if \(!settledTerminal\) s\.setAttachedRecoverySessionId\(null\)/);
  const coordinator = await readFile(new URL('./session-hub-recovery.ts', import.meta.url), 'utf8');
  assert.match(coordinator, /if \(!isCurrent\(\)\) return result;\s*options\.onSettled\?\.\(result\);[\s\S]*if \(!isCurrent\(\)\) return result;/);
  assert.match(source, /onKnownRejection:[\s\S]*rollback\(bookmarkTransaction\)/);
  assert.match(source, /onDetached:[\s\S]*retain\(bookmarkTransaction\)[\s\S]*liveChatAttachmentsRef\.current\.detach/);
  assert.match(source, /const answerNowHandlers = createTextAgentSessionHandlers/);
  assert.match(source, /answerNowStream\([\s\S]*answerNowHandlers,/);
  assert.doesNotMatch(source, /createAnswerNowStreamingMessage/);
});

test('Text Chat adapter keeps busy ownership across producer error until canonical done', async () => {
  const source = await readFile(path.join(testDir, '../app/page.tsx'), 'utf8');
  const start = source.indexOf('await runChatConversationTransport(');
  const complete = source.indexOf('onDone:', start), error = source.indexOf('onError:', complete), end = source.indexOf('lifecycle:', error);
  assert.ok(start >= 0 && error > complete && end > error);
  const doneBlock = source.slice(complete, error), errorBlock = source.slice(error, end);
  assert.match(doneBlock, /detach\(liveAttachment/); assert.match(doneBlock, /finalizeLiveChat\(true\)/);
  assert.match(errorBlock, /applyError\(liveAttachment/);
  assert.doesNotMatch(errorBlock, /detach\(liveAttachment|finalizeLiveChat|stopStreamingMessage|resetStreamingState|chatResume: undefined/);
});

test('Text and Vision Clear clear recovery error before aborting live transports', async () => {
  const actions = await readFile(new URL('./conversation/conversation-actions.ts', import.meta.url), 'utf8');
  const clear = actions.slice(actions.indexOf('export function clearConversation'), actions.indexOf('export function confirmConversationSettings'));
  assert.ok(clear.indexOf('state.setRecoveryError(null)') < clear.indexOf('state.abortControllerRef.current?.abort()'));
  assert.match(clear, /for\s*\(const registry of \[state\.liveAgentAttachmentsRef\.current,\s*state\.liveChatAttachmentsRef\.current\]\)/);
  assert.match(clear, /registry\.detach\(token/);
  for (const relative of ['../app/page.tsx', '../app/vision/page.tsx']) assert.match(await readFile(path.join(testDir, relative), 'utf8'), /clearConversation\(/);
});

test('Text Agent intentional detached path snapshots interruption before clearing recovery attachment', async () => {
  const source = await readFile(path.join(testDir, '../app/page.tsx'), 'utf8');
  const agent = source.indexOf('await runAgentConversationTransport(');
  const start = source.indexOf('onDetached: (error?) => {', agent), end = source.indexOf('\n          }', start);
  assert.ok(agent >= 0 && start > agent && end > start);
  const detached = source.slice(start, end);
  assert.match(detached, /captureAgentInterruptionIntent/);
  assert.match(detached, /if\s*\(intentionalInterruption\)[\s\S]*return/);
  assert.ok(detached.indexOf('captureAgentInterruptionIntent') < detached.indexOf('setAttachedRecoverySessionId(null)'));
  assert.match(detached, /if\s*\(intentionalInterruption\)[\s\S]*return[\s\S]*setAttachedRecoverySessionId\(null\)/);
});
