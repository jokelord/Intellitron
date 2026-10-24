import assert from 'node:assert/strict';
import test from 'node:test';

import {
  appendAssistantDelta,
  createAssistantReasoningState,
  finalizeAssistantReasoning,
} from './assistant-reasoning.ts';

test('content delta without think tags stays in the visible answer body', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });
  const next = appendAssistantDelta(initial, {
    contentDelta: '直接答案',
    defaultReasoningCollapsed: false,
  });

  assert.equal(next.content, '直接答案');
  assert.equal(next.reasoning_content, '');
  assert.equal(next.reasoningDetected, false);
  assert.equal(next.reasoningParseState, 'outside');
});

test('single chunk think block is split into reasoning and final answer', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: true });
  const next = appendAssistantDelta(initial, {
    contentDelta: '<think>先分析问题</think>最终答案',
    defaultReasoningCollapsed: true,
  });

  assert.equal(next.reasoning_content, '先分析问题');
  assert.equal(next.content, '最终答案');
  assert.equal(next.reasoningDetected, true);
  assert.equal(next.reasoningCollapsed, true);
  assert.equal(next.reasoningParseState, 'outside');
});

test('split think tags across chunks keep parser state until the closing tag arrives', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });

  const first = appendAssistantDelta(initial, {
    contentDelta: '<thi',
    defaultReasoningCollapsed: false,
  });
  assert.equal(first.content, '');
  assert.equal(first.reasoning_content, '');

  const second = appendAssistantDelta(first, {
    contentDelta: 'nk>逐步推理',
    defaultReasoningCollapsed: false,
  });
  assert.equal(second.reasoning_content, '逐步推理');
  assert.equal(second.reasoningParseState, 'inside_think');

  const third = appendAssistantDelta(second, {
    contentDelta: '</th',
    defaultReasoningCollapsed: false,
  });
  assert.equal(third.reasoning_content, '逐步推理');
  assert.equal(third.reasoningParseState, 'inside_think');

  const fourth = appendAssistantDelta(third, {
    contentDelta: 'ink>答案',
    defaultReasoningCollapsed: false,
  });
  assert.equal(fourth.reasoning_content, '逐步推理');
  assert.equal(fourth.content, '答案');
  assert.equal(fourth.reasoningParseState, 'outside');
});

test('explicit reasoning delta and tagged reasoning both accumulate into reasoning_content', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });
  const withReasoning = appendAssistantDelta(initial, {
    reasoningDelta: '显式推理',
    defaultReasoningCollapsed: false,
  });
  const final = appendAssistantDelta(withReasoning, {
    contentDelta: '<think>补充推理</think>答案',
    defaultReasoningCollapsed: false,
  });

  assert.equal(final.reasoning_content, '显式推理补充推理');
  assert.equal(final.content, '答案');
  assert.equal(final.reasoningCollapsed, false);
});

test('reasoning-only streaming keeps the visible answer body empty', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: true });
  const next = appendAssistantDelta(initial, {
    contentDelta: '<think>还在推理中</think>',
    defaultReasoningCollapsed: true,
  });

  assert.equal(next.reasoning_content, '还在推理中');
  assert.equal(next.content, '');
  assert.equal(next.reasoningDetected, true);
});

test('finalize flushes an unterminated opening tag buffer into visible content', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });
  const pending = appendAssistantDelta(initial, {
    contentDelta: '<thi',
    defaultReasoningCollapsed: false,
  });

  const final = finalizeAssistantReasoning(pending, {
    defaultReasoningCollapsed: false,
  });

  assert.equal(final.content, '<thi');
  assert.equal(final.reasoning_content, '');
  assert.equal(final.reasoningBuffer, '');
  assert.equal(final.reasoningParseState, 'outside');
  assert.equal(final.reasoningDetected, false);
});

test('finalize flushes an unterminated closing tag buffer into reasoning content', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });
  const insideThink = appendAssistantDelta(initial, {
    contentDelta: '<think>逐步推理</th',
    defaultReasoningCollapsed: false,
  });

  const final = finalizeAssistantReasoning(insideThink, {
    defaultReasoningCollapsed: false,
  });

  assert.equal(final.reasoning_content, '逐步推理</th');
  assert.equal(final.content, '');
  assert.equal(final.reasoningBuffer, '');
  assert.equal(final.reasoningParseState, 'outside');
  assert.equal(final.reasoningDetected, true);
});

test('literal think markup stays visible once answer text has already started', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });
  const next = appendAssistantDelta(initial, {
    contentDelta: '答案<think>字面量</think>尾巴',
    defaultReasoningCollapsed: false,
  });

  assert.equal(next.content, '答案<think>字面量</think>尾巴');
  assert.equal(next.reasoning_content, '');
  assert.equal(next.reasoningDetected, false);
  assert.equal(next.reasoningParseState, 'outside');
});

test('split literal think markup stays visible once answer text has already started', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });
  const first = appendAssistantDelta(initial, {
    contentDelta: '答复<thi',
    defaultReasoningCollapsed: false,
  });
  const second = appendAssistantDelta(first, {
    contentDelta: 'nk>字面量</think>尾巴',
    defaultReasoningCollapsed: false,
  });

  assert.equal(second.content, '答复<think>字面量</think>尾巴');
  assert.equal(second.reasoning_content, '');
  assert.equal(second.reasoningDetected, false);
  assert.equal(second.reasoningParseState, 'outside');
  assert.equal(second.reasoningBuffer, '');
});

test('leading whitespace stays visible while a following think block is parsed as reasoning', () => {
  const initial = createAssistantReasoningState({ defaultReasoningCollapsed: false });
  const next = appendAssistantDelta(initial, {
    contentDelta: '\n<think>推理</think>答案',
    defaultReasoningCollapsed: false,
  });

  assert.equal(next.content, '\n答案');
  assert.equal(next.reasoning_content, '推理');
  assert.equal(next.reasoningDetected, true);
  assert.equal(next.reasoningParseState, 'outside');
});
