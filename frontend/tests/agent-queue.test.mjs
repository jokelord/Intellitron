import test from 'node:test';
import assert from 'node:assert/strict';

function enqueueVisionMessage(queue, text, images) {
  const trimmedText = text.trim();
  if (!trimmedText && images.length === 0) {
    return queue;
  }

  return [
    ...queue,
    {
      text: trimmedText,
      images: [...images],
    },
  ];
}

function dequeueVisionMessage(queue) {
  if (queue.length === 0) {
    return { next: null, remaining: [] };
  }

  return {
    next: queue[0],
    remaining: queue.slice(1),
  };
}

test('enqueueVisionMessage keeps attached images', () => {
  const queue = enqueueVisionMessage([], ' 图中有几辆车 ', ['img-a', 'img-b']);

  assert.deepEqual(queue, [
    {
      text: '图中有几辆车',
      images: ['img-a', 'img-b'],
    },
  ]);
});

test('enqueueVisionMessage ignores empty text without images', () => {
  const queue = enqueueVisionMessage([], '   ', []);

  assert.deepEqual(queue, []);
});

test('dequeueVisionMessage returns next item and remaining queue', () => {
  const { next, remaining } = dequeueVisionMessage([
    { text: '第一条', images: ['img-1'] },
    { text: '第二条', images: [] },
  ]);

  assert.deepEqual(next, { text: '第一条', images: ['img-1'] });
  assert.deepEqual(remaining, [{ text: '第二条', images: [] }]);
});
