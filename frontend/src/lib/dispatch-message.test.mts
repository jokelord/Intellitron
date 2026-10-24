import assert from 'node:assert/strict';
import test from 'node:test';

import {
  appendAnswerNowChunkById,
  finalizeAnswerNowMessage,
  getAssistantRenderableText,
} from './answer-now-ui.ts';
import {
  collectTrackedAssistantMessageIds,
  completeRunningDispatchStep,
  finalizeTrackedAssistantMessage,
  finalizeRunningDispatchStep,
  appendProjectedChatChunk,
  getActiveTrackedAssistantMessageId,
  getContentTargetAssistantMessageId,
  getDispatchCardCopy,
  getDispatchCardLiveText,
  mergeDispatchStep,
  retractTrackedAssistantTailText,
  stripTrailingCanonicalSwitchAgentControl,
  updateTrackedAssistantMessage,
} from './dispatch-message.ts';

test('mergeDispatchStep uses the newest cumulative backend dispatch message', () => {
  const previousStep = {
    step_type: 'dispatch' as const,
    dispatch_status: 'running' as const,
    dispatch_message: 'Routing to coordinator.\n',
  };

  const nextStep = {
    step_type: 'dispatch' as const,
    dispatch_status: 'running' as const,
    dispatch_message: 'Routing to coordinator.\nHanding off to researcher.\n',
  };

  const merged = mergeDispatchStep(previousStep, nextStep);

  assert.equal(
    merged.dispatch_message,
    'Routing to coordinator.\nHanding off to researcher.\n',
  );
});

test('mergeDispatchStep preserves the previous dispatch message when the update omits one', () => {
  const previousStep = {
    step_type: 'dispatch' as const,
    dispatch_status: 'running' as const,
    dispatch_message: 'Routing to coordinator.\nHanding off to researcher.\n',
  };

  const nextStep = {
    step_type: 'dispatch' as const,
    dispatch_status: 'completed' as const,
  };

  const merged = mergeDispatchStep(previousStep, nextStep);

  assert.equal(
    merged.dispatch_message,
    'Routing to coordinator.\nHanding off to researcher.\n',
  );
  assert.equal(merged.dispatch_status, 'completed');
});

test('mergeDispatchStep preserves the existing step id across dispatch updates', () => {
  const previousStep = {
    id: 'dispatch-step-1',
    step_type: 'dispatch' as const,
    dispatch_status: 'running' as const,
    dispatch_message: 'Routing to coordinator.\n',
  };

  const nextStep = {
    id: 'dispatch-step-2',
    step_type: 'dispatch' as const,
    dispatch_status: 'completed' as const,
    dispatch_stage: 'handoff' as const,
  };

  const merged = mergeDispatchStep(previousStep, nextStep);

  assert.equal(merged.id, 'dispatch-step-1');
  assert.equal(merged.dispatch_stage, 'handoff');
});

test('finalizeRunningDispatchStep marks a stopped dispatch as aborted with a stable message', () => {
  const finalized = finalizeRunningDispatchStep(
    {
      step_type: 'dispatch' as const,
      dispatch_status: 'running' as const,
      dispatch_message: 'Routing to coordinator.\n',
    },
    {
      stopWasClicked: true,
      answerNowWasClicked: false,
      errorMessage: 'The operation was aborted.',
    },
  );

  assert.deepEqual(finalized, {
    step_type: 'dispatch',
    dispatch_status: 'aborted',
    dispatch_message: '任务调度已中止',
  });
});

test('finalizeRunningDispatchStep keeps regular error behavior for non-stop terminal failures', () => {
  const finalized = finalizeRunningDispatchStep(
    {
      step_type: 'dispatch' as const,
      dispatch_status: 'running' as const,
      dispatch_message: 'Routing to coordinator.\n',
    },
    {
      stopWasClicked: false,
      answerNowWasClicked: false,
      errorMessage: 'backend exploded',
    },
  );

  assert.equal(finalized?.dispatch_status, 'error');
  assert.equal(finalized?.dispatch_message, 'backend exploded');
});

test('finalizeRunningDispatchStep clears answer-now handoff without leaving a running dispatch', () => {
  const finalized = finalizeRunningDispatchStep(
    {
      step_type: 'dispatch' as const,
      dispatch_status: 'running' as const,
      dispatch_message: 'Routing to coordinator.\n',
    },
    {
      stopWasClicked: false,
      answerNowWasClicked: true,
      errorMessage: 'The operation was aborted.',
    },
  );

  assert.equal(finalized?.dispatch_status, 'completed');
  assert.equal(finalized?.dispatch_message, 'Routing to coordinator.\n');
});

