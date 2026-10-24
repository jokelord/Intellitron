import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { projectTimelineEvent } from './timeline-projection.ts';
import { getDispatchMarqueeSegments } from './dispatch-message.ts';
import {
  getManagerDispatchesAfterOrchestrationStep,
  mergeManagerChildAgentUpdate,
  mergeManagerChildAgentUpdateIntoMessages,
} from './message-types.ts';
import { getOrdinaryOrchestrationStages } from './timeline-projection.ts';
import { createTextAgentSessionHandlers } from './text-agent-session-handlers.ts';
import { createConversationTurnMessages } from './conversation/use-conversation-send.ts';
import { finalizeConversationStop } from './conversation/conversation-actions.ts';
import { resolveConversationDefaults } from './conversation/conversation-bootstrap.ts';
import { DEFAULT_SETTINGS, decideChatSettingsPersist, type Message } from './message-types.ts';

const testDir = path.dirname(fileURLToPath(import.meta.url));

function timelineHarness() {
  let nextId = 0, stopped: string[] = [];
  const turn = createConversationTurnMessages({userContent: 'question', initialRouting: true, createId: () => `message-${++nextId}`, createStepId: () => 'routing', now: () => new Date(0)});
  let messages: Message[] = [turn.userMessage, turn.assistantMessage];
  const activeRunRef = {current: turn.agentRun};
  const handlers = createTextAgentSessionHandlers({activeRunRef, setMessages: update => {messages = update(messages);},
    setAgentSessionId() {}, resetStreamingState() {}, clearStoppedState() {}, clearAnswerNowFlag() {}, clearStopFlag() {},
    getTerminalBehavior: () => ({suppressErrorMessage: false, resetStreamingState: true, resetStoppedState: true, clearAnswerNowFlag: false}),
    newlyCompactMarkedRef: {current: new Set<string>()}, defaultReasoningCollapsed: false, now: () => new Date(0), createId: prefix => `${prefix}-${++nextId}`,
  }, activeRunRef);
  const handoff = (id = 'handoff') => handlers.onStep({step_type: 'dispatch', dispatch_kind: 'secondary_handoff', dispatch_id: id, dispatch_status: 'completed', display_slot: 'after_body'});
  const coordinator = () => handlers.onTimelineEvent({kind: 'message_delta', source: 'coordinator_2', display: 'chat_bubble', message_id: 'coordinator', text: 'Plan {"type":"agent","name":"switch_agent"}', partial: false});
  return {handlers, run: turn.agentRun, activeRunRef, handoff, coordinator, messages: () => messages,
    stop() { finalizeConversationStop({activeAgentRunRef: activeRunRef, stopWasClickedRef: {current: false}, messagesRef: {current: messages},
      recoveryCoordinatorRef: {current: {detach: () => {handlers.onDetached(); return true;}}},
      setStoppedMessageId() {}, setStoppedTrackedMessageIds: (ids: string[]) => {stopped = ids;}, setMessages: (update: (old: Message[]) => Message[]) => {messages = update(messages);},
    } as any, 'agent'); }, stopped: () => stopped};
}

