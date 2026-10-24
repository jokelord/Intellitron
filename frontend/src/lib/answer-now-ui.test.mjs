import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyAnswerNowReply,
  appendAnswerNowChunk,
  createAnswerNowStreamingMessage,
  finalizeStreamingAssistantMessage,
  shouldDisableAnswerNowButton,
  shouldShowAnswerNowStreamError,
  getStreamingTerminalBehavior,
  getAssistantRenderableText,
  captureAgentInterruptionIntent,
} from './answer-now-ui.ts';

test('captureAgentInterruptionIntent snapshots intentional Stop or Answer Now before refs change', () => {
  const flags = { stopWasClicked: true, answerNowWasClicked: false };
  const intentional = captureAgentInterruptionIntent(flags);
  flags.stopWasClicked = false;
  assert.equal(intentional, true);
  assert.equal(captureAgentInterruptionIntent({ stopWasClicked: false, answerNowWasClicked: false }), false);
});

test('finalizeStreamingAssistantMessage closes the active assistant stream', () => {
  const messages = [
    {
      id: 'user-1',
      role: 'user',
      content: 'Tell me more',
      timestamp: new Date('2024-01-01T00:00:00.000Z'),
    },
    {
      id: 'assistant-1',
      role: 'assistant',
      content: 'Partial answer',
      timestamp: new Date('2024-01-01T00:00:01.000Z'),
      isStreaming: true,
      agentSteps: [
        { step_type: 'thought', content: 'Thinking...', partial: true },
      ],
    },
  ];

  const updated = finalizeStreamingAssistantMessage(messages);

  assert.notStrictEqual(updated, messages);
  assert.equal(updated.length, 2);
  assert.equal(updated[1].isStreaming, false);
  assert.equal(updated[1].agentSteps[0].partial, false);
  assert.equal(messages[1].isStreaming, true);
  assert.equal(messages[1].agentSteps[0].partial, true);
});

test('applyAnswerNowReply preserves the interrupted assistant message and appends a new final reply', () => {
  const messages = [
    {
      id: 'assistant-1',
      role: 'assistant',
      content: 'Already streamed content',
      timestamp: new Date('2024-01-01T00:00:00.000Z'),
      isStreaming: true,
      agentSteps: [
        { step_type: 'thought', content: 'Collecting context', partial: true },
      ],
    },
  ];

  const finalReply = {
    id: 'assistant-2',
    role: 'assistant',
    content: 'Here is the final answer now.',
    timestamp: new Date('2024-01-01T00:00:02.000Z'),
  };

  const updated = applyAnswerNowReply(messages, finalReply);

  assert.equal(updated.length, 2);
  assert.equal(updated[0].content, 'Already streamed content');
  assert.equal(updated[0].isStreaming, false);
  assert.equal(updated[0].agentSteps[0].partial, false);
  assert.deepEqual(updated[1], finalReply);
});

test('applyAnswerNowReply drops an empty streaming assistant placeholder before appending final reply', () => {
  const messages = [
    {
      id: 'assistant-1',
      role: 'assistant',
      content: '',
      timestamp: new Date('2024-01-01T00:00:00.000Z'),
      isStreaming: true,
    },
  ];

  const finalReply = {
    id: 'assistant-2',
    role: 'assistant',
    content: 'Final answer',
    timestamp: new Date('2024-01-01T00:00:01.000Z'),
  };

  const updated = applyAnswerNowReply(messages, finalReply);

  assert.equal(updated.length, 1);
  assert.deepEqual(updated[0], finalReply);
});

test('applyAnswerNowReply preserves a reasoning-only assistant bubble before appending final reply', () => {
  const messages = [
    {
      id: 'assistant-1',
      role: 'assistant',
      content: '',
      reasoning_content: '还在推理',
      reasoningDetected: true,
      timestamp: new Date('2024-01-01T00:00:00.000Z'),
      isStreaming: true,
    },
  ];

  const finalReply = {
    id: 'assistant-2',
    role: 'assistant',
    content: 'Final answer',
    timestamp: new Date('2024-01-01T00:00:01.000Z'),
  };

  const updated = applyAnswerNowReply(messages, finalReply);

  assert.equal(updated.length, 2);
  assert.equal(updated[0].reasoning_content, '还在推理');
  assert.equal(updated[0].reasoningDetected, true);
  assert.equal(updated[0].isStreaming, false);
  assert.deepEqual(updated[1], finalReply);
});