test('getDispatchCardCopy treats completed routing as finished instead of still running', () => {
  assert.deepEqual(
    getDispatchCardCopy({
      step_type: 'dispatch',
      dispatch_status: 'completed',
      dispatch_stage: 'routing',
    }),
    {
      statusText: '已完成执行路径判定',
      subText: '系统已完成当前请求的执行路径判定',
    },
  );
});

test('getDispatchCardCopy humanizes completed secondary handoff copy', () => {
  assert.deepEqual(
    getDispatchCardCopy({
      step_type: 'dispatch',
      dispatch_kind: 'secondary_handoff',
      dispatch_status: 'completed',
      dispatch_target_agent: 'basic_agent',
    }),
    {
      statusText: '已转交执行代理',
      subText: '系统已将任务转交给 basic_agent 继续处理',
    },
  );
});

test('getDispatchCardLiveText prefers finalized copy over stale running text once dispatch completes', () => {
  assert.equal(
    getDispatchCardLiveText({
      step_type: 'dispatch',
      dispatch_status: 'completed',
      dispatch_stage: 'routing',
      dispatch_message: '正在分析当前任务',
    }),
    '系统已完成当前请求的执行路径判定',
  );
  assert.equal(
    getDispatchCardLiveText({
      step_type: 'dispatch',
      dispatch_status: 'running',
      dispatch_stage: 'routing',
      dispatch_message: '正在分析当前任务',
    }),
    '正在分析当前任务',
  );
});

test('getAssistantRenderableText applies stopped marker per message instead of globally', () => {
  assert.equal(
    getAssistantRenderableText({ content: '第一条回复', isStreaming: false }, false),
    '第一条回复',
  );
  assert.equal(
    getAssistantRenderableText({ content: '第二条回复', isStreaming: false }, true),
    '第二条回复 [已停止]',
  );
});

test('updateTrackedAssistantMessage updates the tracked assistant even when it is not the last message', () => {
  const messages = [
    {
      id: 'assistant-original',
      role: 'assistant',
      content: '',
      agentSteps: [{ step_type: 'dispatch' as const, dispatch_status: 'running' as const }],
    },
    {
      id: 'assistant-answer-now',
      role: 'assistant',
      content: '',
    },
  ];

  const updated = updateTrackedAssistantMessage(messages, 'assistant-original', (message) => ({
    ...message,
    agentSteps: [finalizeRunningDispatchStep(message.agentSteps?.[0], {
      stopWasClicked: false,
      answerNowWasClicked: true,
      errorMessage: 'aborted',
    })!],
  }));

  assert.equal(updated[0].agentSteps?.[0].dispatch_status, 'completed');
  assert.equal(updated[1].agentSteps, undefined);
});

test('appendProjectedChatChunk creates a distinct assistant message when projection targets a new message id', () => {
  const messages = [{
    id: 'assistant-original',
    role: 'assistant',
    content: '旧气泡',
    timestamp: new Date('2026-05-13T10:00:00Z'),
    isStreaming: false,
  }];

  const updated = appendProjectedChatChunk(messages, {
    messageId: 'assistant-projected',
    text: '新气泡首段',
    partial: true,
  }, () => new Date('2026-05-13T10:00:01Z'));

  assert.equal(updated.length, 2);
  assert.equal(updated[0].content, '旧气泡');
  assert.equal(updated[1].id, 'assistant-projected');
  assert.equal(updated[1].content, '新气泡首段');
  assert.equal(updated[1].reasoningCollapsed, false);
  assert.equal(updated[1].reasoningDetected, false);
  assert.equal(updated[1].isStreaming, true);
});

test('stripTrailingCanonicalSwitchAgentControl removes a complete trailing switch control span', () => {
  const prose = '我需要切换给 basic_agent。\n\n';
  const payload = '[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}]';

  assert.deepEqual(
    stripTrailingCanonicalSwitchAgentControl(`${prose}${payload}`),
    {
      text: prose,
      retracted: true,
    },
  );
});

test('stripTrailingCanonicalSwitchAgentControl removes trailing switch control span with extra trailing newlines', () => {
  const prose = '我需要查最新信息，所以转交执行代理。\n';
  const payload = '[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}]\n\n';

  assert.deepEqual(
    stripTrailingCanonicalSwitchAgentControl(`${prose}${payload}`),
    {
      text: `${prose}\n\n`,
      retracted: true,
    },
  );
});