async function assertSharedAgentRoutes() {
  for (const relative of ['../app/page.tsx', '../app/vision/page.tsx']) {
    const source = await readFile(path.join(testDir, relative), 'utf8');
    assert.match(source, /runAgentConversationTransport\(/);
    assert.match(source, /useConversationRecovery\(/);
    assert.match(source, /<ConversationMessageList/);
  }
}

test('coordinator_2 message_delta with chat_bubble display becomes chat chunk', () => {
  const result = projectTimelineEvent({
    kind: 'message_delta',
    source: 'coordinator_2',
    message_id: 'assistant-msg-42',
    text: '我们先从边界条件开始',
    partial: true,
    display: 'chat_bubble',
  });

  assert.deepEqual(result, {
    kind: 'chat_chunk',
    messageId: 'assistant-msg-42',
    text: '我们先从边界条件开始',
    partial: true,
    reasoning: undefined,
  });
});

test('chat message_delta with chat_bubble display becomes chat chunk', () => {
  const result = projectTimelineEvent({
    kind: 'message_delta',
    source: 'chat',
    message_id: 'chat:session-1:2:assistant',
    text: 'answer',
    partial: true,
    reasoning: false,
    display: 'chat_bubble',
  });

  assert.deepEqual(result, {
    kind: 'chat_chunk',
    messageId: 'chat:session-1:2:assistant',
    text: 'answer',
    partial: true,
    reasoning: false,
  });
});

test('future message_delta sources are not projected into chat bubbles implicitly', () => {
  assert.deepEqual(projectTimelineEvent({
    kind: 'message_delta',
    source: 'future_agent',
    message_id: 'future-1',
    text: 'hidden',
    partial: true,
    display: 'chat_bubble',
  }), { kind: 'noop' });
});

test('routing_delta is archived without creating dispatch card projection', () => {
  const result = projectTimelineEvent({
    kind: 'routing_delta',
    source: 'coordinator_1',
    message_id: 'routing-card-1',
    text: '正在分析当前任务',
    partial: true,
  });

  assert.deepEqual(result, { kind: 'noop' });
});

test('dispatch_delta becomes generic dispatch update projection', () => {
  const result = projectTimelineEvent({
    kind: 'dispatch_delta',
    source: 'coordinator_2',
    display: 'dispatch_card',
    message_id: 'assistant-msg-42',
    dispatch_id: 'dispatch-run-42-secondary',
    dispatch_kind: 'secondary_handoff',
    display_slot: 'after_body',
    text: '正在转交执行代理',
    partial: true,
  });

  assert.deepEqual(result, {
    kind: 'dispatch_update',
    messageId: 'assistant-msg-42',
    dispatchId: 'dispatch-run-42-secondary',
    dispatchKind: 'secondary_handoff',
    displaySlot: 'after_body',
    text: '正在转交执行代理',
    partial: true,
  });
});

test('manager child update becomes dedicated manager dispatch projection', () => {
  const result = projectTimelineEvent({
    kind: 'manager_child_agent_update',
    source: 'manager_agent',
    dispatch_id: 'dispatch-1',
    call_id: 'call-child-1',
    child_run_id: 'child-1',
    child_name: 'news-reading-agent',
    status: 'running',
    assigned_task: 'Read source A and report findings.',
    href: '/agent/child/child-1',
  });

  assert.deepEqual(result, {
    kind: 'manager_child_agent_update',
    dispatchId: 'dispatch-1',
    callId: 'call-child-1',
    childRunId: 'child-1',
    childName: 'news-reading-agent',
    status: 'running',
    assignedTask: 'Read source A and report findings.',
    href: '/agent/child/child-1',
    finalPreview: undefined,
    error: undefined,
  });
});

test('text and vision pages render manager parallel dispatch card from projection', async () => {
  await assertSharedAgentRoutes();
  const component = await readFile(new URL('../components/conversation/ConversationMessageList.tsx', import.meta.url), 'utf8');
  const card = await readFile(new URL('../components/ManagerParallelDispatchCard.tsx', import.meta.url), 'utf8');
  assert.match(component, /<ManagerParallelDispatchCard/);
  assert.match(card, /target="_blank"/); assert.match(card, /rel="noopener noreferrer"/);
  const h = timelineHarness();
  h.handlers.onTimelineEvent({kind: 'manager_child_agent_update', dispatch_id: 'd', call_id: 'call', child_run_id: 'child', child_name: 'reader', status: 'running'});
  assert.equal(h.messages().find(message => message.id === h.run.assistantMessageId)?.managerDispatches?.d[0].childRunId, 'child');
});

test('manager child updates merge into one dispatch card by child_run_id', () => {
  const baseMessage = {
    id: 'assistant-1',
    role: 'assistant' as const,
    content: '',
    timestamp: new Date('2026-07-06T00:00:00Z'),
  };

  const withFirstChild = mergeManagerChildAgentUpdate(baseMessage, {
    kind: 'manager_child_agent_update',
    dispatchId: 'dispatch-1',
    callId: 'call-child-1',
    childRunId: 'child-1',
    childName: 'reader-a',
    status: 'running',
    assignedTask: 'Read source A and report findings.',
    href: '/agent/child/child-1',
  });
  const withSecondChild = mergeManagerChildAgentUpdate(withFirstChild, {
    kind: 'manager_child_agent_update',
    dispatchId: 'dispatch-1',
    callId: 'call-child-2',
    childRunId: 'child-2',
    childName: 'reader-b',
    status: 'pending',
    assignedTask: 'Read source B and report findings.',
    href: '/agent/child/child-2',
  });
  const withUpdatedFirstChild = mergeManagerChildAgentUpdate(withSecondChild, {
    kind: 'manager_child_agent_update',
    dispatchId: 'dispatch-1',
    callId: 'call-child-1',
    childRunId: 'child-1',
    childName: 'reader-a',
    status: 'completed',
    finalPreview: 'Source A report complete.',
  });

  assert.equal(withUpdatedFirstChild.managerDispatches?.['dispatch-1'].length, 2);
  assert.deepEqual(withUpdatedFirstChild.managerDispatches?.['dispatch-1'][0], {
    dispatchId: 'dispatch-1',
    callId: 'call-child-1',
    childRunId: 'child-1',
    childName: 'reader-a',
    status: 'completed',
    assignedTask: 'Read source A and report findings.',
    href: '/agent/child/child-1',
    finalPreview: 'Source A report complete.',
    error: undefined,
  });
  assert.equal(withUpdatedFirstChild.managerDispatches?.['dispatch-1'][1].childRunId, 'child-2');
  assert.equal(withUpdatedFirstChild.managerDispatches?.['dispatch-1'][1].callId, 'call-child-2');
});

test('partial manager child update preserves the established call ID', () => {
  const baseMessage = {
    id: 'assistant-1',
    role: 'assistant' as const,
    content: '',
    timestamp: new Date('2026-07-15T00:00:00Z'),
  };
  const initial = mergeManagerChildAgentUpdate(baseMessage, {
    kind: 'manager_child_agent_update',
    dispatchId: 'dispatch-1',
    callId: 'call-child-1',
    childRunId: 'child-1',
    childName: 'reader-a',
    status: 'running',
    href: '/agent/child/child-1',
  });
  const partial = mergeManagerChildAgentUpdate(initial, {
    kind: 'manager_child_agent_update',
    dispatchId: 'dispatch-1',
    callId: '',
    childRunId: 'child-1',
    childName: 'reader-a',
    status: 'completed',
  });

  assert.equal(partial.managerDispatches?.['dispatch-1'][0].callId, 'call-child-1');
  assert.equal(partial.managerDispatches?.['dispatch-1'][0].href, '/agent/child/child-1');
});

test('manager child page helper updates only the active assistant message', () => {
  const messages = [
    {
      id: 'assistant-older',
      role: 'assistant' as const,
      content: 'old',
      timestamp: new Date('2026-07-06T00:00:00Z'),
    },
    {
      id: 'assistant-active',
      role: 'assistant' as const,
      content: '',
      timestamp: new Date('2026-07-06T00:00:01Z'),
    },
  ];

  const next = mergeManagerChildAgentUpdateIntoMessages(messages, 'assistant-active', {
    kind: 'manager_child_agent_update',
    dispatchId: 'dispatch-1',
    callId: 'call-child-1',
    childRunId: 'child-1',
    childName: 'reader-a',
    status: 'running',
    assignedTask: 'Read source A and report findings.',
    href: '/agent/child/child-1',
  });

  assert.equal(next[0], messages[0]);
  assert.equal(next[1].managerDispatches?.['dispatch-1'][0].childRunId, 'child-1');
  assert.equal(next[1].content, '');
});

test('manager child timeout status remains in the manager chat bubble card data', () => {
  const baseMessage = {
    id: 'assistant-manager',
    role: 'assistant' as const,
    content: '',
    timestamp: new Date('2026-07-06T00:00:00Z'),
  };

  const next = mergeManagerChildAgentUpdate(baseMessage, {
    kind: 'manager_child_agent_update',
    dispatchId: 'dispatch-timeout',
    callId: 'call-timeout',
    childRunId: 'child-timeout',
    childName: 'slow-child',
    status: 'timeout',
    href: '/agent/child/child-timeout',
    error: 'Child agent timed out after 2400 seconds.',
  });

  assert.equal(next.managerDispatches?.['dispatch-timeout'][0].status, 'timeout');
  assert.equal(next.managerDispatches?.['dispatch-timeout'][0].href, '/agent/child/child-timeout');
});

test('manager dispatch join is derived from nested orchestration call IDs in either arrival order', () => {
  const planStep = {
    step_type: 'orchestration' as const,
    orchestration_type: 'plan' as const,
    stages: [{
      stage_id: 0,
      parallel: true,
      tools: [
        { call_id: 'normal-1', name: 'read_file', args: {}, status: 'completed' as const },
        { call_id: 'child-1-call', name: 'dispatching_parallel_agent', args: {}, status: 'running' as const },
        { call_id: 'child-2-call', name: 'dispatching_parallel_agent', args: {}, status: 'pending' as const },
      ],
    }],
  };
  const baseMessage = {
    id: 'assistant-1',
    role: 'assistant' as const,
    content: '',
    timestamp: new Date('2026-07-15T00:00:00Z'),
  };
  const childUpdates = [
    {
      kind: 'manager_child_agent_update' as const,
      dispatchId: 'dispatch-1',
      callId: 'child-1-call',
      childRunId: 'child-run-1',
      childName: 'child-a',
      status: 'running' as const,
      assignedTask: 'Handle child task A.',
      href: '/agent/child/child-run-1',
    },
    {
      kind: 'manager_child_agent_update' as const,
      dispatchId: 'dispatch-1',
      callId: 'child-2-call',
      childRunId: 'child-run-2',
      childName: 'child-b',
      status: 'pending' as const,
      assignedTask: 'Handle child task B.',
      href: '/agent/child/child-run-2',
    },
  ];

  const eventBeforePlanState = childUpdates.reduce(mergeManagerChildAgentUpdate, baseMessage);
  const planBeforeEventState = childUpdates.reduce(mergeManagerChildAgentUpdate, {
    ...baseMessage,
    agentSteps: [planStep],
  });
  for (const state of [eventBeforePlanState, planBeforeEventState]) {
    const joined = getManagerDispatchesAfterOrchestrationStep(planStep, state.managerDispatches);
    assert.equal(joined.length, 1);
    assert.equal(joined[0][0], 'dispatch-1');
    assert.deepEqual(joined[0][1].map((child) => child.callId), ['child-1-call', 'child-2-call']);
    assert.deepEqual(joined[0][1].map((child) => child.href), [
      '/agent/child/child-run-1',
      '/agent/child/child-run-2',
    ]);
  }
});

test('generic orchestration filters dispatch tools and removes dispatch-only stages', () => {
  const dispatch = { call_id: 'child-1', name: 'dispatching_parallel_agent', args: {}, status: 'running' as const };
  const normal = { call_id: 'read-1', name: 'read_file', args: {}, status: 'completed' as const };

  assert.deepEqual(getOrdinaryOrchestrationStages([
    { stage_id: 0, parallel: true, tools: [dispatch] },
  ]), []);
  assert.deepEqual(getOrdinaryOrchestrationStages([
    { stage_id: 0, parallel: true, tools: [normal, dispatch] },
  ]), [
    { stage_id: 0, parallel: true, tools: [normal] },
  ]);
});

test('text and vision pages anchor manager cards after matching orchestration plan call IDs', async () => {
  await assertSharedAgentRoutes();
  const shared = await readFile(new URL('../components/conversation/ConversationMessageList.tsx', import.meta.url), 'utf8');
  assert.match(shared, /getManagerDispatchesAfterOrchestrationStep\(step, message\.managerDispatches\)/);
  assert.match(shared, /renderAfterStep=\{renderManagerDispatchAfterStep\}/);
  assert.doesNotMatch(shared, /nextManagerDispatchIndex|managerDispatchByToolCallIndex/);
  const stepDisplay = await readFile(new URL('../components/AgentStepDisplay.tsx', import.meta.url), 'utf8');
  assert.match(stepDisplay, /renderAfterStep\?\.\(step, i\)/);
});

test('routing_delta does not duplicate dispatch_delta card updates', () => {
  const first = projectTimelineEvent({
    kind: 'routing_delta',
    source: 'coordinator_1',
    message_id: 'routing-card-1',
    text: '正在分析',
    partial: true,
  });
  const second = projectTimelineEvent({
    kind: 'dispatch_delta',
    source: 'coordinator_1',
    display: 'dispatch_card',
    message_id: 'routing-card-1',
    dispatch_id: 'dispatch-session-routing',
    dispatch_kind: 'routing',
    display_slot: 'before_body',
    text: '正在分析当前任务',
    partial: true,
  });

  assert.equal(first.kind, 'noop');
  assert.equal(second.kind, 'dispatch_update');
  assert.equal(second.text, '正在分析当前任务');
});

test('dispatch marquee duplicates text only while routing is still running', () => {
  assert.deepEqual(
    getDispatchMarqueeSegments('正在分析当前任务', true),
    ['正在分析当前任务', '正在分析当前任务'],
  );
  assert.deepEqual(
    getDispatchMarqueeSegments('已完成响应路径判定', false),
    ['已完成响应路径判定'],
  );
});

test('dispatch card uses live SSE text marquee from global styles', async () => {
  const [componentSource, globalCssSource] = await Promise.all([
    readFile(path.join(testDir, '../components/AgentStepDisplay.tsx'), 'utf8'),
    readFile(path.join(testDir, '../app/globals.css'), 'utf8'),
  ]);

  assert.match(componentSource, /className="dispatch-marquee-window"/);
  assert.match(componentSource, /dispatch-marquee-track/);
  assert.match(componentSource, /getDispatchMarqueeSegments\(liveText, isRunning\)/);
  assert.match(componentSource, /key=\{step\.id \|\| `\$\{step\.step_type\}-\$\{i\}`\}/);
  assert.match(componentSource, /<button\s+type="button"/);
  assert.match(componentSource, /aria-expanded=\{expanded\}/);
  assert.match(componentSource, /role="status"/);
  assert.match(componentSource, /aria-live=\{isRunning \? 'polite' : 'off'\}/);
  assert.doesNotMatch(componentSource, /animate-\[dispatch-scan/);
  assert.doesNotMatch(componentSource, /<style jsx>/);

  assert.match(globalCssSource, /@keyframes dispatch-marquee/);
  assert.match(globalCssSource, /\.dispatch-marquee-track/);
  assert.match(globalCssSource, /@keyframes dispatch-scan/);
  assert.match(globalCssSource, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(globalCssSource, /animation: none/);
});

test('text and vision pages keep stop state scoped to a stopped message and guard settings persistence', async () => {
  const h = timelineHarness(); h.coordinator(); h.handoff(); h.handlers.onContent('partial'); h.stop();
  assert.equal(h.run.stopWasClicked, true, 'Stop intent must be recorded before recovery detaches its handler');
  assert.equal(h.activeRunRef.current, null, 'detached recovery releases its active projection');
  assert.ok(h.stopped().includes(h.run.assistantMessageId));
  assert.ok(h.stopped().includes(h.run.executionAssistantMessageId!));
  assert.ok(h.messages().filter(message => h.stopped().includes(message.id)).every(message => !message.isStreaming));
  assert.equal(decideChatSettingsPersist(false, false).persist, false);
  assert.equal(decideChatSettingsPersist(true, false).persist, false);
  assert.equal(decideChatSettingsPersist(true, true).persist, true);
  await assertSharedAgentRoutes();
});

test('text and vision pages use the shared assistant reasoning fold UI without changing agent step cards', async () => {
  await assertSharedAgentRoutes();
  const shared = await readFile(new URL('../components/conversation/ConversationMessageList.tsx', import.meta.url), 'utf8');
  assert.match(shared, /<AssistantReasoningSection/);
  assert.match(shared, /defaultExpanded=\{isCycleMessage \|\| settings\.showReasoning\}/);
  assert.match(shared, /const defaultLeadingSteps = agentSteps\.filter/);
  assert.match(shared, /const defaultTrailingSteps = agentSteps\.filter/);
  assert.match(shared, /const orderedLeadingSteps = message\.bubbleKind === 'execution'/);
  assert.match(shared, /const orderedTrailingSteps = message\.bubbleKind === 'execution'/);
  assert.match(shared, /steps=\{orderedLeadingSteps\}/); assert.match(shared, /steps=\{orderedTrailingSteps\}/);
  assert.match(shared, /autoFoldOnTurnComplete=\{foldThinkingAfterFinalAnswer\}/);
  const component = await readFile(new URL('../components/AssistantReasoningSection.tsx', import.meta.url), 'utf8');
  assert.match(component, /aria-expanded=\{!collapsed\}/);
});

test('dispatch card source renders two-line body without title and uses smaller status dot', async () => {
  const componentSource = await readFile(path.join(testDir, '../components/AgentStepDisplay.tsx'), 'utf8');

  assert.doesNotMatch(componentSource, /Coordinator Transition/);
  assert.doesNotMatch(componentSource, /<div className="text-base font-semibold text-slate-50">任务调度<\/div>/);
  assert.match(componentSource, /inline-flex h-1\.5 w-1\.5 flex-shrink-0 rounded-full bg-amber-500/);
});

test('Vision Agent-to-Chat mode switch detaches local work without retiring its Agent session', async () => {
  const source = await readFile(new URL('./conversation/conversation-actions.ts', import.meta.url), 'utf8');
  const mode = source.slice(source.indexOf('export function retireConversationMode'), source.indexOf('export function finalizeConversationStop'));
  assert.doesNotMatch(mode, /retireBoundAgentSession|deleteAgentSession|stopAgent\(/);
  assert.match(mode, /transport\?\.abort/); assert.match(mode, /input\.invalidate\?\.\(\)/);
  const vision = await readFile(new URL('../app/vision/page.tsx', import.meta.url), 'utf8');
  assert.match(vision, /confirmConversationSettings\(/); assert.match(vision, /invalidateMode:\s*abortAutomationAndInvalidate/);
});

test('tool call cards show hourglass spinner only for slow search tools while running', async () => {
  const [componentSource, globalCssSource] = await Promise.all([
    readFile(path.join(testDir, '../components/AgentStepDisplay.tsx'), 'utf8'),
    readFile(path.join(testDir, '../app/globals.css'), 'utf8'),
  ]);

  assert.match(componentSource, /const WAITING_TOOL_NAMES = new Set\(\[/);
  assert.match(componentSource, /'search_and_fetch'/);
  assert.match(componentSource, /'wikipedia_search_and_fetch'/);
  assert.match(componentSource, /WAITING_TOOL_NAMES\.has\(step\.name\)/);
  assert.match(componentSource, /isLatestStep=\{i === steps\.length - 1\}/);
  assert.match(componentSource, /tool-wait-hourglass/);
  assert.match(componentSource, /⏳/);
  assert.match(globalCssSource, /@keyframes tool-wait-hourglass-spin/);
  assert.match(globalCssSource, /animation: tool-wait-hourglass-spin 1s steps\(1, end\) infinite/);
});

test('thought card renders default-collapsed exact raw tool-call disclosure only when present', async () => {
  const componentSource = await readFile(path.join(testDir, '../components/AgentStepDisplay.tsx'), 'utf8');

  assert.match(componentSource, /step\.step_type === 'thought' && step\.raw_tool_call/);
  assert.match(componentSource, /useState\(false\)/);
  assert.match(componentSource, /查看原始工具调用/);
  assert.match(componentSource, /aria-expanded=\{rawToolCallExpanded\}/);
  assert.match(componentSource, /\{step\.raw_tool_call\}/);
  assert.match(componentSource, /whitespace-pre-wrap/);
});

test('text and vision pages ignore legacy running routing dispatch steps to avoid duplicate routing cards', async () => {
  const h = timelineHarness();
  const before = h.messages().find(message => message.id === h.run.assistantMessageId)?.agentSteps?.length;
  h.handlers.onStep({step_type: 'dispatch', dispatch_kind: 'routing', dispatch_status: 'running'});
  assert.equal(h.messages().find(message => message.id === h.run.assistantMessageId)?.agentSteps?.length, before);
  h.handoff(); assert.ok(h.run.executionAssistantMessageId);
  await assertSharedAgentRoutes();
});

test('text and vision pages create initial routing dispatch only for coordinator_1 entry', async () => {
  for (const initialRouting of [false, true]) {
    const turn = createConversationTurnMessages({userContent: 'x', initialRouting});
    assert.equal(turn.assistantMessage.agentSteps?.length ?? 0, initialRouting ? 1 : 0);
    if (initialRouting) assert.deepEqual([turn.assistantMessage.agentSteps![0].dispatch_stage, turn.assistantMessage.agentSteps![0].dispatch_kind, turn.assistantMessage.agentSteps![0].display_slot], ['routing', 'routing', 'before_body']);
  }
  for (const relative of ['../app/page.tsx', '../app/vision/page.tsx']) {
    const route = await readFile(path.join(testDir, relative), 'utf8');
    assert.match(route, /settings\.agentMode && agentEntryAgentName === 'coordinator_1'/);
    assert.match(route, /initialRouting:\s*shouldShowInitialRoutingDispatch/);
  }
});

test('text and vision pages treat tracked projected bubbles as stopped', async () => {
  const h = timelineHarness(); h.coordinator(); h.handoff(); h.handlers.onContent('partial'); h.stop();
  const stopped = new Set(h.stopped());
  assert.ok(stopped.has(h.run.assistantMessageId)); assert.ok(stopped.has(h.run.executionAssistantMessageId!));
  assert.ok(h.messages().filter(message => stopped.has(message.id)).every(message => message.isStreaming === false));
  const presentation = await readFile(new URL('../components/conversation/ConversationMessageList.tsx', import.meta.url), 'utf8');
  assert.match(presentation, /stoppedMessageIds\.has\(message\.id\)/);
  assert.match(presentation, /'已停止' : '思考中\.\.\.'/);
  for (const relative of ['../app/page.tsx', '../app/vision/page.tsx']) assert.match(await readFile(path.join(testDir, relative), 'utf8'), /stoppedMessageIds=\{trackedStoppedMessageIds\}/);
});

test('text and vision pages preserve manual agent mode on refresh and clear queued prompts on handleClear', async () => {
  const previous = {...DEFAULT_SETTINGS, agentMode: true, enableThinking: false};
  const refreshed = resolveConversationDefaults({...DEFAULT_SETTINGS, agentMode: false, enableThinking: true}, previous, true, null, null);
  assert.equal(refreshed.agentMode, true); assert.equal(refreshed.enableThinking, false);
  for (const relative of ['../app/page.tsx', '../app/vision/page.tsx']) {
    const route = await readFile(path.join(testDir, relative), 'utf8');
    assert.match(route, /useConversationBootstrap\(/); assert.match(route, /clearConversation\([\s\S]*setMessageQueue\(\[\]\)/);
  }
});

test('Agent tool admission guards cover pending lock, Vision exact rejection, and paired Compact IDs', async () => {
  const panel = await readFile(new URL('../components/AgentAdvancedSettingsPanel.tsx', import.meta.url), 'utf8');
  assert.match(panel, /sessionLockState\.kind === 'pending'/); assert.match(panel, /工具列表与服务器默认值暂时锁定/);
  const commands = await readFile(new URL('./conversation/use-conversation-commands.ts', import.meta.url), 'utf8');
  assert.match(commands, /sessionId: settings\.agentMode \? agentToolState\.pairedSessionId : chatSessionId/);
  const admission = await readFile(new URL('./conversation/conversation-tool-admission.ts', import.meta.url), 'utf8');
  assert.match(admission, /dispatchAgentToolState\(\{\s*type: 'reject',\s*sessionId,\s*requestId\s*\}\)/);
  assert.match(admission, /formatAgentToolConfigurationRejection\(code, sessionId\)/);
  for (const relative of ['../app/page.tsx', '../app/vision/page.tsx']) assert.match(await readFile(path.join(testDir, relative), 'utf8'), /rejectConversationToolAdmission\(\{/);
});

test('text and vision pages keep cached web settings over yaml defaults and wire fold-thinking-after-final-answer', async () => {
  const initial = resolveConversationDefaults(DEFAULT_SETTINGS, DEFAULT_SETTINGS, false, {temperature: 0, showReasoning: false, systemPrompt: ''}, null);
  assert.equal(initial.temperature, 0); assert.equal(initial.showReasoning, false); assert.equal(initial.systemPrompt, '');
  const presentation = await readFile(new URL('../components/conversation/ConversationMessageList.tsx', import.meta.url), 'utf8');
  assert.match(presentation, /turnComplete=\{!message\.isStreaming\}/);
  assert.match(presentation, /autoFoldOnTurnComplete=\{foldThinkingAfterFinalAnswer\}/);
  assert.match(presentation, /defaultCollapsed=\{isCycleMessage \? false : settings\.agentMode && !settings\.showReasoning\}/);
  const settings = await readFile(new URL('../components/conversation/ConversationSettingsDialog.tsx', import.meta.url), 'utf8');
  assert.match(settings, /checked=\{pendingSettings\.foldThinkingAfterFinalAnswer \?\? true\}/);
  for (const relative of ['../app/page.tsx', '../app/vision/page.tsx']) assert.match(await readFile(path.join(testDir, relative), 'utf8'), /manuallyToggledReasoningIdsRef\.current\.add\(id\)/);
});

test('text and vision pages default agent mode on when no cached settings exist', async () => {
  const bootstrap = await readFile(new URL('./conversation/use-conversation-bootstrap.ts', import.meta.url), 'utf8');
  assert.match(bootstrap, /getSavedChatSettings\(saved \? DEFAULT_SETTINGS : \{\s*\.\.\.DEFAULT_SETTINGS,\s*agentMode: true\s*\}\)/);
  for (const relative of ['../app/page.tsx', '../app/vision/page.tsx']) assert.match(await readFile(path.join(testDir, relative), 'utf8'), /useConversationBootstrap\(/);
});

test('compact diagnostics metadata does not render as chat text', () => {
  const started = projectTimelineEvent({
    kind: 'compact_started',
    summary_message_id: 'compact-1',
    metadata: { prompt_shape: 'cache_first_tail', cached_tokens: 900 },
  });

  assert.equal(started.kind, 'compact_started');
  assert.equal('text' in started, false);
});

test('text and vision pages route agent callbacks through the active tracked assistant helper', async () => {
  const h = timelineHarness(); h.coordinator(); h.handoff();
  const execution = h.run.executionAssistantMessageId!;
  h.handlers.onContent('active content'); h.handlers.onStep({step_type: 'thought', content: 'active thought'});
  const active = h.messages().find(message => message.id === execution)!;
  assert.equal(active.content, 'active content'); assert.equal(active.agentSteps?.at(-1)?.content, 'active thought');
  assert.notEqual(h.messages().find(message => message.id === h.run.assistantMessageId)?.content, 'active content');
  await assertSharedAgentRoutes();
});

test('text and vision pages keep legacy content off coordinator_2 projected bubbles', async () => {
  const h = timelineHarness(); h.coordinator();
  h.handlers.onContent('legacy content');
  assert.ok(!h.messages().some(message => String(message.content).includes('legacy content')));
  await assertSharedAgentRoutes();
});

test('text and vision pages keep an explicit execution bubble for secondary handoff', async () => {
  const h = timelineHarness(); h.coordinator(); h.handoff();
  assert.ok(h.run.coordinatorMessageId); assert.ok(h.run.executionAssistantMessageId);
  assert.notEqual(h.run.coordinatorMessageId, h.run.executionAssistantMessageId);
  assert.equal(h.messages().find(message => message.id === h.run.executionAssistantMessageId)?.bubbleKind, 'execution');
  await assertSharedAgentRoutes();
});

test('text and vision pages retract coordinator_2 tail text when the secondary handoff dispatch step arrives', async () => {
  const h = timelineHarness(); h.coordinator(); h.handoff();
  assert.ok(!String(h.messages().find(message => message.id === h.run.coordinatorMessageId)?.content).includes('switch_agent'));
  assert.equal(h.run.secondaryHandoffExecutionBubblePendingAgentSwitch, true);
  await assertSharedAgentRoutes();
});

test('text and vision pages create execution bubble before routing secondary handoff dispatch updates', async () => {
  const h = timelineHarness(); h.coordinator(); h.handoff();
  const id = h.run.executionAssistantMessageId;
  assert.ok(h.messages().some(message => message.id === id && message.bubbleKind === 'execution'));
  h.handlers.onTimelineEvent({kind: 'agent_switched', source: 'coordinator_2', target: 'basic_agent'});
  assert.equal(h.run.executionAssistantMessageId, id);
  await assertSharedAgentRoutes();
});

test('Bug 3 regression: text and vision pages open a fresh execution bubble for EVERY secondary handoff, not just the first', async () => {
  const h = timelineHarness(); h.coordinator(); h.handoff('first');
  const first = h.run.executionAssistantMessageId!;
  h.handlers.onTimelineEvent({kind: 'agent_switched', source: 'coordinator_2', target: 'basic_agent'});
  h.handlers.onContent('first execution'); h.handoff('second');
  const second = h.run.executionAssistantMessageId!;
  assert.notEqual(second, first); assert.equal(h.messages().find(message => message.id === first)?.isStreaming, false);
  h.handlers.onContent('second execution');
  assert.equal(h.messages().find(message => message.id === second)?.content, 'second execution');
  assert.equal(h.messages().find(message => message.id === first)?.content, 'first execution');
});

test('Bug 3 regression: agent_switched from coordinator_2 consumes the dispatch-created bubble instead of opening a duplicate one', async () => {
  const h = timelineHarness(); h.coordinator(); h.handoff();
  const before = h.messages().length, execution = h.run.executionAssistantMessageId;
  assert.equal(h.run.secondaryHandoffExecutionBubblePendingAgentSwitch, true);
  h.handlers.onTimelineEvent({kind: 'agent_switched', source: 'coordinator_2', target: 'basic_agent'});
  assert.equal(h.messages().length, before); assert.equal(h.run.executionAssistantMessageId, execution);
  assert.equal(h.run.secondaryHandoffExecutionBubblePendingAgentSwitch, false);
  // A switch without the matching dispatch retains the established fallback.
  h.handlers.onTimelineEvent({kind: 'agent_switched', source: 'coordinator_2', target: 'basic_agent'});
  assert.notEqual(h.run.executionAssistantMessageId, execution);
});

test('text and vision pages apply dispatch updates to the active tracked bubble instead of the initial placeholder', async () => {
  const h = timelineHarness(); h.coordinator(); h.handoff();
  const execution = h.run.executionAssistantMessageId!;
  h.handlers.onTimelineEvent({kind: 'dispatch_delta', display: 'dispatch_card', dispatch_id: 'next-dispatch', dispatch_kind: 'routing', display_slot: 'before_body', text: 'next route', partial: true});
  assert.ok(h.messages().find(message => message.id === execution)?.agentSteps?.some(step => step.dispatch_message === 'next route'));
  assert.ok(!h.messages().find(message => message.id === h.run.assistantMessageId)?.agentSteps?.some(step => step.dispatch_message === 'next route'));
});

test('text and vision pages flush the initial assistant placeholder before agent streaming starts', async () => {
  const [textPageSource, visionPageSource] = await Promise.all([
    readFile(path.join(testDir, '../app/page.tsx'), 'utf8'),
    readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8'),
  ]);

  assert.match(textPageSource, /import \{ flushSync \} from 'react-dom';/);
  assert.match(visionPageSource, /import \{ flushSync \} from 'react-dom';/);
  assert.match(textPageSource, /flushSync\(\(\) => \{[\s\S]*setMessages\(\(prev: Message\[\]\) => \{/);
  assert.match(visionPageSource, /flushSync\(\(\) => \{[\s\S]*setMessages\(\(prev: Message\[\]\) => \{/);
});

test('text and vision pages keep page scrolling locked to the message pane', async () => {
  const [textPageSource, visionPageSource, layoutSource, globalCssSource] = await Promise.all([
    readFile(path.join(testDir, '../app/page.tsx'), 'utf8'),
    readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8'),
    readFile(path.join(testDir, '../app/layout.tsx'), 'utf8'),
    readFile(path.join(testDir, '../app/globals.css'), 'utf8'),
  ]);

  assert.match(layoutSource, /<html lang="zh-CN" className="h-full overflow-hidden">/);
  assert.match(layoutSource, /<body className="h-full overflow-hidden bg-white dark:bg-gray-900">/);
  assert.match(globalCssSource, /html,\s*body\s*\{[\s\S]*height:\s*100%;[\s\S]*overflow:\s*hidden;/);
  assert.match(globalCssSource, /body\s*\{[\s\S]*overscroll-behavior-y:\s*none;/);
  assert.match(globalCssSource, /\.app-shell\s*\{[\s\S]*height:\s*100vh;[\s\S]*min-height:\s*100vh;/);
  assert.match(textPageSource, /className=\{`app-shell flex flex-col overflow-y-auto bg-gray-50 dark:bg-gray-900 \$\{appSkinClass\}`\}/);
  assert.match(visionPageSource, /className=\{`app-shell flex flex-col overflow-y-auto bg-gray-50 dark:bg-gray-900 \$\{appSkinClass\}`\}/);
  // Persistent native scroll ownership is exercised by use-chat-scroll-follow.test.mts.
  assert.doesNotMatch(textPageSource, /scrollIntoView\(/);
  assert.doesNotMatch(visionPageSource, /scrollIntoView\(/);
  assert.match(
    textPageSource,
    /<div className=\{`app-shell flex flex-col overflow-y-auto bg-gray-50 dark:bg-gray-900 \$\{appSkinClass\}`\} style=\{buildWebUIStyleVars\(webUIConfig\)\} ref=\{messageScrollContainerRef\}>/,
  );
  assert.match(
    visionPageSource,
    /<div className=\{`app-shell flex flex-col overflow-y-auto bg-gray-50 dark:bg-gray-900 \$\{appSkinClass\}`\} style=\{buildWebUIStyleVars\(webUIConfig\)\} ref=\{messageScrollContainerRef\}>/,
  );
  const list = await readFile(new URL('../components/conversation/ConversationMessageList.tsx', import.meta.url), 'utf8');
  assert.match(list, /<main[^>]*className="flex-1 px-4 py-6">/);
  assert.match(textPageSource, /contentRef=\{messageContentRef\}/);
  assert.match(visionPageSource, /contentRef=\{messageContentRef\}/);
});


test('compact_started projects to compact_started kind', () => {
  const out = projectTimelineEvent({
    kind: 'compact_started',
    summary_message_id: 'compact-abc',
    trigger: 'auto',
    mode: 'agent',
    original_tokens: 51234,
    threshold: 50000,
    strategy: 'performance',
  });
  assert.equal(out.kind, 'compact_started');
  if (out.kind === 'compact_started') {
    assert.equal(out.summaryMessageId, 'compact-abc');
    assert.equal(out.trigger, 'auto');
    assert.equal(out.mode, 'agent');
    assert.equal(out.originalTokens, 51234);
    assert.equal(out.threshold, 50000);
    assert.equal(out.strategy, 'performance');
  }
});

test('compact_delta projects text', () => {
  const out = projectTimelineEvent({
    kind: 'compact_delta',
    summary_message_id: 'compact-abc',
    text: 'partial...',
    partial: true,
  });
  assert.equal(out.kind, 'compact_delta');
  if (out.kind === 'compact_delta') {
    assert.equal(out.summaryMessageId, 'compact-abc');
    assert.equal(out.text, 'partial...');
    assert.equal(out.partial, true);
  }
});

test('compact_completed projects cleaned text', () => {
  const out = projectTimelineEvent({
    kind: 'compact_completed',
    summary_message_id: 'compact-abc',
    text: 'Summary:\ncleaned',
    partial: false,
    metadata: { summary_tokens: 100 },
  });
  assert.equal(out.kind, 'compact_completed');
  if (out.kind === 'compact_completed') {
    assert.equal(out.summaryMessageId, 'compact-abc');
    assert.equal(out.text, 'Summary:\ncleaned');
    assert.equal(out.metadata?.summary_tokens, 100);
  }
});

test('compact_failed projects reason from metadata', () => {
  const out = projectTimelineEvent({
    kind: 'compact_failed',
    summary_message_id: 'compact-abc',
    metadata: { reason: 'llm_unreachable' },
  });
  assert.equal(out.kind, 'compact_failed');
  if (out.kind === 'compact_failed') {
    assert.equal(out.summaryMessageId, 'compact-abc');
    assert.equal(out.reason, 'llm_unreachable');
  }
});

test('compact_failed falls back to unknown reason when missing', () => {
  const out = projectTimelineEvent({
    kind: 'compact_failed',
    summary_message_id: 'compact-abc',
  });
  if (out.kind === 'compact_failed') {
    assert.equal(out.reason, 'unknown');
  }
});

test('compact_started defaults trigger to manual and mode to chat', () => {
  const out = projectTimelineEvent({
    kind: 'compact_started',
    summary_message_id: 'compact-x',
  });
  if (out.kind === 'compact_started') {
    assert.equal(out.trigger, 'manual');
    assert.equal(out.mode, 'chat');
  }
});

test('shared disclosure distinguishes model XML from API payload with legacy label fallback', async () => {
  const componentSource = await readFile(path.join(testDir, '../components/AgentStepDisplay.tsx'), 'utf8');
  assert.match(componentSource, /step\.raw_tool_call_source === 'provider_payload'\s*\? '查看 API 工具调用'\s*:\s*'查看原始工具调用'/);
});
