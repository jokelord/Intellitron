import assert from 'node:assert/strict';
import test from 'node:test';
import { normalizeMermaidTimelineChart } from './mermaid-timeline.ts';

test('Replaces ASCII colons in period segments while keeping title and event text unchanged', () => {
  const input = [
    'timeline',
    '    title 桌面 8 分钟叙事',
    '    08:41 : 浏览器显示上一轮回复尾部（修复效果 + 一句话总结）',
    '    08:49 : 切到 Codex：47 分钟修复完成，但审阅又发现两个并发竞态',
    '    08:50 : 回浏览器：vision 采集 32/32 成功 0 失败',
  ].join('\n');
  const expected = [
    'timeline',
    '    title 桌面 8 分钟叙事',
    '    08：41 : 浏览器显示上一轮回复尾部（修复效果 + 一句话总结）',
    '    08：49 : 切到 Codex：47 分钟修复完成，但审阅又发现两个并发竞态',
    '    08：50 : 回浏览器：vision 采集 32/32 成功 0 失败',
  ].join('\n');

  assert.equal(normalizeMermaidTimelineChart(input), expected);
});

test('Normalizes bare malformed period lines without events', () => {
  assert.equal(normalizeMermaidTimelineChart('timeline\n    08:41\n'), 'timeline\n    08：41\n');
});

test('Preserves multi-event separator syntax completely unchanged', () => {
  const chart = 'timeline\n    Day 1 : event A : event B\n';

  assert.equal(normalizeMermaidTimelineChart(chart), chart);
});

test('Leaves non-timeline charts byte-for-byte unchanged', () => {
  const chart = 'flowchart TD\n    A[foo: bar] --> B[bar: baz]\n';

  assert.equal(normalizeMermaidTimelineChart(chart), chart);
});

test('Keeps directive lines and accDescr multiline blocks untouched', () => {
  const input = [
    'timeline',
    '    title Some: Title',
    '    section Phase: One',
    '    accTitle doc: title',
    '    # comment: here',
    '    %% comment: here too',
    '    accDescr {',
    '      description: with colon',
    '    }',
    '    08:41 : event one',
  ].join('\n');
  const expected = [
    'timeline',
    '    title Some: Title',
    '    section Phase: One',
    '    accTitle doc: title',
    '    # comment: here',
    '    %% comment: here too',
    '    accDescr {',
    '      description: with colon',
    '    }',
    '    08：41 : event one',
  ].join('\n');

  assert.equal(normalizeMermaidTimelineChart(input), expected);
});

test('Normalizes timeline LR variants', () => {
  assert.equal(
    normalizeMermaidTimelineChart('timeline LR\n    08:41 : event\n'),
    'timeline LR\n    08：41 : event\n',
  );
});

test('Returns empty strings and colon-free charts unchanged', () => {
  assert.equal(normalizeMermaidTimelineChart(''), '');
  assert.equal(
    normalizeMermaidTimelineChart('timeline\n    day one\n    day two'),
    'timeline\n    day one\n    day two',
  );
});