test('stripTrailingCanonicalSwitchAgentControl leaves formulas and plain JSON examples intact', () => {
  assert.deepEqual(
    stripTrailingCanonicalSwitchAgentControl('矩阵是 [a, b, c]'),
    { text: '矩阵是 [a, b, c]', retracted: false },
  );

  assert.deepEqual(
    stripTrailingCanonicalSwitchAgentControl('{"demo":"switch_agent"}'),
    { text: '{"demo":"switch_agent"}', retracted: false },
  );
});

test('retractTrackedAssistantTailText only updates the targeted assistant bubble', () => {
  const messages = [
    {
      id: 'assistant-1',
      role: 'assistant',
      content: '第一气泡',
    },
    {
      id: 'assistant-2',
      role: 'assistant',
      content: '我需要切换。\n\n[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}]',
    },
  ];

  const updated = retractTrackedAssistantTailText(
    messages,
    'assistant-2',
    stripTrailingCanonicalSwitchAgentControl,
  );

  assert.equal(updated[0].content, '第一气泡');
  assert.equal(updated[1].content, '我需要切换。\n\n');
});

test('retractTrackedAssistantTailText leaves mid-body switch JSON examples intact', () => {
  const messages = [
    {
      id: 'assistant-2',
      role: 'assistant',
      content: '解释格式时可以写 [{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}] 作为示例，但这不是尾部控制串。后面还有正文。',
    },
  ];

  const updated = retractTrackedAssistantTailText(
    messages,
    'assistant-2',
    stripTrailingCanonicalSwitchAgentControl,
  );

  assert.equal(updated[0].content, messages[0].content);
});

test('appendProjectedChatChunk splits projected leading think content into reasoning fields by default without options', () => {
  const updated = appendProjectedChatChunk([], {
    messageId: 'assistant-projected',
    text: '<think>默认推理</think>默认答案',
    partial: false,
  }, () => new Date('2026-05-13T10:00:01Z'));

  assert.equal(updated.length, 1);
  assert.equal(updated[0].content, '默认答案');
  assert.equal(updated[0].reasoning_content, '默认推理');
  assert.equal(updated[0].reasoningDetected, true);
  assert.equal(updated[0].reasoningCollapsed, false);
  assert.equal(updated[0].isStreaming, false);
});

test('appendProjectedChatChunk splits projected leading think content into reasoning fields for a new targeted assistant bubble', () => {
  const updated = appendProjectedChatChunk([], {
    messageId: 'assistant-projected',
    text: '<think>先分析</think>最终答案',
    partial: true,
  }, () => new Date('2026-05-13T10:00:01Z'), undefined, {
    defaultReasoningCollapsed: true,
  });

  assert.equal(updated.length, 1);
  assert.equal(updated[0].id, 'assistant-projected');
  assert.equal(updated[0].content, '最终答案');
  assert.equal(updated[0].reasoning_content, '先分析');
  assert.equal(updated[0].reasoningDetected, true);
  assert.equal(updated[0].reasoningCollapsed, true);
  assert.equal(updated[0].isStreaming, true);
});

test('appendProjectedChatChunk keeps a targeted assistant bubble body empty while projected reasoning is still streaming', () => {
  const first = appendProjectedChatChunk([], {
    messageId: 'assistant-projected',
    text: '<think>还在推理',
    partial: true,
  }, () => new Date('2026-05-13T10:00:01Z'), undefined, {
    defaultReasoningCollapsed: false,
  });

  assert.equal(first[0].content, '');
  assert.equal(first[0].reasoning_content, '还在推理');
  assert.equal(first[0].reasoningParseState, 'inside_think');
  assert.equal(first[0].reasoningCollapsed, false);

  const second = appendProjectedChatChunk(first, {
    messageId: 'assistant-projected',
    text: '</think>答案',
    partial: false,
  }, () => new Date('2026-05-13T10:00:02Z'), undefined, {
    defaultReasoningCollapsed: false,
  });

  assert.equal(second.length, 1);
  assert.equal(second[0].content, '答案');
  assert.equal(second[0].reasoning_content, '还在推理');
  assert.equal(second[0].reasoningParseState, 'outside');
  assert.equal(second[0].isStreaming, false);
});

test('appendProjectedChatChunk routes explicit reasoning chunks into the foldable reasoning field', () => {
  const updated = appendProjectedChatChunk([], {
    messageId: 'assistant-projected',
    text: '先分析用户意图',
    partial: true,
    reasoning: true,
  }, () => new Date('2026-05-13T10:00:01Z'), undefined, {
    defaultReasoningCollapsed: true,
  });

  assert.equal(updated.length, 1);
  assert.equal(updated[0].content, '');
  assert.equal(updated[0].reasoning_content, '先分析用户意图');
  assert.equal(updated[0].reasoningDetected, true);
  assert.equal(updated[0].reasoningCollapsed, true);
  assert.equal(updated[0].isStreaming, true);
});

