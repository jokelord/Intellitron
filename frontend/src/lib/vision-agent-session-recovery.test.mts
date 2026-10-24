import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { dispatchAgentSessionPayload, type AgentSessionPayloadHandlers } from './api.ts';
import { getStreamingTerminalBehavior } from './answer-now-ui.ts';
import { buildAgentContextMessages } from './message-types.ts';
import {
  createTextAgentSessionHandlers,
  type TextAgentSessionHandlerEnvironment,
  type TextAgentSessionRunRef,
} from './text-agent-session-handlers.ts';
import type { AgentRunState, Message } from './message-types.ts';

const testDir = path.dirname(fileURLToPath(import.meta.url));
const fullObservation = `${'观'.repeat(200_000)}尾`;

function harness(initial: Message[] = [], run: AgentRunState | null = null) {
  let messages = initial;
  const activeRunRef: TextAgentSessionRunRef = { current: run };
  const environment: TextAgentSessionHandlerEnvironment = {
    activeRunRef,
    setMessages: (update) => { messages = update(messages); },
    setAgentSessionId: () => undefined,
    resetStreamingState: () => undefined,
    clearStoppedState: () => undefined,
    clearAnswerNowFlag: () => undefined,
    clearStopFlag: () => undefined,
    getTerminalBehavior: (run) => getStreamingTerminalBehavior({
      stopWasClicked: run.stopWasClicked,
      answerNowWasClicked: run.answerNowWasClicked,
    }),
    newlyCompactMarkedRef: { current: new Set<string>() },
    defaultReasoningCollapsed: false,
    now: () => new Date(0),
    createId: (prefix) => `${prefix}-vision`,
  };
  const runRef: TextAgentSessionRunRef = { current: run };
  return { handlers: createTextAgentSessionHandlers(environment, runRef), getMessages: () => messages };
}

async function feed(handlers: AgentSessionPayloadHandlers, payloads: Array<Record<string, unknown>>) {
  for (const payload of payloads) await dispatchAgentSessionPayload(payload, handlers);
}