test('finalizeStreamingAssistantMessage finalizes the last streaming assistant bubble instead of a newer completed one', () => {
  const messages = [
    {
      id: 'assistant-streaming',
      role: 'assistant',
      content: 'Earlier stream',
      timestamp: new Date('2024-01-01T00:00:00.000Z'),
      isStreaming: true,
      agentSteps: [
        { step_type: 'thought', content: 'Thinking...', partial: true },
      ],
    },
    {
      id: 'assistant-final',
      role: 'assistant',
      content: 'Already final',
      timestamp: new Date('2024-01-01T00:00:01.000Z'),
      isStreaming: false,
      agentSteps: [
        { step_type: 'thought', content: 'Done', partial: false },
      ],
    },
  ];

  const updated = finalizeStreamingAssistantMessage(messages);

  assert.equal(updated[0].isStreaming, false);
  assert.equal(updated[0].agentSteps[0].partial, false);
  assert.equal(updated[1].isStreaming, false);
  assert.equal(updated[1].agentSteps[0].partial, false);
  assert.equal(updated[1].content, 'Already final');
});

test('finalizeStreamingAssistantMessage flushes trailing reasoning buffer into visible content before closing', () => {
  const messages = [
    {
      id: 'assistant-1',
      role: 'assistant',
      content: '',
      timestamp: new Date('2024-01-01T00:00:00.000Z'),
      isStreaming: true,
      reasoningParseState: 'outside',
      reasoningBuffer: '<thi',
    },
  ];

  const updated = finalizeStreamingAssistantMessage(messages);

  assert.equal(updated[0].isStreaming, false);
  assert.equal(updated[0].content, '<thi');
  assert.equal(updated[0].reasoningBuffer, '');
  assert.equal(updated[0].reasoningParseState, 'outside');
});

test('applyAnswerNowReply flushes buffered reasoning content before appending final reply', () => {
  const messages = [
    {
      id: 'assistant-1',
      role: 'assistant',
      content: '',
      reasoning_content: '逐步分析',
      reasoningDetected: true,
      reasoningParseState: 'inside_think',
      reasoningBuffer: '</th',
      timestamp: new Date('2024-01-01T00:00:00.000Z'),
      isStreaming: true,
    },
  ];

  const finalReply = {
    id: 'assistant-2',
    role: 'assistant',
    content: 'Final answer',
    timestamp: new Date('2024-01-01T00:00:01.000Z'),
  };

  const updated = applyAnswerNowReply(messages, finalReply);

  assert.equal(updated.length, 2);
  assert.equal(updated[0].isStreaming, false);
  assert.equal(updated[0].reasoning_content, '逐步分析</th');
  assert.equal(updated[0].reasoningBuffer, '');
  assert.equal(updated[0].reasoningParseState, 'outside');
  assert.deepEqual(updated[1], finalReply);
});

test('getAssistantRenderableText hides placeholder text for finalized step-only assistant messages', () => {
  const message = {
    id: 'assistant-1',
    role: 'assistant',
    content: '',
    timestamp: new Date('2024-01-01T00:00:00.000Z'),
    isStreaming: false,
    agentSteps: [{ step_type: 'thought', content: 'Collected clues', partial: false }],
  };

  assert.equal(getAssistantRenderableText(message, false), '');
  assert.equal(getAssistantRenderableText(message, true), '已停止');
});

test('getAssistantRenderableText keeps the main bubble empty for reasoning-only assistant messages', () => {
  const message = {
    id: 'assistant-1',
    role: 'assistant',
    content: '',
    reasoning_content: '还在推理',
    reasoningDetected: true,
    timestamp: new Date('2024-01-01T00:00:00.000Z'),
    isStreaming: true,
  };

  assert.equal(getAssistantRenderableText(message, false), '');
  assert.equal(getAssistantRenderableText({ ...message, isStreaming: false }, false), '');
  assert.equal(getAssistantRenderableText({ ...message, isStreaming: false }, true), '已停止');
});