test('appendProjectedChatChunk preserves literal text behavior for plain projected content updates', () => {
  const messages = [{
    id: 'assistant-projected',
    role: 'assistant',
    content: '旧内容',
    timestamp: new Date('2026-05-13T10:00:00Z'),
    isStreaming: true,
  }];

  const updated = appendProjectedChatChunk(messages, {
    messageId: 'assistant-projected',
    text: '继续输出',
    partial: false,
  }, () => new Date('2026-05-13T10:00:01Z'));

  assert.equal(updated.length, 1);
  assert.equal(updated[0].content, '旧内容继续输出');
  assert.equal(updated[0].reasoning_content, '');
  assert.equal(updated[0].reasoningDetected, false);
  assert.equal(updated[0].isStreaming, false);
});

test('appendProjectedChatChunk ignores reasoning-routed chunks without a target message id', () => {
  const messages = [{
    id: 'assistant-existing',
    role: 'assistant',
    content: '现有答案',
    timestamp: new Date('2026-05-13T10:00:00Z'),
    isStreaming: true,
  }];

  const updated = appendProjectedChatChunk(messages, {
    text: '<think>不应串到旧气泡</think>答案',
    partial: true,
  }, () => new Date('2026-05-13T10:00:01Z'), undefined, {
    defaultReasoningCollapsed: true,
  });

  assert.deepEqual(updated, messages);
});

test('appendProjectedChatChunk ignores projected chunks without a target message id even for plain text', () => {
  const messages = [{
    id: 'assistant-existing',
    role: 'assistant',
    content: '现有答案',
    timestamp: new Date('2026-05-13T10:00:00Z'),
    isStreaming: true,
  }];

  const updated = appendProjectedChatChunk(messages, {
    text: '不应追加到最后一个气泡',
    partial: false,
  }, () => new Date('2026-05-13T10:00:01Z'));

  assert.deepEqual(updated, messages);
});

test('collectTrackedAssistantMessageIds preserves the tracked assistant id before projected ids', () => {
  assert.deepEqual(
    collectTrackedAssistantMessageIds('assistant-original', new Set(['assistant-projected-1', 'assistant-projected-2'])),
    ['assistant-original', 'assistant-projected-1', 'assistant-projected-2'],
  );
});

test('collectTrackedAssistantMessageIds appends execution bubble id without duplicates', () => {
  assert.deepEqual(
    collectTrackedAssistantMessageIds(
      'assistant-original',
      new Set(['assistant-projected-1', 'assistant-projected-2']),
      'assistant-execution',
    ),
    ['assistant-original', 'assistant-projected-1', 'assistant-projected-2', 'assistant-execution'],
  );
  assert.deepEqual(
    collectTrackedAssistantMessageIds(
      'assistant-original',
      new Set(['assistant-projected-1', 'assistant-execution']),
      'assistant-execution',
    ),
    ['assistant-original', 'assistant-projected-1', 'assistant-execution'],
  );
});

test('getActiveTrackedAssistantMessageId prefers the newest projected coordinator bubble', () => {
  assert.equal(
    getActiveTrackedAssistantMessageId('assistant-original', new Set(['assistant-projected-1', 'assistant-projected-2'])),
    'assistant-projected-2',
  );
});

test('getActiveTrackedAssistantMessageId falls back to the original assistant id when no projected bubble exists', () => {
  assert.equal(
    getActiveTrackedAssistantMessageId('assistant-original', new Set()),
    'assistant-original',
  );
});

test('getActiveTrackedAssistantMessageId prefers execution bubble once present', () => {
  assert.equal(
    getActiveTrackedAssistantMessageId(
      'assistant-original',
      new Set(['assistant-projected-1', 'assistant-projected-2']),
      'assistant-execution',
    ),
    'assistant-execution',
  );
});

test('getContentTargetAssistantMessageId ignores legacy content when coordinator_2 already owns a projected bubble', () => {
  assert.equal(
    getContentTargetAssistantMessageId(
      'assistant-original',
      new Set(['assistant-projected']),
    ),
    null,
  );
});

test('getContentTargetAssistantMessageId prefers execution bubble after secondary handoff', () => {
  assert.equal(
    getContentTargetAssistantMessageId(
      'assistant-original',
      new Set(['assistant-projected']),
      'assistant-execution',
    ),
    'assistant-execution',
  );
});