test('Vision live and from-zero replay use the shared handler with sequential full-content turns', async () => {
  const payloads = [
    { event_sequence: 1, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-s', turn_index: 1, user_content: 'first', images: [], mode: 'tool_calling' } },
    { event_sequence: 2, type: 'agent_step', step_type: 'thought', content: 'think', partial: false },
    { event_sequence: 3, type: 'agent_step', step_id: 'observation-1', step_type: 'observation', content: fullObservation },
    { event_sequence: 4, type: 'timeline_event', event: { kind: 'dispatch_delta', display: 'dispatch_card', dispatch_id: 'dispatch-1', dispatch_kind: 'routing', text: 'route', partial: false } },
    { event_sequence: 5, type: 'timeline_event', event: { kind: 'manager_child_agent_update', dispatch_id: 'dispatch-1', call_id: 'call-1', child_run_id: 'child-1', child_name: 'reader', status: 'completed' } },
    { event_sequence: 6, type: 'timeline_event', event: { kind: 'message_delta', source: 'coordinator_2', display: 'chat_bubble', message_id: 'vision-s-turn-1-coordinator-2', text: 'coordinator', partial: false } },
    { event_sequence: 7, type: 'timeline_event', event: { kind: 'compact_started', summary_message_id: 'compact-1' } },
    { event_sequence: 8, type: 'timeline_event', event: { kind: 'compact_completed', summary_message_id: 'compact-1', text: 'compact' } },
    { event_sequence: 9, type: 'done', session_id: 'vision-s' },
    { event_sequence: 10, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-s', turn_index: 2, user_content: 'second', images: [], mode: 'tool_calling' } },
    { event_sequence: 11, type: 'content', content: 'second answer' },
    { event_sequence: 12, type: 'done', session_id: 'vision-s' },
  ];
  const optimisticAssistant: Message = { id: 'optimistic-assistant', role: 'assistant', content: '', timestamp: new Date(0), isStreaming: true, agentSteps: [] };
  const liveRun: AgentRunState = { assistantMessageId: optimisticAssistant.id, projectedAssistantMessageIds: new Set(), stopWasClicked: false, answerNowWasClicked: false };
  const live = harness([{ id: 'optimistic-user', role: 'user', content: 'first', timestamp: new Date(0) }, optimisticAssistant], liveRun);
  const replay = harness();
  await feed(live.handlers, payloads);
  await feed(replay.handlers, payloads);
  assert.deepEqual(replay.getMessages(), live.getMessages());
  const first = replay.getMessages().find((message) => message.id === 'vision-s-turn-1-assistant');
  assert.equal(first?.agentSteps?.[0]?.id, 'agent_step-2-thought');
  assert.equal(first?.agentSteps?.find((step) => step.id === 'observation-1')?.content, fullObservation);
  assert.equal(first?.managerDispatches?.['dispatch-1']?.[0]?.childRunId, 'child-1');
  assert.equal(replay.getMessages().find((message) => message.id === 'vision-s-turn-2-assistant')?.content, 'second answer');
});

test('Vision recovery preserves raw textual tool-call debug on the thought card', async () => {
  const raw = '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{\n  "file_path": "/tmp/a"\n}}\n<<END_TOOL_CALL>>';
  const projection = harness();

  await feed(projection.handlers, [
    { event_sequence: 1, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-raw', turn_index: 1, user_content: 'inspect', images: [], mode: 'tool_calling' } },
    { event_sequence: 2, type: 'agent_step', step_type: 'thought', content: 'reasoning', partial: true },
    { event_sequence: 3, type: 'agent_step', step_type: 'thought', raw_tool_call: raw, partial: false },
  ]);

  const thought = projection.getMessages().find((message) => message.role === 'assistant')?.agentSteps?.[0];
  assert.equal(thought?.content, 'reasoning');
  assert.equal(thought?.raw_tool_call, raw);
});

test('Vision Hub replay keeps only the latest real images after an accepted automation turn makes omission sticky', async () => {
  const automationText = [
    '<vision_automation_manifest>',
    'Manual images: 1, image indexes 1-1',
    'Source 1: Desktop, image indexes 2-2',
    'Relative times: 0.00s',
    '</vision_automation_manifest>',
    '',
    'automation turn',
  ].join('\n');
  const payloads = [
    { event_sequence: 1, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-sticky', turn_index: 1, user_content: 'ordinary first', images: ['data:image/jpeg;base64,first'], mode: 'tool_calling', historical_image_omission: false } },
    { event_sequence: 2, type: 'done', session_id: 'vision-sticky' },
    { event_sequence: 3, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-sticky', turn_index: 2, user_content: automationText, images: ['data:image/jpeg;base64,manual', 'data:image/jpeg;base64,desktop'], mode: 'tool_calling', historical_image_omission: true, vision_automation_manual_image_count: 1 } },
    { event_sequence: 4, type: 'done', session_id: 'vision-sticky' },
    { event_sequence: 5, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-sticky', turn_index: 3, user_content: 'ordinary latest', images: ['data:image/jpeg;base64,latest'], mode: 'tool_calling', historical_image_omission: true } },
    { event_sequence: 6, type: 'done', session_id: 'vision-sticky' },
  ];
  const replay = harness();

  await feed(replay.handlers, payloads.slice(0, 4));

  const acceptedAutomationTurn = replay.getMessages().find(
    (message) => message.id === 'vision-sticky-turn-2-user',
  );
  assert.deepEqual(acceptedAutomationTurn?.images, [
    'data:image/jpeg;base64,manual',
    'data:image/jpeg;base64,desktop',
  ]);

  await feed(replay.handlers, payloads.slice(4));

  const messages = replay.getMessages();
  const first = messages.find((message) => message.id === 'vision-sticky-turn-1-user');
  const automation = messages.find((message) => message.id === 'vision-sticky-turn-2-user');
  const latest = messages.find((message) => message.id === 'vision-sticky-turn-3-user');
  assert.match(JSON.stringify(first?.content), /historical images omitted: count=1/);
  assert.match(JSON.stringify(automation?.content), /sources=manual,desktop/);
  assert.equal(automation?.images, undefined);
  assert.match(JSON.stringify(latest?.content), /data:image\/jpeg;base64,latest/);
  assert.equal(latest?.images?.[0], 'data:image/jpeg;base64,latest');
  const context = buildAgentContextMessages({
    messages,
    maxContextLength: 100_000,
    currentUserContent: 'next ordinary prompt',
    preserveFullHistory: true,
  });
  assert.doesNotMatch(JSON.stringify(context), /base64,first|base64,manual|base64,desktop/);
  assert.match(JSON.stringify(context), /base64,latest/);

  await feed(replay.handlers, [
    { event_sequence: 7, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-sticky', turn_index: 4, user_content: 'ordinary text-only latest', images: [], mode: 'tool_calling', historical_image_omission: true } },
    { event_sequence: 8, type: 'done', session_id: 'vision-sticky' },
  ]);
  const textOnlyContext = buildAgentContextMessages({
    messages: replay.getMessages(),
    maxContextLength: 100_000,
    currentUserContent: 'next ordinary prompt',
    preserveFullHistory: true,
  });
  assert.doesNotMatch(JSON.stringify(textOnlyContext), /data:image\/jpeg;base64/);
});

test('ordinary Vision Hub turns never omit historical images without the accepted sticky marker', async () => {
  const replay = harness();
  await feed(replay.handlers, [
    { event_sequence: 1, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-ordinary', turn_index: 1, user_content: 'first', images: ['data:image/jpeg;base64,first'], mode: 'tool_calling', historical_image_omission: false } },
    { event_sequence: 2, type: 'done', session_id: 'vision-ordinary' },
    { event_sequence: 3, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-ordinary', turn_index: 2, user_content: 'second', images: ['data:image/jpeg;base64,second'], mode: 'tool_calling', historical_image_omission: false } },
    { event_sequence: 4, type: 'done', session_id: 'vision-ordinary' },
  ]);

  const context = buildAgentContextMessages({
    messages: replay.getMessages(),
    maxContextLength: 100_000,
    currentUserContent: 'third',
    preserveFullHistory: true,
  });
  assert.match(JSON.stringify(context), /base64,first/);
  assert.match(JSON.stringify(context), /base64,second/);
});

test('Vision recovery projects an evicted Hub image asset as its deterministic placeholder', async () => {
  const replay = harness();
  await feed(replay.handlers, [
    { event_sequence: 1, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-assets', turn_index: 1, user_content: 'first', images: [], historical_image_placeholder: '[historical images omitted: count=1; sources=none; relative_range=unavailable]' } },
    { event_sequence: 2, type: 'done', session_id: 'vision-assets' },
    { event_sequence: 3, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-assets', turn_index: 2, user_content: 'latest', images: ['data:image/jpeg;base64,latest'], historical_image_omission: true } },
    { event_sequence: 4, type: 'done', session_id: 'vision-assets' },
  ]);
  const first = replay.getMessages().find((message) => message.id === 'vision-assets-turn-1-user');
  assert.match(JSON.stringify(first?.content), /historical images omitted: count=1/);
  assert.doesNotMatch(JSON.stringify(first?.content), /base64/);
});

test('Vision Answer Now error and replacement content project into the original bubble like Hub recovery', async () => {
  const payloads = [
    { event_sequence: 1, type: 'timeline_event', event: { kind: 'turn_started', session_id: 'vision-s', turn_index: 1, user_content: 'question', images: [] } },
    { event_sequence: 2, type: 'content', content: 'partial' },
    { event_sequence: 3, type: 'error', error: 'old generation failed' },
    { event_sequence: 4, type: 'done', session_id: 'vision-s' },
    { event_sequence: 5, type: 'session_start', session_id: 'vision-s' },
    { event_sequence: 6, type: 'content', content: ' replacement' },
    { event_sequence: 7, type: 'done', session_id: 'vision-s' },
  ];
  const liveRun: AgentRunState = {
    assistantMessageId: 'vision-s-turn-1-assistant',
    projectedAssistantMessageIds: new Set(),
    stopWasClicked: false,
    answerNowWasClicked: true,
  };
  const live = harness([
    { id: 'vision-s-turn-1-user', role: 'user', content: 'question', images: [], timestamp: new Date(0) },
    { id: 'vision-s-turn-1-assistant', role: 'assistant', content: '', timestamp: new Date(0), isStreaming: true, agentSteps: [] },
  ], liveRun);
  const replay = harness();

  await feed(live.handlers, payloads);
  await feed(replay.handlers, payloads);

  assert.deepEqual(replay.getMessages(), live.getMessages());
  const assistant = replay.getMessages().find((message) => message.id === 'vision-s-turn-1-assistant');
  assert.equal(assistant?.content, 'partial replacement');
  assert.doesNotMatch(assistant?.content ?? '', /流式中断|Agent 执行出错/);
});

test('Vision and child pages subscribe through the shared Hub without lifecycle DELETE on detach', async () => {
  const [vision, child, recovery, header] = await Promise.all([
    readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8'),
    readFile(path.join(testDir, '../app/agent/child/[childRunId]/page.tsx'), 'utf8'),
    readFile(new URL('./conversation/use-conversation-recovery.ts', import.meta.url), 'utf8'),
    readFile(new URL('../components/conversation/ConversationHeader.tsx', import.meta.url), 'utf8'),
  ]);
  assert.match(vision, /useConversationRecovery\(/); assert.match(vision, /runAgentConversationTransport\(/);
  assert.match(recovery, /llmClient\.sessionHubEvents\(bookmark\.sessionId, 0/);
  assert.match(recovery, /bookmark\.interactionMode === 'agent'[\s\S]*dispatchAgentSessionPayload[\s\S]*chatHandlers\.onPayload/);
  assert.match(header, /恢复运行中的任务/);
  assert.match(vision, /bookmarkTransactionsRef\.current\.begin\([\s\S]*interactionMode/);
  assert.match(vision, /onKnownRejection:[\s\S]*rollback\(bookmarkTransaction\)/);
  assert.match(vision, /onDetached:[\s\S]*retain\(bookmarkTransaction\)[\s\S]*liveChatAttachmentsRef\.current\.detach/);
  assert.doesNotMatch(vision, /writeAgentSessionBookmark|writeChatTranscript|llmClient\.deleteSessionHub\(/);
  assert.match(recovery, /onSettled: result => \{\s*if \(!ownedController \|\| s\.abortControllerRef\.current !== ownedController\) return;/);
  assert.match(recovery, /settledTerminal = result\.kind === 'terminal'/);
  assert.match(recovery, /if \(!settledTerminal\) s\.setAttachedRecoverySessionId\(null\)/);
  const coordinator = await readFile(new URL('./session-hub-recovery.ts', import.meta.url), 'utf8');
  assert.match(coordinator, /if \(!isCurrent\(\)\) return result;\s*options\.onSettled\?\.\(result\);[\s\S]*if \(!isCurrent\(\)\) return result;/);
  assert.match(recovery, /s\.isLoadingRef\.current = busy;\s*s\.setIsLoading\(busy\)/);
  assert.match(child, /agentSessionEvents\(\s*params\.childRunId,\s*0,/);
  assert.match(child, /controller\.abort\(\)/); assert.match(child, /abortChildAgentRun\(params\.childRunId\)/);
  assert.doesNotMatch(child, /deleteAgentSession/);
});

test('Vision Trash resolves an unattached bookmark as its exact Hub deletion target', async () => {
  const vision = await readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8');
  const actions = await readFile(new URL('./conversation/conversation-actions.ts', import.meta.url), 'utf8');
  const clear = actions.slice(actions.indexOf('export function clearConversation'), actions.indexOf('export function confirmConversationSettings'));
  assert.match(vision, /clearConversation\(\{\s*pageMode:\s*'vision'/);
  assert.match(clear, /clearRetainedAgentSessionBeforeDelete\(/);
  assert.match(clear, /bookmarkSessionId:\s*state\.recoveryCandidate\?\.bookmark\.sessionId/);
  assert.match(clear, /staleBoundSessionId:\s*detached/);
  assert.match(clear, /deleteSession:\s*id\s*=>\s*llmClient\.deleteSessionHub\(id\)/);
  assert.doesNotMatch(clear, /deleteAgentSession/);
  const {resolveSessionHubClearTarget} = await import('./session-hub-recovery.ts');
  assert.equal(resolveSessionHubClearTarget({bookmarkSessionId:'recoverable', staleBoundSessionId:'old'}), 'recoverable');
  assert.equal(resolveSessionHubClearTarget({attachedSessionId:'current', bookmarkSessionId:'recoverable', staleBoundSessionId:'old'}), 'current');
  assert.match(vision, /const answerNowHandlers = createTextAgentSessionHandlers/);
  assert.match(vision, /answerNowStream\([\s\S]*answerNowHandlers,/);
  assert.doesNotMatch(vision, /createAnswerNowStreamingMessage/);
});

test('Vision Chat adapter keeps busy ownership across producer error until canonical done', async () => {
  const vision = await readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8');
  const chat = vision.indexOf('await runChatConversationTransport(');
  const done = vision.indexOf('onDone:', chat), error = vision.indexOf('onError:', done), end = vision.indexOf('lifecycle:', error);
  assert.ok(chat >= 0 && error > done && end > error);
  const doneBlock = vision.slice(done, error), errorBlock = vision.slice(error, end);
  assert.match(doneBlock, /stopStreamingMessage\(\)/);
  assert.match(errorBlock, /applyError\(liveChatAttachment/);
  assert.doesNotMatch(errorBlock, /detach\(liveChatAttachment|stopStreamingMessage|resetStreamingState|markCandidateComplete|chatResume: undefined/);
});

test('Vision Agent detached callback snapshots intentional interruption before async lifecycle cleanup', async () => {
  const vision = await readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8');
  assert.match(vision, /captureAgentInterruptionIntent/);
  assert.match(vision, /const intentionalInterruption = captureAgentInterruptionIntent\(/);
  assert.match(vision, /sessionLifecycleCallbacks\.onDetached\?\.\(error, \{ intentionalInterruption \}\)/);
  assert.match(vision, /if\s*\(intentionalInterruption\) return/);
  assert.match(vision, /error && !context\?\.intentionalInterruption/);
  assert.match(vision, /await lifecycleCallbacks\?\.onDetached\?\.\(error(?:, context)?\)[\s\S]*if\s*\(!isCurrentLiveAttachment\(\)\) return[\s\S]*bookmarkTransactionsRef\.current\.retain/);
});

test('Vision recovery contract has no timing guard or shadow replay reducer', async () => {
  const pageIntegration = await readFile(path.join(testDir, './vision-automation/page-integration.ts'), 'utf8');
  assert.doesNotMatch(pageIntegration, /AuxiliaryNavigationGuard|createVisionChildLinkActivation|runVisionAgentSessionLifecycle|setTimeout\(expire, 750\)/);
  assert.equal(existsSync(path.join(testDir, './agent-session-projection.ts')), false);
  assert.equal(existsSync(path.join(testDir, './agent-session-projection.test.mts')), false);
});

for (const source of ['model_text', 'provider_payload'] as const) {
  test(`Vision replay preserves ${source} disclosure source`, async () => {
    const projection = harness();
    const raw = '<tool_call>\n<function=read_file></function>\n</tool_call>';
    await feed(projection.handlers, [
      { event_sequence:1, type:'timeline_event', event:{kind:'turn_started', session_id:'raw-source',turn_index:1,user_content:'inspect',images:[],mode:'tool_calling'} },
      { event_sequence:2, type:'agent_step',step_type:'thought',content:'reasoning',partial:true },
      { event_sequence:3, type:'agent_step',step_type:'thought',raw_tool_call:raw,raw_tool_call_source:source,partial:false },
    ]);
    const thought = projection.getMessages().find((m) => m.role === 'assistant')?.agentSteps?.[0];
    assert.equal(thought?.raw_tool_call_source, source);
    assert.equal(thought?.raw_tool_call, raw);
  });
}