test('getAssistantRenderableText ignores whitespace-only content when reasoning is the only real content', () => {
  const message = {
    id: 'assistant-1',
    role: 'assistant',
    content: '   \n\t  ',
    reasoning_content: '还在推理',
    reasoningDetected: true,
    timestamp: new Date('2024-01-01T00:00:00.000Z'),
    isStreaming: false,
  };

  assert.equal(getAssistantRenderableText(message, false), '');
  assert.equal(getAssistantRenderableText(message, true), '已停止');
});

test('getStreamingTerminalBehavior keeps loading active while answer-now is still pending', () => {
  assert.deepEqual(
    getStreamingTerminalBehavior({ stopWasClicked: false, answerNowWasClicked: true }),
    {
      suppressErrorMessage: true,
      resetStreamingState: false,
      resetStoppedState: false,
      clearAnswerNowFlag: false,
    },
  );
});

test('late terminal callback from the old run cannot clear replacement-owned loading state', () => {
  const behavior = getStreamingTerminalBehavior({
    stopWasClicked: false,
    answerNowWasClicked: true,
  });

  assert.equal(behavior.resetStreamingState, false);
  assert.equal(behavior.clearAnswerNowFlag, false);
});

test('Stop during replacement overrides answer-now terminal suppression', () => {
  assert.deepEqual(
    getStreamingTerminalBehavior({ stopWasClicked: true, answerNowWasClicked: true }),
    {
      suppressErrorMessage: true,
      resetStreamingState: true,
      resetStoppedState: false,
      clearAnswerNowFlag: true,
    },
  );
});

test('getStreamingTerminalBehavior preserves existing stop behavior', () => {
  assert.deepEqual(
    getStreamingTerminalBehavior({ stopWasClicked: true, answerNowWasClicked: false }),
    {
      suppressErrorMessage: true,
      resetStreamingState: true,
      resetStoppedState: false,
      clearAnswerNowFlag: true,
    },
  );
});

test('createAnswerNowStreamingMessage creates a fresh streaming assistant bubble', () => {
  const message = createAnswerNowStreamingMessage('assistant-2', new Date('2024-01-01T00:00:02.000Z'));

  assert.deepEqual(message, {
    id: 'assistant-2',
    role: 'assistant',
    content: '',
    timestamp: new Date('2024-01-01T00:00:02.000Z'),
    isStreaming: true,
  });
});

test('appendAnswerNowChunk appends streamed content into the latest assistant bubble', () => {
  const messages = [
    createAnswerNowStreamingMessage('assistant-2', new Date('2024-01-01T00:00:02.000Z')),
  ];

  const updated = appendAnswerNowChunk(messages, '第一段');
  const updatedAgain = appendAnswerNowChunk(updated, '第二段');

  assert.equal(updatedAgain[0].content, '第一段第二段');
  assert.equal(updatedAgain[0].isStreaming, true);
});

test('shouldDisableAnswerNowButton disables repeated clicks while answer-now is pending', () => {
  assert.equal(
    shouldDisableAnswerNowButton({
      isLoading: true,
      isAnswerNowLoading: true,
      agentMode: true,
      agentSessionId: 'agent-session',
    }),
    true,
  );
  assert.equal(
    shouldDisableAnswerNowButton({
      isLoading: true,
      isAnswerNowLoading: false,
      agentMode: true,
      agentSessionId: 'agent-session',
    }),
    false,
  );
});

test('shouldDisableAnswerNowButton requires an agent session while loading', () => {
  assert.equal(
    shouldDisableAnswerNowButton({
      agentMode: true,
      isLoading: true,
      isAnswerNowLoading: false,
      agentSessionId: null,
    }),
    true,
  );
});

test('shouldDisableAnswerNowButton enables vLLM agent sessions without provider gating', () => {
  assert.equal(
    shouldDisableAnswerNowButton({
      agentMode: true,
      isLoading: true,
      isAnswerNowLoading: false,
      agentSessionId: 'vllm-agent-session',
    }),
    false,
  );
});

test('shouldShowAnswerNowStreamError suppresses abort text when user clicked stop', () => {
  assert.equal(shouldShowAnswerNowStreamError(true), false);
  assert.equal(shouldShowAnswerNowStreamError(false), true);
});