test('getContentTargetAssistantMessageId falls back to the original assistant bubble on direct basic_agent turns', () => {
  assert.equal(
    getContentTargetAssistantMessageId('assistant-original', new Set()),
    'assistant-original',
  );
});

test('updateTrackedAssistantMessage finalizes a tracked running dispatch on clean completion', () => {
  const messages = [{
    id: 'assistant-original',
    role: 'assistant',
    content: '',
    agentSteps: [{ step_type: 'dispatch' as const, dispatch_status: 'running' as const }],
  }];

  const updated = updateTrackedAssistantMessage(messages, 'assistant-original', (message) => ({
    ...message,
    agentSteps: [completeRunningDispatchStep(message.agentSteps?.[0])!],
  }));

  assert.equal(updated[0].agentSteps?.[0].dispatch_status, 'completed');
});

test('finalizeTrackedAssistantMessage finalizes only the targeted assistant message', () => {
  const messages = [
    {
      id: 'assistant-original',
      role: 'assistant',
      content: '',
      isStreaming: true,
      agentSteps: [{
        step_type: 'thought' as const,
        partial: true,
        content: 'thinking',
      }],
    },
    {
      id: 'assistant-answer-now',
      role: 'assistant',
      content: '',
      isStreaming: true,
      agentSteps: [{
        step_type: 'thought' as const,
        partial: true,
        content: 'still streaming',
      }],
    },
  ];

  const updated = finalizeTrackedAssistantMessage(messages, 'assistant-original');

  assert.equal(updated[0].isStreaming, false);
  assert.equal(updated[0].agentSteps?.[0].partial, false);
  assert.equal(updated[1].isStreaming, true);
  assert.equal(updated[1].agentSteps?.[0].partial, true);
});

test('finalizeTrackedAssistantMessage flushes projected reasoning parser state for the targeted assistant bubble', () => {
  const messages = [
    {
      id: 'assistant-projected',
      role: 'assistant',
      content: '',
      reasoning_content: '逐步推理',
      reasoningDetected: true,
      reasoningParseState: 'inside_think' as const,
      reasoningBuffer: '</th',
      isStreaming: true,
      agentSteps: [{
        step_type: 'thought' as const,
        partial: true,
        content: 'thinking',
      }],
    },
    {
      id: 'assistant-other',
      role: 'assistant',
      content: '其他消息',
      isStreaming: true,
    },
  ];

  const updated = finalizeTrackedAssistantMessage(messages, 'assistant-projected');

  assert.equal(updated[0].isStreaming, false);
  assert.equal(updated[0].agentSteps?.[0].partial, false);
  assert.equal(updated[0].reasoning_content, '逐步推理</th');
  assert.equal(updated[0].reasoningBuffer, '');
  assert.equal(updated[0].reasoningParseState, 'outside');
  assert.equal(updated[1].isStreaming, true);
});

test('finalizeTrackedAssistantMessage is a no-op when the tracked message is missing', () => {
  const messages = [{
    id: 'assistant-original',
    role: 'assistant',
    content: '',
    isStreaming: true,
  }];

  const updated = finalizeTrackedAssistantMessage(messages, 'missing-id');

  assert.deepEqual(updated, messages);
});

test('appendAnswerNowChunkById updates only the targeted answer-now message', () => {
  const messages = [
    {
      id: 'assistant-newer',
      role: 'assistant',
      content: 'newer run',
      isStreaming: true,
    },
    {
      id: 'assistant-answer-now',
      role: 'assistant',
      content: '',
      isStreaming: true,
    },
  ];

  const updated = appendAnswerNowChunkById(messages, 'assistant-answer-now', 'partial answer');

  assert.equal(updated[0].content, 'newer run');
  assert.equal(updated[1].content, 'partial answer');
});

test('finalizeAnswerNowMessage finalizes only the targeted answer-now message', () => {
  const messages = [
    {
      id: 'assistant-newer',
      role: 'assistant',
      content: 'newer run',
      isStreaming: true,
      agentSteps: [{ step_type: 'thought' as const, partial: true, content: 'new' }],
    },
    {
      id: 'assistant-answer-now',
      role: 'assistant',
      content: '',
      isStreaming: true,
      agentSteps: [{ step_type: 'thought' as const, partial: true, content: 'answer' }],
    },
  ];

  const updated = finalizeAnswerNowMessage(messages, 'assistant-answer-now');

  assert.equal(updated[0].isStreaming, true);
  assert.equal(updated[0].agentSteps?.[0].partial, true);
  assert.equal(updated[1].isStreaming, false);
  assert.equal(updated[1].agentSteps?.[0].partial, false);
});
